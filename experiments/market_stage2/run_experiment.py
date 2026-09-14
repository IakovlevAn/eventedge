from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    CATEGORICAL_BASE_FEATURES,
    NUMERIC_BASE_FEATURES,
    SEED,
    SENTIMENT_FEATURES,
    TEST_MONTHS,
    add_sentiment,
    binary_metrics,
    build_folds,
    load_dataset,
    sha256_file,
)
from experiments.paths import (  # noqa: E402
    dataset_path_from_environment,
    stage_artifact_directory,
)

EXPERIMENT_VERSION = "market-stage2-1.0"
CACHE_VERSION = "moex-predecision-minute-cache-1.0"
MOEX_ISS_BASE_URL = "https://iss.moex.com/iss"
MOSCOW = ZoneInfo("Europe/Moscow")
PAGE_SIZE = 500
MAX_PAGES = 3
RETAIN_LOOKBACK = pd.Timedelta(minutes=90)
FRESH_QUOTE_MINUTES = 3.0
C_GRID = (0.03, 0.1, 0.3, 1.0, 3.0)
CLASS_WEIGHT_GRID: tuple[str | None, ...] = (None, "balanced")

PREPUBLICATION_FEATURES = (
    "stock_pre_return_5m_pct",
    "stock_pre_return_15m_pct",
    "stock_pre_return_60m_pct",
    "benchmark_pre_return_5m_pct",
    "benchmark_pre_return_15m_pct",
    "benchmark_pre_return_60m_pct",
    "abnormal_pre_return_5m_pct",
    "abnormal_pre_return_15m_pct",
    "abnormal_pre_return_60m_pct",
    "abs_abnormal_pre_return_5m_pct",
    "abs_abnormal_pre_return_15m_pct",
    "abs_abnormal_pre_return_60m_pct",
    "stock_pre_volatility_30m_pct",
    "benchmark_pre_volatility_30m_pct",
    "stock_pre_range_30m_pct",
    "benchmark_pre_range_30m_pct",
    "stock_publication_quote_age_minutes",
    "benchmark_publication_quote_age_minutes",
)

REACTION_PRICE_FEATURES = (
    "stock_reaction_0_5m_pct",
    "benchmark_reaction_0_5m_pct",
    "abnormal_reaction_0_5m_pct",
    "abs_stock_reaction_0_5m_pct",
    "abs_benchmark_reaction_0_5m_pct",
    "abs_abnormal_reaction_0_5m_pct",
    "stock_decision_return_5m_pct",
    "stock_decision_return_15m_pct",
    "stock_decision_return_60m_pct",
    "benchmark_decision_return_5m_pct",
    "benchmark_decision_return_15m_pct",
    "benchmark_decision_return_60m_pct",
    "abnormal_decision_return_5m_pct",
    "abnormal_decision_return_15m_pct",
    "abnormal_decision_return_60m_pct",
    "abs_abnormal_decision_return_5m_pct",
    "abs_abnormal_decision_return_15m_pct",
    "abs_abnormal_decision_return_60m_pct",
    "market_data_available",
)

REACTION_CORE_FEATURES = (
    "abnormal_reaction_0_5m_pct",
    "abs_abnormal_reaction_0_5m_pct",
    "market_data_available",
)

MICROSTRUCTURE_FEATURES = (
    "stock_reaction_range_pct",
    "benchmark_reaction_range_pct",
    "stock_reaction_volume_ratio",
    "stock_reaction_value_ratio",
    "stock_reaction_candle_count",
    "benchmark_reaction_candle_count",
    "stock_decision_volatility_30m_pct",
    "benchmark_decision_volatility_30m_pct",
    "stock_decision_range_30m_pct",
    "benchmark_decision_range_30m_pct",
    "stock_decision_quote_age_minutes",
    "benchmark_decision_quote_age_minutes",
    "decision_time_sin",
    "decision_time_cos",
)

FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "legacy": tuple(NUMERIC_BASE_FEATURES),
    "prepublication": tuple(NUMERIC_BASE_FEATURES) + PREPUBLICATION_FEATURES,
    "reaction_core": tuple(NUMERIC_BASE_FEATURES) + REACTION_CORE_FEATURES,
    "reaction_core_finbert": (
        tuple(NUMERIC_BASE_FEATURES) + REACTION_CORE_FEATURES + tuple(SENTIMENT_FEATURES)
    ),
    "reaction_price": (
        tuple(NUMERIC_BASE_FEATURES) + PREPUBLICATION_FEATURES + REACTION_PRICE_FEATURES
    ),
    "reaction_microstructure": (
        tuple(NUMERIC_BASE_FEATURES)
        + PREPUBLICATION_FEATURES
        + REACTION_PRICE_FEATURES
        + MICROSTRUCTURE_FEATURES
    ),
    "reaction_microstructure_finbert": (
        tuple(NUMERIC_BASE_FEATURES)
        + PREPUBLICATION_FEATURES
        + REACTION_PRICE_FEATURES
        + MICROSTRUCTURE_FEATURES
        + tuple(SENTIMENT_FEATURES)
    ),
}


