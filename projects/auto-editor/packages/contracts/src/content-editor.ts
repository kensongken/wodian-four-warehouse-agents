import { z } from "zod";

export const BaseAssetTypeSchema = z.enum([
  "product-image",
  "handmade",
  "scene",
  "persona-copy",
  "customer-interaction",
]);

export const StoryTypeSchema = z.enum([
  "inner-journey",
  "product-journey",
  "persona-monologue",
]);

export const VideoVariantSchema = z.enum([
  "editorial-master",
  "xiaohongshu",
  "douyin",
  "wechat-channels",
]);

export const PermissionStateSchema = z.enum([
  "allowed",
  "review-required",
  "blocked",
  "withdrawn",
]);

export const ProductTagSchema = z.object({
  id: z.string().min(1),
  status: z.enum(["suggested", "confirmed"]),
});

export const UsageTagSchema = z.object({
  storyIds: z.array(z.string()),
  variants: z.array(VideoVariantSchema),
  useCount: z.number().int().nonnegative(),
  lastUsedAt: z.string().datetime().nullable(),
});

export const SegmentTagsSchema = z.object({
  baseTypes: z.array(BaseAssetTypeSchema).min(1),
  people: z.array(z.string()),
  products: z.array(ProductTagSchema),
  processStages: z.array(z.string()),
  topics: z.array(z.string()),
  emotions: z.array(z.string()),
  transcript: z.string(),
  technicalFlags: z.array(z.string()),
  usage: UsageTagSchema,
});

export const LibraryAssetSchema = z.object({
  id: z.string().min(1),
  batchId: z.string().min(1),
  sessionId: z.string().min(1),
  digest: z.string().min(1),
  localPath: z.string().min(1),
  mediaKind: z.enum(["video", "image", "audio"]),
  importedAt: z.string().datetime(),
  permissionState: PermissionStateSchema,
  origin: z.enum(["new", "historical"]),
  technicalFlags: z.array(z.string()),
  derivedFromAssetId: z.string().min(1).nullable().optional(),
  privacyActions: z.array(z.string().min(1)).optional(),
  privacyEvidence: z.object({
    processor: z.string().min(1),
    sourceDigest: z.string().min(1),
    outputDigest: z.string().min(1),
    processedAt: z.string().datetime(),
  }).optional(),
  captureContext: z.object({
    creativeSessionId: z.string().min(1),
    stepId: z.string().min(1),
    deviceId: z.string().min(1),
    deviceRole: z.enum(["watch-director", "main-stone-a", "main-stone-b"]),
    carrier: z.string().min(1),
    scriptVersion: z.number().int().positive(),
    shotPlanVersion: z.number().int().positive(),
    take: z.number().int().positive(),
    stepOutcome: z.enum(["completed", "retake", "skipped"]),
  }).optional(),
});

const SegmentBoundsSchema = z
  .object({
    id: z.string().min(1),
    assetId: z.string().min(1),
    batchId: z.string().min(1),
    startMs: z.number().int().nonnegative(),
    endMs: z.number().int().positive(),
    permissionState: PermissionStateSchema,
  })
  .refine((value) => value.endMs > value.startMs, {
    message: "segment-end-must-follow-start",
  });

export const UntypedSegmentSchema = SegmentBoundsSchema.and(
  z.object({
    transcript: z.string(),
    technicalFlags: z.array(z.string()),
  }),
);

export const ContentSegmentSchema = SegmentBoundsSchema.and(
  z.object({ tags: SegmentTagsSchema }),
);

export const StoryCandidateSchema = z.object({
  id: z.string().min(1),
  batchId: z.string().min(1),
  type: StoryTypeSchema,
  anchorSegmentIds: z
    .array(z.string())
    .min(1, { message: "new-batch-anchor-required" }),
  historicalSegmentIds: z.array(z.string()),
  historicalReasons: z.record(z.string()),
  status: z.enum(["candidate", "catalog-only"]),
});

export const TimelineItemSchema = z.object({
  segmentId: z.string().min(1),
  inMs: z.number().int().nonnegative(),
  outMs: z.number().int().positive(),
  role: z.enum(["anchor", "context", "evidence", "transition"]),
});

export const VariantPlanSchema = z.object({
  kind: VideoVariantSchema,
  timeline: z.array(TimelineItemSchema),
  title: z.string().min(1),
  caption: z.string(),
  cover: z.object({
    segmentId: z.string().min(1),
    atMs: z.number().int().nonnegative(),
  }),
});

export const EditRevisionSchema = z
  .object({
    id: z.string().min(1),
    storyId: z.string().min(1),
    revision: z.number().int().positive(),
    parentRevisionId: z.string().nullable(),
    designerPrompt: z.string().nullable(),
    changeExplanation: z.string().nullable(),
    variants: z.array(VariantPlanSchema),
    selectedStills: z.array(
      z.object({
        segmentId: z.string().min(1),
        atMs: z.number().int().nonnegative(),
        reason: z.string().min(1),
      }),
    ),
    createdAt: z.string().datetime(),
  })
  .superRefine((value, ctx) => {
    const kinds = new Set(value.variants.map((item) => item.kind));
    if (kinds.size !== 4 || value.variants.length !== 4) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "four-distinct-variants-required",
      });
    }
  });

