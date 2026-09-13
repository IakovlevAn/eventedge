"""Bounded, consent-gated research import for public Telegram channel archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge.analysis import DEFAULT_MOEX_ALIASES, NewsAnalysisInput, RuleBasedNewsExtractor
from eventedge.collectors import RssItem, fetch_telegram_channel, parse_telegram_channel
from eventedge.configs.sources import TelegramChannelConfig
from eventedge_research.research_artifacts import (
    exclusive_run_lock,
    file_sha256,
    iter_jsonl_objects,
    publish_immutable_file,
    read_json_object,
)
from eventedge_research.signal_dataset_builder import (
    SignalEventCandidate,
    SignalEventCandidateV2,
)
from eventedge_research.ydb_signal_candidates import (
    MAX_CANDIDATE_CONTENT_CHARS,
    SignalNewsSnapshot,
    build_signal_candidates_from_news_snapshots,
)

ARCHIVE_SCHEMA_VERSION = "eventedge-telegram-archive-message-1.0"
MANIFEST_SCHEMA_VERSION = "eventedge-telegram-archive-manifest-1.0"
MAX_ARCHIVE_BYTES = 2_000_000_000
MAX_ARCHIVE_ROWS = 1_000_000
MAX_PAGES = 50_000
MAX_RETRIES = 5
MAX_BATCH_MANIFESTS = 20
MAX_CHANNELS_PER_MANIFEST = 100
ARCHIVE_MINIMUM_INSTRUMENT_RELEVANCE = 0.78
TELEGRAM_CANDIDATE_ADMISSION_VERSION = "telegram-archive-admission-1.0"
TELEGRAM_INFORMATION_GROUP_POLICY_VERSION = "event-cluster-information-group-1.0"
DATA_POST_PATTERN = re.compile(
    r'data-post="(?P<channel>[A-Za-z0-9_]{3,64})/(?P<message_id>[1-9][0-9]{0,19})"'
)

FetchPage = Callable[[str, float], bytes]
Sleep = Callable[[float], None]
Clock = Callable[[], datetime]
Progress = Callable[[Mapping[str, object]], None]
Monotonic = Callable[[], float]


class TelegramArchiveRecord(BaseModel):
    """One text post observed on a consented public-channel archive page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["eventedge-telegram-archive-message-1.0"]
    source_id: Annotated[str, Field(pattern=r"^telegram_[a-z0-9_-]{2,63}$")]
    channel: Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{3,64}$")]
    external_id: Annotated[str, Field(min_length=5, max_length=100)]
    message_id: Annotated[int, Field(ge=1)]
    published_at: datetime
    retrieved_at: datetime
    title: Annotated[str, Field(min_length=1, max_length=500)]
    content: Annotated[str, Field(min_length=1, max_length=200_000)]
    url: Annotated[str, Field(pattern=r"^https://t\.me/[A-Za-z0-9_]+/[1-9][0-9]*$")]
    language: Literal["ru", "en"]
    edit_marker_present: bool
    causal_text_status: Literal[
        "no_edit_marker_current_view",
        "edited_current_view",
    ]
    snapshot_provenance: Literal["historical_public_page_current_view"]
    permission_asserted: Literal[True]
    consent_reference: Annotated[str, Field(min_length=1, max_length=500)]
    permission_verification: Literal["caller_assertion_not_independently_verified"]

    @model_validator(mode="after")
    def validate_identity_and_time(self) -> TelegramArchiveRecord:
        expected_external_id = f"{self.channel}/{self.message_id}"
        if self.external_id.casefold() != expected_external_id.casefold():
            raise ValueError("Telegram archive external ID does not match channel/message")
        expected_url = f"https://t.me/{self.external_id}"
        if self.url.casefold() != expected_url.casefold():
            raise ValueError("Telegram archive URL does not match external ID")
        if not _is_aware(self.published_at) or not _is_aware(self.retrieved_at):
            raise ValueError("Telegram archive timestamps must be timezone-aware")
        if self.retrieved_at < self.published_at:
            raise ValueError("Telegram archive retrieval precedes publication")
        expected_status = (
            "edited_current_view" if self.edit_marker_present else "no_edit_marker_current_view"
        )
        if self.causal_text_status != expected_status:
            raise ValueError("Telegram archive edit status is inconsistent")
        return self


