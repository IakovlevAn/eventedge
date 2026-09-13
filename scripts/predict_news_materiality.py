"""Run portable materiality inference on outcome-free feature-row JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.news_model_artifact import (
    load_portable_materiality_model,
    predict_materiality_jsonl,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-payload-sha256", required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    model = load_portable_materiality_model(
        args.artifact,
        expected_payload_sha256=args.expected_payload_sha256,
    )
    report = predict_materiality_jsonl(model, args.input, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
