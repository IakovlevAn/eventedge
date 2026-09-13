from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests

from eventedge_research import research_artifacts
from eventedge_research.moex_market_data import MoexCandleSource, download_moex_market_opens
from eventedge_research.signal_dataset_builder import (
    SignalEventCandidate,
    load_market_open_observations,
)
from scripts.download_moex_market_opens import _candidate_tickers


class _FakeResponse:
    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[str, dict[str, object], tuple[float, float]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: tuple[float, float],
    ) -> _FakeResponse:
        self.calls.append((url, dict(params), timeout))
        return next(self._responses)


def test_candidate_tickers_are_limited_to_download_window() -> None:
    candidates = [
        SignalEventCandidate.model_construct(
            ticker="SBER",
            decision_at=datetime(2021, 5, 1, tzinfo=UTC),
        ),
        SignalEventCandidate.model_construct(
            ticker="GAZP",
            decision_at=datetime(2022, 5, 1, tzinfo=UTC),
        ),
    ]

    assert _candidate_tickers(
        candidates,
        from_date=date(2022, 1, 1),
        till_date=date(2022, 12, 31),
    ) == {"GAZP"}
    with pytest.raises(ValueError, match="no candidate tickers"):
        _candidate_tickers(
            candidates,
            from_date=date(2023, 1, 1),
            till_date=date(2023, 12, 31),
        )


def test_download_market_opens_converts_moscow_time(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["open", "begin"],
                        "data": [
                            [100.0, "2026-07-22 10:00:00"],
                            [101.0, "2026-07-22 10:10:00"],
                        ],
                    }
                }
            ),
        ]
    )
    output = tmp_path / "opens.jsonl"

    report = download_moex_market_opens(
        tickers={"sber"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=output,
        session=session,
        request_delay_seconds=0,
        sleep=lambda _: None,
    )

    observations = load_market_open_observations(output)
    assert [item.at for item in observations] == [
        datetime(2026, 7, 22, 7, tzinfo=UTC),
        datetime(2026, 7, 22, 7, 10, tzinfo=UTC),
    ]
    assert [item.open for item in observations] == [100, 101]
    assert all(item.provider_id == "moex-iss-tqbr-10m" for item in observations)
    assert all(item.adjusted is False for item in observations)
    assert report["rows"] == 2
    assert report["tickers_without_data"] == []
    assert report["source"] == "tqbr_shares"
    assert report["market"] == "shares"
    assert report["board"] == "TQBR"
    assert "/markets/shares/boards/TQBR/" in session.calls[0][0]
    assert session.calls[0][1]["start"] == 0
    assert session.calls[0][2] == (15, 30)


@pytest.mark.parametrize("allow_empty", [False, True])
def test_empty_download_requires_explicit_permission(tmp_path: Path, allow_empty: bool) -> None:
    session = _FakeSession([_FakeResponse({"candles": {"columns": ["begin", "open"], "data": []}})])
    output = tmp_path / "empty.jsonl"
    arguments = {
        "tickers": ["SBER"],
        "from_date": date(2026, 1, 1),
        "till_date": date(2026, 1, 1),
        "interval_minutes": 1,
        "output_path": output,
        "session": session,
        "allow_empty": allow_empty,
    }
    if not allow_empty:
        with pytest.raises(ValueError, match="no market opens"):
            download_moex_market_opens(**arguments)
        assert not output.exists()
        assert list(tmp_path.iterdir()) == []
    else:
        report = download_moex_market_opens(**arguments)
        assert output.read_bytes() == b""
        assert report["rows"] == 0
        assert report["tickers_without_data"] == ["SBER"]


def test_download_index_opens_uses_fixed_sndx_source(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 2_750.5]],
                    }
                }
            )
        ]
    )
    output = tmp_path / "imoex.jsonl"

    report = download_moex_market_opens(
        tickers={"IMOEX"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=output,
        session=session,
        request_delay_seconds=0,
        source=MoexCandleSource.SNDX_INDEX,
        sleep=lambda _: None,
    )

    observations = load_market_open_observations(output)
    assert observations[0].provider_id == "moex-iss-sndx-index-10m"
    assert report["source"] == "sndx_index"
    assert report["market"] == "index"
    assert report["board"] == "SNDX"
    assert "/markets/index/boards/SNDX/" in session.calls[0][0]


def test_download_market_opens_maps_historical_security_id(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2020-01-10 10:00:00", 2_500]],
                    }
                }
            )
        ]
    )
    output = tmp_path / "opens.jsonl"

    report = download_moex_market_opens(
        tickers={"YDEX"},
        from_date=date(2020, 1, 10),
        till_date=date(2020, 1, 10),
        interval_minutes=10,
        output_path=output,
        session=session,
        request_delay_seconds=0,
        security_ids={"YDEX": "YNDX"},
        sleep=lambda _: None,
    )

    observations = load_market_open_observations(output)
    assert observations[0].ticker == "YDEX"
    assert "/securities/YNDX/candles.json" in session.calls[0][0]
    assert report["ticker_reports"]["YDEX"]["security_id"] == "YNDX"


