from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

MOEX_ISS_BASE_URL = "https://iss.moex.com/iss"
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_DAILY_CANDLES = 66
MAX_INTRADAY_CANDLES = 1500

# Context signals use product codes rather than exchange tickers. Evals measure
# them against explicit MOEX index proxies instead of silently dropping them.
EVALUATION_INDEX_BENCHMARKS: dict[str, str] = {
    "RUEQ": "IMOEX",
    "RURETAIL": "MOEXCN",
    "RUFIN": "MOEXFN",
    "RUOILGAS": "MOEXOG",
    "RUMETALS": "MOEXMM",
    "RUTECH": "MOEXINN",
    # MOEX has no dedicated agriculture index. Use the broad equity index as
    # an explicit fallback until EventEdge maintains its own sector basket.
    "RUAGRI": "IMOEX",
    "RUTRANS": "MOEXTN",
    "RUPOWER": "MOEXEU",
}

# Product history keeps the canonical ticker used when a signal was created.
# Resolve renamed/redomiciled securities only at the MOEX boundary so old
# signal IDs, API filters and model epochs remain immutable.
MOEX_SECURITY_ALIASES: dict[str, str] = {
    "FIXP": "FIXR",
}


class InstrumentNotFoundError(LookupError):
    pass


class MarketDataUnavailableError(RuntimeError):
    pass


def _request_json(url: str, params: dict[str, object]) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "EventEdge/0.1 (+https://github.com/IakovlevAn/eventedge)",
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataUnavailableError("MOEX ISS request failed") from exc
    if not isinstance(payload, dict):
        raise MarketDataUnavailableError("MOEX ISS returned an invalid response")
    return payload


