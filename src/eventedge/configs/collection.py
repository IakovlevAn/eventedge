from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from eventedge.configs.loader import load_yaml_mapping


@dataclass(frozen=True)
class CollectionLaneConfig:
    id: str
    interval_seconds: int
    source_ids: tuple[str, ...]
    trigger_name: str
    description: str
    cron_expression: str
    payload: str

    def __post_init__(self) -> None:
        if not self.id or not self.source_ids:
            raise ValueError("A collection lane requires an id and at least one source")
        if self.interval_seconds <= 0:
            raise ValueError("A collection lane interval must be positive")

    def as_api_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "interval_seconds": self.interval_seconds,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class CollectionConfig:
    lanes: tuple[CollectionLaneConfig, ...]


def default_collection_config() -> CollectionConfig:
    return CollectionConfig(
        lanes=(
            CollectionLaneConfig(
                id="fast",
                interval_seconds=60,
                source_ids=(
                    "interfax",
                    "tass",
                    "rbc",
                    "moex_news",
                    "telegram_ak47pfl",
                    "telegram_markettwits",
                ),
                trigger_name="eventedge-fast-news",
                description="Priority market news every minute",
                cron_expression="* * ? * * *",
                payload="fast_news",
            ),
            CollectionLaneConfig(
                id="discovery",
                interval_seconds=300,
                source_ids=("google_news", "market_background"),
                trigger_name="eventedge-discovery-news",
                description="Broad market news discovery every five minutes",
                cron_expression="0/5 * ? * * *",
                payload="discovery_news",
            ),
            CollectionLaneConfig(
                id="slow",
                interval_seconds=900,
                source_ids=("cbr_press",),
                trigger_name="eventedge-slow-news",
                description="Macro context refresh every fifteen minutes",
                cron_expression="0/15 * ? * * *",
                payload="slow_news",
            ),
        )
    )


def load_collection_config(directory: str | Path | None = None) -> CollectionConfig:
    defaults = default_collection_config()
    document = load_yaml_mapping("collection.yaml", directory=directory)
    if unknown := set(document) - {"lanes"}:
        raise ValueError(f"Unknown keys in collection.yaml: {', '.join(sorted(unknown))}")
    raw_lanes = document.get("lanes")
    if raw_lanes is None:
        return defaults
    if not isinstance(raw_lanes, dict):
        raise ValueError("collection.yaml lanes must be a mapping")
    if unknown := set(raw_lanes) - {lane.id for lane in defaults.lanes}:
        raise ValueError(f"Unknown collection lanes: {', '.join(sorted(unknown))}")

    lanes: list[CollectionLaneConfig] = []
    for lane in defaults.lanes:
        raw = raw_lanes.get(lane.id)
        if raw is None:
            lanes.append(lane)
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"Collection lane {lane.id} must be a mapping")
        allowed = {
            "interval_seconds",
            "source_ids",
            "trigger_name",
            "description",
            "cron_expression",
            "payload",
        }
        if unknown := set(raw) - allowed:
            raise ValueError(f"Unknown keys for collection lane {lane.id}: {unknown}")
        values: dict[str, object] = dict(raw)
        if "source_ids" in values:
            source_ids = values["source_ids"]
            if not isinstance(source_ids, list) or not all(
                isinstance(source_id, str) for source_id in source_ids
            ):
                raise ValueError(f"Collection lane {lane.id} source_ids must be a string list")
            values["source_ids"] = tuple(source_ids)
        lanes.append(replace(lane, **values))
    return CollectionConfig(lanes=tuple(lanes))
