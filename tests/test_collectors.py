from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from eventedge.collectors import (
    RssFeedConfig,
    RssItem,
    collect_rss_feed,
    google_news_search_url,
    is_cbr_market_news,
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

    assert first == {"fetched": 2, "matched": 2, "accepted": 2, "replayed": 0}
    assert second == {"fetched": 2, "matched": 2, "accepted": 0, "replayed": 2}


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


def test_google_news_search_url_is_a_seven_day_russian_feed() -> None:
    url = google_news_search_url("Сбербанк OR ВТБ")

    assert url.startswith("https://news.google.com/rss/search?")
    assert "when%3A7d" in url
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
