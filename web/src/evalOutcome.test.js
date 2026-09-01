import assert from "node:assert/strict";
import test from "node:test";

import { evalOutcomeView, newestEvalOutcomes } from "./evalOutcome.js";

test("legacy excluded outcome is retrospective and never shown as pending", () => {
  const view = evalOutcomeView({
    status: "excluded",
    verdict_status: "unavailable",
    verdict: null,
    eligibility: {
      eligible: false,
      reason: "retrospective_signal",
      processing_lag_seconds: 1045,
    },
  });

  assert.equal(view.cohort, "retrospective");
  assert.equal(view.cohortLabel, "Ретроспектива");
  assert.equal(view.verdictStatus, "legacy_excluded");
  assert.equal(view.verdictLabel, "Не участвует в live");
  assert.equal(view.reasonLabel, "Рассчитан позже live-окна на 17 мин");
});

test("uses explicit cohort and verdict status from the current API contract", () => {
  const view = evalOutcomeView({
    status: "evaluated",
    verdict_status: "evaluated",
    verdict: true,
    returns: { "4h": 1.2 },
    eligibility: {
      cohort: "retrospective",
      eligible: true,
      reason: "retrospective_signal",
      processing_lag_seconds: 8100,
    },
  });

  assert.equal(view.cohort, "retrospective");
  assert.equal(view.verdictStatus, "evaluated");
  assert.equal(view.verdictLabel, "Попал");
  assert.equal(view.stateLabel, "Оценён · 4 часа");
  assert.equal(view.reasonLabel, "Рассчитан позже live-окна на 2 ч 15 мин");
});

test("neutral outcomes never inherit a legacy hit or miss verdict", () => {
  for (const { verdict, status } of [
    { verdict: true, status: "evaluated" },
    { verdict: false, status: "partial" },
    { verdict: true, status: "excluded" },
  ]) {
    const view = evalOutcomeView({
      direction: "neutral",
      status,
      verdict_status: "evaluated",
      verdict,
      returns: { "4h": verdict ? 0.2 : -0.8 },
      eligibility: { cohort: "live", eligible: true },
    });

    assert.equal(view.verdictStatus, "non_directional");
    assert.equal(view.verdictLabel, "Без направления");
    assert.equal(view.verdictTone, "neutral");
    assert.equal(view.stateLabel, "Не участвует в hit rate");
    assert.equal(view.cohortLabel, "Live");
  }
});

test("explicit not-applicable status is respected during a rolling deploy", () => {
  const view = evalOutcomeView({
    direction: "up",
    status: "evaluated",
    verdict_status: "not_applicable",
    verdict: true,
    eligibility: { cohort: "retrospective", eligible: false },
  });

  assert.equal(view.verdictStatus, "non_directional");
  assert.equal(view.verdictLabel, "Без направления");
  assert.equal(view.stateLabel, "Не участвует в hit rate");
  assert.equal(view.cohortLabel, "Ретроспектива");
});

test("labels a decided partial outcome separately from a complete evaluation", () => {
  const view = evalOutcomeView({
    status: "partial",
    verdict_status: "evaluated",
    verdict: false,
    returns: { "1h": -0.4, "4h": null, "1d": null, "3d": null },
    eligibility: { cohort: "live", eligible: true },
  });

  assert.equal(view.verdictLabel, "Не попал");
  assert.equal(view.verdictTone, "miss");
  assert.equal(view.stateLabel, "Частичный outcome · 1 час");
});

test("shows a missed primary window instead of waiting forever", () => {
  const explicit = evalOutcomeView({
    status: "partial",
    verdict_status: "missed_window",
    verdict: null,
    returns: { "1h": null, "4h": null, "1d": 0.34 },
    eligibility: { cohort: "live", eligible: true },
  });
  const legacyFallback = evalOutcomeView({
    status: "partial",
    verdict: null,
    horizon_observations: {
      "1h": { target_at: "2026-08-29T16:40:00Z", observed_at: "2026-08-30T06:50:00Z", timely: false },
      "4h": { target_at: "2026-08-29T19:40:00Z", observed_at: "2026-08-30T06:50:00Z", timely: false },
    },
  }, Date.parse("2026-09-01T09:00:00Z"));

  for (const view of [explicit, legacyFallback]) {
    assert.equal(view.verdictStatus, "missed_window");
    assert.equal(view.verdictLabel, "Окно пропущено");
    assert.equal(view.stateLabel, "Нет валидного outcome за 1–4 часа");
  }
});

test("an old complete outcome without a short verdict cannot remain pending", () => {
  const view = evalOutcomeView({
    status: "evaluated",
    verdict: null,
    returns: { "1h": null, "4h": null, "3d": 1.3 },
    eligibility: { cohort: "live", eligible: true },
  });

  assert.equal(view.verdictStatus, "missed_window");
  assert.equal(view.verdictLabel, "Окно пропущено");
});

test("keeps a genuinely open first-hour window pending", () => {
  const view = evalOutcomeView({
    status: "partial",
    verdict: null,
    horizon_observations: {
      "1h": { target_at: "2026-09-01T10:00:00Z", observed_at: null, timely: false },
    },
    eligibility: { cohort: "live", eligible: true },
  }, Date.parse("2026-09-01T09:30:00Z"));

  assert.equal(view.cohortLabel, "Live");
  assert.equal(view.verdictStatus, "pending");
  assert.equal(view.verdictLabel, "Ждём 1 час");
});

test("keeps unavailable market history explicit", () => {
  const view = evalOutcomeView({
    status: "unavailable",
    verdict_status: "unavailable",
    verdict: null,
    eligibility: { cohort: "retrospective", eligible: true },
  });

  assert.equal(view.cohortLabel, "Ретроспектива");
  assert.equal(view.verdictLabel, "Нет истории");
  assert.equal(view.stateLabel, "Нет точки входа или рыночной истории");
});

test("sorts newest outcomes first without mutating the API payload", () => {
  const outcomes = [
    { signal_id: "old", as_of: "2026-08-28T08:00:00Z" },
    { signal_id: "new", as_of: "2026-08-31T08:00:00Z" },
    { signal_id: "middle", signal_as_of: "2026-08-30T08:00:00Z" },
  ];

  const visible = newestEvalOutcomes(outcomes, 2);

  assert.deepEqual(visible.map((item) => item.signal_id), ["new", "middle"]);
  assert.deepEqual(outcomes.map((item) => item.signal_id), ["old", "new", "middle"]);
});
