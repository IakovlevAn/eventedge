export function separateAssessmentLayers(item) {
  const newsSignal = item?.news_signal || null;
  const marketContext = item?.market_context || {
    as_of: item?.as_of || null,
    is_signal: false,
    bias_direction: item?.bias_direction || "neutral",
    score: 0,
    confidence: 0,
    factor_contributions: [],
  };
  return {
    newsSignal,
    marketContext,
    marketScenario: item?.market_scenario || null,
    legacyCombined: {
      score: item?.score ?? null,
      confidence: item?.confidence ?? null,
      direction: item?.direction || "neutral",
      modelVersion: item?.model_version || null,
      configVersion: item?.config_version ?? null,
    },
  };
}
