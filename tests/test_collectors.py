from __future__ import annotations

import asyncio

from eventedge.collectors import RssFeedConfig, collect_rss_feed, parse_rss
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
