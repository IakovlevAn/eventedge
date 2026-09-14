"""Build a research signal dataset from stored YDB production outcomes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from eventedge.analysis import SCORING_CONFIG
from eventedge.evals import EVALUATION_METHODOLOGY_VERSION
from eventedge_research.signal_dataset import (
    MarketOutcomeObservation,
    SignalDatasetExample,
    SignalFeatureSnapshot,
)


def build_ydb_production_signal_dataset(
    signals: Sequence[Mapping[str, object]],
    epochs: Sequence[Mapping[str, object]],
    news_by_id: Mapping[str, Mapping[str, object]],
    feature_sets: Sequence[Mapping[str, object]],
    *,
    model_version: str,
    config_version: int,
    cohort: str = "live",
) -> tuple[list[SignalDatasetExample], dict[str, object]]:
    """Convert one stored production epoch to strict, observed 4h examples."""
    if cohort not in {"live", "retrospective", "all"}:
        raise ValueError("cohort must be live, retrospective or all")
    epoch = _select_epoch(
        epochs,
        model_version=model_version,
        config_version=config_version,
    )
    selected_signals = _selected_signals(
        signals,
        model_version=model_version,
        config_version=config_version,
    )
    outcomes = _outcomes(epoch)
    _validate_identities(selected_signals, outcomes)
    signals_by_id = {str(row["signal_id"]): row for row in selected_signals}
    feature_sets_by_identity = _feature_sets_by_identity(feature_sets)

    examples = []
    skipped: Counter[str] = Counter()
    extractors: Counter[str] = Counter()
    for outcome in outcomes:
        reason = _skip_reason(outcome, cohort=cohort)
        if reason is not None:
            skipped[reason] += 1
            continue
        signal = signals_by_id[str(outcome["signal_id"])]
        news_id = str(signal["news_id"])
        news = news_by_id.get(news_id)
        if news is None:
            raise ValueError(f"news item is absent for {news_id}")
        feature_set = feature_sets_by_identity.get((news_id, str(signal["created_at"])))
        if feature_set is None:
            raise ValueError(f"signal-producing feature set is absent for {news_id}")
        example = _example(signal, outcome, news, feature_set)
        examples.append(example)
        extractors[str(feature_set.get("extractor_version", "unknown"))] += 1
    if not examples:
        raise ValueError("selected production cohort has no strict 4h examples")
    return examples, _report(
        epoch,
        examples,
        outcomes=len(outcomes),
        skipped=skipped,
        extractors=extractors,
        cohort=cohort,
    )


def _select_epoch(epochs, *, model_version: str, config_version: int):
    candidates = [
        row
        for row in epochs
        if row.get("model_version") == model_version
        and row.get("config_version") == config_version
        and row.get("observations_truncated") is False
        and _methodology(row) == EVALUATION_METHODOLOGY_VERSION
    ]
    if not candidates:
        raise ValueError("no complete current-method epoch for requested model/config")
    return max(candidates, key=lambda row: (str(row.get("evaluated_at", "")), row["epoch_id"]))


def _methodology(epoch: Mapping[str, object]) -> str:
    outcomes = epoch.get("outcomes")
    if not isinstance(outcomes, list):
        return "invalid"
    values = {
        str(row.get("evaluation_methodology"))
        for row in outcomes
        if isinstance(row, dict) and row.get("evaluation_methodology")
    }
    return values.pop() if len(values) == 1 else "legacy-or-mixed"


def _selected_signals(signals, *, model_version: str, config_version: int):
    selected = [
        row
        for row in signals
        if row.get("model_version") == model_version and row.get("config_version") == config_version
    ]
    if not selected:
        raise ValueError("signal export has no rows for requested model/config")
    return selected


def _outcomes(epoch: Mapping[str, object]) -> list[Mapping[str, object]]:
    values = epoch.get("outcomes")
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise ValueError("evaluation epoch outcomes must be objects")
    return values


def _validate_identities(signals, outcomes) -> None:
    signal_ids = [str(row.get("signal_id", "")) for row in signals]
    outcome_ids = [str(row.get("signal_id", "")) for row in outcomes]
    if "" in signal_ids or len(signal_ids) != len(set(signal_ids)):
        raise ValueError("signal IDs are missing or duplicated")
    if "" in outcome_ids or len(outcome_ids) != len(set(outcome_ids)):
        raise ValueError("outcome IDs are missing or duplicated")
    if set(signal_ids) != set(outcome_ids):
        raise ValueError("signal and outcome identities differ")


def _feature_sets_by_identity(feature_sets):
    result = {
        (str(row.get("news_id", "")), str(row.get("created_at", ""))): row for row in feature_sets
    }
    if len(result) != len(feature_sets):
        raise ValueError("feature sets contain duplicate news/created identities")
    return result


def _skip_reason(outcome: Mapping[str, object], *, cohort: str) -> str | None:
    eligibility = outcome.get("eligibility")
    actual_cohort = eligibility.get("cohort") if isinstance(eligibility, dict) else None
    if cohort != "all" and actual_cohort != cohort:
        return f"cohort:{actual_cohort or 'missing'}"
    returns = outcome.get("returns")
    if not isinstance(returns, dict) or returns.get("4h") is None:
        return "strict_4h_return_unavailable"
    entry = outcome.get("entry")
    if not isinstance(entry, dict) or entry.get("at") is None or entry.get("price") is None:
        return "entry_unavailable"
    horizons = outcome.get("horizon_observations")
    observation = horizons.get("4h") if isinstance(horizons, dict) else None
    if not isinstance(observation, dict) or observation.get("timely") is not True:
        return "strict_4h_observation_not_timely"
    if observation.get("target_at") is None or observation.get("observed_at") is None:
        return "strict_4h_observation_unavailable"
    return None


def _example(signal, outcome, news, feature_set) -> SignalDatasetExample:
    features = feature_set.get("features")
    if not isinstance(features, dict):
        raise ValueError("signal-producing feature payload is absent")
    entry = outcome["entry"]
    observation = outcome["horizon_observations"]["4h"]
    return_pct = float(outcome["returns"]["4h"])
    entry_price = float(entry["price"])
    news_id = str(signal["news_id"])
    return SignalDatasetExample(
        id=f"ydb-{signal['signal_id']}",
        event_id=f"ydb-event-{news_id}",
        ticker=str(signal["ticker"]),
        news_ids=(news_id,),
        primary_news_id=news_id,
        source_id=str(news["source_id"]),
        title=str(news["title"]),
        content=str(news.get("content", "")),
        categories=_categories(news),
        published_at=news["published_at"],
        received_at=news["received_at"],
        decision_at=outcome["eligibility"]["decision_at"],
        entry_at=entry["at"],
        entry_price=entry_price,
        features=_feature_snapshot(
            signal,
            feature_set,
            features,
            source_id=str(news["source_id"]),
        ),
        outcome_4h=MarketOutcomeObservation(
            target_at=observation["target_at"],
            observed_at=observation["observed_at"],
            price=entry_price * (1 + return_pct / 100),
            return_pct=return_pct,
        ),
        label_available_at=observation["observed_at"],
        label_source="market_outcome",
        corporate_action_status="unknown",
        notes=(
            "Read-only YDB production outcome; raw return is not adjusted for corporate "
            "actions. Built for local cross-domain research only."
        ),
    )


def _feature_snapshot(signal, feature_set, features, *, source_id: str) -> SignalFeatureSnapshot:
    facts = features.get("facts")
    fact_count = len(facts) if isinstance(facts, list) else None
    return SignalFeatureSnapshot(
        as_of=feature_set["created_at"],
        event_type=_optional_text(features.get("event_type")),
        polarity=_optional_float(features.get("polarity")),
        materiality=_optional_float(features.get("materiality")),
        novelty=_optional_float(features.get("novelty")),
        source_quality=SCORING_CONFIG.source_quality.get(
            source_id, SCORING_CONFIG.default_source_quality
        ),
        fact_count=fact_count,
        semantic_extractor_version=str(feature_set["extractor_version"]),
        semantic_feature_set_id=str(feature_set["feature_set_id"]),
        rule_direction=str(signal["direction"]),
        rule_score=float(signal["score"]),
        rule_confidence=float(signal["confidence"]),
        rule_model_version=str(signal["model_version"]),
        rule_config_version=int(signal["config_version"]),
        liquidity_status="unavailable",
    )


def _categories(news: Mapping[str, object]) -> tuple[str, ...]:
    metadata = news.get("source_metadata")
    categories = metadata.get("categories") if isinstance(metadata, dict) else None
    if not isinstance(categories, list):
        return ()
    return tuple(str(value)[:200] for value in categories if str(value).strip())[:100]


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _report(epoch, examples, *, outcomes, skipped, extractors, cohort):
    return {
        "schema_version": "ydb-production-signal-dataset-build-1.0",
        "status": "research_only_read_only_extract_no_repricing",
        "selection": {
            "model_version": epoch["model_version"],
            "config_version": epoch["config_version"],
            "epoch_id": epoch["epoch_id"],
            "evaluated_at": epoch["evaluated_at"],
            "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
            "cohort": cohort,
        },
        "input_outcomes": outcomes,
        "examples": len(examples),
        "independent_events": len({row.event_id for row in examples}),
        "formula_directions": dict(
            sorted(Counter(row.features.rule_direction for row in examples).items())
        ),
        "sources": dict(sorted(Counter(row.source_id for row in examples).items())),
        "corporate_action_statuses": dict(
            sorted(Counter(row.corporate_action_status for row in examples).items())
        ),
        "signal_feature_extractors": dict(sorted(extractors.items())),
        "skipped": dict(sorted(skipped.items())),
        "decision_at": {
            "minimum": min(row.decision_at for row in examples).isoformat(),
            "maximum": max(row.decision_at for row in examples).isoformat(),
        },
        "limitations": [
            "Stored raw outcomes are reused without repricing or corporate-action adjustment.",
            "Only signals saved by the requested production config are represented.",
            "Pre-event market context is unavailable until a causal candle join is applied.",
        ],
    }
