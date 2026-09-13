from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from eventedge_research.research_artifacts import file_sha256, write_jsonl
from eventedge_research.signal_dataset import SignalLabelSource
from eventedge_research.signal_dataset_builder import MarketOpenObservation

MOEX_TIMEZONE = ZoneInfo("Europe/Moscow")
MOEX_ISS_PAGE_SIZE = 500
MOEX_CONNECT_TIMEOUT_SECONDS = 15.0
MOEX_READ_TIMEOUT_SECONDS = 30.0
_TICKER_PATTERN = re.compile(r"^[A-Z0-9._-]{1,32}$")


class MoexCandleSource(StrEnum):
    """Fixed public ISS candle sources allowed by the research downloader."""

    TQBR_SHARES = "tqbr_shares"
    SNDX_INDEX = "sndx_index"


_SOURCE_CONFIG = {
    MoexCandleSource.TQBR_SHARES: {
        "market": "shares",
        "board": "TQBR",
        "provider_prefix": "moex-iss-tqbr",
    },
    MoexCandleSource.SNDX_INDEX: {
        "market": "index",
        "board": "SNDX",
        "provider_prefix": "moex-iss-sndx-index",
    },
}


class HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        timeout: tuple[float, float],
    ) -> HttpResponse: ...


def download_moex_market_opens(
    *,
    tickers: Iterable[str],
    from_date: date,
    till_date: date,
    interval_minutes: int,
    output_path: Path,
    session: HttpSession | None = None,
    request_delay_seconds: float = 0.05,
    maximum_pages_per_ticker: int = 500,
    maximum_rows_per_ticker: int = 100_000,
    maximum_attempts: int = 4,
    source: MoexCandleSource = MoexCandleSource.TQBR_SHARES,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    run_deadline_monotonic: float | None = None,
    security_ids: Mapping[str, str] | None = None,
    allow_empty: bool = False,
) -> dict[str, object]:
    """Download bounded MOEX ISS candle opens and atomically write canonical JSONL."""
    normalized_tickers = tuple(sorted({_validate_ticker(ticker) for ticker in tickers}))
    normalized_security_ids = _normalize_security_ids(
        normalized_tickers,
        security_ids or {},
    )
    _validate_download_options(
        tickers=normalized_tickers,
        from_date=from_date,
        till_date=till_date,
        interval_minutes=interval_minutes,
        request_delay_seconds=request_delay_seconds,
        maximum_pages_per_ticker=maximum_pages_per_ticker,
        maximum_rows_per_ticker=maximum_rows_per_ticker,
        maximum_attempts=maximum_attempts,
    )
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite market opens: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    active_session = session or requests.Session()
    if isinstance(active_session, requests.Session):
        active_session.headers.update({"User-Agent": "EventEdge research market-label builder/1.0"})

    total_rows = 0
    ticker_reports: dict[str, object] = {}

    def observation_rows() -> Iterable[dict[str, object]]:
        nonlocal total_rows
        for ticker_index, ticker in enumerate(normalized_tickers):
            _ensure_within_run_deadline(
                monotonic=monotonic,
                run_deadline_monotonic=run_deadline_monotonic,
            )
            security_id = normalized_security_ids[ticker]
            observations, request_count, duplicate_rows = _download_ticker(
                active_session,
                ticker=ticker,
                security_id=security_id,
                from_date=from_date,
                till_date=till_date,
                interval_minutes=interval_minutes,
                request_delay_seconds=request_delay_seconds,
                maximum_pages=maximum_pages_per_ticker,
                maximum_rows=maximum_rows_per_ticker,
                maximum_attempts=maximum_attempts,
                source=source,
                sleep=sleep,
                monotonic=monotonic,
                run_deadline_monotonic=run_deadline_monotonic,
            )
            total_rows += len(observations)
            ticker_reports[ticker] = {
                "security_id": security_id,
                "rows": len(observations),
                "requests": request_count,
                "duplicate_rows_ignored": duplicate_rows,
                "minimum_at": observations[0].at.isoformat() if observations else None,
                "maximum_at": observations[-1].at.isoformat() if observations else None,
            }
            for observation in observations:
                yield observation.model_dump(mode="json")
            if ticker_index + 1 < len(normalized_tickers) and request_delay_seconds:
                _sleep_with_run_deadline(
                    request_delay_seconds,
                    sleep=sleep,
                    monotonic=monotonic,
                    run_deadline_monotonic=run_deadline_monotonic,
                )
        if total_rows == 0 and not allow_empty:
            raise ValueError("MOEX ISS returned no market opens for any ticker")

    try:
        write_jsonl(
            output_path,
            observation_rows(),
            maximum_rows=maximum_rows_per_ticker * len(normalized_tickers),
        )
    finally:
        if session is None and isinstance(active_session, requests.Session):
            active_session.close()

    return {
        "schema_version": "moex-market-open-download-1.0",
        "provider_id": _provider_id(source, interval_minutes),
        "source": source.value,
        "market": _SOURCE_CONFIG[source]["market"],
        "board": _SOURCE_CONFIG[source]["board"],
        "interval_minutes": interval_minutes,
        "timezone": str(MOEX_TIMEZONE),
        "from_date": from_date.isoformat(),
        "till_date": till_date.isoformat(),
        "tickers": len(normalized_tickers),
        "tickers_without_data": [
            ticker
            for ticker, report in ticker_reports.items()
            if isinstance(report, dict) and report["rows"] == 0
        ],
        "rows": total_rows,
        "sha256": file_sha256(output_path),
        "ticker_reports": ticker_reports,
    }


