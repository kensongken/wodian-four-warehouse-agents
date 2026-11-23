import { z } from "zod";

import {
  StudioWorkerRuntimeBundleV1Schema,
  StudioWorkerRuntimeReceiptBindingV1Schema,
} from "./studio-worker-runtime";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const DateTimeSchema = z.string().datetime();

export const STUDIO_MODEL_ANALYSIS_LIMITS = Object.freeze({
  identity: 200,
  sourcePath: 4096,
  observation: 2000,
  observations: 256,
  suggestedTag: 200,
  suggestedTags: 128,
  warning: 1000,
  warnings: 128,
  failureCode: 200,
  failureMessage: 1000,
  qualitySignalKey: 100,
  qualitySignalString: 4000,
  qualitySignalArrayItems: 2048,
  qualitySignalObjectKeys: 256,
  qualitySignalDepth: 8,
  qualitySignalTotalNodes: 4096,
  qualitySignalTotalStringBytes: 65_536,
} as const);

const ModelIdentitySchema = z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.identity);
const SourcePathSchema = z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.sourcePath);
const ObservationSchema = z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.observation);
const SuggestedTagSchema = z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTag);
const WarningSchema = z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.warning);
const addQualitySignalIssue = (
  ctx: z.RefinementCtx,
  path: Array<string | number>,
  message: string,
) => ctx.addIssue({ code: z.ZodIssueCode.custom, path, message });

const addQualitySignalStringBytes = (
  value: string,
  ctx: z.RefinementCtx,
  path: Array<string | number>,
  budget: {
    stringBytes: number;
    stringLimitReported: boolean;
  },
): void => {
  budget.stringBytes += new TextEncoder().encode(value).byteLength;
  if (
    budget.stringBytes > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalTotalStringBytes
    && !budget.stringLimitReported
  ) {
    budget.stringLimitReported = true;
    addQualitySignalIssue(ctx, path, "studio-model-quality-signal-total-string-byte-limit");
  }
};

const validateQualitySignal = (
  value: unknown,
  ctx: z.RefinementCtx,
  path: Array<string | number>,
  depth: number,
  budget: {
    nodes: number;
    stringBytes: number;
    nodeLimitReported: boolean;
    stringLimitReported: boolean;
  },
): void => {
  budget.nodes += 1;
  if (budget.nodes > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalTotalNodes && !budget.nodeLimitReported) {
    budget.nodeLimitReported = true;
    addQualitySignalIssue(ctx, path, "studio-model-quality-signal-total-node-limit");
  }
  if (budget.nodeLimitReported || budget.stringLimitReported) return;
  if (depth > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalDepth) {
    addQualitySignalIssue(ctx, path, "studio-model-quality-signal-depth-limit");
    return;
  }
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) addQualitySignalIssue(ctx, path, "studio-model-quality-signal-finite-number-required");
    return;
  }
  if (typeof value === "string") {
    addQualitySignalStringBytes(value, ctx, path, budget);
    if (value.length > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString) {
      addQualitySignalIssue(ctx, path, "studio-model-quality-signal-string-limit");
    }
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems) {
      addQualitySignalIssue(ctx, path, "studio-model-quality-signal-array-limit");
      return;
    }
    for (const [index, item] of value.entries()) {
      validateQualitySignal(item, ctx, [...path, index], depth + 1, budget);
      if (budget.nodeLimitReported || budget.stringLimitReported) break;
    }
    return;
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      addQualitySignalIssue(ctx, path, "studio-model-quality-signal-json-required");
      return;
    }
    const entries = Object.entries(value as Record<string, unknown>);
    if (
      entries.length > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalObjectKeys
      || Object.getOwnPropertySymbols(value).length > 0
    ) {
      addQualitySignalIssue(ctx, path, "studio-model-quality-signal-object-limit");
      return;
    }
    for (const [key, item] of entries) {
      addQualitySignalStringBytes(key, ctx, [...path, key], budget);
      if (budget.stringLimitReported) break;
      if (key.length > STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalKey) {
        addQualitySignalIssue(ctx, [...path, key], "studio-model-quality-signal-key-limit");
      }
      validateQualitySignal(item, ctx, [...path, key], depth + 1, budget);
      if (budget.nodeLimitReported || budget.stringLimitReported) break;
    }
    return;
  }
  addQualitySignalIssue(ctx, path, "studio-model-quality-signal-json-required");
};

