from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

from eventedge.analysis import NewsAnalysisInput, RuleBasedNewsExtractor

TICKER_SECTORS = {
    "SBER": "Финансы",
    "VTBR": "Финансы",
    "MOEX": "Финансы",
    "LKOH": "Нефть и газ",
    "NVTK": "Нефть и газ",
    "TATN": "Нефть и газ",
    "ROSN": "Нефть и газ",
    "SIBN": "Нефть и газ",
    "GAZP": "Нефть и газ",
    "GMKN": "Металлы",
    "PLZL": "Металлы",
    "CHMF": "Металлы",
    "ALRS": "Металлы",
    "YDEX": "Технологии",
    "MGNT": "Ритейл",
    "AFLT": "Транспорт",
    "AFKS": "Холдинги",
    "AKRN": "Химия",
    "ASTR": "Технологии",
    "BANE": "Нефть и газ",
    "BELU": "Потребительский сектор",
    "BSPB": "Финансы",
    "CBOM": "Финансы",
    "DELI": "Транспорт",
    "DOMRF": "Финансы",
    "ENPG": "Металлы",
    "FEES": "Электроэнергетика",
    "FIXP": "Ритейл",
    "FLOT": "Транспорт",
    "HEAD": "Технологии",
    "HYDR": "Электроэнергетика",
    "IRAO": "Электроэнергетика",
    "KMAZ": "Промышленность",
    "LEAS": "Финансы",
    "LSRG": "Недвижимость",
    "MAGN": "Металлы",
    "MDMG": "Здравоохранение",
    "MTSS": "Телеком",
    "MVID": "Ритейл",
    "NLMK": "Металлы",
    "NMTP": "Транспорт",
    "OGKB": "Электроэнергетика",
    "OZON": "Ритейл",
    "PHOR": "Химия",
    "PIKK": "Недвижимость",
    "POSI": "Технологии",
    "RENI": "Финансы",
    "RTKM": "Телеком",
    "RUAL": "Металлы",
    "SELG": "Металлы",
    "SGZH": "Лесная промышленность",
    "SMLT": "Недвижимость",
    "SNGS": "Нефть и газ",
    "SOFL": "Технологии",
    "TGKA": "Электроэнергетика",
    "TRNFP": "Нефть и газ",
    "UGLD": "Металлы",
    "UNAC": "Промышленность",
    "UPRO": "Электроэнергетика",
    "VKCO": "Технологии",
    "VSMO": "Металлы",
    "WUSH": "Транспорт",
    "X5": "Ритейл",
}

SECTOR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Ритейл и логистика",
        (
            "wildberries",
            "вайлдберриз",
            "ozon",
            "маркетплейс",
            "склад",
            "логистик",
            "ритейл",
        ),
    ),
    ("Финансы", ("банк", "банковск", "кредит", "ипотек", "финансовый сектор")),
    ("Нефть и газ", ("нефт", "газ", "brent", "опек", "нпз", "топлив")),
    ("Металлы", ("металл", "золото", "никел", "сталь", "алмаз")),
    ("Технологии", ("it-сектор", "технологическ", "интернет-компан")),
)

MARKET_MARKERS = (
    "ключевая ставка",
    "банк россии",
    "инфляц",
    "курс рубл",
    "российский рынок",
    "рынок акций",
    "индекс мосбирж",
    "индекс imoex",
    "санкци",
    "геополит",
    "бюджет",
    "минфин",
    "бпла",
    "дрон",
    "обстрел",
    "чрезвычай",
)

MARKET_PRIORITY_MARKERS = (
    "ключевая ставка",
    "банк россии",
    "инфляц",
    "курс рубл",
    "индекс мосбирж",
    "индекс imoex",
    "санкци",
    "бюджет",
    "минфин",
)

EVENT_STOP_WORDS = frozenset(
    {
        "для",
        "как",
        "при",
        "что",
        "это",
        "или",
        "после",
        "перед",
        "под",
        "над",
        "без",
        "уже",
        "будет",
        "россии",
        "российский",
    }
)