def _download_ticker(
    session: HttpSession,
    *,
    ticker: str,
    security_id: str,
    from_date: date,
    till_date: date,
    interval_minutes: int,
    request_delay_seconds: float,
    maximum_pages: int,
    maximum_rows: int,
    maximum_attempts: int,
    source: MoexCandleSource,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    run_deadline_monotonic: float | None,
) -> tuple[list[MarketOpenObservation], int, int]:
    observations: list[MarketOpenObservation] = []
    opens_by_at: dict[datetime, float] = {}
    duplicate_rows = 0
    start = 0
    request_count = 0
    for page_index in range(maximum_pages):
        if page_index and request_delay_seconds:
            _sleep_with_run_deadline(
                request_delay_seconds,
                sleep=sleep,
                monotonic=monotonic,
                run_deadline_monotonic=run_deadline_monotonic,
            )
        payload, attempts = _request_page(
            session,
            security_id=security_id,
            from_date=from_date,
            till_date=till_date,
            interval_minutes=interval_minutes,
            start=start,
            maximum_attempts=maximum_attempts,
            source=source,
            sleep=sleep,
            monotonic=monotonic,
            run_deadline_monotonic=run_deadline_monotonic,
        )
        request_count += attempts
        rows = _candle_rows(payload)
        if not rows:
            break
        for row in rows:
            observation = _market_open(
                row,
                ticker=ticker,
                interval_minutes=interval_minutes,
                source=source,
            )
            previous_open = opens_by_at.get(observation.at)
            if previous_open is not None:
                if previous_open != observation.open:
                    raise ValueError(
                        f"MOEX ISS returned conflicting candles: {ticker}/{observation.at}"
                    )
                duplicate_rows += 1
                continue
            opens_by_at[observation.at] = observation.open
            observations.append(observation)
            if len(observations) > maximum_rows:
                raise ValueError(f"MOEX ISS row limit exceeded for {ticker}")
        start += len(rows)
        if len(rows) < MOEX_ISS_PAGE_SIZE:
            break
    else:
        raise ValueError(f"MOEX ISS page limit exceeded for {ticker}")
    observations.sort(key=lambda observation: observation.at)
    return observations, request_count, duplicate_rows


def _request_page(
    session: HttpSession,
    *,
    security_id: str,
    from_date: date,
    till_date: date,
    interval_minutes: int,
    start: int,
    maximum_attempts: int,
    source: MoexCandleSource,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    run_deadline_monotonic: float | None,
) -> tuple[object, int]:
    source_config = _SOURCE_CONFIG[source]
    url = (
        "https://iss.moex.com/iss/engines/stock/markets/"
        f"{source_config['market']}/boards/{source_config['board']}/"
        f"securities/{security_id}/candles.json"
    )
    parameters: dict[str, object] = {
        "from": from_date.isoformat(),
        "till": till_date.isoformat(),
        "interval": interval_minutes,
        "start": start,
        "iss.meta": "off",
        "iss.only": "candles",
        "candles.columns": "begin,open",
    }
    for attempt in range(1, maximum_attempts + 1):
        request_timeout = _remaining_request_timeout(
            monotonic=monotonic,
            run_deadline_monotonic=run_deadline_monotonic,
        )
        try:
            response = session.get(
                url,
                params=parameters,
                timeout=request_timeout,
            )
            response.raise_for_status()
            return response.json(), attempt
        except (requests.RequestException, ValueError):
            if attempt == maximum_attempts:
                raise
            _sleep_with_run_deadline(
                min(2 ** (attempt - 1), 8),
                sleep=sleep,
                monotonic=monotonic,
                run_deadline_monotonic=run_deadline_monotonic,
            )
    raise RuntimeError("unreachable MOEX ISS retry state")


def _ensure_within_run_deadline(
    *,
    monotonic: Callable[[], float],
    run_deadline_monotonic: float | None,
) -> None:
    if run_deadline_monotonic is not None and monotonic() >= run_deadline_monotonic:
        raise TimeoutError("MOEX ISS download reached its wall-clock limit")


