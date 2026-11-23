import { z } from "zod";

const IdSchema = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/);
const HexIdSchema = z.string().regex(/^[a-f0-9]{32}$/);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const DateTimeSchema = z.string().datetime();
const SafeRelativePathSchema = z.string().trim().min(1).max(512).refine((value) => {
  if (value.startsWith("/") || value.startsWith("\\") || value.includes("\\") || /^[A-Za-z]:/.test(value)) return false;
  return value.split("/").every((segment) => segment.length > 0 && segment !== "." && segment !== "..");
}, "path-must-be-safe-relative-path");

export const HER_WORLD_CLIP_CONTRACT_VERSION = 1 as const;
export const HerWorldClipProfileSchema = z.enum(["editorial-master", "xiaohongshu", "wechat-channels"]);

export const HerWorldClipMediaSchema = z.object({
  id: IdSchema,
  order: z.number().int().nonnegative(),
  kind: z.enum(["image", "video", "audio"]),
  mimeType: z.enum([
    "image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime",
    "audio/mpeg", "audio/mp4", "audio/wav",
  ]),
  byteLength: z.number().int().positive().max(2_147_483_648),
  sha256: DigestSchema,
  fetch: z.object({
    url: z.string().url().refine((value) => value.startsWith("https://"), "https-required"),
    expiresAt: DateTimeSchema,
    authorization: z.literal("bearer-job-token"),
  }).strict(),
  rights: z.object({
    confirmed: z.literal(true),
    scope: z.literal("render-this-story"),
    confirmedAt: DateTimeSchema,
  }).strict(),
}).strict().superRefine((value, ctx) => {
  const prefixByKind = { image: "image/", video: "video/", audio: "audio/" } as const;
  if (!value.mimeType.startsWith(prefixByKind[value.kind])) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["mimeType"], message: "media-kind-mime-mismatch" });
  }
});

export const HerWorldClipActSchema = z.object({
  sequence: z.number().int().positive(),
  stageId: IdSchema,
  mediaId: IdSchema,
  storyFunction: z.enum(["place-entry", "people", "craft", "product-origin", "product-reveal", "reflection"]),
  heading: z.string().trim().min(1).max(120),
  narration: z.string().trim().min(1).max(800),
  sourceBasis: z.string().trim().min(1).max(300),
  productReference: z.object({
    productId: IdSchema,
    role: z.enum(["context", "origin", "reveal"]),
  }).strict().nullable(),
}).strict();

export const HerWorldThemeMaterialSchema = z.object({
  kind: z.enum(["theme_intro", "place_context", "missing_shot"]),
  title: z.string().trim().min(1).max(80),
  content: z.string().trim().min(1).max(300),
  basis: z.string().trim().min(1).max(160),
}).strict();

export const HerWorldClipHandoffV1Schema = z.object({
  schemaVersion: z.literal(HER_WORLD_CLIP_CONTRACT_VERSION),
  kind: z.literal("her-world-story-render"),
  jobId: HexIdSchema,
  operationKey: IdSchema,
  createdAt: DateTimeSchema,
  story: z.object({
    id: HexIdSchema,
    revision: z.number().int().positive(),
    title: z.string().trim().min(1).max(120),
    place: z.string().trim().min(1).max(120),
    visitedAt: DateTimeSchema.nullable(),
    theme: z.string().trim().min(1).max(200).nullable().optional(),
    themeMaterials: z.array(HerWorldThemeMaterialSchema).max(3).optional(),
    ownerConfirmed: z.literal(true),
    confirmedAt: DateTimeSchema,
    acts: z.array(HerWorldClipActSchema).min(1).max(120),
  }).strict(),
  media: z.array(HerWorldClipMediaSchema).min(1).max(500),
  narration: z.object({
    mode: z.enum(["text-only", "provided-audio"]),
    audioMediaId: IdSchema.nullable(),
  }).strict(),
  output: z.object({
    aspectRatio: z.literal("9:16"),
    width: z.literal(720),
    height: z.literal(1280),
    durationSeconds: z.number().int().min(15).max(180),
    profiles: z.array(HerWorldClipProfileSchema).min(1).max(3),
    captions: z.literal(true),
  }).strict(),
  publishClaimed: z.literal(false),
}).strict().superRefine((value, ctx) => {
  const mediaIds = value.media.map((item) => item.id);
  const sequences = value.story.acts.map((act) => act.sequence);
  const stageIds = value.story.acts.map((act) => act.stageId);
  const addDuplicateIssue = <T extends string | number>(values: readonly T[], path: (string | number)[], message: string) => {
    if (new Set(values).size !== values.length) ctx.addIssue({ code: z.ZodIssueCode.custom, path, message });
  };
  addDuplicateIssue(mediaIds, ["media"], "duplicate-media-id");
  addDuplicateIssue(sequences, ["story", "acts"], "duplicate-act-sequence");
  addDuplicateIssue(stageIds, ["story", "acts"], "duplicate-stage-id");
  addDuplicateIssue(value.output.profiles, ["output", "profiles"], "duplicate-output-profile");

  const mediaSet = new Set(mediaIds);
  value.story.acts.forEach((act, index) => {
    if (!mediaSet.has(act.mediaId)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["story", "acts", index, "mediaId"], message: "act-media-not-found" });
    }
  });
  const createdAt = Date.parse(value.createdAt);
  if (createdAt < Date.parse(value.story.confirmedAt)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["createdAt"], message: "handoff-created-before-owner-confirmation" });
  }
  value.media.forEach((item, index) => {
    if (Date.parse(item.fetch.expiresAt) <= createdAt) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["media", index, "fetch", "expiresAt"], message: "media-fetch-expired-at-handoff" });
    }
  });
  if (value.narration.mode === "provided-audio") {
    const audio = value.media.find((item) => item.id === value.narration.audioMediaId);
    if (!audio || audio.kind !== "audio") {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["narration", "audioMediaId"], message: "narration-audio-not-found" });
    }
  } else if (value.narration.audioMediaId !== null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["narration", "audioMediaId"], message: "text-only-must-not-bind-audio" });
  }
});

