"""Causal features and regularized models retained by the news benchmark."""

from __future__ import annotations

import bisect
import gzip
import json
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from eventedge.analysis import SCORING_CONFIG, NewsAnalysisInput, RuleBasedNewsExtractor
from eventedge_research.signal_extended_metrics import select_coverage_threshold
from eventedge_research.signal_investor_policy import ValidationLogitOffset

REGULARIZATION_C = 0.1
COVERAGE_TARGETS_PCT = (20, 50, 100)
MAIN_SESSION_OPEN_UTC_HOUR = 7
MAXIMUM_BOUNDARY_LAG = timedelta(minutes=20)
MAXIMUM_OPEN_ARCHIVE_BYTES = 1_000_000_000
MAXIMUM_OPEN_ARCHIVE_ROWS = 2_000_000
MAXIMUM_OPEN_LINE_BYTES = 1_000_000

PUBLICATION_NUMERIC_FEATURES = (
    "market_pre_event_return_1h_pct",
    "market_pre_event_return_1d_pct",
    "market_pre_event_return_5d_pct",
    "market_benchmark_pre_event_return_1h_pct",
    "risk_volatility_20d_pct",
    "risk_beta_60d",
    "risk_beta_adjusted_pre_event_1h_pct",
    "time_hour_sin",
    "time_hour_cos",
    "time_weekday_sin",
    "time_weekday_cos",
    "time_year",
    "time_month",
)
UPDATE_5M_NUMERIC_FEATURES = (
    "reaction_stock_pct",
    "reaction_benchmark_pct",
    "reaction_abnormal_pct",
)
SYMMETRIC_REACTION_FEATURES = (
    "derived_abs_reaction_stock_pct",
    "derived_abs_reaction_abnormal_pct",
    "derived_abs_reaction_abnormal_to_volatility",
)
CONTROL_CATEGORICAL_FEATURES = ("ticker", "source_id", "publication_session")
CORRECTED_SEMANTIC_FEATURES = (
    "corrected_category",
    "corrected_stage",
    "corrected_issuer_role",
    "corrected_disposition",
)
CORRECTED_REACTION_INTERACTIONS = (
    "corrected_category_x_reaction_sign",
    "corrected_stage_x_reaction_sign",
)
LEGACY_INTERACTION_FEATURE = "reaction_polarity_congruence"


@dataclass(frozen=True)
class NewsFeatureRow:
    """One outcome-free feature row available at its decision timestamp."""

    example_id: str
    event_id: str
    event_group_id: str
    ticker: str
    source_id: str
    title: str
    content: str
    decision_at: datetime
    event_type: str
    temporal_status: str
    numeric: Mapping[str, float | None]
    categorical: Mapping[str, str]

    def with_numeric(
        self,
        updates: Mapping[str, float | None],
    ) -> NewsFeatureRow:
        """Return a copy with the supplied numeric feature updates."""
        return replace(self, numeric={**self.numeric, **updates})


@dataclass(frozen=True)
class NewsFeatureSpec:
    """Exact numeric, categorical and optional text inputs for one model."""

    name: str
    numeric_prefixes: tuple[str, ...]
    categorical_names: tuple[str, ...]
    include_text: bool = False
    numeric_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class FittedNewsModel:
    """Sparse L2 classifier with validation-only intercept calibration."""

    spec: NewsFeatureSpec
    transformer: object
    classifier: object
    calibrator: ValidationLogitOffset
    validation_probabilities_up: tuple[float, ...]
    coverage_thresholds: Mapping[int, float]

    def raw_probabilities_up(
        self,
        rows: Sequence[NewsFeatureRow],
    ) -> tuple[float, ...]:
        """Return uncalibrated positive-class probabilities."""
        matrix = self.transformer.transform(rows)
        return tuple(float(value) for value in self.classifier.predict_proba(matrix)[:, 1])

    def probabilities_up(
        self,
        rows: Sequence[NewsFeatureRow],
    ) -> tuple[float, ...]:
        """Return validation-calibrated positive-class probabilities."""
        return tuple(self.calibrator.calibrate(self.raw_probabilities_up(rows)))


class TimedOpen(Protocol):
    """Minimal market-open input used by the fresh-data feature projection."""

    at: datetime
    open: float


