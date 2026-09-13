import test from "node:test";
import assert from "node:assert/strict";

import {
  compareNewsMateriality,
  readyMaterialityProbability,
} from "./newsMateriality.js";

test("uses only ready predictions admitted for product ranking", () => {
  const materiality = {
    status: "ready",
    predictions: [
      { status: "ready", eligible_for_ranking: true, probability: 0.72 },
      { status: "ready", eligible_for_ranking: false, probability: 0.98 },
      { status: "deferred", eligible_for_ranking: true, probability: null },
    ],
  };

  assert.equal(readyMaterialityProbability(materiality), 0.72);
  assert.equal(readyMaterialityProbability({ status: "pending", predictions: [] }), null);
});

test("sorts scored news first without inventing scores for pending rows", () => {
  const high = { materialityProbability: 0.8 };
  const low = { materialityProbability: 0.3 };
  const pending = { materialityProbability: null };

  assert.ok(compareNewsMateriality(high, low) < 0);
  assert.ok(compareNewsMateriality(low, pending) < 0);
  assert.equal(compareNewsMateriality(pending, pending), 0);
});
