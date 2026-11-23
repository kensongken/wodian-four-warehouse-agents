import { randomUUID } from "node:crypto";

import {
  EditRevisionSchema,
  TimelineItemSchema,
  VideoVariantSchema,
  type ContentSegment,
  type EditRevision,
  type TimelineItem,
  type VariantArtifact,
  type VideoVariant,
} from "@wearable/contracts";
import { z } from "zod";

import type { ContentLibrary } from "./content-library";
import { assertDifferentTimelines } from "./variant-timelines";

export const PromptEditSchema = z.object({
  targetVariants: z.array(VideoVariantSchema).min(1),
  replacementTimelines: z
    .object({
      "editorial-master": z.array(TimelineItemSchema).optional(),
      xiaohongshu: z.array(TimelineItemSchema).optional(),
      douyin: z.array(TimelineItemSchema).optional(),
      "wechat-channels": z.array(TimelineItemSchema).optional(),
    })
    .strict(),
  removePeople: z.array(z.string().min(1)),
  explanation: z.string().min(1),
});

export type PromptEdit = z.infer<typeof PromptEditSchema>;

export interface PromptEditInterpreter {
  interpret(input: Readonly<{
    prompt: string;
    revision: EditRevision;
    allowedSegments: ContentSegment[];
  }>): Promise<PromptEdit>;
}

export async function reviseWithPrompt(
  input: Readonly<{
    revision: EditRevision;
    prompt: string;
    allowedSegments: ContentSegment[];
    designerId: string;
    deferPersistence?: boolean;
  }>,
  interpreter: PromptEditInterpreter,
  library: ContentLibrary,
): Promise<EditRevision> {
  if (input.prompt.trim().length === 0) throw new Error("designer-prompt-required");
  const { story, segments: interpreterSegments } = await library.readPromptRevisionSources(
    input.revision, input.allowedSegments,
  );
  const lineageIds = new Set([...story.anchorSegmentIds, ...story.historicalSegmentIds]);
  const edit = PromptEditSchema.parse(
    await interpreter.interpret({
      prompt: input.prompt,
      revision: input.revision,
      allowedSegments: interpreterSegments,
    }),
  );
  const targetVariants = new Set(edit.targetVariants);
  if (targetVariants.size !== edit.targetVariants.length) {
    throw new Error("duplicate-target-variant");
  }
  for (const kind of Object.keys(edit.replacementTimelines) as VideoVariant[]) {
    if (!targetVariants.has(kind)) throw new Error("edit-target-mismatch");
  }

  const allowedById = new Map(
    interpreterSegments
      .map((segment) => [segment.id, segment]),
  );
  for (const timeline of Object.values(edit.replacementTimelines)) {
    for (const item of timeline ?? []) {
      if (
        !allowedById.has(item.segmentId) ||
        !lineageIds.has(item.segmentId)
      ) {
        throw new Error("requested-material-not-in-authorized-library");
      }
    }
  }

  const variants = input.revision.variants.map((variant) => {
    if (!targetVariants.has(variant.kind)) return variant;
    const replacement = edit.replacementTimelines[variant.kind];
    const timeline = (replacement ?? variant.timeline).filter((item) => {
      const segment = allowedById.get(item.segmentId);
      if (!segment) {
        if (replacement) {
          throw new Error("requested-material-not-in-authorized-library");
        }
        return false;
      }
      return !segment.tags.people.some((person) =>
        edit.removePeople.includes(person),
      );
    });
    if (timeline.length === 0) throw new Error("prompt-produced-empty-variant");
    const coverStillExists = timeline.some(
      (item) => item.segmentId === variant.cover.segmentId,
    );
    return {
      ...variant,
      timeline,
      cover: coverStillExists
        ? variant.cover
        : { segmentId: timeline[0].segmentId, atMs: timeline[0].inMs },
    };
  });
  if (
    variants.some(
      (variant) =>
        !variant.timeline.some((item) =>
          story.anchorSegmentIds.includes(item.segmentId),
        ),
    )
  ) {
    throw new Error("new-batch-anchor-required-after-revision");
  }
  assertDifferentTimelines(variants);

  const nextRevisionNumber = input.revision.revision + 1;
  const existingRevisions = await library.listRevisions(input.revision.storyId);
  if (
    existingRevisions.some(
      (revision) => revision.revision === nextRevisionNumber,
    )
  ) {
    throw new Error("revision-already-exists");
  }

  const next = EditRevisionSchema.parse({
    ...input.revision,
    id: `revision_${input.revision.storyId}_${nextRevisionNumber}`,
    revision: nextRevisionNumber,
    parentRevisionId: input.revision.id,
    designerPrompt: input.prompt,
    changeExplanation: edit.explanation,
    variants,
    createdAt: new Date().toISOString(),
  });

  if (input.deferPersistence) return next;
  await commitPromptRevision(
    { previous: input.revision, next, artifacts: [], designerId: input.designerId },
    library,
  );
  return next;
}

export async function commitPromptRevision(
  input: Readonly<{
    previous: EditRevision;
    next: EditRevision;
    artifacts: VariantArtifact[];
    renderOutputRoot?: string;
    designerId: string;
  }>,
  library: ContentLibrary,
): Promise<void> {
  assertDifferentTimelines(input.next.variants);
  const stillsChanged = JSON.stringify(input.previous.selectedStills) !== JSON.stringify(input.next.selectedStills);
  const changedKinds = new Set(input.next.variants.filter((nextVariant) => {
    const previousVariant = input.previous.variants.find((variant) => variant.kind === nextVariant.kind);
    return stillsChanged || JSON.stringify(previousVariant) !== JSON.stringify(nextVariant);
  }).map((variant) => variant.kind));
  if (changedKinds.has("editorial-master")) {
    changedKinds.add("xiaohongshu");
    changedKinds.add("douyin");
    changedKinds.add("wechat-channels");
  }
  const priorApprovals = (await library.listApprovals()).filter(
    (approval) => approval.storyId === input.previous.storyId &&
      approval.status === "active" && changedKinds.has(approval.variant),
  );
  const invalidatedApprovals = priorApprovals.map((approval) => ({ ...approval, status: "invalidated" as const }));
  const approvalIds = new Set(priorApprovals.map((approval) => approval.id));
  const invalidatedPackages = (await library.listExportPackages())
    .filter((value) => approvalIds.has(value.approvalId) && value.status !== "invalidated")
    .map((value) => ({ ...value, status: "invalidated" as const }));
  await library.commitRevisionBundle({
    renderOutputRoot: input.renderOutputRoot,
    revisions: [input.previous, input.next],
    artifacts: input.artifacts,
    approvals: invalidatedApprovals,
    exportPackages: invalidatedPackages,
    auditEvent: {
    id: `audit_${randomUUID()}`,
    entityType: "revision",
    entityId: input.next.id,
    actorId: input.designerId,
    action: "designer-prompt-revision-created",
    reason: input.next.changeExplanation,
    before: input.previous,
    after: input.next,
    createdAt: new Date().toISOString(),
    },
  });
}
