from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

from eventedge.analysis import (
    DOWN_SCORE_THRESHOLD,
    MINIMUM_DOWN_CONFIDENCE,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
)

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
            "ритейл",
        ),
    ),
    ("Финансы", ("банк", "банковск", "кредит", "ипотек", "финансовый сектор")),
    ("Нефть и газ", ("нефт", "газ", "brent", "опек", "нпз", "топлив")),
    ("Металлы", ("металл", "золото", "никел", "сталь", "алмаз")),
    ("Технологии", ("it-сектор", "технологическ", "интернет-компан")),
    (
        "Сельское хозяйство",
        ("сельхоз", "агропром", "зерн", "урожа", "удобрени", "аграрн"),
    ),
    (
        "Транспорт и логистика",
        ("железнодорож", "ж/д", "перевоз", "транспортн", "портов", "судоход"),
    ),
    ("Электроэнергетика", ("электроэнерг", "электросет", "генераци", "энергосистем")),
)

MARKET_SIGNAL_CODE = "RUEQ"
MARKET_SIGNAL_TARGET = {
    "type": "market",
    "id": "RU_EQUITIES",
    "label": "Российский рынок",
}
SECTOR_SIGNAL_TARGETS = {
    "Ритейл и логистика": ("RURETAIL", "RETAIL_LOGISTICS"),
    "Финансы": ("RUFIN", "FINANCIALS"),
    "Нефть и газ": ("RUOILGAS", "OIL_GAS"),
    "Металлы": ("RUMETALS", "METALS"),
    "Технологии": ("RUTECH", "TECHNOLOGY"),
    "Сельское хозяйство": ("RUAGRI", "AGRICULTURE"),
    "Транспорт и логистика": ("RUTRANS", "TRANSPORT_LOGISTICS"),
    "Электроэнергетика": ("RUPOWER", "POWER"),
}
CONTEXT_SIGNAL_TARGETS = {
    MARKET_SIGNAL_CODE: MARKET_SIGNAL_TARGET,
    **{
        code: {"type": "sector", "id": target_id, "label": label}
        for label, (code, target_id) in SECTOR_SIGNAL_TARGETS.items()
    },
}

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

