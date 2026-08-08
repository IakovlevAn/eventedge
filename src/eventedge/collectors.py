from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from eventedge.analysis import EventType, NewsAnalysisInput, RuleBasedNewsExtractor
from eventedge.storage import (
    NewsDocument,
    NewsRepository,
    canonical_payload_hash,
    stable_id,
)

MAX_FEED_BYTES = 8_000_000
MAX_CONTENT_LENGTH = 200_000
RSS_CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"
LOGGER = logging.getLogger(__name__)


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
        full_text = next(
            (
                child.text
                for child in element
                if child.tag.rsplit("}", 1)[-1] == "full-text" and child.text
            ),
            None,
        )
        description = (
            full_text
            or element.findtext(RSS_CONTENT_TAG)
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
    return sorted(parsed, key=lambda item: item.published_at, reverse=True)


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
            received_at=item.published_at,
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
    max_accepted=3,
)

CBR_MARKET_MARKERS = (
    "ключевая ставка",
    "денежно-кредитн",
    "инфляц",
    "валют",
    "курс рубл",
    "банковский сектор",
    "банковская статистика",
    "ликвидност",
    "кредитован",
    "капитала банк",
    "регулирован",
    "надзор",
)


def is_cbr_market_news(item: RssItem) -> bool:
    context = " ".join((item.title, item.content, *item.categories)).casefold()
    return any(marker in context for marker in CBR_MARKET_MARKERS)


async def collect_cbr_press(repository: NewsRepository) -> dict[str, int]:
    return await collect_rss_feed(
        repository,
        CBR_PRESS_FEED,
        item_filter=is_cbr_market_news,
    )


MOEX_NEWS_FEED = RssFeedConfig(
    source_id="moex_news",
    url="https://www.moex.com/export/news.aspx?cat=100",
    max_items=1000,
    max_accepted=3,
    timeout_seconds=20,
)

MOEX_NON_EQUITY_TITLE_MARKERS = (
    "облигац",
    "операциям репо",
    "риск-параметр",
    "индексными фьючерсами",
    "индикативн",
    "тестового полигона",
    "нагрузочное тестирование",
    "о начале торгов ценными бумагами",
    "об оставлении ценных бумаг в списке",
    "о внесении изменений в список ценных бумаг",
    "об исключении ценных бумаг из списка",
    "о дополнительных условиях проведения торгов ценными бумагами",
    "об изменении уровня листинга ценных бумаг",
    "о приостановке торгов",
    "изменены значения верхней границы ценового коридора",
    "изменены значения нижней границы ценового коридора",
)


def is_moex_equity_title(title: str) -> bool:
    normalized = title.casefold()
    return not any(marker in normalized for marker in MOEX_NON_EQUITY_TITLE_MARKERS)


def is_watched_company_news(item: RssItem) -> bool:
    if not is_moex_equity_title(item.title):
        return False
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="moex_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    return bool(features.instruments)


MARKET_NOISE_TITLE_MARKERS = (
    "прогноз по цене акций",
    "стоит ли покупать",
    "технический анализ",
    "целевая цена",
    "акции могут вырасти",
    "обзор размещения",
    "идеи на первичном рынке",
    "итоги недели",
    "арене",
    "арена",
    "футбол",
    "матч",
    "многодетн",
    "знакомит с производством",
    "в честь пятилетия",
    "в честь юбилея",
    "самокат",
    "воса",
    "реестр акционеров для участия",
    "реестр кредиторов",
    "облигац",
    "бондов",
    "благотвор",
    "фестивал",
)

MARKET_EVENT_MARKERS = (
    "дивиденд",
    "отчетност",
    "отчётност",
    "финансовые результат",
    "прибыл",
    "выручк",
    "ebitda",
    "санкц",
    "ограничен",
    "лицензи",
    "пошлин",
    "добыч",
    "производств",
    "запустил",
    "увелич",
    "снизил",
    "сократил",
    "сделк",
    "продал",
    "купил",
    "банкрот",
    "суд ",
    "директор",
    "партнерств",
    "партнёрств",
    "соглашени",
    "объединил",
    "приостанов",
    "возобнов",
    "авари",
    "пожар",
    "поставк",
    "экспорт",
    "налог",
)


