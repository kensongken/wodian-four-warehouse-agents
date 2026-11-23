import type { ContentSegment } from "@wearable/contracts";

export type HistoricalMatch = Readonly<{
  segment: ContentSegment;
  score: number;
  reasons: string[];
}>;

const PROCESS_ORDER = [
  "idea",
  "design",
  "material-selection",
  "making",
  "setting",
  "polishing",
  "rework",
  "finished",
] as const;

export function retrieveHistoricalSegments(
  anchor: ContentSegment,
  candidates: readonly ContentSegment[],
  limit: number,
): HistoricalMatch[] {
  if (!Number.isInteger(limit) || limit < 0) {
    throw new Error("invalid-retrieval-limit");
  }

  return candidates
    .filter(
      (candidate) =>
        candidate.permissionState === "allowed" && candidate.id !== anchor.id,
    )
    .map((candidate) => scoreCandidate(anchor, candidate))
    .filter((match) => match.reasons.length > 0)
    .sort((left, right) => {
      if (left.score !== right.score) return right.score - left.score;
      return left.segment.id < right.segment.id
        ? -1
        : left.segment.id > right.segment.id
          ? 1
          : 0;
    })
    .slice(0, limit);
}

function scoreCandidate(
  anchor: ContentSegment,
  candidate: ContentSegment,
): HistoricalMatch {
  const reasons: string[] = [];
  let score = 0;

  const sharedProducts = intersection(
    confirmedProducts(anchor),
    confirmedProducts(candidate),
  );
  if (sharedProducts.length > 0) {
    score += 50;
    reasons.push("same-confirmed-product");

    const relation = processRelation(anchor, candidate);
    if (relation === "before") {
      score += 20;
      reasons.push("same-product-before-stage");
    } else if (relation === "after") {
      score += 16;
      reasons.push("same-product-after-stage");
    }
  }

  if (intersection(anchor.tags.people, candidate.tags.people).length > 0) {
    score += 18;
    reasons.push("same-person");
  }

  if (hasAdjacentProcessStage(anchor, candidate)) {
    score += 15;
    reasons.push("adjacent-process-stage");
  }

  if (intersection(anchor.tags.topics, candidate.tags.topics).length > 0) {
    score += 12;
    reasons.push("shared-topic");
  }

  if (intersection(anchor.tags.emotions, candidate.tags.emotions).length > 0) {
    score += 6;
    reasons.push("shared-emotion");
  }

  score -= Math.min(candidate.tags.usage.useCount * 3, 30);
  if (wasUsedRecently(candidate.tags.usage.lastUsedAt)) score -= 10;

  return { segment: candidate, score, reasons };
}

function confirmedProducts(segment: ContentSegment): string[] {
  return segment.tags.products
    .filter((product) => product.status === "confirmed")
    .map((product) => product.id);
}

function processRelation(
  anchor: ContentSegment,
  candidate: ContentSegment,
): "before" | "after" | null {
  const anchorRanks = ranks(anchor.tags.processStages);
  const candidateRanks = ranks(candidate.tags.processStages);
  if (anchorRanks.length === 0 || candidateRanks.length === 0) return null;
  if (Math.max(...candidateRanks) < Math.min(...anchorRanks)) return "before";
  if (Math.min(...candidateRanks) > Math.max(...anchorRanks)) return "after";
  return null;
}

function hasAdjacentProcessStage(
  anchor: ContentSegment,
  candidate: ContentSegment,
): boolean {
  const anchorRanks = ranks(anchor.tags.processStages);
  const candidateRanks = ranks(candidate.tags.processStages);
  return anchorRanks.some((anchorRank) =>
    candidateRanks.some((candidateRank) => Math.abs(anchorRank - candidateRank) === 1),
  );
}

function ranks(stages: string[]): number[] {
  return stages
    .map((stage) => PROCESS_ORDER.indexOf(stage as (typeof PROCESS_ORDER)[number]))
    .filter((rank) => rank >= 0);
}

function intersection(left: string[], right: string[]): string[] {
  const rightValues = new Set(right);
  return [...new Set(left.filter((value) => rightValues.has(value)))];
}

function wasUsedRecently(lastUsedAt: string | null): boolean {
  if (!lastUsedAt) return false;
  const usedAt = Date.parse(lastUsedAt);
  if (!Number.isFinite(usedAt)) return false;
  const ageMs = Date.now() - usedAt;
  return ageMs >= 0 && ageMs <= 30 * 24 * 60 * 60 * 1000;
}
