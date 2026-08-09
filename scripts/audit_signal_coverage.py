from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from eventedge.collectors import (
    RssItem,
    is_google_market_signal_candidate,
    is_market_signal_candidate,
)

STOP_WORDS = {
    "для",
    "как",
    "при",
    "что",
    "это",
    "или",
    "после",
    "перед",
    "уже",
    "акции",
    "акция",
    "рублей",
    "года",
    "году",
}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-zа-яё0-9]+", " ", title.casefold()).split()
        if len(token) > 2 and token not in STOP_WORDS
    }


def same_event(left: dict[str, object], right: dict[str, object]) -> bool:
    if left["ticker"] != right["ticker"]:
        return False
    left_news = left["news"]
    right_news = right["news"]
    assert isinstance(left_news, dict) and isinstance(right_news, dict)
    left_tokens = title_tokens(str(left_news["title"]))
    right_tokens = title_tokens(str(right_news["title"]))
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    if abs((left["published_at"] - right["published_at"]).total_seconds()) > 72 * 3600:
        return False
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) >= 0.6


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * ratio))
    return round(ordered[index], 1)


def audit(news_payload: dict[str, object], eval_payload: dict[str, object]) -> dict[str, object]:
    news = news_payload.get("data", [])
    assert isinstance(news, list)
    news_by_id = {str(item["id"]): item for item in news if isinstance(item, dict)}
    new_candidates = []
    old_candidates = []
    old_signaled = []
    lags = []
    by_source: Counter[str] = Counter()
    newly_by_source: Counter[str] = Counter()

    for item in news_by_id.values():
        published = parse_time(str(item["published_at"]))
        received = parse_time(str(item["received_at"]))
        lags.append(max(0.0, (received - published).total_seconds() / 60))
        metadata = item.get("source_metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("signal_candidate"):
            old_candidates.append(item)
        related = item.get("related_signals", [])
        if isinstance(related, list) and related:
            old_signaled.append(item)

        categories = metadata.get("categories", [])
        rss_item = RssItem(
            external_id=str(item["external_id"]),
            published_at=published,
            title=str(item["title"]),
            url=str(item["url"]),
            content=str(item["content"]),
            categories=tuple(str(value) for value in categories)
            if isinstance(categories, list)
            else (),
        )
        source_id = str(item["source_id"])
        if source_id in {"cbr_press", "market_background"}:
            current_candidate = False
        elif source_id == "google_news":
            current_candidate = is_google_market_signal_candidate(rss_item)
        else:
            current_candidate = is_market_signal_candidate(rss_item)
        if current_candidate:
            new_candidates.append(item)
            by_source[source_id] += 1
            if not metadata.get("signal_candidate"):
                newly_by_source[source_id] += 1

    eval_data = eval_payload.get("data", {})
    eval_data = eval_data if isinstance(eval_data, dict) else {}
    outcomes = eval_data.get("outcomes", [])
    outcomes = outcomes if isinstance(outcomes, list) else []
    comparable = []
    stale_eval = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        outcome_news = outcome.get("news")
        if not isinstance(outcome_news, dict):
            continue
        stored = news_by_id.get(str(outcome_news.get("id")))
        if stored is None:
            continue
        published = parse_time(str(stored["published_at"]))
        as_of = parse_time(str(outcome["as_of"]))
        item = {**outcome, "news": outcome_news, "published_at": published}
        comparable.append(item)
        if (as_of - published).total_seconds() > 6 * 3600:
            stale_eval.append(item)

    groups: list[list[dict[str, object]]] = []
    for item in sorted(comparable, key=lambda value: str(value["as_of"])):
        group = next(
            (candidate for candidate in groups if same_event(candidate[0], item)),
            None,
        )
        if group is None:
            groups.append([item])
        else:
            group.append(item)

    duplicate_groups = [group for group in groups if len(group) > 1]
    return {
        "news": {
            "total": len(news_by_id),
            "old_candidates": len(old_candidates),
            "old_signaled_news": len(old_signaled),
            "current_gate_candidates": len(new_candidates),
            "newly_eligible": len(new_candidates) - len(
                [item for item in new_candidates if item in old_candidates]
            ),
            "candidate_by_source": dict(by_source.most_common()),
            "newly_eligible_by_source": dict(newly_by_source.most_common()),
            "delivery_lag_minutes": {
                "median": round(statistics.median(lags), 1) if lags else None,
                "p90": percentile(lags, 0.9),
                "p99": percentile(lags, 0.99),
                "over_120_minutes": sum(value > 120 for value in lags),
            },
            "newly_eligible_examples": [
                {
                    "source_id": item["source_id"],
                    "published_at": item["published_at"],
                    "title": item["title"],
                }
                for item in new_candidates
                if item not in old_candidates
            ][:20],
        },
        "evals": {
            "raw_outcomes": len(comparable),
            "event_level_outcomes": len(groups),
            "duplicate_outcomes": len(comparable) - len(groups),
            "duplicate_groups": len(duplicate_groups),
            "stale_as_of_over_6h": len(stale_eval),
            "duplicate_examples": [
                {
                    "ticker": group[0]["ticker"],
                    "count": len(group),
                    "titles": [str(item["news"]["title"]) for item in group],
                }
                for group in duplicate_groups[:10]
            ],
            "stale_examples": [
                {
                    "ticker": item["ticker"],
                    "as_of": item["as_of"],
                    "published_at": item["published_at"].isoformat(),
                    "title": item["news"]["title"],
                }
                for item in stale_eval[:10]
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit EventEdge signal coverage and Evals")
    parser.add_argument("--news", type=Path, required=True)
    parser.add_argument("--evals", type=Path, required=True)
    args = parser.parse_args()
    result = audit(
        json.loads(args.news.read_text(encoding="utf-8")),
        json.loads(args.evals.read_text(encoding="utf-8")),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
