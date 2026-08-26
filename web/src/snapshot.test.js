import test from "node:test";
import assert from "node:assert/strict";

import { shouldAcceptSnapshot, snapshotTimestamp } from "./snapshot.js";

test("accepts the first and genuinely newer assessment snapshots", () => {
  assert.equal(shouldAcceptSnapshot(null, {
    snapshot_id: "market_first",
    snapshot_as_of: "2026-08-26T18:34:05Z",
  }), true);
  assert.equal(shouldAcceptSnapshot({
    snapshot_id: "market_first",
    snapshot_as_of: "2026-08-26T18:34:05Z",
  }, {
    snapshot_id: "market_second",
    snapshot_as_of: "2026-08-26T18:34:44Z",
  }), true);
});

test("rejects older or competing snapshots from another instance", () => {
  const current = {
    snapshot_id: "market_current",
    snapshot_as_of: "2026-08-26T18:34:44Z",
  };
  assert.equal(shouldAcceptSnapshot(current, {
    snapshot_id: "market_old",
    snapshot_as_of: "2026-08-26T18:34:05Z",
  }), false);
  assert.equal(shouldAcceptSnapshot(current, {
    snapshot_id: "market_competing",
    snapshot_as_of: "2026-08-26T18:34:44Z",
  }), false);
});

test("keeps backward compatibility when snapshot metadata is absent", () => {
  assert.equal(shouldAcceptSnapshot({ snapshot_id: "market_current" }, {}), true);
  assert.equal(snapshotTimestamp({ snapshot_as_of: "invalid" }), null);
});
