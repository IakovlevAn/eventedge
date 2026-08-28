import test from "node:test";
import assert from "node:assert/strict";

import {
  CANONICAL_NEWS_LIMIT,
  CANONICAL_NEWS_PATH,
  newsCoverageView,
} from "./newsCoverage.js";

test("uses the backend canonical news snapshot limit", () => {
  assert.equal(CANONICAL_NEWS_LIMIT, 500);
  assert.equal(CANONICAL_NEWS_PATH, "/v1/news?limit=500");
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