def _rows(payload: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    table = payload.get(table_name)
    if not isinstance(table, dict):
        return []
    columns = table.get("columns")
    data = table.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        return []
    return [dict(zip(columns, row, strict=False)) for row in data if isinstance(row, list)]


def _utc_timestamp(day: str, clock: str | None = None) -> str:
    if clock:
        date_part = day.split(" ", 1)[0]
        value = f"{date_part}T{clock}"
    else:
        value = day.replace(" ", "T")
    local = datetime.fromisoformat(value).replace(tzinfo=MOSCOW_TIMEZONE)
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _realized_volatility(closes: list[float]) -> tuple[float | None, float | None]:
    if len(closes) < 3:
        return None, None
    returns = [
        math.log(current / previous)
        for previous, current in zip(closes[:-1], closes[1:], strict=True)
    ]
    daily = statistics.stdev(returns) * 100
    annualized = daily * math.sqrt(252)
    return round(daily, 2), round(annualized, 2)


def _liquidity_status(value_rub: float | None) -> str:
    if value_rub is None or value_rub <= 0:
        return "unavailable"
    if value_rub >= 100_000_000:
        return "sufficient"
    return "limited"


class MoexMarketDataClient:
    def __init__(
        self,
        *,
        requester: Callable[[str, dict[str, object]], dict[str, Any]] | None = None,
        cache_ttl_seconds: float = 30.0,
        max_concurrent_requests: int = 6,
    ) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        self._requester = requester or _request_json
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)
        self._inflight: dict[str, asyncio.Task[dict[str, object]]] = {}

    async def _cached_load(
        self,
        cache_key: str,
        loader: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl_seconds:
            return cached[1]

        existing = self._inflight.get(cache_key)
        if existing is not None:
            return await asyncio.shield(existing)

        async def run() -> dict[str, object]:
            async with self._request_slots:
                result = await asyncio.to_thread(loader)
            self._cache[cache_key] = (time.monotonic(), result)
            return result

        task = asyncio.create_task(run())
        self._inflight[cache_key] = task

        def clear_inflight(done: asyncio.Task[dict[str, object]]) -> None:
            if self._inflight.get(cache_key) is done:
                self._inflight.pop(cache_key, None)

        task.add_done_callback(clear_inflight)
        return await asyncio.shield(task)

    async def snapshot(self, ticker: str) -> dict[str, object]:
        normalized = ticker.upper()
        return await self._cached_load(
            f"snapshot:{normalized}",
            lambda: self._load_snapshot(normalized),
        )

    async def candles(
        self,
        ticker: str,
        *,
        interval: int = 10,
        lookback_days: int = 14,
    ) -> dict[str, object]:
        normalized = ticker.upper()
        cache_key = f"candles:{normalized}:{interval}:{lookback_days}"
        return await self._cached_load(
            cache_key,
            lambda: self._load_candles(normalized, interval, lookback_days),
        )

    def _load_candles(
        self,
        ticker: str,
        interval: int,
        lookback_days: int,
    ) -> dict[str, object]:
        benchmark_ticker = EVALUATION_INDEX_BENCHMARKS.get(
            ticker,
            MOEX_SECURITY_ALIASES.get(ticker, ticker),
        )
        encoded_ticker = quote(benchmark_ticker, safe="")
        from_date = (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()
        if ticker in EVALUATION_INDEX_BENCHMARKS:
            candles_url = (
                f"{MOEX_ISS_BASE_URL}/engines/stock/markets/index/boards/SNDX/"
                f"securities/{encoded_ticker}/candles.json"
            )
        else:
            candles_url = (
                f"{MOEX_ISS_BASE_URL}/engines/stock/markets/shares/boards/TQBR/"
                f"securities/{encoded_ticker}/candles.json"
            )
        rows: list[dict[str, Any]] = []
        page_size = 500
        for start in range(0, 1500, page_size):
            payload = self._requester(
                candles_url,
                {
                    "from": from_date,
                    "interval": interval,
                    "start": start,
                    "iss.meta": "off",
                    "candles.columns": "begin,open,close,high,low,value,volume",
                },
            )
            page = _rows(payload, "candles")
            rows.extend(page)
            if len(page) < page_size:
                break
        rows = rows[-MAX_INTRADAY_CANDLES:]
        candles = [
            {
                "begin": _utc_timestamp(str(row["begin"])),
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "value_rub": round(float(row["value"]), 2),
                "volume_shares": int(float(row["volume"])),
            }
            for row in rows
            if all(row.get(field) is not None for field in ("open", "close", "high", "low"))
        ]
        if not candles:
            raise MarketDataUnavailableError("MOEX ISS returned no intraday candles")
        return {
            "ticker": ticker,
            "benchmark_ticker": benchmark_ticker,
            "interval_minutes": interval,
            "candles": candles,
            "observed_at": candles[-1]["begin"],
            "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
        }

    def _load_snapshot(self, ticker: str) -> dict[str, object]:
        market_ticker = MOEX_SECURITY_ALIASES.get(ticker, ticker)
        encoded_ticker = quote(market_ticker, safe="")
        security_url = (
            f"{MOEX_ISS_BASE_URL}/engines/stock/markets/shares/boards/TQBR/"
            f"securities/{encoded_ticker}.json"
        )
        security_payload = self._requester(
            security_url,
            {
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
                "securities.columns": "SECID,SHORTNAME,LOTSIZE",
                "marketdata.columns": (
                    "SECID,LAST,LASTTOPREVPRICE,VALTODAY,VOLTODAY,UPDATETIME,"
                    "LCURRENTPRICE"
                ),
            },
        )
        securities = _rows(security_payload, "securities")
        market_rows = _rows(security_payload, "marketdata")
        if not securities:
            raise InstrumentNotFoundError(ticker)

        from_date = (datetime.now(UTC) - timedelta(days=90)).date().isoformat()
        candles_url = (
            f"{MOEX_ISS_BASE_URL}/engines/stock/markets/shares/boards/TQBR/"
            f"securities/{encoded_ticker}/candles.json"
        )
        candles_payload = self._requester(
            candles_url,
            {
                "from": from_date,
                "interval": 24,
                "iss.meta": "off",
                "candles.columns": "begin,open,close,high,low,value,volume",
            },
        )
        candle_rows = _rows(candles_payload, "candles")[-MAX_DAILY_CANDLES:]
        if not candle_rows:
            raise MarketDataUnavailableError("MOEX ISS returned no daily candles")

        market = market_rows[0] if market_rows else {}
        security = securities[0]
        last_candle = candle_rows[-1]
        last_price = market.get("LAST") or market.get("LCURRENTPRICE") or last_candle.get("close")
        if last_price is None:
            raise MarketDataUnavailableError("MOEX ISS returned no current price")

        closes = [float(row["close"]) for row in candle_rows if row.get("close")]
        daily_volatility_pct, annualized_volatility_pct = _realized_volatility(closes)
        value_rub = float(market["VALTODAY"]) if market.get("VALTODAY") is not None else None
        observed_at = _utc_timestamp(
            str(last_candle["begin"]),
            str(market["UPDATETIME"]) if market.get("UPDATETIME") else None,
        )
        candles = [
            {
                "begin": _utc_timestamp(str(row["begin"])),
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "value_rub": round(float(row["value"]), 2),
                "volume_shares": int(float(row["volume"])),
            }
            for row in candle_rows
            if all(row.get(field) is not None for field in ("open", "close", "high", "low"))
        ]
        return {
            "ticker": ticker,
            "market_ticker": market_ticker,
            "name": str(security.get("SHORTNAME") or ticker),
            "last_price": str(last_price),
            "currency": "RUB",
            "observed_at": observed_at,
            "daily_change_pct": (
                round(float(market["LASTTOPREVPRICE"]), 2)
                if market.get("LASTTOPREVPRICE") is not None
                else None
            ),
            "volume_shares": (
                int(float(market["VOLTODAY"])) if market.get("VOLTODAY") is not None else None
            ),
            "value_rub": round(value_rub, 2) if value_rub is not None else None,
            "lot_size": int(security.get("LOTSIZE") or 1),
            "liquidity_status": _liquidity_status(value_rub),
            "daily_volatility_pct": daily_volatility_pct,
            "annualized_volatility_pct": annualized_volatility_pct,
            "candles": candles,
            "source": {
                "name": "MOEX ISS",
                "url": "https://iss.moex.com/iss/",
            },
        }


def scenario_range(
    market: dict[str, object],
    *,
    direction: str,
    score: float,
    confidence: float,
    horizon_value: int,
    horizon_unit: str,
) -> dict[str, object] | None:
    daily_volatility = market.get("daily_volatility_pct")
    if not isinstance(daily_volatility, (int, float)):
        return None
    if horizon_unit == "hours":
        horizon_days = max(1, math.ceil(horizon_value / 24))
    else:
        horizon_days = max(1, horizon_value)

    base_move = float(daily_volatility) * math.sqrt(horizon_days)
    strength = min(abs(score) / 100, 1)
    calibrated_confidence = min(max(confidence, 0), 1)
    if direction == "neutral":
        low_pct = -base_move * 0.35
        high_pct = base_move * 0.35
    else:
        sign = 1 if direction == "up" else -1
        center = sign * base_move * (0.55 + 0.45 * strength)
        half_width = base_move * (0.25 + 0.25 * (1 - calibrated_confidence))
        low_pct = center - half_width
        high_pct = center + half_width

    return {
        "low_pct": round(min(low_pct, high_pct), 2),
        "high_pct": round(max(low_pct, high_pct), 2),
        "horizon_trading_days": horizon_days,
        "kind": "volatility_scenario",
        "label": "Сценарный диапазон, не таргет",
        "method": "realized_volatility_x_signal_strength_v1",
    }


def volatility_scenario_range(
    market: dict[str, object],
    *,
    horizon_value: int,
    horizon_unit: str,
) -> dict[str, object] | None:
    """Return a symmetric volatility envelope without a directional signal input."""
    daily_volatility = market.get("daily_volatility_pct")
    if not isinstance(daily_volatility, (int, float)):
        return None
    if horizon_unit == "hours":
        horizon_days = max(1, math.ceil(horizon_value / 24))
    else:
        horizon_days = max(1, horizon_value)
    expected_move = float(daily_volatility) * math.sqrt(horizon_days)
    return {
        "low_pct": round(-expected_move, 2),
        "high_pct": round(expected_move, 2),
        "horizon_trading_days": horizon_days,
        "kind": "volatility_scenario",
        "label": "Симметричный диапазон волатильности, не прогноз",
        "method": "realized_volatility_sqrt_time_v1",
    }
