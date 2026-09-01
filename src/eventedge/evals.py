from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from eventedge.analysis import DEFAULT_MOEX_ALIASES
from eventedge.storage import NewsRecord, SignalRecord, to_rfc3339

ASSESSMENT_MODEL_VERSION = "hybrid-market-0.2.1"
ASSESSMENT_CONFIG_VERSION = 3
EVALUATION_METHODOLOGY_VERSION = "market-outcome-0.3.0"
MAX_LIVE_PROCESSING_LAG = timedelta(minutes=15)
MAX_HORIZON_OBSERVATION_LAG = timedelta(minutes=20)
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


def _report_evidence_text(item: NewsRecord, ticker: str) -> str | None:
    """Return text that directly connects one company to a financial report."""
    aliases = tuple(alias.casefold() for alias in DEFAULT_MOEX_ALIASES.get(ticker, ()))
    title = item.title.casefold()
    content = item.content.casefold()
    title_is_direct = any(alias in title for alias in aliases) and any(
        marker in title for marker in REPORT_MARKERS
    )
    if title_is_direct:
        return f"{title} {content}"

    sentences = re.split(r"(?<=[.!?])\s+|[\n\r]+", content)
    direct_sentences = [
        sentence
        for sentence in sentences
        if any(alias in sentence for alias in aliases)
        and any(marker in sentence for marker in REPORT_MARKERS)
    ]
    return " ".join(direct_sentences) or None


def _report_factor(ticker: str, news: Iterable[NewsRecord]) -> dict[str, object]:
    candidates = []
    for item in news:
        text = _report_evidence_text(item, ticker)
        if text is not None:
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
    market_context = {
        "as_of": str(market.get("observed_at")),
        "is_signal": False,
        "bias_direction": _bias_direction(quant_score),
        "score": quant_score,
        "confidence": quant_confidence,
        "factor_contributions": quant,
        "source": market.get("source"),
    }
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

    # Quant factors expose a market bias, but do not become a trading signal
    # without a directional news event.
    has_directional_news = active_signal is not None and active_signal.direction in {
        "up",
        "down",
    }
    direction = _direction(score) if has_directional_news else "neutral"
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
        "market_context": market_context,
        "news_signal": news_signal,
        "model_version": ASSESSMENT_MODEL_VERSION,
        "config_version": ASSESSMENT_CONFIG_VERSION,
        "limitations": [
            "Baseline не откалиброван на point-in-time backtest.",
            "Показатель отчётности доступен только при наличии распознанного раскрытия.",
            "Market context и volatility scenario не являются самостоятельными сигналами.",
        ],
    }


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def evaluation_eligibility(
    signal: SignalRecord,
    news: NewsRecord | None,
) -> dict[str, object]:
    """Classify point-in-time validity and choose a non-lookahead eval anchor."""
    live_decision_at = max(signal.as_of, signal.data_cutoff_at, signal.created_at)
    reason = None
    if signal.as_of > signal.created_at:
        reason = "signal_as_of_after_creation"
    elif signal.data_cutoff_at > signal.created_at:
        reason = "data_cutoff_after_creation"
    elif news is None:
        reason = "missing_evidence"
    elif news.published_at > signal.created_at:
        reason = "evidence_published_after_signal"
    elif news.received_at > signal.created_at:
        reason = "evidence_received_after_signal"
    elif signal.created_at - max(signal.as_of, signal.data_cutoff_at) > MAX_LIVE_PROCESSING_LAG:
        reason = "retrospective_signal"
    cohort = "live" if reason is None else "retrospective"
    if cohort == "retrospective" and news is not None:
        # A replay measures reaction from the first point where the evidence
        # could have been known. Anchoring it on a much later backfill
        # `created_at` would measure the backfill job instead of the event.
        decision_at = max(signal.as_of, signal.data_cutoff_at, news.received_at)
        evaluation_anchor = "evidence_cutoff_replay"
    else:
        decision_at = live_decision_at
        evaluation_anchor = "live_decision"
    return {
        "eligible": reason is None,
        "reason": reason,
        "cohort": cohort,
        "decision_at": to_rfc3339(decision_at),
        "live_decision_at": to_rfc3339(live_decision_at),
        "evaluation_anchor": evaluation_anchor,
        "processing_lag_seconds": max(
            0,
            round((signal.created_at - max(signal.as_of, signal.data_cutoff_at)).total_seconds()),
        ),
    }


