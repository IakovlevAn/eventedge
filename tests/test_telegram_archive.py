from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scripts.download_telegram_archive as telegram_archive_cli
from eventedge.configs.sources import TelegramChannelConfig
from eventedge_research.signal_dataset import load_signal_dataset, write_signal_dataset
from eventedge_research.signal_dataset_builder import (
    MarketOpenObservation,
    SignalEventCandidateV2,
    build_signal_dataset,
    load_signal_event_candidates,
)
from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2
from eventedge_research.telegram_archive import (
    TELEGRAM_CANDIDATE_ADMISSION_VERSION,
    TELEGRAM_INFORMATION_GROUP_POLICY_VERSION,
    TelegramArchiveOptions,
    TelegramArchiveRecord,
    build_telegram_archive_candidates,
    download_telegram_channel_archive,
    iter_telegram_archive_records,
    load_verified_telegram_archive_manifests,
    parse_telegram_archive_page,
)
from eventedge_research.ydb_signal_candidates import write_signal_event_candidates
from scripts.download_telegram_archive import _backfill_start_before_id


def test_archive_parser_keeps_edit_marker_and_all_message_ids() -> None:
    page = parse_telegram_archive_page(_latest_page(), channel="allowed_channel")

    assert page.message_ids == (5, 6)
    assert [item.external_id for item in page.items] == [
        "allowed_channel/6",
        "allowed_channel/5",
    ]
    assert page.items[0].is_edited is True
    assert page.items[1].is_edited is False


