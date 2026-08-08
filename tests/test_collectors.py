from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

import eventedge.collectors as collectors_module
from eventedge.collectors import (
    RssFeedConfig,
    RssItem,
    collect_rss_feed,
    google_news_search_url,
    is_cbr_market_news,
    is_company_news_candidate,
    is_google_market_signal_candidate,
    is_market_signal_candidate,
    is_moex_equity_title,
    is_watched_company_news,
    parse_rss,
)
from eventedge.storage import MemoryNewsRepository

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


def test_rss_parser_extracts_clean_text_and_metadata() -> None:
    items = parse_rss(RSS_FIXTURE, max_items=10)

    assert len(items) == 2
    assert items[0].external_id == "docid_1"
    assert items[0].content == "Ставка сохранена на уровне 12%."
    assert items[0].published_at.isoformat() == "2026-08-07T13:30:00+03:00"
    assert items[0].categories == ("Денежно-кредитная политика",)


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

    result = asyncio.run(collectors_module.collect_market_news(repository))

    assert result == {
        "fetched": 5,
        "matched": 5,
        "signal_candidates": 0,
        "accepted": 5,
        "replayed": 0,
        "failed": 3,
    }
