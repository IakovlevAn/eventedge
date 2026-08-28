export function signalHistoryEntryFromApi(item, now = Date.now()) {
  const signal = item?.signal || {};
  const change = item?.change_from_previous || null;
  const factor = change?.primary_factor_change || null;
  const rawFactorValues = factor
    ? [factor.previous_contribution, factor.current_contribution, factor.delta]
    : [];
  const factorValues = factor
    ? rawFactorValues.map(Number)
    : [];
  const comparableFactor = factor
    && rawFactorValues.every((value) => value !== null && value !== "")
    && factorValues.every(Number.isFinite);
  const expiresAtMs = Date.parse(signal.expires_at);
  return {
    id: signal.id,
    asOf: signal.as_of,
    createdAt: signal.created_at,
    direction: signal.direction || "neutral",
    score: Number(signal.score || 0),
    confidence: Math.round(Number(signal.confidence || 0) * 100),
    summary: signal.summary || "",
    fromDirection: change?.from_direction || null,
    directionChanged: Boolean(change?.direction_changed),
    primaryFactor: comparableFactor
      ? {
          code: factor.code,
          label: factor.label,
          previousContribution: factorValues[0] * 100,
          currentContribution: factorValues[1] * 100,
          delta: factorValues[2] * 100,
        }
      : null,
    expiresAt: signal.expires_at || null,
    expired: Number.isFinite(expiresAtMs) ? expiresAtMs <= now : null,
    invalidationConditions: Array.isArray(signal.invalidation_conditions)
      ? signal.invalidation_conditions
      : [],
  };
}

export function signalHistoryFromApi(payload, now = Date.now()) {
  return (payload?.data || []).map((item) => signalHistoryEntryFromApi(item, now));
}
