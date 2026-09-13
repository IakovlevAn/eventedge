"""Fit the latest retained materiality fold into a portable JSON artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.news_model_training import fit_latest_materiality_artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fit-bundle", type=Path, required=True)
    parser.add_argument("--expected-fit-bundle-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = fit_latest_materiality_artifact(
        args.fit_bundle,
        args.output,
        expected_fit_bundle_sha256=args.expected_fit_bundle_sha256,
    )
    summary = {
        "artifact": str(args.output),
        "artifact_payload_sha256": artifact["artifact_payload_sha256"],
        "dataset_sha256": artifact["provenance"]["dataset_sha256"],
        "fit_bundle_sha256": artifact["provenance"]["fit_bundle_sha256"],
        "fold": artifact["provenance"]["fold"],
        "model_id": artifact["model_id"],
        "model_version": artifact["model_version"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
