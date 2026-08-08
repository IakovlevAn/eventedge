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
    date_part = day.split(" ", 1)[0]
    local = datetime.fromisoformat(f"{date_part}T{clock or '00:00:00'}").replace(
        tzinfo=MOSCOW_TIMEZONE
    )
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
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._requester = requester or _request_json
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}

    async def snapshot(self, ticker: str) -> dict[str, object]:
        normalized = ticker.upper()
        cached = self._cache.get(normalized)
        if cached and time.monotonic() - cached[0] < self._cache_ttl_seconds:
            return cached[1]
        result = await asyncio.to_thread(self._load_snapshot, normalized)
        self._cache[normalized] = (time.monotonic(), result)
        return result

    def _load_snapshot(self, ticker: str) -> dict[str, object]:
        encoded_ticker = quote(ticker, safe="")
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