@dataclass(frozen=True)
class RequestSpec:
    market: str
    ticker: str
    day: str
    retain_from: pd.Timestamp
    needed_until: pd.Timestamp

    @property
    def key(self) -> str:
        return f"{self.market}:{self.ticker}:{self.day}"


def assert_feature_contract() -> None:
    forbidden = ("return_4h", "label", "entry_at", "outcome")
    violations = [
        feature
        for features in FEATURE_SETS.values()
        for feature in features
        if any(fragment in feature for fragment in forbidden)
    ]
    if violations:
        raise AssertionError(f"post-outcome feature selected: {sorted(set(violations))}")
    required_subsets = (
        ("legacy", "prepublication"),
        ("legacy", "reaction_core"),
        ("reaction_core", "reaction_core_finbert"),
        ("prepublication", "reaction_price"),
        ("reaction_core", "reaction_price"),
        ("reaction_price", "reaction_microstructure"),
        ("reaction_microstructure", "reaction_microstructure_finbert"),
    )
    for earlier, later in required_subsets:
        if not set(FEATURE_SETS[earlier]) < set(FEATURE_SETS[later]):
            raise AssertionError(f"feature sets are not strictly nested: {earlier}, {later}")


def _request_specs(frame: pd.DataFrame) -> list[RequestSpec]:
    raw: dict[tuple[str, str, str], dict[str, pd.Timestamp]] = {}
    for row in frame.itertuples(index=False):
        day = row.published_at.tz_convert(MOSCOW).date().isoformat()
        for market, ticker in (("shares", row.ticker), ("index", "IMOEX2")):
            key = (market, ticker, day)
            retain_from = row.published_at - RETAIN_LOOKBACK
            current = raw.get(key)
            if current is None:
                raw[key] = {
                    "retain_from": retain_from,
                    "needed_until": row.decision_at,
                }
            else:
                current["retain_from"] = min(current["retain_from"], retain_from)
                current["needed_until"] = max(current["needed_until"], row.decision_at)
    return sorted(
        (
            RequestSpec(
                market=market,
                ticker=ticker,
                day=day,
                retain_from=window["retain_from"],
                needed_until=window["needed_until"],
            )
            for (market, ticker, day), window in raw.items()
        ),
        key=lambda item: item.key,
    )


def _spec_signature(specs: list[RequestSpec]) -> str:
    payload = [
        {
            "key": spec.key,
            "retain_from": spec.retain_from.isoformat(),
            "needed_until": spec.needed_until.isoformat(),
        }
        for spec in specs
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _moex_url(spec: RequestSpec) -> str:
    market_path = "shares/boards/TQBR" if spec.market == "shares" else "index/boards/SNDX"
    return (
        f"{MOEX_ISS_BASE_URL}/engines/stock/markets/{market_path}/"
        f"securities/{spec.ticker}/candles.json"
    )


def _load_page(spec: RequestSpec, start: int) -> list[dict[str, Any]]:
    params = {
        "from": spec.day,
        "till": spec.day,
        "interval": 1,
        "start": start,
        "iss.meta": "off",
        "candles.columns": "begin,open,close,high,low,value,volume",
    }
    url = f"{_moex_url(spec)}?{urlencode(params)}"
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "EventEdge-stage2-research/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                payload = json.load(response)
            table = payload.get("candles", {})
            columns = table.get("columns", [])
            return [dict(zip(columns, values, strict=False)) for values in table.get("data", [])]
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if isinstance(error, HTTPError) and error.code not in {429, 500, 502, 503, 504}:
                break
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"MOEX ISS failed for {spec.key} start={start}: {last_error}")


