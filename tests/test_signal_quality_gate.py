from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_signal_quality_gate import evaluate_contract, load_contract, main

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retro_signal_audit.json"


def test_signal_quality_contract_reports_only_its_binary_labels() -> None:
    report = evaluate_contract(load_contract(FIXTURE_PATH))

    assert report["observations"] == 32
    assert report["dataset_kind"] == "synthetic_contract"
    assert report["router"] == "signal"
    assert "scope" not in report
    assert report["relevance"] == {
        "true_positive": 16,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 16,
        "precision": 1.0,
        "recall": 1.0,
    }


def test_signal_quality_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    fixture = tmp_path / "invalid.json"
    fixture.write_text(
        '[{"title":"x","content":"","expected_candidate":true,'
        '"reason":"contract","future_outcome":"invented"}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="future_outcome"):
        load_contract(fixture)


def test_signal_quality_cli_exits_nonzero_on_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "regression.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "title": "Сбербанк рекомендовал дивиденды",
                    "content": "Совет директоров определил размер выплаты.",
                    "expected_candidate": False,
                    "reason": "Deliberate mismatch for the failure-path test.",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_signal_quality_gate",
            "--dataset",
            str(fixture),
            "--minimum-observations",
            "1",
        ],
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["gate"]["passed"] is False
    assert report["relevance"]["false_positive"] == 1
