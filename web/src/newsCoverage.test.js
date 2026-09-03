import test from "node:test";
import assert from "node:assert/strict";

import {
  CANONICAL_NEWS_LIMIT,
  CANONICAL_NEWS_PATH,
  DASHBOARD_REFRESH_MS,
  dashboardLoadMode,
  dashboardRefreshDue,
  newsCoverageView,
} from "./newsCoverage.js";

test("uses the backend canonical news snapshot limit", () => {
  assert.equal(CANONICAL_NEWS_LIMIT, 500);
  assert.equal(CANONICAL_NEWS_PATH, "/v1/news?limit=500");
});

test("refreshes dashboard data at most every five minutes", () => {
  assert.equal(DASHBOARD_REFRESH_MS, 300000);
  assert.equal(dashboardLoadMode("signals"), "auto");
  assert.equal(dashboardLoadMode("signal"), "auto");
  assert.equal(dashboardLoadMode("news"), "auto");
  assert.equal(dashboardLoadMode("methodology"), "once");
  assert.equal(dashboardLoadMode("evals"), "once");
  assert.equal(dashboardLoadMode("api"), "once");
  assert.equal(dashboardLoadMode("unknown"), "off");

  assert.equal(dashboardRefreshDue({
    mode: "auto",
    visibilityState: "visible",
    lastLoadedAt: 1000,
    now: 1000 + DASHBOARD_REFRESH_MS - 1,
  }), false);
  assert.equal(dashboardRefreshDue({
    mode: "auto",
    visibilityState: "visible",
    lastLoadedAt: 1000,
    now: 1000 + DASHBOARD_REFRESH_MS,
  }), true);
  assert.equal(dashboardRefreshDue({
    mode: "auto",
    visibilityState: "hidden",
    lastLoadedAt: 0,
    now: DASHBOARD_REFRESH_MS,
  }), false);
  assert.equal(dashboardRefreshDue({
    mode: "once",
    visibilityState: "visible",
    lastLoadedAt: 0,
    now: DASHBOARD_REFRESH_MS,
  }), false);
});

test("marks a truncated news response as partial", () => {
  assert.deepEqual(newsCoverageView({ total: 720, has_more: true }, 500), {
    loaded: 500,
    total: 720,
    partial: true,
  });
});

test("treats a complete response as fully loaded", () => {
  assert.deepEqual(newsCoverageView({ total: 349, has_more: false }, 349), {
    loaded: 349,
    total: 349,
    partial: false,
  });
});

test("does not report fewer publications than were loaded", () => {
  assert.deepEqual(newsCoverageView({ total: 90 }, 100), {
    loaded: 100,
    total: 100,
    partial: false,
  });
});
