"""Tests for bounded local research artifact I/O."""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
from pathlib import Path

import pytest

from eventedge_research import research_artifacts


def test_json_round_trip_is_exclusive(tmp_path):
    path = tmp_path / "artifact.json"

    research_artifacts.write_json(path, {"text": "новость", "value": 1})

    assert research_artifacts.read_json_object(path) == {
        "text": "новость",
        "value": 1,
    }
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        research_artifacts.write_json(path, {"value": 2})


def test_concurrent_json_writers_never_clobber(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.json"
    barrier = threading.Barrier(2)
    original_link = os.link

    def synchronized_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        barrier.wait(timeout=5)
        original_link(
            source,
            destination,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(research_artifacts.os, "link", synchronized_link)
    values = ({"writer": "first"}, {"writer": "second"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(research_artifacts.write_json, path, value) for value in values]

    results = [future.exception() for future in futures]

    assert sum(result is None for result in results) == 1
    errors = [result for result in results if result is not None]
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)
    assert research_artifacts.read_json_object(path) in values
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))


def test_jsonl_round_trip_stops_at_eof(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = [{"id": "one"}, {"id": "two"}]

    research_artifacts.write_jsonl(path, rows)

    assert list(research_artifacts.iter_jsonl(path)) == rows


def test_publish_immutable_file_preserves_competing_destination(tmp_path) -> None:
    source = tmp_path / "partial.jsonl"
    destination = tmp_path / "artifact.jsonl"
    source.write_text('{"writer": "candidate"}\n', encoding="utf-8")
    destination.write_text('{"writer": "winner"}\n', encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        research_artifacts.publish_immutable_file(source, destination)

    assert source.read_text(encoding="utf-8") == '{"writer": "candidate"}\n'
    assert destination.read_text(encoding="utf-8") == '{"writer": "winner"}\n'


def test_exclusive_run_lock_is_fail_closed_and_cleans_up(tmp_path) -> None:
    lock_path = tmp_path / "writer.lock"
    lock_path.write_text("stale owner\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="do not remove it automatically"):
        with research_artifacts.exclusive_run_lock(lock_path):
            pytest.fail("a stale lock must prevent a second writer")

    assert lock_path.read_text(encoding="utf-8") == "stale owner\n"
    lock_path.unlink()
    with pytest.raises(RuntimeError, match="test failure"):
        with research_artifacts.exclusive_run_lock(lock_path):
            assert lock_path.is_file()
            raise RuntimeError("test failure")
    assert not lock_path.exists()


def test_exclusive_run_lock_does_not_delete_a_replacement_lock(tmp_path) -> None:
    lock_path = tmp_path / "writer.lock"

    with research_artifacts.exclusive_run_lock(lock_path):
        lock_path.unlink()
        lock_path.write_text("replacement owner\n", encoding="utf-8")

    assert lock_path.read_text(encoding="utf-8") == "replacement owner\n"


def test_jsonl_reader_enforces_row_and_line_bounds(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line or row bound"):
        list(research_artifacts.iter_jsonl(path, maximum_rows=1))
    with pytest.raises(ValueError, match="line or row bound"):
        list(research_artifacts.iter_jsonl(path, maximum_line_bytes=4))


def test_jsonl_object_reader_rejects_non_object_rows(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="row is not an object"):
        list(research_artifacts.iter_jsonl_objects(path))


def test_json_reader_rejects_large_and_non_object_values(tmp_path):
    large = tmp_path / "large.json"
    large.write_text(json.dumps({"value": "12345"}), encoding="utf-8")
    sequence = tmp_path / "sequence.json"
    sequence.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="missing or too large"):
        research_artifacts.read_json(large, maximum_bytes=2)
    with pytest.raises(ValueError, match="not an object"):
        research_artifacts.read_json_object(sequence)


def test_timeout_handler_fails_closed() -> None:
    with pytest.raises(TimeoutError, match="hard wall-clock limit"):
        research_artifacts.raise_timeout(0, None)


def test_completion_manifest_verifies_relative_files(tmp_path) -> None:
    result = tmp_path / "result.json"
    result.write_text("{}\n", encoding="utf-8")
    research_artifacts.write_json(
        tmp_path / "complete.json",
        {
            "complete": True,
            "sha256": {"result.json": research_artifacts.file_sha256(result)},
        },
    )

    assert research_artifacts.verify_complete(tmp_path)["complete"] is True

    result.write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        research_artifacts.verify_complete(tmp_path)


def test_normalized_text_hash_ignores_case_and_spacing() -> None:
    assert research_artifacts.normalized_text_hash("  Важная  НОВОСТЬ ") == (
        research_artifacts.normalized_text_hash("важная новость")
    )
