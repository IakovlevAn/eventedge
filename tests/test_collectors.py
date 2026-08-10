from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

import eventedge.collectors as collectors_module
from eventedge.collectors import (
    RssFeedConfig,
    RssItem,
    TelegramChannelConfig,
    collect_rss_feed,
    collect_telegram_channel,
    google_news_search_url,
    is_cbr_market_news,
    is_company_news_candidate,
    is_google_market_background_candidate,
    is_google_market_signal_candidate,
    is_market_event_candidate,
    is_market_signal_candidate,
    is_moex_equity_title,
    is_watched_company_news,
    parse_rss,
    parse_telegram_channel,
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
        "signal_candidates": 2,
        "accepted": 2,
        "replayed": 0,
    }
    assert second == {
        "fetched": 2,
        "matched": 2,
        "signal_candidates": 2,
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
        "signal_candidates": 1,
        "accepted": 0,
        "replayed": 1,
    }
    assert received_at > published_at


def test_broad_news_is_stored_without_calling_signal_analyzer() -> None:
    class FailAnalyzer:
        async def extract(self, document: object) -> object:
            raise AssertionError("Broad news must not call the configured LLM analyzer")

    repository = MemoryNewsRepository(analyzer=FailAnalyzer())  # type: ignore[arg-type]
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
    assert news_count == 2
    assert signal_count == 0


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
        "signal_candidates": 0,
        "accepted": 12,
        "replayed": 0,
        "failed": 7,
    }


def test_fast_collector_uses_only_direct_feeds_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    called: list[tuple[str, int | None]] = []
    telegram_called: list[tuple[str, int | None]] = []

    async def fake_collect(
        repository: object,
        config: RssFeedConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        called.append((config.source_id, kwargs.get("stop_after_replays")))
        if config.source_id == "rbc":
            raise TimeoutError("feed unavailable")
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(collectors_module, "collect_rss_feed", fake_collect)

    async def fake_telegram_collect(
        repository: object,
        config: TelegramChannelConfig,
        **kwargs: object,
    ) -> dict[str, int]:
        telegram_called.append((config.source_id, kwargs.get("stop_after_replays")))
        return {"fetched": 1, "matched": 1, "accepted": 1, "replayed": 0}

    monkeypatch.setattr(
        collectors_module,
        "collect_telegram_channel",
        fake_telegram_collect,
    )

    result = asyncio.run(collectors_module.collect_fast_news(repository))

    assert called == [
        ("interfax", 5),
        ("tass", 5),
        ("rbc", 5),
        ("moex_news", 5),
    ]
    assert telegram_called == [
        ("telegram_ak47pfl", 5),
        ("telegram_markettwits", 5),
        ("telegram_centralbank_russia", 5),
        ("telegram_moscowexchangeofficial", 5),
        ("telegram_bcs_express", 5),
        ("telegram_russianmacro", 5),
    ]
    assert result == {
        "fetched": 9,
        "matched": 9,
        "signal_candidates": 0,
        "accepted": 9,
        "replayed": 0,
        "failed": 1,
    }


def test_ui_managed_telegram_runs_in_five_minute_discovery_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    assert telegram_called == ["telegram_private_news"]
    assert result["accepted"] == 2
