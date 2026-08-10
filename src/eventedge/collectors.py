from __future__ import annotations

import asyncio
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from eventedge.analysis import EventType, NewsAnalysisInput, RuleBasedNewsExtractor
from eventedge.configs.sources import (
    RssFeedConfig,
    TelegramChannelConfig,
    load_source_config,
)
from eventedge.configs.sources import (
    google_news_search_url as configured_google_news_search_url,
)
from eventedge.events import is_broad_market_event_text
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
HTML_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


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


def google_news_search_url(query: str) -> str:
    """Keep the existing collector helper API while its configuration moves out."""
    return configured_google_news_search_url(query)


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
            full_text or element.findtext(RSS_CONTENT_TAG) or element.findtext("description") or ""
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


class _TelegramChannelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[RssItem] = []
        self._message_depth = 0
        self._text_depth = 0
        self._external_id: str | None = None
        self._published_at: datetime | None = None
        self._url: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        is_void = tag in HTML_VOID_TAGS

        if not self._message_depth:
            external_id = attributes.get("data-post")
            if tag == "div" and "tgme_widget_message" in classes and external_id:
                self._message_depth = 1
                self._external_id = external_id
                self._published_at = None
                self._url = f"https://t.me/{external_id}"
                self._text_parts = []
            return

        if not is_void:
            self._message_depth += 1

        if self._text_depth:
            if not is_void:
                self._text_depth += 1
            if tag == "br":
                self._text_parts.append("\n")
        elif tag == "div" and "tgme_widget_message_text" in classes:
            self._text_depth = 1

        if tag == "time" and (published := attributes.get("datetime")):
            try:
                parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                if parsed.tzinfo is not None:
                    self._published_at = parsed

        if tag == "a" and "tgme_widget_message_date" in classes:
            self._url = attributes.get("href") or self._url

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self._text_depth and tag == "br":
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._message_depth:
            return
        if self._text_depth:
            self._text_depth -= 1
        self._message_depth -= 1
        if not self._message_depth:
            self._finish_message()

    def handle_data(self, data: str) -> None:
        if self._text_depth:
            self._text_parts.append(data)

    def _finish_message(self) -> None:
        content = "\n".join(
            line.strip() for line in "".join(self._text_parts).splitlines() if line.strip()
        )
        if self._external_id and self._published_at and self._url and content:
            title = content.splitlines()[0][:500]
            self.items.append(
                RssItem(
                    external_id=self._external_id,
                    published_at=self._published_at,
                    title=title,
                    url=self._url,
                    content=content[:MAX_CONTENT_LENGTH],
                    categories=(),
                )
            )
        self._external_id = None
        self._published_at = None
        self._url = None
        self._text_parts = []


def parse_telegram_channel(page: bytes, *, max_items: int) -> list[RssItem]:
    if len(page) > MAX_FEED_BYTES:
        raise ValueError("Telegram channel page exceeds the configured size limit")
    parser = _TelegramChannelParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    parser.close()
    return sorted(parser.items, key=lambda item: item.published_at, reverse=True)[:max_items]