export const StudioModelQualitySignalsSchema = z.record(z.unknown()).superRefine(
  (value, ctx) => validateQualitySignal(value, ctx, [], 0, {
    nodes: 0,
    stringBytes: 0,
    nodeLimitReported: false,
    stringLimitReported: false,
  }),
);

export const StudioAnalysisLayerSchema = z.enum([
  "technical",
  "scene",
  "transcript",
  "embedding",
  "vision",
]);

export const StudioModelProviderIdentitySchema = z.object({
  layer: StudioAnalysisLayerSchema,
  adapterName: ModelIdentitySchema,
  modelName: ModelIdentitySchema,
  modelVersion: ModelIdentitySchema,
  checkpointDigest: DigestSchema.nullable(),
}).strict();

export const StudioAnalysisStatusSchema = z.enum([
  "queued",
  "running",
  "succeeded",
  "failed-retryable",
  "failed-manual-review",
  "cancelled",
]);

const StudioAnalysisReceiptStatusSchema = z.enum([
  "succeeded",
  "failed-retryable",
  "failed-manual-review",
]);

export const StudioModelReceiptInputBindingV1Schema = z.object({
  sourceDigest: DigestSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

const StudioModelReceiptArtifactIdentityV1Schema = z.object({
  id: ModelIdentitySchema,
  version: ModelIdentitySchema,
  sha256: DigestSchema,
}).strict();

export const StudioModelReceiptArtifactBindingV1Schema = z.object({
  adapter: StudioWorkerRuntimeReceiptBindingV1Schema,
  model: StudioModelReceiptArtifactIdentityV1Schema,
}).strict();

export const StudioModelReceiptRuntimeMetricsV1Schema = z.object({
  runtimeMs: z.number().int().nonnegative(),
}).strict();

export const StudioTranscriptSpeechStateV1Schema = z.enum([
  "speech",
  "no-speech",
  "no-audio",
]);

export const ModelAnalysisJobV1Schema = z.object({
  schemaVersion: z.literal(1),
  jobId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourcePath: SourcePathSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  layer: StudioAnalysisLayerSchema,
  operationKey: NonEmptyIdSchema,
  requestedAt: DateTimeSchema,
}).strict().refine((value) => value.sourceEndMs > value.sourceStartMs, {
  message: "source-end-must-follow-start",
  path: ["sourceEndMs"],
});

export const ModelAnalysisReceiptV1Schema = z.object({
  schemaVersion: z.literal(1),
  jobId: NonEmptyIdSchema,
  customerId: NonEmptyIdSchema,
  sessionId: NonEmptyIdSchema,
  sourceAssetId: NonEmptyIdSchema,
  sourceDigest: DigestSchema,
  sourcePath: SourcePathSchema,
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  layer: StudioAnalysisLayerSchema,
  operationKey: NonEmptyIdSchema,
  adapterName: ModelIdentitySchema,
  modelName: ModelIdentitySchema,
  modelVersion: ModelIdentitySchema,
  checkpointDigest: DigestSchema.nullable(),
  inputDigest: DigestSchema,
  promptVersion: ModelIdentitySchema,
  observations: z.array(ObservationSchema).max(STUDIO_MODEL_ANALYSIS_LIMITS.observations),
  suggestedTags: z.array(SuggestedTagSchema).max(STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTags),
  qualitySignals: StudioModelQualitySignalsSchema,
  confidence: z.number().min(0).max(1),
  warnings: z.array(WarningSchema).max(STUDIO_MODEL_ANALYSIS_LIMITS.warnings),
  startedAt: DateTimeSchema,
  completedAt: DateTimeSchema,
  runtimeMs: z.number().int().nonnegative(),
  status: StudioAnalysisReceiptStatusSchema,
  failure: z.object({
    code: z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.failureCode),
    message: z.string().trim().min(1).max(STUDIO_MODEL_ANALYSIS_LIMITS.failureMessage).optional(),
  }).strict().nullable(),
  synthetic: z.boolean(),
  providerMode: z.enum(["local", "offline-fallback"]),
  inputBinding: StudioModelReceiptInputBindingV1Schema.optional(),
  artifactBinding: StudioModelReceiptArtifactBindingV1Schema.nullable().optional(),
  runtimeMetrics: StudioModelReceiptRuntimeMetricsV1Schema.optional(),
  speechState: StudioTranscriptSpeechStateV1Schema.nullable().optional(),
}).strict().superRefine((value, ctx) => {
  if (value.sourceEndMs <= value.sourceStartMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "source-end-must-follow-start",
      path: ["sourceEndMs"],
    });
  }
  if (Date.parse(value.completedAt) < Date.parse(value.startedAt)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "analysis-completion-must-follow-start",
      path: ["completedAt"],
    });
  }
  if (value.status === "succeeded" && value.failure !== null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "succeeded-analysis-receipt-must-not-have-failure",
      path: ["failure"],
    });
  }
  if (value.status !== "succeeded" && value.failure === null) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "failed-analysis-receipt-requires-reason",
      path: ["failure"],
    });
  }
  const isTranscriptWorkerReceipt = value.promptVersion === "studio-transcript-zh-v1";
  const isVisionWorkerReceipt = [
    "studio-qwen3-vl-zh-json-v1",
    "studio-qwen3-vl-zh-json-v2",
    "studio-qwen3-vl-zh-json-v3",
    "studio-qwen3-vl-zh-json-v4",
    "studio-qwen3-vl-zh-json-v5",
    "studio-qwen3-vl-highlight-json-v1",
    "studio-qwen3-vl-highlight-json-v2",
    "studio-qwen3-vl-highlight-json-v3",
    "studio-qwen3-vl-highlight-json-v4",
    "studio-qwen3-vl-highlight-json-v5",
    "studio-qwen3-vl-highlight-json-v6",
    "studio-qwen3-vl-highlight-json-v7",
    "studio-qwen3-vl-highlight-json-v8",
    "studio-qwen3-vl-highlight-json-v9",
    "studio-qwen3-vl-highlight-json-v11",
    "studio-qwen3-vl-highlight-pair-json-v12",
    "studio-qwen3-vl-highlight-pair-json-v13",
    "studio-qwen3-vl-highlight-pair-json-v14",
    "studio-qwen3-vl-highlight-pair-json-v15",
    "studio-qwen3-vl-highlight-pair-json-v16",
    "studio-qwen3-vl-highlight-pair-json-v17",
    "studio-qwen3-vl-highlight-pair-json-v18",
    "studio-qwen3-vl-highlight-pair-json-v19",
  ].includes(value.promptVersion);
  if (isTranscriptWorkerReceipt || isVisionWorkerReceipt) {
    if (
      value.inputBinding === undefined
      || value.inputBinding.sourceDigest !== value.sourceDigest
      || value.inputBinding.sourceStartMs !== value.sourceStartMs
      || value.inputBinding.sourceEndMs !== value.sourceEndMs
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "analysis-input-binding-mismatch",
        path: ["inputBinding"],
      });
    }
    if (value.runtimeMetrics === undefined || value.runtimeMetrics.runtimeMs !== value.runtimeMs) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "analysis-runtime-binding-mismatch",
        path: ["runtimeMetrics"],
      });
    }
    if (value.artifactBinding === undefined || (
      value.status === "succeeded" && value.artifactBinding === null
    )) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "analysis-artifact-binding-required",
        path: ["artifactBinding"],
      });
    } else if (value.artifactBinding !== null && (
      value.artifactBinding.model.id !== value.modelName
      || value.artifactBinding.model.version !== value.modelVersion
      || value.artifactBinding.model.sha256 !== value.checkpointDigest
    )) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "analysis-artifact-binding-mismatch",
        path: ["artifactBinding", "model"],
      });
    }
  }
  if (isTranscriptWorkerReceipt) {
    if (value.status === "succeeded" && value.speechState == null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "transcript-speech-state-required",
        path: ["speechState"],
      });
    }
    if (value.status !== "succeeded" && value.speechState !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "failed-transcript-speech-state-must-be-null",
        path: ["speechState"],
      });
    }
  } else if (value.speechState !== undefined) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "speech-state-is-transcript-only",
      path: ["speechState"],
    });
  }
});

