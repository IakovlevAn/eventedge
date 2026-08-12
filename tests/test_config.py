from pathlib import Path

import pytest

from eventedge.configs.collection import load_collection_config
from eventedge.configs.scoring import load_scoring_config
from eventedge.configs.sources import load_source_config


def write_config(directory: Path, filename: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(content, encoding="utf-8")


def test_missing_yaml_uses_dataclass_defaults(tmp_path: Path) -> None:
    sources = load_source_config(tmp_path)
    collection = load_collection_config(tmp_path)
    scoring = load_scoring_config(tmp_path)

    assert [source.channel for source in sources.telegram_channels] == [
        "AK47pfl",
        "markettwits",
        "centralbank_russia",
        "MoscowExchangeOfficial",
        "bcs_express",
        "russianmacro",
    ]
    assert collection.lanes[0].interval_seconds == 60
    assert scoring.source_quality["telegram_markettwits"] == 0.68


def test_yaml_overrides_only_explicit_source_values(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "sources.yaml",
        """
telegram_channels:
  telegram_markettwits:
    max_items: 40
    timeout_seconds: 12
""",
    )

    config = load_source_config(tmp_path)
    ak47, markettwits, *_ = config.telegram_channels

    assert ak47.max_items == 20
    assert markettwits.max_items == 40
    assert markettwits.timeout_seconds == 12
    assert markettwits.channel == "markettwits"


def test_collection_and_scoring_yaml_overrides(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "collection.yaml",
        """
lanes:
  fast:
    interval_seconds: 120
""",
    )
    write_config(
        tmp_path,
        "scoring.yaml",
        """
default_source_quality: 0.6
source_quality:
  telegram_markettwits: 0.72
""",
    )

    collection = load_collection_config(tmp_path)
    scoring = load_scoring_config(tmp_path)

    assert collection.lanes[0].interval_seconds == 120
    assert collection.lanes[0].source_ids[-1] == "moex_news"
    assert collection.lanes[1].source_ids[-1] == "telegram_russianmacro"
    assert scoring.default_source_quality == 0.6
    assert scoring.source_quality["telegram_markettwits"] == 0.72
    assert scoring.source_quality["interfax"] == 0.9


def test_unknown_source_override_fails_fast(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "sources.yaml",
        """
telegram_channels:
  telegram_unknown:
    channel: unknown
""",
    )

    with pytest.raises(ValueError, match="telegram_unknown"):
        load_source_config(tmp_path)
