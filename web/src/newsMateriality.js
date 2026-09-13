export function readyMaterialityProbability(materiality) {
  if (materiality?.status !== "ready" || !Array.isArray(materiality.predictions)) return null;
  const probabilities = materiality.predictions
    .filter((prediction) => prediction.status === "ready" && prediction.eligible_for_ranking)
    .map((prediction) => Number(prediction.probability))
    .filter((probability) => Number.isFinite(probability) && probability >= 0 && probability <= 1);
  return probabilities.length ? Math.max(...probabilities) : null;
}

export function compareNewsMateriality(left, right) {
  const leftProbability = left.materialityProbability;
  const rightProbability = right.materialityProbability;
  if (leftProbability === null && rightProbability === null) return 0;
  if (leftProbability === null) return 1;
  if (rightProbability === null) return -1;
  return rightProbability - leftProbability;
}