def fetch_telegram_channel(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "EventEdge/0.1 (+https://github.com/IakovlevAn/eventedge)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        page = response.read(MAX_FEED_BYTES + 1)
    if len(page) > MAX_FEED_BYTES:
        raise ValueError("Telegram channel page exceeds the configured size limit")
    return page


async def collect_news_items(
    repository: NewsRepository,
    *,
    source_id: str,
    source_url: str,
    language: str,
    collector: str,
    items: list[RssItem],
    item_filter: Callable[[RssItem], bool] | None = None,
    signal_filter: Callable[[RssItem], bool] | None = None,
    stop_after_replays: int | None = None,
) -> dict[str, int]:
    accepted = 0
    replayed = 0
    matched = 0
    filtered = 0
    signal_candidates = 0
    consecutive_replays = 0

    for item in items:
        event_candidate = item_filter(item) if item_filter is not None else True
        if event_candidate:
            matched += 1
        else:
            filtered += 1
        generate_signals = event_candidate and (
            signal_filter(item) if signal_filter is not None else True
        )
        if generate_signals:
            signal_candidates += 1
        features = RuleBasedNewsExtractor().extract(
            NewsAnalysisInput(
                source_id=source_id,
                title=item.title,
                content=item.content,
                language=language,
            )
        )
        # The legacy metadata subset remains the payload-hash contract. This
        # prevents a one-time re-ingestion of every existing article when
        # lossless classification metadata is added. New items rejected by the
        # old gate are still persisted because they never had an ingestion key.
        hash_metadata: dict[str, object] = {
            "collector": collector,
            "categories": list(item.categories),
            "signal_candidate": generate_signals,
            "tickers": [
                instrument.ticker
                for instrument in features.instruments
                if instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
            ],
        }
        # Keep the existing RSS payload shape stable so a deployment does not
        # re-ingest all known feed items merely because Telegram was added.
        if collector == "rss":
            hash_metadata["feed_url"] = source_url
        else:
            hash_metadata["channel_url"] = source_url
        classification_status = (
            "signal_candidate"
            if generate_signals
            else "event_candidate"
            if event_candidate
            else "unclassified"
        )
        classification_reason = (
            "eligible_for_signal_analysis"
            if generate_signals
            else "stored_as_market_context"
            if event_candidate
            else "stored_for_future_reclassification"
        )
        source_metadata = {
            **hash_metadata,
            "processing_status": "processed",
            "classification_status": classification_status,
            "classification_reason": classification_reason,
            "classification_version": "candidate-gate-0.2.0",
            "event_candidate": event_candidate,
        }
        hash_payload = {
            "source_id": source_id,
            "external_id": item.external_id,
            "published_at": item.published_at.isoformat(),
            "title": item.title,
            "url": item.url,
            "content": item.content,
            "language": language,
            "source_metadata": hash_metadata,
        }
        payload_hash = canonical_payload_hash(hash_payload)
        document = NewsDocument(
            source_id=source_id,
            external_id=item.external_id,
            published_at=item.published_at,
            received_at=datetime.now(UTC),
            title=item.title,
            url=item.url,
            content=item.content,
            language=language,
            source_metadata=source_metadata,
            payload_hash=payload_hash,
        )
        idempotency_key = stable_id(
            "ing_",
            f"{source_id}\x00{item.external_id}\x00{payload_hash}",
        )
        result = await repository.ingest(
            idempotency_key,
            document,
            generate_signals=generate_signals,
        )
        if result.replayed:
            replayed += 1
            consecutive_replays += 1
            if stop_after_replays is not None and consecutive_replays >= stop_after_replays:
                break
        else:
            accepted += 1
            consecutive_replays = 0

    return {
        "fetched": len(items),
        "matched": matched,
        "filtered": filtered,
        "signal_candidates": signal_candidates,
        "accepted": accepted,
        "replayed": replayed,
    }


async def collect_rss_feed(
    repository: NewsRepository,
    config: RssFeedConfig,
    *,
    fetcher: Callable[[str, float], bytes] = fetch_rss,
    item_filter: Callable[[RssItem], bool] | None = None,
    signal_filter: Callable[[RssItem], bool] | None = None,
    stop_after_replays: int | None = None,
) -> dict[str, int]:
    feed = await asyncio.to_thread(fetcher, config.url, config.timeout_seconds)
    items = parse_rss(feed, max_items=config.max_items)
    return await collect_news_items(
        repository,
        source_id=config.source_id,
        source_url=config.url,
        language=config.language,
        collector="rss",
        items=items,
        item_filter=item_filter,
        signal_filter=signal_filter,
        stop_after_replays=stop_after_replays,
    )


async def collect_telegram_channel(
    repository: NewsRepository,
    config: TelegramChannelConfig,
    *,
    fetcher: Callable[[str, float], bytes] = fetch_telegram_channel,
    item_filter: Callable[[RssItem], bool] | None = None,
    signal_filter: Callable[[RssItem], bool] | None = None,
    stop_after_replays: int | None = None,
) -> dict[str, int]:
    page = await asyncio.to_thread(fetcher, config.url, config.timeout_seconds)
    items = parse_telegram_channel(page, max_items=config.max_items)
    return await collect_news_items(
        repository,
        source_id=config.source_id,
        source_url=config.url,
        language=config.language,
        collector="telegram_public",
        items=items,
        item_filter=item_filter,
        signal_filter=signal_filter,
        stop_after_replays=stop_after_replays,
    )


SOURCE_CONFIG = load_source_config()
CBR_PRESS_FEED = SOURCE_CONFIG.cbr_press

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
        signal_filter=lambda item: False,
    )