@dataclass(frozen=True)
class MinuteOpenSeries:
    """Sorted local minute opens used to build a bounded future outcome."""

    times: tuple[datetime, ...]
    prices: tuple[float, ...]
    daily_times: Mapping[date, datetime]
    daily_prices: Mapping[date, float]

    @classmethod
    def from_paths(cls, paths: Sequence[Path]) -> MinuteOpenSeries:
        """Load and de-duplicate bounded local minute-open archives."""
        pairs = []
        for path in paths:
            if not path.exists():
                continue
            pairs.extend(_open_pairs(path))
        return cls._from_pairs(pairs)

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[TimedOpen],
    ) -> MinuteOpenSeries:
        """Build a series from already validated market-open observations."""
        return cls._from_pairs((row.at, float(row.open)) for row in observations)

    @classmethod
    def _from_pairs(
        cls,
        pairs: Iterable[tuple[datetime, float]],
    ) -> MinuteOpenSeries:
        observations: dict[datetime, float] = {}
        for timestamp, price in pairs:
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("minute-open timestamp must be timezone-aware")
            if not math.isfinite(price) or price <= 0:
                raise ValueError("minute-open price must be finite and positive")
            existing_price = observations.get(timestamp)
            if existing_price is not None and existing_price != price:
                raise ValueError(
                    "conflicting minute opens for timestamp "
                    f"{timestamp.isoformat()}"
                )
            observations[timestamp] = price
        ordered = sorted(observations.items())
        daily_times: dict[date, datetime] = {}
        daily_prices: dict[date, float] = {}
        for timestamp, price in ordered:
            utc = timestamp.astimezone(UTC)
            if utc.hour < MAIN_SESSION_OPEN_UTC_HOUR:
                continue
            day = (utc + timedelta(hours=3)).date()
            if day not in daily_times:
                daily_times[day] = timestamp
                daily_prices[day] = price
        return cls(
            times=tuple(value[0] for value in ordered),
            prices=tuple(value[1] for value in ordered),
            daily_times=daily_times,
            daily_prices=daily_prices,
        )

    def first_at_or_after(
        self,
        requested: datetime,
        *,
        maximum_lag: timedelta = MAXIMUM_BOUNDARY_LAG,
    ) -> tuple[datetime, float] | None:
        """Return the first timely observation at or after the boundary."""
        index = bisect.bisect_left(self.times, requested)
        if index >= len(self.times) or self.times[index] - requested > maximum_lag:
            return None
        return self.times[index], self.prices[index]

    def last_at_or_before(
        self,
        requested: datetime,
        *,
        maximum_lag: timedelta = MAXIMUM_BOUNDARY_LAG,
    ) -> tuple[datetime, float] | None:
        """Return the last timely observation at or before the boundary."""
        index = bisect.bisect_right(self.times, requested) - 1
        if index < 0 or requested - self.times[index] > maximum_lag:
            return None
        return self.times[index], self.prices[index]


def build_dataset_feature_rows(
    examples: Sequence[object],
    issuer_series: Mapping[str, MinuteOpenSeries],
    benchmark_series: MinuteOpenSeries,
) -> list[NewsFeatureRow]:
    """Project homogeneous labeled rows into outcome-free five-minute features.

    The projection reads identity, text and feature snapshots only up to
    ``published_at + 5 minutes``. Stored outcomes are deliberately not accessed.
    """
    extractor = RuleBasedNewsExtractor()
    rows = []
    for example in sorted(examples, key=lambda row: (row.published_at, row.id)):
        feature_cutoff = example.published_at + timedelta(minutes=5)
        if any(
            timestamp > feature_cutoff
            for timestamp in (
                example.received_at,
                example.decision_at,
                example.features.as_of,
            )
        ):
            raise ValueError(
                "fresh model preparation requires the decision, text and features "
                "by publication +5m"
            )
        extracted = extractor.extract(
            NewsAnalysisInput(
                source_id=example.source_id,
                title=example.title[:500],
                content=example.content or example.title,
                language="ru",
            )
        )
        stock = issuer_series.get(example.ticker)
        risk_volatility, risk_beta = _past_risk(
            stock,
            benchmark_series,
            feature_cutoff,
        )
        reaction_stock = _initial_return(stock, example.published_at, feature_cutoff)
        reaction_benchmark = _initial_return(
            benchmark_series,
            example.published_at,
            feature_cutoff,
        )
        reaction_abnormal = _subtract(reaction_stock, reaction_benchmark)
        group_id = getattr(example, "event_group_id", example.event_id)
        source_quality = SCORING_CONFIG.source_quality.get(
            example.source_id,
            SCORING_CONFIG.default_source_quality,
        )
        rows.append(
            NewsFeatureRow(
                example_id=example.id,
                event_id=example.event_id,
                event_group_id=group_id,
                ticker=example.ticker,
                source_id=example.source_id,
                title=example.title,
                content=example.content,
                decision_at=feature_cutoff,
                event_type=extracted.event_type.value,
                temporal_status=extracted.temporal_status.value,
                numeric={
                    "market_pre_event_return_1h_pct": (
                        example.features.pre_event_return_1h_pct
                    ),
                    "market_pre_event_return_1d_pct": (
                        example.features.pre_event_return_1d_pct
                    ),
                    "market_pre_event_return_5d_pct": (
                        example.features.pre_event_return_5d_pct
                    ),
                    "market_benchmark_pre_event_return_1h_pct": (
                        example.features.benchmark_pre_event_return_1h_pct
                    ),
                    "risk_volatility_20d_pct": risk_volatility,
                    "risk_beta_60d": risk_beta,
                    "risk_beta_adjusted_pre_event_1h_pct": _beta_adjusted_pre_event(
                        example,
                        risk_beta,
                    ),
                    "reaction_stock_pct": reaction_stock,
                    "reaction_benchmark_pct": reaction_benchmark,
                    "reaction_abnormal_pct": reaction_abnormal,
                    "reaction_polarity_congruence": (
                        extracted.polarity * reaction_stock
                        if reaction_stock is not None
                        else None
                    ),
                    "semantic_source_quality": source_quality,
                },
                categorical={
                    "ticker": example.ticker,
                    "source_id": example.source_id,
                    "event_type": extracted.event_type.value,
                    "temporal_status": extracted.temporal_status.value,
                },
            )
        )
    return rows


