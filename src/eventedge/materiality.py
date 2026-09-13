"""Runtime inference for the retained news-materiality model."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from zoneinfo import ZoneInfo

ARTIFACT_SCHEMA_VERSION = "news-materiality-model-artifact-1.0"
MODEL_ID = "update_5m_symmetric_materiality_logistic"
MODEL_VERSION = "update-5m-symmetric-materiality-logistic-1.0"
PREDICTION_SCHEMA_VERSION = "news-materiality-prediction-1.0"
TARGET = "materiality_4h"
TARGET_DEFINITION = (
    "abs(stock_return(entry_at,entry_at+4h)-"
    "benchmark_return(entry_at,entry_at+4h))>=0.5_pct_points"
)
FEATURE_SCHEMA_VERSION = "news-materiality-runtime-features-1.0"
FEATURE_SPEC_NAME = "update_5m_symmetric_materiality"
PACKAGED_ARTIFACT_NAME = "news-materiality-v1.json"
PACKAGED_ARTIFACT_PAYLOAD_SHA256 = (
    "18bb8bd1d91078710ed83379aa04c7110dd547bf87ffd914c5fb44c616ab9b6c"
)
MAXIMUM_ARTIFACT_BYTES = 5_000_000
MAXIMUM_MODEL_FEATURES = 10_000
MAXIMUM_STRING_LENGTH = 10_000
DECISION_DELAY = timedelta(minutes=5)
MAXIMUM_BOUNDARY_LAG = timedelta(minutes=20)
PRE_EVENT_MOMENTUM_HORIZON = timedelta(hours=1)
PRE_EVENT_RETURN_1D_HORIZON = timedelta(days=1)
PRE_EVENT_RETURN_5D_HORIZON = timedelta(days=5)
MAXIMUM_LONG_PRE_EVENT_LAG = timedelta(days=4)
MARKET_TIMEZONE = ZoneInfo("Europe/Moscow")
MATERIALITY_BENCHMARK_TICKER = "IMOEX2"
INTRADAY_LOOKBACK_DAYS = 14
DAILY_LOOKBACK_DAYS = 120
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

NUMERIC_FEATURE_NAMES = (
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
    "reaction_stock_pct",
    "reaction_benchmark_pct",
    "reaction_abnormal_pct",
    "derived_abs_reaction_stock_pct",
    "derived_abs_reaction_abnormal_pct",
    "derived_abs_reaction_abnormal_to_volatility",
)
CATEGORICAL_FEATURE_NAMES = ("ticker", "source_id", "publication_session")

_ARTIFACT_KEYS = {
    "schema_version",
    "artifact_payload_sha256",
    "model_id",
    "model_version",
    "task",
    "target",
    "target_definition",
    "decision_mode",
    "feature_spec",
    "preprocessing",
    "classifier",
    "calibration",
    "coverage_thresholds",
    "provenance",
}
_PROVENANCE_KEYS = {
    "dataset_sha256",
    "fit_bundle_sha256",
    "feature_input_filename",
    "feature_input_sha256",
    "fold",
    "fit_policy",
    "training_rows",
    "validation_rows",
    "training_until_exclusive",
    "validation_from_inclusive",
    "validation_until_exclusive",
    "evaluation_from_inclusive",
    "evaluation_until_exclusive",
}


class MaterialityMode(StrEnum):
    """How materiality predictions affect the product pipeline."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    RANK = "rank"


class MaterialityFeatureInput(Protocol):
    """Minimum outcome-free input accepted by portable inference."""

    ticker: str
    source_id: str
    numeric: Mapping[str, float | None]
    categorical: Mapping[str, str]


@dataclass(frozen=True)
class MaterialityFeatureRow:
    """Causal feature row available at the five-minute decision boundary."""

    news_id: str
    ticker: str
    source_id: str
    decision_at: datetime
    numeric: Mapping[str, float | None]
    categorical: Mapping[str, str]
    feature_schema_version: str = FEATURE_SCHEMA_VERSION


@dataclass(frozen=True)
class MaterialityScore:
    """One calibrated materiality score bound to its immutable model artifact."""

    raw_probability: float
    calibrated_probability: float
    selected_at_coverage_pct: Mapping[str, tuple[int, ...]]
    model_id: str
    model_version: str
    artifact_payload_sha256: str


