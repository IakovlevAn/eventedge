from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import eventedge.collectors as collectors_module
from eventedge.analysis import RuleBasedNewsExtractor
from eventedge.collectors import (
    RssFeedConfig,
    RssItem,
    TelegramChannelConfig,
    collect_news_items,
    collect_rss_feed,
    collect_telegram_channel,
    google_news_search_url,
    is_analytical_review,
    is_cbr_market_news,
    is_company_news_candidate,
    is_google_market_background_candidate,
    is_google_market_signal_candidate,
    is_market_event_candidate,
    is_market_signal_candidate,
    is_moex_equity_title,
    is_multi_company_roundup,
    is_obvious_company_product_or_marketing_noise,
    is_semantic_analysis_candidate,
    is_signal_analysis_candidate,
    is_watched_company_news,
    parse_rss,
    parse_telegram_channel,
    signal_analysis_exclusion_reason,
    signal_analysis_priority,
)
from eventedge.storage import MemoryNewsRepository, TelegramSourceRecord

RSS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Банк России сохранил ключевую ставку</title>
      <link>https://www.cbr.ru/press/keypr/one/</link>
      <guid isPermaLink="false">docid_1</guid>
      <description>
        &lt;p&gt;Ставка сохранена на уровне &lt;strong&gt;12%&lt;/strong&gt;.&lt;/p&gt;
      </description>
      <pubDate>Fri, 07 Aug 2026 13:30:00 +0300</pubDate>
      <category>Денежно-кредитная политика</category>
    </item>
    <item>
      <title>Опубликована банковская статистика</title>
      <link>https://www.cbr.ru/press/bankstat/two/</link>
      <guid isPermaLink="false">docid_2</guid>
      <description>&lt;p&gt;Новые данные по банковскому сектору.&lt;/p&gt;</description>
      <pubDate>Thu, 06 Aug 2026 10:00:00 +0300</pubDate>
      <category>Банковский сектор</category>
    </item>
  </channel>
</rss>
""".encode()

TELEGRAM_FIXTURE = """
<section class="tgme_channel_history js-message_history">
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="AK47pfl/21664">
      <div class="tgme_widget_message_text js-message_text">
        Старый пост<br/>Без упоминания компании
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/AK47pfl/21664">
        <time datetime="2026-08-08T07:00:00+00:00">07:00</time>
      </a>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="AK47pfl/21665">
      <div class="tgme_widget_message_text js-message_text">
        Сбербанк увеличил прибыль<br/><b>Выручка выросла на 12%.</b>
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/AK47pfl/21665">
        <time datetime="2026-08-08T08:13:59+00:00">08:13</time>
      </a>
    </div>
  </div>
</section>
""".encode()

BBBREAKING_SECTOR_FIXTURE = """
<section class="tgme_channel_history js-message_history">
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="bbbreaking/235334">
      <div class="tgme_widget_message_text js-message_text">
        ❗️Правительство готовит проект распоряжения о выделении порядка 10 млрд руб.
        на поддержку ж/д перевозок направляемой на экспорт сельхозпродукции.
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/bbbreaking/235334">
        <time datetime="2026-08-10T07:10:13+00:00">07:10</time>
      </a>
    </div>
  </div>
