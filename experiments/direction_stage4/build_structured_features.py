from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    load_dataset,
    sha256_file,
)
from experiments.paths import stage_artifact_directory  # noqa: E402

EXTRACTOR_VERSION = "direction-structured-features-1.0"

# Frozen before looking at stage-4 outcomes. This is the same mapping used by
# src/eventedge/events.py and covers every ticker in the research dataset.
TICKER_SECTORS = {
    "AFKS": "holdings",
    "AFLT": "transport",
    "AKRN": "chemicals",
    "ALRS": "metals",
    "ASTR": "technology",
    "BANE": "oil_gas",
    "BELU": "consumer",
    "BSPB": "financials",
    "CBOM": "financials",
    "CHMF": "metals",
    "DELI": "transport",
    "DOMRF": "financials",
    "ENPG": "metals",
    "FEES": "power",
    "FIXP": "retail",
    "FLOT": "transport",
    "GAZP": "oil_gas",
    "GMKN": "metals",
    "HEAD": "technology",
    "HYDR": "power",
    "IRAO": "power",
    "KMAZ": "industrial",
    "LEAS": "financials",
    "LKOH": "oil_gas",
    "LSRG": "real_estate",
    "MAGN": "metals",
    "MDMG": "healthcare",
    "MGNT": "retail",
    "MTSS": "telecom",
    "MVID": "retail",
    "NLMK": "metals",
    "NMTP": "transport",
    "NVTK": "oil_gas",
    "OGKB": "power",
    "OZON": "retail",
    "PHOR": "chemicals",
    "PIKK": "real_estate",
    "PLZL": "metals",
    "POSI": "technology",
    "RENI": "financials",
    "ROSN": "oil_gas",
    "RTKM": "telecom",
    "RUAL": "metals",
    "SBER": "financials",
    "SELG": "metals",
    "SGZH": "forestry",
    "SIBN": "oil_gas",
    "SMLT": "real_estate",
    "SNGS": "oil_gas",
    "SOFL": "technology",
    "TATN": "oil_gas",
    "TGKA": "power",
    "TRNFP": "oil_gas",
    "UGLD": "metals",
    "UNAC": "industrial",
    "UPRO": "power",
    "VKCO": "technology",
    "VSMO": "metals",
    "VTBR": "financials",
    "WUSH": "transport",
    "X5": "retail",
    "YDEX": "technology",
}

POSITIVE_ROOTS = (
    "вырос",
    "выросл",
    "рост",
    "увелич",
    "улучш",
    "выше",
    "ускор",
    "рекорд",
    "позитив",
    "повыс",
    "восстанов",
    "одобр",
    "утверд",
    "выкуп",
    "байбэк",
)
NEGATIVE_ROOTS = (
    "сниз",
    "упал",
    "упала",
    "упали",
    "паден",
    "сократ",
    "ухудш",
    "ниже",
    "замедл",
    "негатив",
    "отмен",
    "приостанов",
    "банкрот",
    "штраф",
    "авари",
    "дефолт",
    "разводнен",
)
UNCERTAINTY_ROOTS = (
    "может",
    "возможн",
    "планир",
    "ожида",
    "прогноз",
    "намерен",
    "рассмотр",
    "предлож",
    "обсуд",
)
CONFIRMATION_ROOTS = (
    "утверд",
    "одобр",
    "заверш",
    "получил",
    "подписал",
    "заключил",
    "вступил в силу",
)
BENEFICIAL_METRIC_ROOTS = (
    "прибыл",
    "выруч",
    "ebitda",
    "ebit",
    "денежн",
    "рентабель",
    "продаж",
    "производ",
    "добыч",
    "выпуск",
    "трафик",
    "клиент",
)
ADVERSE_METRIC_ROOTS = (
    "убыт",
    "долг",
    "расход",
    "затрат",
    "себестоим",
    "отток",
    "просроч",
    "авари",
    "штраф",
)

NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
UNIT = r"(?:трлн|млрд|млн|тыс)"
COMPARISON_RE = re.compile(
    rf"(?P<first>{NUMBER})\s*(?P<first_unit>{UNIT})?[^\d\n]{{0,24}}"
    rf"(?:против|vs\.?)\s*(?P<second>{NUMBER})\s*(?P<second_unit>{UNIT})?",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(
    rf"(?P<movement>вырос\w*|увелич\w*|повыс\w*|сниз\w*|упал\w*|упала|упали|"
    rf"сократ\w*|уменьш\w*)[^.%\n]{{0,36}}?(?P<value>{NUMBER})\s*%",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(NUMBER)
PERCENT_TOKEN_RE = re.compile(rf"{NUMBER}\s*%")

SUBTYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "dividend_gap",
        ("последн.*день.*дивиденд", "дивидендн.*гэп", "торговаться.*без дивиденд"),
    ),
    ("dividend_cancel", ("не выплачивать дивиденд", "отмен.*дивиденд")),
    ("dilution", ("допэмисс", "дополнительн.*размещен", "размыт", "разводнен")),
    ("buyback", ("обратн.*выкуп", "байбэк", "buyback")),
    ("rating_up", ("повыс.*рейтинг", "рейтинг.*повыш", "улучш.*прогноз.*рейтинг")),
    ("rating_down", ("сниз.*рейтинг", "рейтинг.*сниж", "ухудш.*прогноз.*рейтинг")),
    ("sanctions_relief", ("сня.*санкц", "ослаб.*санкц", "лицензи.*продаж.*актив")),
    ("sanctions", ("санкц", "ограничен")),
    ("bankruptcy", ("банкрот", "дефолт")),
    ("dividend", ("дивиденд",)),
    ("financial_results", ("отчетност", "финансов.*результ", "мсфо", "рсбу")),
    ("production", ("производствен.*результ", "объем.*производ", "добыч", "выпуск")),
    ("guidance", ("прогноз", "гайденс", "ожидает по итогам")),
    ("contract", ("контракт", "договор", "соглашен", "партнерств")),
    ("legal", ("суд", "иск", "фас", "штраф")),
    ("management", ("совет директор", "гендиректор", "руководител", "назнач")),
)

SPECIFIC_SUBTYPE_SIGNAL = {
    "dividend_gap": -1.0,
    "dividend_cancel": -1.0,
    "dilution": -1.0,
    "buyback": 1.0,
    "rating_up": 1.0,
    "rating_down": -1.0,
    "sanctions_relief": 1.0,
    "sanctions": -1.0,
    "bankruptcy": -1.0,
}


def _model_text(row: pd.Series) -> str:
    title = str(row["title"]).strip()
    content = str(row["content"]).strip()
    if not content:
        return title
    if title and content.casefold().startswith(title.casefold()):
        return content
    return f"{title}\n{content}" if title else content


def _count_roots(text: str, roots: tuple[str, ...]) -> int:
    return sum(len(re.findall(re.escape(root), text)) for root in roots)


def _metric_polarity(context: str) -> int:
    beneficial = max((context.rfind(root) for root in BENEFICIAL_METRIC_ROOTS), default=-1)
    adverse = max((context.rfind(root) for root in ADVERSE_METRIC_ROOTS), default=-1)
    if beneficial < 0 and adverse < 0:
        return 0
    return 1 if beneficial > adverse else -1


def _scaled_number(value: str, unit: str | None) -> float:
    parsed = float(value.replace(",", "."))
    scale = {None: 1.0, "тыс": 1e3, "млн": 1e6, "млрд": 1e9, "трлн": 1e12}
    return parsed * scale[unit.casefold() if unit else None]


def _comparison_features(text: str) -> tuple[int, float, float]:
    signals: list[float] = []
    magnitudes: list[float] = []
    for match in COMPARISON_RE.finditer(text):
        context = text[max(0, match.start() - 100) : match.start()]
        polarity = _metric_polarity(context)
        if polarity == 0:
            continue
        first = _scaled_number(match.group("first"), match.group("first_unit"))
        second = _scaled_number(match.group("second"), match.group("second_unit"))
        # If only one side specifies a scale, assume the omitted side uses the same unit.
        if match.group("first_unit") and not match.group("second_unit"):
            second = _scaled_number(match.group("second"), match.group("first_unit"))
        elif match.group("second_unit") and not match.group("first_unit"):
            first = _scaled_number(match.group("first"), match.group("second_unit"))
        if (
            not match.group("first_unit")
            and not match.group("second_unit")
            and 1900 <= first <= 2100
            and 1900 <= second <= 2100
        ):
            continue
        if abs(second) < 1e-12:
            continue
        relative = (first - second) / abs(second)
        signals.append(float(np.sign(relative) * polarity))
        magnitudes.append(min(abs(relative), 10.0))
    if not signals:
        return 0, 0.0, 0.0
    return len(signals), float(np.mean(signals)), float(math.log1p(max(magnitudes)))