@dataclass(frozen=True)
class PortableMaterialityModel:
    """Validated logistic model that requires no executable model format."""

    artifact_payload_sha256: str
    numeric_feature_names: tuple[str, ...]
    numeric_scale_factors: tuple[float, ...]
    categorical_feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    calibration_offset: float
    coverage_thresholds: Mapping[str, Mapping[str, float]]

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    def raw_probability_material(self, row: MaterialityFeatureInput) -> float:
        """Return the uncalibrated probability of a material market move."""
        numeric = _numeric_record(row)
        categorical = _categorical_record(row)
        linear = self.intercept
        for index, name in enumerate(self.numeric_feature_names):
            linear += (
                self.coefficients[index]
                * numeric.get(name, 0.0)
                / self.numeric_scale_factors[index]
            )
        offset = len(self.numeric_feature_names)
        for index, name in enumerate(self.categorical_feature_names, offset):
            linear += self.coefficients[index] * float(name in categorical)
        return _sigmoid(linear)

    def probability_material(self, row: MaterialityFeatureInput) -> float:
        """Return the validation-calibrated probability of a material move."""
        raw = self.raw_probability_material(row)
        return _sigmoid(_logit(raw) + self.calibration_offset)

    def predict(self, row: MaterialityFeatureInput) -> MaterialityScore:
        """Score one causal row and report its validation coverage cohorts."""
        raw = self.raw_probability_material(row)
        calibrated = _sigmoid(_logit(raw) + self.calibration_offset)
        return MaterialityScore(
            raw_probability=raw,
            calibrated_probability=calibrated,
            selected_at_coverage_pct=MappingProxyType(
                {
                    score: tuple(
                        int(coverage)
                        for coverage, threshold in sorted(
                            thresholds.items(), key=lambda item: int(item[0])
                        )
                        if (raw if score == "raw" else calibrated) >= threshold
                    )
                    for score, thresholds in self.coverage_thresholds.items()
                }
            ),
            model_id=MODEL_ID,
            model_version=MODEL_VERSION,
            artifact_payload_sha256=self.artifact_payload_sha256,
        )

    def category_was_seen(self, name: str, value: str) -> bool:
        """Return whether training preprocessing contains this category."""
        if name not in CATEGORICAL_FEATURE_NAMES:
            raise ValueError(f"unsupported materiality category: {name}")
        return f"{name}={value}" in self.categorical_feature_names


@dataclass(frozen=True)
class MaterialityRuntime:
    """Environment-selected materiality behavior and its loaded model."""

    mode: MaterialityMode
    model: PortableMaterialityModel | None
    batch_limit: int

    @property
    def enabled(self) -> bool:
        return self.model is not None


@dataclass(frozen=True)
class _OpenSeries:
    """Sorted candle opens used only up to an explicit decision boundary."""

    times: tuple[datetime, ...]
    prices: tuple[float, ...]

    @classmethod
    def from_market_payload(cls, value: Mapping[str, object]) -> _OpenSeries:
        candles = value.get("candles")
        if not isinstance(candles, list):
            raise ValueError("market candle payload must contain a candle list")
        observations: dict[datetime, float] = {}
        for candle in candles:
            if not isinstance(candle, dict):
                raise ValueError("market candle must be an object")
            timestamp = _market_timestamp(candle.get("begin"))
            price = _positive_float(candle.get("open"), "market candle open")
            current = observations.get(timestamp)
            if current is not None and current != price:
                raise ValueError("market payload contains conflicting candle opens")
            observations[timestamp] = price
        if not observations:
            raise ValueError("market candle payload is empty")
        ordered = sorted(observations.items())
        return cls(
            times=tuple(item[0] for item in ordered),
            prices=tuple(item[1] for item in ordered),
        )

    def first_at_or_after(
        self,
        requested: datetime,
        *,
        maximum_lag: timedelta = MAXIMUM_BOUNDARY_LAG,
    ) -> tuple[datetime, float] | None:
        index = bisect_left(self.times, requested)
        if index >= len(self.times) or self.times[index] - requested > maximum_lag:
            return None
        return self.times[index], self.prices[index]

    def last_at_or_before(
        self,
        requested: datetime,
        *,
        maximum_lag: timedelta = MAXIMUM_BOUNDARY_LAG,
    ) -> tuple[datetime, float] | None:
        index = bisect_right(self.times, requested) - 1
        if index < 0 or requested - self.times[index] > maximum_lag:
            return None
        return self.times[index], self.prices[index]


