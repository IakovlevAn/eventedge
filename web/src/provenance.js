export function resolveSignalEvidence(signal, groupedNews) {
  const refs = signal?.evidenceRefs || signal?.evidence_refs || [];
  const embedded = signal?.evidence || [];
  const groupedByEvidenceId = new Map();
  groupedNews.forEach((item) => {
    const ids = item.evidenceIds?.length ? item.evidenceIds : [item.id];
    ids.forEach((id) => groupedByEvidenceId.set(id, item));
  });
  const embeddedById = new Map(embedded.map((item) => [item.id, item]));
  const requestedIds = refs.length ? refs : embedded.map((item) => item.id);
  return requestedIds
    .map((id) => groupedByEvidenceId.get(id) || embeddedById.get(id))
    .filter((item, index, values) => (
      item && values.findIndex((candidate) => candidate?.id === item.id) === index
    ));
}
