export function formatDurationSeconds(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  const seconds = Math.max(0, Math.round(Number(value)));
  if (seconds < 60) return `${seconds} сек`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч`;
  return `${Math.floor(seconds / 86400)} д`;
}

export function sourceFreshnessView(source) {
  const status = source?.freshnessStatus || "unknown";
  const age = formatDurationSeconds(source?.freshnessAgeSeconds);
  const views = {
    fresh: {
      label: "Свежие данные",
      detail: age ? `получено ${age} назад` : "есть недавняя публикация",
    },
    delayed: {
      label: "Давно без новых публикаций",
      detail: age ? `последнее получение ${age} назад` : "контрольное окно превышено",
    },
    no_data: {
      label: "Нет данных в окне",
      detail: "это не доказывает сбой коллектора",
    },
    unknown: {
      label: "Freshness недоступна",
      detail: "read model не ответил",
    },
    not_applicable: {
      label: "Проверяется через market API",
      detail: "не новостной источник",
    },
    disabled: {
      label: "Отключён",
      detail: "опрос не выполняется",
    },
  };
  return { status, ...(views[status] || views.unknown) };
}

export function sourceScheduleLabel(source) {
  const lane = source?.collectionLane || "unassigned";
  const poll = formatDurationSeconds(source?.pollIntervalSeconds);
  const lag = formatDurationSeconds(source?.latestDeliveryLagSeconds);
  return {
    lane: poll ? `${lane} · опрос ${poll}` : lane,
    lag: lag ? `последний delivery lag ${lag}` : "delivery lag недоступен",
  };
}
