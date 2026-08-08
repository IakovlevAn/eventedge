from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventType(StrEnum):
    FINANCIAL_RESULTS = "financial_results"
    DIVIDEND = "dividend"
    REGULATION = "regulation"
    SANCTIONS = "sanctions"
    PRODUCTION = "production"
    MANAGEMENT = "management"
    PARTNERSHIP = "partnership"
    PRODUCT = "product"
    MACRO = "macro"
    OTHER = "other"


class TemporalStatus(StrEnum):
    PAST = "past"
    CURRENT = "current"
    FUTURE = "future"


class SignalDirection(StrEnum):
    UP = "up"
    NEUTRAL = "neutral"
    DOWN = "down"


class SignalAction(StrEnum):
    CONSIDER_BUY = "consider_buy"
    NO_ACTION = "no_action"
    REVIEW_POSITION = "review_position"


class NewsAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    content: Annotated[str, Field(min_length=1, max_length=200_000)]
    language: Literal["ru", "en"] = "ru"


class InstrumentMention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,12}$")]
    relevance: Annotated[float, Field(ge=0, le=1)]
    matched_alias: Annotated[str, Field(min_length=1, max_length=100)]


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["percentage", "money", "quantity", "date", "text"]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    value: Annotated[str, Field(min_length=1, max_length=120)]
    unit: Annotated[str | None, Field(max_length=40)] = None
    period: Annotated[str | None, Field(max_length=80)] = None
    source_quote: Annotated[str, Field(min_length=1, max_length=500)]


