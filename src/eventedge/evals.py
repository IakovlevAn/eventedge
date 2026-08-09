from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from eventedge.analysis import DEFAULT_MOEX_ALIASES
from eventedge.storage import NewsRecord, SignalRecord, to_rfc3339

ASSESSMENT_MODEL_VERSION = "hybrid-market-0.1.0"
ASSESSMENT_CONFIG_VERSION = 1
POSITIVE_THRESHOLD = 18.0
NEGATIVE_THRESHOLD = -18.0
REPORT_MARKERS = (
    "отчётност",
    "отчетност",
    "чистая прибыль",
    "выручк",
    "ebitda",
    "мсфо",
    "рсбу",
    "финансовые результаты",
)
POSITIVE_REPORT_TERMS = (
    "выше ожиданий",
    "лучше ожиданий",
    "вырос",
    "увеличил",
    "рост",
    "превысил",
    "улучшил",
    "рекорд",
)
NEGATIVE_REPORT_TERMS = (
    "ниже ожиданий",
    "хуже ожиданий",
    "снизил",
    "сократил",
    "падени",
    "убыток",
    "ухудшил",
)


def _clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _direction(score: float) -> str:
    if score >= POSITIVE_THRESHOLD:
        return "up"
    if score <= NEGATIVE_THRESHOLD:
        return "down"
    return "neutral"


def _bias_direction(score: float) -> str:
    if score >= 5:
        return "up"
    if score <= -5:
        return "down"
    return "neutral"


def _action(direction: str) -> str:
    if direction == "up":
        return "consider_buy"
    if direction == "down":
        return "review_position"
    return "no_action"


def _news_matches_ticker(item: NewsRecord, ticker: str) -> bool:
    metadata_tickers = item.source_metadata.get("tickers", [])
    if isinstance(metadata_tickers, (list, tuple)) and ticker in {
        str(value).upper() for value in metadata_tickers
    }:
        return True
    haystack = f"{item.title} {item.content}".lower()
    return any(alias.lower() in haystack for alias in DEFAULT_MOEX_ALIASES.get(ticker, ()))


def _report_factor(ticker: str, news: Iterable[NewsRecord]) -> dict[str, object]:
    candidates = []
    for item in news:
        text = f"{item.title} {item.content}".lower()
        if _news_matches_ticker(item, ticker) and any(marker in text for marker in REPORT_MARKERS):
            candidates.append((item, text))
    if not candidates:
        return {
            "code": "reporting",
            "label": "Данные отчётности",
            "value": None,
            "display_value": "нет свежих данных",
            "contribution": 0.0,
            "role": "direction",
            "available": False,
            "source_news_id": None,
        }

    item, text = max(candidates, key=lambda pair: pair[0].published_at)
    positive = sum(term in text for term in POSITIVE_REPORT_TERMS)
    negative = sum(term in text for term in NEGATIVE_REPORT_TERMS)
    balance = positive - negative
    contribution = _clip(balance * 7.0, -20.0, 20.0)
    return {
        "code": "reporting",
        "label": "Данные отчётности",
        "value": balance,
        "display_value": item.title,
        "contribution": round(contribution, 2),
        "role": "direction",
        "available": True,
        "source_news_id": item.id,
    }