const InstalledStudioModelStateV1Schema = z.object({
  mode: z.literal("local"),
  installed: z.literal(true),
  modelName: NonEmptyIdSchema,
  checkpointDigest: DigestSchema,
}).strict();

const UninstalledStudioModelStateV1Schema = z.object({
  mode: z.enum(["local", "offline-fallback"]),
  installed: z.literal(false),
  modelName: NonEmptyIdSchema.nullable(),
  checkpointDigest: z.null(),
}).strict();

export const StudioModelStateV1Schema = z.discriminatedUnion("installed", [
  InstalledStudioModelStateV1Schema,
  UninstalledStudioModelStateV1Schema,
]);

export const StudioModelArtifactSourceRevisionV1Schema = z.string({
  invalid_type_error: "immutable-model-artifact-source-revision-required",
}).regex(/^[a-f0-9]{40}$/, "immutable-model-artifact-source-revision-required");

export const StudioModelArtifactFileV1Schema = z.object({
  relativePath: z.string().trim().min(1).max(1024).refine(
    (value) => !value.startsWith("/") && !value.split("/").includes(".."),
    "model-artifact-relative-path-required",
  ),
  sourceUrl: z.string().url().refine(
    (value) => value.startsWith("https://"),
    "https-source-required",
  ).nullable(),
  required: z.literal(true),
  executable: z.boolean(),
  sha256: DigestSchema.nullable(),
  byteLength: z.number().int().positive().nullable(),
}).strict();

