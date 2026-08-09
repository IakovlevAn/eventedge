from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from eventedge.configs.loader import load_yaml_mapping

SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,64}$")


@dataclass(frozen=True)
class RssFeedConfig:
    source_id: str
    url: str
    language: str = "ru"
    max_items: int = 20
    timeout_seconds: float = 15
    name: str | None = None

    def __post_init__(self) -> None:
        _validate_common_source(self.source_id, self.language, self.max_items, self.timeout_seconds)
        if not self.url.startswith("https://"):
            raise ValueError(f"RSS source {self.source_id} must use an https URL")

    @property
    def config_key(self) -> str:
        return self.name or self.source_id


@dataclass(frozen=True)
class TelegramChannelConfig:
    source_id: str
    channel: str
    language: str = "ru"
    max_items: int = 20
    timeout_seconds: float = 15
    name: str | None = None

    def __post_init__(self) -> None:
        _validate_common_source(self.source_id, self.language, self.max_items, self.timeout_seconds)
        if not CHANNEL_PATTERN.fullmatch(self.channel):
            raise ValueError(f"Invalid public Telegram channel name: {self.channel}")

    @property
    def config_key(self) -> str:
        return self.name or self.source_id

    @property
    def url(self) -> str:
        return f"https://t.me/s/{self.channel}"


@dataclass(frozen=True)
class SourceConfig:
    cbr_press: RssFeedConfig
    moex_news: RssFeedConfig
    market_news_feeds: tuple[RssFeedConfig, ...]
    telegram_channels: tuple[TelegramChannelConfig, ...]

    @property
    def fast_news_feeds(self) -> tuple[RssFeedConfig, ...]:
        direct = tuple(
            config
            for config in self.market_news_feeds
            if config.source_id in {"interfax", "tass", "rbc"}
        )
        return (
            *direct,
            replace(
                self.moex_news,
                name="moex_news_fast",
                max_items=100,
                timeout_seconds=10,
            ),
        )

    @property
    def discovery_news_feeds(self) -> tuple[RssFeedConfig, ...]:
        return tuple(
            config
            for config in self.market_news_feeds
            if config.source_id in {"google_news", "market_background"}
        )


def _validate_common_source(
    source_id: str,
    language: str,
    max_items: int,
    timeout_seconds: float,
) -> None:
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise ValueError(f"Invalid source_id: {source_id}")
    if language not in {"ru", "en"}:
        raise ValueError(f"Unsupported source language: {language}")
    if not 1 <= max_items <= 1000:
        raise ValueError("max_items must be between 1 and 1000")
    if not 0 < timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be between 0 and 60")


def google_news_search_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"({query}) when:30d", "hl": "ru", "gl": "RU", "ceid": "RU:ru"}
    )


