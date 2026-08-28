import assert from "node:assert/strict";
import test from "node:test";

import { signalHistoryEntryFromApi, signalHistoryFromApi } from "./signalHistory.js";

test("maps an exact stored transition and factor delta to display points", () => {
  const entry = signalHistoryEntryFromApi({
    signal: {
      id: "sig_new",
      as_of: "2026-08-27T10:00:00Z",
      created_at: "2026-08-27T10:01:00Z",
      direction: "up",
      score: 30.5,
      confidence: 0.74,
      summary: "Новый факт изменил оценку.",
      expires_at: "2026-08-30T10:00:00Z",
      invalidation_conditions: ["Появилась новая существенная информация."],
    },
    change_from_previous: {
      from_direction: "neutral",
      direction_changed: true,
      primary_factor_change: {
        code: "event_impact",
        label: "Влияние события",
        previous_contribution: 0.05,
        current_contribution: 0.31,
        delta: 0.26,
      },
    },
  }, Date.parse("2026-08-28T10:00:00Z"));

  assert.deepEqual(entry.primaryFactor, {
    code: "event_impact",
    label: "Влияние события",
    previousContribution: 5,
    currentContribution: 31,
    delta: 26,
  });
  assert.equal(entry.fromDirection, "neutral");
  assert.equal(entry.createdAt, "2026-08-27T10:01:00Z");
  assert.equal(entry.direction, "up");
  assert.equal(entry.directionChanged, true);
  assert.equal(entry.expired, false);
  assert.equal(entry.confidence, 74);
});

test("keeps the first event and missing factor schema explicit", () => {
  const entries = signalHistoryFromApi({
    data: [{
      signal: {
        id: "sig_first",
        direction: "neutral",
        expires_at: "invalid",
        invalidation_conditions: [],
      },
      change_from_previous: null,
    }],
  });

  assert.equal(entries.length, 1);
  assert.equal(entries[0].fromDirection, null);
  assert.equal(entries[0].primaryFactor, null);
  assert.equal(entries[0].expired, null);
});

test("does not turn null factor contributions into invented zeroes", () => {
  const entry = signalHistoryEntryFromApi({
    signal: { id: "sig_schema_change", expires_at: null },
    change_from_previous: {
      primary_factor_change: {
        code: "new_factor",
        label: "Новый фактор",
        previous_contribution: null,
        current_contribution: 0.2,
        delta: null,
      },
    },
  });

  assert.equal(entry.primaryFactor, null);
});