def _percent_change_features(text: str) -> tuple[int, float, float]:
    signals: list[float] = []
    magnitudes: list[float] = []
    for match in PERCENT_RE.finditer(text):
        movement = match.group("movement").casefold()
        down_roots = ("сниз", "упал", "упала", "упали", "сократ", "уменьш")
        movement_sign = -1 if movement.startswith(down_roots) else 1
        context = text[max(0, match.start() - 100) : match.start()]
        polarity = _metric_polarity(context)
        signals.append(float(movement_sign * (polarity or 1)))
        magnitudes.append(min(abs(float(match.group("value").replace(",", "."))), 500.0))
    if not signals:
        return 0, 0.0, 0.0
    weights = np.array([math.log1p(value) for value in magnitudes])
    signal = float(np.average(np.array(signals), weights=weights)) if weights.sum() else 0.0
    return len(signals), signal, float(math.log1p(max(magnitudes)))


def _event_subtype(text: str, fallback: str) -> str:
    for subtype, patterns in SUBTYPE_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            return subtype
    return fallback


def _signal_bin(value: float) -> str:
    if value <= -0.15:
        return "negative"
    if value >= 0.15:
        return "positive"
    return "neutral"


def extract_text_features(row: pd.Series) -> dict[str, Any]:
    original = _model_text(row)
    text = re.sub(r"\s+", " ", original.casefold()).strip()
    positive = _count_roots(text, POSITIVE_ROOTS)
    negative = _count_roots(text, NEGATIVE_ROOTS)
    uncertainty = _count_roots(text, UNCERTAINTY_ROOTS)
    confirmation = _count_roots(text, CONFIRMATION_ROOTS)
    comparison_count, comparison_signal, comparison_strength = _comparison_features(text)
    percent_count, percent_signal, percent_strength = _percent_change_features(text)
    # Long analytical posts mention many unrelated risks. Classify subtype from
    # the headline and lead instead of letting a late incidental word dominate.
    subtype_text = text[:800]
    subtype = _event_subtype(subtype_text, str(row["event_type"]))
    specific_signal = SPECIFIC_SUBTYPE_SIGNAL.get(subtype, 0.0)
    if subtype == "dilution" and re.search(
        r"допэмисс[^.\n]{0,80}(?:не рассматри|не планир)|"
        r"(?:не рассматри|не планир)[^.\n]{0,80}допэмисс",
        subtype_text,
    ):
        specific_signal = 0.0
    lexical_signal = (positive - negative) / max(positive + negative, 1)
    components = [value for value in (comparison_signal, percent_signal) if value != 0]
    evidence_signal = float(np.mean(components)) if components else 0.0
    structured_signal = float(
        np.tanh(0.7 * lexical_signal + 1.2 * evidence_signal + 1.0 * specific_signal)
    )
    return {
        "id": row["id"],
        "text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "model_text": original,
        "sector": TICKER_SECTORS[row["ticker"]],
        "event_subtype": subtype,
        "text_char_count_log": math.log1p(len(original)),
        "text_token_count_log": math.log1p(len(text.split())),
        "number_count_log": math.log1p(len(NUMBER_RE.findall(text))),
        "percent_token_count_log": math.log1p(len(PERCENT_TOKEN_RE.findall(text))),
        "positive_marker_count_log": math.log1p(positive),
        "negative_marker_count_log": math.log1p(negative),
        "uncertainty_marker_count_log": math.log1p(uncertainty),
        "confirmation_marker_count_log": math.log1p(confirmation),
        "lexical_signal": lexical_signal,
        "comparison_count_log": math.log1p(comparison_count),
        "comparison_signal": comparison_signal,
        "comparison_strength_log": comparison_strength,
        "percent_change_count_log": math.log1p(percent_count),
        "percent_change_signal": percent_signal,
        "percent_change_strength_log": percent_strength,
        "specific_event_signal": specific_signal,
        "structured_signal": structured_signal,
        "structured_signal_bin": _signal_bin(structured_signal),
        "has_consensus": int(bool(re.search(r"консенсус|ожидани|прогноз аналитик", text))),
        "has_previous_period": int(bool(re.search(r"годом ранее|г[./]г|квартал.*ранее", text))),
        "has_negation": int(bool(re.search(r"\bне\b|\bнет\b|без ", text))),
    }


def _regime(value: Any, *, threshold: float) -> str:
    if value is None or pd.isna(value):
        return "missing"
    if float(value) <= -threshold:
        return "down"
    if float(value) >= threshold:
        return "up"
    return "flat"


def _session(timestamp: pd.Timestamp) -> str:
    local_hour = timestamp.tz_convert("Europe/Moscow").hour
    if local_hour < 10:
        return "open"
    if local_hour < 15:
        return "midday"
    return "close"


