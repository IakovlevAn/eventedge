from __future__ import annotations

import asyncio
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor, ThreadPoolExecutor
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
from eventedge.events import (
    classify_news_event,
    context_signal_specs,
    is_broad_market_event_text,
)
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
COLLECTION_LANE_DEADLINE_SECONDS = 20.0
FAST_SOURCE_TIMEOUT_SECONDS = 10.0
FAST_SOURCE_DEADLINE_SECONDS = 15.0
DISCOVERY_SOURCE_DEADLINE_SECONDS = 12.0
DISCOVERY_REGISTRY_DEADLINE_SECONDS = 3.0
DISCOVERY_BUCKET_COUNT = 5
CANDIDATE_POLICY_VERSION = "candidate-gate-0.7.0"
CONTEXT_ONLY_SOURCE_IDS = frozenset(
    {
        "telegram_bitkogan",
        "telegram_finamalert",
        "telegram_mozgovikresearch",
        "telegram_spydell_finance",
        "telegram_tb_invest_official",
        "telegram_truevalue",
    }
)
PRIORITY_FEED_FETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="eventedge-priority-feed",
)
BACKGROUND_FEED_FETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=12,
    thread_name_prefix="eventedge-background-feed",
)
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
    analyze_signals: bool = True,
) -> dict[str, int]:
    accepted = 0
    replayed = 0
    matched = 0
    filtered = 0
    signal_candidates = 0
    analysis_candidates = 0
    consecutive_replays = 0

    for item in items:
        product_marketing_noise = is_obvious_company_product_or_marketing_noise(item)
        analysis_exclusion_reason = signal_analysis_exclusion_reason(source_id, item)
        event_candidate = item_filter(item) if item_filter is not None else True
        if event_candidate:
            matched += 1
        else:
            filtered += 1
        direct_signal_candidate = (
            event_candidate
            and not product_marketing_noise
            and analysis_exclusion_reason is None
            and (signal_filter(item) if signal_filter is not None else True)
        )
        if direct_signal_candidate:
            signal_candidates += 1
        analysis_candidate = (
            not product_marketing_noise
            and analysis_exclusion_reason is None
            and (direct_signal_candidate or is_signal_analysis_candidate(item))
        )
        if analysis_candidate:
            analysis_candidates += 1
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
            "signal_candidate": direct_signal_candidate,
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
            "noise_filtered"
            if product_marketing_noise
            else "context_only"
            if analysis_exclusion_reason is not None
            else "signal_candidate"
            if direct_signal_candidate
            else "semantic_candidate"
            if analysis_candidate
            else "event_candidate"
            if event_candidate
            else "unclassified"
        )
        classification_reason = (
            "obvious_company_product_or_marketing_noise"
            if product_marketing_noise
            else analysis_exclusion_reason
            if analysis_exclusion_reason is not None
            else "eligible_for_direct_signal_analysis"
            if direct_signal_candidate
            else "eligible_for_semantic_signal_analysis"
            if analysis_candidate
            else "stored_as_market_context"
            if event_candidate
            else "stored_for_future_reclassification"
        )
        source_metadata = {
            **hash_metadata,
            "processing_status": (
                "processed" if analyze_signals or not analysis_candidate else "pending"
            ),
            "classification_status": classification_status,
            "classification_reason": classification_reason,
            "classification_version": CANDIDATE_POLICY_VERSION,
            "event_candidate": event_candidate,
            "analysis_candidate": analysis_candidate,
            **(
                {
                    "signal_outcome": {
                        "status": "rejected_before_analysis",
                        "reason": "product_or_marketing_noise",
                        "signal_count": 0,
                        "policy_version": CANDIDATE_POLICY_VERSION,
                    }
                }
                if product_marketing_noise
                else {
                    "signal_outcome": {
                        "status": "rejected_before_analysis",
                        "reason": analysis_exclusion_reason,
                        "signal_count": 0,
                        "policy_version": CANDIDATE_POLICY_VERSION,
                    }
                }
                if analysis_exclusion_reason is not None
                else {}
            ),
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
            generate_signals=analyze_signals and analysis_candidate,
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
        "analysis_candidates": analysis_candidates,
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
    analyze_signals: bool = True,
    timeout_seconds: float | None = None,
    executor: Executor = BACKGROUND_FEED_FETCH_EXECUTOR,
) -> dict[str, int]:
    feed = await asyncio.get_running_loop().run_in_executor(
        executor,
        fetcher,
        config.url,
        timeout_seconds if timeout_seconds is not None else config.timeout_seconds,
    )
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
        analyze_signals=analyze_signals,
    )