def evaluate_signal(
    signal: SignalRecord,
    candles: list[dict[str, object]],
    news: NewsRecord | None,
) -> dict[str, object]:
    eligibility = evaluation_eligibility(signal, news)
    decision_at = _parse_timestamp(str(eligibility["decision_at"]))
    base = {
        "signal_id": signal.id,
        "ticker": signal.ticker,
        "as_of": to_rfc3339(signal.as_of),
        "data_cutoff_at": to_rfc3339(signal.data_cutoff_at),
        "signal_created_at": to_rfc3339(signal.created_at),
        "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
        "eligibility": eligibility,
        "direction": signal.direction,
        "score": signal.score,
        "confidence": signal.confidence,
        "model_version": signal.model_version,
        "config_version": signal.config_version,
        "news": {
            "id": news.id,
            "title": news.title,
            "source_id": news.source_id,
            "url": news.url,
            "published_at": to_rfc3339(news.published_at),
            "received_at": to_rfc3339(news.received_at),
            "delivery_lag_seconds": max(
                0,
                round((news.received_at - news.published_at).total_seconds()),
            ),
        }
        if news
        else None,
    }
    empty_returns = {label: None for label in ("1h", "4h", "1d", "3d")}
    rows = sorted(
        (
            (_parse_timestamp(str(item["begin"])), item)
            for item in candles
            if item.get("begin") and _number(item.get("open")) is not None
        ),
        key=lambda pair: pair[0],
    )
    if not rows:
        return {
            **base,
            "status": "unavailable",
            "entry": None,
            "returns": empty_returns,
            "horizon_observations": {},
            "verdict": None,
            "verdict_status": "unavailable",
            "outcome_terminal": False,
        }

    entry_pair = next((pair for pair in rows if pair[0] > decision_at), None)
    if entry_pair is None:
        return {
            **base,
            "status": "unavailable",
            "entry": None,
            "returns": empty_returns,
            "horizon_observations": {},
            "verdict": None,
            "verdict_status": "unavailable",
            "outcome_terminal": False,
        }
    entry_time, entry_row = entry_pair
    entry_price = _number(entry_row.get("open"))
    assert entry_price is not None
    returns: dict[str, float | None] = {}
    horizon_observations: dict[str, dict[str, object]] = {}
    target_hours = {"1h": 1, "4h": 4, "1d": 24, "3d": 72}
    for label, hours in target_hours.items():
        target = entry_time + timedelta(hours=hours)
        observation = next(
            ((timestamp, row) for timestamp, row in rows if timestamp >= target),
            None,
        )
        observed_at, target_row = observation if observation else (None, None)
        delay = observed_at - target if observed_at is not None else None
        timely = delay is not None and delay <= MAX_HORIZON_OBSERVATION_LAG
        # The candle open is observable at `begin`; using its close would
        # silently add one interval of future price information.
        price = _number(target_row.get("open")) if target_row is not None and timely else None
        returns[label] = round((price / entry_price - 1) * 100, 2) if price is not None else None
        horizon_observations[label] = {
            "target_at": to_rfc3339(target),
            "observed_at": to_rfc3339(observed_at) if observed_at is not None else None,
            "delay_seconds": round(delay.total_seconds()) if delay is not None else None,
            "timely": timely,
            "price_field": "open",
        }
    latest_price = _number(rows[-1][1].get("close"))
    latest_return = (
        round((latest_price / entry_price - 1) * 100, 2) if latest_price is not None else None
    )
    # The product is intentionally short-term: the UI verdict is based on the
    # first four tradable hours, while 1d/3d observations remain available for
    # raw research and exports.
    primary = next((returns[key] for key in ("4h", "1h") if returns[key] is not None), None)
    verdict = None
    if primary is not None:
        if signal.direction == "neutral":
            verdict = abs(primary) < 0.5
        elif signal.direction == "up":
            verdict = primary > 0
        else:
            verdict = primary < 0
    status = "evaluated" if returns["3d"] is not None else "partial"
    three_day_target = _parse_timestamp(str(horizon_observations["3d"]["target_at"]))
    outcome_terminal = (
        returns["3d"] is not None
        or datetime.now(UTC) > three_day_target + MAX_HORIZON_OBSERVATION_LAG
    )
    if verdict is not None:
        verdict_status = "evaluated"
    else:
        short_observations = [horizon_observations[key] for key in ("1h", "4h")]
        one_hour_target = _parse_timestamp(str(horizon_observations["1h"]["target_at"]))
        missed_window = (
            any(
                observation.get("observed_at") is not None and not observation.get("timely")
                for observation in short_observations
            )
            or datetime.now(UTC) > one_hour_target + MAX_HORIZON_OBSERVATION_LAG
        )
        verdict_status = "missed_window" if missed_window else "pending"
    return {
        **base,
        "status": status,
        "entry": {
            "at": to_rfc3339(entry_time),
            "price": round(entry_price, 4),
            "delay_seconds": round((entry_time - decision_at).total_seconds()),
        },
        "returns": returns,
        "horizon_observations": horizon_observations,
        "latest_price": round(latest_price, 4) if latest_price is not None else None,
        "latest_return_pct": latest_return,
        "verdict": verdict,
        "verdict_status": verdict_status,
        "outcome_terminal": outcome_terminal,
    }