@dataclass(frozen=True)
class TelegramArchiveOptions:
    """Safety and provenance bounds for one channel archive download."""

    since: datetime
    until: datetime
    consent_reference: str
    max_pages: int = 5_000
    max_messages: int = 100_000
    delay_seconds: float = 1.0
    timeout_seconds: float = 20.0
    max_retries: int = 3
    start_before_id: int | None = None

    def __post_init__(self) -> None:
        if not _is_aware(self.since) or not _is_aware(self.until):
            raise ValueError("archive time bounds must be timezone-aware")
        if self.since >= self.until:
            raise ValueError("archive since must be earlier than until")
        reference = self.consent_reference.strip()
        if not reference or len(reference) > 500 or any(ord(char) < 32 for char in reference):
            raise ValueError("consent reference must be 1-500 printable characters")
        if not 1 <= self.max_pages <= MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
        if not 1 <= self.max_messages <= MAX_ARCHIVE_ROWS:
            raise ValueError(f"max_messages must be between 1 and {MAX_ARCHIVE_ROWS}")
        if not 0.5 <= self.delay_seconds <= 60:
            raise ValueError("delay_seconds must be between 0.5 and 60")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        if not 0 <= self.max_retries <= MAX_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES}")
        if self.start_before_id is not None and self.start_before_id < 1:
            raise ValueError("start_before_id must be positive")


def load_verified_telegram_archive_manifests(
    manifest_paths: Sequence[Path],
    *,
    artifact_root: Path,
) -> tuple[list[Path], dict[str, object]]:
    """Resolve and verify completed downloader manifests and their JSONL files."""
    if not 1 <= len(manifest_paths) <= MAX_BATCH_MANIFESTS:
        raise ValueError(
            f"expected between 1 and {MAX_BATCH_MANIFESTS} Telegram manifests"
        )
    root = Path(artifact_root).resolve()
    if not root.is_dir():
        raise ValueError("Telegram artifact root must be an existing directory")
    archives: list[Path] = []
    seen_archives: set[Path] = set()
    manifest_summaries = []
    total_rows = 0
    for supplied_path in manifest_paths:
        manifest_path = _path_within_root(root, supplied_path, kind="manifest")
        manifest = read_json_object(manifest_path, maximum_bytes=10_000_000)
        channels = manifest.get("channels")
        consent_reference = manifest.get("consent_reference")
        if (
            manifest.get("schema_version")
            != "eventedge-telegram-archive-batch-manifest-1.0"
            or manifest.get("complete") is not True
            or manifest.get("permission_asserted") is not True
            or not isinstance(consent_reference, str)
            or not consent_reference.strip()
            or not isinstance(channels, list)
            or not 1 <= len(channels) <= MAX_CHANNELS_PER_MANIFEST
        ):
            raise ValueError("Telegram batch manifest is incomplete or incompatible")
        manifest_rows = 0
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("Telegram manifest channel entry must be an object")
            archive = _verified_manifest_channel(
                root,
                channel,
                consent_reference=consent_reference,
            )
            if archive in seen_archives:
                raise ValueError("Telegram manifests contain a duplicate archive path")
            seen_archives.add(archive)
            archives.append(archive)
            rows = int(channel["records_written"])
            manifest_rows += rows
            total_rows += rows
            if total_rows > MAX_ARCHIVE_ROWS:
                raise ValueError("Telegram manifests exceed the global archive row limit")
        manifest_summaries.append(
            {
                "path": str(manifest_path),
                "sha256": file_sha256(manifest_path),
                "channels": len(channels),
                "records": manifest_rows,
            }
        )
    if total_rows == 0:
        raise ValueError("Telegram manifests contain no archive rows")
    return archives, {
        "mode": "verified_batch_manifests",
        "verified": True,
        "manifests": manifest_summaries,
        "archives": len(archives),
        "records": total_rows,
    }