# A context event may be worth storing and reading without being large enough
# to justify a market- or sector-wide trading signal. These markers are a
# deterministic scale guard applied after the semantic extraction. They keep
# country/industry policy and material physical disruptions while rejecting a
# single delayed flight or a local municipal subsidy as a proxy for the whole
# sector/market.
CONTEXT_SIGNAL_SCALE_MARKERS = (
    "отрасл",
    "сектор",
    "рынок",
    "по всей россии",
    "федеральн",
    "экспорт",
    "импорт",
    "пошлин",
    "тариф",
    "квот",
    "санкци",
    "господдерж",
    "субсиди",
    "для компаний",
    "для производителей",
    "поставк",
    "бпла",
    "дрон",
    "обстрел",
    "пожар",
    "авари",
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
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(
            source_id="event_projection",
            title=title,
            content=content,
            language="ru",
        )
    )
    semantic = {
        "event_type": features.event_type.value,
        "materiality": features.materiality,
        "extractor_version": features.extractor_version,
    }
    signal_tickers = [
        ticker
        for signal in related_signals
        if signal.get("ticker")
        and (ticker := str(signal["ticker"]).upper()) not in CONTEXT_SIGNAL_TARGETS
    ]
    metadata_tickers = source_metadata.get("tickers", [])
    title_tickers = {
        instrument.ticker
        for instrument in features.instruments
        if instrument.relevance >= 0.9
    }
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
            **semantic,
            "scope": "company",
            "scope_label": "Компания",
            "tickers": tickers,
            "sectors": sectors,
            "reason": "В публикации распознана торгуемая компания.",
        }

    normalized = " ".join((title, content)).casefold()
    if any(marker in normalized for marker in MARKET_PRIORITY_MARKERS):
        return {
            **semantic,
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
            **semantic,
            "scope": "sector",
            "scope_label": "Отрасль",
            "tickers": [],
            "sectors": sectors,
            "reason": "Событие затрагивает отрасль, но не привязано к одной бумаге.",
        }

    return {
        **semantic,
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


def is_product_event_candidate(
    source_metadata: Mapping[str, object],
    related_signals: Iterable[Mapping[str, object]] = (),
) -> bool:
    """Keep only routed or signal-backed news on public event surfaces.

    Raw collection remains untouched so a future router can recover false
    negatives. A generated historical signal is also enough to retain an
    event after that signal is no longer present in the active read model.
    """
    if any(True for _ in related_signals):
        return True
    if any(
        source_metadata.get(field) is True
        for field in ("event_candidate", "analysis_candidate", "signal_candidate")
    ):
        return True
    outcome = source_metadata.get("signal_outcome")
    signal_count = outcome.get("signal_count") if isinstance(outcome, Mapping) else None
    return bool(
        isinstance(outcome, Mapping)
        and outcome.get("status") == "generated"
        and isinstance(signal_count, int)
        and signal_count > 0
    )


def signal_target(ticker: str) -> dict[str, str]:
    context = CONTEXT_SIGNAL_TARGETS.get(ticker)
    if context is not None:
        return dict(context)
    return {
        "type": "instrument",
        "id": ticker,
        "label": ticker,
    }


def is_publishable_news_signal(
    *,
    ticker: str,
    direction: str,
    score: float,
    confidence: float,
) -> bool:
    """Fail closed for synthetic targets and weak company downside signals.

    Market and sector projections remain useful as readable context, but the
    current rules do not validate their target or direction well enough to
    publish them as trading signals. Keeping this boundary in the shared read
    model also hides context signals persisted by older revisions.
    """
    if ticker in CONTEXT_SIGNAL_TARGETS:
        return False
    if direction != "down":
        return True
    return bool(
        score <= DOWN_SCORE_THRESHOLD
        and confidence >= MINIMUM_DOWN_CONFIDENCE
    )


def context_signal_specs(
    projection: Mapping[str, object],
    *,
    title: str = "",
    content: str = "",
) -> list[tuple[str, str]]:
    normalized = " ".join((title, content)).casefold()
    scope = projection.get("scope")
    if scope == "market":
        return (
            [(MARKET_SIGNAL_CODE, str(MARKET_SIGNAL_TARGET["label"]))]
            if any(marker in normalized for marker in MARKET_MARKERS)
            else []
        )
    if scope != "sector":
        return []
    if not any(marker in normalized for marker in CONTEXT_SIGNAL_SCALE_MARKERS):
        return []
    sectors = projection.get("sectors", [])
    if not isinstance(sectors, list):
        return []
    return [
        (code, str(sector))
        for sector in sectors
        if isinstance(sector, str)
        and (definition := SECTOR_SIGNAL_TARGETS.get(sector)) is not None
        for code in [definition[0]]
    ]


def is_broad_market_event_text(title: str, content: str) -> bool:
    normalized = " ".join((title, content)).casefold()
    return any(marker in normalized for marker in MARKET_MARKERS) or any(
        marker in normalized for _, markers in SECTOR_MARKERS for marker in markers
    )


def cluster_market_events(items: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Collapse corroborating publications into deterministic event records."""
    groups: list[dict[str, object]] = []
    ordered_items = sorted((dict(item) for item in items), key=_item_order_key)
    for item in ordered_items:
        group = next((candidate for candidate in groups if _same_event(candidate, item)), None)
        if group is None:
            groups.append(_new_event_group(item))
            continue
        _merge_event_group(group, item)
    return sorted(
        groups,
        key=lambda item: (str(item["event_time"]), str(item["id"])),
        reverse=True,
    )


def _new_event_group(item: Mapping[str, object]) -> dict[str, object]:
    group = dict(item)
    group["event_time"] = item["published_at"]
    group["news_ids"] = [item["news_id"]]
    group["sources"] = [
        {"source_id": item["source_id"], "url": item["source_url"]}
    ]
    evidence = item.get("evidence")
    group["evidence"] = [dict(evidence)] if isinstance(evidence, Mapping) else []
    group["related_signals"] = _deduplicate_mappings(item.get("related_signals", []))
    group["source_count"] = 1
    group.pop("news_id", None)
    return group


def _merge_event_group(group: dict[str, object], item: Mapping[str, object]) -> None:
    news_ids = [*group["news_ids"], item["news_id"]]
    sources = list(group["sources"])
    source = {"source_id": item["source_id"], "url": item["source_url"]}
    if source not in sources:
        sources.append(source)
    evidence = list(group["evidence"])
    item_evidence = item.get("evidence")
    if isinstance(item_evidence, Mapping) and dict(item_evidence) not in evidence:
        evidence.append(dict(item_evidence))
    signals = _deduplicate_mappings(
        [*group.get("related_signals", []), *item.get("related_signals", [])]
    )
    tickers = sorted({*group.get("tickers", []), *item.get("tickers", [])})
    sectors = sorted({*group.get("sectors", []), *item.get("sectors", [])})
    materiality = max(
        float(group.get("materiality", 0)),
        float(item.get("materiality", 0)),
    )
    event_type = str(group.get("event_type", "other"))
    if event_type == "other" and item.get("event_type"):
        event_type = str(item["event_type"])
    canonical_id = group["id"]
    event_time = group["event_time"]
    detected_at = min(
        (str(value) for value in (group.get("detected_at"), item.get("detected_at")) if value),
        default=None,
    )

    if _event_weight(item) > _event_weight(group):
        group.clear()
        group.update(item)
        group.pop("news_id", None)

    group.update(
        {
            "id": canonical_id,
            "event_time": event_time,
            "news_ids": news_ids,
            "sources": sources,
            "source_count": len(sources),
            "evidence": evidence,
            "related_signals": signals,
            "tickers": tickers,
            "sectors": sectors,
            "materiality": materiality,
            "event_type": event_type,
        }
    )
    if detected_at is not None:
        group["detected_at"] = detected_at


def _item_order_key(item: Mapping[str, object]) -> tuple[str, str, str, str]:
    return (
        str(item.get("published_at", "")),
        str(item.get("news_id", "")),
        str(item.get("source_id", "")),
        str(item.get("id", "")),
    )


def _deduplicate_mappings(items: object) -> list[dict[str, object]]:
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        return []
    unique: dict[str, dict[str, object]] = {}
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        key = str(item.get("id") or sorted((str(key), repr(value)) for key, value in item.items()))
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _same_event(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    if left.get("scope") != right.get("scope"):
        return False
    left_type = str(left.get("event_type", "other"))
    right_type = str(right.get("event_type", "other"))
    if left_type != "other" and right_type != "other" and left_type != right_type:
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