def test_download_market_opens_retries_bounded_request(tmp_path: Path) -> None:
    delays = []
    session = _FakeSession(
        [
            _FakeResponse({}, error=requests.ConnectionError("temporary")),
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 100]],
                    }
                }
            ),
        ]
    )

    report = download_moex_market_opens(
        tickers={"SBER"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=tmp_path / "opens.jsonl",
        session=session,
        request_delay_seconds=0,
        maximum_attempts=2,
        sleep=delays.append,
    )

    assert report["ticker_reports"]["SBER"]["requests"] == 2
    assert delays == [1]


def test_download_market_opens_reports_missing_ticker(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _FakeResponse({"candles": {"columns": ["begin", "open"], "data": []}}),
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 100]],
                    }
                }
            ),
        ]
    )

    report = download_moex_market_opens(
        tickers={"AAA", "SBER"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=tmp_path / "opens.jsonl",
        session=session,
        request_delay_seconds=0,
        sleep=lambda _: None,
    )

    assert report["tickers_without_data"] == ["AAA"]


def test_download_market_opens_honors_wall_clock_deadline(tmp_path: Path) -> None:
    session = _FakeSession([])
    output = tmp_path / "opens.jsonl"

    with pytest.raises(TimeoutError, match="wall-clock limit"):
        download_moex_market_opens(
            tickers={"SBER"},
            from_date=date(2026, 7, 22),
            till_date=date(2026, 7, 22),
            interval_minutes=10,
            output_path=output,
            session=session,
            request_delay_seconds=0,
            monotonic=lambda: 10.0,
            run_deadline_monotonic=10.0,
        )

    assert session.calls == []
    assert not output.exists()


def test_download_market_opens_caps_request_at_run_deadline(
    tmp_path: Path,
) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 100]],
                    }
                }
            )
        ]
    )

    download_moex_market_opens(
        tickers={"SBER"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=tmp_path / "opens.jsonl",
        session=session,
        request_delay_seconds=0,
        monotonic=lambda: 80.0,
        run_deadline_monotonic=100.0,
    )

    assert session.calls[0][2] == (10.0, 10.0)


def test_download_market_opens_pages_and_ignores_identical_overlap(
    tmp_path: Path,
) -> None:
    first_at = datetime(2026, 7, 22, 10)
    first_page = [
        [(first_at + timedelta(minutes=10 * index)).isoformat(sep=" "), 100 + index]
        for index in range(500)
    ]
    overlap = first_page[-1]
    next_row = [(first_at + timedelta(minutes=5_000)).isoformat(sep=" "), 601]
    session = _FakeSession(
        [
            _FakeResponse({"candles": {"columns": ["begin", "open"], "data": first_page}}),
            _FakeResponse({"candles": {"columns": ["begin", "open"], "data": [overlap, next_row]}}),
        ]
    )

    report = download_moex_market_opens(
        tickers={"SBER"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 8, 1),
        interval_minutes=10,
        output_path=tmp_path / "opens.jsonl",
        session=session,
        request_delay_seconds=0,
        sleep=lambda _: None,
    )

    ticker_report = report["ticker_reports"]["SBER"]
    assert ticker_report["rows"] == 501
    assert ticker_report["requests"] == 2
    assert ticker_report["duplicate_rows_ignored"] == 1
    assert session.calls[1][1]["start"] == 500


def test_download_market_opens_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "opens.jsonl"
    output.write_text("existing\n")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        download_moex_market_opens(
            tickers={"SBER"},
            from_date=date(2026, 7, 22),
            till_date=date(2026, 7, 22),
            interval_minutes=10,
            output_path=output,
            session=_FakeSession([]),
        )


def test_download_market_opens_never_replaces_competing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "opens.jsonl"
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
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 100]],
                    }
                }
            )
        ]
    )

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        download_moex_market_opens(
            tickers={"SBER"},
            from_date=date(2026, 7, 22),
            till_date=date(2026, 7, 22),
            interval_minutes=10,
            output_path=output,
            session=session,
            request_delay_seconds=0,
            sleep=lambda _: None,
        )

    assert output.read_text(encoding="utf-8") == "competing writer\n"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_download_market_opens_validates_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be later"):
        download_moex_market_opens(
            tickers={"SBER"},
            from_date=date(2026, 7, 23),
            till_date=date(2026, 7, 22),
            interval_minutes=10,
            output_path=tmp_path / "opens.jsonl",
            session=_FakeSession([]),
        )


def test_downloaded_jsonl_is_canonical_and_fingerprinted(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "candles": {
                        "columns": ["begin", "open"],
                        "data": [["2026-07-22 10:00:00", 100]],
                    }
                }
            ),
        ]
    )
    output = tmp_path / "opens.jsonl"

    report = download_moex_market_opens(
        tickers={"SBER"},
        from_date=date(2026, 7, 22),
        till_date=date(2026, 7, 22),
        interval_minutes=10,
        output_path=output,
        session=session,
        request_delay_seconds=0,
        sleep=lambda _: None,
    )

    assert len(report["sha256"]) == 64
    assert json.loads(output.read_text().splitlines()[0])["label_source"] == "market_outcome"
