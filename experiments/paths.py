"""Portable local paths shared by the archived five-stage experiment runners."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DATASET_SHA256 = (
    "ac639e944a100c83b91275bbf627d3e9af9c654e5a0ce726dc484c0e2c0b81b6"
)
DEFAULT_DATASET_PATH = PROJECT_ROOT / ".local-data/news-direction-legacy/dataset.jsonl"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / ".local-artifacts/news-direction-legacy"


def dataset_path_from_environment() -> Path:
    """Return an explicit override or the repository-local ignored dataset path."""
    return Path(os.environ.get("EVENTEDGE_DATASET_PATH", DEFAULT_DATASET_PATH))


def artifact_root_from_environment() -> Path:
    """Return an explicit override or the repository-local ignored output root."""
    return Path(os.environ.get("EVENTEDGE_EXPERIMENT_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT))


def stage_artifact_directory(stage: str) -> Path:
    """Keep every stage in a separate generated-output directory."""
    if not stage or "/" in stage or "\\" in stage or stage in {".", ".."}:
        raise ValueError("stage must be one safe path component")
    return artifact_root_from_environment() / stage