def test_archive_download_is_bounded_resumable_and_builds_candidates(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "telegram_allowed.jsonl"
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    base_options = {
        "since": datetime(2026, 9, 1, tzinfo=UTC),
        "until": datetime(2026, 9, 5, tzinfo=UTC),
        "consent_reference": "consent-test-1",
        "max_messages": 10,
        "delay_seconds": 0.5,
        "timeout_seconds": 5,
        "max_retries": 0,
    }
    pages = {
        "https://t.me/s/allowed_channel": _latest_page(),
        "https://t.me/s/allowed_channel?before=5": _older_page(),
    }
    requested_urls: list[str] = []

    def fetcher(url: str, timeout: float) -> bytes:
        assert timeout == 5
        requested_urls.append(url)
        return pages[url]

    incomplete = download_telegram_channel_archive(
        config,
        output_path,
        options=TelegramArchiveOptions(max_pages=1, **base_options),
        content_permission_confirmed=True,
        fetcher=fetcher,
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert incomplete["complete"] is False
    assert incomplete["completion_reason"] == "max_pages_reached"
    assert not output_path.exists()
    assert output_path.with_suffix(".jsonl.partial").is_file()

    complete = download_telegram_channel_archive(
        config,
        output_path,
        options=TelegramArchiveOptions(max_pages=3, **base_options),
        content_permission_confirmed=True,
        resume=True,
        fetcher=fetcher,
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert complete["complete"] is True
    assert complete["completion_reason"] == "since_reached"
    assert complete["pages_fetched"] == 2
    assert complete["records_written"] == 3
    assert complete["edited_records"] == 1
    assert requested_urls == [
        "https://t.me/s/allowed_channel",
        "https://t.me/s/allowed_channel?before=5",
    ]
    records = list(iter_telegram_archive_records([output_path], max_rows=10))
    assert {record.message_id for record in records} == {4, 5, 6}
    assert all(record.permission_asserted for record in records)

    candidates, report = build_telegram_archive_candidates(records, max_rows=10)

    assert {candidate.ticker for candidate in candidates} == {"GAZP", "SBER"}
    assert report["first_snapshot_guaranteed"] is False
    assert report["import"]["skipped_rows"] == {"edited_current_view": 1}


def test_long_archive_record_builds_one_bounded_version_two_labeled_dataset(
    tmp_path: Path,
) -> None:
    published_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    decision_at = published_at + timedelta(minutes=5)
    retrieved_at = datetime(2026, 9, 6, tzinfo=UTC)
    title = "Сбербанк сообщил о росте чистой прибыли на 20%"
    content = title + " абв" * 20_000 + " Газпром сообщил о росте добычи"
    assert len(content) > 50_000
    record = TelegramArchiveRecord(
        schema_version="eventedge-telegram-archive-message-1.0",
        source_id="telegram_allowed",
        channel="allowed_channel",
        external_id="allowed_channel/7",
        message_id=7,
        published_at=published_at,
        retrieved_at=retrieved_at,
        title=title,
        content=content,
        url="https://t.me/allowed_channel/7",
        language="ru",
        edit_marker_present=False,
        causal_text_status="no_edit_marker_current_view",
        snapshot_provenance="historical_public_page_current_view",
        permission_asserted=True,
        consent_reference="consent-test-1",
        permission_verification="caller_assertion_not_independently_verified",
    )
    raw_before = record.model_dump(mode="json")

    candidates, candidate_report = build_telegram_archive_candidates([record])

    assert record.model_dump(mode="json") == raw_before
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, SignalEventCandidateV2)
    assert candidate.ticker == "SBER"
    assert candidate.schema_version == "signal-event-candidate-2.0"
    assert candidate.event_group_id == candidate.event_id
    assert candidate.source_url == record.url
    assert candidate.retrieved_at == retrieved_at
    assert candidate.admission_version == TELEGRAM_CANDIDATE_ADMISSION_VERSION
    assert candidate.analysis_title == record.title
    assert len(candidate.content) == 50_000
    assert candidate.content == record.content[:50_000]
    assert candidate.analysis_content_sha256 == hashlib.sha256(
        candidate.content.encode("utf-8")
    ).hexdigest()
    assert candidate.receipt_provenance == "publication_plus_assumed_5_minutes"
    assert candidate.text_provenance == (
        "no_edit_marker_current_view_not_first_snapshot"
    )
    assert candidate.corporate_action_status == "unknown"
    assert candidate_report["information_group_policy"] == {
        "version": TELEGRAM_INFORMATION_GROUP_POLICY_VERSION,
        "field": "event_group_id",
        "basis": "deterministic_event_cluster_id",
        "conservative_initial_group": True,
    }
    assert candidate_report["analysis_payload_policy"] == {
        "version": "telegram-analysis-payload-1.0",
        "title": "exact_archive_title",
        "content": "archive_content_prefix",
        "maximum_content_characters": 50_000,
    }
    with pytest.raises(ValueError, match="analysis content fingerprint"):
        SignalEventCandidateV2.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "analysis_content_sha256": "0" * 64,
            }
        )

    candidate_path = tmp_path / "candidates.jsonl"
    write_signal_event_candidates(candidate_path, candidates)
    loaded_candidates = load_signal_event_candidates(candidate_path)
    assert loaded_candidates == candidates

    entry_at = decision_at + timedelta(minutes=1)
    stock = [
        _market_open("SBER", entry_at, 100),
        _market_open("SBER", entry_at + timedelta(hours=4), 102),
    ]
    benchmark = [
        _market_open("IMOEX2", entry_at, 3_000),
        _market_open("IMOEX2", entry_at + timedelta(hours=4), 3_015),
    ]
    examples, dataset_report = build_signal_dataset(
        loaded_candidates,
        stock,
        benchmark_observations=benchmark,
        benchmark_id="IMOEX2",
        maximum_entry_lag=timedelta(seconds=60),
    )
    dataset_path = tmp_path / "dataset.jsonl"
    write_signal_dataset(dataset_path, examples)
    loaded_examples = load_signal_dataset(dataset_path, allow_synthetic=True)

    assert dataset_report["candidate_schema_version"] == (
        "signal-event-candidate-2.0"
    )
    assert dataset_report["dataset_schema_version"] == "signal-dataset-example-2.0"
    assert len(loaded_examples) == 1
    example = loaded_examples[0]
    assert isinstance(example, SignalDatasetExampleV2)
    assert example.event_group_id == candidate.event_group_id
    assert example.source_url == candidate.source_url
    assert example.retrieved_at == candidate.retrieved_at
    assert example.admission_version == candidate.admission_version
    assert example.analysis_title == candidate.analysis_title
    assert example.analysis_content_sha256 == candidate.analysis_content_sha256
    assert example.content == candidate.content
    assert example.receipt_provenance == candidate.receipt_provenance
    assert example.text_provenance == candidate.text_provenance
    assert example.corporate_action_status == "unknown"
    assert example.outcome_4h.benchmark_id == "IMOEX2"
    assert example.outcome_4h.abnormal_return_pct is not None