async def collect_telegram_channel(
    repository: NewsRepository,
    config: TelegramChannelConfig,
    *,
    fetcher: Callable[[str, float], bytes] = fetch_telegram_channel,
    item_filter: Callable[[RssItem], bool] | None = None,
    signal_filter: Callable[[RssItem], bool] | None = None,
    stop_after_replays: int | None = None,
    analyze_signals: bool = True,
    timeout_seconds: float | None = None,
    executor: Executor = BACKGROUND_FEED_FETCH_EXECUTOR,
) -> dict[str, int]:
    page = await asyncio.get_running_loop().run_in_executor(
        executor,
        fetcher,
        config.url,
        timeout_seconds if timeout_seconds is not None else config.timeout_seconds,
    )
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
        analyze_signals=analyze_signals,
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
    "выглядит заниженной",
    "выглядят заниженными",
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
    "лидеры роста",
    "лидеры падения",
    "динамика финансовых инструментов",
    "динамика акций",
    "топ растущих акций",
)

NON_MARKET_CATEGORY_MARKERS = (
    "спорт",
    "культура",
    "шоу-бизнес",
)

PRICE_REACTION_TITLE_PATTERN = re.compile(
    r"(?:акци[ия]|котировк[аи]|#[A-ZА-Я0-9]{2,12}).{0,80}"
    r"(?:подскочил|выросл|рост|упал|снизил|прибавил|потерял|"
    r"подорожал|подешевел|обновил[аи]? максимум)",
    re.IGNORECASE,
)

OBVIOUS_COMPANY_PRODUCT_OR_MARKETING_MARKERS = (
    "акция для клиентов",
    "благотвор",
    "в честь юбилея",
    "кешбэк",
    "кэшбэк",
    "конкурс",
    "новая функция",
    "новое приложение",
    "новый продукт",
    "новый сервис",
    "обновил приложение",
    "обновила приложение",
    "опрос клиентов",
    "представил сервис",
    "представила сервис",
    "спецприз",
    "спрос на ",
    "скидк",
    "теперь показывает",
    "теперь показывают",
    "фестивал",
    "запустил сервис",
    "запустила сервис",
)

MATERIAL_COMPANY_EVENT_MARKERS = (
    "дивиденд",
    "отчетност",
    "отчётност",
    "финансовые результат",
    "чистая прибыль",
    "чистый убыт",
    "выручк",
    "ebitda",
    "мсфо",
    "рсбу",
    "санкц",
    "эмбарго",
    "блокирующ",
    "приобрел долю",
    "приобрёл долю",
    "продал долю",
    "покупк бизнеса",
    "продаж бизнеса",
    "слияни",
    "поглощени",
    "остановил производств",
    "приостановил производств",
    "возобновил производств",
    "сократил производств",
    "увеличил производств",
    "добыча выросла",
    "добыча снизилась",
)

MATERIAL_MA_MARKERS = (
    "приобрел долю",
    "приобрёл долю",
    "продал долю",
    "покупк бизнеса",
    "продаж бизнеса",
    "слияни",
    "поглощени",
)

FINANCIAL_RESULTS_PRIORITY_MARKERS = (
    "финансовые результат",
    "финотчет",
    "финотчёт",
    "чистая прибыль",
    "чистый убыт",
    "выручк",
    "ebitda",
    "мсфо",
    "рсбу",
)

SANCTIONS_PRIORITY_MARKERS = (
    "санкц",
    "эмбарго",
    "блокирующ",
)

PRODUCTION_PRIORITY_MARKERS = (
    "добыча выросла",
    "добыча снизилась",
    "запустил производств",
    "запустила производств",
    "остановил производств",
    "приостановил производств",
    "возобновил производств",
    "сократил производств",
    "увеличил производств",
)


