import {
  EditRevisionSchema,
  VariantPlanSchema,
  type ContentSegment,
  type EditRevision,
  type StoryCandidate,
  type VariantPlan,
} from "@wearable/contracts";
import { assertDifferentTimelines } from "./variant-timelines";

type PlannedVariant = Omit<VariantPlan, "kind">;

export interface EditPlanAnalyzer {
  plan(input: Readonly<{
    story: StoryCandidate;
    segments: ContentSegment[];
  }>): Promise<
    Readonly<{
      master: PlannedVariant;
      xiaohongshu: PlannedVariant;
      douyin: PlannedVariant;
      wechatChannels: PlannedVariant;
      stills: Array<{ segmentId: string; atMs: number; reason: string }>;
    }>
  >;
}

export async function planInitialRevision(
  story: StoryCandidate,
  segments: ContentSegment[],
  analyzer: EditPlanAnalyzer,
): Promise<EditRevision> {
  const lineageIds = new Set([
    ...story.anchorSegmentIds,
    ...story.historicalSegmentIds,
  ]);
  const resolvedIds = new Set(segments.map((segment) => segment.id));
  for (const segmentId of lineageIds) {
    if (!resolvedIds.has(segmentId)) {
      throw new Error(`story-segment-not-resolved:${segmentId}`);
    }
  }

  const planned = await analyzer.plan({ story, segments });
  const variants = [
    VariantPlanSchema.parse({ kind: "editorial-master", ...planned.master }),
    VariantPlanSchema.parse({ kind: "xiaohongshu", ...planned.xiaohongshu }),
    VariantPlanSchema.parse({ kind: "douyin", ...planned.douyin }),
    VariantPlanSchema.parse({
      kind: "wechat-channels",
      ...planned.wechatChannels,
    }),
  ];

  assertLineage(variants, planned.stills, lineageIds, resolvedIds);
  assertDifferentTimelines(variants);

  return EditRevisionSchema.parse({
    id: `revision_${story.id}_1`,
    storyId: story.id,
    revision: 1,
    parentRevisionId: null,
    designerPrompt: null,
    changeExplanation: null,
    variants,
    selectedStills: planned.stills,
    createdAt: new Date().toISOString(),
  });
}

function assertLineage(
  variants: VariantPlan[],
  stills: Array<{ segmentId: string; atMs: number; reason: string }>,
  lineageIds: Set<string>,
  resolvedIds: Set<string>,
): void {
  const referencedIds = [
    ...variants.flatMap((variant) => [
      variant.cover.segmentId,
      ...variant.timeline.map((item) => item.segmentId),
    ]),
    ...stills.map((still) => still.segmentId),
  ];

  if (
    referencedIds.some(
      (segmentId) =>
        !lineageIds.has(segmentId) || !resolvedIds.has(segmentId),
    )
  ) {
    throw new Error("segment-outside-story-lineage");
  }
}