MOEX_NEWS_FEED = SOURCE_CONFIG.moex_news

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
    "цена акций",
    "котировки выросли",
    "котировки упали",
    "котировки снизились",
    "обновили максимум",
    "обзор размещения",
    "идеи на первичном рынке",
    "итоги недели",
    "итоги торгов",
    "главное к открытию",
    "прогнозы и комментарии",
    "мнение аналитиков",
    "мнение экспертов",
    "инвестиционный кейс",
    "идея в профите",
    "спекулятивн",
    "выделяем акции",
    "привлекательны для инвесторов",
    "повысили целев",
    "повысил целев",
    "снизили целев",
    "целевую цену",
    "таргет по акци",
    "рейтинг «покупать»",
    "рейтинг покупать",
    "рекомендуем покупать",
    "наш выбор",
    "топ акци",
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
    "иск ",
    "штраф",
    "расследован",
    "арест",
    "обыск",
    "директор",
    "назначил",
    "покинул",
    "партнерств",
    "партнёрств",
    "соглашени",
    "контракт",
    "объединил",
    "приостанов",
    "возобнов",
    "авари",
    "пожар",
    "поставк",
    "экспорт",
    "налог",
    "рекомендовал",
    "утвердил",
    "одобрил",
    "подтвердил прогноз",
    "отменил",
    "капзатрат",
    "инвестпрограмм",
    "допэмисс",
    "эмисси",
    "кредитн рейтинг",
    "рейтинг",
)


def is_market_signal_candidate(item: RssItem) -> bool:
    """High-recall gate for direct company events before semantic analysis.

    The gate only decides whether analysis is worth running. The semantic
    extractor and deterministic scorer still decide direction and strength.
    """
    if not is_moex_equity_title(item.title):
        return False
    normalized_context = f"{item.title} {item.content[:1200]}".casefold()
    if any(marker in normalized_context for marker in MARKET_NOISE_TITLE_MARKERS):
        return False
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    direct_instruments = [
        instrument for instrument in features.instruments if instrument.relevance >= 0.9
    ]
    if not direct_instruments:
        return False
    normalized_context = f"{item.title} {item.content[:1500]}".casefold()
    if (
        not any(marker in normalized_context for marker in MARKET_EVENT_MARKERS)
        and features.event_type is EventType.OTHER
    ):
        return False
    if all(instrument.ticker == "MOEX" for instrument in direct_instruments):
        return features.event_type in {
            EventType.FINANCIAL_RESULTS,
            EventType.DIVIDEND,
            EventType.MANAGEMENT,
        }
    return True


def is_company_news_candidate(item: RssItem) -> bool:
    """Keep readable company news even when it is not strong enough for a signal."""
    if not is_moex_equity_title(item.title):
        return False
    normalized_title = item.title.casefold()
    if any(marker in normalized_title for marker in MARKET_NOISE_TITLE_MARKERS):
        return False
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    return any(
        instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
        for instrument in features.instruments
    )


def is_market_event_candidate(item: RssItem) -> bool:
    """Keep company, sector and country-level events without spending LLM budget."""
    normalized_title = item.title.casefold()
    if any(marker in normalized_title for marker in MARKET_NOISE_TITLE_MARKERS):
        return False
    return is_company_news_candidate(item) or is_broad_market_event_text(
        item.title,
        item.content,
    )


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


def is_google_company_news_candidate(item: RssItem) -> bool:
    if not is_company_news_candidate(item):
        return False
    normalized_title = item.title.casefold()
    return any(publisher in normalized_title for publisher in GOOGLE_TRUSTED_PUBLISHERS)


MARKET_BACKGROUND_MARKERS = (
    "ключевая ставка",
    "банк россии",
    "курс рубл",
    "российский рынок",
    "рынок акций",
    "мосбирж",
    "индекс imoex",
    "цены на нефть",
    "нефть brent",
    "санкции против россии",
)


def is_google_market_background_candidate(item: RssItem) -> bool:
    normalized = " ".join((item.title, item.content)).casefold()
    if any(marker in normalized for marker in MARKET_NOISE_TITLE_MARKERS):
        return False
    trusted_publisher = any(
        publisher in item.title.casefold() for publisher in GOOGLE_TRUSTED_PUBLISHERS
    )
    return trusted_publisher and any(marker in normalized for marker in MARKET_BACKGROUND_MARKERS)


