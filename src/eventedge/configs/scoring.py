from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from eventedge.configs.loader import load_yaml_mapping


def default_source_quality_values() -> dict[str, float]:
    return {
        "company": 0.95,
        "cbr_press": 0.95,
        "moex": 0.95,
        "moex_news": 0.95,
        "interfax": 0.90,
        "reuters": 0.90,
        "tass": 0.82,
        "rbc": 0.78,
        "google_news": 0.74,
        "market_news": 0.74,
        "telegram_ak47pfl": 0.68,
        "telegram_markettwits": 0.68,
    }


@dataclass(frozen=True)
class ScoringConfig:
    default_source_quality: float = 0.65
    source_quality: Mapping[str, float] = field(default_factory=default_source_quality_values)

    def __post_init__(self) -> None:
        _validate_quality(self.default_source_quality, "default_source_quality")
        for source_id, quality in self.source_quality.items():
            _validate_quality(quality, source_id)


def load_scoring_config(directory: str | Path | None = None) -> ScoringConfig:
    defaults = ScoringConfig()
    document = load_yaml_mapping("scoring.yaml", directory=directory)
    allowed = {"default_source_quality", "source_quality"}
    if unknown := set(document) - allowed:
        raise ValueError(f"Unknown keys in scoring.yaml: {', '.join(sorted(unknown))}")

    source_quality = dict(defaults.source_quality)
    raw_quality = document.get("source_quality", {})
    if not isinstance(raw_quality, dict):
        raise ValueError("scoring.yaml source_quality must be a mapping")
    for source_id, quality in raw_quality.items():
        if not isinstance(source_id, str) or not isinstance(quality, int | float):
            raise ValueError("Source quality entries must map source ids to numbers")
        source_quality[source_id] = float(quality)
    default_quality = document.get("default_source_quality", defaults.default_source_quality)
    if not isinstance(default_quality, int | float):
        raise ValueError("default_source_quality must be numeric")
    return ScoringConfig(
        default_source_quality=float(default_quality),
        source_quality=source_quality,
    )


def _validate_quality(value: float, name: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"Source quality {name} must be between 0 and 1")