def retained_feature_specs() -> dict[str, NewsFeatureSpec]:
    """Return the exact feature sets used by the retained benchmark."""
    publication = PUBLICATION_NUMERIC_FEATURES
    update = (*publication, *UPDATE_5M_NUMERIC_FEATURES)
    symmetric = (*update, *SYMMETRIC_REACTION_FEATURES)
    semantics = (*CONTROL_CATEGORICAL_FEATURES, *CORRECTED_SEMANTIC_FEATURES)
    return {
        "publication_control": NewsFeatureSpec(
            "publication_control",
            (),
            CONTROL_CATEGORICAL_FEATURES,
            numeric_names=publication,
        ),
        "update_5m_market_control": NewsFeatureSpec(
            "update_5m_market_control",
            (),
            CONTROL_CATEGORICAL_FEATURES,
            numeric_names=update,
        ),
        "update_5m_common_direction": NewsFeatureSpec(
            "update_5m_common_direction",
            (),
            (*CONTROL_CATEGORICAL_FEATURES, "event_type", "temporal_status"),
            numeric_names=update,
        ),
        "update_5m_symmetric_materiality": NewsFeatureSpec(
            "update_5m_symmetric_materiality",
            (),
            CONTROL_CATEGORICAL_FEATURES,
            numeric_names=symmetric,
        ),
        "update_5m_corrected_reaction_interactions": NewsFeatureSpec(
            "update_5m_corrected_reaction_interactions",
            (),
            (*semantics, *CORRECTED_REACTION_INTERACTIONS),
            numeric_names=update,
        ),
    }


def build_decision_feature_rows(
    rows: Sequence[NewsFeatureRow],
    publication_times: Mapping[str, datetime],
    *,
    mode: str,
) -> list[NewsFeatureRow]:
    """Build publication or five-minute causal feature views."""
    if mode not in {"publication", "update_5m"}:
        raise ValueError(f"unsupported decision mode: {mode}")
    result = []
    for row in rows:
        published_at = publication_times[row.example_id]
        decision_at = (
            published_at if mode == "publication" else published_at + timedelta(minutes=5)
        )
        numeric = dict(row.numeric)
        numeric.update(_time_features(decision_at))
        if mode == "publication":
            for name in (*UPDATE_5M_NUMERIC_FEATURES, LEGACY_INTERACTION_FEATURE):
                numeric[name] = None
        numeric.update(_symmetric_reaction_features(numeric))
        categorical = dict(row.categorical)
        categorical["publication_session"] = _publication_session(published_at)
        reaction_sign = _reaction_sign(numeric.get("reaction_abnormal_pct"))
        if "corrected_category" in categorical and "corrected_stage" in categorical:
            categorical.update(
                {
                    "corrected_category_x_reaction_sign": (
                        f"{categorical['corrected_category']}|{reaction_sign}"
                    ),
                    "corrected_stage_x_reaction_sign": (
                        f"{categorical['corrected_stage']}|{reaction_sign}"
                    ),
                }
            )
        result.append(
            replace(
                row,
                decision_at=decision_at,
                numeric=numeric,
                categorical=categorical,
            )
        )
    return result


