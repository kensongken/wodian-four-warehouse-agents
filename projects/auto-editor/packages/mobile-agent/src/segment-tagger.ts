import { randomUUID } from "node:crypto";

import {
  SegmentTagsSchema,
  type ContentSegment,
  type SegmentTags,
  type UntypedSegment,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";

export interface SegmentTagAnalyzer {
  suggest(segment: UntypedSegment): Promise<SegmentTags>;
}

export type DesignerTagPatch = Partial<
  Pick<
    SegmentTags,
    "people" | "products" | "processStages" | "topics" | "emotions"
  >
>;

export async function tagSegments(
  segments: readonly UntypedSegment[],
  analyzer: SegmentTagAnalyzer,
  library: ContentLibrary,
): Promise<ContentSegment[]> {
  const tagged: ContentSegment[] = [];

  for (const segment of segments) {
    const suggestion = SegmentTagsSchema.parse(
      await analyzer.suggest(segment),
    );
    tagged.push({
      id: segment.id,
      assetId: segment.assetId,
      batchId: segment.batchId,
      startMs: segment.startMs,
      endMs: segment.endMs,
      permissionState: segment.permissionState,
      tags: {
        ...suggestion,
        products: suggestion.products.map((product) => ({
          ...product,
          status: "suggested" as const,
        })),
        technicalFlags: unique([
          ...segment.technicalFlags,
          ...suggestion.technicalFlags,
        ]),
      },
    });
  }

  await library.putSegments(tagged);
  return tagged;
}

export async function confirmSegmentTags(
  segmentId: string,
  patch: DesignerTagPatch,
  designerId: string,
  library: ContentLibrary,
): Promise<ContentSegment> {
  const existing = (await library.listSegments()).find(
    (segment) => segment.id === segmentId,
  );
  if (!existing) throw new Error(`segment-not-found:${segmentId}`);

  const before = existing.tags;
  const after = SegmentTagsSchema.parse({
    ...before,
    ...(patch.people === undefined ? {} : { people: patch.people }),
    ...(patch.products === undefined ? {} : { products: patch.products }),
    ...(patch.processStages === undefined
      ? {}
      : { processStages: patch.processStages }),
    ...(patch.topics === undefined ? {} : { topics: patch.topics }),
    ...(patch.emotions === undefined ? {} : { emotions: patch.emotions }),
  });
  const corrected: ContentSegment = { ...existing, tags: after };

  await library.putSegments([corrected]);
  await library.putAuditEvent({
    id: `audit_${randomUUID()}`,
    entityType: "segment",
    entityId: segmentId,
    actorId: designerId,
    action: "segment-tags-confirmed",
    reason: null,
    before,
    after,
    createdAt: new Date().toISOString(),
  });
  return corrected;
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
