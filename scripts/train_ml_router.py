from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from eventedge.collectors import RssItem, is_signal_analysis_candidate
from eventedge.ml_router import (
    MlRouterTrainingRow,
    artifact_summary,
    build_ml_router_artifact,
    write_artifact,
)
from eventedge.quality import QualityExample, load_quality_dataset, temporal_group_split


def _training_rows(examples: tuple[QualityExample, ...]) -> list[MlRouterTrainingRow]:
    rows = []
    for example in examples:
        item = RssItem(
            external_id=example.id,
            published_at=example.published_at,
            title=example.title,
            url=f"quality://{example.id}",
            content=example.content,
            categories=example.categories,
        )
        # This model runs after the deterministic router. Training on examples
        # that never reach that point would optimize a different population.
        if not is_signal_analysis_candidate(item):
            continue
        rows.append(
            MlRouterTrainingRow(
                id=example.id,
                event_id=example.event_id,
                title=example.title,
                content=example.content,
                categories=example.categories,
                relevant=example.labels.relevant,
                received_at=example.received_at,
                labeled_at=example.labeled_at,
            )
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the local EventEdge pre-LLM router without network calls"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test labels; artifact remains forbidden in production",
    )
    parser.add_argument(
        "--trained-at",
        type=datetime.fromisoformat,
        default=None,
        help="Timezone-aware ISO timestamp; useful for byte-reproducible test artifacts",
    )
    args = parser.parse_args()

    dataset_bytes = args.dataset.read_bytes()
    examples = load_quality_dataset(
        args.dataset,
        allow_synthetic=args.allow_synthetic,
    )
    label_sources = {example.label_source.value for example in examples}
    if len(label_sources) != 1:
        raise ValueError("an ML artifact cannot mix human and synthetic labels")

    split = temporal_group_split(examples)
    artifact = build_ml_router_artifact(
        train=_training_rows(split.train),
        validation=_training_rows(split.validation),
        test=_training_rows(split.test),
        label_source=label_sources.pop(),
        trained_at=args.trained_at or datetime.now(UTC),
        dataset_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
    )
    write_artifact(args.output, artifact)
    report = artifact_summary(artifact)
    report["filtered_by_upstream_heuristic"] = {
        "train": len(split.train) - artifact.training_examples,
        "validation": len(split.validation) - artifact.validation.observations,
        "test": len(split.test) - artifact.test.observations,
    }
    report["artifact_path"] = str(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
