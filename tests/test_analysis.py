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
    assert signals[0].config_version == 7
    assert signals[0].extractor_version == "rules-0.3.0"


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


def test_down_signal_requires_conservative_score_and_confidence() -> None:
    document = NewsAnalysisInput(
        source_id="reuters",
        title="Ограничения затронули проект Новатэка",
        content="Новые санкции ограничили поставки оборудования.",
    )
    features = RuleBasedNewsExtractor().extract(document)

    low_confidence = score_features(
        features,
        source_id="reuters",
        config=BaselineScoringConfig(minimum_down_confidence=0.99),
    )[0]
    weak_score = score_features(
        features,
        source_id="reuters",
        config=BaselineScoringConfig(negative_threshold=-60),
    )[0]

    assert BaselineScoringConfig().config_version == 7
    assert BaselineScoringConfig().negative_threshold == -30
    assert BaselineScoringConfig().minimum_down_confidence == 0.80
    assert low_confidence.direction is SignalDirection.NEUTRAL
    assert low_confidence.action is SignalAction.NO_ACTION
    assert weak_score.direction is SignalDirection.NEUTRAL
    assert weak_score.action is SignalAction.NO_ACTION


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


def test_ozon_pharmaceutical_company_is_not_ozon_marketplace() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="telegram_selfinvestor",
            title=(
                "Дивиденды рекомендовали НОВАТЭК, Норникель, "
                "Озон Фармацевтика и Черкизово"
            ),
            content="Советы директоров рекомендовали выплаты акционерам.",
        )
    )

    assert {item.ticker for item in features.instruments} == {"NVTK", "GMKN"}


def test_quoted_company_is_context_when_it_evaluates_another_issuer() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="rbc",
            title="В ВТБ оценили негативный эффект для Wildberries и Ozon от атак БПЛА",
            content="Аналитики считают, что выручка Ozon может снизиться на 20%.",
        )
    )
    relevance = {item.ticker: item.relevance for item in features.instruments}

    assert relevance["OZON"] >= 0.9
    assert relevance["VTBR"] < 0.9


def test_company_remains_direct_when_it_evaluates_its_own_results() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="interfax",
            title="В ВТБ оценили влияние ставки на прибыль банка",
            content="ВТБ ожидает, что чистая прибыль увеличится на 10%.",
        )
    )

    assert [(item.ticker, item.relevance) for item in features.instruments] == [
        ("VTBR", 0.96)
    ]


def test_rule_extractor_bounds_large_instrument_lists() -> None:
    aliases = {f"T{index:02d}": (f"issuer{index}",) for index in range(25)}
    features = RuleBasedNewsExtractor(aliases=aliases).extract(
        NewsAnalysisInput(
            source_id="archive_test",
            title=" ".join(alias[0] for alias in aliases.values()),
            content="Сводка по множеству эмитентов.",
        )
    )

    assert len(features.instruments) == 20
    assert [item.ticker for item in features.instruments[:2]] == ["T00", "T01"]


def test_rule_extractor_keeps_direct_title_match_when_bounding_list() -> None:
    aliases = {f"T{index:02d}": (f"issuer{index}",) for index in range(21)}
    features = RuleBasedNewsExtractor(aliases=aliases).extract(
        NewsAnalysisInput(
            source_id="archive_test",
            title="issuer20",
            content=" ".join(f"issuer{index}" for index in range(20)),
        )
    )

    relevance = {item.ticker: item.relevance for item in features.instruments}
    assert len(relevance) == 20
    assert relevance["T20"] == 0.96
    assert "T19" not in relevance


def test_platform_is_context_when_another_issuer_acts_for_its_sellers() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="google_news",
            title="ВТБ окажет поддержку продавцам Ozon, пострадавшим в результате атак",
            content="Банк увеличил объём программы поддержки на 20%.",
        )
    )
    relevance = {item.ticker: item.relevance for item in features.instruments}

    assert relevance["VTBR"] >= 0.9
    assert relevance["OZON"] < 0.9


def test_direct_ozon_event_remains_a_direct_company_mention() -> None:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="interfax",
            title="Ozon увеличил выручку и улучшил прогноз EBITDA",
            content="Компания повысила прогноз после публикации отчётности.",
        )
    )

    assert [(item.ticker, item.relevance) for item in features.instruments] == [
        ("OZON", 0.96)
    ]


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


