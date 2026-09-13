"""Bounded SELECT-only YDB export with explicit consistency semantics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import ydb

from eventedge_research.research_artifacts import (
    exclusive_run_lock,
    iter_jsonl_objects,
    publish_immutable_file,
    write_json,
)

MAXIMUM_EXPORTED_JSONL_ROWS = 200_000
MAXIMUM_EXPORTED_JSONL_LINE_BYTES = 64 * 1024 * 1024
YDB_EXPORT_MANIFEST_SCHEMA_VERSION = "eventedge-ydb-export-manifest-1.1"
_LEGACY_YDB_EXPORT_MANIFEST_SCHEMA_VERSION = "eventedge-ydb-export-manifest-1.0"
_SUPPORTED_YDB_EXPORT_MANIFEST_SCHEMAS = frozenset(
    {
        _LEGACY_YDB_EXPORT_MANIFEST_SCHEMA_VERSION,
        YDB_EXPORT_MANIFEST_SCHEMA_VERSION,
    }
)


@dataclass(frozen=True)
class YdbConnection:
    """Endpoint and database path parsed from a YDB connection string."""

    endpoint: str
    database: str


@dataclass(frozen=True)
class YdbExportTable:
    """Fixed schema for one table allowed in the research export."""

    name: str
    key_column: str
    timestamp_column: str
    columns: tuple[str, ...]
    object_columns: frozenset[str] = frozenset()
    array_columns: frozenset[str] = frozenset()
    timestamp_columns: frozenset[str] = frozenset()


YDB_SIGNAL_TABLE_NAMES = ("news_items", "feature_sets", "signals")

YDB_EXPORT_TABLES = (
    YdbExportTable(
        name="news_items",
        key_column="news_id",
        timestamp_column="published_at",
        columns=(
            "news_id",
            "source_id",
            "external_id",
            "published_at",
            "received_at",
            "title",
            "url",
            "content",
            "language",
            "source_metadata",
            "created_at",
        ),
        object_columns=frozenset({"source_metadata"}),
        timestamp_columns=frozenset({"published_at", "received_at", "created_at"}),
    ),
    YdbExportTable(
        name="feature_sets",
        key_column="feature_set_id",
        timestamp_column="created_at",
        columns=(
            "feature_set_id",
            "news_id",
            "schema_version",
            "extractor_version",
            "features",
            "created_at",
        ),
        object_columns=frozenset({"features"}),
        timestamp_columns=frozenset({"created_at"}),
    ),
    YdbExportTable(
        name="signals",
        key_column="signal_id",
        timestamp_column="as_of",
        columns=(
            "signal_id",
            "news_id",
            "ticker",
            "as_of",
            "data_cutoff_at",
            "status",
            "direction",
            "action",
            "horizon_value",
            "horizon_unit",
            "score",
            "strength",
            "confidence",
            "summary",
            "factor_contributions",
            "evidence_refs",
            "expires_at",
            "invalidation_conditions",
            "model_version",
            "config_version",
            "created_at",
        ),
        array_columns=frozenset(
            {"factor_contributions", "evidence_refs", "invalidation_conditions"}
        ),
        timestamp_columns=frozenset({"as_of", "data_cutoff_at", "expires_at", "created_at"}),
    ),
    YdbExportTable(
        name="evaluation_epochs",
        key_column="epoch_id",
        timestamp_column="evaluated_at",
        columns=(
            "epoch_id",
            "model_version",
            "config_version",
            "evaluated_at",
            "outcomes",
            "observations",
            "observations_truncated",
        ),
        array_columns=frozenset({"outcomes", "observations"}),
        timestamp_columns=frozenset({"evaluated_at"}),
    ),
)
YDB_EXPORT_TABLES_BY_NAME = {table.name: table for table in YDB_EXPORT_TABLES}

_FORBIDDEN_QUERY_TOKEN = re.compile(
    r"\b(ALTER|CREATE|DELETE|DROP|INSERT|REPLACE|UPDATE|UPSERT)\b",
    re.IGNORECASE,
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1_000_000


class YdbQueryPool(Protocol):
    """Minimal query pool contract used by the bounded exporter."""

    async def execute_with_retries(
        self,
        query: str,
        parameters: dict[str, object] | None = None,
    ) -> list[object]: ...


def parse_ydb_connection_string(value: str) -> YdbConnection:
    """Split the YDB CLI connection string into SDK endpoint and database values."""
    normalized = value.strip().replace(r"\://", "://", 1)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"grpc", "grpcs"} or not parsed.hostname:
        raise ValueError("YDB connection string must use grpc:// or grpcs://")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("YDB connection string must not contain credentials or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("YDB database must be supplied in the database query parameter")
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    if set(parameters) != {"database"} or len(parameters["database"]) != 1:
        raise ValueError("YDB connection string must contain exactly one database parameter")
    database = parameters["database"][0]
    if not database.startswith("/"):
        raise ValueError("YDB database path must be absolute")
    return YdbConnection(
        endpoint=f"{parsed.scheme}://{parsed.netloc}",
        database=database,
    )


def validate_service_account_key(path: Path) -> None:
    """Require a regular, non-group-readable service-account key file."""
    if not path.is_file():
        raise ValueError(f"service-account key is not a regular file: {path}")
    permissions = stat.S_IMODE(path.stat().st_mode)
    if permissions & 0o077:
        raise ValueError("service-account key permissions must not allow group or other access")


def ydb_export_read_consistency(manifest: Mapping[str, object]) -> dict[str, object]:
    """Resolve a manifest's read semantics without implying snapshot isolation."""
    schema_version = manifest.get("schema_version")
    if schema_version == YDB_EXPORT_MANIFEST_SCHEMA_VERSION:
        return _validate_read_consistency(manifest.get("read_consistency"))
    if schema_version == _LEGACY_YDB_EXPORT_MANIFEST_SCHEMA_VERSION:
        return {
            "mode": "legacy_manifest_unspecified",
            "shared_snapshot": False,
            "concurrent_mutation_safe": False,
            "interpretation": (
                "legacy manifest has no consistency assertion; do not treat it "
                "as a coherent point-in-time snapshot"
            ),
        }
    raise ValueError("unsupported YDB export manifest version")