const RepositorySourceUrlSchema = z.string().url().refine((value) => {
  const url = new URL(value);
  const pathSegments = url.pathname.split("/").filter(Boolean);
  const hostname = url.hostname.toLowerCase();
  const isReservedExampleDomain = ["example.com", "example.net", "example.org"].some(
    (domain) => hostname === domain || hostname.endsWith(`.${domain}`),
  );
  return url.protocol === "https:"
    && url.search === ""
    && url.hash === ""
    && pathSegments.length === 2
    && !isReservedExampleDomain;
}, "model-artifact-repository-root-source-required");

const isFileUrlBoundToSourceRevision = (
  sourceUrl: string,
  revision: string,
  fileUrl: string,
): boolean => {
  const source = new URL(sourceUrl);
  const file = new URL(fileUrl);
  const sourcePath = source.pathname.replace(/\/$/, "");
  if (source.origin !== file.origin || !file.pathname.startsWith(`${sourcePath}/`)) return false;

  if (
    source.hostname === "codeload.github.com"
    && file.pathname === `${sourcePath}/tar.gz/${revision}`
  ) return true;

  if (
    file.pathname.startsWith(`${sourcePath}/resolve/${revision}/`)
    || file.pathname.startsWith(`${sourcePath}/raw/${revision}/`)
  ) return true;

  const archivePath = `${sourcePath}/archive/${revision}`;
  return file.pathname === archivePath
    || file.pathname === `${archivePath}.tar.gz`
    || file.pathname === `${archivePath}.zip`;
};

const isLicenseUrlBoundToSourceRevision = (
  sourceUrl: string,
  revision: string,
  evidenceUrl: string,
): boolean => {
  if (isFileUrlBoundToSourceRevision(sourceUrl, revision, evidenceUrl)) return true;
  const source = new URL(sourceUrl);
  const evidence = new URL(evidenceUrl);
  const sourcePath = source.pathname.replace(/\/$/, "");
  return source.hostname === "codeload.github.com"
    && evidence.hostname === "raw.githubusercontent.com"
    && evidence.pathname.startsWith(`${sourcePath}/${revision}/`);
};

const hasLicenseEvidencePlaceholder = (
  sourceUrl: string,
  revision: string,
  evidenceUrl: string,
): boolean => {
  const sourcePath = new URL(sourceUrl).pathname.replace(/\/$/, "");
  const url = new URL(evidenceUrl);
  let revisionPrefix = [
    `${sourcePath}/resolve/${revision}`,
    `${sourcePath}/raw/${revision}`,
    `${sourcePath}/archive/${revision}`,
  ].find((candidate) => url.pathname.startsWith(candidate));
  const source = new URL(sourceUrl);
  if (
    revisionPrefix === undefined
    && source.hostname === "codeload.github.com"
    && url.hostname === "raw.githubusercontent.com"
    && url.pathname.startsWith(`${sourcePath}/${revision}/`)
  ) revisionPrefix = `${sourcePath}/${revision}`;
  if (revisionPrefix === undefined) return true;
  try {
    const evidenceLocation = `${url.pathname.slice(revisionPrefix.length)}${url.search}${url.hash}`;
    const decodedLocation = decodeURIComponent(evidenceLocation);
    return /(?:^|[^a-z0-9])(?:todo|placeholder)(?=$|[^a-z0-9])/i.test(decodedLocation);
  } catch {
    return true;
  }
};