def build_structured_features(
    frame: pd.DataFrame,
    market: pd.DataFrame,
) -> pd.DataFrame:
    unknown = sorted(set(frame["ticker"]) - set(TICKER_SECTORS))
    if unknown:
        raise AssertionError(f"sector mapping missing tickers: {unknown}")
    extracted = pd.DataFrame([extract_text_features(row) for _, row in frame.iterrows()])
    market_columns = [
        "id",
        "benchmark_decision_return_60m_pct",
        "abnormal_decision_return_60m_pct",
        "abnormal_reaction_0_5m_pct",
    ]
    result = extracted.merge(market[market_columns], on="id", validate="one_to_one")
    decision = frame.set_index("id")["decision_at"]
    event_type = frame.set_index("id")["event_type"]
    result["market_regime"] = result["benchmark_decision_return_60m_pct"].map(
        lambda value: _regime(value, threshold=0.2)
    )
    result["relative_momentum_regime"] = result["abnormal_decision_return_60m_pct"].map(
        lambda value: _regime(value, threshold=0.2)
    )
    result["reaction_regime"] = result["abnormal_reaction_0_5m_pct"].map(
        lambda value: _regime(value, threshold=0.05)
    )
    result["decision_session"] = result["id"].map(decision).map(_session)
    result["event_type_x_signal"] = [
        f"{event_type[row_id]}:{signal}"
        for row_id, signal in zip(result["id"], result["structured_signal_bin"], strict=True)
    ]
    result["sector_x_signal"] = (
        result["sector"].astype(str) + ":" + result["structured_signal_bin"].astype(str)
    )
    result = result.drop(
        columns=[
            "benchmark_decision_return_60m_pct",
            "abnormal_decision_return_60m_pct",
            "abnormal_reaction_0_5m_pct",
        ]
    )
    if len(result) != len(frame) or result["id"].duplicated().any():
        raise AssertionError("structured features do not map one-to-one to the dataset")
    return result


def _self_test_extractor() -> None:
    def features(text: str, event_type: str = "other") -> dict[str, Any]:
        return extract_text_features(
            pd.Series(
                {
                    "id": "synthetic",
                    "ticker": "SBER",
                    "title": text,
                    "content": "",
                    "event_type": event_type,
                }
            )
        )

    lower_profit = features("Прибыль 10,7 млрд рублей против 28,59 млрд годом ранее")
    assert lower_profit["comparison_signal"] < 0
    lower_loss = features("Убыток снизился на 30%")
    assert lower_loss["percent_change_signal"] > 0
    higher_revenue = features("Выручка выросла на 20%")
    assert higher_revenue["percent_change_signal"] > 0
    dilution = features("Компания проведет допэмиссию акций")
    assert dilution["event_subtype"] == "dilution" and dilution["specific_event_signal"] < 0
    rejected_dilution = features("Компания сообщила: допэмиссия не рассматривается")
    assert rejected_dilution["specific_event_signal"] == 0
    rating = features("АКРА повысило кредитный рейтинг компании")
    assert rating["event_subtype"] == "rating_up" and rating["specific_event_signal"] > 0
    dividend = features("Совет директоров решил не выплачивать дивиденды")
    assert dividend["event_subtype"] == "dividend_cancel"
    dividend_gap = features("Последний день с дивидендами, затем торги без дивидендов")
    assert dividend_gap["event_subtype"] == "dividend_gap"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stage-4 structured news features")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--market-features",
        type=Path,
        default=stage_artifact_directory("market_stage2") / "market_features.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=stage_artifact_directory("direction_stage4") / "structured_features.csv",
    )
    arguments = parser.parse_args()
    _self_test_extractor()
    frame = load_dataset(arguments.dataset)
    market = pd.read_csv(arguments.market_features)
    features = build_structured_features(frame, market)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    features.to_csv(arguments.output, index=False, float_format="%.10g")
    report = {
        "extractor_version": EXTRACTOR_VERSION,
        "dataset_sha256": sha256_file(arguments.dataset),
        "market_features_sha256": sha256_file(arguments.market_features),
        "rows": len(features),
        "structured_features_sha256": sha256_file(arguments.output),
        "sectors": features["sector"].value_counts().sort_index().to_dict(),
        "event_subtypes": features["event_subtype"].value_counts().sort_index().to_dict(),
        "coverage": {
            "comparison_signal_nonzero": float((features["comparison_signal"] != 0).mean()),
            "percent_change_signal_nonzero": float(
                (features["percent_change_signal"] != 0).mean()
            ),
            "specific_event_signal_nonzero": float(
                (features["specific_event_signal"] != 0).mean()
            ),
            "structured_signal_nonzero": float((features["structured_signal"] != 0).mean()),
        },
    }
    (arguments.output.parent / "feature_build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
