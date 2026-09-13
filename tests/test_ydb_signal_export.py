from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventedge_research import research_artifacts
from eventedge_research.ydb_signal_export import (
    YDB_EXPORT_MANIFEST_SCHEMA_VERSION,
    YDB_EXPORT_TABLES,
    YDB_SIGNAL_TABLE_NAMES,
    build_ydb_export_query,
    export_ydb_tables,
    parse_ydb_connection_string,
    validate_service_account_key,
    verify_ydb_export,
    ydb_export_read_consistency,
)


class _FakePool:
    def __init__(self, pages: list[list[object]]) -> None:
        self._pages = iter(pages)
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def execute_with_retries(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> list[object]:
        self.calls.append((query, parameters))
        return [SimpleNamespace(rows=next(self._pages))]


def test_parse_ydb_connection_string_accepts_cli_and_escaped_forms() -> None:
    expected_database = "/ru-central1/cloud/database"

    for value in (
        f"grpcs://ydb.example:2135/?database={expected_database}",
        rf"grpcs\://ydb.example:2135/?database={expected_database}",
    ):
        connection = parse_ydb_connection_string(value)
        assert connection.endpoint == "grpcs://ydb.example:2135"
        assert connection.database == expected_database


@pytest.mark.parametrize(
    "value",
    [
        "https://ydb.example/?database=/database",
        "grpcs://user@ydb.example/?database=/database",
        "grpcs://ydb.example/database",
        "grpcs://ydb.example/?database=relative",
        "grpcs://ydb.example/?database=/one&database=/two",
    ],
)
def test_parse_ydb_connection_string_rejects_unsafe_or_ambiguous_values(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        parse_ydb_connection_string(value)


def test_export_queries_are_bounded_selects_from_fixed_tables() -> None:
    forbidden = {"ALTER", "CREATE", "DELETE", "DROP", "INSERT", "UPDATE", "UPSERT"}

    for table in YDB_EXPORT_TABLES:
        query = build_ydb_export_query(table, has_cursor=True, limit=25)
        assert f"FROM `{table.name}`" in query
        assert f"ORDER BY `{table.key_column}`" in query
        assert "LIMIT 25" in query
        assert not forbidden.intersection(query.upper().replace(";", "").split())


def test_default_export_tables_remain_signal_inputs() -> None:
    assert YDB_SIGNAL_TABLE_NAMES == ("news_items", "feature_sets", "signals")


def test_export_ydb_tables_writes_normalized_jsonl_and_manifest(tmp_path: Path) -> None:
    published_at = datetime(2026, 1, 2, 10, 30)
    pool = _FakePool(
        [
            [
                _news_row("news_1", published_at),
                _news_row("news_2", published_at + timedelta(hours=1)),
            ],
            [],
        ]
    )
    output_directory = tmp_path / "export"

    manifest = asyncio.run(
        export_ydb_tables(
            pool,
            database="/ru-central1/cloud/database",
            output_directory=output_directory,
            table_names=("news_items",),
            batch_size=2,
            max_rows_per_table=10,
        )
    )

    rows = [
        json.loads(line)
        for line in (output_directory / "news_items.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["news_id"] for row in rows] == ["news_1", "news_2"]
    assert rows[0]["published_at"] == "2026-01-02T10:30:00Z"
    assert rows[0]["source_metadata"] == {"categories": ["macro"]}
    assert manifest["schema_version"] == YDB_EXPORT_MANIFEST_SCHEMA_VERSION
    assert manifest["read_consistency"] == {
        "mode": "independent_paginated_selects",
        "shared_snapshot": False,
        "concurrent_mutation_safe": False,
        "interpretation": (
            "bounded read-only extract, not a coherent point-in-time snapshot"
        ),
    }
    assert manifest["complete"] is True
    assert manifest["tables"]["news_items"]["rows"] == 2
    assert manifest["tables"]["news_items"]["truncated"] is False
    assert len(manifest["tables"]["news_items"]["sha256"]) == 64
    assert json.loads((output_directory / "manifest.json").read_text()) == manifest
    assert pool.calls[0][1] is None
    assert pool.calls[1][1] == {"$after_key": "news_2"}


def test_export_ydb_tables_marks_a_hard_row_limit(tmp_path: Path) -> None:
    published_at = datetime(2026, 1, 2, tzinfo=UTC)
    pool = _FakePool([[_news_row("news_1", published_at), _news_row("news_2", published_at)]])

    manifest = asyncio.run(
        export_ydb_tables(
            pool,
            database="/database",
            output_directory=tmp_path / "export",
            table_names=("news_items",),
            batch_size=10,
            max_rows_per_table=1,
        )
    )

    report = manifest["tables"]["news_items"]
    assert manifest["complete"] is False
    assert report["rows"] == 1
    assert report["truncated"] is True


def test_export_rejects_row_limit_larger_than_downstream_reader_cap(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 200000"):
        asyncio.run(
            export_ydb_tables(
                _FakePool([]),
                database="/database",
                output_directory=tmp_path / "export",
                table_names=("news_items",),
                max_rows_per_table=200_001,
            )
        )


def test_export_ydb_tables_supports_evaluation_epochs(tmp_path: Path) -> None:
    evaluated_at = datetime(2026, 9, 9, 8, 30, tzinfo=UTC)
    pool = _FakePool(
        [
            [
                SimpleNamespace(
                    epoch_id="eval_1",
                    model_version="signal-engine-0.6.1",
                    config_version=7,
                    evaluated_at=evaluated_at,
                    outcomes='[{"signal_id":"sig_1","verdict":true}]',
                    observations="[]",
                    observations_truncated=False,
                )
            ],
            [],
        ]
    )
    output_directory = tmp_path / "evaluation-export"

    manifest = asyncio.run(
        export_ydb_tables(
            pool,
            database="/database",
            output_directory=output_directory,
            table_names=("evaluation_epochs",),
        )
    )

    row = json.loads((output_directory / "evaluation_epochs.jsonl").read_text(encoding="utf-8"))
    assert row == {
        "config_version": 7,
        "epoch_id": "eval_1",
        "evaluated_at": "2026-09-09T08:30:00Z",
        "model_version": "signal-engine-0.6.1",
        "observations": [],
        "observations_truncated": False,
        "outcomes": [{"signal_id": "sig_1", "verdict": True}],
    }
    assert manifest["tables"]["evaluation_epochs"]["rows"] == 1


def test_export_refuses_to_overwrite_existing_data(tmp_path: Path) -> None:
    output_directory = tmp_path / "export"
    output_directory.mkdir()
    (output_directory / "news_items.jsonl").write_text("existing\n")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        asyncio.run(
            export_ydb_tables(
                _FakePool([]),
                database="/database",
                output_directory=output_directory,
                table_names=("news_items",),
            )
        )


def test_export_never_replaces_file_created_during_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "export"
    output_path = output_directory / "news_items.jsonl"
    original_link = research_artifacts.os.link

    def publish_after_competing_writer(
        source: str | Path,
        destination: str | Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        Path(destination).write_text("competing writer\n", encoding="utf-8")
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(research_artifacts.os, "link", publish_after_competing_writer)

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        asyncio.run(
            export_ydb_tables(
                _FakePool(
                    [
                        [_news_row("news_1", datetime(2026, 1, 2, tzinfo=UTC))],
                        [],
                    ]
                ),
                database="/database",
                output_directory=output_directory,
                table_names=("news_items",),
            )
        )

    assert output_path.read_text(encoding="utf-8") == "competing writer\n"
    assert not (output_directory / "manifest.json").exists()
    assert not (output_directory / ".eventedge-ydb-export.lock").exists()


def test_service_account_key_must_not_be_group_readable(tmp_path: Path) -> None:
    key_path = tmp_path / "key.json"
    key_path.write_text("{}")
    key_path.chmod(0o644)

    with pytest.raises(ValueError, match="permissions"):
        validate_service_account_key(key_path)

    key_path.chmod(0o600)
    validate_service_account_key(key_path)


def test_table_selection_rejects_non_allowlisted_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(
            export_ydb_tables(
                _FakePool([]),
                database="/database",
                output_directory=tmp_path / "export",
                table_names=("jobs",),
            )
        )


def test_verify_ydb_export_rejects_changed_data(tmp_path: Path) -> None:
    output_directory = tmp_path / "export"
    manifest = asyncio.run(
        export_ydb_tables(
            _FakePool([[_news_row("news_1", datetime(2026, 1, 2, tzinfo=UTC))], []]),
            database="/database",
            output_directory=output_directory,
            table_names=("news_items",),
        )
    )
    assert (
        verify_ydb_export(
            output_directory,
            required_tables=("news_items",),
        )
        == manifest
    )

    with (output_directory / "news_items.jsonl").open("a", encoding="utf-8") as output:
        output.write("{}\n")
    with pytest.raises(ValueError, match="integrity check failed"):
        verify_ydb_export(output_directory, required_tables=("news_items",))


def test_verify_ydb_export_accepts_legacy_v10_manifest(tmp_path: Path) -> None:
    output_directory = tmp_path / "export"
    asyncio.run(
        export_ydb_tables(
            _FakePool([[_news_row("news_1", datetime(2026, 1, 2, tzinfo=UTC))], []]),
            database="/database",
            output_directory=output_directory,
            table_names=("news_items",),
        )
    )
    manifest_path = output_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "eventedge-ydb-export-manifest-1.0"
    del manifest["read_consistency"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_ydb_export(
        output_directory,
        required_tables=("news_items",),
    ) == manifest
    assert ydb_export_read_consistency(manifest) == {
        "mode": "legacy_manifest_unspecified",
        "shared_snapshot": False,
        "concurrent_mutation_safe": False,
        "interpretation": (
            "legacy manifest has no consistency assertion; do not treat it as a "
            "coherent point-in-time snapshot"
        ),
    }


@pytest.mark.parametrize(
    "read_consistency",
    [
        None,
        {},
        {
            "mode": "independent_paginated_selects",
            "shared_snapshot": True,
            "concurrent_mutation_safe": False,
            "interpretation": (
                "bounded read-only extract, not a coherent point-in-time snapshot"
            ),
        },
        {
            "mode": "independent_paginated_selects",
            "shared_snapshot": False,
            "concurrent_mutation_safe": False,
            "interpretation": (
                "bounded read-only extract, not a coherent point-in-time snapshot"
            ),
            "unexpected": "extension",
        },
    ],
)
def test_verify_ydb_export_rejects_invalid_v11_consistency_contract(
    tmp_path: Path,
    read_consistency: object,
) -> None:
    output_directory = tmp_path / "export"
    asyncio.run(
        export_ydb_tables(
            _FakePool([[_news_row("news_1", datetime(2026, 1, 2, tzinfo=UTC))], []]),
            database="/database",
            output_directory=output_directory,
            table_names=("news_items",),
        )
    )
    manifest_path = output_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["read_consistency"] = read_consistency
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="read-consistency contract"):
        verify_ydb_export(output_directory, required_tables=("news_items",))


def _news_row(news_id: str, published_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        news_id=news_id,
        source_id="source",
        external_id=f"external-{news_id}",
        published_at=published_at,
        received_at=published_at + timedelta(minutes=1),
        title=f"Title {news_id}",
        url=f"https://example.test/{news_id}",
        content="Content",
        language="ru",
        source_metadata='{"categories":["macro"]}',
        created_at=published_at + timedelta(minutes=2),
    )