EVAL_HORIZONS = ("1h", "4h", "1d", "3d")
PRIMARY_EVAL_HORIZON = "4h"


def _return_at(item: dict[str, object], horizon: str) -> float | None:
    returns = item.get("returns")
    if not isinstance(returns, dict):
        return None
    return _number(returns.get(horizon))


def _primary_return(item: dict[str, object]) -> float | None:
    return next(
        (
            value
            for horizon in (PRIMARY_EVAL_HORIZON, "1h")
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


def deduplicate_eval_signals(signals: Iterable[SignalRecord]) -> list[SignalRecord]:
    """Count one trading decision once when several publications confirm it."""
    unique: dict[tuple[object, ...], SignalRecord] = {}
    for signal in signals:
        key = (
            signal.ticker,
            signal.as_of,
            signal.direction,
            signal.action,
            signal.horizon_value,
            signal.horizon_unit,
            signal.score,
            signal.model_version,
            signal.config_version,
        )
        previous = unique.get(key)
        if previous is None or (signal.confidence, signal.created_at, signal.id) > (
            previous.confidence,
            previous.created_at,
            previous.id,
        ):
            unique[key] = signal
    return sorted(unique.values(), key=lambda signal: (signal.as_of, signal.id), reverse=True)


EVAL_TITLE_STOP_WORDS = frozenset(
    {
        "для",
        "как",
        "при",
        "что",
        "это",
        "или",
        "после",
        "перед",
        "уже",
        "акции",
        "акция",
        "рублей",
        "года",
        "году",
    }
)


def _eval_title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-zа-яё0-9]+", " ", title.casefold()).split()
        if len(token) > 2 and token not in EVAL_TITLE_STOP_WORDS
    }


def _same_eval_event(left: NewsRecord, right: NewsRecord) -> bool:
    left_tokens = _eval_title_tokens(left.title)
    right_tokens = _eval_title_tokens(right.title)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    if abs((left.published_at - right.published_at).total_seconds()) > 72 * 60 * 60:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.6


