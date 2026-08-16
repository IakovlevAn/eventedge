from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eventedge.collectors import RssItem, is_signal_analysis_candidate
from eventedge.quality import QualityGateThresholds, check_quality_gate


class SignalContractCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1)
    content: str
    expected_candidate: bool
    reason: str = Field(min_length=1)


def load_contract(path: Path) -> list[SignalContractCase]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list) or not document:
        raise ValueError("signal quality contract must be a non-empty JSON array")
    return [SignalContractCase.model_validate(case) for case in document]


def evaluate_contract(cases: list[SignalContractCase]) -> dict[str, object]:
    predictions = []
    true_positive = false_positive = false_negative = true_negative = 0
    for index, case in enumerate(cases, 1):
        item = RssItem(
            external_id=f"signal-contract-{index}",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            title=case.title,
            url=f"quality-contract://signal/{index}",
            content=case.content,
            categories=(),
        )
        predicted = is_signal_analysis_candidate(item)
        if predicted and case.expected_candidate:
            true_positive += 1
        elif predicted:
            false_positive += 1
        elif case.expected_candidate:
            false_negative += 1
        else:
            true_negative += 1
        predictions.append(
            {
                "id": item.external_id,
                "expected_candidate": case.expected_candidate,
                "predicted_candidate": predicted,
                "reason": case.reason,
            }
        )

    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative
    return {
        "schema_version": "quality-report-1.0",
        "router": "signal",
        "dataset_kind": "synthetic_contract",
        "observations": len(cases),
        "relevance": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "precision": (
                round(true_positive / predicted_positive, 4) if predicted_positive else None
            ),
            "recall": round(true_positive / actual_positive, 4) if actual_positive else None,
        },
        "predictions": predictions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enforce the synthetic deterministic signal-router contract"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--minimum-observations", type=int, default=32)
    parser.add_argument("--minimum-precision", type=float, default=1.0)
    parser.add_argument("--minimum-recall", type=float, default=1.0)
    args = parser.parse_args()

    thresholds = QualityGateThresholds(
        minimum_observations=args.minimum_observations,
        minimum_precision=args.minimum_precision,
        minimum_recall=args.minimum_recall,
    )
    report = evaluate_contract(load_contract(args.dataset))
    report["gate"] = check_quality_gate(report, thresholds)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["gate"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
