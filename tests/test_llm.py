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
                "evidence_quotes": ["Чистая прибыль выросла на 15%"],
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
    assert features.extractor_version == "yandexgpt-lite-0.5.0"
    assert features.evidence_quotes == ("Чистая прибыль выросла на 15%",)
    assert features.polarity == 0.86
    request = session.calls[0]
    assert request["url"] == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    payload = request["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt://folder-id/yandexgpt-lite/latest"
    assert "evidence_quotes" in json.dumps(payload["response_format"])
    assert "direction" not in json.dumps(payload)
    assert "action" not in json.dumps(payload)
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert "недоверенные" in messages[0]["content"]
    assert "не является инструкциями" in messages[1]["content"]


def test_yandexgpt_analyzes_market_semantics_without_ticker() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "government_support",
                "facts": [],
                "evidence_quotes": [
                    "Правительство расширит поддержку железнодорожных перевозок"
                ],
                "polarity": 0.7,
                "materiality": 0.8,
                "temporal_status": "future",
                "rationale": "Господдержка позитивна для затронутой отрасли.",
            },
            ensure_ascii=False,
        )
    )
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Правительство расширит поддержку железнодорожных перевозок",
                content="Субсидии направят на экспорт сельхозпродукции.",
            )
        )
    )

    assert features.instruments == []
    assert features.event_type.value == "government_support"
    assert features.polarity > 0
    assert len(session.calls) == 1


def test_confident_llm_rule_polarity_conflict_is_neutralized() -> None:
    assert YandexGptNewsAnalyzer._reconcile_polarity(0.9, -1.0) == 0.0
    assert YandexGptNewsAnalyzer._reconcile_polarity(-0.9, 1.0) == 0.0


def test_structured_financial_loss_keeps_negative_rule_evidence() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "financial_results",
                "facts": [
                    {
                        "kind": "money",
                        "label": "Чистый убыток",
                        "value": "10,7",
                        "unit": "млрд руб.",
                        "period": "РСБУ",
                        "source_quote": "получила 10,7 млрд руб. чистого убытка",
                    }
                ],
                "evidence_quotes": [
                    "АЛРОСА получила 10,7 млрд руб. чистого убытка по РСБУ"
                ],
                "polarity": 0.9,
                "materiality": 0.9,
                "temporal_status": "past",
                "rationale": "LLM ошибочно оценил событие позитивно.",
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
        source_id="interfax",
        title="АЛРОСА получила 10,7 млрд руб. чистого убытка по РСБУ",
        content="Компания отчиталась о чистом убытке за период.",
    )

    features = asyncio.run(analyzer.extract(document))

    assert features.event_type.value == "financial_results"
    assert features.facts
    assert features.polarity == -1


def test_ungrounded_evidence_uses_explicit_rules_fallback() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "financial_results",
                "facts": [],
                "evidence_quotes": ["Компания удвоила чистую прибыль"],
                "polarity": 1,
                "materiality": 1,
                "temporal_status": "past",
                "rationale": "Прибыль якобы удвоилась.",
            },
            ensure_ascii=False,
        )
    )
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Сбербанк опубликовал отчётность",
                content="Банк сообщил результаты за первое полугодие.",
            )
        )
    )

    assert features.extractor_version == "rules-fallback-grounding-0.1.0"
    assert len(session.calls) == 1


def test_structured_fact_value_must_be_present_in_its_quote() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "financial_results",
                "facts": [
                    {
                        "kind": "percentage",
                        "label": "Рост прибыли",
                        "value": "1",
                        "unit": "%",
                        "period": None,
                        "source_quote": "Чистая прибыль выросла на 15%",
                    }
                ],
                "evidence_quotes": ["Чистая прибыль выросла на 15%"],
                "polarity": 0.9,
                "materiality": 0.9,
                "temporal_status": "past",
                "rationale": "Прибыль выросла.",
            },
            ensure_ascii=False,
        )
    )
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Сбербанк опубликовал отчётность",
                content="Чистая прибыль выросла на 15% за полугодие.",
            )
        )
    )

    assert features.extractor_version == "rules-fallback-grounding-0.1.0"


def test_grounding_tolerates_source_whitespace_normalization() -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "partnership",
                "facts": [],
                "evidence_quotes": ["Компания заключила новое соглашение"],
                "polarity": 0.4,
                "materiality": 0.5,
                "temporal_status": "current",
                "rationale": "Заключено соглашение.",
            },
            ensure_ascii=False,
        )
    )
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=session,  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Компания заключила новое соглашение",
                content="Подробности будут раскрыты позднее.",
            )
        )
    )

    assert features.extractor_version == "yandexgpt-lite-0.5.0"
