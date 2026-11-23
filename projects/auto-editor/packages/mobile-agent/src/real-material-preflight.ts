import { createHash } from "node:crypto";

import {
  ModelAnalysisReceiptV1Schema,
  RealMaterialCapabilityReportSchema,
  RealMaterialStoryReadinessSchema,
  type ContentSegment,
  type ModelAnalysisReceiptV1,
  type RealMaterialCapabilityLayer,
  type RealMaterialCapabilityReport,
  type RealMaterialStoryErrorCode,
  type RealMaterialStoryReadiness,
  type StudioAnalysisLayer,
  type StoryType,
} from "@wearable/contracts";

import {
  inferStoryEvidenceForSegments,
} from "./story-decomposer";

const STORY_REQUIREMENTS: Record<StoryType, readonly string[]> = {
  "product-journey": ["confirmed-product", "process-change"],
  "inner-journey": ["emotion-change", "first-person-choice"],
  "persona-monologue": ["clear-opinion", "supporting-evidence"],
};

const INSUFFICIENT_CODES: Record<StoryType, RealMaterialStoryErrorCode> = {
  "product-journey": "insufficient-process-evidence",
  "inner-journey": "insufficient-inner-journey-evidence",
  "persona-monologue": "insufficient-persona-evidence",
};

const MODEL_LAYERS = ["transcript", "vision", "embedding"] as const;

export function buildRealMaterialCapabilityReport(input: Readonly<{
  mediaToolsReady: boolean;
  receipts: readonly ModelAnalysisReceiptV1[];
  artifacts: readonly Readonly<{ id: string; status: string }>[];
}>): RealMaterialCapabilityReport {
  const receipts = input.receipts.flatMap((receipt) => {
    const parsed = ModelAnalysisReceiptV1Schema.safeParse(receipt);
    return parsed.success ? [parsed.data] : [];
  });
  const layers: RealMaterialCapabilityReport["layers"] = [
    {
      layer: "media",
      status: input.mediaToolsReady ? "ready" : "blocked",
      evidenceCodes: [input.mediaToolsReady ? "ffmpeg-ffprobe-ready" : "media-tools-unavailable"],
      message: input.mediaToolsReady
        ? "FFmpeg and ffprobe are available for real local media."
        : "FFmpeg or ffprobe is unavailable.",
    },
    capabilityForAnalysisLayer("scene", receipts, input.artifacts),
    ...MODEL_LAYERS.map((layer) => capabilityForAnalysisLayer(layer, receipts, input.artifacts)),
  ];
  const generatedAt = receipts.map((receipt) => receipt.completedAt).sort().at(-1)
    ?? "1970-01-01T00:00:00.000Z";
  const identity = createHash("sha256").update(JSON.stringify({
    mediaToolsReady: input.mediaToolsReady,
    layers: layers.map(({ layer, status, evidenceCodes }) => ({ layer, status, evidenceCodes })),
  })).digest("hex");
  return RealMaterialCapabilityReportSchema.parse({
    schemaVersion: 1,
    runId: `capability_${identity}`,
    generatedAt,
    layers,
  });
}

export function evaluateRealMaterialStories(input: Readonly<{
  requestedStoryTypes: readonly StoryType[];
  segments: readonly ContentSegment[];
  capability: RealMaterialCapabilityReport;
}>): readonly RealMaterialStoryReadiness[] {
  const segments = input.segments.filter((segment) => segment.permissionState === "allowed");
  const capability = RealMaterialCapabilityReportSchema.parse(input.capability);
  return input.requestedStoryTypes.map((storyType) => {
    const evidence = inferStoryEvidenceForSegments(storyType, segments);
    const missingEvidence = STORY_REQUIREMENTS[storyType].filter(
      (required) => !evidence.reasons.includes(required),
    );
    const sourceIds = [...new Set(segments.map((segment) => segment.id))];
    if (evidence.eligible) {
      return RealMaterialStoryReadinessSchema.parse({
        storyType,
        status: "ready",
        evidence: evidence.reasons,
        missingEvidence,
        sourceIds,
        errorCode: null,
        message: "The authorized material contains the required evidence for this story.",
      });
    }

    const blocked = blockingCapability(storyType, segments, capability);
    if (blocked) {
      return RealMaterialStoryReadinessSchema.parse({
        storyType,
        status: "blocked-by-capability",
        evidence: evidence.reasons,
        missingEvidence,
        sourceIds,
        errorCode: blocked,
        message: `The source could carry this evidence, but ${blocked} blocks analysis.`,
      });
    }

    return RealMaterialStoryReadinessSchema.parse({
      storyType,
      status: "insufficient-evidence",
      evidence: evidence.reasons,
      missingEvidence,
      sourceIds,
      errorCode: INSUFFICIENT_CODES[storyType],
      message: "The authorized material does not contain enough confirmed evidence for this story.",
    });
  });
}