export const StudioModelArtifactSourceV1Schema = z.object({
  id: NonEmptyIdSchema,
  kind: z.enum(["code", "weights"]),
  sourceUrl: RepositorySourceUrlSchema,
  sourceRevision: StudioModelArtifactSourceRevisionV1Schema.nullable(),
  inventoryVerified: z.boolean(),
  files: z.array(StudioModelArtifactFileV1Schema).min(1).max(256),
}).strict().superRefine((value, ctx) => {
  if (new Set(value.files.map((file) => file.relativePath)).size !== value.files.length) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "duplicate-model-artifact-file-path",
      path: ["files"],
    });
  }
  for (const [index, file] of value.files.entries()) {
    if (
      file.sourceUrl !== null
      && (
        value.sourceRevision === null
        || !isFileUrlBoundToSourceRevision(value.sourceUrl, value.sourceRevision, file.sourceUrl)
      )
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "model-artifact-file-source-revision-mismatch",
        path: ["files", index, "sourceUrl"],
      });
    }
  }
});

export const STUDIO_MODEL_LICENSE_EXPRESSIONS = [
  "Apache-2.0",
  "BSD-3-Clause",
  "ISC",
  "MIT",
  "MPL-2.0",
  "Apache-2.0 AND MIT",
] as const;

const ApplicableLicenseEvidenceSchema = z.object({
  applies: z.literal(true),
  sourceId: NonEmptyIdSchema.nullable(),
  sourceRevision: StudioModelArtifactSourceRevisionV1Schema.nullable(),
  spdxExpression: z.enum(STUDIO_MODEL_LICENSE_EXPRESSIONS).nullable(),
  evidenceUrl: z.string().url().refine(
    (value) => value.startsWith("https://"),
    "https-license-evidence-required",
  ).nullable(),
}).strict();

const NotApplicableLicenseEvidenceSchema = z.object({
  applies: z.literal(false),
  sourceId: z.null(),
  sourceRevision: z.null(),
  spdxExpression: z.null(),
  evidenceUrl: z.null(),
}).strict();

export const StudioModelLicenseEvidenceV1Schema = z.discriminatedUnion("applies", [
  ApplicableLicenseEvidenceSchema,
  NotApplicableLicenseEvidenceSchema,
]);

export const StudioModelRuntimeFileV1Schema = z.object({
  relativePath: z.string().trim().min(1).max(1024).refine(
    (value) => !value.startsWith("/") && !value.split("/").includes(".."),
    "model-artifact-relative-path-required",
  ),
  candidateRelativePath: z.string().trim().min(1).max(1024).refine(
    (value) => !value.startsWith("/") && !value.split("/").includes(".."),
    "model-artifact-relative-path-required",
  ),
  required: z.literal(true),
  executable: z.boolean(),
  sourceKind: z.enum(["code", "weights"]),
  sha256: DigestSchema,
  byteLength: z.number().int().positive(),
}).strict();

export const StudioModelDerivationReceiptV1Schema = z.object({
  schemaVersion: z.literal(1),
  recipeId: NonEmptyIdSchema,
  inputSourceIds: z.array(NonEmptyIdSchema).min(1).max(8).refine(
    (ids) => new Set(ids).size === ids.length,
    "duplicate-model-artifact-derivation-input-source-id",
  ),
  outputTreeSha256: DigestSchema,
  probeReportSha256: DigestSchema,
  status: z.literal("succeeded"),
  syntheticInput: z.literal(true),
  realModel: z.literal(true),
}).strict();