def is_obvious_company_product_or_marketing_noise(item: RssItem) -> bool:
    """Drop cheap, explicit company promotion before semantic analysis."""
    normalized = f"{item.title} {item.content[:1500]}".casefold()
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    explicit_noise = any(
        marker in normalized for marker in OBVIOUS_COMPANY_PRODUCT_OR_MARKETING_MARKERS
    )
    if not explicit_noise and features.event_type is not EventType.PRODUCT:
        return False
    if any(marker in normalized for marker in MATERIAL_COMPANY_EVENT_MARKERS):
        return False
    return any(
        instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
        for instrument in features.instruments
    )


def is_market_noise(item: RssItem) -> bool:
    """Reject posts that describe non-market topics or an already realised price move."""
    normalized_title = item.title.casefold()
    categories = " ".join(item.categories).casefold()
    if any(marker in categories for marker in NON_MARKET_CATEGORY_MARKERS):
        return True
    if any(marker in normalized_title for marker in MARKET_NOISE_TITLE_MARKERS):
        return True
    if is_obvious_company_product_or_marketing_noise(item):
        return True
    if PRICE_REACTION_TITLE_PATTERN.search(item.title):
        return True
    return (
        normalized_title.count("%") >= 3
        and ("лидер" in normalized_title or "динамик" in normalized_title)
    )

MARKET_EVENT_MARKERS = (
    "дивиденд",
    "финотчет",
    "финотчёт",
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

MULTI_COMPANY_ROUNDUP_TITLE_MARKERS = (
    "дайджест",
    "подборка",
    "сводка",
)
MULTI_COMPANY_COUNT_PATTERN = re.compile(
    r"\bсразу\s+(?:\d+|две|три|четыре|пять|шесть|семь|восемь|девять|десять)"
    r"\s+компани",
    re.IGNORECASE,
)


def is_multi_company_roundup(item: RssItem) -> bool:
    """Identify aggregate headlines, without blocking a joint issuer event."""
    normalized_title = item.title.casefold()
    if not (
        any(marker in normalized_title for marker in MULTI_COMPANY_ROUNDUP_TITLE_MARKERS)
        or MULTI_COMPANY_COUNT_PATTERN.search(item.title)
    ):
        return False
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    direct_tickers = {
        instrument.ticker
        for instrument in features.instruments
        if instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
    }
    return len(direct_tickers) >= 2


def signal_analysis_exclusion_reason(source_id: str, item: RssItem) -> str | None:
    """Return a stable reason when an item is context, not causal evidence."""
    if source_id in CONTEXT_ONLY_SOURCE_IDS:
        return "analysis_only_source"
    if is_multi_company_roundup(item):
        return "multi_company_roundup"
    return None


def is_market_signal_candidate(item: RssItem) -> bool:
    """High-recall gate for direct company events before semantic analysis.

    The gate only decides whether analysis is worth running. The semantic
    extractor and deterministic scorer still decide direction and strength.
    """
    if not is_moex_equity_title(item.title):
        return False
    if is_market_noise(item):
        return False
    if is_multi_company_roundup(item):
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
    if is_market_noise(item):
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
    if is_market_noise(item):
        return False
    return is_company_news_candidate(item) or is_broad_market_event_text(
        item.title,
        item.content,
    )


SEMANTIC_EVENT_MARKERS = (
    *MARKET_EVENT_MARKERS,
    "правительств",
    "господдерж",
    "поддержк",
    "субсиди",
    "экспорт",
    "импорт",
    "пошлин",
    "тариф",
    "железнодорож",
    "ж/д",
    "перевоз",
    "логистик",
    "сельхоз",
    "агропром",
    "зерн",
    "урожа",
    "нефт",
    "газ",
    "курс рубл",
    "инфляц",
    "ключевая ставка",
)


def is_semantic_analysis_candidate(item: RssItem) -> bool:
    """High-recall economic router; final target and signal remain downstream."""
    normalized = f"{item.title} {item.content[:2500]}".casefold()
    if is_market_noise(item):
        return False
    return is_market_event_candidate(item) or any(
        marker in normalized for marker in SEMANTIC_EVENT_MARKERS
    )


def is_signal_analysis_candidate(item: RssItem) -> bool:
    """Route only targetable company or material context events to the LLM."""
    if is_multi_company_roundup(item):
        return False
    if not is_semantic_analysis_candidate(item):
        return False
    if is_market_signal_candidate(item):
        return True
    projection = classify_news_event(
        title=item.title,
        content=item.content,
        source_metadata={},
    )
    return bool(
        context_signal_specs(
            projection,
            title=item.title,
            content=item.content,
        )
    )


def signal_analysis_priority(item: RssItem) -> int:
    """Rank material company events while preserving source order within a rank."""
    if is_obvious_company_product_or_marketing_noise(item):
        return 0
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="market_news",
            title=item.title,
            content=item.content,
            language="ru",
        )
    )
    if not any(
        instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
        for instrument in features.instruments
    ):
        return 1
    normalized_title = item.title.casefold()
    priority_context = (
        f"{item.title} {item.content[:1500]}".casefold()
        if len(item.title) <= 40
        else normalized_title
    )
    financial_results = "нефинансов" not in priority_context and any(
        marker in priority_context for marker in FINANCIAL_RESULTS_PRIORITY_MARKERS
    )
    if (
        "дивиденд" in priority_context
        or financial_results
        or any(marker in priority_context for marker in SANCTIONS_PRIORITY_MARKERS)
        or any(marker in priority_context for marker in MATERIAL_MA_MARKERS)
    ):
        return 3
    if any(marker in priority_context for marker in PRODUCTION_PRIORITY_MARKERS):
        return 2
    return 1


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
        "analysis_candidates": 0,
        "accepted": 0,
        "replayed": 0,
        "failed": 0,
    }