function capabilityForAnalysisLayer(
  layer: Exclude<StudioAnalysisLayer, "technical">,
  receipts: readonly ModelAnalysisReceiptV1[],
  artifacts: readonly Readonly<{ id: string; status: string }>[],
): RealMaterialCapabilityReport["layers"][number] {
  const candidates = receipts.filter((receipt) => receipt.layer === layer);
  const evidenceCodes = new Set<string>();
  if (candidates.length === 0) evidenceCodes.add("receipt-missing");
  for (const receipt of candidates) {
    if (receipt.status !== "succeeded") {
      evidenceCodes.add("failed-receipt");
      continue;
    }
    if (receipt.synthetic) {
      evidenceCodes.add("synthetic-receipt-rejected");
      continue;
    }
    if (receipt.providerMode !== "local") {
      evidenceCodes.add("non-local-receipt-rejected");
      continue;
    }
    if (layer !== "scene") {
      if (receipt.checkpointDigest === null) {
        evidenceCodes.add("checkpoint-digest-missing");
        continue;
      }
      if (!hasReadyBoundArtifact(receipt, artifacts)) {
        evidenceCodes.add("model-artifact-not-ready");
        continue;
      }
    }
    evidenceCodes.add("real-local-receipt");
    return {
      layer: layer as RealMaterialCapabilityLayer,
      status: "ready",
      evidenceCodes: [...evidenceCodes],
      message: `${layer} has a succeeded non-synthetic local receipt.`,
    };
  }
  return {
    layer: layer as RealMaterialCapabilityLayer,
    status: layer === "scene" ? "degraded" : "blocked",
    evidenceCodes: [...evidenceCodes],
    message: `${layer} has no acceptable real local receipt.`,
  };
}

function hasReadyBoundArtifact(
  receipt: ModelAnalysisReceiptV1,
  artifacts: readonly Readonly<{ id: string; status: string }>[],
): boolean {
  const acceptedIds = new Set([
    receipt.modelName,
    `${receipt.modelName}-${receipt.modelVersion}`,
  ]);
  return artifacts.some((artifact) => artifact.status === "ready" && acceptedIds.has(artifact.id));
}

function blockingCapability(
  storyType: StoryType,
  segments: readonly ContentSegment[],
  capability: RealMaterialCapabilityReport,
): RealMaterialStoryErrorCode | null {
  if (layerStatus(capability, "media") === "blocked" && segments.length > 0) {
    return "media-capability-unavailable";
  }
  const couldContainSpeechEvidence = segments.some((segment) =>
    segment.tags.technicalFlags.some((flag) => /(?:has[-_: ]?audio|audio[:=]?(?:true|present|aac))/i.test(flag)),
  );
  if (
    (storyType === "inner-journey" || storyType === "persona-monologue")
    && couldContainSpeechEvidence
    && layerStatus(capability, "transcript") === "blocked"
  ) {
    return "transcript-capability-unavailable";
  }
  return null;
}

function layerStatus(
  capability: RealMaterialCapabilityReport,
  layer: RealMaterialCapabilityLayer,
): "ready" | "degraded" | "blocked" {
  return capability.layers.find((candidate) => candidate.layer === layer)?.status ?? "blocked";
}
