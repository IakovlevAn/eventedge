from __future__ import annotations

from pathlib import Path

import pytest

from experiments.paths import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_DATASET_PATH,
    artifact_root_from_environment,
    dataset_path_from_environment,
    stage_artifact_directory,
)


def test_experiment_paths_default_to_ignored_project_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVENTEDGE_DATASET_PATH", raising=False)
    monkeypatch.delenv("EVENTEDGE_EXPERIMENT_ARTIFACT_ROOT", raising=False)

    assert dataset_path_from_environment() == DEFAULT_DATASET_PATH
    assert artifact_root_from_environment() == DEFAULT_ARTIFACT_ROOT
    assert stage_artifact_directory("direction_stage5") == (
        DEFAULT_ARTIFACT_ROOT / "direction_stage5"
    )


def test_experiment_paths_accept_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("EVENTEDGE_DATASET_PATH", str(dataset))
    monkeypatch.setenv("EVENTEDGE_EXPERIMENT_ARTIFACT_ROOT", str(artifacts))

    assert dataset_path_from_environment() == dataset
    assert artifact_root_from_environment() == artifacts
    assert stage_artifact_directory("market_stage2") == artifacts / "market_stage2"


@pytest.mark.parametrize("stage", ["", ".", "..", "../escape", "nested/stage", "nested\\stage"])
def test_experiment_stage_path_rejects_unsafe_components(stage: str) -> None:
    with pytest.raises(ValueError, match="safe path component"):
        stage_artifact_directory(stage)