def quant_factors(
    ticker: str,
    market: dict[str, object],
    news: Iterable[NewsRecord],
) -> tuple[list[dict[str, object]], float, float]:
    candles = market.get("candles")
    rows = candles if isinstance(candles, list) else []
    closes = [
        value
        for row in rows
        if isinstance(row, dict)
        if (value := _number(row.get("close"))) is not None
    ]
    volumes = [
        value
        for row in rows
        if isinstance(row, dict)
        if (value := _number(row.get("volume_shares"))) is not None and value > 0
    ]

    one_day = _number(market.get("daily_change_pct"))
    if one_day is None and len(closes) >= 2 and closes[-2]:
        one_day = (closes[-1] / closes[-2] - 1) * 100
    five_day = None
    if len(closes) >= 6 and closes[-6]:
        five_day = (closes[-1] / closes[-6] - 1) * 100
    reaction = (one_day or 0.0) * 0.55 + (five_day or 0.0) * 0.45
    price_contribution = _clip((one_day or 0.0) / 3 * 20 + (five_day or 0.0) / 6 * 20, -40, 40)

    session_volume = _number(market.get("volume_shares"))
    historical_volumes = volumes[-21:-1] if len(volumes) > 1 else []
    if session_volume is None and volumes:
        session_volume = volumes[-1]
    volume_ratio = None
    if session_volume is not None and historical_volumes:
        median_volume = statistics.median(historical_volumes)
        if median_volume > 0:
            volume_ratio = session_volume / median_volume
    reaction_sign = 1 if reaction > 0 else -1 if reaction < 0 else 0
    volume_contribution = (
        reaction_sign * 15 * _clip((volume_ratio - 1) / 1.5, 0, 1)
        if volume_ratio is not None
        else 0.0
    )

    volatility = _number(market.get("daily_volatility_pct"))
    liquidity = str(market.get("liquidity_status") or "unavailable")
    report = _report_factor(ticker, news)
    factors = [
        {
            "code": "price_reaction",
            "label": "Реакция цены",
            "value": round(reaction, 2),
            "display_value": f"1д {one_day or 0:+.2f}% · 5д {five_day or 0:+.2f}%",
            "contribution": round(price_contribution, 2),
            "role": "direction",
            "available": one_day is not None or five_day is not None,
        },
        {
            "code": "volume",
            "label": "Объём",
            "value": round(volume_ratio, 2) if volume_ratio is not None else None,
            "display_value": (
                f"{volume_ratio:.2f}× к медиане"
                if volume_ratio is not None
                else "нет базы сравнения"
            ),
            "contribution": round(volume_contribution, 2),
            "role": "direction",
            "available": volume_ratio is not None,
        },
        {
            "code": "volatility",
            "label": "Волатильность",
            "value": round(volatility, 2) if volatility is not None else None,
            "display_value": (
                f"{volatility:.2f}% в день" if volatility is not None else "нет данных"
            ),
            "contribution": 0.0,
            "role": "confidence",
            "available": volatility is not None,
        },
        {
            "code": "liquidity",
            "label": "Ликвидность",
            "value": liquidity,
            "display_value": (
                "достаточная"
                if liquidity == "sufficient"
                else "ограниченная"
                if liquidity == "limited"
                else "нет данных"
            ),
            "contribution": 0.0,
            "role": "confidence",
            "available": liquidity != "unavailable",
        },
        report,
    ]
    score = round(sum(float(factor["contribution"]) for factor in factors), 2)
    availability = sum(bool(factor["available"]) for factor in factors) / len(factors)
    confidence = 0.40 + availability * 0.30
    if liquidity == "sufficient":
        confidence += 0.08
    elif liquidity == "limited":
        confidence -= 0.08
    if volatility is not None and volatility > 4:
        confidence -= min((volatility - 4) * 0.025, 0.12)
    return factors, score, round(_clip(confidence, 0.25, 0.85), 2)