def classify_news_event(
    *,
    title: str,
    content: str,
    source_metadata: Mapping[str, object],
    related_signals: Iterable[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Project one news item into a stable, explainable market-event scope."""
    signal_tickers = [
        str(signal.get("ticker", "")).upper() for signal in related_signals if signal.get("ticker")
    ]
    metadata_tickers = source_metadata.get("tickers", [])
    title_features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="event_projection",
            title=title,
            content=title,
            language="ru",
        )
    )
    title_tickers = {instrument.ticker for instrument in title_features.instruments}
    tickers = list(
        dict.fromkeys(
            [
                *(
                    str(ticker).upper()
                    for ticker in metadata_tickers
                    if ticker and str(ticker).upper() in title_tickers
                ),
                *signal_tickers,
            ]
        )
    )
    if tickers:
        sectors = list(
            dict.fromkeys(TICKER_SECTORS[ticker] for ticker in tickers if ticker in TICKER_SECTORS)
        )
        return {
            "scope": "company",
            "scope_label": "Компания",
            "tickers": tickers,
            "sectors": sectors,
            "reason": "В публикации распознана торгуемая компания.",
        }

    normalized = " ".join((title, content)).casefold()
    if any(marker in normalized for marker in MARKET_PRIORITY_MARKERS):
        return {
            "scope": "market",
            "scope_label": "Рынок",
            "tickers": [],
            "sectors": [],
            "reason": "Распознан общий рыночный или страновой фактор.",
        }
    sectors = [
        sector
        for sector, markers in SECTOR_MARKERS
        if any(marker in normalized for marker in markers)
    ]
    if sectors:
        return {
            "scope": "sector",
            "scope_label": "Отрасль",
            "tickers": [],
            "sectors": sectors,
            "reason": "Событие затрагивает отрасль, но не привязано к одной бумаге.",
        }

    return {
        "scope": "market",
        "scope_label": "Рынок",
        "tickers": [],
        "sectors": [],
        "reason": (
            "Распознан общий рыночный или страновой фактор."
            if any(marker in normalized for marker in MARKET_MARKERS)
            else "Событие пока не привязано к отрасли или компании."
        ),
    }


def is_broad_market_event_text(title: str, content: str) -> bool:
    normalized = " ".join((title, content)).casefold()
    return any(marker in normalized for marker in MARKET_MARKERS) or any(
        marker in normalized for _, markers in SECTOR_MARKERS for marker in markers
    )


def cluster_market_events(items: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Collapse corroborating publications into one event-centric API record."""
    groups: list[dict[str, object]] = []
    for raw_item in items:
        item = dict(raw_item)
        group = next((candidate for candidate in groups if _same_event(candidate, item)), None)
        if group is None:
            group = {
                **item,
                "news_ids": [item["news_id"]],
                "sources": [
                    {
                        "source_id": item["source_id"],
                        "url": item["source_url"],
                    }
                ],
                "source_count": 1,
            }
            group.pop("news_id", None)
            groups.append(group)
            continue

        news_ids = list(group["news_ids"])
        news_ids.append(item["news_id"])
        sources = list(group["sources"])
        if not any(
            source["source_id"] == item["source_id"] and source["url"] == item["source_url"]
            for source in sources
        ):
            sources.append({"source_id": item["source_id"], "url": item["source_url"]})
        if _event_weight(item) > _event_weight(group):
            event_id = group["id"]
            group.clear()
            group.update(item)
            group["id"] = event_id
            group.pop("news_id", None)
        group["news_ids"] = news_ids
        group["sources"] = sources
        group["source_count"] = len(sources)
    return groups


def _same_event(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    if left.get("scope") != right.get("scope"):
        return False
    left_tickers = set(left.get("tickers", []))
    right_tickers = set(right.get("tickers", []))
    if left_tickers and right_tickers and left_tickers.isdisjoint(right_tickers):
        return False
    left_sectors = set(left.get("sectors", []))
    right_sectors = set(right.get("sectors", []))
    if not left_tickers and not right_tickers and left_sectors and right_sectors:
        if left_sectors.isdisjoint(right_sectors):
            return False
    left_time = datetime.fromisoformat(str(left["published_at"]).replace("Z", "+00:00"))
    right_time = datetime.fromisoformat(str(right["published_at"]).replace("Z", "+00:00"))
    if abs((left_time - right_time).total_seconds()) > 48 * 60 * 60:
        return False
    left_tokens = _title_tokens(str(left["title"]))
    right_tokens = _title_tokens(str(right["title"]))
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.6


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-zа-яё0-9]+", " ", title.casefold()).split()
        if len(token) > 2 and token not in EVENT_STOP_WORDS
    }


def _event_weight(item: Mapping[str, object]) -> float:
    signals = item.get("related_signals", [])
    signal_weight = max(
        (abs(float(signal.get("score", 0))) for signal in signals),
        default=0.0,
    )
    return signal_weight + min(len(str(item.get("summary", ""))), 600) / 120