def test_archive_download_requires_explicit_permission(tmp_path: Path) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
    )

    with pytest.raises(ValueError, match="permission confirmation"):
        download_telegram_channel_archive(
            config,
            tmp_path / "archive.jsonl",
            options=options,
            content_permission_confirmed=False,
        )


def test_archive_download_allows_only_one_resumable_writer(tmp_path: Path) -> None:
    output_path = tmp_path / "archive.jsonl"
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
        max_pages=1,
        delay_seconds=0.5,
        max_retries=0,
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def blocking_fetcher(url: str, timeout: float) -> bytes:
        del url, timeout
        fetch_started.set()
        if not release_fetch.wait(timeout=5):
            raise TimeoutError("test did not release the bounded fetch")
        return _latest_page()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        first_writer = executor.submit(
            download_telegram_channel_archive,
            config,
            output_path,
            options=options,
            content_permission_confirmed=True,
            fetcher=blocking_fetcher,
            sleeper=lambda _: None,
            clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        )
        try:
            assert fetch_started.wait(timeout=5)
            with pytest.raises(
                FileExistsError,
                match="do not remove it automatically",
            ):
                download_telegram_channel_archive(
                    config,
                    output_path,
                    options=options,
                    content_permission_confirmed=True,
                    fetcher=lambda *_: pytest.fail("second writer must not fetch"),
                    sleeper=lambda _: None,
                )
        finally:
            release_fetch.set()
        report = first_writer.result(timeout=5)

    assert report["complete"] is False
    assert output_path.with_suffix(".jsonl.partial").is_file()
    assert not output_path.with_suffix(".jsonl.lock").exists()


def test_archive_batch_allows_only_one_output_directory_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "batch"
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
    )
    download_started = threading.Event()
    release_download = threading.Event()

    def blocking_download(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        download_started.set()
        if not release_download.wait(timeout=5):
            raise TimeoutError("test did not release the bounded download")
        return {"complete": False}

    monkeypatch.setattr(
        telegram_archive_cli,
        "download_telegram_channel_archive",
        blocking_download,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        first_writer = executor.submit(
            telegram_archive_cli._download_batch,
            output_dir,
            channels=(config,),
            options=options,
            backfill_from_dir=None,
            resume=False,
            max_run_seconds=60,
        )
        try:
            assert download_started.wait(timeout=5)
            with pytest.raises(FileExistsError, match="do not remove it automatically"):
                telegram_archive_cli._download_batch(
                    output_dir,
                    channels=(config,),
                    options=options,
                    backfill_from_dir=None,
                    resume=True,
                    max_run_seconds=60,
                )
        finally:
            release_download.set()
        manifest = first_writer.result(timeout=5)

    assert manifest["complete"] is False
    assert not (
        output_dir / telegram_archive_cli._BATCH_LOCK_FILENAME
    ).exists()


def test_archive_batch_never_removes_stale_output_directory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "batch"
    output_dir.mkdir()
    lock_path = output_dir / telegram_archive_cli._BATCH_LOCK_FILENAME
    stale_payload = '{"pid": 12345, "token": "stale"}\n'
    lock_path.write_text(stale_payload, encoding="utf-8")
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
    )
    monkeypatch.setattr(
        telegram_archive_cli,
        "download_telegram_channel_archive",
        lambda *args, **kwargs: pytest.fail("stale lock must fail before download"),
    )

    with pytest.raises(FileExistsError, match="manual removal"):
        telegram_archive_cli._download_batch(
            output_dir,
            channels=(config,),
            options=options,
            backfill_from_dir=None,
            resume=True,
            max_run_seconds=60,
        )

    assert lock_path.read_text(encoding="utf-8") == stale_payload


def test_archive_finalization_never_replaces_competing_output(tmp_path: Path) -> None:
    output_path = tmp_path / "archive.jsonl"
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )

    def competing_fetcher(url: str, timeout: float) -> bytes:
        del url, timeout
        output_path.write_text("competing writer\n", encoding="utf-8")
        return _older_page()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        download_telegram_channel_archive(
            config,
            output_path,
            options=TelegramArchiveOptions(
                since=datetime(2026, 8, 30, 8, tzinfo=UTC),
                until=datetime(2026, 9, 1, tzinfo=UTC),
                consent_reference="consent-test-1",
                max_pages=1,
                delay_seconds=0.5,
                max_retries=0,
                start_before_id=5,
            ),
            content_permission_confirmed=True,
            fetcher=competing_fetcher,
            sleeper=lambda _: None,
            clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        )

    assert output_path.read_text(encoding="utf-8") == "competing writer\n"
    assert output_path.with_suffix(".jsonl.partial").is_file()
    assert output_path.with_suffix(".jsonl.checkpoint.json").is_file()
    assert not output_path.with_suffix(".jsonl.lock").exists()


