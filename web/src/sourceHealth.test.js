import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDurationSeconds,
  sourceFreshnessView,
  sourceScheduleLabel,
} from "./sourceHealth.js";

test("formats source freshness without claiming collector uptime", () => {
  const view = sourceFreshnessView({
    freshnessStatus: "delayed",
    freshnessAgeSeconds: 620,
  });

  assert.equal(view.status, "delayed");
  assert.equal(view.label, "Давно без новых публикаций");
  assert.equal(view.detail, "последнее получение 10 мин назад");
  assert.doesNotMatch(`${view.label} ${view.detail}`, /слом|сбой коллектора/i);
});

test("keeps missing observations distinct from a source with no data", () => {
  const unknown = sourceFreshnessView({ freshnessStatus: "unknown" });
  const noData = sourceFreshnessView({ freshnessStatus: "no_data" });

  assert.equal(unknown.label, "Freshness недоступна");
  assert.equal(noData.label, "Нет данных в окне");
  assert.notEqual(unknown.status, noData.status);
});

test("shows collection schedule and the latest measured delivery lag", () => {
  assert.deepEqual(
    sourceScheduleLabel({
      collectionLane: "fast",
      pollIntervalSeconds: 60,
      latestDeliveryLagSeconds: 15,
    }),
    {
      lane: "fast · опрос 1 мин",
      lag: "последний delivery lag 15 сек",
    },
  );
  assert.equal(formatDurationSeconds(90061), "1 д");
});