def is_market_signal_candidate(item: RssItem) -> bool:
    """Keep direct, event-like company news and reject opinion/sports collisions."""
    if not is_moex_equity_title(item.title):
        return False
    normalized_title = item.title.casefold()
    if any(marker in normalized_title for marker in MARKET_NOISE_TITLE_MARKERS):
        return False
    if not any(marker in normalized_title for marker in MARKET_EVENT_MARKERS):
        return False
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    if not features.instruments or features.event_type is EventType.OTHER:
        return False
    if all(item.ticker == "MOEX" for item in features.instruments):
        return features.event_type in {
            EventType.FINANCIAL_RESULTS,
            EventType.DIVIDEND,
            EventType.MANAGEMENT,
        }
    return True


GOOGLE_TRUSTED_PUBLISHERS = (
    "бкс экспресс",
    "интерфакс",
    "тасс",
    "рбк",
    "ведомости",
    "коммерсант",
    "риа новости",
    "прайм",
    "forbes",
    "финам",
    "frank media",
    "банки.ру",
    "агентство бизнес новостей",
    "энергетика и промышленность россии",
    "нефть и капитал",
)


def is_google_market_signal_candidate(item: RssItem) -> bool:
    if not is_market_signal_candidate(item):
        return False
    normalized_title = item.title.casefold()
    return any(publisher in normalized_title for publisher in GOOGLE_TRUSTED_PUBLISHERS)


def google_news_search_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"({query}) when:7d", "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    )


MARKET_NEWS_FEEDS = (
    RssFeedConfig(
        source_id="google_news",
        url=google_news_search_url("Сбербанк OR ВТБ"),
        max_items=100,
        max_accepted=2,
    ),
    RssFeedConfig(
        source_id="google_news",
        url=google_news_search_url(
            "Лукойл OR Газпром OR Роснефть OR Новатэк OR Татнефть"
        ),
        max_items=100,
        max_accepted=2,
    ),
    RssFeedConfig(
        source_id="google_news",
        url=google_news_search_url(
            "Яндекс OR Норникель OR Магнит OR Полюс OR Северсталь OR АЛРОСА"
        ),
        max_items=100,
        max_accepted=2,
    ),
    RssFeedConfig(
        source_id="interfax",
        url="https://www.interfax.ru/rss",
        max_items=50,
        max_accepted=3,
    ),
    RssFeedConfig(
        source_id="tass",
        url="https://tass.ru/rss/v2.xml",
        max_items=100,
        max_accepted=3,
    ),
    RssFeedConfig(
        source_id="rbc",
        url="https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
        max_items=50,
        max_accepted=3,
    ),
)


async def collect_moex_news(repository: NewsRepository) -> dict[str, int]:
    return await collect_rss_feed(
        repository,
        MOEX_NEWS_FEED,
        item_filter=is_market_signal_candidate,
    )


async def collect_market_news(repository: NewsRepository) -> dict[str, int]:
    """Backfill and refresh the complete low-cost market news surface."""
    totals = {
        "fetched": 0,
        "matched": 0,
        "accepted": 0,
        "replayed": 0,
        "failed": 0,
    }
    results = await asyncio.gather(
        *(
            collect_rss_feed(
                repository,
                config,
                item_filter=(
                    is_google_market_signal_candidate
                    if config.source_id == "google_news"
                    else is_market_signal_candidate
                ),
            )
            for config in MARKET_NEWS_FEEDS
        ),
        collect_cbr_press(repository),
        collect_moex_news(repository),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            totals["failed"] += 1
            LOGGER.warning(
                "Market feed collection failed: %s",
                type(result).__name__,
            )
            continue
        for key in totals:
            totals[key] += result.get(key, 0)
    return totals
