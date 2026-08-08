from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from eventedge.market import MAX_DAILY_CANDLES, MoexMarketDataClient, scenario_range


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