def discovery_source_bucket(source_id: str) -> int:
    """Keep a source on one stable minute within every five-minute window."""
    return zlib.crc32(source_id.encode("utf-8")) % DISCOVERY_BUCKET_COUNT


def aggregate_collection_results(
    results: list[dict[str, int] | BaseException],
) -> dict[str, int]:
    totals = empty_collection_totals()
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            totals["failed"] += 1
            continue
        for key in totals:
            totals[key] += result.get(key, 0)
    return totals


async def collect_source_tasks(
    tasks: list[tuple[str, Awaitable[dict[str, int]]]],
    *,
    deadline_seconds: float | None = None,
    task_timeout_seconds: float | None = None,
) -> dict[str, int]:
    """Run independent sources without allowing one lane to exceed its trigger window."""
    scheduled = [
        (
            source_id,
            asyncio.create_task(
                asyncio.wait_for(task, timeout=task_timeout_seconds)
                if task_timeout_seconds is not None
                else task
            ),
        )
        for source_id, task in tasks
    ]
    scheduled_tasks = [task for _, task in scheduled]
    pending: set[asyncio.Task[dict[str, int]]] = set()
    try:
        _, pending = await asyncio.wait(
            scheduled_tasks,
            timeout=deadline_seconds,
        )
        for task in pending:
            task.cancel()
        outcomes = await asyncio.gather(*scheduled_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in scheduled_tasks:
            task.cancel()
        await asyncio.gather(*scheduled_tasks, return_exceptions=True)
        raise

    results: list[dict[str, int] | BaseException] = []
    for (source_id, task), outcome in zip(scheduled, outcomes, strict=True):
        if task in pending or isinstance(outcome, asyncio.CancelledError):
            error: BaseException = TimeoutError("collection source deadline exceeded")
        elif isinstance(outcome, BaseException):
            error = outcome
        else:
            results.append(outcome)
            continue
        LOGGER.warning(
            "Market feed collection failed source_id=%s error=%s",
            source_id,
            type(error).__name__,
        )
        results.append(error)
    return aggregate_collection_results(results)


async def collect_feed_group(
    repository: NewsRepository,
    feeds: tuple[RssFeedConfig, ...],
    *,
    stop_after_replays: int | None,
    analyze_signals: bool = True,
    timeout_seconds: float | None = None,
    task_timeout_seconds: float | None = None,
    executor: Executor = BACKGROUND_FEED_FETCH_EXECUTOR,
) -> dict[str, int]:
    return await collect_source_tasks(
        [
            (
                config.source_id,
                collect_rss_feed(
                    repository,
                    config,
                    item_filter=collection_filters(config)[0],
                    signal_filter=collection_filters(config)[1],
                    stop_after_replays=stop_after_replays,
                    analyze_signals=analyze_signals,
                    timeout_seconds=timeout_seconds,
                    executor=executor,
                ),
            )
            for config in feeds
        ],
        task_timeout_seconds=task_timeout_seconds,
    )


async def collect_fast_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh direct priority feeds every minute without discovery latency."""
    return await collect_source_tasks(
        [
            (
                "fast_rss",
                collect_feed_group(
                    repository,
                    FAST_NEWS_FEEDS,
                    stop_after_replays=1,
                    analyze_signals=False,
                    timeout_seconds=FAST_SOURCE_TIMEOUT_SECONDS,
                    task_timeout_seconds=FAST_SOURCE_DEADLINE_SECONDS,
                    executor=PRIORITY_FEED_FETCH_EXECUTOR,
                ),
            ),
        ],
        deadline_seconds=COLLECTION_LANE_DEADLINE_SECONDS,
        task_timeout_seconds=FAST_SOURCE_DEADLINE_SECONDS + 1,
    )


async def collect_discovery_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh one stable fifth of broad sources each minute."""
    try:
        managed_sources = await asyncio.wait_for(
            repository.list_telegram_sources(),
            timeout=DISCOVERY_REGISTRY_DEADLINE_SECONDS,
        )
    except Exception as error:
        LOGGER.warning(
            "Managed Telegram registry unavailable; continuing without it error=%s",
            type(error).__name__,
        )
        managed_sources = []
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
    telegram_channels = {
        config.source_id: config for config in (*TELEGRAM_CHANNELS, *dynamic_channels)
    }
    bucket = datetime.now(UTC).minute % DISCOVERY_BUCKET_COUNT
    discovery_sources = (
        *(("rss", config.config_key, config) for config in DISCOVERY_NEWS_FEEDS),
        *(("telegram", config.source_id, config) for config in telegram_channels.values()),
    )
    selected_sources = [
        source
        for source in discovery_sources
        if discovery_source_bucket(source[1]) == bucket
    ]
    selected_feeds = tuple(
        config for kind, _, config in selected_sources if kind == "rss"
    )
    selected_channels = tuple(
        config for kind, _, config in selected_sources if kind == "telegram"
    )
    tasks: list[tuple[str, Awaitable[dict[str, int]]]] = []
    if selected_feeds:
        tasks.append(
            (
                "discovery_rss",
                collect_feed_group(
                    repository,
                    selected_feeds,
                    stop_after_replays=10,
                    analyze_signals=False,
                    task_timeout_seconds=DISCOVERY_SOURCE_DEADLINE_SECONDS,
                ),
            )
        )
    tasks.extend(
        (
            config.source_id,
            collect_telegram_channel(
                repository,
                config,
                item_filter=is_market_event_candidate,
                signal_filter=is_market_signal_candidate,
                stop_after_replays=5,
                analyze_signals=False,
                executor=BACKGROUND_FEED_FETCH_EXECUTOR,
            ),
        )
        for config in selected_channels
    )
    return await collect_source_tasks(
        tasks,
        deadline_seconds=COLLECTION_LANE_DEADLINE_SECONDS,
        task_timeout_seconds=DISCOVERY_SOURCE_DEADLINE_SECONDS + 1,
    )


async def collect_slow_news(repository: NewsRepository) -> dict[str, int]:
    """Refresh macro context that does not require minute-level polling."""
    return await collect_source_tasks(
        [
            (
                CBR_PRESS_FEED.source_id,
                collect_rss_feed(
                    repository,
                    CBR_PRESS_FEED,
                    item_filter=is_cbr_market_news,
                    signal_filter=lambda item: False,
                    stop_after_replays=5,
                    analyze_signals=False,
                ),
            )
        ],
        deadline_seconds=COLLECTION_LANE_DEADLINE_SECONDS,
        task_timeout_seconds=DISCOVERY_SOURCE_DEADLINE_SECONDS,
    )


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
