from eventedge.events import classify_news_event, cluster_market_events


def test_company_event_wins_when_a_traded_ticker_is_known() -> None:
    event = classify_news_event(
        title="Сбербанк опубликовал отчётность",
        content="Прибыль выросла.",
        source_metadata={"tickers": ["SBER"]},
    )

    assert event["scope"] == "company"
    assert event["tickers"] == ["SBER"]
    assert event["sectors"] == ["Финансы"]


def test_warehouse_incident_becomes_retail_logistics_context() -> None:
    event = classify_news_event(
        title="БПЛА повредил склад Wildberries",
        content="Работа логистического объекта временно остановлена.",
        source_metadata={},
    )

    assert event["scope"] == "sector"
    assert event["sectors"] == ["Ритейл и логистика"]
    assert event["tickers"] == []


def test_key_rate_is_market_context_without_automatic_company_signal() -> None:
    event = classify_news_event(
        title="Банк России изменил ключевую ставку",
        content="Решение меняет денежно-кредитные условия.",
        source_metadata={},
    )

    assert event["scope"] == "market"
    assert event["tickers"] == []


def test_corroborating_publications_form_one_event() -> None:
    common = {
        "scope": "company",
        "scope_label": "Компания",
        "tickers": ["SBER"],
        "sectors": ["Финансы"],
        "published_at": "2026-08-09T10:00:00Z",
        "related_signals": [],
    }
    events = cluster_market_events(
        [
            {
                **common,
                "id": "event_one",
                "news_id": "news_one",
                "source_id": "interfax",
                "source_url": "https://example.com/one",
                "title": "Сбербанк опубликовал сильные результаты за полугодие",
                "summary": "Короткая публикация.",
            },
            {
                **common,
                "id": "event_two",
                "news_id": "news_two",
                "source_id": "rbc",
                "source_url": "https://example.com/two",
                "title": "Сбербанк опубликовал результаты за полугодие",
                "summary": "Более подробная подтверждающая публикация с цифрами.",
            },
        ]
    )

    assert len(events) == 1
    assert events[0]["news_ids"] == ["news_one", "news_two"]
    assert events[0]["source_count"] == 2
    assert events[0]["title"] == "Сбербанк опубликовал результаты за полугодие"
