from __future__ import annotations

import pytest
from pydantic import ValidationError

from eventedge.analysis import (
    BaselineScoringConfig,
    EventType,
    InstrumentMention,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
    SemanticFeatures,
    SignalAction,
    SignalDirection,
    TemporalStatus,
    extract_and_score,
    score_features,
)


def test_positive_company_results_create_algorithmic_up_signal() -> None:
    document = NewsAnalysisInput(
        source_id="interfax",
        title="Сбербанк опубликовал сильную отчётность",
        content=(
            "Чистая прибыль выросла на 17% и оказалась выше ожиданий рынка. "
            "Рентабельность капитала улучшилась."
        ),
    )

    features, signals = extract_and_score(document)

    assert features.event_type is EventType.FINANCIAL_RESULTS
    assert features.instruments[0].ticker == "SBER"
    assert features.facts[0].value == "17"
    assert features.facts[0].unit == "%"
    assert features.polarity == 1
    assert len(signals) == 1
    assert signals[0].direction is SignalDirection.UP
    assert signals[0].action is SignalAction.CONSIDER_BUY
    assert signals[0].score == 50.5
    assert signals[0].confidence == pytest.approx(0.9063)
    assert signals[0].model_version == "signal-engine-0.6.1"
    assert signals[0].extractor_version == "rules-0.1.0"


def test_negative_restrictions_create_down_signal() -> None:
    document = NewsAnalysisInput(
        source_id="reuters",
        title="Ограничения затронули проект Новатэка",
        content=(
            "Новые санкции ограничили поставки оборудования. "
            "Компания может приостановить часть работ."
        ),
    )

    features, signals = extract_and_score(document)

    assert features.event_type is EventType.SANCTIONS
    assert features.instruments[0].ticker == "NVTK"
    assert features.polarity == -1
    assert signals[0].direction is SignalDirection.DOWN
    assert signals[0].action is SignalAction.REVIEW_POSITION
    assert signals[0].score == -49.6


def test_conflicting_text_stays_in_neutral_zone() -> None:
    document = NewsAnalysisInput(
        source_id="rbc",
        title="Яндекс обновил облачный сервис",
        content=(
            "Выручка направления выросла на 12%, но операционная маржа снизилась на 12%. "
            "Новых ориентиров компания не дала."
        ),
    )

    features, signals = extract_and_score(document)

    assert features.event_type is EventType.FINANCIAL_RESULTS
    assert features.polarity == 0
    assert signals[0].direction is SignalDirection.NEUTRAL
    assert signals[0].action is SignalAction.NO_ACTION
    assert signals[0].score == 0


def test_macro_news_without_direct_company_does_not_publish_stock_signal() -> None:
    document = NewsAnalysisInput(
        source_id="cbr_press",
        title="Банк России сохранил ключевую ставку",
        content="Ключевая ставка сохранена на уровне 12% годовых.",
    )

    features, signals = extract_and_score(document)

    assert features.event_type is EventType.MACRO
    assert features.instruments == []
    assert signals == []


def test_other_event_cannot_become_directional_from_llm_tone_alone() -> None:
    features = SemanticFeatures(
        extractor_version="yandexgpt-lite-0.1.0",
        event_type=EventType.OTHER,
        instruments=[InstrumentMention(ticker="SBER", relevance=0.9, matched_alias="сбер")],
        facts=[],
        polarity=1,
        materiality=0.95,
        novelty=1,
        temporal_status=TemporalStatus.CURRENT,
        rationale="Служебное сообщение без классифицированного драйвера.",
    )

    signal = score_features(features, source_id="moex_news")[0]

    assert signal.direction is SignalDirection.NEUTRAL
    assert signal.action is SignalAction.NO_ACTION
    assert signal.score == 0


def test_exchange_boilerplate_is_not_a_moex_company_mention() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="moex_news",
            title="Московская биржа обновила список ценных бумаг",
            content="Техническое уведомление торговой площадки.",
        )
    )

    assert features.instruments == []


def test_gazprom_neft_does_not_also_match_gazprom() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="interfax",
            title="Газпром нефть запустила новый сервис",
            content="Компания повысила эффективность производства.",
        )
    )

    assert [item.ticker for item in features.instruments] == ["SIBN"]


def test_gazprom_and_gazprom_neft_can_both_match() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="interfax",
            title="Газпром и Газпром нефть подписали соглашение",
            content="Компании расширят совместный проект.",
        )
    )

    assert {item.ticker for item in features.instruments} == {"GAZP", "SIBN"}


def test_feature_contract_cannot_accept_llm_direction_or_action() -> None:
    with pytest.raises(ValidationError) as error:
        SemanticFeatures.model_validate(
            {
                "extractor_version": "yandexgpt-lite-0.1",
                "event_type": "other",
                "instruments": [],
                "facts": [],
                "polarity": 0,
                "materiality": 0.3,
                "novelty": 1,
                "temporal_status": "current",
                "rationale": "Недостаточно данных.",
                "direction": "up",
                "action": "consider_buy",
            }
        )

    assert "direction" in str(error.value)
    assert "action" in str(error.value)


def test_extraction_and_scoring_are_reproducible() -> None:
    document = NewsAnalysisInput(
        source_id="company",
        title="Лукойл увеличил дивиденды",
        content="Совет директоров повысил дивиденды на 9% до 540 рублей на акцию.",
    )
    extractor = RuleBasedNewsExtractor()
    config = BaselineScoringConfig(config_version=7)

    first = extract_and_score(document, extractor=extractor, config=config)
    second = extract_and_score(document, extractor=extractor, config=config)

    assert first == second
    assert first[1][0].config_version == 7
    assert len(first[1][0].factor_contributions) == 5
