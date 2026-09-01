const COHORTS = new Set(["live", "retrospective"]);
const VERDICT_STATUSES = new Set([
  "evaluated",
  "non_directional",
  "not_applicable",
  "pending",
  "missed_window",
  "unavailable",
]);

const REASON_LABELS = {
  signal_as_of_after_creation: "Время сигнала позже времени его создания",
  data_cutoff_after_creation: "Срез данных позже времени создания сигнала",
  missing_evidence: "Источник сигнала не сохранён",
  evidence_published_after_signal: "Новость опубликована после расчёта сигнала",
  evidence_received_after_signal: "Источник получен после расчёта сигнала",
};

function numericReturn(outcome, horizon) {
  const value = outcome?.returns?.[horizon];
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? Number(value)
    : null;
}

function processingLagLabel(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value < 60) return `${Math.round(value)} с`;
  if (value < 3600) return `${Math.round(value / 60)} мин`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.round((value % 3600) / 60);
  return minutes ? `${hours} ч ${minutes} мин` : `${hours} ч`;
}

function eligibilityReason(outcome) {
  const eligibility = outcome?.eligibility || {};
  if (typeof eligibility.reason_label === "string" && eligibility.reason_label.trim()) {
    return eligibility.reason_label.trim();
  }
  if (eligibility.reason === "retrospective_signal") {
    const lag = processingLagLabel(eligibility.processing_lag_seconds);
    return lag
      ? `Рассчитан позже live-окна на ${lag}`
      : "Рассчитан позже live-окна";
  }
  return REASON_LABELS[eligibility.reason] || "";
}

function outcomeCohort(outcome) {
  const eligibility = outcome?.eligibility || {};
  const explicit = eligibility.cohort || outcome?.cohort;
  if (COHORTS.has(explicit)) return explicit;
  return eligibility.eligible === false || outcome?.status === "excluded"
    ? "retrospective"
    : "live";
}

function primaryWindowMissed(outcome, now) {
  const observations = outcome?.horizon_observations || {};
  const primaryObservations = [observations["1h"], observations["4h"]]
    .filter((item) => item && typeof item === "object");
  if (primaryObservations.some((item) => item.timely === true)) return false;
  if (primaryObservations.some((item) => item.timely === false && item.observed_at)) {
    return true;
  }

  const oneHour = observations["1h"];
  const targetAt = Date.parse(oneHour?.target_at || "");
  if (!Number.isFinite(targetAt)) return false;
  // The backend accepts a horizon observation only within 20 minutes of its
  // target. Once this window is over, a later candle cannot repair the verdict.
  return targetAt + 20 * 60 * 1000 < now;
}

function fallbackVerdictStatus(outcome, now) {
  if (outcome?.status === "excluded") return "legacy_excluded";
  if (outcome?.status === "unavailable") return "unavailable";
  if (outcome?.verdict !== null && outcome?.verdict !== undefined) return "evaluated";
  if (outcome?.status === "evaluated") return "missed_window";
  return primaryWindowMissed(outcome, now) ? "missed_window" : "pending";
}

function observedHorizon(outcome) {
  return ["4h", "1h"].find((horizon) => numericReturn(outcome, horizon) !== null) || null;
}

function outcomeStateLabel(outcome, verdictStatus) {
  if (verdictStatus === "non_directional") return "Не участвует в hit rate";
  if (verdictStatus === "legacy_excluded") return "Старая методика";
  if (verdictStatus === "missed_window") return "Нет валидного outcome за 1–4 часа";
  if (verdictStatus === "unavailable") return "Нет точки входа или рыночной истории";
  if (verdictStatus === "pending") return "Окно первого outcome ещё открыто";

  const horizon = observedHorizon(outcome);
  const horizonLabel = horizon === "4h" ? "4 часа" : horizon === "1h" ? "1 час" : "1–4 часа";
  return outcome?.status === "partial"
    ? `Частичный outcome · ${horizonLabel}`
    : `Оценён · ${horizonLabel}`;
}

export function evalOutcomeView(outcome, now = Date.now()) {
  const explicitVerdictStatus = outcome?.verdict_status;
  const verdictStatus = outcome?.direction === "neutral"
    || explicitVerdictStatus === "non_directional"
    || explicitVerdictStatus === "not_applicable"
    ? "non_directional"
    : outcome?.status === "excluded"
      ? "legacy_excluded"
      : VERDICT_STATUSES.has(explicitVerdictStatus)
        ? explicitVerdictStatus
        : fallbackVerdictStatus(outcome, now);
  const cohort = outcomeCohort(outcome);
  const reasonLabel = eligibilityReason(outcome);

  let verdictLabel = "Ждём 1 час";
  let verdictTone = "pending";
  if (verdictStatus === "non_directional") {
    verdictLabel = "Без направления";
    verdictTone = "neutral";
  } else if (verdictStatus === "legacy_excluded") {
    verdictLabel = "Не участвует в live";
    verdictTone = "retrospective";
  } else if (verdictStatus === "missed_window") {
    verdictLabel = "Окно пропущено";
    verdictTone = "missed";
  } else if (verdictStatus === "unavailable") {
    verdictLabel = "Нет истории";
    verdictTone = "unavailable";
  } else if (verdictStatus === "evaluated") {
    verdictLabel = outcome?.verdict ? "Попал" : "Не попал";
    verdictTone = outcome?.verdict ? "hit" : "miss";
  }

  return {
    cohort,
    cohortLabel: cohort === "retrospective" ? "Ретроспектива" : "Live",
    reasonLabel,
    verdictStatus,
    verdictLabel,
    verdictTone,
    stateLabel: outcomeStateLabel(outcome, verdictStatus),
  };
}

function outcomeTimestamp(outcome) {
  const candidates = [
    outcome?.signal_as_of,
    outcome?.as_of,
    outcome?.signal_created_at,
    outcome?.news?.published_at,
  ];
  for (const candidate of candidates) {
    const timestamp = Date.parse(candidate || "");
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return Number.NEGATIVE_INFINITY;
}

export function newestEvalOutcomes(outcomes, limit = 30) {
  return [...(Array.isArray(outcomes) ? outcomes : [])]
    .sort((left, right) => {
      const byTime = outcomeTimestamp(right) - outcomeTimestamp(left);
      if (byTime !== 0) return byTime;
      return String(right?.signal_id || "").localeCompare(String(left?.signal_id || ""));
    })
    .slice(0, limit);
}
