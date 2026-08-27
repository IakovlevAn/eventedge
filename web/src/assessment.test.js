import assert from "node:assert/strict";
import test from "node:test";

import { separateAssessmentLayers } from "./assessment.js";

test("keeps news direction, market bias and volatility scenario independent", () => {
  const layers = separateAssessmentLayers({
    direction: "neutral",
    score: 3.2,
    confidence: 0.71,
    model_version: "hybrid-market-0.2.1",
    config_version: 3,
    news_signal: { direction: "up", score: 42.7, confidence: 0.76 },
    market_context: { is_signal: false, bias_direction: "down", score: -8.4 },
    market_scenario: { low_pct: -2.6, high_pct: 2.6 },
  });

  assert.equal(layers.newsSignal.direction, "up");
  assert.equal(layers.newsSignal.score, 42.7);
  assert.equal(layers.marketContext.is_signal, false);
  assert.equal(layers.marketContext.bias_direction, "down");
  assert.deepEqual(layers.marketScenario, { low_pct: -2.6, high_pct: 2.6 });
  assert.equal(layers.legacyCombined.direction, "neutral");
  assert.equal(layers.legacyCombined.score, 3.2);
});

test("market context never manufactures a news signal", () => {
  const layers = separateAssessmentLayers({
    as_of: "2026-08-27T12:00:00Z",
    direction: "neutral",
    bias_direction: "up",
    score: 12,
    news_signal: null,
    market_context: { is_signal: false, bias_direction: "up", score: 22 },
    market_scenario: { low_pct: -3, high_pct: 3 },
  });

  assert.equal(layers.newsSignal, null);
  assert.equal(layers.marketContext.bias_direction, "up");
  assert.equal(layers.marketContext.is_signal, false);
});