def default_source_config() -> SourceConfig:
    market_news_feeds = (
        RssFeedConfig(
            name="google_banks",
            source_id="google_news",
            url=google_news_search_url("Сбербанк OR ВТБ"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_gas",
            source_id="google_news",
            url=google_news_search_url("Газпром OR Новатэк"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_oil",
            source_id="google_news",
            url=google_news_search_url("Лукойл OR Роснефть"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_oil_secondary",
            source_id="google_news",
            url=google_news_search_url("Татнефть OR Газпром нефть"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_tech_retail",
            source_id="google_news",
            url=google_news_search_url("Яндекс OR Магнит"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_metals",
            source_id="google_news",
            url=google_news_search_url("Норникель OR Полюс"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_industrials",
            source_id="google_news",
            url=google_news_search_url("Северсталь OR АЛРОСА OR Московская биржа"),
            max_items=100,
        ),
        RssFeedConfig(
            name="google_market_background",
            source_id="market_background",
            url=google_news_search_url(
                '"российский рынок" OR "ключевая ставка" OR рубль OR Brent OR санкции'
            ),
            max_items=100,
        ),
        RssFeedConfig(
            name="interfax",
            source_id="interfax",
            url="https://www.interfax.ru/rss",
            max_items=50,
        ),
        RssFeedConfig(
            name="tass",
            source_id="tass",
            url="https://tass.ru/rss/v2.xml",
            max_items=100,
        ),
        RssFeedConfig(
            name="rbc",
            source_id="rbc",
            url="https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
            max_items=50,
        ),
    )
    return SourceConfig(
        cbr_press=RssFeedConfig(
            name="cbr_press",
            source_id="cbr_press",
            url="https://www.cbr.ru/rss/RssPress",
            max_items=10,
        ),
        moex_news=RssFeedConfig(
            name="moex_news",
            source_id="moex_news",
            url="https://www.moex.com/export/news.aspx?cat=100",
            max_items=1000,
            timeout_seconds=20,
        ),
        market_news_feeds=market_news_feeds,
        telegram_channels=(
            TelegramChannelConfig(
                name="telegram_ak47pfl",
                source_id="telegram_ak47pfl",
                channel="AK47pfl",
                max_items=20,
                timeout_seconds=10,
            ),
            TelegramChannelConfig(
                name="telegram_markettwits",
                source_id="telegram_markettwits",
                channel="markettwits",
                max_items=20,
                timeout_seconds=10,
            ),
            TelegramChannelConfig(
                name="telegram_centralbank_russia",
                source_id="telegram_centralbank_russia",
                channel="centralbank_russia",
                max_items=20,
                timeout_seconds=10,
            ),
            TelegramChannelConfig(
                name="telegram_moscowexchangeofficial",
                source_id="telegram_moscowexchangeofficial",
                channel="MoscowExchangeOfficial",
                max_items=20,
                timeout_seconds=10,
            ),
            TelegramChannelConfig(
                name="telegram_bcs_express",
                source_id="telegram_bcs_express",
                channel="bcs_express",
                max_items=20,
                timeout_seconds=10,
            ),
            TelegramChannelConfig(
                name="telegram_russianmacro",
                source_id="telegram_russianmacro",
                channel="russianmacro",
                max_items=20,
                timeout_seconds=10,
            ),
        ),
    )


def load_source_config(directory: str | Path | None = None) -> SourceConfig:
    defaults = default_source_config()
    overrides = load_yaml_mapping("sources.yaml", directory=directory)
    allowed = {"cbr_press", "moex_news", "market_news_feeds", "telegram_channels"}
    _reject_unknown_keys(overrides, allowed, "sources.yaml")
    return SourceConfig(
        cbr_press=_override_rss(defaults.cbr_press, overrides.get("cbr_press")),
        moex_news=_override_rss(defaults.moex_news, overrides.get("moex_news")),
        market_news_feeds=_override_rss_group(
            defaults.market_news_feeds,
            overrides.get("market_news_feeds"),
        ),
        telegram_channels=_override_telegram_group(
            defaults.telegram_channels,
            overrides.get("telegram_channels"),
        ),
    )


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a mapping")
    return value


def _reject_unknown_keys(
    values: Mapping[str, object],
    allowed: set[str],
    context: str,
) -> None:
    if unknown := set(values) - allowed:
        raise ValueError(f"Unknown keys in {context}: {', '.join(sorted(unknown))}")


def _override_rss(default: RssFeedConfig, raw: object) -> RssFeedConfig:
    if raw is None:
        return default
    values = _mapping(raw, default.config_key)
    allowed = {"source_id", "url", "language", "max_items", "timeout_seconds"}
    _reject_unknown_keys(values, allowed, default.config_key)
    return replace(default, **values)


def _override_rss_group(
    defaults: tuple[RssFeedConfig, ...],
    raw: object,
) -> tuple[RssFeedConfig, ...]:
    if raw is None:
        return defaults
    overrides = _mapping(raw, "market_news_feeds")
    _reject_unknown_keys(overrides, {item.config_key for item in defaults}, "market_news_feeds")
    return tuple(_override_rss(item, overrides.get(item.config_key)) for item in defaults)


def _override_telegram_group(
    defaults: tuple[TelegramChannelConfig, ...],
    raw: object,
) -> tuple[TelegramChannelConfig, ...]:
    if raw is None:
        return defaults
    overrides = _mapping(raw, "telegram_channels")
    _reject_unknown_keys(overrides, {item.config_key for item in defaults}, "telegram_channels")
    configured: list[TelegramChannelConfig] = []
    for item in defaults:
        values = overrides.get(item.config_key)
        if values is None:
            configured.append(item)
            continue
        mapping = _mapping(values, item.config_key)
        allowed = {"source_id", "channel", "language", "max_items", "timeout_seconds"}
        _reject_unknown_keys(mapping, allowed, item.config_key)
        configured.append(replace(item, **mapping))
    return tuple(configured)