@pytest.mark.parametrize(
    ("title", "expected_polarity", "expected_direction"),
    [
        ("КАМАЗ снизил чистый убыток по РСБУ на 29,5%", 1, SignalDirection.UP),
        ("АЛРОСА сократила затраты на 65 млрд рублей", 1, SignalDirection.UP),
        ("АЛРОСА снизила долг на 50 млрд рублей", 1, SignalDirection.UP),
        ("Чистый убыток АЛРОСА сократился на 50%", 1, SignalDirection.UP),
        ("Расходы АЛРОСА снизились на 20%", 1, SignalDirection.UP),
        ("Чистый убыток АЛРОСА вырос на 50%", -1, SignalDirection.DOWN),
        ("АЛРОСА увеличила расходы на 20%", -1, SignalDirection.DOWN),
        ("Долг АЛРОСА увеличился на 50 млрд рублей", -1, SignalDirection.DOWN),
        ("Сбербанк снизил чистую прибыль на 20%", -1, SignalDirection.DOWN),
        ("Выручка Сбербанка выросла на 20%", 1, SignalDirection.UP),
        (
            "Прибыль Газпрома по МСФО за II квартал выросла на 60.7% - akm.ru",
            1,
            SignalDirection.UP,
        ),
    ],
)
def test_financial_direction_follows_the_metric_not_the_change_word(
    title: str, expected_polarity: float, expected_direction: SignalDirection
) -> None:
    features, signals = extract_and_score(
        NewsAnalysisInput(
            source_id="interfax",
            title=title,
            content="Компания раскрыла результаты по МСФО.",
        )
    )

    assert features.event_type is EventType.FINANCIAL_RESULTS
    assert features.polarity == expected_polarity
    assert signals[0].direction is expected_direction


@pytest.mark.parametrize(
    ("title", "content"),
    [
        ("КАМАЗ не снизил чистый убыток на 29,5%", "Компания раскрыла результаты по РСБУ."),
        ("Сбербанк ожидает роста прибыли на 20%", "Компания представила прогноз."),
        ("АЛРОСА может сократить расходы на 20%", "Компания представила прогноз по МСФО."),
        ("Сбербанк опроверг рост прибыли на 20%", "Компания опубликовала пояснение."),
        (
            "Сбербанк увеличил чистую прибыль на 20%",
            "Чистая прибыль выросла на 20%, но оказалась ниже ожиданий рынка.",
        ),
        (
            "Сбербанк снизил чистую прибыль на 20%",
            "Чистая прибыль снизилась на 20%, но оказалась выше ожиданий рынка.",
        ),
        (
            "КАМАЗ снизил чистый убыток по РСБУ на 29,5%",
            "Выручка выросла на 7%, себестоимость выросла на 10%. "
            "Валовая прибыль снизилась на 19%, операционный убыток сократился на 55,5%.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Чистая прибыль составила 100 млрд рублей. Число клиентов выросло на 20%.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Годом ранее чистая прибыль выросла на 20%. За текущий период сравнение не приведено.",
        ),
        (
            "Сбербанк увеличил чистую прибыль на 20%",
            "Результат оказался ниже ожиданий рынка.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Прибыль выросла на 20%, оказавшись хуже прогноза аналитиков.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Чистая прибыль выросла на 20% в прошлом году. В этом году она снизилась на 10%.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Чистая прибыль выросла на 20% в 2025 году. В 2026 году она снизилась на 10%.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Прибыль составила 100 млрд рублей благодаря росту числа клиентов на 20%.",
        ),
        (
            "Сбербанк опубликовал финансовые результаты",
            "Сбербанк опубликовал выручку 100 млрд рублей при росте клиентской базы на 20%.",
        ),
        ("АЛРОСА отказалась от сокращения расходов на 20%", "Компания раскрыла данные по МСФО."),
        ("АЛРОСА исключила рост прибыли на 20%", "Компания раскрыла данные по МСФО."),
        (
            "АЛРОСА сообщила об отсутствии роста выручки на 20%",
            "Компания раскрыла данные по МСФО.",
        ),
        ("Сбербанк сократил долги заемщиков на 20%", "Банк раскрыл финансовые результаты."),
    ],
)
def test_uncertain_or_mixed_financial_evidence_abstains(title: str, content: str) -> None:
    features, signals = extract_and_score(
        NewsAnalysisInput(source_id="interfax", title=title, content=content)
    )

    if features.event_type is EventType.FINANCIAL_RESULTS:
        assert features.polarity == 0
    assert signals[0].direction is SignalDirection.NEUTRAL
    assert signals[0].score == 0


def test_prior_year_loss_does_not_reverse_current_profit_growth() -> None:
    features, signals = extract_and_score(
        NewsAnalysisInput(
            source_id="interfax",
            title="Сбербанк увеличил чистую прибыль на 20%",
            content="Годом ранее компания получила чистый убыток в 10 млрд рублей.",
        )
    )

    assert features.polarity == 1
    assert signals[0].direction is SignalDirection.UP


@pytest.mark.parametrize(
    ("title", "content", "expected_direction"),
    [
        (
            "Сбербанк снизил расходы на 20%",
            "Банк внедрил новую технологию.",
            SignalDirection.UP,
        ),
        (
            "АЛРОСА снизила долг на 50 млрд рублей",
            "Компания продала непрофильные активы.",
            SignalDirection.UP,
        ),
        (
            "АЛРОСА снизила долг на 50 млрд рублей",
            "Компания продала непрофильные активы. Выручка снизилась на 20%.",
            SignalDirection.NEUTRAL,
        ),
    ],
)
def test_primary_financial_headline_is_not_overridden_by_incidental_event_words(
    title: str, content: str, expected_direction: SignalDirection
) -> None:
    features, signals = extract_and_score(
        NewsAnalysisInput(source_id="interfax", title=title, content=content)
    )

    assert features.event_type is EventType.FINANCIAL_RESULTS
    assert signals[0].direction is expected_direction
