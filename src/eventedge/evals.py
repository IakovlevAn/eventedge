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


EVAL_HORIZONS = ("1h", "1d", "3d")


def _return_at(item: dict[str, object], horizon: str) -> float | None:
    returns = item.get("returns")
    if not isinstance(returns, dict):
        return None
    return _number(returns.get(horizon))


def _primary_return(item: dict[str, object]) -> float | None:
    return next(
        (
            value
            for horizon in reversed(EVAL_HORIZONS)
            if (value := _return_at(item, horizon)) is not None
        ),
        None,
    )


def _signed_return(direction: object, value: float | None) -> float | None:
    if value is None or direction not in {"up", "down"}:
        return None
    return value * (-1 if direction == "down" else 1)


def _verdict(direction: object, value: float | None) -> bool | None:
    if value is None:
        return None
    if direction == "neutral":
        return abs(value) < 0.5
    if direction == "up":
        return value > 0
    if direction == "down":
        return value < 0
    return None


def _metric_slice(
    items: list[dict[str, object]],
    *,
    horizon: str | None = None,
) -> dict[str, object]:
    values = [
        (item, _return_at(item, horizon) if horizon else _primary_return(item))
        for item in items
    ]
    evaluated = [(item, value) for item, value in values if value is not None]
    verdicts = [
        verdict
        for item, value in evaluated
        if (verdict := _verdict(item.get("direction"), value)) is not None
    ]
    signed = [
        result
        for item, value in evaluated
        if (result := _signed_return(item.get("direction"), value)) is not None
    ]
    return {
        "signals": len(items),
        "observations": len(evaluated),
        "hit_rate_pct": (
            round(sum(verdicts) / len(verdicts) * 100, 1) if verdicts else None
        ),
        "average_signed_return_pct": (
            round(statistics.mean(signed), 2) if signed else None
        ),
        "median_signed_return_pct": (
            round(statistics.median(signed), 2) if signed else None
        ),
    }


def eval_summary(outcomes: list[dict[str, object]]) -> dict[str, object]:
    decided = [item for item in outcomes if item.get("verdict") is not None]
    metrics = _metric_slice(outcomes)
    return {
        "signals_total": len(outcomes),
        "evaluated": len(decided),
        "pending": sum(item.get("status") == "partial" for item in outcomes),
        "unavailable": sum(item.get("status") == "unavailable" for item in outcomes),
        "hit_rate_pct": metrics["hit_rate_pct"],
        "average_signed_return_pct": metrics["average_signed_return_pct"],
        "median_signed_return_pct": metrics["median_signed_return_pct"],
        "coverage_pct": round(len(decided) / len(outcomes) * 100, 1) if outcomes else 0.0,
    }


def eval_breakdowns(outcomes: list[dict[str, object]]) -> dict[str, object]:
    by_horizon = [
        {"horizon": horizon, **_metric_slice(outcomes, horizon=horizon)}
        for horizon in EVAL_HORIZONS
    ]
    by_direction = [
        {
            "direction": direction,
            **_metric_slice([item for item in outcomes if item.get("direction") == direction]),
        }
        for direction in ("up", "down", "neutral")
    ]
    tickers = sorted({str(item["ticker"]) for item in outcomes})
    by_ticker = sorted(
        (
            {
                "ticker": ticker,
                **_metric_slice([item for item in outcomes if item.get("ticker") == ticker]),
            }
            for ticker in tickers
        ),
        key=lambda item: (-int(item["signals"]), str(item["ticker"])),
    )
    confidence_ranges = (
        ("<60%", 0.0, 0.6),
        ("60–70%", 0.6, 0.7),
        ("70–80%", 0.7, 0.8),
        ("≥80%", 0.8, 1.01),
    )
    by_confidence = []
    for label, low, high in confidence_ranges:
        bucket = [
            item
            for item in outcomes
            if (confidence := _number(item.get("confidence"))) is not None
            and low <= confidence < high
        ]
        by_confidence.append({"bucket": label, **_metric_slice(bucket)})
    return {
        "by_horizon": by_horizon,
        "by_direction": by_direction,
        "by_ticker": by_ticker,
        "by_confidence": by_confidence,
    }


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    if len(set(left)) < 2 or len(set(right)) < 2:
        return None
    return round(statistics.correlation(left, right), 3)


def _correlation_interpretation(value: float | None, observations: int) -> str:
    if value is None or observations < 8:
        return "недостаточно данных"
    strength = abs(value)
    if strength < 0.2:
        return "слабая связь"
    if strength < 0.5:
        return "умеренная связь"
    return "сильная связь"


