from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eventedge.collectors import RssItem, is_market_signal_candidate

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retro_signal_audit.json"
AUDIT_CASES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("title", "content", "expected", "reason"),
    [
        (
            case["title"],
            case["content"],
            case["expected_candidate"],
            case["reason"],
        )
        for case in AUDIT_CASES
    ],
    ids=[f"retro-{index + 1}" for index in range(len(AUDIT_CASES))],
)
def test_codex_reviewed_retro_signal_gate(
    title: str,
    content: str,
    expected: bool,
    reason: str,
) -> None:
    item = RssItem(
        external_id="retro-audit",
        published_at=datetime(2026, 8, 6, 10, tzinfo=UTC),
        title=title,
        url="https://example.com/retro-audit",
        content=content,
        categories=(),
    )

    assert is_market_signal_candidate(item) is expected, reason