export const StudioModelArtifactV1Schema = z.object({
  id: NonEmptyIdSchema,
  version: NonEmptyIdSchema,
  artifactKind: z.enum(["source-package", "binary-model", "checkpoint"]),
  sources: z.array(StudioModelArtifactSourceV1Schema).min(1).max(8),
  licenseEvidence: z.object({
    code: StudioModelLicenseEvidenceV1Schema,
    weights: StudioModelLicenseEvidenceV1Schema,
  }).strict(),
  licenseReview: z.enum(["pending", "approved", "rejected"]),
  runtimeBackend: NonEmptyIdSchema,
  quantization: NonEmptyIdSchema.nullable(),
  enabled: z.boolean(),
  approved: z.boolean(),
  approvedBy: NonEmptyIdSchema.nullable(),
  approvedAt: DateTimeSchema.nullable(),
  testedDevices: z.array(NonEmptyIdSchema),
  testSetVersion: NonEmptyIdSchema.nullable(),
  runtimeFiles: z.array(StudioModelRuntimeFileV1Schema).min(1).max(256).optional(),
  derivationReceipt: StudioModelDerivationReceiptV1Schema.optional(),
}).strict().superRefine((value, ctx) => {
  if (new Set(value.sources.map((source) => source.id)).size !== value.sources.length) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "duplicate-model-artifact-source-id",
      path: ["sources"],
    });
  }
  if (new Set(value.sources.map((source) => source.kind)).size !== value.sources.length) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "duplicate-model-artifact-source-kind",
      path: ["sources"],
    });
  }
  const filePaths = value.sources.flatMap((source) => source.files.map((file) => file.relativePath));
  if (new Set(filePaths).size !== filePaths.length) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "duplicate-model-artifact-file-path",
      path: ["sources"],
    });
  }
  if ((value.runtimeFiles === undefined) !== (value.derivationReceipt === undefined)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "derived-model-artifact-requires-runtime-manifest-and-receipt",
      path: ["runtimeFiles"],
    });
  }
  if (value.runtimeFiles !== undefined && value.derivationReceipt !== undefined) {
    const runtimePaths = value.runtimeFiles.map((file) => file.relativePath);
    const candidatePaths = value.runtimeFiles.map((file) => file.candidateRelativePath);
    const sourceIds = new Set(value.sources.map((source) => source.id));
    const sourceKinds = new Set(value.sources.map((source) => source.kind));
    if (new Set(runtimePaths).size !== runtimePaths.length) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "duplicate-model-artifact-runtime-file-path",
        path: ["runtimeFiles"],
      });
    }
    if (new Set(candidatePaths).size !== candidatePaths.length) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "duplicate-model-artifact-candidate-file-path",
        path: ["runtimeFiles"],
      });
    }
    if (value.runtimeFiles.some((file) => !sourceKinds.has(file.sourceKind))) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "derived-model-artifact-runtime-source-kind-mismatch",
        path: ["runtimeFiles"],
      });
    }
    if (value.derivationReceipt.inputSourceIds.some((id) => !sourceIds.has(id))) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "derived-model-artifact-input-source-mismatch",
        path: ["derivationReceipt", "inputSourceIds"],
      });
    }
  }
  const hasCode = value.sources.some((source) => source.kind === "code");
  const hasWeights = value.sources.some((source) => source.kind === "weights");
  if (value.licenseEvidence.code.applies !== hasCode || value.licenseEvidence.weights.applies !== hasWeights) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "model-artifact-license-scope-mismatch",
      path: ["licenseEvidence"],
    });
  }
  if (
    value.approved
    && value.sources.some((source) => (
      source.sourceRevision === null
      || !source.inventoryVerified
      || source.files.some((file) => (
        file.sourceUrl === null
        || file.sha256 === null
        || file.byteLength === null
      ))
    ))
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "approved-model-artifact-requires-complete-file-manifest",
      path: ["sources"],
    });
  }
  if (value.approved && value.licenseReview !== "approved") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "approved-model-artifact-requires-license-review",
      path: ["licenseReview"],
    });
  }
  for (const kind of ["code", "weights"] as const) {
    const evidence = value.licenseEvidence[kind];
    if (value.licenseReview !== "approved" || !evidence.applies) continue;
    if (
      evidence.sourceId === null
      || evidence.sourceRevision === null
      || evidence.spdxExpression === null
      || evidence.evidenceUrl === null
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `approved-license-review-requires-${kind}-license-evidence`,
        path: ["licenseEvidence", kind],
      });
      continue;
    }
    const source = value.sources.find((candidate) => candidate.id === evidence.sourceId);
    if (
      source === undefined
      || source.kind !== kind
      || source.sourceRevision !== evidence.sourceRevision
      || !isLicenseUrlBoundToSourceRevision(source.sourceUrl, evidence.sourceRevision, evidence.evidenceUrl)
      || hasLicenseEvidencePlaceholder(source.sourceUrl, evidence.sourceRevision, evidence.evidenceUrl)
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${kind}-license-evidence-source-revision-mismatch`,
        path: ["licenseEvidence", kind],
      });
    }
  }
  if (value.approved && (value.approvedBy === null || value.approvedAt === null)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "approved-model-artifact-requires-approval-record",
      path: ["approvedBy"],
    });
  }
  if (value.enabled && !value.approved) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "enabled-model-artifact-must-be-approved",
      path: ["enabled"],
    });
  }
});

export const StudioModelArtifactRegistryV1Schema = z.object({
  schemaVersion: z.literal(1),
  artifacts: z.array(StudioModelArtifactV1Schema).refine(
    (artifacts) => new Set(artifacts.map((artifact) => artifact.id)).size === artifacts.length,
    "duplicate-model-artifact-id",
  ),
  workerRuntimes: z.array(StudioWorkerRuntimeBundleV1Schema).refine(
    (runtimes) => new Set(runtimes.map((runtime) => runtime.id)).size === runtimes.length,
    "duplicate-worker-runtime-id",
  ).optional(),
}).strict();

const RECEIPT_BINDING_FIELDS = [
  "jobId",
  "customerId",
  "sessionId",
  "sourceAssetId",
  "sourceDigest",
  "sourcePath",
  "sourceStartMs",
  "sourceEndMs",
  "layer",
  "operationKey",
] as const;

export function assertReceiptMatchesJob(jobInput: unknown, receiptInput: unknown): void {
  const job = ModelAnalysisJobV1Schema.parse(jobInput);
  const receipt = ModelAnalysisReceiptV1Schema.parse(receiptInput);

  if (RECEIPT_BINDING_FIELDS.some((field) => receipt[field] !== job[field])) {
    throw new Error("studio-model-receipt-binding-mismatch");
  }
}

export type StudioAnalysisLayer = z.infer<typeof StudioAnalysisLayerSchema>;
export type StudioModelProviderIdentity = z.infer<typeof StudioModelProviderIdentitySchema>;
export type StudioAnalysisStatus = z.infer<typeof StudioAnalysisStatusSchema>;
export type StudioModelReceiptInputBindingV1 = z.infer<typeof StudioModelReceiptInputBindingV1Schema>;
export type StudioModelReceiptArtifactBindingV1 = z.infer<typeof StudioModelReceiptArtifactBindingV1Schema>;
export type StudioModelReceiptRuntimeMetricsV1 = z.infer<typeof StudioModelReceiptRuntimeMetricsV1Schema>;
export type StudioTranscriptSpeechStateV1 = z.infer<typeof StudioTranscriptSpeechStateV1Schema>;
export type ModelAnalysisJobV1 = z.infer<typeof ModelAnalysisJobV1Schema>;
export type ModelAnalysisReceiptV1 = z.infer<typeof ModelAnalysisReceiptV1Schema>;
export type StudioModelStateV1 = z.infer<typeof StudioModelStateV1Schema>;
export type StudioModelArtifactSourceRevisionV1 = z.infer<typeof StudioModelArtifactSourceRevisionV1Schema>;
export type StudioModelArtifactFileV1 = z.infer<typeof StudioModelArtifactFileV1Schema>;
export type StudioModelArtifactSourceV1 = z.infer<typeof StudioModelArtifactSourceV1Schema>;
export type StudioModelLicenseEvidenceV1 = z.infer<typeof StudioModelLicenseEvidenceV1Schema>;
export type StudioModelRuntimeFileV1 = z.infer<typeof StudioModelRuntimeFileV1Schema>;
export type StudioModelDerivationReceiptV1 = z.infer<typeof StudioModelDerivationReceiptV1Schema>;
export type StudioModelArtifactV1 = z.infer<typeof StudioModelArtifactV1Schema>;
export type StudioModelArtifactRegistryV1 = z.infer<typeof StudioModelArtifactRegistryV1Schema>;