def test_archive_download_honors_wall_clock_deadline(tmp_path: Path) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
        delay_seconds=0.5,
        max_retries=0,
    )
    requested_urls: list[str] = []

    with pytest.raises(TimeoutError, match="wall-clock limit"):
        download_telegram_channel_archive(
            config,
            tmp_path / "archive.jsonl",
            options=options,
            content_permission_confirmed=True,
            fetcher=lambda url, _: requested_urls.append(url) or _latest_page(),
            monotonic=lambda: 10.0,
            run_deadline_monotonic=10.0,
        )

    assert requested_urls == []
    assert (tmp_path / "archive.jsonl.partial").is_file()
    assert (tmp_path / "archive.jsonl.checkpoint.json").is_file()
    assert not (tmp_path / "archive.jsonl.lock").exists()


def test_archive_download_caps_network_timeout_at_run_deadline(
    tmp_path: Path,
) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    observed_timeouts: list[float] = []

    report = download_telegram_channel_archive(
        config,
        tmp_path / "archive.jsonl",
        options=TelegramArchiveOptions(
            since=datetime(2026, 8, 30, 8, tzinfo=UTC),
            until=datetime(2026, 9, 1, tzinfo=UTC),
            consent_reference="consent-test-1",
            max_pages=1,
            delay_seconds=0.5,
            timeout_seconds=5,
            max_retries=0,
            start_before_id=5,
        ),
        content_permission_confirmed=True,
        fetcher=lambda _, timeout: observed_timeouts.append(timeout) or _older_page(),
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        monotonic=lambda: 8.0,
        run_deadline_monotonic=10.0,
    )

    assert observed_timeouts == [2.0]
    assert report["complete"] is True