</section>
""".encode()


def test_rss_parser_extracts_clean_text_and_metadata() -> None:
    items = parse_rss(RSS_FIXTURE, max_items=10)

    assert len(items) == 2
    assert items[0].external_id == "docid_1"
    assert items[0].content == "Ставка сохранена на уровне 12%."
    assert items[0].published_at.isoformat() == "2026-08-07T13:30:00+03:00"
    assert items[0].categories == ("Денежно-кредитная политика",)


def test_telegram_parser_extracts_posts_in_reverse_chronological_order() -> None:
    items = parse_telegram_channel(TELEGRAM_FIXTURE, max_items=10)

    assert len(items) == 2
    assert items[0].external_id == "AK47pfl/21665"
    assert items[0].title == "Сбербанк увеличил прибыль"
    assert items[0].content == "Сбербанк увеличил прибыль\nВыручка выросла на 12%."
    assert items[0].url == "https://t.me/AK47pfl/21665"
    assert items[0].published_at.isoformat() == "2026-08-08T08:13:59+00:00"


def test_telegram_collection_is_idempotent_and_marks_source_metadata() -> None:
    repository = MemoryNewsRepository()
    config = TelegramChannelConfig(source_id="telegram_ak47pfl", channel="AK47pfl")

    async def scenario() -> tuple[dict[str, int], dict[str, int], dict[str, object]]:
        fetcher = lambda url, timeout: TELEGRAM_FIXTURE  # noqa: E731
        first = await collect_telegram_channel(repository, config, fetcher=fetcher)
        second = await collect_telegram_channel(repository, config, fetcher=fetcher)
        news = await repository.list_news(source_id="telegram_ak47pfl", limit=10)
        return first, second, dict(news[0].source_metadata)

    first, second, metadata = asyncio.run(scenario())

    assert first["accepted"] == 2
    assert second["replayed"] == 2
    assert metadata["collector"] == "telegram_public"
    assert metadata["channel_url"] == "https://t.me/s/AK47pfl"


def test_sector_news_without_ticker_is_analyzed_and_stored() -> None:
    repository = MemoryNewsRepository()
    config = TelegramChannelConfig(source_id="telegram_bbbreaking", channel="bbbreaking")

    async def scenario() -> tuple[dict[str, int], dict[str, object], list[dict[str, object]]]:
        result = await collect_telegram_channel(
            repository,
            config,
            fetcher=lambda url, timeout: BBBREAKING_SECTOR_FIXTURE,
            item_filter=is_market_event_candidate,
            signal_filter=is_market_signal_candidate,
        )
        news = await repository.list_news(source_id="telegram_bbbreaking", limit=10)
        signals = await repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=10,
        )
        return result, news[0].as_api_dict(), [signal.as_api_dict() for signal in signals]

    result, stored, signals = asyncio.run(scenario())

    assert result == {
        "fetched": 1,
        "matched": 1,
        "filtered": 0,
        "signal_candidates": 0,
        "analysis_candidates": 1,
        "accepted": 1,
        "replayed": 0,
    }
    assert stored["external_id"] == "bbbreaking/235334"
    assert stored["processing"] == {
        "status": "processed",
        "classification": "semantic_candidate",
        "reason": "eligible_for_semantic_signal_analysis",
        "event_candidate": True,
        "signal_candidate": False,
        "analysis_candidate": True,
        "classification_version": "candidate-gate-0.7.1",
    }
    assert signals == []
    assert stored["source_metadata"]["signal_outcome"] == {
        "status": "rejected_after_analysis",
        "signal_count": 0,
        "model_version": "signal-engine-0.6.1",
        "reason": "unvalidated_context_signal",
    }


def test_timer_collection_persists_candidate_before_semantic_analysis() -> None:
    class UnexpectedAnalyzer:
        async def extract(self, document: object) -> object:
            raise AssertionError("timer collection must not wait for semantic analysis")

    repository = MemoryNewsRepository(analyzer=UnexpectedAnalyzer())  # type: ignore[arg-type]
    config = TelegramChannelConfig(source_id="telegram_bbbreaking", channel="bbbreaking")

    async def scenario() -> tuple[dict[str, int], dict[str, object], int]:
        result = await collect_telegram_channel(
            repository,
            config,
            fetcher=lambda url, timeout: BBBREAKING_SECTOR_FIXTURE,
            item_filter=is_market_event_candidate,
            signal_filter=is_market_signal_candidate,
            analyze_signals=False,
        )
        news = await repository.list_news(source_id="telegram_bbbreaking", limit=10)
        signals = await repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=10,
        )
        return result, news[0].as_api_dict(), len(signals)

    result, stored, signal_count = asyncio.run(scenario())

    assert result["accepted"] == 1
    assert result["analysis_candidates"] == 1
    assert stored["processing"]["status"] == "pending"
    assert signal_count == 0


def test_rss_collection_is_idempotent() -> None:
    repository = MemoryNewsRepository()
    config = RssFeedConfig(
        source_id="cbr_press",
        url="https://www.cbr.ru/rss/RssPress",
    )

    async def scenario() -> tuple[dict[str, int], dict[str, int]]:
        def fetcher(url: str, timeout: float) -> bytes:
            return RSS_FIXTURE

        first = await collect_rss_feed(repository, config, fetcher=fetcher)
        second = await collect_rss_feed(repository, config, fetcher=fetcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == {
        "fetched": 2,
        "matched": 2,
        "filtered": 0,
        "signal_candidates": 2,
        "analysis_candidates": 2,
        "accepted": 2,
        "replayed": 0,
    }
    assert second == {
        "fetched": 2,
        "matched": 2,
        "filtered": 0,
        "signal_candidates": 2,
        "analysis_candidates": 2,
        "accepted": 0,
        "replayed": 2,
    }


def test_minute_collector_stops_after_known_head_items() -> None:
    repository = MemoryNewsRepository()
    config = RssFeedConfig(source_id="interfax", url="https://example.com/feed")

    async def scenario() -> tuple[dict[str, int], datetime, datetime]:
        await collect_rss_feed(
            repository,
            config,
            fetcher=lambda url, timeout: RSS_FIXTURE,
        )
        result = await collect_rss_feed(
            repository,
            config,
            fetcher=lambda url, timeout: RSS_FIXTURE,
            stop_after_replays=1,
        )
        news = await repository.list_news(source_id="interfax", limit=10)
        return result, news[0].published_at, news[0].received_at

    result, published_at, received_at = asyncio.run(scenario())

    assert result == {
        "fetched": 2,
        "matched": 1,
        "filtered": 0,
        "signal_candidates": 1,
        "analysis_candidates": 1,
        "accepted": 0,
        "replayed": 1,
    }
    assert received_at > published_at


def test_broad_economic_news_is_sent_to_semantic_analyzer() -> None:
    class CountingAnalyzer:
        calls = 0

        async def extract(self, document: object) -> object:
            self.calls += 1
            return RuleBasedNewsExtractor().extract(document)  # type: ignore[arg-type]

    analyzer = CountingAnalyzer()
    repository = MemoryNewsRepository(analyzer=analyzer)  # type: ignore[arg-type]
    config = RssFeedConfig(source_id="interfax", url="https://example.com/feed")

    async def scenario() -> tuple[dict[str, int], int, int]:
        result = await collect_rss_feed(
            repository,
            config,
            fetcher=lambda url, timeout: RSS_FIXTURE,
            item_filter=lambda item: True,
            signal_filter=lambda item: False,
        )
        news = await repository.list_news(source_id="interfax", limit=10)
        signals = await repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=10,
        )
        return result, len(news), len(signals)

    result, news_count, signal_count = asyncio.run(scenario())

    assert result["accepted"] == 2
    assert result["signal_candidates"] == 0
    assert result["analysis_candidates"] == 2
    assert analyzer.calls == 2
    assert news_count == 2
    # Semantic analysis is auditable, but neutral context is not published as a signal.
    assert signal_count == 0


def test_semantic_router_rejects_unrelated_post() -> None:
    item = RssItem(
        external_id="misc-1",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title="Городской фестиваль открылся в парке",
        url="https://example.com/misc-1",
        content="Посетителей ждут музыка и выставки.",
        categories=(),
    )

    assert is_semantic_analysis_candidate(item) is False


def test_semantic_router_rejects_sports_story_with_market_words() -> None:
    item = RssItem(
        external_id="sports-visa",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title="Гимнастка рассказала о визовых ограничениях на чемпионате Европы",
        url="https://example.com/sports-visa",
        content="Решение о выдаче виз пересмотрели для части сборной.",
        categories=("Спорт",),
    )

    assert is_market_event_candidate(item) is False
    assert is_semantic_analysis_candidate(item) is False


@pytest.mark.parametrize(
    ("title", "content"),
    [
        (
            "Путин призвал расширить субсидии регионам на оснащение остановок",
            "Мера касается остановок в отдельных регионах.",
        ),
        (
            "Аэропорт Омска отменил один рейс и задержал еще несколько",
            "Об ограничениях сообщила транспортная прокуратура.",
        ),
    ],
)
def test_signal_router_keeps_local_context_out_of_llm(
    title: str,
    content: str,
) -> None:
    item = RssItem(
        external_id="local-context",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title=title,
        url="https://example.com/local-context",
        content=content,
        categories=("Экономика и бизнес",),
    )

    assert is_semantic_analysis_candidate(item) is True
    assert is_signal_analysis_candidate(item) is False


def test_signal_router_keeps_key_rate_for_llm() -> None:
    item = RssItem(
        external_id="key-rate",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title="Банк России изменил ключевую ставку",
        url="https://example.com/key-rate",
        content="Решение меняет денежно-кредитные условия для всего рынка.",
        categories=("Экономика и бизнес",),
    )

    assert is_signal_analysis_candidate(item) is True


@pytest.mark.parametrize(
    ("title", "content"),
    [
        (
            "Акции Русала подскочили на 5% на фоне роста цен на алюминий",
            "Движение уже произошло в ходе торгов.",
        ),
        (
            "Динамика финансовых инструментов",
            "Лидеры роста: SMLT +10%, RUAL +8%, MAGN +6%, CHMF +5%.",
        ),
    ],
)
def test_signal_router_rejects_realised_price_recap(
    title: str,
    content: str,
) -> None:
    item = RssItem(
        external_id="price-recap",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title=title,
        url="https://example.com/price-recap",
        content=content,
        categories=(),
    )

    assert is_market_signal_candidate(item) is False
    assert is_semantic_analysis_candidate(item) is False


def test_semantic_router_keeps_company_report_with_many_percentages() -> None:
    item = RssItem(
        external_id="company-report",
        published_at=datetime(2026, 8, 10, tzinfo=UTC),
        title="Рыбка идет, выручка растет",
        url="https://example.com/company-report",
        content=(
            "Инарктика представила операционный отчет: выручка +38%, "
            "объем реализации +57%, биомасса +33%. Динамика продаж улучшилась."
        ),
        categories=(),
    )

    assert is_semantic_analysis_candidate(item) is True


def test_signal_router_keeps_compact_company_financial_report_title() -> None:
    item = RssItem(
        external_id="aeroflot-report",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="📒 Финотчет Аэрофлот за 2026, 6 месяцев #AFLT",
        url="https://example.com/aeroflot-report",
        content="ПАО Аэрофлот опубликовало данные за первое полугодие.",
        categories=(),
    )

    assert is_signal_analysis_candidate(item) is True
    assert signal_analysis_priority(item) == 3


def test_moex_equity_filter_rejects_mechanical_listing_notice() -> None:
    item = RssItem(
        external_id="moex-1",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="О регистрации выпуска биржевых облигаций",
        url="https://www.moex.com/n1",
        content="Эмитент: Сбербанк. Торги проходят на Московской бирже.",
        categories=(),
    )

    assert is_watched_company_news(item) is False
    assert is_moex_equity_title(item.title) is False
    assert is_moex_equity_title("Сбербанк опубликовал финансовые результаты") is True
    assert is_moex_equity_title("О приостановке торгов ценными бумагами") is False


def test_market_candidate_requires_company_event_and_rejects_opinion() -> None:
    material = RssItem(
        external_id="market-1",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Совет директоров Яндекса рекомендовал дивиденды",
        url="https://example.com/market-1",
        content="Размер выплаты составит 110 рублей на акцию.",
        categories=("Бизнес",),
    )
    opinion = RssItem(
        external_id="market-2",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Стоит ли покупать акции Яндекса: прогноз цены",
        url="https://example.com/market-2",
        content="Автор ожидает рост котировок.",
        categories=("Мнение",),
    )

    assert is_market_signal_candidate(material) is True
    assert is_market_signal_candidate(opinion) is False
    assert is_company_news_candidate(material) is True
    assert is_company_news_candidate(opinion) is False


def test_multi_company_roundup_is_context_but_joint_event_stays_signal_candidate() -> None:
    roundup = RssItem(
        external_id="dividend-roundup",
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        title=(
            "На этой неделе дивиденды рекомендовали сразу пять компаний — "
            "НОВАТЭК, Норникель и Озон Фармацевтика"
        ),
        url="https://example.com/dividend-roundup",
        content="Сводка решений советов директоров за неделю.",
        categories=("Компании",),
    )
    joint_event = RssItem(
        external_id="joint-event",
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        title="Газпром и Газпром нефть подписали соглашение о совместном проекте",
        url="https://example.com/joint-event",
        content="Компании подтвердили заключение соглашения.",
        categories=("Компании",),
    )

    assert is_multi_company_roundup(roundup) is True
    assert is_market_event_candidate(roundup) is True
    assert is_market_signal_candidate(roundup) is False
    assert is_signal_analysis_candidate(roundup) is False
    assert signal_analysis_exclusion_reason("interfax", roundup) == "multi_company_roundup"
    assert is_multi_company_roundup(joint_event) is False
    assert is_market_signal_candidate(joint_event) is True
    assert is_signal_analysis_candidate(joint_event) is True


def test_analytical_report_review_is_context_but_report_publication_stays_candidate() -> None:
    review = RssItem(
        external_id="lkoh-report-review",
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        title=(
            "ЛУКОЙЛ: без иностранных активов прибыль все еще есть. "
            "Обзор отчета за 1-е полугодие 2026 по МСФО"
        ),
        url="https://example.com/lkoh-report-review",
        content="Автор разбирает ранее опубликованные показатели компании.",
        categories=("Компании",),
    )
    publication = RssItem(
        external_id="lkoh-report-publication",
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        title="ЛУКОЙЛ опубликовал финансовые результаты по МСФО за полугодие",
        url="https://example.com/lkoh-report-publication",
        content="Чистая прибыль компании выросла на 10%.",
        categories=("Компании",),
    )

    assert is_analytical_review(review) is True
    assert is_market_event_candidate(review) is True
    assert is_market_signal_candidate(review) is False
    assert is_signal_analysis_candidate(review) is False
    assert signal_analysis_exclusion_reason("google_news", review) == "analytical_review"
    assert is_analytical_review(publication) is False
    assert is_market_signal_candidate(publication) is True
    assert is_signal_analysis_candidate(publication) is True


def test_analysis_only_source_is_stored_as_context_without_calling_analyzer() -> None:
    analyzer = AsyncMock(side_effect=AssertionError("analysis-only source reached analyzer"))
    repository = MemoryNewsRepository(analyzer=analyzer)
    item = RssItem(
        external_id="finam-lkohl-analysis",
        published_at=datetime(2026, 8, 31, tzinfo=UTC),
        title="ЛУКОЙЛ отлично отчитался и остается качественной защитной историей",
        url="https://t.me/finamalert/1",
        content="Финам приводит аналитическое обоснование торговой идеи.",
        categories=(),
    )

    async def scenario() -> tuple[dict[str, int], dict[str, object]]:
        result = await collect_news_items(
            repository,
            source_id="telegram_finamalert",
            source_url="https://t.me/s/finamalert",
            language="ru",
            collector="telegram_public",
            items=[item],
            item_filter=lambda candidate: True,
            signal_filter=lambda candidate: True,
        )
        stored = await repository.list_news(source_id="telegram_finamalert", limit=1)
        return result, dict(stored[0].source_metadata)

    result, metadata = asyncio.run(scenario())

    analyzer.assert_not_awaited()
    assert result["matched"] == 1
    assert result["accepted"] == 1
    assert result["signal_candidates"] == 0
    assert result["analysis_candidates"] == 0
    assert metadata["event_candidate"] is True
    assert metadata["classification_status"] == "context_only"
    assert metadata["classification_reason"] == "analysis_only_source"
    assert metadata["signal_outcome"] == {
        "status": "rejected_before_analysis",
        "reason": "analysis_only_source",
        "signal_count": 0,
        "policy_version": "candidate-gate-0.7.1",
    }
    assert signal_analysis_exclusion_reason("telegram_selfinvestor", item) is None


def test_market_event_filter_keeps_sector_incident_without_company_signal() -> None:
    incident = RssItem(
        external_id="sector-incident",
        published_at=datetime(2026, 8, 9, tzinfo=UTC),
        title="БПЛА повредил склад Wildberries",
        url="https://t.me/example/1",
        content="Логистический объект временно остановил работу.",
        categories=(),
    )

    assert is_market_event_candidate(incident) is True
    assert is_market_signal_candidate(incident) is False


def test_market_candidate_rejects_promo_and_debt_noise() -> None:
    promo = RssItem(
        external_id="market-3",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Татнефть знакомит многодетные семьи с производством",
        url="https://example.com/market-3",
        content="Гости посетили предприятие.",
        categories=(),
    )
    debt = RssItem(
        external_id="market-4",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="ВТБ разместил выпуск однодневных бондов",
        url="https://example.com/market-4",
        content="Объем выпуска составил 9 млрд рублей.",
        categories=(),
    )

    assert is_market_signal_candidate(promo) is False
    assert is_market_signal_candidate(debt) is False


def test_product_noise_is_rejected_before_analyzer_but_material_news_is_kept() -> None:
    product = RssItem(
        external_id="yandex-maps-product",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Яндекс Карты теперь показывают ограничения скорости",
        url="https://example.com/yandex-maps-product",
        content="Новая функция появилась в приложении для водителей.",
        categories=("Технологии",),
    )
    material = RssItem(
        external_id="sber-results-and-product",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Сбербанк опубликовал отчётность и представил новый сервис",
        url="https://example.com/sber-results-and-product",
        content="Чистая прибыль выросла на 20%.",
        categories=("Компании",),
    )
    product_launch = RssItem(
        external_id="sber-autoleasing",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Сбербанк запустил автолизинг",
        url="https://example.com/sber-autoleasing",
        content="Услуга стала доступна клиентам банка.",
        categories=("Компании",),
    )

    assert is_obvious_company_product_or_marketing_noise(product) is True
    assert is_signal_analysis_candidate(product) is False
    assert is_obvious_company_product_or_marketing_noise(product_launch) is True
    assert is_signal_analysis_candidate(product_launch) is False
    assert is_obvious_company_product_or_marketing_noise(material) is False
    assert is_signal_analysis_candidate(material) is True


def test_product_noise_records_bounded_reason_without_calling_analyzer() -> None:
    analyzer = AsyncMock(side_effect=AssertionError("product noise reached analyzer"))
    repository = MemoryNewsRepository(analyzer=analyzer)
    item = RssItem(
        external_id="sber-contest",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Сбер учредил спецприз для конкурса стартапов",
        url="https://example.com/sber-contest",
        content="Победители получат специальный приз.",
        categories=("Компании",),
    )

    async def scenario() -> tuple[dict[str, int], dict[str, object]]:
        result = await collect_news_items(
            repository,
            source_id="interfax",
            source_url="https://example.com/feed",
            language="ru",
            collector="rss",
            items=[item],
            item_filter=lambda candidate: True,
            signal_filter=lambda candidate: True,
            analyze_signals=True,
        )
        news = await repository.list_news(source_id="interfax", limit=1)
        return result, dict(news[0].source_metadata)

    result, metadata = asyncio.run(scenario())

    analyzer.assert_not_awaited()
    assert result["signal_candidates"] == 0
    assert result["analysis_candidates"] == 0
    assert metadata["classification_status"] == "noise_filtered"
    assert metadata["signal_outcome"] == {
        "status": "rejected_before_analysis",
        "reason": "product_or_marketing_noise",
        "signal_count": 0,
        "policy_version": "candidate-gate-0.7.1",
    }


def test_signal_analysis_priority_only_promotes_material_company_events() -> None:
    standard = RssItem(
        external_id="sber-management",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Сбербанк назначил нового руководителя направления",
        url="https://example.com/sber-management",
        content="Компания сообщила о кадровом назначении.",
        categories=(),
    )
    production = RssItem(
        external_id="nornickel-production",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Норникель увеличил производство металлов",
        url="https://example.com/nornickel-production",
        content="Объем производства увеличился на 12%.",
        categories=(),
    )
    acquisition = RssItem(
        external_id="yandex-acquisition",
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        title="Яндекс приобрёл долю в технологической компании",
        url="https://example.com/yandex-acquisition",
        content="Сделка закрыта после согласования условий.",
        categories=(),
    )

    assert signal_analysis_priority(standard) == 1
    assert signal_analysis_priority(production) == 2
    assert signal_analysis_priority(acquisition) == 3


def test_cbr_filter_keeps_market_policy_and_rejects_commemorative_news() -> None:
    policy, _ = parse_rss(RSS_FIXTURE, max_items=10)
    coin = RssItem(
        external_id="cbr-coin",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Редкие монеты с редкими животными",
        url="https://www.cbr.ru/coin",
        content="Банк России выпустил памятные монеты.",
        categories=("Нумизматика",),
    )

    assert is_cbr_market_news(policy) is True
    assert is_cbr_market_news(coin) is False


def test_google_news_search_url_is_a_thirty_day_russian_feed() -> None:
    url = google_news_search_url("Сбербанк OR ВТБ")

    assert url.startswith("https://news.google.com/rss/search?")
    assert "when%3A30d" in url
    assert "ceid=RU%3Aru" in url


def test_google_candidate_requires_trusted_publisher() -> None:
    trusted = RssItem(
        external_id="google-1",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Яндекс рекомендовал дивиденды - БКС Экспресс",
        url="https://news.google.com/trusted",
        content="Выплата составит 110 рублей на акцию.",
        categories=(),
    )
    unknown = RssItem(
        external_id="google-2",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Яндекс рекомендовал дивиденды - Новости рядом",
        url="https://news.google.com/unknown",
        content="Выплата составит 110 рублей на акцию.",
        categories=(),
    )

    assert is_google_market_signal_candidate(trusted) is True
    assert is_google_market_signal_candidate(unknown) is False


def test_market_background_keeps_trusted_macro_context_without_signal_hype() -> None:
    context = RssItem(
        external_id="google-background-1",
        published_at=datetime(2026, 8, 8, tzinfo=UTC),
        title="Банк России обсудил ключевую ставку - РБК",
        url="https://news.google.com/background",
        content="Решение влияет на российский рынок акций и курс рубля.",
        categories=(),
    )

    assert is_google_market_background_candidate(context) is True


def test_composite_collector_isolates_a_failed_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()

    async def flaky_collect(
        repository: object,
        config: RssFeedConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        if config.source_id == "google_news":
            raise TimeoutError("feed unavailable")
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(collectors_module, "collect_rss_feed", flaky_collect)

    async def telegram_collect(*args: object, **kwargs: object) -> dict[str, int]:
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(collectors_module, "collect_telegram_channel", telegram_collect)

    result = asyncio.run(collectors_module.collect_market_news(repository))

    assert result == {
        "fetched": 12,
        "matched": 12,
        "filtered": 0,
        "signal_candidates": 0,
        "analysis_candidates": 0,
        "accepted": 12,
        "replayed": 0,
        "failed": 7,
    }


def test_fast_collector_uses_only_direct_feeds_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    called: list[tuple[str, int | None, bool | None, float | None]] = []
    telegram_called: list[tuple[str, int | None, bool | None, float | None]] = []

    async def fake_collect(
        repository: object,
        config: RssFeedConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        called.append(
            (
                config.source_id,
                kwargs.get("stop_after_replays"),
                kwargs.get("analyze_signals"),
                kwargs.get("timeout_seconds"),
            )
        )
        if config.source_id == "rbc":
            raise TimeoutError("feed unavailable")
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(collectors_module, "collect_rss_feed", fake_collect)

    async def fake_telegram_collect(
        repository: object,
        config: TelegramChannelConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        telegram_called.append(
            (
                config.source_id,
                kwargs.get("stop_after_replays"),
                kwargs.get("analyze_signals"),
                kwargs.get("timeout_seconds"),
            )
        )
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(
        collectors_module,
        "collect_telegram_channel",
        fake_telegram_collect,
    )

    result = asyncio.run(collectors_module.collect_fast_news(repository))

    assert called == [
        ("interfax", 1, False, 10.0),
        ("tass", 1, False, 10.0),
        ("rbc", 1, False, 10.0),
    ]
    assert telegram_called == []
    assert result == {
        "fetched": 2,
        "matched": 2,
        "filtered": 0,
        "signal_candidates": 0,
        "analysis_candidates": 0,
        "accepted": 2,
        "replayed": 0,
        "failed": 1,
    }


def test_collection_deadline_returns_partial_success_and_cancels_late_source() -> None:
    cancelled = False

    async def scenario() -> dict[str, int]:
        async def quick() -> dict[str, int]:
            return {"accepted": 1}

        async def slow() -> dict[str, int]:
            nonlocal cancelled
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return {"accepted": 1}

        return await collectors_module.collect_source_tasks(
            [("quick", quick()), ("slow", slow())],
            deadline_seconds=0.01,
        )

    result = asyncio.run(scenario())

    assert cancelled is True
    assert result["accepted"] == 1
    assert result["failed"] == 1


def test_per_source_deadline_prevents_one_feed_from_holding_lane() -> None:
    cancelled = False

    async def scenario() -> dict[str, int]:
        async def slow() -> dict[str, int]:
            nonlocal cancelled
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return {"accepted": 1}

        return await collectors_module.collect_source_tasks(
            [("slow", slow())],
            deadline_seconds=1,
            task_timeout_seconds=0.01,
        )

    result = asyncio.run(scenario())

    assert cancelled is True
    assert result["accepted"] == 0
    assert result["failed"] == 1


def test_cancelled_child_is_counted_as_source_failure() -> None:
    async def scenario() -> dict[str, int]:
        async def cancelled() -> dict[str, int]:
            raise asyncio.CancelledError

        return await collectors_module.collect_source_tasks([("cancelled", cancelled())])

    result = asyncio.run(scenario())

    assert result["accepted"] == 0
    assert result["failed"] == 1


def test_ui_managed_telegram_runs_in_five_minute_discovery_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collectors_module, "DISCOVERY_BUCKET_COUNT", 1)
    repository = MemoryNewsRepository()
    source = TelegramSourceRecord(
        source_id="telegram_private_news",
        channel="private_news",
        display_name="Частные новости",
        description="Пользовательский новостной источник",
        enabled=True,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    asyncio.run(repository.upsert_telegram_source(source))
    telegram_called: list[str] = []

    async def fake_feed_group(*args: object, **kwargs: object) -> dict[str, int]:
        return {"fetched": 1, "matched": 1, "signal_candidates": 0, "accepted": 1, "replayed": 0}

    async def fake_telegram_collect(
        repository: object,
        config: TelegramChannelConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        telegram_called.append(config.source_id)
        return {"fetched": 1, "matched": 1, "signal_candidates": 0, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(collectors_module, "collect_feed_group", fake_feed_group)
    monkeypatch.setattr(collectors_module, "collect_telegram_channel", fake_telegram_collect)

    result = asyncio.run(collectors_module.collect_discovery_news(repository))

    assert telegram_called == [
        "telegram_ak47pfl",
        "telegram_markettwits",
        "telegram_centralbank_russia",
        "telegram_moscowexchangeofficial",
        "telegram_bcs_express",
        "telegram_russianmacro",
        "telegram_private_news",
    ]
    assert result["accepted"] == 8


def test_discovery_continues_when_managed_source_registry_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collectors_module, "DISCOVERY_BUCKET_COUNT", 1)
    repository = MemoryNewsRepository()
    telegram_called: list[str] = []

    async def slow_registry() -> list[TelegramSourceRecord]:
        await asyncio.sleep(60)
        return []

    async def fake_feed_group(*args: object, **kwargs: object) -> dict[str, int]:
        return {"accepted": 1}

    async def fake_telegram_collect(
        repository: object,
        config: TelegramChannelConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        telegram_called.append(config.source_id)
        return {"accepted": 1}

    monkeypatch.setattr(repository, "list_telegram_sources", slow_registry)
    monkeypatch.setattr(collectors_module, "DISCOVERY_REGISTRY_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(collectors_module, "collect_feed_group", fake_feed_group)
    monkeypatch.setattr(collectors_module, "collect_telegram_channel", fake_telegram_collect)

    result = asyncio.run(collectors_module.collect_discovery_news(repository))

    assert telegram_called == [
        "telegram_ak47pfl",
        "telegram_markettwits",
        "telegram_centralbank_russia",
        "telegram_moscowexchangeofficial",
        "telegram_bcs_express",
        "telegram_russianmacro",
    ]
    assert result["accepted"] == 7


def test_discovery_sources_have_stable_staggered_buckets() -> None:
    source_ids = [
        *(config.config_key for config in collectors_module.DISCOVERY_NEWS_FEEDS),
        *(config.source_id for config in collectors_module.TELEGRAM_CHANNELS),
    ]

    first = {
        source_id: collectors_module.discovery_source_bucket(source_id) for source_id in source_ids
    }
    second = {
        source_id: collectors_module.discovery_source_bucket(source_id) for source_id in source_ids
    }

    assert first == second
    assert set(first.values()) == set(range(collectors_module.DISCOVERY_BUCKET_COUNT))
