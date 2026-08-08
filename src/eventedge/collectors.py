from __future__ import annotations

import asyncio
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from eventedge.analysis import NewsAnalysisInput, RuleBasedNewsExtractor
from eventedge.storage import (
    NewsDocument,
    NewsRepository,
    canonical_payload_hash,
    stable_id,
    utc_now,
)

MAX_FEED_BYTES = 8_000_000
MAX_CONTENT_LENGTH = 200_000
RSS_CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"


@dataclass(frozen=True)
class RssFeedConfig:
    source_id: str
    url: str
    language: str = "ru"
    max_items: int = 20
    max_accepted: int | None = None
    timeout_seconds: float = 15


@dataclass(frozen=True)
class RssItem:
    external_id: str
    published_at: datetime
    title: str
    url: str
    content: str
    categories: tuple[str, ...]


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "iframe"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "iframe"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def plain_text(html: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(html)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    text = re.sub(r"\s+([,.;:!?%)\]»])", r"\1", text)
    return re.sub(r"([(\[«])\s+", r"\1", text)


def parse_rss(feed: bytes, *, max_items: int) -> list[RssItem]:
    if len(feed) > MAX_FEED_BYTES:
        raise ValueError("RSS feed exceeds the configured size limit")
    root = ET.fromstring(feed)
    parsed: list[RssItem] = []
    for element in root.findall("./channel/item")[:max_items]:
        title = (element.findtext("title") or "").strip()
        url = (element.findtext("link") or "").strip()
        external_id = (element.findtext("guid") or url).strip()
        published = (element.findtext("pubDate") or "").strip()
        description = (
            element.findtext(RSS_CONTENT_TAG)
            or element.findtext("description")
            or ""
        )
        if not all((title, url, external_id, published)):
            continue

        published_at = parsedate_to_datetime(published)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        categories = tuple(
            value
            for category in element.findall("category")
            if (value := (category.text or "").strip())
        )
        content = plain_text(description) or title
        parsed.append(
            RssItem(
                external_id=external_id,
                published_at=published_at,
                title=title[:500],
                url=url,
                content=content[:MAX_CONTENT_LENGTH],
                categories=categories,
            )
        )
    return parsed


def fetch_rss(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/rss+xml, application/xml;q=0.9",
            "User-Agent": "EventEdge/0.1 (+https://github.com/IakovlevAn/eventedge)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        feed = response.read(MAX_FEED_BYTES + 1)
    if len(feed) > MAX_FEED_BYTES:
        raise ValueError("RSS feed exceeds the configured size limit")
    return feed


async def collect_rss_feed(
    repository: NewsRepository,
    config: RssFeedConfig,
    *,
    fetcher: Callable[[str, float], bytes] = fetch_rss,
    item_filter: Callable[[RssItem], bool] | None = None,
) -> dict[str, int]:
    feed = await asyncio.to_thread(fetcher, config.url, config.timeout_seconds)
    items = parse_rss(feed, max_items=config.max_items)
    received_at = utc_now()
    accepted = 0
    replayed = 0
    matched = 0

    for item in items:
        if item_filter is not None and not item_filter(item):
            continue
        matched += 1
        source_metadata = {
            "collector": "rss",
            "feed_url": config.url,
            "categories": list(item.categories),
        }
        hash_payload = {
            "source_id": config.source_id,
            "external_id": item.external_id,
            "published_at": item.published_at.isoformat(),
            "title": item.title,
            "url": item.url,
            "content": item.content,
            "language": config.language,
            "source_metadata": source_metadata,
        }
        payload_hash = canonical_payload_hash(hash_payload)
        document = NewsDocument(
            source_id=config.source_id,
            external_id=item.external_id,
            published_at=item.published_at,
            received_at=received_at,
            title=item.title,
            url=item.url,
            content=item.content,
            language=config.language,
            source_metadata=source_metadata,
            payload_hash=payload_hash,
        )
        idempotency_key = stable_id(
            "ing_",
            f"{config.source_id}\x00{item.external_id}\x00{payload_hash}",
        )
        result = await repository.ingest(idempotency_key, document)
        if result.replayed:
            replayed += 1
        else:
            accepted += 1
            if config.max_accepted is not None and accepted >= config.max_accepted:
                break

    return {
        "fetched": len(items),
        "matched": matched,
        "accepted": accepted,
        "replayed": replayed,
    }


CBR_PRESS_FEED = RssFeedConfig(
    source_id="cbr_press",
    url="https://www.cbr.ru/rss/RssPress",
    max_items=10,
)


async def collect_cbr_press(repository: NewsRepository) -> dict[str, int]:
    return await collect_rss_feed(repository, CBR_PRESS_FEED)


MOEX_NEWS_FEED = RssFeedConfig(
    source_id="moex_news",
    url="https://www.moex.com/export/news.aspx?cat=100",
    max_items=300,
    max_accepted=3,
    timeout_seconds=20,
)


def is_watched_company_news(item: RssItem) -> bool:
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="moex_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    return bool(features.instruments)


async def collect_moex_news(repository: NewsRepository) -> dict[str, int]:
    return await collect_rss_feed(
        repository,
        MOEX_NEWS_FEED,
        item_filter=is_watched_company_news,
    )