def eval_relationships(outcomes: list[dict[str, object]]) -> list[dict[str, object]]:
    strength_pairs = []
    confidence_pairs = []
    for item in outcomes:
        value = _return_at(item, "3d")
        signed = _signed_return(item.get("direction"), value)
        score = _number(item.get("score"))
        confidence = _number(item.get("confidence"))
        if signed is not None and score is not None:
            strength_pairs.append((abs(score), signed))
        verdict = _verdict(item.get("direction"), value)
        if verdict is not None and confidence is not None:
            confidence_pairs.append((confidence, float(verdict)))

    definitions = (
        (
            "signal_strength_vs_3d_return",
            "Сила сигнала ↔ результат 3д",
            strength_pairs,
        ),
        (
            "confidence_vs_3d_hit",
            "Уверенность ↔ попадание 3д",
            confidence_pairs,
        ),
    )
    result = []
    for code, label, pairs in definitions:
        value = _correlation(pairs)
        result.append(
            {
                "code": code,
                "label": label,
                "method": "pearson",
                "value": value,
                "observations": len(pairs),
                "interpretation": _correlation_interpretation(value, len(pairs)),
            }
        )
    return result


def eval_quality_series(outcomes: list[dict[str, object]]) -> list[dict[str, object]]:
    evaluated = sorted(
        (item for item in outcomes if item.get("verdict") is not None),
        key=lambda item: str(item["as_of"]),
    )
    points = []
    hits: list[bool] = []
    signed_returns: list[float] = []
    for item in evaluated:
        hits.append(bool(item["verdict"]))
        signed = _signed_return(item.get("direction"), _primary_return(item))
        if signed is not None:
            signed_returns.append(signed)
        points.append(
            {
                "as_of": item["as_of"],
                "signal_id": item["signal_id"],
                "ticker": item["ticker"],
                "evaluated_count": len(hits),
                "cumulative_hit_rate_pct": round(sum(hits) / len(hits) * 100, 1),
                "cumulative_average_signed_return_pct": (
                    round(statistics.mean(signed_returns), 2) if signed_returns else None
                ),
            }
        )
    return points


def outcome_export_rows(outcomes: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for item in outcomes:
        entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
        news = item.get("news") if isinstance(item.get("news"), dict) else {}
        rows.append(
            {
                "signal_id": item["signal_id"],
                "ticker": item["ticker"],
                "signal_as_of": item["as_of"],
                "direction": item["direction"],
                "score": item["score"],
                "confidence": item["confidence"],
                "model_version": item["model_version"],
                "status": item["status"],
                "news_id": news.get("id"),
                "news_source_id": news.get("source_id"),
                "news_url": news.get("url"),
                "entry_at": entry.get("at"),
                "entry_price": entry.get("price"),
                "return_1h_pct": _return_at(item, "1h"),
                "return_1d_pct": _return_at(item, "1d"),
                "return_3d_pct": _return_at(item, "3d"),
                "latest_price": item.get("latest_price"),
                "latest_return_pct": item.get("latest_return_pct"),
                "verdict": item.get("verdict"),
            }
        )
    return rows


def event_time_export_rows(
    signals: list[SignalRecord],
    candles_by_ticker: dict[str, list[dict[str, object]]],
    news_by_id: dict[str, NewsRecord],
    *,
    max_rows: int = 5000,
) -> tuple[list[dict[str, object]], bool]:
    rows = []
    for signal in signals:
        candles = sorted(
            (
                (_parse_timestamp(str(item["begin"])), item)
                for item in candles_by_ticker.get(signal.ticker, [])
                if item.get("begin") and _number(item.get("open")) is not None
            ),
            key=lambda pair: pair[0],
        )
        entry = next((pair for pair in candles if pair[0] >= signal.as_of), None)
        if entry is None or entry[0] - signal.as_of > timedelta(days=3):
            continue
        entry_at, entry_row = entry
        entry_price = _number(entry_row.get("open"))
        if entry_price is None:
            continue
        news = news_by_id.get(signal.news_id)
        for observed_at, candle in candles:
            if observed_at < entry_at or observed_at > entry_at + timedelta(days=3):
                continue
            close = _number(candle.get("close"))
            raw_return = round((close / entry_price - 1) * 100, 4) if close is not None else None
            rows.append(
                {
                    "signal_id": signal.id,
                    "ticker": signal.ticker,
                    "signal_as_of": to_rfc3339(signal.as_of),
                    "direction": signal.direction,
                    "score": signal.score,
                    "confidence": signal.confidence,
                    "model_version": signal.model_version,
                    "news_id": signal.news_id,
                    "news_source_id": news.source_id if news else None,
                    "entry_at": to_rfc3339(entry_at),
                    "entry_price": round(entry_price, 4),
                    "observation_at": to_rfc3339(observed_at),
                    "offset_minutes": int((observed_at - entry_at).total_seconds() / 60),
                    "open": _number(candle.get("open")),
                    "high": _number(candle.get("high")),
                    "low": _number(candle.get("low")),
                    "close": close,
                    "value_rub": _number(candle.get("value_rub")),
                    "volume_shares": _number(candle.get("volume_shares")),
                    "return_pct": raw_return,
                    "signed_return_pct": _signed_return(signal.direction, raw_return),
                }
            )
            if len(rows) >= max_rows:
                return rows, True
    return rows, False