def build_materiality_feature_row(
    *,
    news_id: str,
    ticker: str,
    source_id: str,
    published_at: datetime,
    stock_intraday: Mapping[str, object],
    benchmark_intraday: Mapping[str, object],
    stock_daily: Mapping[str, object],
    benchmark_daily: Mapping[str, object],
) -> MaterialityFeatureRow:
    """Build the retained model's causal feature contract at publication +5m."""
    _require_aware(published_at, "published_at")
    published_at = published_at.astimezone(UTC)
    decision_at = published_at + DECISION_DELAY
    stock_series = _OpenSeries.from_market_payload(stock_intraday)
    benchmark_series = _OpenSeries.from_market_payload(benchmark_intraday)
    stock_return_1h = _pre_event_return(
        stock_series,
        decision_at,
        horizon=PRE_EVENT_MOMENTUM_HORIZON,
        maximum_past_lag=MAXIMUM_BOUNDARY_LAG,
        require_same_market_date=True,
    )
    benchmark_return_1h = _pre_event_return(
        benchmark_series,
        decision_at,
        horizon=PRE_EVENT_MOMENTUM_HORIZON,
        maximum_past_lag=MAXIMUM_BOUNDARY_LAG,
        require_same_market_date=True,
    )
    risk_volatility, risk_beta = _past_risk(
        stock_daily,
        benchmark_daily,
        decision_at,
    )
    reaction_stock = _initial_return(stock_series, published_at, decision_at)
    reaction_benchmark = _initial_return(
        benchmark_series,
        published_at,
        decision_at,
    )
    reaction_abnormal = _subtract(reaction_stock, reaction_benchmark)
    numeric = {
        "market_pre_event_return_1h_pct": stock_return_1h,
        "market_pre_event_return_1d_pct": _pre_event_return(
            stock_series,
            decision_at,
            horizon=PRE_EVENT_RETURN_1D_HORIZON,
            maximum_past_lag=MAXIMUM_LONG_PRE_EVENT_LAG,
            require_same_market_date=False,
        ),
        "market_pre_event_return_5d_pct": _pre_event_return(
            stock_series,
            decision_at,
            horizon=PRE_EVENT_RETURN_5D_HORIZON,
            maximum_past_lag=MAXIMUM_LONG_PRE_EVENT_LAG,
            require_same_market_date=False,
        ),
        "market_benchmark_pre_event_return_1h_pct": benchmark_return_1h,
        "risk_volatility_20d_pct": risk_volatility,
        "risk_beta_60d": risk_beta,
        "risk_beta_adjusted_pre_event_1h_pct": (
            stock_return_1h - risk_beta * benchmark_return_1h
            if stock_return_1h is not None
            and benchmark_return_1h is not None
            and risk_beta is not None
            else None
        ),
        **_time_features(decision_at),
        "reaction_stock_pct": reaction_stock,
        "reaction_benchmark_pct": reaction_benchmark,
        "reaction_abnormal_pct": reaction_abnormal,
    }
    numeric.update(_symmetric_reaction_features(numeric))
    return MaterialityFeatureRow(
        news_id=news_id,
        ticker=ticker,
        source_id=source_id,
        decision_at=decision_at,
        numeric=MappingProxyType(numeric),
        categorical=MappingProxyType(
            {
                "ticker": ticker,
                "source_id": source_id,
                "publication_session": _publication_session(published_at),
            }
        ),
    )


def missing_materiality_features(row: MaterialityFeatureRow) -> tuple[str, ...]:
    """Return model features unavailable at the causal decision boundary."""
    return tuple(name for name in NUMERIC_FEATURE_NAMES if row.numeric.get(name) is None)