def test_archive_download_can_start_before_an_adjacent_message_id(
    tmp_path: Path,
) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    requested_urls: list[str] = []

    report = download_telegram_channel_archive(
        config,
        tmp_path / "backfill.jsonl",
        options=TelegramArchiveOptions(
            since=datetime(2026, 8, 30, 8, tzinfo=UTC),
            until=datetime(2026, 9, 1, tzinfo=UTC),
            consent_reference="consent-test-1",
            max_pages=2,
            delay_seconds=0.5,
            max_retries=0,
            start_before_id=5,
        ),
        content_permission_confirmed=True,
        fetcher=lambda url, _: requested_urls.append(url) or _older_page(),
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert requested_urls == ["https://t.me/s/allowed_channel?before=5"]
    assert report["complete"] is True
    assert report["start_before_id"] == 5
    assert report["records_written"] == 1


def test_backfill_cursor_requires_a_valid_contiguous_archive(tmp_path: Path) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    adjacent_dir = tmp_path / "adjacent"
    adjacent_dir.mkdir()
    data_path = adjacent_dir / "telegram_allowed.jsonl"
    report = download_telegram_channel_archive(
        config,
        data_path,
        options=TelegramArchiveOptions(
            since=datetime(2026, 9, 1, tzinfo=UTC),
            until=datetime(2026, 9, 5, tzinfo=UTC),
            consent_reference="consent-test-1",
            max_pages=3,
            delay_seconds=0.5,
            max_retries=0,
        ),
        content_permission_confirmed=True,
        fetcher=lambda url, _: {
            "https://t.me/s/allowed_channel": _latest_page(),
            "https://t.me/s/allowed_channel?before=5": _older_page(),
        }[url],
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    report_path = adjacent_dir / "telegram_allowed.report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    start_before_id = _backfill_start_before_id(
        adjacent_dir,
        config=config,
        adjacent_until=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert start_before_id == 4
    with pytest.raises(ValueError, match="end exactly"):
        _backfill_start_before_id(
            adjacent_dir,
            config=config,
            adjacent_until=datetime(2026, 8, 31, tzinfo=UTC),
        )


def test_resume_can_start_a_channel_without_an_existing_checkpoint(
    tmp_path: Path,
) -> None:
    config = TelegramChannelConfig(
        source_id="telegram_allowed",
        channel="allowed_channel",
    )
    options = TelegramArchiveOptions(
        since=datetime(2026, 9, 1, tzinfo=UTC),
        until=datetime(2026, 9, 5, tzinfo=UTC),
        consent_reference="consent-test-1",
        max_pages=3,
        delay_seconds=0.5,
        max_retries=0,
    )
    pages = {
        "https://t.me/s/allowed_channel": _latest_page(),
        "https://t.me/s/allowed_channel?before=5": _older_page(),
    }

    report = download_telegram_channel_archive(
        config,
        tmp_path / "archive.jsonl",
        options=options,
        content_permission_confirmed=True,
        resume=True,
        fetcher=lambda url, _: pages[url],
        sleeper=lambda _: None,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert report["complete"] is True
    assert report["records_written"] == 3


def test_archive_reader_rejects_tampered_identity(tmp_path: Path) -> None:
    path = tmp_path / "archive.jsonl"
    record = {
        "schema_version": "eventedge-telegram-archive-message-1.0",
        "source_id": "telegram_allowed",
        "channel": "allowed_channel",
        "external_id": "allowed_channel/7",
        "message_id": 8,
        "published_at": "2026-09-01T00:00:00Z",
        "retrieved_at": "2026-09-06T00:00:00Z",
        "title": "Сбербанк сообщил результаты",
        "content": "Сбербанк сообщил результаты",
        "url": "https://t.me/allowed_channel/7",
        "language": "ru",
        "edit_marker_present": False,
        "causal_text_status": "no_edit_marker_current_view",
        "snapshot_provenance": "historical_public_page_current_view",
        "permission_asserted": True,
        "consent_reference": "consent-test-1",
        "permission_verification": "caller_assertion_not_independently_verified",
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid Telegram archive row"):
        list(iter_telegram_archive_records([path]))


def test_batch_manifest_verifies_archive_hash_count_identity_and_window(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    archive = root / "raw/archive.jsonl"
    archive.parent.mkdir(parents=True)
    record = _manifest_record()
    archive.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    manifest = _batch_manifest(
        data_path="raw/archive.jsonl",
        data_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        records_written=1,
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    paths, report = load_verified_telegram_archive_manifests(
        [Path("manifest.json")],
        artifact_root=root,
    )

    assert paths == [archive.resolve()]
    assert report["verified"] is True
    assert report["records"] == 1


@pytest.mark.parametrize(
    ("data_path", "digest", "rows", "error"),
    [
        ("raw/archive.jsonl", "0" * 64, 1, "fingerprint differs"),
        ("raw/archive.jsonl", None, 2, "row count differs"),
        ("../outside.jsonl", None, 1, "escapes the artifact root"),
    ],
)
def test_batch_manifest_rejects_tampering(
    tmp_path: Path,
    data_path: str,
    digest: str | None,
    rows: int,
    error: str,
) -> None:
    root = tmp_path / "artifacts"
    archive = root / "raw/archive.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text(_manifest_record().model_dump_json() + "\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(archive.read_bytes())
    manifest = _batch_manifest(
        data_path=data_path,
        data_sha256=digest or hashlib.sha256(archive.read_bytes()).hexdigest(),
        records_written=rows,
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_verified_telegram_archive_manifests(
            [manifest_path],
            artifact_root=root,
        )


def _market_open(ticker: str, at: datetime, price: float) -> MarketOpenObservation:
    return MarketOpenObservation(
        ticker=ticker,
        at=at,
        open=price,
        provider_id="synthetic-fixture",
        adjusted=False,
        label_source="synthetic_test",
    )


def _manifest_record() -> TelegramArchiveRecord:
    return TelegramArchiveRecord(
        schema_version="eventedge-telegram-archive-message-1.0",
        source_id="telegram_allowed",
        channel="allowed_channel",
        external_id="allowed_channel/7",
        message_id=7,
        published_at=datetime(2026, 9, 1, 8, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 6, tzinfo=UTC),
        title="Сбербанк сообщил результаты",
        content="Сбербанк сообщил результаты",
        url="https://t.me/allowed_channel/7",
        language="ru",
        edit_marker_present=False,
        causal_text_status="no_edit_marker_current_view",
        snapshot_provenance="historical_public_page_current_view",
        permission_asserted=True,
        consent_reference="consent-test-1",
        permission_verification="caller_assertion_not_independently_verified",
    )


def _batch_manifest(
    *,
    data_path: str,
    data_sha256: str,
    records_written: int,
) -> dict[str, object]:
    return {
        "schema_version": "eventedge-telegram-archive-batch-manifest-1.0",
        "complete": True,
        "permission_asserted": True,
        "consent_reference": "consent-test-1",
        "channels": [
            {
                "schema_version": "eventedge-telegram-archive-manifest-1.0",
                "complete": True,
                "source_id": "telegram_allowed",
                "channel": "allowed_channel",
                "data_path": data_path,
                "data_sha256": data_sha256,
                "records_written": records_written,
                "window": {
                    "since_inclusive": "2026-09-01T00:00:00+00:00",
                    "until_exclusive": "2026-09-02T00:00:00+00:00",
                },
                "permission": {
                    "asserted": True,
                    "consent_reference": "consent-test-1",
                },
            }
        ],
    }


def _latest_page() -> bytes:
    return _page(
        _message(
            5,
            "2026-09-03T08:00:00+00:00",
            "Газпром сообщил о росте добычи",
        ),
        _message(
            6,
            "2026-09-04T08:00:00+00:00",
            "Лукойл сообщил результаты",
            edited=True,
        ),
    )


def _older_page() -> bytes:
    return _page(
        _message(
            3,
            "2026-08-30T08:00:00+00:00",
            "Северсталь сообщила результаты",
        ),
        _message(
            4,
            "2026-09-01T08:00:00+00:00",
            "Сбербанк сообщил о росте прибыли",
        ),
    )


def _page(*messages: str) -> bytes:
    return (
        '<section class="tgme_channel_history js-message_history">'
        + "".join(messages)
        + "</section>"
    ).encode()


def _message(
    message_id: int,
    published_at: str,
    content: str,
    *,
    edited: bool = False,
) -> str:
    edit_marker = "edited &nbsp;" if edited else ""
    return f"""
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message js-widget_message"
             data-post="allowed_channel/{message_id}">
          <div class="tgme_widget_message_text js-message_text">{content}</div>
          <span class="tgme_widget_message_meta">{edit_marker}
            <a class="tgme_widget_message_date"
               href="https://t.me/allowed_channel/{message_id}">
              <time datetime="{published_at}">08:00</time>
            </a>
          </span>
        </div>
      </div>
    """