class SemanticFeatures(BaseModel):
    """Strict extractor output. It intentionally has no signal direction or action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["news-features-0.1"] = "news-features-0.1"
    extractor_version: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")]
    event_type: EventType
    instruments: Annotated[list[InstrumentMention], Field(max_length=20)]
    facts: Annotated[list[ExtractedFact], Field(max_length=20)]
    polarity: Annotated[float, Field(ge=-1, le=1)]
    materiality: Annotated[float, Field(ge=0, le=1)]
    novelty: Annotated[float, Field(ge=0, le=1)]
    temporal_status: TemporalStatus
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]


class FactorContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "semantic_effect",
        "event_materiality",
        "event_novelty",
        "source_quality",
        "fact_coverage",
    ]
    contribution: Annotated[float, Field(ge=-1, le=1)]
    label: Annotated[str, Field(min_length=1, max_length=120)]


class BaselineSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,12}$")]
    direction: SignalDirection
    action: SignalAction
    score: Annotated[float, Field(ge=-100, le=100)]
    strength: Annotated[float, Field(ge=0, le=1)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    factor_contributions: Annotated[list[FactorContribution], Field(min_length=5, max_length=5)]
    feature_schema_version: Literal["news-features-0.1"]
    extractor_version: str
    model_version: Literal["news-baseline-0.1.0"]
    config_version: int


class BaselineScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_version: Literal["news-baseline-0.1.0"] = "news-baseline-0.1.0"
    config_version: Annotated[int, Field(ge=1)] = 1
    positive_threshold: Annotated[float, Field(ge=0, le=100)] = 18
    negative_threshold: Annotated[float, Field(ge=-100, le=0)] = -18
    calibration_scale: Annotated[float, Field(gt=0, le=1)] = 0.55
    semantic_weight: Annotated[float, Field(ge=0, le=1)] = 0.55
    materiality_weight: Annotated[float, Field(ge=0, le=1)] = 0.20
    novelty_weight: Annotated[float, Field(ge=0, le=1)] = 0.10
    source_quality_weight: Annotated[float, Field(ge=0, le=1)] = 0.10
    fact_coverage_weight: Annotated[float, Field(ge=0, le=1)] = 0.05

    @model_validator(mode="after")
    def validate_contract(self) -> BaselineScoringConfig:
        if self.negative_threshold >= self.positive_threshold:
            raise ValueError("negative_threshold must be below positive_threshold")
        total = (
            self.semantic_weight
            + self.materiality_weight
            + self.novelty_weight
            + self.source_quality_weight
            + self.fact_coverage_weight
        )
        if abs(total - 1) > 1e-9:
            raise ValueError("scoring weights must sum to one")
        return self


DEFAULT_MOEX_ALIASES: dict[str, tuple[str, ...]] = {
    "SBER": ("сбербанк", "сбер", "sber"),
    "LKOH": ("лукойл", "lukoil", "lkoh"),
    "YDEX": ("яндекс", "yandex", "ydex"),
    "NVTK": ("новатэк", "novatek", "nvtk"),
    "TATN": ("татнефть", "tatneft", "tatn"),
    "ROSN": ("роснефть", "rosneft", "rosn"),
    "GMKN": ("норникель", "норильский никель", "nornickel", "gmkn"),
    "MGNT": ("магнит", "magnit", "mgnt"),
    "GAZP": ("газпром", "gazprom", "gazp"),
    "VTBR": ("втб", "vtb", "vtbr"),
    "PLZL": ("полюс", "polyus", "plzl"),
    "CHMF": ("северсталь", "severstal", "chmf"),
    "ALRS": ("алроса", "alrosa", "alrs"),
    "MOEX": ("московская биржа", "мосбиржа", "moex"),
}

EVENT_RULES: tuple[tuple[EventType, tuple[str, ...], float], ...] = (
    (
        EventType.SANCTIONS,
        ("санкц", "ограничен", "запрет", "эмбарго", "блокир"),
        0.95,
    ),
    (
        EventType.FINANCIAL_RESULTS,
        ("отчётност", "отчетност", "чистая прибыль", "выручк", "ebitda", "рентабельност"),
        0.90,
    ),
    (EventType.DIVIDEND, ("дивиденд", "выплат акционер", "реестр акционер"), 0.85),
    (EventType.REGULATION, ("регулятор", "лицензи", "пошлин", "требовани"), 0.78),
    (EventType.PRODUCTION, ("добыч", "производств", "выпуск", "мощност"), 0.72),
    (EventType.MANAGEMENT, ("генеральный директор", "совет директоров", "руководител"), 0.65),
    (EventType.PARTNERSHIP, ("партнерств", "партнёрств", "соглашени", "совместн"), 0.62),
    (EventType.PRODUCT, ("запустил", "новый продукт", "сервис", "технолог"), 0.55),
    (EventType.MACRO, ("ключевая ставка", "инфляц", "ввп", "денежно-кредитн"), 0.75),
)

POSITIVE_TERMS = (
    "выше ожиданий",
    "лучше ожиданий",
    "вырос",
    "увеличил",
    "рост",
    "ускорил",
    "превысил",
    "улучшил",
    "повысил",
    "рекордн",
    "одобрил",
)

NEGATIVE_TERMS = (
    "ниже ожиданий",
    "хуже ожиданий",
    "снизил",
    "сократил",
    "падени",
    "убыток",
    "ухудшил",
    "приостановил",
    "отозвал",
    "дефолт",
    "санкц",
    "ограничен",
)

FUTURE_TERMS = ("планирует", "ожидает", "намерен", "прогнозирует", "может", "будет")
PAST_TERMS = ("опубликовал", "сообщил", "вырос", "снизил", "завершил", "получил")

PERCENT_PATTERN = re.compile(r"(?<!\w)(\d{1,3}(?:[.,]\d+)?)\s*%")
MONEY_PATTERN = re.compile(
    r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(млн|млрд|трлн)?\s*(₽|руб(?:лей|ля|ль|\.)?)",
    re.IGNORECASE,
)
SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")
CYRILLIC_COMPANY_ALIAS_PATTERN = re.compile(r"^[а-яё]{5,}$")
RUSSIAN_CASE_SUFFIXES = ("а", "у", "ом", "е", "ой", "ы", "и")


class RuleBasedNewsExtractor:
    """Cheap deterministic baseline used before connecting the LLM extractor."""

    version = "rules-0.1.0"

    def __init__(self, aliases: dict[str, tuple[str, ...]] | None = None) -> None:
        self._aliases = aliases or DEFAULT_MOEX_ALIASES

    def extract(self, document: NewsAnalysisInput) -> SemanticFeatures:
        title = document.title.casefold()
        body = f"{document.title}. {document.content}".casefold()
        instruments = self._instruments(title, body)
        event_type, base_materiality = self._event_type(body)
        facts = self._facts(document)
        positive_hits = sum(body.count(term) for term in POSITIVE_TERMS)
        negative_hits = sum(body.count(term) for term in NEGATIVE_TERMS)
        total_hits = positive_hits + negative_hits
        polarity = 0.0 if total_hits == 0 else (positive_hits - negative_hits) / total_hits
        materiality = min(
            1.0,
            base_materiality
            + (0.05 if facts else 0)
            + (0.05 if abs(polarity) >= 0.75 else 0),
        )
        temporal_status = self._temporal_status(body)
        rationale = self._rationale(
            event_type=event_type,
            instruments=instruments,
            polarity=polarity,
            facts=facts,
        )
        return SemanticFeatures(
            extractor_version=self.version,
            event_type=event_type,
            instruments=instruments,
            facts=facts,
            polarity=round(polarity, 4),
            materiality=round(materiality, 4),
            novelty=1.0,
            temporal_status=temporal_status,
            rationale=rationale,
        )

    def _instruments(self, title: str, body: str) -> list[InstrumentMention]:
        matches: list[InstrumentMention] = []
        for ticker, aliases in self._aliases.items():
            for alias in aliases:
                normalized_alias = alias.casefold()
                if not self._contains(title, normalized_alias) and not self._contains(
                    body, normalized_alias
                ):
                    continue
                relevance = 0.96 if self._contains(title, normalized_alias) else 0.78
                matches.append(
                    InstrumentMention(
                        ticker=ticker,
                        relevance=relevance,
                        matched_alias=alias,
                    )
                )
                break
        return matches

    @staticmethod
    def _contains(text: str, alias: str) -> bool:
        suffixes = (
            rf"(?:{'|'.join(RUSSIAN_CASE_SUFFIXES)})?"
            if CYRILLIC_COMPANY_ALIAS_PATTERN.fullmatch(alias)
            else ""
        )
        return bool(re.search(rf"(?<!\w){re.escape(alias)}{suffixes}(?!\w)", text))

    @staticmethod
    def _event_type(text: str) -> tuple[EventType, float]:
        for event_type, terms, materiality in EVENT_RULES:
            if any(term in text for term in terms):
                return event_type, materiality
        return EventType.OTHER, 0.35

    @staticmethod
    def _temporal_status(text: str) -> TemporalStatus:
        if any(term in text for term in FUTURE_TERMS):
            return TemporalStatus.FUTURE
        if any(term in text for term in PAST_TERMS):
            return TemporalStatus.PAST
        return TemporalStatus.CURRENT

    @staticmethod
    def _facts(document: NewsAnalysisInput) -> list[ExtractedFact]:
        facts: list[ExtractedFact] = []
        for sentence in SENTENCE_PATTERN.split(f"{document.title}. {document.content}"):
            quote = sentence.strip()[:500]
            if not quote:
                continue
            for match in PERCENT_PATTERN.finditer(sentence):
                facts.append(
                    ExtractedFact(
                        kind="percentage",
                        label="Процентное значение",
                        value=match.group(1).replace(",", "."),
                        unit="%",
                        source_quote=quote,
                    )
                )
            for match in MONEY_PATTERN.finditer(sentence):
                scale = match.group(2) or ""
                facts.append(
                    ExtractedFact(
                        kind="money",
                        label="Денежное значение",
                        value=match.group(1).replace(",", "."),
                        unit=" ".join(part for part in (scale, "RUB") if part),
                        source_quote=quote,
                    )
                )
            if len(facts) >= 20:
                return facts[:20]
        return facts

    @staticmethod
    def _rationale(
        *,
        event_type: EventType,
        instruments: list[InstrumentMention],
        polarity: float,
        facts: list[ExtractedFact],
    ) -> str:
        tickers = ", ".join(item.ticker for item in instruments) or "нет прямой привязки"
        tone = "положительный" if polarity > 0 else "отрицательный" if polarity < 0 else "смешанный"
        return (
            f"Событие {event_type.value}; инструменты: {tickers}; "
            f"текстовый эффект: {tone}; извлечено фактов: {len(facts)}."
        )


SOURCE_QUALITY: dict[str, float] = {
    "company": 0.95,
    "cbr_press": 0.95,
    "moex": 0.95,
    "interfax": 0.90,
    "reuters": 0.90,
    "tass": 0.82,
    "rbc": 0.78,
}

CONTRIBUTION_LABELS = {
    "semantic_effect": "Текстовый эффект события",
    "event_materiality": "Существенность события",
    "event_novelty": "Новизна события",
    "source_quality": "Качество источника",
    "fact_coverage": "Полнота извлечённых фактов",
}


def score_features(
    features: SemanticFeatures,
    *,
    source_id: str,
    config: BaselineScoringConfig | None = None,
) -> list[BaselineSignal]:
    scoring = config or BaselineScoringConfig()
    if not features.instruments:
        return []

    sign = 1.0 if features.polarity > 0 else -1.0 if features.polarity < 0 else 0.0
    source_quality = SOURCE_QUALITY.get(source_id, 0.65)
    fact_coverage = min(len(features.facts) / 3, 1.0)
    signals: list[BaselineSignal] = []

    for instrument in features.instruments:
        raw_contributions = {
            "semantic_effect": scoring.semantic_weight * features.polarity,
            "event_materiality": scoring.materiality_weight * sign * features.materiality,
            "event_novelty": scoring.novelty_weight * sign * features.novelty,
            "source_quality": scoring.source_quality_weight * sign * source_quality,
            "fact_coverage": scoring.fact_coverage_weight * sign * fact_coverage,
        }
        scaled = {
            code: value * instrument.relevance * scoring.calibration_scale
            for code, value in raw_contributions.items()
        }
        score = round(max(-100.0, min(100.0, sum(scaled.values()) * 100)), 1)
        if score >= scoring.positive_threshold:
            direction = SignalDirection.UP
            action = SignalAction.CONSIDER_BUY
        elif score <= scoring.negative_threshold:
            direction = SignalDirection.DOWN
            action = SignalAction.REVIEW_POSITION
        else:
            direction = SignalDirection.NEUTRAL
            action = SignalAction.NO_ACTION

        confidence = min(
            0.95,
            0.10
            + 0.30 * instrument.relevance
            + 0.20 * features.materiality
            + 0.15 * source_quality
            + 0.10 * features.novelty
            + 0.10 * fact_coverage
            + 0.05 * abs(features.polarity),
        )
        signals.append(
            BaselineSignal(
                ticker=instrument.ticker,
                direction=direction,
                action=action,
                score=score,
                strength=round(min(abs(score) / 55, 1.0), 4),
                confidence=round(confidence, 4),
                summary=features.rationale,
                factor_contributions=[
                    FactorContribution(
                        code=code,
                        contribution=round(value, 4),
                        label=CONTRIBUTION_LABELS[code],
                    )
                    for code, value in scaled.items()
                ],
                feature_schema_version=features.schema_version,
                extractor_version=features.extractor_version,
                model_version=scoring.model_version,
                config_version=scoring.config_version,
            )
        )
    return signals


def extract_and_score(
    document: NewsAnalysisInput,
    *,
    extractor: RuleBasedNewsExtractor | None = None,
    config: BaselineScoringConfig | None = None,
) -> tuple[SemanticFeatures, list[BaselineSignal]]:
    active_extractor = extractor or RuleBasedNewsExtractor()
    features = active_extractor.extract(document)
    return features, score_features(features, source_id=document.source_id, config=config)
