import { createHash } from "node:crypto";
import {
  StoryTypeSchema,
  type ContentSegment,
  type HistoricalMaterialConsent,
  type StoryCandidate,
  type StoryType,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";
import { retrieveHistoricalSegments } from "./historical-retriever";

export type StoryProposal = Readonly<{
  type: StoryType;
  anchorSegmentIds: string[];
}>;

export interface StoryAnalyzer {
  propose(
    newSegments: readonly ContentSegment[],
  ): Promise<readonly StoryProposal[]>;
}

export type StoryEvidenceResult = Readonly<{
  eligible: boolean;
  reasons: string[];
}>;

const REQUIRED_EVIDENCE: Record<StoryType, readonly string[]> = {
  "inner-journey": ["emotion-change", "first-person-choice"],
  "product-journey": ["confirmed-product", "process-change"],
  "persona-monologue": ["clear-opinion", "supporting-evidence"],
};

export type StoryDecompositionInput = Readonly<{
    batchId: string;
    authorization: HistoricalMaterialConsent;
    newSegments: ContentSegment[];
    historicalSegments: ContentSegment[];
    historicalCandidateDigest?: string;
}>;

export async function decomposeStories(
  input: StoryDecompositionInput,
  analyzer: StoryAnalyzer,
  library: ContentLibrary,
): Promise<StoryCandidate[]> {
  const stories = await prepareStoryCandidates(input, analyzer, library);
  await library.commitStoryCandidates({ batchId: input.batchId, authorization: input.authorization, stories });
  return stories;
}

export async function prepareStoryCandidates(
  input: StoryDecompositionInput,
  analyzer: StoryAnalyzer,
  library: ContentLibrary,
): Promise<StoryCandidate[]> {
  await library.validateStoryAuthorization(input);
  const currentSegments = input.newSegments.filter(
    (segment) => segment.batchId === input.batchId,
  );
  const currentById = new Map(
    currentSegments.map((segment) => [segment.id, segment]),
  );
  const proposals = await analyzer.propose(currentSegments);
  const { historicalSegments } = await library.validateStoryAuthorization(input);
  const requestedHistoricalIds = new Set(input.historicalSegments.map((segment) => segment.id));
  const eligibleHistoricalSegments = input.historicalCandidateDigest
    ? frozenHistoricalCandidates(input, historicalSegments)
    : historicalSegments.filter((segment) => requestedHistoricalIds.has(segment.id));
  const stories: StoryCandidate[] = [];

  for (const [proposalIndex, proposal] of proposals.entries()) {
    const type = StoryTypeSchema.parse(proposal.type);
    const anchorIds = [...new Set(proposal.anchorSegmentIds)];
    if (
      anchorIds.length === 0 ||
      anchorIds.some((segmentId) => !currentById.has(segmentId))
    ) {
      throw new Error("new-batch-anchor-required");
    }

    const anchors = anchorIds.map((segmentId) => currentById.get(segmentId)!);
    const evidence = inferStoryEvidenceForSegments(type, anchors);
    if (!evidence.eligible) continue;

    const historicalReasons = new Map<string, Set<string>>();
    for (const anchor of anchors) {
      for (const match of retrieveHistoricalSegments(
        anchor,
        eligibleHistoricalSegments,
        3,
      )) {
        const reasons = historicalReasons.get(match.segment.id) ?? new Set();
        match.reasons.forEach((reason) => reasons.add(reason));
        historicalReasons.set(match.segment.id, reasons);
      }
    }

    const historicalSegmentIds = [...historicalReasons.keys()].sort();
    stories.push({
      id: `story_${input.batchId}_${proposalIndex + 1}`,
      batchId: input.batchId,
      type,
      anchorSegmentIds: anchorIds,
      historicalSegmentIds,
      historicalReasons: Object.fromEntries(
        historicalSegmentIds.map((segmentId) => [
          segmentId,
          [...historicalReasons.get(segmentId)!].join(","),
        ]),
      ),
      status: "candidate",
    });
  }

  return stories;
}

function stableHistoricalEvidence(segment: ContentSegment) {
  const { usage: _mutableUsage, ...tags } = segment.tags;
  return { ...segment, tags };
}

function frozenHistoricalCandidates(
  input: StoryDecompositionInput,
  currentSegments: readonly ContentSegment[],
): ContentSegment[] {
  const ordered = [...input.historicalSegments].sort((left, right) => left.id.localeCompare(right.id));
  if (JSON.stringify(ordered.map((segment) => segment.id)) !==
    JSON.stringify(currentSegments.map((segment) => segment.id).sort())) {
    throw new Error("creative-editorial-historical-candidates-missing");
  }
  const digest = createHash("sha256").update(JSON.stringify(ordered)).digest("hex");
  if (digest !== input.historicalCandidateDigest) {
    throw new Error("creative-editorial-historical-checkpoint-invalid");
  }
  const currentById = new Map(currentSegments.map((segment) => [segment.id, segment]));
  return ordered.map((segment) => {
    const current = currentById.get(segment.id);
    if (!current || JSON.stringify(stableHistoricalEvidence(current)) !== JSON.stringify(stableHistoricalEvidence(segment))) {
      throw new Error("creative-editorial-historical-evidence-changed");
    }
    return segment;
  });
}

export function validateStoryEvidence(
  type: StoryType,
  evidence: string[],
): StoryEvidenceResult {
  const checkedType = StoryTypeSchema.parse(type);
  const uniqueEvidence = [...new Set(evidence)];
  return {
    eligible: REQUIRED_EVIDENCE[checkedType].every((required) =>
      uniqueEvidence.includes(required),
    ),
    reasons: uniqueEvidence,
  };
}

export function inferStoryEvidenceForSegments(
  type: StoryType,
  segments: readonly ContentSegment[],
): StoryEvidenceResult {
  const evidence: string[] = [];
  const transcripts = segments.map((segment) => segment.tags.transcript).join(" ");
  const emotions = uniqueFlat(segments.map((segment) => segment.tags.emotions));
  const processStages = uniqueFlat(
    segments.map((segment) => segment.tags.processStages),
  );
  const hasConfirmedProduct = segments.some((segment) =>
    segment.tags.products.some((product) => product.status === "confirmed"),
  );
  const baseTypes = uniqueFlat(segments.map((segment) => segment.tags.baseTypes));

  if (type === "inner-journey") {
    if (emotions.length >= 2) evidence.push("emotion-change");
    if (
      /(?:我|\bI\b)/i.test(transcripts) &&
      /(?:决定|选择|愿意|坚持|放弃|再做|choose|decid)/i.test(transcripts)
    ) {
      evidence.push("first-person-choice");
    }
  }

  if (type === "product-journey") {
    if (hasConfirmedProduct) evidence.push("confirmed-product");
    if (hasConfirmedProductProcessChange(segments)) evidence.push("process-change");
  }

  if (type === "persona-monologue") {
    if (
      baseTypes.includes("persona-copy") &&
      /(?:我认为|我觉得|在我看来|应该|不应该|I believe|I think)/i.test(transcripts)
    ) {
      evidence.push("clear-opinion");
    }
    if (
      hasConfirmedProduct ||
      processStages.length > 0 ||
      baseTypes.includes("handmade") ||
      baseTypes.includes("product-image")
    ) {
      evidence.push("supporting-evidence");
    }
  }

  return validateStoryEvidence(type, evidence);
}

function hasConfirmedProductProcessChange(segments: readonly ContentSegment[]): boolean {
  const stagesByProduct = new Map<string, Set<string>>();
  for (const segment of segments) {
    for (const product of segment.tags.products) {
      if (product.status !== "confirmed") continue;
      const stages = stagesByProduct.get(product.id) ?? new Set<string>();
      segment.tags.processStages.forEach((stage) => stages.add(stage));
      stagesByProduct.set(product.id, stages);
    }
  }
  return [...stagesByProduct.values()].some((stages) => stages.size >= 2);
}

function uniqueFlat<T>(values: T[][]): T[] {
  return [...new Set(values.flat())];
}