def verify_ydb_export(
    directory: Path,
    *,
    required_tables: Sequence[str] = YDB_SIGNAL_TABLE_NAMES,
) -> dict[str, object]:
    """Verify a completed export manifest, row counts and file fingerprints."""
    selected_tables = _selected_tables(required_tables)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("YDB export manifest is missing or too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("YDB export manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise ValueError("YDB export manifest must be a JSON object")
    if manifest.get("schema_version") not in _SUPPORTED_YDB_EXPORT_MANIFEST_SCHEMAS:
        raise ValueError("unsupported YDB export manifest version")
    ydb_export_read_consistency(manifest)
    if manifest.get("complete") is not True:
        raise ValueError("YDB export manifest is incomplete")
    reports = manifest.get("tables")
    if not isinstance(reports, dict):
        raise ValueError("YDB export manifest tables must be an object")
    for table in selected_tables:
        report = reports.get(table.name)
        if not isinstance(report, dict) or report.get("truncated") is not False:
            raise ValueError(f"YDB export table is missing or truncated: {table.name}")
        file_name = report.get("file")
        expected_sha256 = report.get("sha256")
        expected_rows = report.get("rows")
        if file_name != f"{table.name}.jsonl":
            raise ValueError(f"unexpected YDB export filename for {table.name}")
        if not isinstance(expected_sha256, str) or not _SHA256_PATTERN.fullmatch(expected_sha256):
            raise ValueError(f"invalid YDB export fingerprint for {table.name}")
        if type(expected_rows) is not int or expected_rows < 0:
            raise ValueError(f"invalid YDB export row count for {table.name}")
        actual_sha256, actual_rows = _jsonl_fingerprint(directory / file_name)
        if actual_sha256 != expected_sha256 or actual_rows != expected_rows:
            raise ValueError(f"YDB export integrity check failed for {table.name}")
    return manifest


def iter_ydb_export_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream validated object rows from one bounded local YDB export file."""
    return iter_jsonl_objects(
        path,
        maximum_rows=MAXIMUM_EXPORTED_JSONL_ROWS,
        maximum_line_bytes=MAXIMUM_EXPORTED_JSONL_LINE_BYTES,
    )


def build_ydb_export_query(
    table: YdbExportTable,
    *,
    has_cursor: bool,
    limit: int,
) -> str:
    """Build one bounded SELECT for a table from the fixed export allowlist."""
    if YDB_EXPORT_TABLES_BY_NAME.get(table.name) != table:
        raise ValueError(f"table is not allowed for YDB export: {table.name}")
    if not 1 <= limit <= 1_000:
        raise ValueError("YDB export query limit must be between 1 and 1000")
    declarations = "DECLARE $after_key AS Utf8;\n\n" if has_cursor else ""
    where_clause = f"\nWHERE `{table.key_column}` > $after_key" if has_cursor else ""
    columns = ",\n    ".join(f"`{column}`" for column in table.columns)
    query = (
        f"{declarations}SELECT\n    {columns}\n"
        f"FROM `{table.name}`{where_clause}\n"
        f"ORDER BY `{table.key_column}`\nLIMIT {limit};\n"
    )
    _require_read_only_query(query)
    return query


async def export_ydb_signal_data(
    *,
    connection: YdbConnection,
    service_account_key_file: Path,
    output_directory: Path,
    table_names: Sequence[str] = YDB_SIGNAL_TABLE_NAMES,
    batch_size: int = 500,
    max_rows_per_table: int = 100_000,
    query_timeout_seconds: float = 60,
) -> dict[str, object]:
    """Connect with a service-account key and export the allowlisted signal tables."""
    validate_service_account_key(service_account_key_file)
    credentials = ydb.iam.ServiceAccountCredentials.from_file(str(service_account_key_file))
    config = ydb.DriverConfig(
        endpoint=connection.endpoint,
        database=connection.database,
        credentials=credentials,
        root_certificates=ydb.load_ydb_root_certificate(),
    )
    driver = ydb.aio.Driver(config)
    pool: ydb.aio.QuerySessionPool | None = None
    try:
        await driver.wait(timeout=15, fail_fast=True)
        pool = ydb.aio.QuerySessionPool(driver, size=1)
        return await export_ydb_tables(
            pool,
            database=connection.database,
            output_directory=output_directory,
            table_names=table_names,
            batch_size=batch_size,
            max_rows_per_table=max_rows_per_table,
            query_timeout_seconds=query_timeout_seconds,
        )
    finally:
        if pool is not None:
            await pool.stop()
        await driver.stop(timeout=5)


async def export_ydb_tables(
    pool: YdbQueryPool,
    *,
    database: str,
    output_directory: Path,
    table_names: Sequence[str] = YDB_SIGNAL_TABLE_NAMES,
    batch_size: int = 500,
    max_rows_per_table: int = 100_000,
    query_timeout_seconds: float = 60,
) -> dict[str, object]:
    """Export fixed YDB tables through a provided query pool."""
    if not 1 <= batch_size <= 1_000:
        raise ValueError("batch_size must be between 1 and 1000")
    if not 1 <= max_rows_per_table <= MAXIMUM_EXPORTED_JSONL_ROWS:
        raise ValueError(
            "max_rows_per_table must be between 1 and "
            f"{MAXIMUM_EXPORTED_JSONL_ROWS}"
        )
    if query_timeout_seconds <= 0:
        raise ValueError("query_timeout_seconds must be positive")
    selected_tables = _selected_tables(table_names)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory.chmod(0o700)
    lock_path = output_directory / ".eventedge-ydb-export.lock"
    with exclusive_run_lock(lock_path):
        return await _export_selected_tables(
            pool,
            database=database,
            output_directory=output_directory,
            selected_tables=selected_tables,
            batch_size=batch_size,
            max_rows_per_table=max_rows_per_table,
            query_timeout_seconds=query_timeout_seconds,
        )


async def _export_selected_tables(
    pool: YdbQueryPool,
    *,
    database: str,
    output_directory: Path,
    selected_tables: Sequence[YdbExportTable],
    batch_size: int,
    max_rows_per_table: int,
    query_timeout_seconds: float,
) -> dict[str, object]:
    """Write one YDB export while its output-directory lock is held."""
    output_paths = [output_directory / f"{table.name}.jsonl" for table in selected_tables]
    manifest_path = output_directory / "manifest.json"
    existing_paths = [path for path in (*output_paths, manifest_path) if path.exists()]
    if existing_paths:
        raise FileExistsError(f"refusing to overwrite existing export: {existing_paths[0]}")

    started_at = datetime.now(UTC)
    table_reports: dict[str, object] = {}
    created_paths: list[Path] = []
    try:
        for table, output_path in zip(selected_tables, output_paths, strict=True):
            table_reports[table.name] = await _export_table(
                pool,
                table=table,
                output_path=output_path,
                batch_size=batch_size,
                max_rows=max_rows_per_table,
                query_timeout_seconds=query_timeout_seconds,
            )
            created_paths.append(output_path)
        manifest: dict[str, object] = {
            "schema_version": YDB_EXPORT_MANIFEST_SCHEMA_VERSION,
            "started_at": _timestamp_text(started_at),
            "completed_at": _timestamp_text(datetime.now(UTC)),
            "database": database,
            "read_policy": "select-only-fixed-table-allowlist",
            "read_consistency": _current_read_consistency(),
            "batch_size": batch_size,
            "max_rows_per_table": max_rows_per_table,
            "complete": all(
                not bool(report["truncated"])
                for report in table_reports.values()
                if isinstance(report, dict)
            ),
            "tables": table_reports,
        }
        write_json(manifest_path, manifest)
        return manifest
    except BaseException:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise


async def _export_table(
    pool: YdbQueryPool,
    *,
    table: YdbExportTable,
    output_path: Path,
    batch_size: int,
    max_rows: int,
    query_timeout_seconds: float,
) -> dict[str, object]:
    digest = hashlib.sha256()
    row_count = 0
    after_key: str | None = None
    first_key: str | None = None
    minimum_timestamp: datetime | None = None
    maximum_timestamp: datetime | None = None
    truncated = False
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as destination:
            temporary_path = Path(destination.name)
            while True:
                remaining = max_rows - row_count
                page_limit = min(batch_size, max(1, remaining + 1))
                query = build_ydb_export_query(
                    table,
                    has_cursor=after_key is not None,
                    limit=page_limit,
                )
                parameters = {"$after_key": after_key} if after_key is not None else None
                async with asyncio.timeout(query_timeout_seconds):
                    result_sets = await pool.execute_with_retries(query, parameters)
                rows = _result_rows(result_sets)
                if len(rows) > page_limit:
                    raise RuntimeError(f"YDB returned too many rows for {table.name}")
                if not rows:
                    break
                for row in rows:
                    key = _row_value(row, table.key_column)
                    if not isinstance(key, str):
                        raise ValueError(f"{table.name}.{table.key_column} must be a string")
                    if after_key is not None and key <= after_key:
                        raise ValueError(f"{table.name} export keys are not strictly increasing")
                    if row_count >= max_rows:
                        truncated = True
                        break
                    payload = _export_row(table, row)
                    serialized = (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                    destination.write(serialized)
                    digest.update(serialized)
                    first_key = first_key or key
                    after_key = key
                    row_count += 1
                    observed_at = _row_value(row, table.timestamp_column)
                    if not isinstance(observed_at, datetime):
                        raise ValueError(
                            f"{table.name}.{table.timestamp_column} must be a timestamp"
                        )
                    normalized_at = _normalize_timestamp(observed_at)
                    minimum_timestamp = min(minimum_timestamp or normalized_at, normalized_at)
                    maximum_timestamp = max(maximum_timestamp or normalized_at, normalized_at)
                if truncated or len(rows) < page_limit:
                    break
            destination.flush()
        publish_immutable_file(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {
        "file": output_path.name,
        "rows": row_count,
        "truncated": truncated,
        "first_key": first_key,
        "last_key": after_key,
        "minimum_timestamp": (
            _timestamp_text(minimum_timestamp) if minimum_timestamp is not None else None
        ),
        "maximum_timestamp": (
            _timestamp_text(maximum_timestamp) if maximum_timestamp is not None else None
        ),
        "sha256": digest.hexdigest(),
    }


def _selected_tables(table_names: Sequence[str]) -> tuple[YdbExportTable, ...]:
    if not table_names:
        raise ValueError("at least one YDB export table must be selected")
    if len(set(table_names)) != len(table_names):
        raise ValueError("YDB export tables must be unique")
    unknown = [name for name in table_names if name not in YDB_EXPORT_TABLES_BY_NAME]
    if unknown:
        raise ValueError(f"table is not allowed for YDB export: {unknown[0]}")
    return tuple(YDB_EXPORT_TABLES_BY_NAME[name] for name in table_names)


def _current_read_consistency() -> dict[str, object]:
    """Return the exact read-consistency contract for newly created exports."""
    return {
        "mode": "independent_paginated_selects",
        "shared_snapshot": False,
        "concurrent_mutation_safe": False,
        "interpretation": (
            "bounded read-only extract, not a coherent point-in-time snapshot"
        ),
    }


def _validate_read_consistency(value: object) -> dict[str, object]:
    """Require the exact v1.1 consistency contract without silent extensions."""
    if not isinstance(value, dict) or value != _current_read_consistency():
        raise ValueError("invalid YDB export read-consistency contract")
    return dict(value)


def _require_read_only_query(query: str) -> None:
    if _FORBIDDEN_QUERY_TOKEN.search(query):
        raise ValueError("YDB export query contains a mutating statement")
    statements = [statement.strip() for statement in query.split(";") if statement.strip()]
    if not statements or not statements[-1].lstrip().upper().startswith("SELECT"):
        raise ValueError("YDB export query must end with SELECT")
    if any(not statement.upper().startswith(("DECLARE ", "SELECT")) for statement in statements):
        raise ValueError("YDB export query may contain only DECLARE and SELECT statements")


def _result_rows(result_sets: list[object]) -> tuple[object, ...]:
    if not result_sets:
        return ()
    if len(result_sets) != 1:
        raise RuntimeError("YDB export query returned an unexpected result-set count")
    rows = getattr(result_sets[0], "rows", None)
    if rows is None:
        raise RuntimeError("YDB export result set does not contain rows")
    return tuple(rows)


def _export_row(table: YdbExportTable, row: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    for column in table.columns:
        value = _row_value(row, column)
        if column in table.timestamp_columns:
            if not isinstance(value, datetime):
                raise ValueError(f"{table.name}.{column} must be a timestamp")
            value = _timestamp_text(value)
        elif column in table.object_columns:
            value = _stored_json(value)
            if not isinstance(value, dict):
                raise ValueError(f"{table.name}.{column} must contain a JSON object")
        elif column in table.array_columns:
            value = _stored_json(value)
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{table.name}.{column} must contain a JSON array")
            value = list(value)
        payload[column] = value
    return payload


def _row_value(row: object, column: str) -> object:
    if isinstance(row, Mapping):
        return row[column]
    return getattr(row, column)


def _stored_json(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return _normalize_timestamp(value).isoformat().replace("+00:00", "Z")


def _jsonl_fingerprint(path: Path) -> tuple[str, int]:
    if not path.is_file():
        raise ValueError(f"YDB export file is missing: {path.name}")
    digest = hashlib.sha256()
    row_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            row_count += chunk.count(b"\n")
    return digest.hexdigest(), row_count
