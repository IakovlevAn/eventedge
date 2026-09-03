export const CANONICAL_NEWS_LIMIT = 500;
export const CANONICAL_NEWS_PATH = `/v1/news?limit=${CANONICAL_NEWS_LIMIT}`;
export const DASHBOARD_REFRESH_MS = 5 * 60 * 1000;

const AUTO_REFRESH_VIEWS = new Set(["signals", "signal", "news"]);

export function dashboardLoadMode(view) {
  if (AUTO_REFRESH_VIEWS.has(view)) return "auto";
  // Non-market screens still need one snapshot for the global company search,
  // but they must not keep the heavy dashboard polling loop alive.
  if (["methodology", "evals", "api"].includes(view)) return "once";
  return "off";
}

export function dashboardRefreshDue({
  mode,
  visibilityState,
  lastLoadedAt,
  now,
}) {
  return mode === "auto"
    && visibilityState === "visible"
    && (!lastLoadedAt || now - lastLoadedAt >= DASHBOARD_REFRESH_MS);
}

export function newsCoverageView(meta, loadedPublicationCount) {
  const loaded = Math.max(0, Number(loadedPublicationCount) || 0);
  const reportedTotal = Number(meta?.total);
  const total = Number.isFinite(reportedTotal)
    ? Math.max(loaded, reportedTotal)
    : loaded;

  return {
    loaded,
    total,
    partial: Boolean(meta?.has_more) || total > loaded,
  };
}
