"""Bounded, immutable I/O helpers for local research artifacts."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

DEFAULT_MAXIMUM_JSON_BYTES = 100_000_000
DEFAULT_MAXIMUM_JSONL_BYTES = 1_000_000_000
DEFAULT_MAXIMUM_JSONL_ROWS = 200_000
DEFAULT_MAXIMUM_LINE_BYTES = 1_000_000


def raise_timeout(signum: int, frame: FrameType | None) -> None:
    """Raise a generic hard-deadline error for bounded local scripts."""
    raise TimeoutError("local research command exceeded its hard wall-clock limit")


def file_sha256(path: Path) -> str:
    """Hash a local file without retaining its contents in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_complete(directory: Path, *, maximum_files: int = 100) -> dict[str, Any]:
    """Verify an immutable completion manifest and every relative output hash."""
    directory = Path(directory)
    complete = read_json_object(directory / "complete.json")
    fingerprints = complete.get("sha256")
    if (
        complete.get("complete") is not True
        or not isinstance(fingerprints, dict)
        or not 1 <= len(fingerprints) <= maximum_files
    ):
        raise ValueError("research output is incomplete or exceeds its file bound")
    for name, expected_digest in fingerprints.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or file_sha256(directory / name) != expected_digest
        ):
            raise ValueError("research output fingerprint mismatch")
    return complete


def normalized_text_hash(content: str) -> str:
    """Hash text after case folding and whitespace normalization."""
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def split_overlap_audit(
    partitions: Mapping[str, Sequence[Any]],
) -> dict[str, dict[str, int]]:
    """Count exact identities shared by two or more dataset partitions."""
    extractors = {
        "event_id": lambda row: [row.event_id],
        "news_id": lambda row: row.news_ids,
        "exact_content": lambda row: [normalized_text_hash(row.content or row.title)],
        "exact_title_ticker": lambda row: [normalized_text_hash(row.title) + row.ticker],
        "price_window": lambda row: [str((row.ticker, row.entry_at, row.outcome_4h.observed_at))],
    }
    result: dict[str, dict[str, int]] = {}
    for name, extract in extractors.items():
        locations: defaultdict[str, set[str]] = defaultdict(set)
        for partition, rows in partitions.items():
            for row in rows:
                for key in extract(row):
                    locations[key].add(partition)
        shared = Counter(tuple(sorted(parts)) for parts in locations.values() if len(parts) > 1)
        result[name] = {" + ".join(parts): count for parts, count in sorted(shared.items())}
    return result


def read_json(
    path: Path,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_JSON_BYTES,
) -> Any:
    """Read a size-bounded JSON artifact."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError(f"JSON input is missing or too large: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_object(
    path: Path,
    *,
    maximum_bytes: int = DEFAULT_MAXIMUM_JSON_BYTES,
) -> dict[str, Any]:
    """Read a size-bounded JSON object and reject other top-level values."""
    value = read_json(path, maximum_bytes=maximum_bytes)
    if not isinstance(value, dict):
        raise ValueError(f"JSON input is not an object: {path}")
    return value


def iter_jsonl(
    path: Path,
    *,
    maximum_rows: int = DEFAULT_MAXIMUM_JSONL_ROWS,
    maximum_file_bytes: int = DEFAULT_MAXIMUM_JSONL_BYTES,
    maximum_line_bytes: int = DEFAULT_MAXIMUM_LINE_BYTES,
) -> Iterator[Any]:
    """Stream finite JSONL with explicit file, row and line bounds."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum_file_bytes:
        raise ValueError(f"JSONL input is missing or too large: {path}")
    opener = gzip.open if path.suffix == ".gz" else open
    decoded_bytes = 0
    with opener(path, "rb") as source:
        for index in range(maximum_rows + 1):
            line = source.readline(maximum_line_bytes + 1)
            if line == b"":
                return
            decoded_bytes += len(line)
            if (
                decoded_bytes > maximum_file_bytes
                or len(line) > maximum_line_bytes
                or index >= maximum_rows
            ):
                raise ValueError(f"JSONL input exceeds line or row bound: {path}")
            if not line.strip():
                continue
            yield json.loads(line)


def iter_jsonl_objects(
    path: Path,
    *,
    maximum_rows: int = DEFAULT_MAXIMUM_JSONL_ROWS,
    maximum_file_bytes: int = DEFAULT_MAXIMUM_JSONL_BYTES,
    maximum_line_bytes: int = DEFAULT_MAXIMUM_LINE_BYTES,
) -> Iterator[dict[str, Any]]:
    """Stream a bounded JSONL artifact and require object rows."""
    for row in iter_jsonl(
        path,
        maximum_rows=maximum_rows,
        maximum_file_bytes=maximum_file_bytes,
        maximum_line_bytes=maximum_line_bytes,
    ):
        if not isinstance(row, dict):
            raise ValueError(f"JSONL row is not an object: {path}")
        yield row


def write_json(
    path: Path,
    value: object,
    *,
    ensure_ascii: bool = False,
) -> None:
    """Atomically create an immutable JSON artifact."""
    serialized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=ensure_ascii,
        indent=2,
        sort_keys=True,
    )
    _write_text(path, (serialized, "\n"))


def write_jsonl(
    path: Path,
    rows: Iterable[Mapping[str, object]],
    *,
    maximum_rows: int = DEFAULT_MAXIMUM_JSONL_ROWS,
) -> None:
    """Atomically create a row-bounded JSONL artifact without buffering it."""

    def serialized_rows() -> Iterator[str]:
        for index, row in enumerate(rows):
            if index >= maximum_rows:
                raise ValueError(f"JSONL output exceeds row bound: {path}")
            yield json.dumps(row, allow_nan=False, ensure_ascii=False, sort_keys=True)
            yield "\n"

    _write_text(path, serialized_rows())


def publish_immutable_file(source_path: Path, destination_path: Path) -> None:
    """Publish a completed local file without replacing an existing artifact.

    Both paths must be on the same filesystem because the hard link is the
    atomic commit point. The source remains in place so callers can decide
    whether it is a disposable temporary file or a resumable partial output.
    """
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError(f"immutable artifact source is not a regular file: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source:
        os.fsync(source.fileno())
    source_path.chmod(0o600)
    try:
        os.link(source_path, destination_path, follow_symlinks=False)
    except FileExistsError as error:
        raise FileExistsError(
            f"refusing to overwrite research artifact: {destination_path}"
        ) from error


@contextmanager
def exclusive_run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, fail-closed lock for one local writer run.

    A lock left by a crashed process is never removed automatically. An
    operator must first verify that no process still owns that run and then
    remove the stale lock explicitly.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    payload = (
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "token": token,
            },
            sort_keys=True,
        )
        + "\n"
    )
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise FileExistsError(
            "research writer lock already exists; do not remove it automatically. "
            f"Verify that no process owns the run before manual removal: {path}"
        ) from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
        owner = os.fstat(lock_file.fileno())
        try:
            lock_file.write(payload)
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield
        finally:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (owner.st_dev, owner.st_ino):
                    path.unlink()
            except OSError:
                pass


def _write_text(path: Path, chunks: Iterable[str]) -> None:
    """Create text atomically through a same-directory temporary file.

    The hard link is the commit point: the operating system creates the
    destination only when it is absent. This avoids the time-of-check to
    time-of-use race inherent in checking ``Path.exists`` before ``replace``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            for chunk in chunks:
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        publish_immutable_file(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