def _verified_manifest_channel(
    artifact_root: Path,
    channel: Mapping[str, object],
    *,
    consent_reference: str,
) -> Path:
    """Verify one immutable data file declared by a completed channel report."""
    data_path = channel.get("data_path")
    expected_sha256 = channel.get("data_sha256")
    expected_rows = channel.get("records_written")
    if (
        channel.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or channel.get("complete") is not True
        or not isinstance(data_path, str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256))
        or not isinstance(expected_rows, int)
        or isinstance(expected_rows, bool)
        or not 0 <= expected_rows <= MAX_ARCHIVE_ROWS
    ):
        raise ValueError("Telegram manifest channel is incomplete or incompatible")
    permission = channel.get("permission")
    if (
        not isinstance(permission, dict)
        or permission.get("asserted") is not True
        or permission.get("consent_reference") != consent_reference
    ):
        raise ValueError("Telegram manifest channel has no permission assertion")
    source_id = channel.get("source_id")
    channel_name = channel.get("channel")
    window = channel.get("window")
    if (
        not isinstance(source_id, str)
        or not isinstance(channel_name, str)
        or not isinstance(window, dict)
    ):
        raise ValueError("Telegram manifest channel identity is invalid")
    try:
        since = datetime.fromisoformat(str(window["since_inclusive"]))
        until = datetime.fromisoformat(str(window["until_exclusive"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Telegram manifest channel window is invalid") from error
    if not _is_aware(since) or not _is_aware(until) or since >= until:
        raise ValueError("Telegram manifest channel window is invalid")
    archive = _path_within_root(artifact_root, Path(data_path), kind="archive")
    if file_sha256(archive) != expected_sha256:
        raise ValueError("Telegram archive fingerprint differs from its manifest")
    actual_rows = 0
    for record in iter_telegram_archive_records(
        [archive],
        max_rows=max(1, expected_rows),
    ):
        actual_rows += 1
        if (
            record.source_id != source_id
            or record.channel.casefold() != channel_name.casefold()
            or not since <= record.published_at < until
            or record.consent_reference != consent_reference
        ):
            raise ValueError(
                "Telegram archive row differs from its manifest identity or window"
            )
    if actual_rows != expected_rows:
        raise ValueError("Telegram archive row count differs from its manifest")
    return archive


def _path_within_root(root: Path, supplied: Path, *, kind: str) -> Path:
    """Resolve a manifest-owned path without allowing artifact-root escape."""
    candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Telegram {kind} path escapes the artifact root or is missing")
    return resolved


@dataclass(frozen=True)
class TelegramArchivePage:
    """Parsed text items plus all message IDs needed for lossless pagination."""

    items: tuple[RssItem, ...]
    message_ids: tuple[int, ...]


def parse_telegram_archive_page(
    page: bytes,
    *,
    channel: str,
) -> TelegramArchivePage:
    """Parse one bounded public page and validate that it belongs to the channel."""
    items = tuple(parse_telegram_channel(page, max_items=1_000))
    observed = [
        (match.group("channel"), int(match.group("message_id")))
        for match in DATA_POST_PATTERN.finditer(page.decode("utf-8", errors="replace"))
    ]
    unexpected = {
        observed_channel
        for observed_channel, _ in observed
        if observed_channel.casefold() != channel.casefold()
    }
    if unexpected:
        raise ValueError(f"Telegram page contains unexpected channels: {unexpected!r}")
    message_ids = tuple(sorted({message_id for _, message_id in observed}))
    for item in items:
        item_channel, _, raw_message_id = item.external_id.rpartition("/")
        if item_channel.casefold() != channel.casefold() or not raw_message_id.isdigit():
            raise ValueError("parsed Telegram item does not match requested channel")
    return TelegramArchivePage(items=items, message_ids=message_ids)


def download_telegram_channel_archive(
    config: TelegramChannelConfig,
    output_path: Path,
    *,
    options: TelegramArchiveOptions,
    content_permission_confirmed: bool,
    resume: bool = False,
    fetcher: FetchPage = fetch_telegram_channel,
    sleeper: Sleep = time.sleep,
    clock: Clock = lambda: datetime.now(UTC),
    monotonic: Monotonic = time.monotonic,
    run_deadline_monotonic: float | None = None,
    progress: Progress | None = None,
) -> dict[str, object]:
    """Download one archive to JSONL with a crash-safe resumable checkpoint."""
    if not content_permission_confirmed:
        raise ValueError("explicit Telegram content permission confirmation is required")
    paths = _ArchivePaths.for_output(output_path)
    paths.output.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_run_lock(paths.lock):
        return _download_telegram_channel_archive_locked(
            config,
            paths,
            options=options,
            resume=resume,
            fetcher=fetcher,
            sleeper=sleeper,
            clock=clock,
            monotonic=monotonic,
            run_deadline_monotonic=run_deadline_monotonic,
            progress=progress,
        )


def _download_telegram_channel_archive_locked(
    config: TelegramChannelConfig,
    paths: _ArchivePaths,
    *,
    options: TelegramArchiveOptions,
    resume: bool,
    fetcher: FetchPage,
    sleeper: Sleep,
    clock: Clock,
    monotonic: Monotonic,
    run_deadline_monotonic: float | None,
    progress: Progress | None,
) -> dict[str, object]:
    """Download an archive while holding its output-specific writer lock."""
    state = _prepare_download(paths, config=config, options=options, resume=resume)
    seen_ids = _validate_partial_archive(paths.partial, state=state)
    completion_reason: str | None = None

    while state["pages_fetched"] < options.max_pages:
        page_url = _archive_page_url(config.channel, state["next_before_id"])
        page_bytes = _fetch_with_retries(
            page_url,
            timeout_seconds=options.timeout_seconds,
            max_retries=options.max_retries,
            delay_seconds=options.delay_seconds,
            fetcher=fetcher,
            sleeper=sleeper,
            monotonic=monotonic,
            run_deadline_monotonic=run_deadline_monotonic,
        )
        page = parse_telegram_archive_page(page_bytes, channel=config.channel)
        if not page.message_ids:
            raise RuntimeError(f"Telegram archive returned no message IDs: {page_url}")
        completion_reason = _append_archive_page(
            paths,
            state=state,
            page=page,
            config=config,
            options=options,
            seen_ids=seen_ids,
            retrieved_at=clock(),
        )
        _write_json_atomic(paths.checkpoint, state)
        if progress is not None:
            progress(_progress_payload(config, state))
        if completion_reason is not None:
            break
        sleeper(
            _remaining_delay(
                options.delay_seconds,
                monotonic=monotonic,
                run_deadline_monotonic=run_deadline_monotonic,
            )
        )

    if completion_reason is None:
        completion_reason = "max_pages_reached"
    complete = completion_reason in {"since_reached", "archive_start_reached"}
    return _finalize_download(
        paths,
        state=state,
        config=config,
        options=options,
        complete=complete,
        completion_reason=completion_reason,
    )


def iter_telegram_archive_records(
    paths: Sequence[Path],
    *,
    max_rows: int = MAX_ARCHIVE_ROWS,
) -> Iterator[TelegramArchiveRecord]:
    """Yield strictly validated records from bounded completed JSONL archives."""
    if not paths:
        raise ValueError("at least one Telegram archive path is required")
    if not 1 <= max_rows <= MAX_ARCHIVE_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_ARCHIVE_ROWS}")
    row_count = 0
    for path in paths:
        remaining_rows = max_rows - row_count
        try:
            rows = iter_jsonl_objects(
                path,
                maximum_rows=remaining_rows,
                maximum_file_bytes=MAX_ARCHIVE_BYTES,
            )
            for line_number, payload in enumerate(rows, 1):
                row_count += 1
                try:
                    yield TelegramArchiveRecord.model_validate(payload)
                except Exception as error:
                    raise ValueError(
                        f"invalid Telegram archive row {path}:{line_number}"
                    ) from error
        except ValueError as error:
            if "row bound" in str(error):
                raise ValueError(
                    f"Telegram archive exceeds the {max_rows}-row limit"
                ) from error
            raise


def build_telegram_archive_candidates(
    records: Iterable[TelegramArchiveRecord],
    *,
    assumed_receipt_lag_minutes: int = 5,
    max_rows: int = MAX_ARCHIVE_ROWS,
) -> tuple[list[SignalEventCandidateV2], dict[str, object]]:
    """Build exploratory candidates from consented posts without edit markers."""
    if not 1 <= assumed_receipt_lag_minutes <= 60:
        raise ValueError("assumed receipt lag must be between 1 and 60 minutes")
    if not 1 <= max_rows <= MAX_ARCHIVE_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_ARCHIVE_ROWS}")
    snapshots, import_report = _archive_snapshots(
        records,
        assumed_receipt_lag_minutes=assumed_receipt_lag_minutes,
        max_rows=max_rows,
    )
    legacy_candidates, report = build_signal_candidates_from_news_snapshots(
        snapshots,
        candidate_notes=(
            "Research-only consented Telegram public-page archive. Posts carrying "
            "an edit marker were excluded; remaining text is a historical current "
            "view without a cryptographic first-snapshot guarantee. received_at and "
            f"decision_at assume published_at + {assumed_receipt_lag_minutes}m."
        ),
        max_rows=max_rows,
        include_telegram_with_permission=True,
        minimum_instrument_relevance=ARCHIVE_MINIMUM_INSTRUMENT_RELEVANCE,
    )
    snapshots_by_id = {snapshot.news_id: snapshot for snapshot in snapshots}
    candidates = [
        _telegram_candidate_v2(
            candidate,
            snapshots_by_id=snapshots_by_id,
            assumed_receipt_lag_minutes=assumed_receipt_lag_minutes,
        )
        for candidate in legacy_candidates
    ]
    return candidates, {
        **report,
        "schema_version": "telegram-archive-candidate-build-2.0",
        "candidate_schema_version": "signal-event-candidate-2.0",
        "admission_version": TELEGRAM_CANDIDATE_ADMISSION_VERSION,
        "information_group_policy": {
            "version": TELEGRAM_INFORMATION_GROUP_POLICY_VERSION,
            "field": "event_group_id",
            "basis": "deterministic_event_cluster_id",
            "conservative_initial_group": True,
        },
        "analysis_payload_policy": {
            "version": "telegram-analysis-payload-1.0",
            "title": "exact_archive_title",
            "content": "archive_content_prefix",
            "maximum_content_characters": MAX_CANDIDATE_CONTENT_CHARS,
        },
        "research_only": True,
        "permission_asserted": True,
        "received_at_provenance": "published_at_plus_assumed_lag",
        "assumed_receipt_lag_minutes": assumed_receipt_lag_minutes,
        "text_policy": "exclude_posts_with_edit_marker",
        "first_snapshot_guaranteed": False,
        "import": import_report,
    }


def _telegram_candidate_v2(
    candidate: SignalEventCandidate,
    *,
    snapshots_by_id: Mapping[str, SignalNewsSnapshot],
    assumed_receipt_lag_minutes: int,
) -> SignalEventCandidateV2:
    """Bind a clustered candidate to the exact Telegram analysis input."""
    primary = snapshots_by_id.get(candidate.primary_news_id)
    if primary is None:
        raise ValueError("Telegram candidate primary news snapshot is missing")
    return SignalEventCandidateV2.model_validate(
        {
            **candidate.model_dump(mode="python"),
            "schema_version": "signal-event-candidate-2.0",
            "corporate_action_status": "unknown",
            "event_group_id": candidate.event_id,
            "source_url": primary.url,
            "retrieved_at": primary.created_at,
            "admission_version": TELEGRAM_CANDIDATE_ADMISSION_VERSION,
            "analysis_title": primary.title,
            "analysis_content_sha256": hashlib.sha256(
                primary.content.encode("utf-8")
            ).hexdigest(),
            "receipt_provenance": (
                f"publication_plus_assumed_{assumed_receipt_lag_minutes}_minutes"
            ),
            "text_provenance": (
                "no_edit_marker_current_view_not_first_snapshot"
            ),
        }
    )


@dataclass(frozen=True)
class _ArchivePaths:
    output: Path
    partial: Path
    checkpoint: Path
    lock: Path

    @classmethod
    def for_output(cls, output: Path) -> _ArchivePaths:
        return cls(
            output=output,
            partial=output.with_suffix(f"{output.suffix}.partial"),
            checkpoint=output.with_suffix(f"{output.suffix}.checkpoint.json"),
            lock=output.with_suffix(f"{output.suffix}.lock"),
        )


def _prepare_download(
    paths: _ArchivePaths,
    *,
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
    resume: bool,
) -> dict[str, object]:
    if paths.output.exists():
        raise FileExistsError(f"refusing to overwrite Telegram archive: {paths.output}")
    paths.output.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = _request_fingerprint(config, options)
    if resume and (paths.partial.exists() or paths.checkpoint.exists()):
        if not paths.partial.is_file() or not paths.checkpoint.is_file():
            raise ValueError("resume requires both partial archive and checkpoint")
        state = _read_checkpoint(paths.checkpoint)
        if state.get("request_fingerprint") != fingerprint:
            raise ValueError("Telegram archive checkpoint does not match this request")
        safe_bytes = _state_int(state, "safe_bytes", minimum=0)
        with paths.partial.open("r+b") as partial:
            if safe_bytes > partial.seek(0, os.SEEK_END):
                raise ValueError("Telegram archive checkpoint exceeds partial file")
            partial.truncate(safe_bytes)
        return state
    if paths.partial.exists() or paths.checkpoint.exists():
        raise FileExistsError("partial Telegram archive exists; pass --resume")
    paths.partial.touch(mode=0o600)
    state: dict[str, object] = {
        "schema_version": "eventedge-telegram-archive-checkpoint-1.0",
        "request_fingerprint": fingerprint,
        "pages_fetched": 0,
        "next_before_id": options.start_before_id,
        "safe_bytes": 0,
        "records_written": 0,
        "edited_records": 0,
        "non_text_messages_seen": 0,
        "duplicates_skipped": 0,
        "after_until_skipped": 0,
        "before_since_skipped": 0,
        "minimum_published_at": None,
        "maximum_published_at": None,
    }
    _write_json_atomic(paths.checkpoint, state)
    return state


def _validate_partial_archive(path: Path, *, state: Mapping[str, object]) -> set[str]:
    safe_bytes = _state_int(state, "safe_bytes", minimum=0)
    if path.stat().st_size != safe_bytes:
        raise ValueError("Telegram partial archive does not match safe checkpoint size")
    expected_rows = _state_int(state, "records_written", minimum=0)
    seen: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                record = TelegramArchiveRecord.model_validate_json(line)
            except Exception as error:
                raise ValueError(f"invalid Telegram partial archive row {line_number}") from error
            if record.external_id.casefold() in seen:
                raise ValueError("Telegram partial archive contains duplicate IDs")
            seen.add(record.external_id.casefold())
    if len(seen) != expected_rows:
        raise ValueError("Telegram partial archive row count does not match checkpoint")
    return seen


def _fetch_with_retries(
    url: str,
    *,
    timeout_seconds: float,
    max_retries: int,
    delay_seconds: float,
    fetcher: FetchPage,
    sleeper: Sleep,
    monotonic: Monotonic,
    run_deadline_monotonic: float | None,
) -> bytes:
    for attempt in range(max_retries + 1):
        request_timeout = _remaining_request_timeout(
            timeout_seconds,
            monotonic=monotonic,
            run_deadline_monotonic=run_deadline_monotonic,
        )
        try:
            return fetcher(url, request_timeout)
        except Exception:
            if attempt == max_retries:
                raise
            sleeper(
                _remaining_delay(
                    min(delay_seconds * (2**attempt), 30.0),
                    monotonic=monotonic,
                    run_deadline_monotonic=run_deadline_monotonic,
                )
            )
    raise AssertionError("unreachable retry loop")


def _remaining_request_timeout(
    configured_seconds: float,
    *,
    monotonic: Monotonic,
    run_deadline_monotonic: float | None,
) -> float:
    if run_deadline_monotonic is None:
        return configured_seconds
    remaining = run_deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("Telegram archive run reached its wall-clock limit")
    return min(configured_seconds, remaining)


def _remaining_delay(
    configured_seconds: float,
    *,
    monotonic: Monotonic,
    run_deadline_monotonic: float | None,
) -> float:
    if run_deadline_monotonic is None:
        return configured_seconds
    remaining = run_deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("Telegram archive run reached its wall-clock limit")
    return min(configured_seconds, remaining)


def _append_archive_page(
    paths: _ArchivePaths,
    *,
    state: dict[str, object],
    page: TelegramArchivePage,
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
    seen_ids: set[str],
    retrieved_at: datetime,
) -> str | None:
    if not _is_aware(retrieved_at):
        raise ValueError("archive retrieval clock must return an aware datetime")
    before_id = state.get("next_before_id")
    next_before_id = min(page.message_ids)
    if isinstance(before_id, int) and next_before_id >= before_id:
        raise RuntimeError("Telegram archive pagination made no backward progress")
    records = _page_records(
        page.items,
        config=config,
        options=options,
        retrieved_at=retrieved_at,
        seen_ids=seen_ids,
        state=state,
    )
    remaining = options.max_messages - _state_int(
        state,
        "records_written",
        minimum=0,
    )
    selected = records[:remaining]
    _append_records(paths.partial, selected)
    for record in selected:
        seen_ids.add(record.external_id.casefold())
        _update_time_range(state, record.published_at)
    state["records_written"] = _state_int(state, "records_written", minimum=0) + len(selected)
    state["edited_records"] = _state_int(state, "edited_records", minimum=0) + sum(
        record.edit_marker_present for record in selected
    )
    state["non_text_messages_seen"] = _state_int(state, "non_text_messages_seen", minimum=0) + max(
        0, len(page.message_ids) - len(page.items)
    )
    state["pages_fetched"] = _state_int(state, "pages_fetched", minimum=0) + 1
    state["next_before_id"] = next_before_id
    state["safe_bytes"] = paths.partial.stat().st_size

    if len(records) > remaining or state["records_written"] >= options.max_messages:
        return "max_messages_reached"
    oldest = min((item.published_at for item in page.items), default=None)
    if oldest is not None and oldest <= options.since:
        return "since_reached"
    if next_before_id <= 1:
        return "archive_start_reached"
    return None


def _page_records(
    items: Sequence[RssItem],
    *,
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
    retrieved_at: datetime,
    seen_ids: set[str],
    state: dict[str, object],
) -> list[TelegramArchiveRecord]:
    records: list[TelegramArchiveRecord] = []
    for item in items:
        identity = item.external_id.casefold()
        if identity in seen_ids:
            state["duplicates_skipped"] = _state_int(state, "duplicates_skipped", minimum=0) + 1
            continue
        if item.published_at < options.since:
            state["before_since_skipped"] = _state_int(state, "before_since_skipped", minimum=0) + 1
            continue
        if item.published_at >= options.until:
            state["after_until_skipped"] = _state_int(state, "after_until_skipped", minimum=0) + 1
            continue
        _, _, raw_message_id = item.external_id.rpartition("/")
        records.append(
            TelegramArchiveRecord(
                schema_version=ARCHIVE_SCHEMA_VERSION,
                source_id=config.source_id,
                channel=config.channel,
                external_id=item.external_id,
                message_id=int(raw_message_id),
                published_at=item.published_at,
                retrieved_at=retrieved_at,
                title=item.title,
                content=item.content,
                url=item.url,
                language=config.language,
                edit_marker_present=item.is_edited,
                causal_text_status=(
                    "edited_current_view" if item.is_edited else "no_edit_marker_current_view"
                ),
                snapshot_provenance="historical_public_page_current_view",
                permission_asserted=True,
                consent_reference=options.consent_reference.strip(),
                permission_verification="caller_assertion_not_independently_verified",
            )
        )
    return records


def _append_records(path: Path, records: Sequence[TelegramArchiveRecord]) -> None:
    if not records:
        return
    payload = b"".join(
        (
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        for record in records
    )
    with path.open("ab") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _finalize_download(
    paths: _ArchivePaths,
    *,
    state: Mapping[str, object],
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
    complete: bool,
    completion_reason: str,
) -> dict[str, object]:
    data_path = paths.partial
    if complete:
        publish_immutable_file(paths.partial, paths.output)
        paths.partial.unlink()
        paths.checkpoint.unlink(missing_ok=True)
        data_path = paths.output
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "complete": complete,
        "completion_reason": completion_reason,
        "source_id": config.source_id,
        "channel": config.channel,
        "source_url": config.url,
        "permission": {
            "asserted": True,
            "consent_reference": options.consent_reference.strip(),
            "verification": "caller_assertion_not_independently_verified",
        },
        "window": {
            "since_inclusive": options.since.isoformat(),
            "until_exclusive": options.until.isoformat(),
        },
        "start_before_id": options.start_before_id,
        "snapshot_provenance": "historical_public_page_current_view",
        "first_snapshot_guaranteed": False,
        "pages_fetched": state["pages_fetched"],
        "records_written": state["records_written"],
        "edited_records": state["edited_records"],
        "non_text_messages_seen": state["non_text_messages_seen"],
        "duplicates_skipped": state["duplicates_skipped"],
        "after_until_skipped": state["after_until_skipped"],
        "before_since_skipped": state["before_since_skipped"],
        "minimum_published_at": state["minimum_published_at"],
        "maximum_published_at": state["maximum_published_at"],
        "data_path": str(data_path),
        "data_sha256": _file_sha256(data_path),
    }


def _archive_snapshots(
    records: Iterable[TelegramArchiveRecord],
    *,
    assumed_receipt_lag_minutes: int,
    max_rows: int,
) -> tuple[list[SignalNewsSnapshot], dict[str, object]]:
    snapshots: list[SignalNewsSnapshot] = []
    skipped: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    permission_references: set[str] = set()
    seen_ids: set[str] = set()
    extractor = RuleBasedNewsExtractor()
    row_count = 0
    for row_count, record in enumerate(records, 1):
        if row_count > max_rows:
            raise ValueError(f"Telegram archive exceeds the {max_rows}-row limit")
        identity = record.external_id.casefold()
        if identity in seen_ids:
            skipped["duplicate_external_id"] += 1
            continue
        seen_ids.add(identity)
        permission_references.add(record.consent_reference)
        if record.edit_marker_present:
            skipped["edited_current_view"] += 1
            continue
        analysis_content = record.content[:MAX_CANDIDATE_CONTENT_CHARS]
        features = extractor.extract(
            NewsAnalysisInput(
                source_id=record.source_id,
                title=record.title,
                content=analysis_content,
                language=record.language,
            )
        )
        tickers = sorted(
            {
                instrument.ticker
                for instrument in features.instruments
                if instrument.relevance >= ARCHIVE_MINIMUM_INSTRUMENT_RELEVANCE
                and instrument.ticker in DEFAULT_MOEX_ALIASES
                and instrument.ticker != "MOEX"
            }
        )
        received_at = record.published_at + timedelta(minutes=assumed_receipt_lag_minutes)
        snapshots.append(
            SignalNewsSnapshot(
                news_id=_archive_news_id(record.external_id),
                source_id=record.source_id,
                external_id=record.external_id,
                published_at=record.published_at,
                received_at=received_at,
                title=record.title,
                url=record.url,
                content=analysis_content,
                language=record.language,
                source_metadata={
                    "collector": "telegram_public_archive_with_permission",
                    "channel": record.channel,
                    "tickers": tickers,
                    "categories": [],
                    "snapshot_provenance": record.snapshot_provenance,
                    "causal_text_status": record.causal_text_status,
                    "permission_asserted": True,
                    "consent_reference": record.consent_reference,
                },
                created_at=record.retrieved_at,
            )
        )
        source_counts[record.source_id] += 1
    return snapshots, {
        "input_rows": row_count,
        "snapshot_rows": len(snapshots),
        "source_rows": dict(source_counts.most_common()),
        "permission_references": sorted(permission_references),
        "skipped_rows": dict(skipped.most_common()),
    }


def _request_fingerprint(
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
) -> str:
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_id": config.source_id,
        "channel": config.channel,
        "language": config.language,
        "since": options.since.isoformat(),
        "until": options.until.isoformat(),
        "consent_reference": options.consent_reference.strip(),
        "start_before_id": options.start_before_id,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _archive_page_url(channel: str, before_id: object) -> str:
    base = f"https://t.me/s/{channel}"
    return base if before_id is None else f"{base}?before={int(before_id)}"


def _read_checkpoint(path: Path) -> dict[str, object]:
    if path.stat().st_size > 1_000_000:
        raise ValueError("Telegram archive checkpoint is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Telegram archive checkpoint is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("Telegram archive checkpoint must be an object")
    if payload.get("schema_version") != "eventedge-telegram-archive-checkpoint-1.0":
        raise ValueError("unsupported Telegram archive checkpoint")
    _state_int(payload, "pages_fetched", minimum=0)
    _state_int(payload, "records_written", minimum=0)
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
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
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        temporary_path.replace(path)
        path.chmod(0o600)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _state_int(state: Mapping[str, object], key: str, *, minimum: int) -> int:
    value = state.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid Telegram archive checkpoint field: {key}")
    return value


def _update_time_range(state: dict[str, object], published_at: datetime) -> None:
    value = published_at.isoformat()
    minimum = state.get("minimum_published_at")
    maximum = state.get("maximum_published_at")
    if minimum is None or value < minimum:
        state["minimum_published_at"] = value
    if maximum is None or value > maximum:
        state["maximum_published_at"] = value


def _progress_payload(
    config: TelegramChannelConfig,
    state: Mapping[str, object],
) -> dict[str, object]:
    return {
        "source_id": config.source_id,
        "channel": config.channel,
        "pages_fetched": state["pages_fetched"],
        "records_written": state["records_written"],
        "next_before_id": state["next_before_id"],
        "minimum_published_at": state["minimum_published_at"],
    }


def _archive_news_id(external_id: str) -> str:
    digest = hashlib.sha256(f"telegram-archive-1.0\0{external_id}".encode()).hexdigest()
    return f"tg_{digest[:32]}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