def remaining_four_hour_outcome(
    example: object,
    stock: MinuteOpenSeries | None,
    benchmark: MinuteOpenSeries,
    *,
    decision_delay: timedelta = timedelta(minutes=5),
    maximum_lag: timedelta = timedelta(minutes=20),
) -> dict[str, float | None]:
    """Return the move after the decision through publication plus four hours."""
    empty = {
        "remaining_raw_4h": None,
        "remaining_benchmark_4h": None,
        "remaining_abnormal_4h": None,
        "remaining_4h_available_epoch": None,
    }
    if stock is None:
        return empty
    decision_at = example.published_at + decision_delay
    target_at = example.published_at + timedelta(hours=4)
    entry_stock = stock.first_at_or_after(decision_at, maximum_lag=maximum_lag)
    entry_market = benchmark.first_at_or_after(decision_at, maximum_lag=maximum_lag)
    target_stock = stock.first_at_or_after(target_at, maximum_lag=maximum_lag)
    target_market = benchmark.first_at_or_after(target_at, maximum_lag=maximum_lag)
    if None in (entry_stock, entry_market, target_stock, target_market):
        return empty
    raw = _return(entry_stock[1], target_stock[1])
    market = _return(entry_market[1], target_market[1])
    return {
        "remaining_raw_4h": raw,
        "remaining_benchmark_4h": market,
        "remaining_abnormal_4h": raw - market,
        "remaining_4h_available_epoch": max(
            target_stock[0], target_market[0]
        ).timestamp(),
    }


def fit_news_model(
    training_rows: Sequence[NewsFeatureRow],
    training_returns: Sequence[float],
    validation_rows: Sequence[NewsFeatureRow],
    validation_returns: Sequence[float],
    *,
    spec: NewsFeatureSpec,
    regularization_c: float = REGULARIZATION_C,
) -> FittedNewsModel:
    """Fit one fixed sparse classifier and validation-only intercept offset."""
    if len(training_rows) != len(training_returns) or len(validation_rows) != len(
        validation_returns
    ):
        raise ValueError("news model rows and targets are unaligned")
    training = [
        (row, value)
        for row, value in zip(training_rows, training_returns, strict=True)
        if value != 0 and math.isfinite(value)
    ]
    validation = [
        (row, value)
        for row, value in zip(validation_rows, validation_returns, strict=True)
        if value != 0 and math.isfinite(value)
    ]
    _require_two_classes(training, "training", minimum=10)
    _require_two_classes(validation, "validation", minimum=5)
    transformer = _fit_transformer([row for row, _ in training], spec)
    weights = _group_weights([row for row, _ in training])
    classifier = _fit_classifier(
        transformer.transform([row for row, _ in training]),
        [value > 0 for _, value in training],
        weights,
        regularization_c,
    )
    validation_inputs = [row for row, _ in validation]
    raw = tuple(
        float(value)
        for value in classifier.predict_proba(
            transformer.transform(validation_inputs)
        )[:, 1]
    )
    calibrator = ValidationLogitOffset.fit(
        [value > 0 for _, value in validation],
        raw,
    )
    calibrated = tuple(calibrator.calibrate(raw))
    return FittedNewsModel(
        spec=spec,
        transformer=transformer,
        classifier=classifier,
        calibrator=calibrator,
        validation_probabilities_up=calibrated,
        coverage_thresholds={
            coverage: select_coverage_threshold(calibrated, coverage)
            for coverage in COVERAGE_TARGETS_PCT
        },
    )


def serialize_feature_row(row: NewsFeatureRow) -> dict[str, object]:
    """Return the stable JSON representation of an outcome-free row."""
    return {
        "schema_version": "direction-research-input-1.0",
        "example_id": row.example_id,
        "event_id": row.event_id,
        "event_group_id": row.event_group_id,
        "ticker": row.ticker,
        "source_id": row.source_id,
        "title": row.title,
        "content": row.content,
        "decision_at": row.decision_at.isoformat(),
        "event_type": row.event_type,
        "temporal_status": row.temporal_status,
        "numeric": dict(row.numeric),
        "categorical": dict(row.categorical),
    }


