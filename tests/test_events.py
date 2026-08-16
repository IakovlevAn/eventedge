from eventedge.events import classify_news_event, cluster_market_events, context_signal_specs


def test_company_event_wins_when_a_traded_ticker_is_known() -> None:
    event = classify_news_event(
        title="Сбербанк опубликовал отчётность",
        content="Прибыль выросла.",
        source_metadata={"tickers": ["SBER"]},
    )

    assert event["scope"] == "company"
    assert event["tickers"] == ["SBER"]
    assert event["sectors"] == ["Финансы"]
    assert event["event_type"] == "financial_results"
    assert event["materiality"] > 0
    assert event["extractor_version"] == "rules-0.1.0"


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


def test_local_subsidy_does_not_become_whole_market_signal() -> None:
    title = "Путин призвал расширить субсидии регионам на оснащение остановок"
    event = classify_news_event(title=title, content=title, source_metadata={})

    assert event["scope"] == "market"
    assert context_signal_specs(event, title=title, content=title) == []


def test_single_airport_delay_does_not_become_transport_sector_signal() -> None:
    title = "Аэропорт Омска отменил один рейс и задержал еще несколько"
    content = (
        "Ограничения на прием самолетов затронули несколько рейсов, "
        "сообщила транспортная прокуратура."
    )
    event = classify_news_event(title=title, content=content, source_metadata={})

    assert event["scope"] == "sector"
    assert context_signal_specs(event, title=title, content=content) == []


def test_export_support_remains_sector_signal_eligible() -> None:
    title = "Правительство расширяет господдержку экспорта сельхозпродукции"
    content = "Субсидии затронут железнодорожную логистику и аграрных производителей."
    event = classify_news_event(title=title, content=content, source_metadata={})

    assert event["scope"] == "sector"
    assert {ticker for ticker, _ in context_signal_specs(event, title=title, content=content)} == {
        "RUAGRI",
        "RUTRANS",
    }


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


def test_event_clustering_is_order_independent_and_preserves_provenance() -> None:
    common = {
        "scope": "company",
        "scope_label": "Компания",
        "tickers": ["SBER"],
        "sectors": ["Финансы"],
        "event_type": "financial_results",
        "extractor_version": "rules-0.1.0",
        "materiality": 0.8,
    }
    earlier = {
        **common,
        "id": "evt_earlier",
        "news_id": "news_earlier",
        "source_id": "interfax",
        "source_url": "https://example.com/earlier",
        "title": "Сбербанк опубликовал результаты за полугодие",
        "summary": "Короткая публикация.",
        "published_at": "2026-08-09T10:00:00Z",
        "detected_at": "2026-08-09T10:01:00Z",
        "evidence": {"news_id": "news_earlier", "content_hash": "one"},
        "related_signals": [{"id": "sig_one", "score": 20}],
    }
    later = {
        **common,
        "id": "evt_later",
        "news_id": "news_later",
        "source_id": "rbc",
        "source_url": "https://example.com/later",
        "title": "Сбербанк опубликовал сильные результаты за полугодие",
        "summary": "Более подробная подтверждающая публикация с цифрами.",
        "published_at": "2026-08-09T11:00:00Z",
        "detected_at": "2026-08-09T11:01:00Z",
        "evidence": {"news_id": "news_later", "content_hash": "two"},
        "related_signals": [{"id": "sig_two", "score": 30}],
    }

    chronological = cluster_market_events([earlier, later])
    reversed_input = cluster_market_events([later, earlier])

    assert chronological == reversed_input
    assert chronological[0]["id"] == "evt_earlier"
    assert chronological[0]["event_time"] == earlier["published_at"]
    assert chronological[0]["news_ids"] == ["news_earlier", "news_later"]
    assert [item["news_id"] for item in chronological[0]["evidence"]] == [
        "news_earlier",
        "news_later",
    ]
    assert [item["id"] for item in chronological[0]["related_signals"]] == [
        "sig_one",
        "sig_two",
    ]


def test_different_semantic_event_types_are_not_merged() -> None:
    common = {
        "scope": "company",
        "scope_label": "Компания",
        "tickers": ["SBER"],
        "sectors": ["Финансы"],
        "published_at": "2026-08-09T10:00:00Z",
        "title": "Сбербанк сообщил о новом корпоративном решении",
        "summary": "Подробности решения.",
        "related_signals": [],
    }
    events = cluster_market_events(
        [
            {
                **common,
                "id": "evt_results",
                "news_id": "news_results",
                "source_id": "interfax",
                "source_url": "https://example.com/results",
                "event_type": "financial_results",
            },
            {
                **common,
                "id": "evt_dividend",
                "news_id": "news_dividend",
                "source_id": "rbc",
                "source_url": "https://example.com/dividend",
                "event_type": "dividend",
            },
        ]
    )

    assert len(events) == 2
