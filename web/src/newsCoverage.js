export const CANONICAL_NEWS_LIMIT = 500;
export const CANONICAL_NEWS_PATH = `/v1/news?limit=${CANONICAL_NEWS_LIMIT}`;

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