def runtime_from_environment(environment: Mapping[str, str] | None = None) -> MaterialityRuntime:
    """Load the pinned model only when materiality inference is enabled."""
    values = environment if environment is not None else os.environ
    try:
        mode = MaterialityMode(values.get("NEWS_MATERIALITY_MODE", "disabled").casefold())
    except ValueError as error:
        raise RuntimeError(
            "NEWS_MATERIALITY_MODE must be disabled, shadow or rank"
        ) from error
    try:
        batch_limit = int(values.get("NEWS_MATERIALITY_BATCH_LIMIT", "8"))
    except ValueError as error:
        raise RuntimeError("NEWS_MATERIALITY_BATCH_LIMIT must be an integer") from error
    if not 1 <= batch_limit <= 40:
        raise RuntimeError("NEWS_MATERIALITY_BATCH_LIMIT must be between 1 and 40")
    if mode is MaterialityMode.DISABLED:
        return MaterialityRuntime(mode=mode, model=None, batch_limit=batch_limit)

    expected_digest = values.get(
        "NEWS_MATERIALITY_EXPECTED_SHA256",
        PACKAGED_ARTIFACT_PAYLOAD_SHA256,
    )
    configured_path = values.get("NEWS_MATERIALITY_ARTIFACT_PATH")
    try:
        model = (
            load_materiality_model(Path(configured_path), expected_digest)
            if configured_path
            else load_packaged_materiality_model(expected_digest)
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Unable to load the configured news materiality model") from error
    return MaterialityRuntime(mode=mode, model=model, batch_limit=batch_limit)


def load_packaged_materiality_model(
    expected_payload_sha256: str = PACKAGED_ARTIFACT_PAYLOAD_SHA256,
) -> PortableMaterialityModel:
    """Load the model shipped atomically with the EventEdge application."""
    artifact = resources.files("eventedge").joinpath("models", PACKAGED_ARTIFACT_NAME)
    payload_bytes = artifact.read_bytes()
    return load_materiality_payload_bytes(payload_bytes, expected_payload_sha256)


def load_materiality_model(
    path: Path,
    expected_payload_sha256: str,
) -> PortableMaterialityModel:
    """Read a bounded artifact file and require its configured digest."""
    if not path.is_file() or path.stat().st_size > MAXIMUM_ARTIFACT_BYTES:
        raise ValueError("materiality artifact is missing or too large")
    return load_materiality_payload_bytes(path.read_bytes(), expected_payload_sha256)


def load_materiality_payload_bytes(
    payload_bytes: bytes,
    expected_payload_sha256: str,
) -> PortableMaterialityModel:
    """Decode and validate a bounded serialized artifact."""
    if not payload_bytes or len(payload_bytes) > MAXIMUM_ARTIFACT_BYTES:
        raise ValueError("materiality artifact is empty or too large")
    payload = json.loads(payload_bytes)
    return load_materiality_payload(payload, expected_payload_sha256)


def load_materiality_payload(
    value: object,
    expected_payload_sha256: str,
) -> PortableMaterialityModel:
    """Validate an in-memory artifact before constructing runtime state."""
    payload = _object(value, "artifact")
    _require_exact_keys(payload, _ARTIFACT_KEYS, "artifact")
    expected_digest = _digest(expected_payload_sha256, "expected artifact digest")
    payload_digest = _digest(payload["artifact_payload_sha256"], "artifact digest")
    if artifact_payload_sha256(payload) != payload_digest:
        raise ValueError("materiality artifact payload digest mismatch")
    if payload_digest != expected_digest:
        raise ValueError("materiality artifact does not match the expected digest")
    _validate_identity(payload)
    _validate_feature_spec(_object(payload["feature_spec"], "feature spec"))
    numeric_names, scales, categorical_names = _load_preprocessing(
        _object(payload["preprocessing"], "preprocessing")
    )
    coefficients, intercept = _load_classifier(
        _object(payload["classifier"], "classifier"),
        len(numeric_names) + len(categorical_names),
    )
    calibration = _object(payload["calibration"], "calibration")
    calibration_offset = _validate_calibration(calibration)
    provenance = _object(payload["provenance"], "provenance")
    _validate_provenance(provenance)
    if calibration["observations"] != provenance["validation_rows"]:
        raise ValueError("materiality calibration and validation rows are unaligned")
    thresholds = _load_thresholds(payload["coverage_thresholds"])
    _validate_threshold_calibration(thresholds, calibration_offset)
    return PortableMaterialityModel(
        artifact_payload_sha256=payload_digest,
        numeric_feature_names=numeric_names,
        numeric_scale_factors=scales,
        categorical_feature_names=categorical_names,
        coefficients=coefficients,
        intercept=intercept,
        calibration_offset=calibration_offset,
        coverage_thresholds=MappingProxyType(
            {
                score: MappingProxyType(dict(values))
                for score, values in thresholds.items()
            }
        ),
    )


def artifact_payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash canonical artifact content while excluding its self digest."""
    content = dict(payload)
    content.pop("artifact_payload_sha256", None)
    encoded = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pre_event_return(
    series: _OpenSeries,
    decision_at: datetime,
    *,
    horizon: timedelta,
    maximum_past_lag: timedelta,
    require_same_market_date: bool,
) -> float | None:
    current_index = bisect_left(series.times, decision_at) - 1
    if current_index < 0:
        return None
    current_at = series.times[current_index]
    if decision_at - current_at > MAXIMUM_BOUNDARY_LAG:
        return None
    target_at = current_at - horizon
    previous_index = bisect_right(series.times, target_at, hi=current_index) - 1
    if previous_index < 0:
        return None
    previous_at = series.times[previous_index]
    if target_at - previous_at > maximum_past_lag:
        return None
    if require_same_market_date and (
        current_at.astimezone(MARKET_TIMEZONE).date()
        != previous_at.astimezone(MARKET_TIMEZONE).date()
    ):
        return None
    return _return_pct(series.prices[previous_index], series.prices[current_index])


def _initial_return(
    series: _OpenSeries,
    start: datetime,
    stop: datetime,
) -> float | None:
    first = series.first_at_or_after(start)
    last = series.last_at_or_before(stop)
    if first is None or last is None or first[0] > last[0]:
        return None
    return _return_pct(first[1], last[1])


def _past_risk(
    stock_daily: Mapping[str, object],
    benchmark_daily: Mapping[str, object],
    cutoff: datetime,
) -> tuple[float | None, float | None]:
    stock_returns = _daily_returns(stock_daily, cutoff)
    benchmark_returns = _daily_returns(benchmark_daily, cutoff)
    dates = sorted(set(stock_returns) & set(benchmark_returns))[-60:]
    stock_values = [stock_returns[value] for value in dates]
    benchmark_values = [benchmark_returns[value] for value in dates]
    volatility = (
        statistics.stdev(stock_values[-20:]) if len(stock_values) >= 20 else None
    )
    if len(stock_values) < 20 or statistics.pvariance(benchmark_values) == 0:
        return volatility, None
    beta = statistics.covariance(stock_values, benchmark_values) / statistics.variance(
        benchmark_values
    )
    return volatility, max(-2.0, min(4.0, beta))


def _daily_returns(
    value: Mapping[str, object],
    cutoff: datetime,
) -> dict[date, float]:
    candles = value.get("candles")
    if not isinstance(candles, list):
        raise ValueError("daily market payload must contain a candle list")
    daily_opens: dict[date, float] = {}
    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("daily market candle must be an object")
        timestamp = _market_timestamp(candle.get("begin"))
        market_day = timestamp.astimezone(MARKET_TIMEZONE).date()
        session_open = datetime.combine(
            market_day,
            datetime.min.time(),
            MARKET_TIMEZONE,
        ).replace(hour=10)
        if session_open.astimezone(UTC) > cutoff:
            continue
        price = _positive_float(candle.get("open"), "daily market candle open")
        current = daily_opens.get(market_day)
        if current is not None and current != price:
            raise ValueError("daily market payload contains conflicting opens")
        daily_opens[market_day] = price
    days = sorted(daily_opens)
    return {
        current: _return_pct(daily_opens[previous], daily_opens[current])
        for previous, current in zip(days, days[1:], strict=False)
    }


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
    minute = value.astimezone(UTC).hour * 60 + value.astimezone(UTC).minute
    if minute < 7 * 60:
        return "pre_open"
    if minute < 15 * 60 + 50:
        return "main_session"
    return "post_close"


def _return_pct(start: float, end: float) -> float:
    return (end / start - 1) * 100


def _subtract(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _market_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("market candle timestamp must be a string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("market candle timestamp is invalid") from error
    _require_aware(result, "market candle timestamp")
    return result


def _positive_float(value: object, name: str) -> float:
    result = _finite_float(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_identity(payload: Mapping[str, object]) -> None:
    expected = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "task": "materiality",
        "target": TARGET,
        "target_definition": TARGET_DEFINITION,
        "decision_mode": "update_5m",
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise ValueError("materiality artifact identity is incompatible")


def _validate_feature_spec(payload: Mapping[str, object]) -> None:
    _require_exact_keys(
        payload,
        {
            "name",
            "numeric_prefixes",
            "numeric_names",
            "categorical_names",
            "include_text",
        },
        "feature spec",
    )
    if (
        payload["name"] != FEATURE_SPEC_NAME
        or payload["numeric_prefixes"] != []
        or _string_tuple(payload["numeric_names"], "numeric feature names")
        != NUMERIC_FEATURE_NAMES
        or _string_tuple(payload["categorical_names"], "categorical feature names")
        != CATEGORICAL_FEATURE_NAMES
        or payload["include_text"] is not False
    ):
        raise ValueError("materiality artifact feature spec is incompatible")


def _load_preprocessing(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[float, ...], tuple[str, ...]]:
    _require_exact_keys(
        payload,
        {
            "numeric_feature_names",
            "numeric_scale_factors",
            "categorical_feature_names",
        },
        "preprocessing",
    )
    numeric = _string_tuple(payload["numeric_feature_names"], "numeric features")
    scales = _float_tuple(payload["numeric_scale_factors"], "numeric scales")
    categorical = _string_tuple(
        payload["categorical_feature_names"], "categorical features"
    )
    if not numeric or len(numeric) != len(scales) or any(value <= 0 for value in scales):
        raise ValueError("materiality artifact numeric preprocessing is invalid")
    if len(numeric) + len(categorical) > MAXIMUM_MODEL_FEATURES:
        raise ValueError("materiality artifact has too many model features")
    for feature in numeric:
        if feature.removeprefix("missing_") not in NUMERIC_FEATURE_NAMES:
            raise ValueError("materiality artifact has an unknown numeric feature")
    prefixes = tuple(f"{name}=" for name in CATEGORICAL_FEATURE_NAMES)
    if any(not feature.startswith(prefixes) for feature in categorical):
        raise ValueError("materiality artifact has an unknown categorical feature")
    return numeric, scales, categorical


def _load_classifier(
    payload: Mapping[str, object],
    feature_count: int,
) -> tuple[tuple[float, ...], float]:
    _require_exact_keys(
        payload,
        {"kind", "solver", "regularization_c", "classes", "coefficients", "intercept"},
        "classifier",
    )
    if (
        payload["kind"] != "binary_logistic_regression"
        or payload["solver"] != "liblinear"
        or payload["classes"] != [0, 1]
        or _finite_float(payload["regularization_c"], "regularization C") <= 0
    ):
        raise ValueError("materiality artifact classifier is incompatible")
    coefficients = _float_tuple(payload["coefficients"], "classifier coefficients")
    if len(coefficients) != feature_count:
        raise ValueError("materiality artifact coefficient count is invalid")
    return coefficients, _finite_float(payload["intercept"], "classifier intercept")


def _validate_calibration(payload: Mapping[str, object]) -> float:
    _require_exact_keys(
        payload,
        {"kind", "offset", "observed_prevalence", "observations"},
        "calibration",
    )
    prevalence = _finite_float(payload["observed_prevalence"], "prevalence")
    observations = payload["observations"]
    if (
        payload["kind"] != "validation_logit_offset"
        or not 0 < prevalence < 1
        or isinstance(observations, bool)
        or not isinstance(observations, int)
        or observations <= 0
    ):
        raise ValueError("materiality artifact calibration is invalid")
    return _finite_float(payload["offset"], "calibration offset")


def _load_thresholds(value: object) -> dict[str, dict[str, float]]:
    payload = _object(value, "coverage thresholds")
    _require_exact_keys(payload, {"raw", "calibrated"}, "coverage thresholds")
    result = {}
    for score in ("raw", "calibrated"):
        thresholds = _object(payload[score], f"{score} coverage thresholds")
        _require_exact_keys(thresholds, {"20", "50", "100"}, f"{score} thresholds")
        result[score] = {
            coverage: _probability(threshold, f"{score} {coverage} threshold")
            for coverage, threshold in thresholds.items()
        }
        if not (
            result[score]["20"] >= result[score]["50"] >= result[score]["100"]
            and result[score]["100"] == 0.0
        ):
            raise ValueError("materiality coverage thresholds are not monotonic")
    return result


def _validate_threshold_calibration(
    thresholds: Mapping[str, Mapping[str, float]],
    calibration_offset: float,
) -> None:
    for coverage in ("20", "50"):
        expected = _sigmoid(
            _logit(thresholds["raw"][coverage]) + calibration_offset
        )
        if not math.isclose(
            thresholds["calibrated"][coverage], expected, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("materiality coverage threshold calibration is invalid")


def _validate_provenance(payload: Mapping[str, object]) -> None:
    _require_exact_keys(payload, _PROVENANCE_KEYS, "provenance")
    for name in ("dataset_sha256", "fit_bundle_sha256", "feature_input_sha256"):
        _digest(payload[name], name)
    filename = _string(payload["feature_input_filename"], "feature input filename")
    if Path(filename).name != filename or Path(filename).suffix != ".jsonl":
        raise ValueError("materiality artifact feature input filename is invalid")
    if payload["fit_policy"] != "latest_walk_forward_fold_no_post_evaluation_refit":
        raise ValueError("materiality artifact fit policy is incompatible")
    _string(payload["fold"], "fold")
    for name in ("training_rows", "validation_rows"):
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"materiality artifact {name} is invalid")
    cutoffs = [
        _aware_datetime(payload[name], name)
        for name in (
            "training_until_exclusive",
            "validation_from_inclusive",
            "validation_until_exclusive",
            "evaluation_from_inclusive",
            "evaluation_until_exclusive",
        )
    ]
    if (
        cutoffs[0] != cutoffs[1]
        or cutoffs[2] != cutoffs[3]
        or cutoffs != sorted(cutoffs)
        or cutoffs[1] >= cutoffs[2]
        or cutoffs[3] >= cutoffs[4]
    ):
        raise ValueError("materiality artifact temporal cutoffs are invalid")


def _numeric_record(row: MaterialityFeatureInput) -> dict[str, float]:
    result = {}
    for name in NUMERIC_FEATURE_NAMES:
        value = row.numeric.get(name)
        if value is not None and not math.isfinite(float(value)):
            raise ValueError(f"numeric feature is not finite: {name}")
        result[name] = float(value or 0.0)
        result[f"missing_{name}"] = float(value is None)
    return result


def _categorical_record(row: MaterialityFeatureInput) -> set[str]:
    missing = [name for name in CATEGORICAL_FEATURE_NAMES if name not in row.categorical]
    if missing:
        raise ValueError(f"missing categorical features: {', '.join(missing)}")
    for name, expected in (("ticker", row.ticker), ("source_id", row.source_id)):
        if row.categorical.get(name) != expected:
            raise ValueError(f"feature row {name} identity differs from its category")
    return {f"{name}={row.categorical[name]}" for name in CATEGORICAL_FEATURE_NAMES}


def _require_exact_keys(
    payload: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    if set(payload) != expected:
        raise ValueError(f"materiality {name} fields are incompatible")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"materiality {name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAXIMUM_STRING_LENGTH:
        raise ValueError(f"materiality {name} must be a non-empty string")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"materiality {name} must be a list")
    result = tuple(_string(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"materiality {name} must be unique")
    return result


def _float_tuple(value: object, name: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"materiality {name} must be a list")
    return tuple(_finite_float(item, name) for item in value)


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"materiality {name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"materiality {name} must be finite")
    return result


def _probability(value: object, name: str) -> float:
    result = _finite_float(value, name)
    if not 0 <= result <= 1:
        raise ValueError(f"materiality {name} must be within [0, 1]")
    return result


def _digest(value: object, name: str) -> str:
    result = _string(value, name)
    if not _DIGEST_PATTERN.fullmatch(result):
        raise ValueError(f"materiality {name} is not a SHA-256 digest")
    return result


def _aware_datetime(value: object, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(_string(value, name))
    except ValueError as error:
        raise ValueError(f"materiality {name} is not an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"materiality {name} must be timezone-aware")
    return result


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-value)
        return 1 / (1 + exponent)
    exponent = math.exp(value)
    return exponent / (1 + exponent)


def _logit(value: float) -> float:
    clipped = min(max(value, 1e-12), 1 - 1e-12)
    return math.log(clipped / (1 - clipped))
