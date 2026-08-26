export function snapshotTimestamp(meta) {
  const value = meta?.snapshot_as_of || meta?.generated_at;
  const timestamp = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function shouldAcceptSnapshot(currentMeta, nextMeta) {
  if (!nextMeta?.snapshot_id || !currentMeta?.snapshot_id) return true;
  if (nextMeta.snapshot_id === currentMeta.snapshot_id) return true;

  const currentTimestamp = snapshotTimestamp(currentMeta);
  const nextTimestamp = snapshotTimestamp(nextMeta);
  if (currentTimestamp === null || nextTimestamp === null) return true;

  // Different payloads with the same market timestamp are competing server
  // snapshots, not a newer market observation. Keep the first complete one.
  return nextTimestamp > currentTimestamp;
}
