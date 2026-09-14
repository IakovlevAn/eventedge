"""Explicit historical provenance and information-update aware research data contracts."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    SignalDatasetSplit,
    _flatten,
    _partition_name,
    _validate_split_boundaries,
)


class SignalDatasetExampleV2(SignalDatasetExample):
    """Versioned label row; price-only outputs do not resolve corporate-action uncertainty."""

    schema_version: Literal["signal-dataset-example-2.0"] = "signal-dataset-example-2.0"
    corporate_action_status: Literal["unknown"] = "unknown"
    event_group_id: str = Field(min_length=1, max_length=200)
    source_url: str = Field(pattern=r"^https://t\.me/")
    retrieved_at: datetime
    admission_version: str
    analysis_title: str = Field(min_length=1, max_length=500)
    analysis_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_provenance: Annotated[
        str,
        Field(pattern=r"^publication_plus_assumed_([1-9]|[1-5][0-9]|60)_minutes$"),
    ]
    text_provenance: Literal["no_edit_marker_current_view_not_first_snapshot"]

    @model_validator(mode="after")
    def validate_research_provenance(self) -> SignalDatasetExampleV2:
        match = re.fullmatch(
            r"publication_plus_assumed_([1-9]|[1-5][0-9]|60)_minutes",
            self.receipt_provenance,
        )
        if match is None:
            raise ValueError("invalid v2 receipt provenance")
        assumed_lag = timedelta(minutes=int(match.group(1)))
        if self.received_at != self.published_at + assumed_lag:
            raise ValueError("v2 receipt must match its explicit assumption")
        if self.decision_at != self.received_at:
            raise ValueError("v2 decision must equal assumed receipt")
        if self.retrieved_at.tzinfo is None or self.retrieved_at < self.published_at:
            raise ValueError("invalid archive retrieval provenance")
        return self


def split_information_updates(
    examples: list[SignalDatasetExampleV2],
    *,
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta = timedelta(hours=72),
) -> SignalDatasetSplit:
    """Purge whole issuer-time groups while preserving individual update IDs.

    The same embargo also excludes early shadow decisions; related same-issuer
    updates can therefore not straddle an evaluation boundary less than 72h apart.
    """
    _validate_split_boundaries(validation_from, test_from, test_until, embargo)
    grouped = defaultdict(list)
    for row in examples:
        grouped[row.event_group_id].append(row)
    partitions = defaultdict(list)
    for rows in grouped.values():
        name = _partition_name(
            rows,
            validation_from=validation_from,
            test_from=test_from,
            test_until=test_until,
            embargo=embargo,
        )
        if name == "future" and min(row.decision_at for row in rows) < test_until + embargo:
            name = "purged"
        partitions[name].append(rows)
    result = SignalDatasetSplit(
        **{
            name: _flatten(partitions[name])
            for name in ("train", "validation", "test", "purged", "future")
        }
    )
    if not result.train or not result.validation or not result.test:
        raise ValueError("v2 temporal split produced an empty development partition")
    return result