MARKET_NEWS_FEEDS = SOURCE_CONFIG.market_news_feeds
FAST_NEWS_FEEDS = SOURCE_CONFIG.fast_news_feeds
TELEGRAM_CHANNELS = SOURCE_CONFIG.telegram_channels
DISCOVERY_NEWS_FEEDS = SOURCE_CONFIG.discovery_news_feeds


def collection_filters(
    config: RssFeedConfig,
) -> tuple[Callable[[RssItem], bool], Callable[[RssItem], bool]]:
    if config.source_id == "google_news":
        return is_google_company_news_candidate, is_google_market_signal_candidate
    if config.source_id == "market_background":
        return is_google_market_background_candidate, lambda item: False
    return is_market_event_candidate, is_market_signal_candidate


def empty_collection_totals() -> dict[str, int]:
    return {
        "fetched": 0,
        "matched": 0,
        "filtered": 0,
        "signal_candidates": 0,
        "accepted": 0,
        "replayed": 0,
        "failed": 0,
    }


def aggregate_collection_results(
    results: list[dict[str, int] | BaseException],
) -> dict[str, int]:
    totals = empty_collection_totals()
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


async def collect_feed_group(
    repository: NewsRepository,
    feeds: tuple[RssFeedConfig, ...],
    *,
    stop_after_replays: int | None,
) -> dict[str, int]:
    results = await asyncio.gather(
        *(
            collect_rss_feed(
                repository,
                config,
                item_filter=collection_filters(config)[0],
                signal_filter=collection_filters(config)[1],
                stop_after_replays=stop_after_replays,
            )
            for config in feeds
        ),
        return_exceptions=True,
    )
    return aggregate_collection_results(results)


async def collect_fast_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh direct priority feeds every minute without discovery latency."""
    results = await asyncio.gather(
        collect_feed_group(
            repository,
            FAST_NEWS_FEEDS,
            stop_after_replays=5,
        ),
        *(
            collect_telegram_channel(
                repository,
                config,
                item_filter=is_market_event_candidate,
                signal_filter=is_market_signal_candidate,
                stop_after_replays=5,
            )
            for config in TELEGRAM_CHANNELS
        ),
        return_exceptions=True,
    )
    return aggregate_collection_results(results)


async def collect_discovery_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh broad discovery and UI-managed Telegram every five minutes."""
    managed_sources = await repository.list_telegram_sources()
    dynamic_channels = tuple(
        TelegramChannelConfig(
            source_id=source.source_id,
            name=source.display_name,
            channel=source.channel,
            max_items=20,
            timeout_seconds=10,
        )
        for source in managed_sources
        if source.enabled
    )
    results = await asyncio.gather(
        collect_feed_group(
            repository,
            DISCOVERY_NEWS_FEEDS,
            stop_after_replays=10,
        ),
        *(
            collect_telegram_channel(
                repository,
                config,
                item_filter=is_market_event_candidate,
                signal_filter=is_market_signal_candidate,
                stop_after_replays=5,
            )
            for config in dynamic_channels
        ),
        return_exceptions=True,
    )
    return aggregate_collection_results(results)


async def collect_slow_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh macro context that does not require minute-level polling."""
    result = await asyncio.gather(
        collect_rss_feed(
            repository,
            CBR_PRESS_FEED,
            item_filter=is_cbr_market_news,
            signal_filter=lambda item: False,
            stop_after_replays=5,
        ),
        return_exceptions=True,
    )
    return aggregate_collection_results(result)


async def collect_moex_news(repository: NewsRepository) -> dict[str, int]:
    return await collect_rss_feed(
        repository,
        MOEX_NEWS_FEED,
        item_filter=is_market_signal_candidate,
    )


async def collect_market_news(repository: NewsRepository) -> dict[str, int]:
    """Backfill and refresh the complete low-cost market news surface."""
    results = await asyncio.gather(
        *(
            collect_rss_feed(
                repository,
                config,
                item_filter=collection_filters(config)[0],
                signal_filter=collection_filters(config)[1],
            )
            for config in MARKET_NEWS_FEEDS
        ),
        *(
            collect_telegram_channel(
                repository,
                config,
                item_filter=is_market_event_candidate,
                signal_filter=is_market_signal_candidate,
            )
            for config in TELEGRAM_CHANNELS
        ),
        collect_cbr_press(repository),
        collect_moex_news(repository),
        return_exceptions=True,
    )
    return aggregate_collection_results(results)
