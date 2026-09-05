from __future__ import annotations

import asyncio
import json

import pytest

from eventedge.analysis import NewsAnalysisInput, SignalDirection, score_features
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
    assert features.extractor_version == "yandexgpt-lite-0.6.1"
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


def test_explicit_financial_loss_conflicting_with_llm_abstains() -> None:
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
    assert features.polarity == 0


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

    assert features.extractor_version == "rules-fallback-grounding-0.2.0"
    assert len(session.calls) == 1


def test_async_deadline_uses_explicit_rules_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stuck_to_thread(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(60)
        raise AssertionError("stuck extraction should have been cancelled")

    monkeypatch.setattr("eventedge.llm.asyncio.to_thread", stuck_to_thread)
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=FakeSession("{}"),  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )
    analyzer._async_deadline_seconds = 0.01

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="moex_news",
                title="Сбербанк рекомендовал дивиденды",
                content="Совет директоров рекомендовал выплатить 500 рублей на акцию.",
            )
        )
    )

    assert features.extractor_version == "rules-fallback-timeout-0.2.0"
    assert [instrument.ticker for instrument in features.instruments] == ["SBER"]


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

    assert features.extractor_version == "rules-fallback-grounding-0.2.0"


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

    assert features.extractor_version == "yandexgpt-lite-0.6.1"


@pytest.mark.parametrize(
    ("title", "content", "llm_polarity", "expected_polarity", "expected_direction"),
    [
        (
            "КАМАЗ снизил чистый убыток по РСБУ на 29,5%",
            "Компания раскрыла результаты за полугодие.",
            0.8, 0.86, SignalDirection.UP,
        ),
        (
            "АЛРОСА снизила долг на 50 млрд рублей",
            "Компания раскрыла результаты по МСФО.",
            0.8, 0.86, SignalDirection.UP,
        ),
        (
            "АЛРОСА увеличила чистый убыток по РСБУ на 50%",
            "Компания раскрыла результаты за полугодие.",
            -0.8, -0.86, SignalDirection.DOWN,
        ),
        (
            "Сбербанк увеличил чистую прибыль на 20%",
            "Компания раскрыла результаты за полугодие.",
            0.0, 0.0, SignalDirection.NEUTRAL,
        ),
        (
            "Сбербанк увеличил чистую прибыль на 20%",
            "Компания раскрыла результаты за полугодие.",
            -0.2, 0.0, SignalDirection.NEUTRAL,
        ),
        (
            "КАМАЗ снизил чистый убыток по РСБУ на 29,5%",
            "Выручка выросла на 7%, себестоимость выросла на 10%.",
            0.8, 0.0, SignalDirection.NEUTRAL,
        ),
        (
            "АЛРОСА не снизила чистый убыток на 50%",
            "Компания раскрыла результаты по МСФО.",
            0.8, 0.0, SignalDirection.NEUTRAL,
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Сбербанк опубликовал выручку 100 млрд рублей при росте клиентской базы на 20%.",
            0.8, 0.0, SignalDirection.NEUTRAL,
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Прибыль составила 100 млрд рублей благодаря росту числа клиентов на 20%.",
            0.8, 0.0, SignalDirection.NEUTRAL,
        ),
    ],
)
def test_financial_llm_reconciliation_preserves_metric_semantics_and_abstention(
    title: str,
    content: str,
    llm_polarity: float,
    expected_polarity: float,
    expected_direction: SignalDirection,
) -> None:
    session = FakeSession(
        json.dumps(
            {
                "event_type": "financial_results",
                "facts": [],
                "evidence_quotes": [title],
                "polarity": llm_polarity,
                "materiality": 0.9,
                "temporal_status": "past",
                "rationale": "Оценка финансовых результатов по источнику.",
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
        analyzer.extract(NewsAnalysisInput(source_id="interfax", title=title, content=content))
    )

    assert features.extractor_version == "yandexgpt-lite-0.6.1"
    assert features.polarity == expected_polarity
    assert score_features(features, source_id="interfax")[0].direction is expected_direction


def test_llm_failure_fallback_understands_a_reduction_in_losses() -> None:
    analyzer = YandexGptNewsAnalyzer(
        folder_id="folder-id",
        session=FakeSession("{}"),  # type: ignore[arg-type]
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )

    features = asyncio.run(
        analyzer.extract(
            NewsAnalysisInput(
                source_id="interfax",
                title="КАМАЗ снизил чистый убыток по РСБУ на 29,5%",
                content="Компания раскрыла результаты за полугодие.",
            )
        )
    )

    assert features.extractor_version == "rules-fallback-0.2.0"
    assert features.polarity == 1
    assert score_features(features, source_id="interfax")[0].direction is SignalDirection.UP
