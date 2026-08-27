import assert from "node:assert/strict";
import test from "node:test";

import { resolveSignalEvidence } from "./provenance.js";

test("resolves only immutable evidence refs and never substitutes ticker news", () => {
  const exact = { id: "news_exact", title: "Exact evidence", evidenceIds: ["news_exact"] };
  const unrelated = { id: "news_same_ticker", title: "Same ticker, different event", evidenceIds: ["news_same_ticker"] };
  const signal = {
    ticker: "SBER",
    evidenceRefs: ["news_exact"],
    evidence: [],
  };

  assert.deepEqual(resolveSignalEvidence(signal, [unrelated, exact]), [exact]);
});

test("uses embedded source metadata when referenced news is outside the feed window", () => {
  const embedded = { id: "news_old", source: "Интерфакс", title: "Older evidence" };
  const signal = {
    evidenceRefs: ["news_old"],
    evidence: [embedded],
  };

  assert.deepEqual(resolveSignalEvidence(signal, []), [embedded]);
});

test("does not invent evidence when a ref cannot be resolved", () => {
  const signal = { ticker: "SBER", evidenceRefs: ["news_missing"], evidence: [] };
  const sameTicker = { id: "news_same_ticker", tickers: ["SBER"] };

  assert.deepEqual(resolveSignalEvidence(signal, [sameTicker]), []);
});