const HerWorldClipOutputSchema = z.object({
  profile: HerWorldClipProfileSchema,
  relativePath: SafeRelativePathSchema,
  sha256: DigestSchema,
  durationSeconds: z.number().positive().max(180),
  width: z.literal(720),
  height: z.literal(1280),
  sourceMap: z.array(z.object({
    mediaId: IdSchema,
    timelineStartMs: z.number().int().nonnegative(),
    timelineEndMs: z.number().int().positive(),
  }).strict().refine((value) => value.timelineEndMs > value.timelineStartMs, "source-map-end-must-follow-start")).min(1),
}).strict();

export const HerWorldClipReceiptV1Schema = z.object({
  schemaVersion: z.literal(HER_WORLD_CLIP_CONTRACT_VERSION),
  kind: z.literal("her-world-story-render-receipt"),
  jobId: HexIdSchema,
  storyId: HexIdSchema,
  storyRevision: z.number().int().positive(),
  status: z.enum(["queued", "pulling", "rendering", "review-ready", "confirmed", "failed", "cancelled"]),
  progress: z.number().int().min(0).max(100),
  outputs: z.array(HerWorldClipOutputSchema).max(3),
  ownerAccepted: z.boolean(),
  published: z.literal(false),
  error: z.object({ code: IdSchema, message: z.string().trim().min(1).max(500), retryable: z.boolean() }).strict().nullable(),
  updatedAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  const ready = value.status === "review-ready" || value.status === "confirmed";
  if (ready && value.outputs.length === 0) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["outputs"], message: "ready-status-requires-output" });
  }
  if (value.status === "confirmed" && (!value.ownerAccepted || value.progress !== 100)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["ownerAccepted"], message: "confirmed-status-requires-owner-acceptance" });
  }
  if (value.status !== "confirmed" && value.ownerAccepted) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["ownerAccepted"], message: "owner-acceptance-only-after-confirmation" });
  }
  if (value.status === "failed" && value.error === null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["error"], message: "failed-status-requires-error" });
  }
  if (value.status !== "failed" && value.error !== null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["error"], message: "error-only-for-failed-status" });
  }
  if (new Set(value.outputs.map((item) => item.profile)).size !== value.outputs.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["outputs"], message: "duplicate-output-profile" });
  }
});

export function assertHerWorldClipHandoffFresh(handoff: HerWorldClipHandoffV1, now: Date) {
  for (const media of handoff.media) {
    if (Date.parse(media.fetch.expiresAt) <= now.getTime()) throw new Error(`media-fetch-expired:${media.id}`);
  }
}

export function assertHerWorldClipReceiptMatches(handoff: HerWorldClipHandoffV1, receipt: HerWorldClipReceiptV1) {
  if (receipt.jobId !== handoff.jobId || receipt.storyId !== handoff.story.id || receipt.storyRevision !== handoff.story.revision) {
    throw new Error("receipt-handoff-binding-mismatch");
  }
  const requestedProfiles = new Set(handoff.output.profiles);
  const sourceMedia = new Set(handoff.media.map((item) => item.id));
  for (const output of receipt.outputs) {
    if (!requestedProfiles.has(output.profile)) throw new Error(`unrequested-output-profile:${output.profile}`);
    for (const source of output.sourceMap) {
      if (!sourceMedia.has(source.mediaId)) throw new Error(`unknown-source-media:${source.mediaId}`);
    }
  }
  if (Date.parse(receipt.updatedAt) < Date.parse(handoff.createdAt)) throw new Error("receipt-predates-handoff");
}

export type HerWorldClipHandoffV1 = z.infer<typeof HerWorldClipHandoffV1Schema>;
export type HerWorldClipReceiptV1 = z.infer<typeof HerWorldClipReceiptV1Schema>;
