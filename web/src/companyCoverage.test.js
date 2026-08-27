import assert from "node:assert/strict";
import test from "node:test";

import { companyCoverageView } from "./companyCoverage.js";

test("company coverage keeps absence of news separate from an analyzed item without signal", () => {
  const view = companyCoverageView({
    supported: 3,
    with_relevant_news: 2,
    with_signal: 1,
    items: [
      { ticker: "SBER", relevant_news: 2, analysis_candidates: 2, signaled_news: 1, status: "signal_available" },
      { ticker: "LKOH", relevant_news: 1, analysis_candidates: 1, signaled_news: 0, status: "awaiting_signal" },
      { ticker: "MGNT", relevant_news: 0, analysis_candidates: 0, signaled_news: 0, status: "no_relevant_news" },
    ],
  });

  assert.equal(view.available, true);
  assert.equal(view.withSignal, 1);
  assert.equal(view.items[1].label, "есть новости, сигнала нет");
  assert.equal(view.items[2].label, "нет релевантных новостей в окне");
});

test("missing company coverage remains explicitly unavailable", () => {
  assert.deepEqual(companyCoverageView(null), {
    available: false,
    basis: null,
    windowNews: 0,
    supported: 0,
    withRelevantNews: 0,
    withSignal: 0,
    items: [],
  });
});