def _moex_timestamp(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_localize(MOSCOW).tz_convert("UTC")


def _fetch_spec(spec: RequestSpec) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        page_rows = _load_page(spec, page * PAGE_SIZE)
        rows.extend(page_rows)
        if len(page_rows) < PAGE_SIZE:
            break
        last_begin = _moex_timestamp(str(page_rows[-1]["begin"]))
        if last_begin >= spec.needed_until:
            break
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if any(row.get(field) is None for field in ("begin", "open", "close", "high", "low")):
            continue
        begin_at = _moex_timestamp(str(row["begin"]))
        if begin_at < spec.retain_from or begin_at > spec.needed_until:
            continue
        parsed.append(
            {
                "request_key": spec.key,
                "market": spec.market,
                "ticker": spec.ticker,
                "day": spec.day,
                "begin_at": begin_at,
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "value_rub": float(row["value"]) if row.get("value") is not None else np.nan,
                "volume_shares": (
                    float(row["volume"]) if row.get("volume") is not None else np.nan
                ),
            }
        )
    return spec.key, parsed


def load_or_download_candles(
    frame: pd.DataFrame,
    *,
    dataset_sha256: str,
    cache_path: Path,
    metadata_path: Path,
    workers: int,
    offline: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    specs = _request_specs(frame)
    signature = _spec_signature(specs)
    if cache_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        valid = (
            metadata.get("cache_version") == CACHE_VERSION
            and metadata.get("dataset_sha256") == dataset_sha256
            and metadata.get("request_spec_sha256") == signature
            and metadata.get("request_specs") == len(specs)
        )
        if valid:
            candles = pd.read_csv(cache_path, parse_dates=["begin_at"])
            if metadata.get("rows") != len(candles):
                raise AssertionError("cached candle row count mismatch")
            if metadata.get("data_sha256") != sha256_file(cache_path):
                raise AssertionError("cached candle hash mismatch")
            return candles, metadata
    if offline:
        raise FileNotFoundError("valid candle cache is unavailable in offline mode")

    results: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_spec = {pool.submit(_fetch_spec, spec): spec for spec in specs}
        for completed, future in enumerate(as_completed(future_to_spec), 1):
            spec = future_to_spec[future]
            try:
                key, rows = future.result()
                results[key] = rows
            except RuntimeError as error:
                failures.append({"key": spec.key, "error": str(error)})
                results[spec.key] = []
            if completed % 100 == 0 or completed == len(specs):
                print(f"MOEX cache: {completed}/{len(specs)} requests completed", flush=True)

    flat_rows = [row for key in sorted(results) for row in results[key]]
    candles = pd.DataFrame(flat_rows)
    if candles.empty:
        raise RuntimeError("MOEX ISS returned no usable minute candles")
    candles = candles.sort_values(["request_key", "begin_at"]).reset_index(drop=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    candles.to_csv(
        cache_path,
        index=False,
        float_format="%.10g",
        compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
    )
    metadata = {
        "cache_version": CACHE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": dataset_sha256,
        "request_spec_sha256": signature,
        "request_specs": len(specs),
        "successful_specs": sum(bool(results[spec.key]) for spec in specs),
        "empty_or_failed_specs": sum(not results[spec.key] for spec in specs),
        "failures": failures,
        "rows": len(candles),
        "data_sha256": sha256_file(cache_path),
        "source": "MOEX ISS",
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return candles, metadata


def _completed(candles: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    if candles.empty:
        return candles
    return candles[candles["begin_at"] + pd.Timedelta(minutes=1) <= cutoff]


def _last_price(
    candles: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> tuple[float | None, float | None, pd.Timestamp | None]:
    complete = _completed(candles, cutoff)
    if complete.empty:
        return None, None, None
    row = complete.iloc[-1]
    observed_at = row["begin_at"] + pd.Timedelta(minutes=1)
    age = (cutoff - observed_at).total_seconds() / 60
    if age < 0:
        raise AssertionError("a candle ending after cutoff was consumed")
    return float(row["close"]), float(age), observed_at


def _return_at(candles: pd.DataFrame, cutoff: pd.Timestamp, minutes: int) -> float | None:
    current, age, _ = _last_price(candles, cutoff)
    if current is None or age is None or age > FRESH_QUOTE_MINUTES:
        return None
    prior, _, _ = _last_price(candles, cutoff - pd.Timedelta(minutes=minutes))
    if prior is None or prior <= 0:
        return None
    return (current / prior - 1) * 100


def _window_stats(
    candles: pd.DataFrame,
    cutoff: pd.Timestamp,
    minutes: int,
) -> tuple[float | None, float | None]:
    complete = _completed(candles, cutoff)
    window = complete[
        complete["begin_at"] + pd.Timedelta(minutes=1) > cutoff - pd.Timedelta(minutes=minutes)
    ]
    if len(window) < 5:
        return None, None
    closes = window["close"].to_numpy(dtype=float)
    log_returns = np.diff(np.log(closes))
    volatility = float(np.std(log_returns, ddof=1) * 100) if len(log_returns) >= 2 else None
    low = float(window["low"].min())
    high = float(window["high"].max())
    range_pct = (high / low - 1) * 100 if low > 0 else None
    return volatility, range_pct


def _reaction_stats(
    candles: pd.DataFrame,
    published_at: pd.Timestamp,
    decision_at: pd.Timestamp,
) -> dict[str, float | None]:
    before_price, before_age, before_observed = _last_price(candles, published_at)
    decision_price, decision_age, decision_observed = _last_price(candles, decision_at)
    fresh = (
        before_price is not None
        and decision_price is not None
        and before_age is not None
        and decision_age is not None
        and before_age <= FRESH_QUOTE_MINUTES
        and decision_age <= FRESH_QUOTE_MINUTES
    )
    reaction = (
        (decision_price / before_price - 1) * 100
        if fresh and before_price and decision_price
        else None
    )
    start = published_at.ceil("min")
    complete = _completed(candles, decision_at)
    reaction_rows = complete[complete["begin_at"] >= start]
    before_rows = _completed(candles, published_at)
    before_rows = before_rows[
        before_rows["begin_at"] + pd.Timedelta(minutes=1) > published_at - pd.Timedelta(minutes=30)
    ]

    def ratio(column: str) -> float | None:
        after = reaction_rows[column].dropna()
        before = before_rows[column].dropna()
        if after.empty or before.empty or float(before.mean()) <= 0:
            return None
        return float(after.mean() / before.mean())

    range_pct = None
    if not reaction_rows.empty:
        low = float(reaction_rows["low"].min())
        high = float(reaction_rows["high"].max())
        range_pct = (high / low - 1) * 100 if low > 0 else None
    consumed = [value for value in (before_observed, decision_observed) if value is not None]
    if consumed and max(consumed) > decision_at:
        raise AssertionError("reaction used a price unavailable at decision_at")
    return {
        "reaction_pct": reaction,
        "range_pct": range_pct,
        "volume_ratio": ratio("volume_shares"),
        "value_ratio": ratio("value_rub"),
        "candle_count": float(len(reaction_rows)),
        "publication_quote_age_minutes": before_age,
        "decision_quote_age_minutes": decision_age,
    }


def _safe_difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or pd.isna(left) or pd.isna(right):
        return None
    return float(left - right)


def _market_features_for_row(row: Any, groups: dict[str, pd.DataFrame]) -> dict[str, Any]:
    day = row.published_at.tz_convert(MOSCOW).date().isoformat()
    empty = next(iter(groups.values())).iloc[0:0]
    stock = groups.get(f"shares:{row.ticker}:{day}", empty)
    benchmark = groups.get(f"index:IMOEX2:{day}", empty)
    values: dict[str, Any] = {"id": row.id}
    pre_returns: dict[str, dict[int, float | None]] = {}
    decision_returns: dict[str, dict[int, float | None]] = {}
    for label, candles in (("stock", stock), ("benchmark", benchmark)):
        pre_returns[label] = {
            horizon: _return_at(candles, row.published_at, horizon) for horizon in (5, 15, 60)
        }
        decision_returns[label] = {
            horizon: _return_at(candles, row.decision_at, horizon) for horizon in (5, 15, 60)
        }
        for horizon in (5, 15, 60):
            values[f"{label}_pre_return_{horizon}m_pct"] = pre_returns[label][horizon]
            values[f"{label}_decision_return_{horizon}m_pct"] = decision_returns[label][horizon]
        pre_volatility, pre_range = _window_stats(candles, row.published_at, 30)
        decision_volatility, decision_range = _window_stats(candles, row.decision_at, 30)
        values[f"{label}_pre_volatility_30m_pct"] = pre_volatility
        values[f"{label}_pre_range_30m_pct"] = pre_range
        values[f"{label}_decision_volatility_30m_pct"] = decision_volatility
        values[f"{label}_decision_range_30m_pct"] = decision_range

    for horizon in (5, 15, 60):
        abnormal_pre = _safe_difference(
            pre_returns["stock"][horizon], pre_returns["benchmark"][horizon]
        )
        abnormal_decision = _safe_difference(
            decision_returns["stock"][horizon], decision_returns["benchmark"][horizon]
        )
        values[f"abnormal_pre_return_{horizon}m_pct"] = abnormal_pre
        values[f"abs_abnormal_pre_return_{horizon}m_pct"] = (
            abs(abnormal_pre) if abnormal_pre is not None else None
        )
        values[f"abnormal_decision_return_{horizon}m_pct"] = abnormal_decision
        values[f"abs_abnormal_decision_return_{horizon}m_pct"] = (
            abs(abnormal_decision) if abnormal_decision is not None else None
        )

    stock_reaction = _reaction_stats(stock, row.published_at, row.decision_at)
    benchmark_reaction = _reaction_stats(benchmark, row.published_at, row.decision_at)
    for label, reaction in (("stock", stock_reaction), ("benchmark", benchmark_reaction)):
        values[f"{label}_reaction_0_5m_pct"] = reaction["reaction_pct"]
        values[f"abs_{label}_reaction_0_5m_pct"] = (
            abs(reaction["reaction_pct"]) if reaction["reaction_pct"] is not None else None
        )
        values[f"{label}_reaction_range_pct"] = reaction["range_pct"]
        values[f"{label}_reaction_candle_count"] = reaction["candle_count"]
        values[f"{label}_publication_quote_age_minutes"] = reaction["publication_quote_age_minutes"]
        values[f"{label}_decision_quote_age_minutes"] = reaction["decision_quote_age_minutes"]
    values["stock_reaction_volume_ratio"] = stock_reaction["volume_ratio"]
    values["stock_reaction_value_ratio"] = stock_reaction["value_ratio"]
    abnormal_reaction = _safe_difference(
        stock_reaction["reaction_pct"], benchmark_reaction["reaction_pct"]
    )
    values["abnormal_reaction_0_5m_pct"] = abnormal_reaction
    values["abs_abnormal_reaction_0_5m_pct"] = (
        abs(abnormal_reaction) if abnormal_reaction is not None else None
    )
    values["market_data_available"] = float(abnormal_reaction is not None)
    local_decision = row.decision_at.tz_convert(MOSCOW)
    minute = local_decision.hour * 60 + local_decision.minute
    angle = 2 * math.pi * minute / (24 * 60)
    values["decision_time_sin"] = math.sin(angle)
    values["decision_time_cos"] = math.cos(angle)
    return values


def build_market_features(frame: pd.DataFrame, candles: pd.DataFrame) -> pd.DataFrame:
    groups = {
        key: group.sort_values("begin_at").reset_index(drop=True)
        for key, group in candles.groupby("request_key", sort=False)
    }
    rows = [_market_features_for_row(row, groups) for row in frame.itertuples(index=False)]
    features = pd.DataFrame(rows)
    if len(features) != len(frame) or features["id"].duplicated().any():
        raise AssertionError("market features do not map one-to-one to dataset rows")
    return features


def make_pipeline(feature_set: str, *, c: float, class_weight: str | None) -> Pipeline:
    return Pipeline(
        [
            (
                "features",
                ColumnTransformer(
                    [
                        (
                            "numeric",
                            Pipeline(
                                [
                                    (
                                        "imputer",
                                        SimpleImputer(
                                            strategy="median",
                                            add_indicator=True,
                                            keep_empty_features=True,
                                        ),
                                    ),
                                    ("scale", StandardScaler()),
                                ]
                            ),
                            list(FEATURE_SETS[feature_set]),
                        ),
                        (
                            "categorical",
                            Pipeline(
                                [
                                    ("imputer", SimpleImputer(strategy="most_frequent")),
                                    (
                                        "one_hot",
                                        OneHotEncoder(handle_unknown="ignore", min_frequency=2),
                                    ),
                                ]
                            ),
                            list(CATEGORICAL_BASE_FEATURES),
                        ),
                    ]
                ),
            ),
            (
                "model",
                LogisticRegression(
                    C=c,
                    class_weight=class_weight,
                    max_iter=5_000,
                    random_state=SEED,
                ),
            ),
        ]
    )


def _best_threshold(y_true: np.ndarray, probability: np.ndarray) -> float:
    best_score = (float("-inf"), float("-inf"), float("-inf"))
    best_threshold = 0.5
    for threshold in np.unique(np.concatenate(([0.05, 0.95], probability))):
        predicted = (probability >= threshold).astype(int)
        score = (
            balanced_accuracy_score(y_true, predicted),
            accuracy_score(y_true, predicted),
            -abs(float(threshold) - 0.5),
        )
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _fit_selected(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    target: str,
    feature_set: str,
    selection_metric: str,
) -> tuple[Pipeline, dict[str, Any], float]:
    best: tuple[float, float, str | None, Pipeline, np.ndarray] | None = None
    for c in C_GRID:
        for class_weight in CLASS_WEIGHT_GRID:
            random.seed(SEED)
            np.random.seed(SEED)
            model = make_pipeline(feature_set, c=c, class_weight=class_weight)
            model.fit(train, train[target])
            probability = model.predict_proba(validation)[:, 1]
            score = (
                average_precision_score(validation[target], probability)
                if selection_metric == "pr_auc"
                else roc_auc_score(validation[target], probability)
            )
            candidate = (float(score), -c, class_weight, model, probability)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        raise AssertionError("no model candidate was fitted")
    _, negative_c, class_weight, model, validation_probability = best
    threshold = _best_threshold(validation[target].to_numpy(), validation_probability)
    return model, {"C": -negative_c, "class_weight": class_weight}, threshold


def _rows(frame: pd.DataFrame, ids: tuple[str, ...]) -> pd.DataFrame:
    return frame.set_index("id", drop=False).loc[list(ids)].reset_index(drop=True)


def run_models(
    frame: pd.DataFrame,
    folds: list[Any],
    *,
    target: str,
    selection_metric: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    predictions: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        train = _rows(frame, fold.train_ids)
        validation = _rows(frame, fold.validation_ids)
        test = _rows(frame, fold.test_ids)
        output = test[
            [
                "id",
                "event_group_id",
                "published_at",
                "materiality_label",
                "abnormal_direction_label",
                "raw_direction_label",
                "rule_direction",
                "market_data_available",
                "abnormal_reaction_0_5m_pct",
                "abs_abnormal_reaction_0_5m_pct",
            ]
        ].copy()
        output["fold"] = fold.name
        for feature_set in FEATURE_SETS:
            model, selected, threshold = _fit_selected(
                train,
                validation,
                target=target,
                feature_set=feature_set,
                selection_metric=selection_metric,
            )
            probability = model.predict_proba(test)[:, 1]
            output[f"probability_{feature_set}"] = probability
            output[f"threshold_{feature_set}"] = threshold
            output[f"prediction_{feature_set}"] = (probability >= threshold).astype(int)
            metrics = binary_metrics(test[target].to_numpy(), probability, threshold=threshold)
            fold_rows.append(
                {
                    "task": target,
                    "fold": fold.name,
                    "feature_set": feature_set,
                    "train_n": len(train),
                    "validation_n": len(validation),
                    "test_n": len(test),
                    "selected_C": selected["C"],
                    "selected_class_weight": selected["class_weight"],
                    **{key: value for key, value in metrics.items() if key != "confusion_matrix"},
                }
            )
        predictions.append(output)
    result = pd.concat(predictions, ignore_index=True)
    if len(result) != 480 or result["id"].nunique() != 480:
        raise AssertionError(f"expected 480 unique predictions for {target}")
    return result, pd.DataFrame(fold_rows)


def _aggregate_metrics(
    predictions: pd.DataFrame,
    *,
    target: str,
) -> dict[str, dict[str, Any]]:
    y = predictions[target].to_numpy()
    result: dict[str, dict[str, Any]] = {}
    for feature_set in FEATURE_SETS:
        probability = predictions[f"probability_{feature_set}"].to_numpy()
        predicted = predictions[f"prediction_{feature_set}"].to_numpy()
        metrics = binary_metrics(y, probability)
        metrics.update(
            {
                "threshold": "selected_per_fold",
                "accuracy": float(accuracy_score(y, predicted)),
                "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
                "precision": float(precision_score(y, predicted, zero_division=0)),
                "recall": float(recall_score(y, predicted, zero_division=0)),
                "f1": float(f1_score(y, predicted, zero_division=0)),
                "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
            }
        )
        result[feature_set] = metrics
    return result


def _metric_score(y: pd.Series, probability: pd.Series, metric: str) -> float:
    if metric == "pr_auc":
        return float(average_precision_score(y, probability))
    if metric == "roc_auc":
        return float(roc_auc_score(y, probability))
    raise ValueError(metric)


def _cluster_bootstrap(
    predictions: pd.DataFrame,
    *,
    target: str,
    baseline: str,
    challenger: str,
    metric: str,
    iterations: int = 2_000,
) -> dict[str, float]:
    groups = {key: value for key, value in predictions.groupby("event_group_id")}
    keys = np.array(list(groups), dtype=object)
    rng = np.random.default_rng(SEED)
    deltas: list[float] = []
    for _ in range(iterations):
        sampled = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([groups[key] for key in sampled], ignore_index=True)
        if sample[target].nunique() != 2:
            continue
        deltas.append(
            _metric_score(sample[target], sample[f"probability_{challenger}"], metric)
            - _metric_score(sample[target], sample[f"probability_{baseline}"], metric)
        )
    low, high = np.percentile(deltas, [2.5, 97.5])
    estimate = _metric_score(
        predictions[target], predictions[f"probability_{challenger}"], metric
    ) - _metric_score(predictions[target], predictions[f"probability_{baseline}"], metric)
    return {
        "estimate": float(estimate),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(deltas),
    }


def _fold_deltas(
    predictions: pd.DataFrame,
    *,
    target: str,
    challenger: str,
    metric: str,
) -> list[dict[str, Any]]:
    rows = []
    for fold, group in predictions.groupby("fold", sort=True):
        base = _metric_score(group[target], group["probability_legacy"], metric)
        challenge = _metric_score(group[target], group[f"probability_{challenger}"], metric)
        rows.append(
            {
                "fold": fold,
                "baseline": base,
                "challenger": challenge,
                "delta": challenge - base,
            }
        )
    return rows


def _config6_comparison(direction_predictions: pd.DataFrame) -> dict[str, Any]:
    paired = direction_predictions[
        direction_predictions["rule_direction"].isin(["up", "down"])
    ].copy()
    if len(paired) != 157:
        raise AssertionError(f"expected 157 config-6 rows, found {len(paired)}")
    y = paired["abnormal_direction_label"].to_numpy()
    rule = (paired["rule_direction"] == "up").astype(int).to_numpy()
    paired["prediction_config_6"] = rule
    result: dict[str, Any] = {
        "n": len(paired),
        "config_6_hit_rate": float(accuracy_score(y, rule)),
        "config_6_balanced_accuracy": float(balanced_accuracy_score(y, rule)),
        "models": {},
    }
    for feature_set in FEATURE_SETS:
        predicted = paired[f"prediction_{feature_set}"].to_numpy()
        result["models"][feature_set] = {
            "hit_rate": float(accuracy_score(y, predicted)),
            "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
            "hit_rate_delta_vs_config_6": float(
                accuracy_score(y, predicted) - accuracy_score(y, rule)
            ),
            "hit_rate_delta_bootstrap": _cluster_bootstrap_hit_rate(
                paired,
                prediction_a="prediction_config_6",
                prediction_b=f"prediction_{feature_set}",
            ),
        }
    return result


def _cluster_bootstrap_hit_rate(
    predictions: pd.DataFrame,
    *,
    prediction_a: str,
    prediction_b: str,
    iterations: int = 2_000,
) -> dict[str, float]:
    groups = {key: value for key, value in predictions.groupby("event_group_id")}
    keys = np.array(list(groups), dtype=object)
    rng = np.random.default_rng(SEED)
    deltas = []
    for _ in range(iterations):
        sampled = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([groups[key] for key in sampled], ignore_index=True)
        y = sample["abnormal_direction_label"]
        deltas.append(
            accuracy_score(y, sample[prediction_b]) - accuracy_score(y, sample[prediction_a])
        )
    low, high = np.percentile(deltas, [2.5, 97.5])
    y = predictions["abnormal_direction_label"]
    return {
        "estimate": float(
            accuracy_score(y, predictions[prediction_b])
            - accuracy_score(y, predictions[prediction_a])
        ),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(deltas),
    }


def _covered_materiality_summary(predictions: pd.DataFrame) -> dict[str, Any]:
    covered = predictions[predictions["market_data_available"] == 1].copy()
    summary: dict[str, Any] = {
        "n": len(covered),
        "coverage": float(len(covered) / len(predictions)),
        "models": _aggregate_metrics(covered, target="materiality_label"),
    }
    y = covered["materiality_label"]
    direct = covered["abs_abnormal_reaction_0_5m_pct"]
    summary["direct_abs_abnormal_reaction"] = {
        "pr_auc": float(average_precision_score(y, direct)),
        "roc_auc": float(roc_auc_score(y, direct)),
    }
    return summary


def save_plot(predictions: pd.DataFrame, output_dir: Path) -> None:
    from sklearn.metrics import PrecisionRecallDisplay, RocCurveDisplay

    y = predictions["materiality_label"]
    figure, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for feature_set in FEATURE_SETS:
        probability = predictions[f"probability_{feature_set}"]
        RocCurveDisplay.from_predictions(y, probability, name=feature_set, ax=axes[0])
        PrecisionRecallDisplay.from_predictions(y, probability, name=feature_set, ax=axes[1])
    axes[0].set_title("Materiality ROC")
    axes[1].set_title("Materiality precision-recall")
    figure.tight_layout()
    figure.savefig(output_dir / "materiality_feature_sets.png", dpi=160)
    plt.close(figure)


def run_experiment(
    dataset_path: Path,
    output_dir: Path,
    *,
    workers: int = 12,
    offline: bool = False,
) -> dict[str, Any]:
    assert_feature_contract()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = stage_artifact_directory("market_stage2") / "cache"
    dataset_sha256 = sha256_file(dataset_path)
    frame = load_dataset(dataset_path)
    candles, cache_metadata = load_or_download_candles(
        frame,
        dataset_sha256=dataset_sha256,
        cache_path=cache_dir / "minute_candles.csv.gz",
        metadata_path=cache_dir / "minute_candles.metadata.json",
        workers=workers,
        offline=offline,
    )
    market_features = build_market_features(frame, candles)
    market_features.to_csv(output_dir / "market_features.csv", index=False, float_format="%.10g")
    frame = frame.merge(market_features, on="id", validate="one_to_one")
    frame = add_sentiment(
        frame,
        cache_path=stage_artifact_directory("finbert_stage1") / "sentiment_predictions.csv",
        dataset_sha256=dataset_sha256,
    )
    folds = build_folds(frame)
    material_predictions, material_folds = run_models(
        frame, folds, target="materiality_label", selection_metric="pr_auc"
    )
    direction_predictions, direction_folds = run_models(
        frame, folds, target="abnormal_direction_label", selection_metric="roc_auc"
    )
    material_predictions.to_csv(
        output_dir / "materiality_predictions.csv", index=False, float_format="%.10g"
    )
    direction_predictions.to_csv(
        output_dir / "direction_predictions.csv", index=False, float_format="%.10g"
    )
    pd.concat([material_folds, direction_folds], ignore_index=True).to_csv(
        output_dir / "fold_metrics.csv", index=False, float_format="%.10g"
    )
    save_plot(material_predictions, output_dir)

    material_metrics = _aggregate_metrics(material_predictions, target="materiality_label")
    direction_metrics = _aggregate_metrics(direction_predictions, target="abnormal_direction_label")
    comparisons: dict[str, Any] = {}
    for challenger in FEATURE_SETS:
        if challenger == "legacy":
            continue
        comparisons[challenger] = {
            metric: _cluster_bootstrap(
                material_predictions,
                target="materiality_label",
                baseline="legacy",
                challenger=challenger,
                metric=metric,
            )
            for metric in ("pr_auc", "roc_auc")
        }
        comparisons[challenger]["pr_auc_by_fold"] = _fold_deltas(
            material_predictions,
            target="materiality_label",
            challenger=challenger,
            metric="pr_auc",
        )

    feature_coverage = {
        feature: {
            "available_n": int(frame[feature].notna().sum()),
            "available_rate": float(frame[feature].notna().mean()),
        }
        for feature in PREPUBLICATION_FEATURES + REACTION_PRICE_FEATURES + MICROSTRUCTURE_FEATURES
    }
    result: dict[str, Any] = {
        "experiment": EXPERIMENT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "seed": SEED,
        "dataset": {"path": str(dataset_path), "sha256": dataset_sha256, "rows": len(frame)},
        "market_cache": cache_metadata,
        "protocol": {
            "test_months": list(TEST_MONTHS),
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "candle_interval_minutes": 1,
            "candle_availability_rule": "begin_at + 1 minute <= feature cutoff",
            "publication_cutoff": "published_at",
            "decision_cutoff": "published_at + 5 minutes",
            "fresh_quote_max_age_minutes": FRESH_QUOTE_MINUTES,
            "nested_feature_sets": {key: list(value) for key, value in FEATURE_SETS.items()},
        },
        "folds": [
            {
                "name": fold.name,
                "train_n": len(fold.train_ids),
                "validation_n": len(fold.validation_ids),
                "test_n": len(fold.test_ids),
            }
            for fold in folds
        ],
        "feature_coverage": feature_coverage,
        "materiality": material_metrics,
        "materiality_market_available_population": _covered_materiality_summary(
            material_predictions
        ),
        "materiality_paired_delta_vs_legacy": comparisons,
        "finbert_delta_vs_reaction_core": {
            metric: _cluster_bootstrap(
                material_predictions,
                target="materiality_label",
                baseline="reaction_core",
                challenger="reaction_core_finbert",
                metric=metric,
            )
            for metric in ("pr_auc", "roc_auc")
        },
        "abnormal_direction": direction_metrics,
        "abnormal_direction_config6_population": _config6_comparison(direction_predictions),
        "artifact_hashes": {},
    }
    for filename in (
        "market_features.csv",
        "materiality_predictions.csv",
        "direction_predictions.csv",
        "fold_metrics.csv",
        "materiality_feature_sets.png",
    ):
        result["artifact_hashes"][filename] = sha256_file(output_dir / filename)
    (output_dir / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EventEdge stage-2 market experiment")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=dataset_path_from_environment(),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=stage_artifact_directory("market_stage2"),
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    if not 1 <= arguments.workers <= 24:
        parser.error("--workers must be between 1 and 24")
    result = run_experiment(
        arguments.dataset,
        arguments.output_dir,
        workers=arguments.workers,
        offline=arguments.offline,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