export const VariantArtifactSchema = z.object({
  id: z.string().min(1),
  revisionId: z.string().min(1),
  kind: VideoVariantSchema,
  videoPath: z.string().min(1),
  digest: z.string().min(1),
  sourceSegmentIds: z.array(z.string()).min(1).refine(allUnique, "duplicate-source-segment-id"),
  coverPath: z.string().min(1),
  coverDigest: z.string().min(1),
  stillPaths: z.array(z.string().min(1)),
  stillDigests: z.array(z.string().min(1)),
  visualSourceSegmentIds: z.array(z.string().min(1)).refine(allUnique, "duplicate-visual-source-segment-id"),
  renderInputDigest: z.string().min(1),
  status: z.enum(["active", "invalidated"]),
}).refine((value) => value.stillPaths.length === value.stillDigests.length, {
  message: "still-digest-count-mismatch",
});

export const ApprovalSchema = z.object({
  id: z.string().min(1),
  storyId: z.string().min(1),
  revisionId: z.string().min(1),
  revision: z.number().int().positive(),
  artifactId: z.string().min(1),
  artifactDigest: z.string().min(1),
  coverDigest: z.string().min(1),
  stillDigests: z.array(z.string().min(1)),
  renderInputDigest: z.string().min(1),
  variant: VideoVariantSchema,
  reviewerId: z.string().min(1),
  approvedAt: z.string().datetime(),
  status: z.enum(["active", "invalidated"]),
});

export const AuditEventSchema = z.object({
  id: z.string().min(1),
  entityType: z.enum([
    "asset",
    "segment",
    "story",
    "revision",
    "artifact",
    "approval",
    "export",
    "creative-session",
    "material-decision",
    "script",
    "shot-plan",
    "device-task",
    "device-event",
    "evidence-card",
  ]),
  entityId: z.string().min(1),
  actorId: z.string().min(1),
  action: z.string().min(1),
  reason: z.string().nullable(),
  before: z.unknown().nullable(),
  after: z.unknown().nullable(),
  createdAt: z.string().datetime(),
});

export const ExportPackageSchema = z.object({
  id: z.string().min(1),
  approvalId: z.string().min(1),
  artifactId: z.string().min(1),
  variant: VideoVariantSchema,
  videoPath: z.string().min(1),
  videoDigest: z.string().min(1),
  title: z.string().min(1),
  caption: z.string(),
  coverPath: z.string().min(1),
  coverDigest: z.string().min(1),
  stillPaths: z.array(z.string()),
  stillDigests: z.array(z.string().min(1)),
  renderInputDigest: z.string().min(1),
  idempotencyKey: z.string().min(1),
  status: z.enum(["pending", "delivered", "pending-retry", "invalidated"]),
  createdAt: z.string().datetime(),
}).refine((value) => value.stillPaths.length === value.stillDigests.length, {
  message: "export-still-digest-count-mismatch",
});

export const ExportDeliverySchema = z.object({
  packageId: z.string().min(1),
  status: z.enum(["received", "pending-retry"]),
  receiptId: z.string().nullable(),
  receivedAt: z.string().datetime().nullable(),
  publishedAt: z.string().datetime().nullable(),
  publishedClaimed: z.literal(false),
});

export type BaseAssetType = z.infer<typeof BaseAssetTypeSchema>;
export type StoryType = z.infer<typeof StoryTypeSchema>;
export type VideoVariant = z.infer<typeof VideoVariantSchema>;
export type PermissionState = z.infer<typeof PermissionStateSchema>;
export type ProductTag = z.infer<typeof ProductTagSchema>;
export type UsageTag = z.infer<typeof UsageTagSchema>;
export type SegmentTags = z.infer<typeof SegmentTagsSchema>;
export type LibraryAsset = z.infer<typeof LibraryAssetSchema>;
export type UntypedSegment = z.infer<typeof UntypedSegmentSchema>;
export type ContentSegment = z.infer<typeof ContentSegmentSchema>;
export type StoryCandidate = z.infer<typeof StoryCandidateSchema>;
export type TimelineItem = z.infer<typeof TimelineItemSchema>;
export type VariantPlan = z.infer<typeof VariantPlanSchema>;
export type EditRevision = z.infer<typeof EditRevisionSchema>;
export type VariantArtifact = z.infer<typeof VariantArtifactSchema>;
export type Approval = z.infer<typeof ApprovalSchema>;
export type AuditEvent = z.infer<typeof AuditEventSchema>;
export type ExportPackage = z.infer<typeof ExportPackageSchema>;
export type ExportDelivery = z.infer<typeof ExportDeliverySchema>;

function allUnique(values: string[]): boolean {
  return new Set(values).size === values.length;
}