def build_assessment(
    ticker: str,
    market: dict[str, object],
    news: Iterable[NewsRecord],
    active_signal: SignalRecord | None,
) -> dict[str, object]:
    quant, quant_score, quant_confidence = quant_factors(ticker, market, news)
    if active_signal is None:
        score = quant_score
        confidence = quant_confidence
        factors = quant
        assessment_type = "quant"
        summary = (
            "Свежего сильного news-event нет. Направление рассчитано только по цене, "
            "объёму, риску ликвидности, волатильности и доступным данным отчётности."
        )
        news_signal = None
    else:
        score = round(active_signal.score * 0.65 + quant_score * 0.35, 2)
        confidence = round(active_signal.confidence * 0.65 + quant_confidence * 0.35, 2)
        factors = [
            {
                "code": "news_signal",
                "label": "Семантика новости",
                "value": active_signal.score,
                "display_value": f"{active_signal.score:+.1f} п. news baseline",
                "contribution": round(active_signal.score * 0.65, 2),
                "role": "direction",
                "available": True,
            },
            *[
                {**factor, "contribution": round(float(factor["contribution"]) * 0.35, 2)}
                for factor in quant
            ],
        ]
        assessment_type = "hybrid"
        relation = "подтверждает" if quant_score * active_signal.score > 0 else "не подтверждает"
        summary = (
            f"Новость даёт {active_signal.score:+.1f} п.; рыночный слой {relation} её "
            f"оценкой {quant_score:+.1f} п. Итог собран фиксированной формулой 65/35."
        )
        news_signal = active_signal.as_api_dict()

    direction = _direction(score)
    return {
        "id": f"assessment_{ticker}",
        "ticker": ticker,
        "as_of": str(market.get("observed_at")),
        "status": "live",
        "assessment_type": assessment_type,
        "direction": direction,
        "bias_direction": _bias_direction(score),
        "action": _action(direction),
        "horizon": {"value": 3, "unit": "trading_days"},
        "score": score,
        "confidence": confidence,
        "summary": summary,
        "factor_contributions": factors,
        "market": {key: value for key, value in market.items() if key not in {"ticker", "name"}},
        "news_signal": news_signal,
        "model_version": ASSESSMENT_MODEL_VERSION,
        "config_version": ASSESSMENT_CONFIG_VERSION,
        "limitations": [
            "Baseline не откалиброван на point-in-time backtest.",
            "Показатель отчётности доступен только при наличии распознанного раскрытия.",
        ],
    }


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def evaluate_signal(
    signal: SignalRecord,
    candles: list[dict[str, object]],
    news: NewsRecord | None,
) -> dict[str, object]:
    rows = sorted(
        (
            (_parse_timestamp(str(item["begin"])), item)
            for item in candles
            if item.get("begin") and _number(item.get("open")) is not None
        ),
        key=lambda pair: pair[0],
    )
    base = {
        "signal_id": signal.id,
        "ticker": signal.ticker,
        "as_of": to_rfc3339(signal.as_of),
        "direction": signal.direction,
        "score": signal.score,
        "confidence": signal.confidence,
        "model_version": signal.model_version,
        "news": {
            "id": news.id,
            "title": news.title,
            "source_id": news.source_id,
            "url": news.url,
        }
        if news
        else None,
    }
    if not rows:
        return {**base, "status": "unavailable", "entry": None, "returns": {}, "verdict": None}

    entry_pair = next((pair for pair in rows if pair[0] >= signal.as_of), None)
    if entry_pair is None or entry_pair[0] - signal.as_of > timedelta(days=3):
        return {**base, "status": "unavailable", "entry": None, "returns": {}, "verdict": None}
    entry_time, entry_row = entry_pair
    entry_price = _number(entry_row.get("open"))
    assert entry_price is not None
    returns: dict[str, float | None] = {}
    target_hours = {"1h": 1, "1d": 24, "3d": 72}
    for label, hours in target_hours.items():
        target = entry_time + timedelta(hours=hours)
        target_row = next((row for timestamp, row in rows if timestamp >= target), None)
        price = _number(target_row.get("close")) if target_row else None
        returns[label] = round((price / entry_price - 1) * 100, 2) if price is not None else None
    latest_price = _number(rows[-1][1].get("close"))
    latest_return = (
        round((latest_price / entry_price - 1) * 100, 2)
        if latest_price is not None
        else None
    )
    primary = next((returns[key] for key in ("3d", "1d", "1h") if returns[key] is not None), None)
    verdict = None
    if primary is not None:
        if signal.direction == "neutral":
            verdict = abs(primary) < 0.5
        elif signal.direction == "up":
            verdict = primary > 0
        else:
            verdict = primary < 0
    status = "evaluated" if returns["3d"] is not None else "partial"
    return {
        **base,
        "status": status,
        "entry": {"at": to_rfc3339(entry_time), "price": round(entry_price, 4)},
        "returns": returns,
        "latest_price": round(latest_price, 4) if latest_price is not None else None,
        "latest_return_pct": latest_return,
        "verdict": verdict,
    }