def deduplicate_eval_events(
    signals: Iterable[SignalRecord],
    news_by_id: dict[str, NewsRecord],
    *,
    preserve_model_epochs: bool = False,
) -> list[SignalRecord]:
    """Evaluate one market event once, even when several publications repeat it."""
    groups: list[list[SignalRecord]] = []
    for signal in sorted(signals, key=lambda item: (item.as_of, item.id)):
        news = news_by_id.get(signal.news_id)
        if news is None:
            continue
        group = next(
            (
                candidate
                for candidate in groups
                if candidate[0].ticker == signal.ticker
                and (
                    not preserve_model_epochs
                    or (
                        candidate[0].model_version == signal.model_version
                        and candidate[0].config_version == signal.config_version
                    )
                )
                and (representative_news := news_by_id.get(candidate[0].news_id)) is not None
                and _same_eval_event(representative_news, news)
            ),
            None,
        )
        if group is None:
            groups.append([signal])
        else:
            group.append(signal)

    representatives = []
    for group in groups:
        latest_model = max(signal.model_version for signal in group)
        current_model = (
            group
            if preserve_model_epochs
            else [signal for signal in group if signal.model_version == latest_model]
        )
        representatives.append(
            min(
                current_model,
                key=lambda signal: (
                    news_by_id[signal.news_id].published_at,
                    signal.as_of,
                    signal.id,
                ),
            )
        )
    return sorted(representatives, key=lambda signal: (signal.as_of, signal.id), reverse=True)


def _metric_slice(
    items: list[dict[str, object]],
    *,
    horizon: str | None = None,
) -> dict[str, object]:
    values = [
        (item, _return_at(item, horizon) if horizon else _primary_return(item)) for item in items
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
        "hit_rate_pct": (round(sum(verdicts) / len(verdicts) * 100, 1) if verdicts else None),
        "average_signed_return_pct": (round(statistics.mean(signed), 2) if signed else None),
        "median_signed_return_pct": (round(statistics.median(signed), 2) if signed else None),
    }