def _remaining_request_timeout(
    *,
    monotonic: Callable[[], float],
    run_deadline_monotonic: float | None,
) -> tuple[float, float]:
    if run_deadline_monotonic is None:
        return (MOEX_CONNECT_TIMEOUT_SECONDS, MOEX_READ_TIMEOUT_SECONDS)
    remaining = run_deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("MOEX ISS download reached its wall-clock limit")
    connect_timeout = min(MOEX_CONNECT_TIMEOUT_SECONDS, remaining / 2)
    read_timeout = min(MOEX_READ_TIMEOUT_SECONDS, remaining - connect_timeout)
    return (connect_timeout, read_timeout)


def _sleep_with_run_deadline(
    seconds: float,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    run_deadline_monotonic: float | None,
) -> None:
    if run_deadline_monotonic is None:
        sleep(seconds)
        return
    remaining = run_deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("MOEX ISS download reached its wall-clock limit")
    sleep(min(seconds, remaining))


def _candle_rows(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError("MOEX ISS response must be a JSON object")
    candles = payload.get("candles")
    if not isinstance(candles, dict):
        raise ValueError("MOEX ISS response does not contain candles")
    columns = candles.get("columns")
    data = candles.get("data")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise ValueError("MOEX ISS candle columns are invalid")
    if not isinstance(data, list):
        raise ValueError("MOEX ISS candle data is invalid")
    if "begin" not in columns or "open" not in columns:
        raise ValueError("MOEX ISS candles must contain begin and open")
    rows = []
    for values in data:
        if not isinstance(values, list) or len(values) != len(columns):
            raise ValueError("MOEX ISS candle row does not match columns")
        rows.append(dict(zip(columns, values, strict=True)))
    return rows


def _market_open(
    row: Mapping[str, object],
    *,
    ticker: str,
    interval_minutes: int,
    source: MoexCandleSource,
) -> MarketOpenObservation:
    begin = row.get("begin")
    open_price = row.get("open")
    if not isinstance(begin, str):
        raise ValueError("MOEX ISS candle begin must be a string")
    if not isinstance(open_price, (int, float)) or isinstance(open_price, bool):
        raise ValueError("MOEX ISS candle open must be numeric")
    local_at = datetime.fromisoformat(begin)
    if local_at.tzinfo is not None:
        raise ValueError("MOEX ISS candle begin unexpectedly contains a timezone")
    return MarketOpenObservation(
        ticker=ticker,
        at=local_at.replace(tzinfo=MOEX_TIMEZONE).astimezone(UTC),
        open=float(open_price),
        provider_id=_provider_id(source, interval_minutes),
        adjusted=False,
        label_source=SignalLabelSource.MARKET_OUTCOME,
    )


def _validate_download_options(
    *,
    tickers: tuple[str, ...],
    from_date: date,
    till_date: date,
    interval_minutes: int,
    request_delay_seconds: float,
    maximum_pages_per_ticker: int,
    maximum_rows_per_ticker: int,
    maximum_attempts: int,
) -> None:
    if not tickers:
        raise ValueError("at least one ticker is required")
    if from_date > till_date:
        raise ValueError("from_date cannot be later than till_date")
    if (till_date - from_date).days > 370:
        raise ValueError("MOEX ISS download window cannot exceed 370 days")
    if interval_minutes not in {1, 10}:
        raise ValueError("interval_minutes must be 1 or 10")
    if not 0 <= request_delay_seconds <= 10:
        raise ValueError("request_delay_seconds must be between 0 and 10")
    if not 1 <= maximum_pages_per_ticker <= 2_000:
        raise ValueError("maximum_pages_per_ticker must be between 1 and 2000")
    if not 1 <= maximum_rows_per_ticker <= 1_000_000:
        raise ValueError("maximum_rows_per_ticker must be between 1 and 1000000")
    if not 1 <= maximum_attempts <= 10:
        raise ValueError("maximum_attempts must be between 1 and 10")


def _validate_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not _TICKER_PATTERN.fullmatch(ticker):
        raise ValueError(f"invalid MOEX ticker: {value}")
    return ticker


def _normalize_security_ids(
    tickers: tuple[str, ...],
    values: Mapping[str, str],
) -> dict[str, str]:
    normalized_values = {
        _validate_ticker(ticker): _validate_ticker(security_id)
        for ticker, security_id in values.items()
    }
    unknown = set(normalized_values) - set(tickers)
    if unknown:
        raise ValueError(
            f"security_id mapping contains unknown tickers: {', '.join(sorted(unknown))}"
        )
    return {ticker: normalized_values.get(ticker, ticker) for ticker in tickers}


def _provider_id(source: MoexCandleSource, interval_minutes: int) -> str:
    return f"{_SOURCE_CONFIG[source]['provider_prefix']}-{interval_minutes}m"