def eval_summary(outcomes: list[dict[str, object]]) -> dict[str, object]:
    decided = [item for item in outcomes if item.get("verdict") is not None]
    signed_returns = []
    for item in decided:
        returns = item.get("returns")
        if not isinstance(returns, dict):
            continue
        value = next(
            (
                returns.get(key)
                for key in ("3d", "1d", "1h")
                if returns.get(key) is not None
            ),
            None,
        )
        if isinstance(value, (int, float)):
            sign = -1 if item["direction"] == "down" else 1
            signed_returns.append(float(value) * sign)
    return {
        "signals_total": len(outcomes),
        "evaluated": len(decided),
        "pending": sum(item.get("status") == "partial" for item in outcomes),
        "unavailable": sum(item.get("status") == "unavailable" for item in outcomes),
        "hit_rate_pct": (
            round(sum(bool(item["verdict"]) for item in decided) / len(decided) * 100, 1)
            if decided
            else None
        ),
        "average_signed_return_pct": (
            round(statistics.mean(signed_returns), 2) if signed_returns else None
        ),
        "coverage_pct": round(len(decided) / len(outcomes) * 100, 1) if outcomes else 0.0,
    }


def demo_account(
    outcomes: list[dict[str, object]],
    *,
    initial_balance: float = 1_000_000.0,
    position_share: float = 0.10,
    commission_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
) -> dict[str, object]:
    trades = []
    total_pnl = 0.0
    total_commission = 0.0
    notional = initial_balance * position_share
    max_positions = max(1, math.floor(1 / position_share))
    active_positions: dict[str, datetime] = {}
    skipped_signals = 0
    for outcome in sorted(outcomes, key=lambda item: str(item["as_of"])):
        if outcome["direction"] == "neutral" or not outcome.get("entry"):
            continue
        entry = outcome["entry"]
        assert isinstance(entry, dict)
        opened_at = _parse_timestamp(str(entry["at"]))
        active_positions = {
            ticker: exit_at
            for ticker, exit_at in active_positions.items()
            if exit_at > opened_at
        }
        if outcome["ticker"] in active_positions or len(active_positions) >= max_positions:
            skipped_signals += 1
            continue
        raw_entry = _number(entry.get("price"))
        returns = outcome.get("returns")
        if raw_entry is None or not isinstance(returns, dict):
            continue
        closed = returns.get("3d") is not None
        raw_exit = (
            raw_entry * (1 + float(returns["3d"]) / 100)
            if closed
            else _number(outcome.get("latest_price"))
        )
        if raw_exit is None:
            continue
        active_positions[str(outcome["ticker"])] = (
            opened_at + timedelta(days=3) if closed else datetime.max.replace(tzinfo=UTC)
        )
        side = "long" if outcome["direction"] == "up" else "short"
        entry_exec = raw_entry * (1 + slippage_rate if side == "long" else 1 - slippage_rate)
        exit_exec = (
            raw_exit * (1 - slippage_rate if side == "long" else 1 + slippage_rate)
            if closed
            else raw_exit
        )
        quantity = notional / entry_exec
        entry_commission = notional * commission_rate
        exit_value = quantity * exit_exec
        exit_commission = exit_value * commission_rate if closed else 0.0
        gross = quantity * (exit_exec - entry_exec) * (1 if side == "long" else -1)
        pnl = gross - entry_commission - exit_commission
        total_pnl += pnl
        total_commission += entry_commission + exit_commission
        trades.append(
            {
                "signal_id": outcome["signal_id"],
                "ticker": outcome["ticker"],
                "side": side,
                "status": "closed" if closed else "open",
                "opened_at": entry["at"],
                "entry_price": round(entry_exec, 4),
                "exit_or_mark_price": round(exit_exec, 4),
                "notional_rub": round(notional, 2),
                "commission_rub": round(entry_commission + exit_commission, 2),
                "pnl_rub": round(pnl, 2),
                "return_pct": round(pnl / notional * 100, 2),
            }
        )
    equity = initial_balance + total_pnl
    return {
        "initial_balance_rub": initial_balance,
        "equity_rub": round(equity, 2),
        "net_return_pct": round((equity / initial_balance - 1) * 100, 2),
        "total_commission_rub": round(total_commission, 2),
        "open_positions": sum(trade["status"] == "open" for trade in trades),
        "closed_trades": sum(trade["status"] == "closed" for trade in trades),
        "skipped_signals": skipped_signals,
        "rules": {
            "position_share_pct": position_share * 100,
            "commission_per_side_pct": commission_rate * 100,
            "slippage_per_side_pct": slippage_rate * 100,
            "exit_horizon": "3 calendar days",
            "max_open_positions": max_positions,
        },
        "trades": list(reversed(trades)),
    }
