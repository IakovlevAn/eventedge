from __future__ import annotations

import asyncio
import json

from eventedge.analysis import NewsAnalysisInput
from eventedge.llm import YandexGptNewsAnalyzer


class FakeTokenProvider:
    def get(self) -> str:
        return "test-iam-token"


class FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "choices": [{"message": {"content": self._content}}],
        }


class FakeSession:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.content)


def test_yandexgpt_extracts_semantics_but_keeps_ticker_deterministic() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "financial_results",
                "facts": [
                    {
                        "kind": "percentage",
                        "label": "Рост прибыли",
                        "value": "15",
                        "unit": "%",
                        "period": None,
                        "source_quote": "Чистая прибыль выросла на 15%",
                    }
                ],
                "polarity": 0.8,
                "materiality": 0.9,
                "temporal_status": "past",
                "rationale": "Прибыль выросла сильнее ожиданий.",
            },
            ensure_ascii=False,
        )
    )
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )
    document = NewsAnalysisInput(
        source_id="moex_news",
        title="Сбербанк опубликовал отчётность",
        content="Чистая прибыль выросла на 15% и оказалась выше ожиданий.",
    )

    features = asyncio.run(analyzer.extract(document))

    assert [item.ticker for item in features.instruments] == ["SBER"]
    assert features.extractor_version == "yandexgpt-lite-0.2.0"
    assert features.polarity == 0.86
    request = session.calls[0]
    assert request["url"] == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    payload = request["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt://folder-id/yandexgpt-lite/latest"
    assert "direction" not in json.dumps(payload)
    assert "action" not in json.dumps(payload)


def test_yandexgpt_is_not_called_for_unrelated_news() -> None:
    session = FakeSession("{}")
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Биржа изменила параметры торгов",
                content="Изменения вступают в силу в понедельник.",
            )
        )
    )

    assert features.instruments == []
    assert session.calls == []


def test_confident_llm_rule_polarity_conflict_is_neutralized() -> None:
    assert YandexGptNewsAnalyzer._reconcile_polarity(0.9, -1.0) == 0.0
    assert YandexGptNewsAnalyzer._reconcile_polarity(-0.9, 1.0) == 0.0
