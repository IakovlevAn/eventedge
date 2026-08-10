from __future__ import annotations

import asyncio
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from eventedge.market import (
    MAX_DAILY_CANDLES,
    MAX_INTRADAY_CANDLES,
    MoexMarketDataClient,
    scenario_range,
)


def fake_moex_request(url: str, params: dict[str, object]) -> dict[str, Any]:
    if url.endswith("/candles.json"):
        return {
            "candles": {
                "columns": ["begin", "open", "close", "high", "low", "value", "volume"],
                "data": [
                    ["2026-08-04 00:00:00", 98, 100, 101, 97, 300_000_000, 3_000_000],
                    ["2026-08-05 00:00:00", 100, 102, 103, 99, 320_000_000, 3_100_000],
                    ["2026-08-06 00:00:00", 102, 101, 103, 100, 280_000_000, 2_800_000],
                    ["2026-08-07 00:00:00", 101, 104, 105, 100, 410_000_000, 4_000_000],
                ],
            }
        }
    assert params["iss.only"] == "securities,marketdata"
    return {
        "securities": {
            "columns": ["SECID", "SHORTNAME", "LOTSIZE"],
            "data": [["SBER", "Сбербанк", 1]],
        },
        "marketdata": {
            "columns": [
                "SECID",
                "LAST",
                "LASTTOPREVPRICE",
                "VALTODAY",
                "VOLTODAY",
                "UPDATETIME",
                "LCURRENTPRICE",
            ],
            "data": [["SBER", 104.2, 1.17, 450_000_000, 4_300_000, "18:49:10", 104.1]],
        },
    }


def test_snapshot_normalizes_moex_market_data() -> None:
    client = MoexMarketDataClient(requester=fake_moex_request)

    result = asyncio.run(client.snapshot("sber"))

    assert result["ticker"] == "SBER"
    assert result["name"] == "Сбербанк"
    assert result["last_price"] == "104.2"
    assert result["daily_change_pct"] == 1.17
    assert result["liquidity_status"] == "sufficient"
    assert result["daily_volatility_pct"] is not None
    assert result["annualized_volatility_pct"] is not None
    assert len(result["candles"]) == 4
    assert result["observed_at"] == "2026-08-07T15:49:10Z"


def test_snapshot_keeps_three_month_chart_window() -> None:
    def many_candles_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        if not url.endswith("/candles.json"):
            return fake_moex_request(url, params)
        first_day = date(2026, 5, 1)
        rows = []
        for index in range(MAX_DAILY_CANDLES + 8):
            day = first_day + timedelta(days=index)
            close = 100 + index
            rows.append(
                [
                    f"{day.isoformat()} 00:00:00",
                    close - 1,
                    close,
                    close + 1,
                    close - 2,
                    300_000_000 + index,
                    3_000_000 + index,
                ]
            )
        return {
            "candles": {
                "columns": ["begin", "open", "close", "high", "low", "value", "volume"],
                "data": rows,
            }
        }

    client = MoexMarketDataClient(requester=many_candles_request)

    result = asyncio.run(client.snapshot("SBER"))

    assert len(result["candles"]) == MAX_DAILY_CANDLES
    assert result["candles"][0]["close"] == 108.0
    assert result["candles"][-1]["close"] == 173.0


def test_intraday_candles_keep_moscow_time_and_interval() -> None:
    captured: dict[str, object] = {}

    def intraday_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        captured.update(params)
        return {
            "candles": {
                "columns": ["begin", "open", "close", "high", "low", "value", "volume"],
                "data": [
                    ["2026-08-07 10:00:00", 100, 101, 102, 99, 10_000_000, 100_000],
                    ["2026-08-07 10:10:00", 101, 100.5, 101.5, 100, 8_000_000, 80_000],
                ],
            }
        }

    client = MoexMarketDataClient(requester=intraday_request)

    result = asyncio.run(client.candles("sber", interval=10, lookback_days=14))

    assert captured["interval"] == 10
    assert result["ticker"] == "SBER"
    assert result["interval_minutes"] == 10
    assert result["candles"][0]["begin"] == "2026-08-07T07:00:00Z"
    assert result["candles"][-1]["close"] == 100.5


