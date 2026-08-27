const STATUS_COPY = {
  signal_available: "есть news-сигнал",
  awaiting_signal: "есть новости, сигнала нет",
  no_relevant_news: "нет релевантных новостей в окне",
};

function safeCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.round(number)) : 0;
}

export function companyCoverageView(coverage) {
  if (!coverage || !Array.isArray(coverage.items)) {
    return {
      available: false,
      basis: null,
      windowNews: 0,
      supported: 0,
      withRelevantNews: 0,
      withSignal: 0,
      items: [],
    };
  }
  const items = coverage.items.map((item) => {
    const status = STATUS_COPY[item.status] ? item.status : "no_relevant_news";
    return {
      ticker: String(item.ticker || "—"),
      status,
      label: STATUS_COPY[status],
      relevantNews: safeCount(item.relevant_news),
      analysisCandidates: safeCount(item.analysis_candidates),
      signaledNews: safeCount(item.signaled_news),
      lastPublishedAt: item.last_published_at || null,
    };
  });
  return {
    available: true,
    basis: coverage.basis || "current_content_snapshot",
    windowNews: safeCount(coverage.window_news),
    supported: safeCount(coverage.supported) || items.length,
    withRelevantNews: safeCount(coverage.with_relevant_news),
    withSignal: safeCount(coverage.with_signal),
    items,
  };
}
