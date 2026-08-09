from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Annotated, Protocol

import requests
from pydantic import BaseModel, ConfigDict, Field

from eventedge.analysis import (
    EventType,
    ExtractedFact,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
    SemanticFeatures,
    TemporalStatus,
)

LOGGER = logging.getLogger(__name__)
METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
CHAT_COMPLETIONS_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"


class NewsAnalyzer(Protocol):
    async def extract(self, document: NewsAnalysisInput) -> SemanticFeatures: ...


class RuleBasedNewsAnalyzer:
    def __init__(self, extractor: RuleBasedNewsExtractor | None = None) -> None:
        self._extractor = extractor or RuleBasedNewsExtractor()

    async def extract(self, document: NewsAnalysisInput) -> SemanticFeatures:
        return self._extractor.extract(document)


class LlmSemanticPayload(BaseModel):
    """LLM-owned fields. Tickers, novelty and the final signal stay outside the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: EventType
    facts: Annotated[list[ExtractedFact], Field(max_length=12)]
    polarity: Annotated[float, Field(ge=-1, le=1)]
    materiality: Annotated[float, Field(ge=0, le=1)]
    temporal_status: TemporalStatus
    rationale: Annotated[str, Field(min_length=1, max_length=700)]


class MetadataIamTokenProvider:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        response = self._session.get(
            METADATA_TOKEN_URL,
            headers={"Metadata-Flavor": "Google"},
            timeout=3,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 300)
        if not isinstance(token, str) or not token:
            raise ValueError("metadata service returned no IAM token")
        self._token = token
        self._expires_at = now + float(expires_in)
        return token


class YandexGptNewsAnalyzer:
    """Semantic extractor with deterministic ticker prefilter and safe rule fallback."""

    version = "yandexgpt-lite-0.2.0"

    def __init__(
        self,
        *,
        folder_id: str,
        model_name: str = "yandexgpt-lite",
        timeout_seconds: float = 18,
        max_content_chars: int = 12_000,
        rules: RuleBasedNewsExtractor | None = None,
        session: requests.Session | None = None,
        token_provider: MetadataIamTokenProvider | None = None,
    ) -> None:
        if not folder_id:
            raise ValueError("folder_id is required")
        self._folder_id = folder_id
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._max_content_chars = max_content_chars
        self._rules = rules or RuleBasedNewsExtractor()
        self._session = session or requests.Session()
        self._token_provider = token_provider or MetadataIamTokenProvider()

    async def extract(self, document: NewsAnalysisInput) -> SemanticFeatures:
        baseline = self._rules.extract(document)
        if not baseline.instruments:
            return baseline
        try:
            payload = await asyncio.to_thread(self._extract_sync, document, baseline)
        except Exception as error:  # the deterministic path must remain available
            LOGGER.warning(
                "YandexGPT semantic extraction failed; using rules fallback: %s",
                type(error).__name__,
            )
            return baseline.model_copy(
                update={"extractor_version": "rules-fallback-0.1.0"}
            )
        polarity = self._reconcile_polarity(payload.polarity, baseline.polarity)
        return SemanticFeatures(
            extractor_version=self.version,
            event_type=payload.event_type,
            instruments=baseline.instruments,
            facts=payload.facts,
            polarity=polarity,
            materiality=payload.materiality,
            novelty=1.0,
            temporal_status=payload.temporal_status,
            rationale=payload.rationale,
        )

    @staticmethod
    def _reconcile_polarity(llm_polarity: float, rule_polarity: float) -> float:
        """Prevent a confident text contradiction from becoming a one-sided signal."""
        if rule_polarity == 0:
            return llm_polarity
        blended = 0.7 * llm_polarity + 0.3 * rule_polarity
        if llm_polarity * rule_polarity < 0 and abs(rule_polarity) >= 0.5:
            if abs(llm_polarity) >= 0.5:
                return 0.0
            return min(0.0, blended) if rule_polarity < 0 else max(0.0, blended)
        return round(max(-1.0, min(1.0, blended)), 4)

    def _extract_sync(
        self,
        document: NewsAnalysisInput,
        baseline: SemanticFeatures,
    ) -> LlmSemanticPayload:
        ticker_list = ", ".join(item.ticker for item in baseline.instruments)
        schema = LlmSemanticPayload.model_json_schema()
        response = self._session.post(
            CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {self._token_provider.get()}",
                "Content-Type": "application/json",
            },
            json={
                "model": f"gpt://{self._folder_id}/{self._model_name}/latest",
                "temperature": 0,
                "max_tokens": 900,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "eventedge_semantic_features",
                        "strict": True,
                        "schema": schema,
                    },
                },
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты извлекаешь только семантические признаки из официальной "
                            "финансовой новости на русском языке. Не прогнозируй цену, не "
                            "давай торговых рекомендаций и не добавляй факты, которых нет "
                            "в тексте. source_quote должен быть дословным коротким фрагментом. "
                            "polarity: -1 негативно для акционера, 0 без направленного эффекта, "
                            "+1 позитивно. materiality: 0..1 для краткосрочного движения."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Предварительно найденные тикеры: {ticker_list}.\n"
                            f"Заголовок: {document.title}\n"
                            f"Текст: {document.content[: self._max_content_chars]}"
                        ),
                    },
                ],
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("YandexGPT returned non-text content")
        return LlmSemanticPayload.model_validate_json(content)


def analyzer_from_environment(environment: Mapping[str, str]) -> NewsAnalyzer:
    enabled = environment.get("YANDEX_GPT_ENABLED", "false").casefold() in {
        "1",
        "true",
        "yes",
    }
    folder_id = environment.get("YANDEX_GPT_FOLDER_ID", "")
    if enabled and folder_id:
        return YandexGptNewsAnalyzer(
            folder_id=folder_id,
            model_name=environment.get("YANDEX_GPT_MODEL", "yandexgpt-lite"),
        )
    return RuleBasedNewsAnalyzer()
