from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from eventedge.configs.sources import TelegramChannelConfig, load_source_config
from eventedge_research.research_artifacts import exclusive_run_lock, write_json
from eventedge_research.telegram_archive import (
    TelegramArchiveOptions,
    download_telegram_channel_archive,
    iter_telegram_archive_records,
)

_BATCH_LOCK_FILENAME = ".telegram-archive-batch.lock"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download bounded public Telegram archives only after explicit content "
            "permission has been obtained"
        )
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--all-configured-channels",
        action="store_true",
        help="Download every Telegram channel in configs/sources.yaml",
    )
    selection.add_argument(
        "--channel",
        action="append",
        help="Configured source_id or channel name; repeat for multiple channels",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--since",
        type=_aware_datetime,
        required=True,
        help="Inclusive ISO-8601 instant; YYYY-MM-DD means 00:00 UTC",
    )
    parser.add_argument(
        "--until",
        type=_aware_datetime,
        required=True,
        help="Exclusive ISO-8601 instant; YYYY-MM-DD means 00:00 UTC",
    )
    parser.add_argument(
        "--confirm-content-permission",
        action="store_true",
        help="Assert that channel owners/authors authorized this exact ML use",
    )
    parser.add_argument(
        "--consent-reference",
        required=True,
        help="Non-secret document/ticket/reference identifying the obtained consent",
    )
    parser.add_argument("--max-pages-per-channel", type=int, default=5_000)
    parser.add_argument("--max-messages-per-channel", type=int, default=100_000)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-run-seconds",
        type=_bounded_run_seconds,
        default=3_600.0,
        help=(
            "Hard wall-clock budget for this invocation; checkpoints make a "
            "timed-out run safe to resume (60-21600 seconds)"
        ),
    )
    parser.add_argument(
        "--backfill-from-dir",
        type=Path,
        help=(
            "Start each channel immediately before the oldest message in a "
            "validated adjacent completed archive"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from crash-safe per-channel checkpoints",
    )
    args = parser.parse_args()

    if not args.confirm_content_permission:
        parser.error("--confirm-content-permission is required")
    channels = _selected_channels(
        load_source_config().telegram_channels,
        all_configured=args.all_configured_channels,
        requested=args.channel or [],
    )
    options = TelegramArchiveOptions(
        since=args.since,
        until=args.until,
        consent_reference=args.consent_reference,
        max_pages=args.max_pages_per_channel,
        max_messages=args.max_messages_per_channel,
        delay_seconds=args.delay_seconds,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )
    manifest = _download_batch(
        args.output_dir,
        channels=channels,
        options=options,
        backfill_from_dir=args.backfill_from_dir,
        resume=args.resume,
        max_run_seconds=args.max_run_seconds,
    )
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    print(serialized)
    if not manifest["complete"]:
        raise SystemExit(2)


def _download_batch(
    output_dir: Path,
    *,
    channels: tuple[TelegramChannelConfig, ...],
    options: TelegramArchiveOptions,
    backfill_from_dir: Path | None,
    resume: bool,
    max_run_seconds: float,
) -> dict[str, object]:
    """Download one bounded channel batch under an output-directory lock."""
    lock_path = output_dir / _BATCH_LOCK_FILENAME
    with exclusive_run_lock(lock_path):
        return _download_batch_locked(
            output_dir,
            channels=channels,
            options=options,
            backfill_from_dir=backfill_from_dir,
            resume=resume,
            max_run_seconds=max_run_seconds,
        )


def _download_batch_locked(
    output_dir: Path,
    *,
    channels: tuple[TelegramChannelConfig, ...],
    options: TelegramArchiveOptions,
    backfill_from_dir: Path | None,
    resume: bool,
    max_run_seconds: float,
) -> dict[str, object]:
    """Download a channel batch while its output-directory lock is held."""
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not resume:
        raise FileExistsError(f"refusing to overwrite Telegram manifest: {manifest_path}")
    run_deadline_monotonic = time.monotonic() + max_run_seconds
    reports = []
    for config in channels:
        channel_options = options
        if backfill_from_dir is not None:
            channel_options = replace(
                options,
                start_before_id=_backfill_start_before_id(
                    backfill_from_dir,
                    config=config,
                    adjacent_until=options.until,
                ),
            )
        output_path = output_dir / f"{config.source_id}.jsonl"
        report_path = output_dir / f"{config.source_id}.report.json"
        if resume and output_path.is_file() and report_path.is_file():
            report = _load_completed_report(
                report_path,
                output_path=output_path,
                config=config,
                options=channel_options,
            )
        else:
            report = download_telegram_channel_archive(
                config,
                output_path,
                options=channel_options,
                content_permission_confirmed=True,
                resume=resume,
                run_deadline_monotonic=run_deadline_monotonic,
                progress=_print_progress,
            )
            _publish_batch_json(report_path, report, resume=resume)
        reports.append(report)
    manifest = {
        "schema_version": "eventedge-telegram-archive-batch-manifest-1.0",
        "complete": all(report["complete"] is True for report in reports),
        "permission_asserted": True,
        "consent_reference": options.consent_reference.strip(),
        "backfill_from_dir": (
            str(backfill_from_dir) if backfill_from_dir is not None else None
        ),
        "runtime_safety": {
            "max_run_seconds": max_run_seconds,
            "network_timeout_seconds": options.timeout_seconds,
            "request_delay_seconds": options.delay_seconds,
            "max_retries": options.max_retries,
        },
        "channels": reports,
    }
    _publish_batch_json(manifest_path, manifest, resume=resume)
    return manifest


def _selected_channels(
    configured: tuple[TelegramChannelConfig, ...],
    *,
    all_configured: bool,
    requested: list[str],
) -> tuple[TelegramChannelConfig, ...]:
    if all_configured:
        return configured
    by_name = {
        key.casefold(): config
        for config in configured
        for key in (config.source_id, config.channel, f"@{config.channel}")
    }
    selected = []
    unknown = []
    for value in requested:
        config = by_name.get(value.casefold())
        if config is None:
            unknown.append(value)
        elif config not in selected:
            selected.append(config)
    if unknown:
        raise ValueError(f"unknown configured Telegram channels: {unknown!r}")
    if not selected:
        raise ValueError("at least one Telegram channel must be selected")
    return tuple(selected)


def _aware_datetime(value: str) -> datetime:
    candidate = f"{value}T00:00:00+00:00" if len(value) == 10 else value
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _bounded_run_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a number of seconds") from error
    if not 60 <= seconds <= 21_600:
        raise argparse.ArgumentTypeError("run time must be between 60 and 21600 seconds")
    return seconds


def _print_progress(progress: dict[str, object]) -> None:
    pages = progress["pages_fetched"]
    if pages == 1 or (isinstance(pages, int) and pages % 25 == 0):
        print(json.dumps(progress, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _publish_batch_json(
    path: Path,
    payload: dict[str, object],
    *,
    resume: bool,
) -> None:
    """Publish immutable initial JSON or replace mutable resume state.

    The resume branch is called only by ``_download_batch_locked`` while the
    output-directory batch lock is held. Initial publication uses the common
    no-clobber artifact writer.
    """
    if not resume or not path.exists():
        write_json(path, payload)
        return
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
            json.dump(
                payload,
                output,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
        path.chmod(0o600)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _load_completed_report(
    report_path: Path,
    *,
    output_path: Path,
    config: TelegramChannelConfig,
    options: TelegramArchiveOptions,
) -> dict[str, object]:
    if report_path.stat().st_size > 1_000_000:
        raise ValueError(f"Telegram channel report is too large: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Telegram channel report: {report_path}") from error
    if not isinstance(report, dict) or report.get("complete") is not True:
        raise ValueError(f"Telegram channel report is not complete: {report_path}")
    expected = {
        "source_id": config.source_id,
        "channel": config.channel,
        "data_path": str(output_path),
        "data_sha256": _file_sha256(output_path),
    }
    actual = {key: report.get(key) for key in expected}
    if actual != expected:
        raise ValueError(f"completed Telegram channel report mismatch: {report_path}")
    if report.get("window") != {
        "since_inclusive": options.since.isoformat(),
        "until_exclusive": options.until.isoformat(),
    }:
        raise ValueError(f"completed Telegram channel window mismatch: {report_path}")
    if report.get("start_before_id") != options.start_before_id:
        raise ValueError(f"completed Telegram start cursor mismatch: {report_path}")
    permission = report.get("permission")
    if (
        not isinstance(permission, dict)
        or permission.get("consent_reference") != options.consent_reference.strip()
    ):
        raise ValueError(f"completed Telegram consent reference mismatch: {report_path}")
    return report


def _backfill_start_before_id(
    adjacent_dir: Path,
    *,
    config: TelegramChannelConfig,
    adjacent_until: datetime,
) -> int:
    data_path = adjacent_dir / f"{config.source_id}.jsonl"
    report_path = adjacent_dir / f"{config.source_id}.report.json"
    if not data_path.is_file() or not report_path.is_file():
        raise ValueError(f"adjacent Telegram archive is incomplete: {config.source_id}")
    if report_path.stat().st_size > 1_000_000:
        raise ValueError(f"adjacent Telegram report is too large: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid adjacent Telegram report: {report_path}") from error
    if not isinstance(report, dict):
        raise ValueError(f"invalid adjacent Telegram report: {report_path}")
    expected_identity = {
        "complete": True,
        "completion_reason": "since_reached",
        "source_id": config.source_id,
        "channel": config.channel,
        "data_sha256": _file_sha256(data_path),
    }
    actual_identity = {key: report.get(key) for key in expected_identity}
    if actual_identity != expected_identity:
        raise ValueError(f"adjacent Telegram archive identity mismatch: {report_path}")
    window = report.get("window")
    if not isinstance(window, dict) or window.get("since_inclusive") != adjacent_until.isoformat():
        raise ValueError("backfill window must end exactly where adjacent archive starts")

    minimum_id: int | None = None
    for record in iter_telegram_archive_records([data_path]):
        if record.source_id != config.source_id or record.channel != config.channel:
            raise ValueError(f"adjacent Telegram row identity mismatch: {data_path}")
        minimum_id = record.message_id if minimum_id is None else min(minimum_id, record.message_id)
    if minimum_id is None:
        raise ValueError(f"adjacent Telegram archive contains no text posts: {data_path}")
    return minimum_id


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