def test_context_signal_candles_use_moex_index_benchmark() -> None:
    captured: dict[str, object] = {}

    def index_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        captured["url"] = url
        return {
            "candles": {
                "columns": ["begin", "open", "close", "high", "low", "value", "volume"],
                "data": [
                    ["2026-08-10 10:00:00", 6000, 6010, 6020, 5990, 0, 0],
                ],
            }
        }

    client = MoexMarketDataClient(requester=index_request)
    result = asyncio.run(client.candles("RUOILGAS", interval=10, lookback_days=14))

    assert captured["url"] == (
        "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/"
        "securities/MOEXOG/candles.json"
    )
    assert result["ticker"] == "RUOILGAS"
    assert result["benchmark_ticker"] == "MOEXOG"


def test_intraday_candles_keep_full_eval_window_across_pages() -> None:
    starts: list[int] = []
    first_candle = datetime(2026, 7, 27, 10)
    all_rows = []
    for index in range(1200):
        begin = first_candle + timedelta(minutes=index * 10)
        close = 100 + index / 100
        all_rows.append(
            [
                begin.strftime("%Y-%m-%d %H:%M:%S"),
                close - 0.1,
                close,
                close + 0.2,
                close - 0.2,
                10_000_000 + index,
                100_000 + index,
            ]
        )

    def paged_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        assert url.endswith("/candles.json")
        start = int(params["start"])
        starts.append(start)
        return {
            "candles": {
                "columns": ["begin", "open", "close", "high", "low", "value", "volume"],
                "data": all_rows[start : start + 500],
            }
        }

    client = MoexMarketDataClient(requester=paged_request)

    result = asyncio.run(client.candles("SBER", interval=10, lookback_days=14))

    assert starts == [0, 500, 1000]
    assert len(result["candles"]) == 1200
    assert len(result["candles"]) <= MAX_INTRADAY_CANDLES
    assert result["candles"][0]["close"] == 100.0
    assert result["candles"][-1]["close"] == 111.99


def test_identical_concurrent_snapshots_share_one_load() -> None:
    calls = 0
    calls_lock = threading.Lock()

    def slow_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.01)
        return fake_moex_request(url, params)

    async def load() -> tuple[dict[str, object], dict[str, object]]:
        client = MoexMarketDataClient(requester=slow_request)
        first, second = await asyncio.gather(
            client.snapshot("SBER"),
            client.snapshot("sber"),
        )
        return first, second

    first, second = asyncio.run(load())

    assert first == second
    assert calls == 2


def test_market_loads_limit_parallel_external_requests() -> None:
    active = 0
    peak = 0
    calls_lock = threading.Lock()

    def measured_request(url: str, params: dict[str, object]) -> dict[str, Any]:
        nonlocal active, peak
        with calls_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        result = fake_moex_request(url, params)
        with calls_lock:
            active -= 1
        return result

    async def load() -> None:
        client = MoexMarketDataClient(
            requester=measured_request,
            max_concurrent_requests=2,
        )
        await asyncio.gather(*(client.snapshot(ticker) for ticker in ("SBER", "LKOH", "YDEX")))

    asyncio.run(load())

    assert peak <= 2


def test_directional_scenario_is_a_range_not_a_price_target() -> None:
    result = scenario_range(
        {"daily_volatility_pct": 1.5},
        direction="up",
        score=42.7,
        confidence=0.76,
        horizon_value=3,
        horizon_unit="trading_days",
    )

    assert result is not None
    assert 0 < result["low_pct"] < result["high_pct"]
    assert result["label"] == "Сценарный диапазон, не таргет"


def test_scenario_is_unavailable_without_price_history() -> None:
    assert (
        scenario_range(
            {"daily_volatility_pct": None},
            direction="down",
            score=-30,
            confidence=0.7,
            horizon_value=3,
            horizon_unit="calendar_days",
        )
        is None
    )