def deserialize_feature_row(payload: Mapping[str, object]) -> NewsFeatureRow:
    """Validate and load one outcome-free feature row."""
    if payload.get("schema_version") != "direction-research-input-1.0":
        raise ValueError("unexpected news feature input schema")
    return NewsFeatureRow(
        example_id=str(payload["example_id"]),
        event_id=str(payload["event_id"]),
        event_group_id=str(payload["event_group_id"]),
        ticker=str(payload["ticker"]),
        source_id=str(payload["source_id"]),
        title=str(payload["title"]),
        content=str(payload["content"]),
        decision_at=datetime.fromisoformat(str(payload["decision_at"])),
        event_type=str(payload["event_type"]),
        temporal_status=str(payload["temporal_status"]),
        numeric={
            str(key): _optional_float(value)
            for key, value in payload["numeric"].items()
        },
        categorical={
            str(key): str(value) for key, value in payload["categorical"].items()
        },
    )


class _NewsTransformer:
    """Fitted sparse feature transformer for one fixed input specification."""

    def __init__(
        self,
        spec: NewsFeatureSpec,
        numeric_vectorizer: object,
        scaler: object,
        categorical_vectorizer: object | None,
    ) -> None:
        self.spec = spec
        self.numeric_vectorizer = numeric_vectorizer
        self.scaler = scaler
        self.categorical_vectorizer = categorical_vectorizer

    def transform(self, rows: Sequence[NewsFeatureRow]):
        """Transform rows without refitting any preprocessing state."""
        from scipy.sparse import hstack

        numeric = self.scaler.transform(
            self.numeric_vectorizer.transform(
                [_numeric_record(row, self.spec) for row in rows]
            )
        )
        parts = [numeric]
        if self.categorical_vectorizer is not None:
            parts.append(
                self.categorical_vectorizer.transform(
                    [_categorical_record(row, self.spec) for row in rows]
                )
            )
        return hstack(parts, format="csr")


def _fit_transformer(
    rows: Sequence[NewsFeatureRow],
    spec: NewsFeatureSpec,
) -> _NewsTransformer:
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.preprocessing import StandardScaler

    numeric_vectorizer = DictVectorizer(sparse=True)
    numeric = numeric_vectorizer.fit_transform(
        [_numeric_record(row, spec) for row in rows]
    )
    scaler = StandardScaler(with_mean=False).fit(numeric)
    categorical_vectorizer = None
    if spec.categorical_names:
        categorical_vectorizer = DictVectorizer(sparse=True).fit(
            [_categorical_record(row, spec) for row in rows]
        )
    return _NewsTransformer(
        spec,
        numeric_vectorizer,
        scaler,
        categorical_vectorizer,
    )


def _fit_classifier(matrix, labels, weights, regularization_c):
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(
        C=regularization_c,
        class_weight=None,
        max_iter=2_000,
        random_state=0,
        solver="liblinear",
    ).fit(matrix, labels, sample_weight=weights)


def _numeric_record(
    row: NewsFeatureRow,
    spec: NewsFeatureSpec,
) -> dict[str, float]:
    result = {}
    for name, value in row.numeric.items():
        if name not in spec.numeric_names and not any(
            name.startswith(prefix) for prefix in spec.numeric_prefixes
        ):
            continue
        result[name] = float(value or 0.0)
        result[f"missing_{name}"] = float(value is None)
    return result


def _categorical_record(
    row: NewsFeatureRow,
    spec: NewsFeatureSpec,
) -> dict[str, str]:
    return {name: row.categorical[name] for name in spec.categorical_names}


def _group_weights(rows: Sequence[NewsFeatureRow]) -> list[float]:
    counts = Counter(row.event_group_id for row in rows)
    raw = [1 / counts[row.event_group_id] for row in rows]
    scale = len(raw) / sum(raw)
    return [value * scale for value in raw]


def _time_features(value: datetime) -> dict[str, float]:
    minute = value.hour * 60 + value.minute
    angle = 2 * math.pi * minute / (24 * 60)
    weekday_angle = 2 * math.pi * value.weekday() / 7
    return {
        "time_hour_sin": math.sin(angle),
        "time_hour_cos": math.cos(angle),
        "time_weekday_sin": math.sin(weekday_angle),
        "time_weekday_cos": math.cos(weekday_angle),
        "time_year": float(value.year),
        "time_month": float(value.month),
    }


