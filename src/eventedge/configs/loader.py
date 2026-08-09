from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

CONFIG_DIR_ENV = "EVENTEDGE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("configs")


def config_directory(
    directory: str | Path | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
) -> Path:
    if directory is not None:
        return Path(directory)
    return Path(environment.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR))


def load_yaml_mapping(
    filename: str,
    *,
    directory: str | Path | None = None,
    environment: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    path = config_directory(directory, environment=environment) / filename
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise ValueError(f"{path} must contain a YAML mapping at the document root")
    return loaded
