"""Paired hit-rate uncertainty on fixed selections, without model or policy fitting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence


def paired_hit_diagnostics(
    candidate_hits: Sequence[bool],
    baseline_hits: Sequence[bool],
    cluster_ids: Sequence[str],
    *,
    repetitions: int = 5000,
    seed: int = 20260907,
) -> dict[str, object]:
    """Resample entire clusters and preserve row weighting within each bootstrap draw.

    Inputs must describe the same fixed directional rows in the same order.
    Intervals are conditional development diagnostics: they do not correct for
    model selection, dependence between clusters, or a small number of dates.
    A cluster must not be split into individual event rows during resampling.
    """
    _validate_inputs(candidate_hits, baseline_hits, cluster_ids, repetitions, seed)
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for candidate, baseline, cluster in zip(
        candidate_hits, baseline_hits, cluster_ids, strict=True
    ):
        totals = groups[cluster]
        totals[0] += 1
        totals[1] += candidate
        totals[2] += baseline
    rows = len(candidate_hits)
    candidate_only = sum(
        candidate and not baseline
        for candidate, baseline in zip(candidate_hits, baseline_hits, strict=True)
    )
    baseline_only = sum(
        baseline and not candidate
        for candidate, baseline in zip(candidate_hits, baseline_hits, strict=True)
    )
    result = {
        "rows": rows,
        "candidate_hits": sum(candidate_hits),
        "baseline_hits": sum(baseline_hits),
        "candidate_hit_rate_pct": sum(candidate_hits) / rows * 100,
        "baseline_hit_rate_pct": sum(baseline_hits) / rows * 100,
        "difference_percentage_points": (candidate_only - baseline_only) / rows * 100,
        "candidate_only_correct": candidate_only,
        "baseline_only_correct": baseline_only,
        "both_correct": sum(candidate_hits) - candidate_only,
        "both_wrong": rows - sum(candidate_hits) - baseline_only,
        "clusters": len(groups),
        "minimum_cluster_rows": min(value[0] for value in groups.values()),
        "maximum_cluster_rows": max(value[0] for value in groups.values()),
        "bootstrap_seed": seed,
        "bootstrap_repetitions": repetitions,
        "bootstrap_method": "paired_cluster_percentile_row_weighted",
        "cluster_counts_below_20": len(groups) < 20,
        "conditional_on_fixed_selection_not_multiplicity_adjusted": True,
        "by_cluster": {
            key: {"rows": value[0], "candidate_hits": value[1], "baseline_hits": value[2]}
            for key, value in sorted(groups.items())
        },
    }
    if len(groups) < 2:
        return {**result, "bootstrap_95": None, "reason": "fewer_than_two_clusters"}
    return {
        **result,
        "bootstrap_95": _bootstrap_intervals(groups, repetitions, seed),
        "leave_one_cluster_out": _leave_one_out(groups),
    }


def paired_auc_diagnostics(
    labels: Sequence[bool],
    candidate_probabilities: Sequence[float],
    baseline_probabilities: Sequence[float],
    cluster_ids: Sequence[str],
    *,
    repetitions: int = 5000,
    seed: int = 20260909,
) -> dict[str, object]:
    """Estimate a paired AUC difference with whole-cluster resampling.

    This is conditional uncertainty for already frozen predictions. It does not
    account for model selection, historical-window reuse, or market regime drift.
    """
    import math

    import numpy as np
    from sklearn.metrics import roc_auc_score

    if not labels or not len(labels) == len(candidate_probabilities) == len(
        baseline_probabilities
    ) == len(cluster_ids):
        raise ValueError("paired AUC bootstrap needs nonempty aligned rows")
    if (
        len(labels) > 20_000
        or any(not isinstance(value, bool) for value in labels)
        or any(
            not math.isfinite(value) or not 0 <= value <= 1
            for probabilities in (candidate_probabilities, baseline_probabilities)
            for value in probabilities
        )
        or any(not isinstance(value, str) or not 1 <= len(value) <= 300 for value in cluster_ids)
        or not isinstance(repetitions, int)
        or not 100 <= repetitions <= 10_000
        or not isinstance(seed, int)
        or not 0 <= seed <= 2**32 - 1
    ):
        raise ValueError("paired AUC bootstrap inputs exceed bounded finite limits")
    groups: dict[str, list[int]] = defaultdict(list)
    for index, cluster in enumerate(cluster_ids):
        groups[cluster].append(index)
    if len(groups) < 2 or len(set(labels)) < 2:
        return {
            "rows": len(labels),
            "clusters": len(groups),
            "auc_difference": None,
            "bootstrap_95": None,
            "reason": "fewer_than_two_clusters_or_classes",
        }
    keys = sorted(groups)
    rng = np.random.default_rng(seed)
    estimates = []
    skipped = 0
    for _ in range(repetitions):
        sampled = rng.integers(0, len(keys), size=len(keys))
        indices = [index for position in sampled for index in groups[keys[position]]]
        sampled_labels = [labels[index] for index in indices]
        if len(set(sampled_labels)) < 2:
            skipped += 1
            continue
        estimates.append(
            float(
                roc_auc_score(sampled_labels, [candidate_probabilities[index] for index in indices])
                - roc_auc_score(
                    sampled_labels, [baseline_probabilities[index] for index in indices]
                )
            )
        )
    if not estimates:
        raise ValueError("all paired AUC bootstrap replicates contained one class")
    return {
        "rows": len(labels),
        "clusters": len(groups),
        "auc_difference": float(
            roc_auc_score(labels, candidate_probabilities)
            - roc_auc_score(labels, baseline_probabilities)
        ),
        "bootstrap_95": np.quantile(estimates, [0.025, 0.975]).tolist(),
        "valid_replicates": len(estimates),
        "single_class_replicates_skipped": skipped,
        "seed": seed,
        "method": "paired_day_cluster_percentile_row_weighted",
        "conditional_on_frozen_models_not_multiple_trial_corrected": True,
    }


def _bootstrap_intervals(groups: dict[str, list[int]], repetitions: int, seed: int) -> dict:
    import numpy as np

    counts = np.asarray([groups[key] for key in sorted(groups)], dtype=np.int64)
    rng = np.random.default_rng(seed)
    results = np.empty((repetitions, 3))
    # At most 64 x 2,000 cluster indices are resident in each bounded batch.
    for start in range(0, repetitions, 64):
        stop = min(repetitions, start + 64)
        indices = rng.integers(0, len(counts), size=(stop - start, len(counts)))
        totals = counts[indices].sum(axis=1)
        results[start:stop, :2] = totals[:, 1:] / totals[:, :1] * 100
        results[start:stop, 2] = results[start:stop, 0] - results[start:stop, 1]
    intervals = np.quantile(results, [0.025, 0.975], axis=0)
    return {
        "candidate_hit_rate_pct": intervals[:, 0].tolist(),
        "baseline_hit_rate_pct": intervals[:, 1].tolist(),
        "difference_percentage_points": intervals[:, 2].tolist(),
    }


def _leave_one_out(groups: dict[str, list[int]]) -> dict[str, object]:
    rows = sum(value[0] for value in groups.values())
    candidate = sum(value[1] for value in groups.values())
    baseline = sum(value[2] for value in groups.values())
    rates, differences = [], []
    for count, candidate_hits, baseline_hits in groups.values():
        denominator = rows - count
        rates.append((candidate - candidate_hits) / denominator * 100)
        differences.append(
            (candidate - candidate_hits - baseline + baseline_hits) / denominator * 100
        )
    return {
        "candidate_hit_rate_pct_range": [min(rates), max(rates)],
        "difference_percentage_points_range": [min(differences), max(differences)],
        "removed_clusters_with_nonpositive_remaining_difference": sum(
            value <= 0 for value in differences
        ),
    }


def _validate_inputs(candidate, baseline, clusters, repetitions, seed) -> None:
    if not 1 <= len(candidate) <= 20_000 or len(candidate) != len(baseline):
        raise ValueError("paired diagnostics require 1..20,000 aligned hit pairs")
    if len(clusters) != len(candidate) or any(
        type(value) is not bool for value in (*candidate, *baseline)
    ):
        raise ValueError("paired hits must be boolean and cluster-aligned")
    if any(not isinstance(value, str) or not 1 <= len(value) <= 300 for value in clusters):
        raise ValueError("cluster ids must be nonempty bounded strings")
    if len(set(clusters)) > 2000:
        raise ValueError("paired bootstrap supports at most 2,000 clusters")
    if type(repetitions) is not int or not 100 <= repetitions <= 10_000:
        raise ValueError("bootstrap repetitions must be an integer in 100..10,000")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("bootstrap seed must be a bounded nonnegative integer")
