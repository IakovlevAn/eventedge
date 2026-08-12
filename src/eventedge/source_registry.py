from __future__ import annotations

from eventedge.configs.sources import load_source_config

SOURCE_DETAILS: dict[str, dict[str, object]] = {
    "cbr_press": {
        "name": "Банк России",
        "kind": "Первичный",
        "quality": 95,
        "freshness": "до 15 мин",
        "role": "Пресс-релизы и решения регулятора",
        "url": "https://www.cbr.ru/press/",
    },
    "moex_news": {
        "name": "Московская биржа",
        "kind": "Первичный",
        "quality": 95,
        "freshness": "до 5 мин",
        "role": "Сообщения биржи и раскрытия эмитентов",
        "url": "https://www.moex.com/ru/news/",
    },
    "interfax": {
        "name": "Интерфакс",
        "kind": "Агентство",
        "quality": 90,
        "freshness": "цель ≤ 2 мин",
        "role": "Оперативные корпоративные и рыночные новости",
        "url": "https://www.interfax.ru/business/",
    },
    "tass": {
        "name": "ТАСС",
        "kind": "Агентство",
        "quality": 82,
        "freshness": "цель ≤ 2 мин",
        "role": "Подтверждение значимых событий",
        "url": "https://tass.ru/ekonomika",
    },
    "rbc": {
        "name": "РБК",
        "kind": "Медиа",
        "quality": 78,
        "freshness": "цель ≤ 2 мин",
        "role": "Рыночный контекст и дополнительное подтверждение",
        "url": "https://www.rbc.ru/quote/",
    },
    "google_news": {
        "name": "Google News",
        "kind": "Discovery",
        "quality": 74,
        "freshness": "до 5 мин",
        "role": "Поиск публикаций; не заменяет первичный источник",
        "url": "https://news.google.com/",
    },
    "market_background": {
        "name": "Рыночный фон",
        "kind": "Контекст",
        "quality": 74,
        "freshness": "до 5 мин",
        "role": "Ставка, рубль, нефть, санкции и общий фон",
        "url": "https://news.google.com/",
    },
    "telegram_ak47pfl": {
        "name": "AK47 PFL",
        "quality": 68,
        "role": "Оперативные рыночные сообщения",
    },
    "telegram_markettwits": {
        "name": "MarketTwits",
        "quality": 68,
        "role": "Оперативный корпоративный и рыночный поток",
    },
    "telegram_centralbank_russia": {
        "name": "Банк России · Telegram",
        "quality": 95,
        "role": "Оперативные решения и комментарии регулятора",
    },
    "telegram_moscowexchangeofficial": {
        "name": "MOEX · Telegram",
        "quality": 95,
        "role": "Новости торгов и инфраструктуры рынка",
    },
    "telegram_bcs_express": {
        "name": "БКС Экспресс",
        "quality": 82,
        "role": "Корпоративные события и обзоры рынка",
    },
    "telegram_russianmacro": {
        "name": "MMI",
        "quality": 78,
        "role": "Российский и глобальный макроэкономический фон",
    },
}


def configured_sources() -> list[dict[str, object]]:
    config = load_source_config()
    ids = [
        "cbr_press",
        "moex_news",
        "interfax",
        "tass",
        "rbc",
        "google_news",
        "market_background",
    ]
    sources = [
        {"source_id": source_id, "enabled": True, "managed": False, **SOURCE_DETAILS[source_id]}
        for source_id in ids
    ]
    for channel in config.telegram_channels:
        details = SOURCE_DETAILS.get(channel.source_id, {})
        sources.append(
            {
                "source_id": channel.source_id,
                "channel": channel.channel,
                "name": details.get("name", f"@{channel.channel}"),
                "kind": "Telegram",
                "quality": details.get("quality", 65),
                "freshness": "до 5 мин",
                "role": details.get("role", "Публичный Telegram-канал"),
                "url": channel.url,
                "enabled": True,
                "managed": False,
            }
        )
    sources.append(
        {
            "source_id": "moex_iss",
            "name": "MOEX ISS",
            "kind": "Рыночные данные",
            "quality": 100,
            "freshness": "до 60 сек",
            "role": "Цена, объём, свечи, ликвидность и волатильность",
            "url": "https://iss.moex.com/iss/",
            "enabled": True,
            "managed": False,
        }
    )
    return sources