def _symmetric_reaction_features(
    numeric: Mapping[str, float | None],
) -> dict[str, float | None]:
    stock = numeric.get("reaction_stock_pct")
    abnormal = numeric.get("reaction_abnormal_pct")
    volatility = numeric.get("risk_volatility_20d_pct")
    ratio = None
    if abnormal is not None and volatility is not None and volatility > 0:
        ratio = abs(abnormal) / max(volatility, 0.05)
    return {
        "derived_abs_reaction_stock_pct": abs(stock) if stock is not None else None,
        "derived_abs_reaction_abnormal_pct": (
            abs(abnormal) if abnormal is not None else None
        ),
        "derived_abs_reaction_abnormal_to_volatility": ratio,
    }


def _publication_session(value: datetime) -> str:
    utc = value.astimezone(UTC)
    minute = utc.hour * 60 + utc.minute
    if minute < 7 * 60:
        return "pre_open"
    if minute < 15 * 60 + 50:
        return "main_session"
    return "post_close"


def _reaction_sign(value: float | None) -> str:
    if value is None:
        return "missing"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def _return(start: float, end: float) -> float:
    return (end / start - 1) * 100


def _open_pairs(path: Path) -> Iterable[tuple[datetime, float]]:
    """Yield bounded timestamp/open pairs from JSONL or compressed JSONL."""
    if not path.is_file() or path.stat().st_size > MAXIMUM_OPEN_ARCHIVE_BYTES:
        raise ValueError(f"minute-open archive is missing or too large: {path}")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        for index in range(MAXIMUM_OPEN_ARCHIVE_ROWS + 1):
            line = source.readline(MAXIMUM_OPEN_LINE_BYTES + 1)
            if line == "":
                return
            if len(line) > MAXIMUM_OPEN_LINE_BYTES or index >= MAXIMUM_OPEN_ARCHIVE_ROWS:
                raise ValueError(f"minute-open archive exceeds a safety bound: {path}")
            try:
                payload = json.loads(line)
                timestamp = datetime.fromisoformat(
                    str(payload["at"]).replace("Z", "+00:00")
                )
                price = float(payload["open"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid minute-open row {path}:{index + 1}") from error
            yield timestamp, price


def _initial_return(
    series: MinuteOpenSeries | None,
    start: datetime,
    stop: datetime,
) -> float | None:
    if series is None or start > stop:
        return None
    first = series.first_at_or_after(start)
    last = series.last_at_or_before(stop)
    if first is None or last is None or first[0] > last[0]:
        return None
    return _return(first[1], last[1])


def _past_risk(
    stock: MinuteOpenSeries | None,
    benchmark: MinuteOpenSeries,
    cutoff: datetime,
) -> tuple[float | None, float | None]:
    if stock is None:
        return None, None
    stock_returns = _daily_returns(stock, cutoff)
    benchmark_returns = _daily_returns(benchmark, cutoff)
    dates = sorted(set(stock_returns) & set(benchmark_returns))[-60:]
    stock_values = [stock_returns[value] for value in dates]
    benchmark_values = [benchmark_returns[value] for value in dates]
    volatility = (
        statistics.stdev(stock_values[-20:]) if len(stock_values) >= 20 else None
    )
    if len(stock_values) < 20 or statistics.pvariance(benchmark_values) == 0:
        return volatility, None
    covariance = statistics.covariance(stock_values, benchmark_values)
    beta = covariance / statistics.variance(benchmark_values)
    return volatility, max(-2.0, min(4.0, beta))


def _daily_returns(
    series: MinuteOpenSeries,
    cutoff: datetime,
) -> dict[date, float]:
    dates = sorted(
        day for day, timestamp in series.daily_times.items() if timestamp <= cutoff
    )
    return {
        current: _return(series.daily_prices[previous], series.daily_prices[current])
        for previous, current in zip(dates, dates[1:], strict=False)
    }


def _beta_adjusted_pre_event(example: object, beta: float | None) -> float | None:
    stock = example.features.pre_event_return_1h_pct
    benchmark = example.features.benchmark_pre_event_return_1h_pct
    if beta is None or stock is None or benchmark is None:
        return None
    return stock - beta * benchmark


def _subtract(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _require_two_classes(rows, name: str, *, minimum: int) -> None:
    counts = Counter(value > 0 for _, value in rows)
    if len(counts) != 2 or min(counts.values()) < minimum:
        raise ValueError(f"{name} requires at least {minimum} rows per direction")


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