def eval_summary(outcomes: list[dict[str, object]]) -> dict[str, object]:
    def cohort(item: dict[str, object]) -> str:
        eligibility = item.get("eligibility")
        if not isinstance(eligibility, dict):
            return "retrospective"
        stored = eligibility.get("cohort")
        if stored in {"live", "retrospective"}:
            return str(stored)
        return "live" if eligibility.get("eligible") is True else "retrospective"

    def verdict_status(item: dict[str, object]) -> str:
        stored = item.get("verdict_status")
        if stored in {"evaluated", "pending", "missed_window", "unavailable"}:
            return str(stored)
        if item.get("verdict") is not None:
            return "evaluated"
        if item.get("status") in {"unavailable", "excluded"}:
            return "unavailable"
        return "pending"

    def cohort_summary(items: list[dict[str, object]]) -> dict[str, object]:
        decided = [item for item in items if item.get("verdict") is not None]
        metrics = _metric_slice(items)
        statuses = [verdict_status(item) for item in items]
        return {
            "signals_total": len(items),
            "evaluated": len(decided),
            "complete": sum(item.get("status") == "evaluated" for item in items),
            "partial": sum(
                item.get("status") == "partial" and item.get("verdict") is not None
                for item in items
            ),
            "pending": statuses.count("pending"),
            "missed_window": statuses.count("missed_window"),
            "unavailable": statuses.count("unavailable"),
            "hit_rate_pct": metrics["hit_rate_pct"],
            "average_signed_return_pct": metrics["average_signed_return_pct"],
            "median_signed_return_pct": metrics["median_signed_return_pct"],
            "coverage_pct": round(len(decided) / len(items) * 100, 1) if items else 0.0,
        }

    live = [item for item in outcomes if cohort(item) == "live"]
    retrospective = [item for item in outcomes if cohort(item) == "retrospective"]
    all_summary = cohort_summary(outcomes)
    live_summary = cohort_summary(live)
    retrospective_summary = cohort_summary(retrospective)
    exclusion_reasons: dict[str, int] = {}
    for item in retrospective:
        eligibility = item.get("eligibility")
        reason = eligibility.get("reason") if isinstance(eligibility, dict) else None
        key = str(reason or "unknown")
        exclusion_reasons[key] = exclusion_reasons.get(key, 0) + 1
    return {
        **all_summary,
        # Stable product aliases intentionally describe the point-in-time live
        # cohort. Research/all-signal metrics remain explicit below.
        "metric_scope": "live",
        "hit_rate_pct": live_summary["hit_rate_pct"],
        "average_signed_return_pct": live_summary["average_signed_return_pct"],
        "median_signed_return_pct": live_summary["median_signed_return_pct"],
        "coverage_pct": live_summary["coverage_pct"],
        # Compatibility aliases. `excluded` no longer means an outcome is skipped;
        # it is the historical count of signals outside the point-in-time cohort.
        "eligible": len(live),
        "excluded": len(retrospective),
        "live_eligible": len(live),
        "research_only": len(retrospective),
        "exclusion_reasons": exclusion_reasons,
        "live_evaluated": live_summary["evaluated"],
        "research_evaluated": retrospective_summary["evaluated"],
        "live_coverage_pct": live_summary["coverage_pct"],
        "research_coverage_pct": retrospective_summary["coverage_pct"],
        "cohorts": {
            "all": all_summary,
            "live": live_summary,
            "retrospective": retrospective_summary,
        },
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
    definitions = []
    for horizon, horizon_label in (("1h", "1ч"), ("4h", "4ч")):
        strength_pairs = []
        confidence_pairs = []
        for item in outcomes:
            value = _return_at(item, horizon)
            signed = _signed_return(item.get("direction"), value)
            score = _number(item.get("score"))
            confidence = _number(item.get("confidence"))
            if signed is not None and score is not None:
                strength_pairs.append((abs(score), signed))
            verdict = _verdict(item.get("direction"), value)
            if verdict is not None and confidence is not None:
                confidence_pairs.append((confidence, float(verdict)))
        definitions.extend(
            (
                (
                    f"signal_strength_vs_{horizon}_return",
                    f"Сила сигнала ↔ результат {horizon_label}",
                    strength_pairs,
                ),
                (
                    f"confidence_vs_{horizon}_hit",
                    f"Уверенность ↔ попадание {horizon_label}",
                    confidence_pairs,
                ),
            )
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
        benchmark = (
            item.get("evaluation_benchmark")
            if isinstance(item.get("evaluation_benchmark"), dict)
            else {}
        )
        eligibility = item.get("eligibility") if isinstance(item.get("eligibility"), dict) else {}
        observations = (
            item.get("horizon_observations")
            if isinstance(item.get("horizon_observations"), dict)
            else {}
        )
        rows.append(
            {
                "signal_id": item["signal_id"],
                "ticker": item["ticker"],
                "signal_as_of": item["as_of"],
                "signal_data_cutoff_at": item.get("data_cutoff_at"),
                "signal_created_at": item.get("signal_created_at"),
                "decision_at": eligibility.get("decision_at"),
                "live_decision_at": eligibility.get("live_decision_at"),
                "evaluation_anchor": eligibility.get("evaluation_anchor"),
                "processing_lag_seconds": eligibility.get("processing_lag_seconds"),
                "evaluation_methodology": item.get("evaluation_methodology"),
                "evaluation_eligible": eligibility.get("eligible"),
                "exclusion_reason": eligibility.get("reason"),
                "eligibility_cohort": eligibility.get("cohort"),
                "direction": item["direction"],
                "score": item["score"],
                "confidence": item["confidence"],
                "model_version": item["model_version"],
                "config_version": item.get("config_version"),
                "evaluation_benchmark": benchmark.get("ticker"),
                "status": item["status"],
                "news_id": news.get("id"),
                "news_source_id": news.get("source_id"),
                "news_url": news.get("url"),
                "news_published_at": news.get("published_at"),
                "news_received_at": news.get("received_at"),
                "delivery_lag_seconds": news.get("delivery_lag_seconds"),
                "entry_at": entry.get("at"),
                "entry_price": entry.get("price"),
                "entry_delay_seconds": entry.get("delay_seconds"),
                "return_1h_pct": _return_at(item, "1h"),
                "return_4h_pct": _return_at(item, "4h"),
                "return_1d_pct": _return_at(item, "1d"),
                "return_3d_pct": _return_at(item, "3d"),
                **{
                    f"observed_{horizon}_at": (
                        observations.get(horizon, {}).get("observed_at")
                        if isinstance(observations.get(horizon), dict)
                        else None
                    )
                    for horizon in EVAL_HORIZONS
                },
                **{
                    f"observation_{horizon}_delay_seconds": (
                        observations.get(horizon, {}).get("delay_seconds")
                        if isinstance(observations.get(horizon), dict)
                        else None
                    )
                    for horizon in EVAL_HORIZONS
                },
                "latest_price": item.get("latest_price"),
                "latest_return_pct": item.get("latest_return_pct"),
                "verdict": item.get("verdict"),
                "verdict_status": item.get("verdict_status"),
                "outcome_terminal": item.get("outcome_terminal"),
                "raw_observation_count": item.get("raw_observation_count"),
            }
        )
    return rows


def event_time_export_rows(
    signals: list[SignalRecord],
    candles_by_ticker: dict[str, list[dict[str, object]]],
    news_by_id: dict[str, NewsRecord],
    *,
    max_rows: int | None = None,
) -> tuple[list[dict[str, object]], bool]:
    rows = []
    for signal in signals:
        news = news_by_id.get(signal.news_id)
        eligibility = evaluation_eligibility(signal, news)
        decision_at = _parse_timestamp(str(eligibility["decision_at"]))
        candles = sorted(
            (
                (_parse_timestamp(str(item["begin"])), item)
                for item in candles_by_ticker.get(signal.ticker, [])
                if item.get("begin") and _number(item.get("open")) is not None
            ),
            key=lambda pair: pair[0],
        )
        entry = next((pair for pair in candles if pair[0] > decision_at), None)
        if entry is None:
            continue
        entry_at, entry_row = entry
        entry_price = _number(entry_row.get("open"))
        if entry_price is None:
            continue
        for observed_at, candle in candles:
            if observed_at < entry_at or observed_at > entry_at + timedelta(days=3):
                continue
            observed_price = _number(candle.get("open"))
            close = _number(candle.get("close"))
            raw_return = (
                round((observed_price / entry_price - 1) * 100, 4)
                if observed_price is not None
                else None
            )
            rows.append(
                {
                    "signal_id": signal.id,
                    "ticker": signal.ticker,
                    "signal_as_of": to_rfc3339(signal.as_of),
                    "signal_data_cutoff_at": to_rfc3339(signal.data_cutoff_at),
                    "signal_created_at": to_rfc3339(signal.created_at),
                    "decision_at": eligibility["decision_at"],
                    "live_decision_at": eligibility["live_decision_at"],
                    "evaluation_anchor": eligibility["evaluation_anchor"],
                    "processing_lag_seconds": eligibility["processing_lag_seconds"],
                    "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
                    "evaluation_eligible": eligibility["eligible"],
                    "exclusion_reason": eligibility["reason"],
                    "eligibility_cohort": eligibility["cohort"],
                    "direction": signal.direction,
                    "score": signal.score,
                    "confidence": signal.confidence,
                    "model_version": signal.model_version,
                    "config_version": signal.config_version,
                    "news_id": signal.news_id,
                    "news_source_id": news.source_id if news else None,
                    "entry_at": to_rfc3339(entry_at),
                    "entry_price": round(entry_price, 4),
                    "entry_delay_seconds": round((entry_at - decision_at).total_seconds()),
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
            if max_rows is not None and len(rows) >= max_rows:
                return rows, True
    return rows, False
