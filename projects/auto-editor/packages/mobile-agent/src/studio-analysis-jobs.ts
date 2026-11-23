import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { isAbsolute } from "node:path";
import { promisify } from "node:util";

import {
  ModelAnalysisReceiptV1Schema,
  StudioAnalysisLayerSchema,
  StudioAnalysisStatusSchema,
  StudioOperationKeySchema,
  assertReceiptMatchesJob,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type StudioAnalysisLayer,
  type StudioAnalysisStatus,
} from "@wearable/contracts";
import { z } from "zod";

import {
  CustomerMaterialLibrary,
  type ReferenceAnalysisAssetSnapshot,
  type StudioAnalysisAssetSnapshot,
} from "./customer-material-library";
import type { StudioScope } from "./studio-capabilities";
import {
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
  type StudioModelWorker,
  type StudioTrustedWorkerContext,
} from "./studio-model-worker";
import type { StudioWorkerStageTelemetrySummaryV1 } from "./studio-worker-stage-telemetry";

const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const DateTimeSchema = z.string().datetime();
const StudioWorkerStageNameSchema = z.enum([
  "runtime-preaccept",
  "frontstage-extraction",
  "model-load",
  "processor-prepare",
  "model-generation",
  "output-validation",
]);
const StudioWorkerStageTelemetrySchema = z.object({
  schemaVersion: z.literal(1),
  lastStage: StudioWorkerStageNameSchema.nullable(),
  completedStageMs: z.object({
    "runtime-preaccept": z.number().int().nonnegative().optional(),
    "frontstage-extraction": z.number().int().nonnegative().optional(),
    "model-load": z.number().int().nonnegative().optional(),
    "processor-prepare": z.number().int().nonnegative().optional(),
    "model-generation": z.number().int().nonnegative().optional(),
    "output-validation": z.number().int().nonnegative().optional(),
  }).strict(),
  activeStageElapsedMs: z.number().int().nonnegative().nullable(),
}).strict();
const StudioAnalysisFailureSchema = z.object({
  code: z.string().regex(/^[a-z0-9-]{1,200}$/),
  message: z.string().min(1).max(1_000).nullable(),
  stageTelemetry: StudioWorkerStageTelemetrySchema.optional(),
}).strict();
const StudioAnalysisMigrationSchema = z.object({
  schemaVersion: z.literal(1),
  kind: z.literal("legacy-sessionless-source-session-repair"),
  jobId: z.string().min(1),
  previousSourceAssetSessionId: z.string().min(1),
  previousStatus: z.literal("cancelled"),
  previousCancellationReason: z.literal("studio-analysis-source-changed"),
  migratedAt: DateTimeSchema,
}).strict();

const StudioAnalysisJobRecordSchema = z.object({
  schemaVersion: z.literal(1),
  id: z.string().min(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  actorId: z.string().min(1),
  sourceAssetId: z.string().min(1),
  sourceAssetSessionId: z.string().min(1).nullable().optional(),
  sourceDigest: DigestSchema,
  sourcePath: z.string().min(1),
  sourceStartMs: z.number().int().nonnegative(),
  sourceEndMs: z.number().int().positive(),
  sourceDurationMs: z.number().int().positive(),
  sourceAuthorization: z.enum(["output-use-authorized", "reference-view-authorized"])
    .default("output-use-authorized"),
  layers: z.array(StudioAnalysisLayerSchema).min(1),
  operationKey: StudioOperationKeySchema,
  requestDigest: DigestSchema,
  status: StudioAnalysisStatusSchema,
  attempt: z.number().int().nonnegative().max(10_000),
  receiptJobIds: z.array(z.string().min(1)),
  runnerInstanceId: z.string().min(1).nullable(),
  failure: StudioAnalysisFailureSchema.nullable(),
  cancellationReason: z.string().min(1).max(1_000).nullable(),
  createdAt: DateTimeSchema,
  updatedAt: DateTimeSchema,
}).strict().superRefine((value, ctx) => {
  if (new Set(value.layers).size !== value.layers.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["layers"], message: "duplicate-analysis-layer" });
  }
  if (new Set(value.receiptJobIds).size !== value.receiptJobIds.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["receiptJobIds"], message: "duplicate-analysis-receipt" });
  }
  if (value.sourceEndMs <= value.sourceStartMs || value.sourceEndMs > value.sourceDurationMs) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceEndMs"], message: "analysis-source-interval-invalid" });
  }
  if (value.status === "cancelled" && value.cancellationReason === null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["cancellationReason"], message: "analysis-cancellation-reason-required" });
  }
  if (value.status !== "cancelled" && value.cancellationReason !== null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["cancellationReason"], message: "analysis-cancellation-reason-not-allowed" });
  }
  const failed = value.status === "failed-retryable" || value.status === "failed-manual-review";
  if (failed !== (value.failure !== null)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["failure"], message: "analysis-failure-state-invalid" });
  }
  if (!["queued", "running"].includes(value.status) && value.runnerInstanceId !== null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["runnerInstanceId"], message: "analysis-runner-state-invalid" });
  }
  if (["queued", "running"].includes(value.status) && value.runnerInstanceId === null) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["runnerInstanceId"], message: "analysis-runner-required" });
  }
});

const StudioAnalysisStateSchema = z.object({
  schemaVersion: z.literal(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  jobs: z.array(StudioAnalysisJobRecordSchema),
  receipts: z.array(ModelAnalysisReceiptV1Schema),
  migrations: z.array(StudioAnalysisMigrationSchema).default([]),
}).strict().superRefine((value, ctx) => {
  if (new Set(value.jobs.map((job) => job.id)).size !== value.jobs.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["jobs"], message: "duplicate-analysis-job" });
  }
  if (new Set(value.receipts.map((receipt) => receipt.jobId)).size !== value.receipts.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["receipts"], message: "duplicate-analysis-receipt" });
  }
  const receiptsById = new Map(value.receipts.map((receipt) => [receipt.jobId, receipt]));
  const jobsById = new Map(value.jobs.map((job) => [job.id, job]));
  const receiptOwners = new Map<string, number>();
  for (const [index, job] of value.jobs.entries()) {
    if (job.customerId !== value.customerId || job.sessionId !== value.sessionId) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["jobs", index], message: "analysis-state-scope-mismatch" });
    }
    const expectedJobId = analysisJobId(job.customerId, job.sessionId, job.actorId, job.operationKey);
    if (job.id !== expectedJobId) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["jobs", index, "id"], message: "analysis-job-id-binding-mismatch" });
    }
    const expectedReceipts = expectedReceiptBindings(job);
    for (const [receiptIndex, receiptId] of job.receiptJobIds.entries()) {
      receiptOwners.set(receiptId, (receiptOwners.get(receiptId) ?? 0) + 1);
      const receipt = receiptsById.get(receiptId);
      if (!receipt) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["jobs", index, "receiptJobIds", receiptIndex],
          message: "analysis-receipt-not-found",
        });
        continue;
      }
      const expected = expectedReceipts.get(receiptId);
      if (!expected || !receiptMatchesParent(receipt, job, expected.layer)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["jobs", index, "receiptJobIds", receiptIndex],
          message: "analysis-receipt-parent-binding-mismatch",
        });
      }
    }
    if (job.status === "succeeded") {
      const expectedFinalIds = job.layers.map((layer, layerIndex) =>
        modelReceiptId(job.id, job.attempt, layerIndex, layer));
      const finalIds = job.receiptJobIds.slice(-job.layers.length);
      const finalReceipts = finalIds.map((receiptId) => receiptsById.get(receiptId));
      const finalAttemptComplete = job.attempt > 0
        && finalIds.length === job.layers.length
        && finalIds.every((receiptId, receiptIndex) => receiptId === expectedFinalIds[receiptIndex])
        && finalReceipts.every((receipt) => receipt !== undefined);
      if (!finalAttemptComplete) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["jobs", index, "receiptJobIds"],
          message: "analysis-final-attempt-incomplete",
        });
      } else if (isCanonicalReferenceDegradationJob(job)) {
        if (finalReceipts[0]!.status !== "succeeded") {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["jobs", index, "receiptJobIds"],
            message: "analysis-required-scene-not-succeeded",
          });
        }
      } else if (finalReceipts.some((receipt) => receipt!.status !== "succeeded")) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["jobs", index, "receiptJobIds"],
          message: "analysis-noncanonical-success-with-failure-receipt",
        });
      }
    }
  }
  for (const [index, receipt] of value.receipts.entries()) {
    if (receipt.customerId !== value.customerId || receipt.sessionId !== value.sessionId) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["receipts", index], message: "analysis-state-scope-mismatch" });
    }
    if (receiptOwners.get(receipt.jobId) !== 1) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["receipts", index], message: "analysis-receipt-owner-invalid" });
    }
  }
  const migrationKeys = new Set<string>();
  for (const [index, migration] of value.migrations.entries()) {
    const key = `${migration.kind}\0${migration.jobId}`;
    if (migrationKeys.has(key)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["migrations", index], message: "duplicate-analysis-migration" });
    }
    migrationKeys.add(key);
    const job = jobsById.get(migration.jobId);
    if (
      !job
      || job.sourceAuthorization !== "reference-view-authorized"
      || job.sourceAssetSessionId !== null
      || migration.previousSourceAssetSessionId !== job.sessionId
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["migrations", index], message: "analysis-migration-binding-invalid" });
    }
  }
});

export type StudioAnalysisJobRecord = z.infer<typeof StudioAnalysisJobRecordSchema>;
type StudioAnalysisState = z.infer<typeof StudioAnalysisStateSchema>;
type StudioAnalysisFailure = z.infer<typeof StudioAnalysisFailureSchema>;

export type StudioAnalysisHistoryEntry = Readonly<{
  job: StudioAnalysisJobRecord;
  receipts: readonly ModelAnalysisReceiptV1[];
}>;

export type StudioSourceInterval = Readonly<{
  sourceStartMs: number;
  sourceEndMs: number;
  sourceDurationMs: number;
}>;

export type StudioSourceIntervalResolver = (
  input: Readonly<{
    scope: StudioScope;
    asset: StudioAnalysisAssetSnapshot["asset"];
    sourcePath: string;
  }>,
) => StudioSourceInterval | null | Promise<StudioSourceInterval | null>;

export type StudioAnalysisJobServiceOptions = Readonly<{
  now?: () => string;
  resolveSourceInterval?: StudioSourceIntervalResolver;
  resolveWorkerContext?: (input: Readonly<{
    scope: StudioScope;
    job: ModelAnalysisJobV1;
    priorReceipts: readonly ModelAnalysisReceiptV1[];
  }>) => StudioTrustedWorkerContext | undefined | Promise<StudioTrustedWorkerContext | undefined>;
}>;

const execFileAsync = promisify(execFile);
const CanonicalDurationSecondsString = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

function durationMilliseconds(value: unknown): number {
  let durationSeconds: number;
  if (typeof value === "number") {
    durationSeconds = value;
  } else if (typeof value === "string" && CanonicalDurationSecondsString.test(value)) {
    durationSeconds = Number(value);
  } else {
    throw new Error("studio-analysis-ffprobe-duration-invalid");
  }
  const durationMs = Math.floor(durationSeconds * 1_000 + 0.5);
  if (
    !Number.isFinite(durationSeconds)
    || durationSeconds <= 0
    || !Number.isSafeInteger(durationMs)
    || durationMs <= 0
  ) {
    throw new Error("studio-analysis-ffprobe-duration-invalid");
  }
  return durationMs;
}

/** Builds the trusted full-source interval used before any analysis job is persisted. */
export function createFfprobeStudioSourceIntervalResolver(
  ffprobePath: string,
): StudioSourceIntervalResolver {
  if (typeof ffprobePath !== "string" || !isAbsolute(ffprobePath)) {
    throw new Error("studio-analysis-ffprobe-path-invalid");
  }
  return async ({ sourcePath }) => {
    let stdout: string;
    try {
      const result = await execFileAsync(ffprobePath, [
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        sourcePath,
      ], {
        encoding: "utf8",
        timeout: 30_000,
        killSignal: "SIGKILL",
        maxBuffer: 64 * 1024,
      });
      stdout = result.stdout;
    } catch {
      throw new Error("studio-analysis-ffprobe-failed");
    }
    let parsed: { format?: { duration?: unknown } };
    try {
      parsed = JSON.parse(stdout) as { format?: { duration?: unknown } };
    } catch {
      throw new Error("studio-analysis-ffprobe-output-invalid");
    }
    let sourceDurationMs: number;
    try {
      sourceDurationMs = durationMilliseconds(parsed.format?.duration);
    } catch {
      throw new Error("studio-analysis-ffprobe-duration-invalid");
    }
    return { sourceStartMs: 0, sourceEndMs: sourceDurationMs, sourceDurationMs };
  };
}

type ActiveRun = {
  actorId: string;
  assetId: string;
  controller: AbortController;
  cancellationReason: string | null;
};

const activeRuns = new Map<string, Map<string, ActiveRun>>();

const transitions: Readonly<Record<StudioAnalysisStatus, readonly StudioAnalysisStatus[]>> = {
  queued: ["running", "cancelled"],
  running: ["succeeded", "failed-retryable", "failed-manual-review", "cancelled"],
  "failed-retryable": ["running", "cancelled"],
  succeeded: [],
  "failed-manual-review": [],
  cancelled: [],
};

/** Durable, customer/session-scoped orchestration around an isolated model worker. */
export class StudioAnalysisJobService {
  private static readonly stateQueues = new Map<string, Promise<void>>();
  private static readonly executionQueues = new Map<string, Promise<void>>();
  private static readonly inFlightJobs = new Map<string, Promise<StudioAnalysisHistoryEntry>>();
  private static readonly retryClaims = new Map<string, Promise<StudioAnalysisHistoryEntry>>();
  private readonly now: () => string;
  private readonly resolveSourceInterval: StudioSourceIntervalResolver;
  private readonly resolveWorkerContext: NonNullable<StudioAnalysisJobServiceOptions["resolveWorkerContext"]>;
  private readonly runnerInstanceId = randomUUID();

  constructor(
    private readonly library: CustomerMaterialLibrary,
    private readonly worker: StudioModelWorker,
    options: StudioAnalysisJobServiceOptions = {},
  ) {
    this.now = options.now ?? (() => new Date().toISOString());
    this.resolveSourceInterval = options.resolveSourceInterval ?? (() => null);
    this.resolveWorkerContext = options.resolveWorkerContext ?? (() => undefined);
  }

  async enqueue(
    scope: StudioScope,
    assetId: string,
    layersInput: readonly StudioAnalysisLayer[],
    operationKeyInput: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    assertScope(scope);
    assertId(assetId, "studio-analysis-asset-id-required");
    let source: StudioAnalysisAssetSnapshot;
    try {
      source = await this.library.inspectAnalysisAsset(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        assetId,
      );
    } catch (error) {
      throw analysisSourceError(error);
    }
    return this.enqueueAuthorized(
      scope, source, "output-use-authorized", layersInput, operationKeyInput,
    );
  }

  async enqueueReference(
    scope: StudioScope,
    suppliedSource: ReferenceAnalysisAssetSnapshot,
    layersInput: readonly StudioAnalysisLayer[],
    operationKeyInput: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    assertScope(scope);
    if (
      !suppliedSource
      || suppliedSource.authorization !== "reference-view-authorized"
      || suppliedSource.asset.customerId !== scope.customerId
    ) throw new Error("studio-analysis-reference-source-binding-mismatch");
    let source: ReferenceAnalysisAssetSnapshot;
    try {
      source = await this.library.inspectReferenceAnalysisAsset(
        scope.actorId, scope.customerId, scope.sessionId, suppliedSource.asset.id,
      );
    } catch (error) {
      throw analysisSourceError(error);
    }
    if (
      source.asset.digest !== suppliedSource.asset.digest
      || source.sourcePath !== suppliedSource.sourcePath
    ) throw new Error("studio-analysis-reference-source-binding-mismatch");
    return this.enqueueAuthorized(
      scope, source, "reference-view-authorized", layersInput, operationKeyInput,
    );
  }

  private async enqueueAuthorized(
    scope: StudioScope,
    source: StudioAnalysisAssetSnapshot | ReferenceAnalysisAssetSnapshot,
    sourceAuthorization: "output-use-authorized" | "reference-view-authorized",
    layersInput: readonly StudioAnalysisLayer[],
    operationKeyInput: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    const assetId = source.asset.id;
    const layers = parseLayers(layersInput);
    const operationKey = StudioOperationKeySchema.parse(operationKeyInput);
    const requestDigest = digest(JSON.stringify(sourceAuthorization === "output-use-authorized"
      ? { actorId: scope.actorId, assetId, layers }
      : { actorId: scope.actorId, assetId, layers, sourceAuthorization }));
    const coordinationKey = this.library.studioAnalysisCoordinationKey(scope.customerId, scope.sessionId);

    const reservation = await this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const existing = state.jobs.find((job) =>
        job.actorId === scope.actorId && job.operationKey === operationKey);
      if (existing) {
        if (existing.requestDigest !== requestDigest) {
          throw new Error("studio-analysis-operation-key-conflict");
        }
        if (isLegacySessionlessSourceSessionCancellation(
          existing,
          state,
          scope,
          source,
          sourceAuthorization,
          layers,
        )) {
          const timestamp = this.timestamp();
          // 6d0ff5c replaced a real null source session with the workflow session.
          // This is the sole terminal-to-queued repair path and remains auditable.
          const repaired = StudioAnalysisJobRecordSchema.parse({
            ...existing,
            sourceAssetSessionId: null,
            status: "queued",
            runnerInstanceId: this.runnerInstanceId,
            failure: null,
            cancellationReason: null,
            updatedAt: timestamp,
          });
          const migration = StudioAnalysisMigrationSchema.parse({
            schemaVersion: 1,
            kind: "legacy-sessionless-source-session-repair",
            jobId: existing.id,
            previousSourceAssetSessionId: existing.sourceAssetSessionId,
            previousStatus: existing.status,
            previousCancellationReason: existing.cancellationReason,
            migratedAt: timestamp,
          });
          const index = state.jobs.findIndex((job) => job.id === existing.id);
          const next = StudioAnalysisStateSchema.parse({
            ...state,
            jobs: replaceAt(state.jobs, index, repaired),
            migrations: [...state.migrations, migration],
          });
          try {
            await this.library.writeStudioReferenceAnalysisStateIfCurrent(
              bindingFor(repaired),
              next,
              () => true,
            );
          } catch (error) {
            throw analysisSourceError(error);
          }
          return { created: true as const, entry: entryFor(repaired, next) };
        }
        return { created: false as const, entry: entryFor(existing, state) };
      }

      let resolvedInterval: StudioSourceInterval | null;
      try {
        resolvedInterval = await this.resolveSourceInterval({
          scope: clone(scope),
          asset: clone(source.asset),
          sourcePath: source.sourcePath,
        });
      } catch {
        throw new Error("studio-analysis-source-interval-unavailable");
      }
      const interval = parseSourceInterval(resolvedInterval);

      const timestamp = this.timestamp();
      const job = StudioAnalysisJobRecordSchema.parse({
        schemaVersion: 1,
        id: analysisJobId(scope.customerId, scope.sessionId, scope.actorId, operationKey),
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        actorId: scope.actorId,
        sourceAssetId: assetId,
        sourceAssetSessionId: source.asset.sessionId,
        sourceDigest: source.asset.digest,
        sourcePath: source.sourcePath,
        ...interval,
        sourceAuthorization,
        layers,
        operationKey,
        requestDigest,
        status: "queued",
        attempt: 0,
        receiptJobIds: [],
        runnerInstanceId: this.runnerInstanceId,
        failure: null,
        cancellationReason: null,
        createdAt: timestamp,
        updatedAt: timestamp,
      });
      const next = StudioAnalysisStateSchema.parse({ ...state, jobs: [...state.jobs, job] });
      await this.persistState(scope, next);
      return { created: true as const, entry: entryFor(job, next) };
    });

    const jobKey = `${coordinationKey}\0${reservation.entry.job.id}`;
    const inFlight = StudioAnalysisJobService.inFlightJobs.get(jobKey);
    if (!reservation.created) return inFlight ? clone(await inFlight) : clone(reservation.entry);

    const execution = this.enqueueExecution(
      coordinationKey,
      () => this.execute(scope, reservation.entry.job.id, coordinationKey),
    );
    StudioAnalysisJobService.inFlightJobs.set(jobKey, execution);
    try {
      return clone(await execution);
    } finally {
      if (StudioAnalysisJobService.inFlightJobs.get(jobKey) === execution) {
        StudioAnalysisJobService.inFlightJobs.delete(jobKey);
      }
    }
  }

  async cancelForAsset(
    scope: StudioScope,
    assetId: string,
    reasonInput: string,
  ): Promise<StudioAnalysisHistoryEntry[]> {
    assertScope(scope);
    assertId(assetId, "studio-analysis-asset-id-required");
    const reason = normalizeReason(reasonInput);
    const coordinationKey = this.library.studioAnalysisCoordinationKey(scope.customerId, scope.sessionId);
    const active = activeRuns.get(coordinationKey);
    for (const run of active?.values() ?? []) {
      if (run.actorId !== scope.actorId || run.assetId !== assetId) continue;
      run.cancellationReason = reason;
      run.controller.abort(new Error(reason));
    }

    return this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const cancelledIds = new Set<string>();
      const jobs = state.jobs.map((job) => {
        if (
          job.actorId !== scope.actorId
          || job.sourceAssetId !== assetId
          || !transitions[job.status].includes("cancelled")
        ) return job;
        cancelledIds.add(job.id);
        return transition(job, "cancelled", this.timestamp(), reason);
      });
      const next = StudioAnalysisStateSchema.parse({ ...state, jobs });
      if (cancelledIds.size > 0) await this.persistState(scope, next);
      return jobs
        .filter((job) => cancelledIds.has(job.id))
        .map((job) => entryFor(job, next));
    });
  }

  async retry(scope: StudioScope, jobId: string): Promise<StudioAnalysisHistoryEntry> {
    assertScope(scope);
    assertId(jobId, "studio-analysis-job-id-required");
    const coordinationKey = this.library.studioAnalysisCoordinationKey(scope.customerId, scope.sessionId);
    const expectedAttempt = await this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const job = state.jobs.find((candidate) => candidate.id === jobId);
      if (!job || job.actorId !== scope.actorId) throw new Error("studio-analysis-job-not-found");
      if (job.status !== "failed-retryable") throw new Error("studio-analysis-retry-not-allowed");
      return job.attempt;
    });
    return this.retryIfCurrent(scope, jobId, expectedAttempt);
  }

  async retryIfCurrent(
    scope: StudioScope,
    jobId: string,
    expectedAttempt: number,
  ): Promise<StudioAnalysisHistoryEntry> {
    assertScope(scope);
    assertId(jobId, "studio-analysis-job-id-required");
    if (!Number.isSafeInteger(expectedAttempt) || expectedAttempt < 1) {
      throw new Error("studio-analysis-retry-attempt-invalid");
    }
    const coordinationKey = this.library.studioAnalysisCoordinationKey(scope.customerId, scope.sessionId);
    const claimKey = `${coordinationKey}\0${jobId}\0${expectedAttempt}`;
    let resolveClaim!: (entry: StudioAnalysisHistoryEntry) => void;
    let rejectClaim!: (error: unknown) => void;
    const claim = new Promise<StudioAnalysisHistoryEntry>((resolve, reject) => {
      resolveClaim = resolve;
      rejectClaim = reject;
    });
    void claim.catch(() => undefined);
    const decision = await this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const job = state.jobs.find((candidate) => candidate.id === jobId);
      if (!job || job.actorId !== scope.actorId) throw new Error("studio-analysis-job-not-found");
      if (job.status !== "failed-retryable" || job.attempt !== expectedAttempt) {
        return { kind: "current" as const, entry: entryFor(job, state) };
      }
      const existing = StudioAnalysisJobService.retryClaims.get(claimKey);
      if (existing) return { kind: "join" as const, promise: existing };
      StudioAnalysisJobService.retryClaims.set(claimKey, claim);
      return { kind: "owner" as const };
    });
    if (decision.kind === "current") return clone(decision.entry);
    if (decision.kind === "join") return clone(await decision.promise);
    try {
      const result = await this.runClaimedRetry(scope, jobId, coordinationKey);
      resolveClaim(result);
      return clone(result);
    } catch (error) {
      rejectClaim(error);
      throw error;
    } finally {
      if (StudioAnalysisJobService.retryClaims.get(claimKey) === claim) {
        StudioAnalysisJobService.retryClaims.delete(claimKey);
      }
    }
  }

  private async runClaimedRetry(
    scope: StudioScope,
    jobId: string,
    coordinationKey: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    const jobKey = `${coordinationKey}\0${jobId}`;
    const existing = StudioAnalysisJobService.inFlightJobs.get(jobKey);
    if (existing) return clone(await existing);
    const execution = this.enqueueExecution(coordinationKey, () => this.execute(scope, jobId, coordinationKey));
    StudioAnalysisJobService.inFlightJobs.set(jobKey, execution);
    try {
      return clone(await execution);
    } finally {
      if (StudioAnalysisJobService.inFlightJobs.get(jobKey) === execution) {
        StudioAnalysisJobService.inFlightJobs.delete(jobKey);
      }
    }
  }

  async history(scope: StudioScope): Promise<StudioAnalysisHistoryEntry[]> {
    assertScope(scope);
    const coordinationKey = this.library.studioAnalysisCoordinationKey(scope.customerId, scope.sessionId);
    return this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      return state.jobs
        .filter((job) => job.actorId === scope.actorId)
        .slice()
        .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id))
        .map((job) => entryFor(job, state));
    });
  }

  private async execute(
    scope: StudioScope,
    jobId: string,
    coordinationKey: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    const started = await this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const index = state.jobs.findIndex((job) => job.id === jobId);
      if (index < 0) throw new Error("studio-analysis-job-not-found");
      const current = state.jobs[index]!;
      if (current.status === "cancelled") return { runnable: false as const, entry: entryFor(current, state) };
      if (!transitions[current.status].includes("running")) {
        throw new Error("studio-analysis-illegal-transition");
      }
      const running = StudioAnalysisJobRecordSchema.parse({
        ...transition(current, "running", this.timestamp(), null, null, this.runnerInstanceId),
        attempt: current.attempt + 1,
        runnerInstanceId: this.runnerInstanceId,
      });
      const jobs = replaceAt(state.jobs, index, running);
      const next = StudioAnalysisStateSchema.parse({ ...state, jobs });
      await this.persistState(scope, next);
      return { runnable: true as const, job: running };
    });
    if (!started.runnable) return clone(started.entry);

    try {
      await this.requireCurrentSource(scope, started.job);
    } catch (error) {
      await this.cancelJob(scope, started.job.id, coordinationKey, errorMessage(error));
      throw error;
    }

    const controller = new AbortController();
    const active = activeRunsFor(coordinationKey);
    const activeRun: ActiveRun = {
      actorId: started.job.actorId,
      assetId: started.job.sourceAssetId,
      controller,
      cancellationReason: null,
    };
    active.set(started.job.id, activeRun);
    const attemptReceipts: ModelAnalysisReceiptV1[] = [];
    let terminalStatus: Extract<StudioAnalysisStatus, "succeeded" | "failed-retryable" | "failed-manual-review"> = "succeeded";
    let terminalStageTelemetry: StudioWorkerStageTelemetrySummaryV1 | null = null;
    try {
      for (const [index, layer] of started.job.layers.entries()) {
        const modelJob: ModelAnalysisJobV1 = {
          schemaVersion: 1,
          jobId: modelReceiptId(started.job.id, started.job.attempt, index, layer),
          customerId: started.job.customerId,
          sessionId: started.job.sessionId,
          sourceAssetId: started.job.sourceAssetId,
          sourceDigest: started.job.sourceDigest,
          sourcePath: started.job.sourcePath,
          sourceStartMs: started.job.sourceStartMs,
          sourceEndMs: started.job.sourceEndMs,
          layer,
          operationKey: started.job.operationKey,
          requestedAt: this.timestamp(),
        };
        const trustedContext = await this.resolveWorkerContext({
          scope: clone(scope),
          job: clone(modelJob),
          priorReceipts: clone(attemptReceipts),
        });
        const handoff = await this.withStateLock(coordinationKey, async () => {
          const state = await this.loadState(scope);
          const current = state.jobs.find((job) => job.id === started.job.id);
          if (!current) throw new Error("studio-analysis-job-not-found");
          if (current.status === "cancelled") {
            activeRun.cancellationReason = current.cancellationReason ?? "studio-analysis-cancelled";
            controller.abort(new Error(activeRun.cancellationReason));
            throw new Error(activeRun.cancellationReason);
          }
          if (current.status !== "running") throw new Error("studio-analysis-illegal-transition");
          const start = () => trustedContext === undefined
            ? this.worker.run(modelJob, controller.signal)
            : this.worker.run(modelJob, controller.signal, trustedContext);
          return started.job.sourceAuthorization === "reference-view-authorized"
            ? this.library.startReferenceAnalysisRun(bindingFor(started.job), start)
            : this.library.startStudioAnalysisRun(bindingFor(started.job), start);
        });
        let rawReceipt: ModelAnalysisReceiptV1;
        try {
          rawReceipt = await handoff.result;
        } catch (error) {
          if (!(error instanceof StudioModelWorkerFailureError)) throw error;
          rawReceipt = error.receipt;
          terminalStageTelemetry = error.stageTelemetry;
        }
        const receipt = ModelAnalysisReceiptV1Schema.parse(rawReceipt);
        assertReceiptMatchesJob(modelJob, receipt);
        attemptReceipts.push(receipt);
        if (receipt.status !== "succeeded") {
          if (isCanonicalReferenceDegradationJob(started.job) && layer !== "scene") continue;
          terminalStatus = receipt.status;
          break;
        }
      }

      return await this.withStateLock(coordinationKey, async () => {
        const state = await this.loadState(scope);
        const index = state.jobs.findIndex((job) => job.id === started.job.id);
        if (index < 0) throw new Error("studio-analysis-job-not-found");
        const current = state.jobs[index]!;
        if (current.status === "cancelled" || activeRun.cancellationReason !== null) {
          return entryFor(current.status === "cancelled"
            ? current
            : transition(current, "cancelled", this.timestamp(), activeRun.cancellationReason!), state);
        }
        if (current.status !== "running") throw new Error("studio-analysis-illegal-transition");
        const completed = transition(
          current,
          terminalStatus,
          this.timestamp(),
          null,
          terminalStatus === "succeeded"
            ? null
            : failureForReceipt(terminalStatus, terminalStageTelemetry),
        );
        const receipts = [...state.receipts, ...attemptReceipts];
        const job = StudioAnalysisJobRecordSchema.parse({
          ...completed,
          receiptJobIds: [...current.receiptJobIds, ...attemptReceipts.map((receipt) => receipt.jobId)],
        });
        const next = StudioAnalysisStateSchema.parse({
          ...state,
          jobs: replaceAt(state.jobs, index, job),
          receipts,
        });
        const canCommit = () => activeRun.cancellationReason === null && !controller.signal.aborted;
        if (started.job.sourceAuthorization === "reference-view-authorized") {
          await this.library.writeStudioReferenceAnalysisStateIfCurrent(
            bindingFor(started.job), next, canCommit,
          );
        } else {
          await this.library.writeStudioAnalysisStateIfCurrent(
            bindingFor(started.job), next, canCommit,
          );
        }
        return entryFor(job, next);
      });
    } catch (error) {
      if (activeRun.cancellationReason !== null || controller.signal.aborted) {
        return this.cancelJob(
          scope,
          started.job.id,
          coordinationKey,
          activeRun.cancellationReason ?? "studio-analysis-cancelled",
        );
      }
      const sourceError = normalizePostRunSourceError(error);
      if (sourceError) {
        return this.cancelJob(scope, started.job.id, coordinationKey, sourceError.message);
      }
      const failedStatus = isRetryableWorkerError(error) ? "failed-retryable" : "failed-manual-review";
      return this.finishWithoutReceipt(
        scope,
        started.job.id,
        coordinationKey,
        failedStatus,
        failureForWorkerError(error, failedStatus),
      );
    } finally {
      active.delete(started.job.id);
      if (active.size === 0 && activeRuns.get(coordinationKey) === active) {
        activeRuns.delete(coordinationKey);
      }
    }
  }

  private async requireCurrentSource(
    scope: StudioScope,
    job: StudioAnalysisJobRecord,
  ): Promise<StudioAnalysisAssetSnapshot | ReferenceAnalysisAssetSnapshot> {
    let current: StudioAnalysisAssetSnapshot | ReferenceAnalysisAssetSnapshot;
    try {
      current = job.sourceAuthorization === "reference-view-authorized"
        ? await this.library.inspectReferenceAnalysisAsset(
            scope.actorId, scope.customerId, scope.sessionId, job.sourceAssetId,
          )
        : await this.library.inspectAnalysisAsset(
            scope.actorId, scope.customerId, scope.sessionId, job.sourceAssetId,
          );
    } catch (error) {
      throw analysisSourceError(error);
    }
    if (
      current.asset.customerId !== job.customerId
      || current.asset.sessionId !== (job.sourceAssetSessionId === undefined
        ? job.sessionId
        : job.sourceAssetSessionId)
      || current.asset.digest !== job.sourceDigest
      || current.sourcePath !== job.sourcePath
    ) {
      throw new Error("studio-analysis-source-changed");
    }
    return current;
  }

  private async cancelJob(
    scope: StudioScope,
    jobId: string,
    coordinationKey: string,
    reason: string,
  ): Promise<StudioAnalysisHistoryEntry> {
    return this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const index = state.jobs.findIndex((job) => job.id === jobId);
      if (index < 0) throw new Error("studio-analysis-job-not-found");
      const current = state.jobs[index]!;
      if (current.status === "cancelled") return entryFor(current, state);
      const cancelled = transition(current, "cancelled", this.timestamp(), normalizeReason(reason));
      const next = StudioAnalysisStateSchema.parse({ ...state, jobs: replaceAt(state.jobs, index, cancelled) });
      await this.persistState(scope, next);
      return entryFor(cancelled, next);
    });
  }

  private async finishWithoutReceipt(
    scope: StudioScope,
    jobId: string,
    coordinationKey: string,
    status: "failed-retryable" | "failed-manual-review",
    failure: StudioAnalysisFailure,
  ): Promise<StudioAnalysisHistoryEntry> {
    return this.withStateLock(coordinationKey, async () => {
      const state = await this.loadState(scope);
      const index = state.jobs.findIndex((job) => job.id === jobId);
      if (index < 0) throw new Error("studio-analysis-job-not-found");
      const current = state.jobs[index]!;
      if (current.status === "cancelled") return entryFor(current, state);
      const failed = transition(current, status, this.timestamp(), null, failure);
      const next = StudioAnalysisStateSchema.parse({ ...state, jobs: replaceAt(state.jobs, index, failed) });
      await this.persistState(scope, next);
      return entryFor(failed, next);
    });
  }

  private async loadState(scope: StudioScope): Promise<StudioAnalysisState> {
    const raw = await this.library.readStudioAnalysisState(scope.customerId, scope.sessionId);
    try {
      let state = StudioAnalysisStateSchema.parse(raw ?? emptyState(scope));
      if (state.customerId !== scope.customerId || state.sessionId !== scope.sessionId) {
        throw new Error("studio-analysis-state-scope-mismatch");
      }
      const interrupted = state.jobs.some((job) =>
        ["queued", "running"].includes(job.status) && job.runnerInstanceId !== this.runnerInstanceId);
      if (interrupted) {
        const jobs = state.jobs.map((job) => {
          if (!["queued", "running"].includes(job.status) || job.runnerInstanceId === this.runnerInstanceId) return job;
          return StudioAnalysisJobRecordSchema.parse({
            ...job,
            status: "failed-retryable",
            runnerInstanceId: null,
            failure: { code: "studio-analysis-interrupted", message: null },
            cancellationReason: null,
            updatedAt: this.timestamp(),
          });
        });
        state = StudioAnalysisStateSchema.parse({ ...state, jobs });
        await this.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, state);
      }
      return state;
    } catch (error) {
      if (error instanceof Error && error.message === "studio-analysis-state-scope-mismatch") throw error;
      throw new Error("studio-analysis-state-invalid");
    }
  }

  private async persistState(scope: StudioScope, state: StudioAnalysisState): Promise<void> {
    await this.library.writeStudioAnalysisState(
      scope.customerId,
      scope.sessionId,
      StudioAnalysisStateSchema.parse(state),
    );
  }

  private withStateLock<T>(coordinationKey: string, work: () => Promise<T>): Promise<T> {
    return enqueueLock(StudioAnalysisJobService.stateQueues, coordinationKey, work);
  }

  private enqueueExecution<T>(coordinationKey: string, work: () => Promise<T>): Promise<T> {
    return enqueueLock(StudioAnalysisJobService.executionQueues, coordinationKey, work);
  }

  private timestamp(): string {
    const timestamp = this.now();
    if (!Number.isFinite(Date.parse(timestamp))) throw new Error("studio-analysis-timestamp-invalid");
    return timestamp;
  }
}

function emptyState(scope: StudioScope): StudioAnalysisState {
  return {
    schemaVersion: 1,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    jobs: [],
    receipts: [],
    migrations: [],
  };
}

function entryFor(job: StudioAnalysisJobRecord, state: StudioAnalysisState): StudioAnalysisHistoryEntry {
  const receiptsById = new Map(state.receipts.map((receipt) => [receipt.jobId, receipt]));
  return clone({
    job,
    receipts: job.receiptJobIds.map((id) => {
      const receipt = receiptsById.get(id);
      if (!receipt) throw new Error("studio-analysis-state-invalid");
      return receipt;
    }),
  });
}

function transition(
  job: StudioAnalysisJobRecord,
  status: StudioAnalysisStatus,
  updatedAt: string,
  cancellationReason: string | null = null,
  failure: StudioAnalysisFailure | null = null,
  runnerInstanceId: string | null = null,
): StudioAnalysisJobRecord {
  if (!transitions[job.status].includes(status)) throw new Error("studio-analysis-illegal-transition");
  return StudioAnalysisJobRecordSchema.parse({
    ...job,
    status,
    updatedAt,
    cancellationReason,
    failure,
    runnerInstanceId: ["queued", "running"].includes(status)
      ? (runnerInstanceId ?? job.runnerInstanceId)
      : null,
  });
}

function bindingFor(job: StudioAnalysisJobRecord) {
  return {
    actorId: job.actorId,
    customerId: job.customerId,
    sessionId: job.sessionId,
    sourceAssetSessionId: job.sourceAssetSessionId === undefined
      ? job.sessionId
      : job.sourceAssetSessionId,
    assetId: job.sourceAssetId,
    sourceDigest: job.sourceDigest,
    sourcePath: job.sourcePath,
  };
}

function analysisJobId(
  customerId: string,
  sessionId: string,
  actorId: string,
  operationKey: string,
): string {
  return `analysis_${digest(`${customerId}\0${sessionId}\0${actorId}\0${operationKey}`)}`;
}

function modelReceiptId(
  parentJobId: string,
  attempt: number,
  layerIndex: number,
  layer: StudioAnalysisLayer,
): string {
  return `model_${digest(`${parentJobId}\0${attempt}\0${layerIndex}\0${layer}`)}`;
}

function expectedReceiptBindings(
  job: StudioAnalysisJobRecord,
): Map<string, { layer: StudioAnalysisLayer }> {
  const expected = new Map<string, { layer: StudioAnalysisLayer }>();
  for (let attempt = 1; attempt <= job.attempt; attempt += 1) {
    for (const [layerIndex, layer] of job.layers.entries()) {
      expected.set(modelReceiptId(job.id, attempt, layerIndex, layer), { layer });
    }
  }
  return expected;
}

function isCanonicalReferenceDegradationJob(
  job: Pick<StudioAnalysisJobRecord, "sourceAuthorization" | "layers">,
): boolean {
  return job.sourceAuthorization === "reference-view-authorized"
    && job.layers.length === 3
    && job.layers[0] === "scene"
    && job.layers[1] === "transcript"
    && job.layers[2] === "vision";
}

export type ReferenceAnalysisReceiptBinding = Readonly<{
  customerId: string;
  sessionId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourcePath: string;
  sourceStartMs: number;
  sourceEndMs: number;
}>;

export type FinalReferenceAnalysisAttempt = Readonly<{
  allReceipts: readonly ModelAnalysisReceiptV1[];
  evidenceReceipts: readonly ModelAnalysisReceiptV1[];
}>;

/** Selects and validates the exact final canonical reference-analysis attempt. */
export function selectFinalReferenceAnalysisAttempt(
  entry: StudioAnalysisHistoryEntry,
  binding: ReferenceAnalysisReceiptBinding,
): FinalReferenceAnalysisAttempt {
  if (entry.job.status === "failed-retryable") {
    throw new Error("reference-analysis-temporarily-unavailable");
  }
  if (entry.job.status === "failed-manual-review") {
    throw new Error("reference-analysis-manual-review-required");
  }
  if (entry.job.status !== "succeeded" || entry.job.attempt < 1) {
    throw new Error("reference-analysis-final-attempt-not-succeeded");
  }
  if (!isCanonicalReferenceDegradationJob(entry.job)) {
    throw new Error("reference-analysis-final-attempt-layer-invalid");
  }
  if (
    entry.job.customerId !== binding.customerId
    || entry.job.sessionId !== binding.sessionId
    || entry.job.sourceAssetId !== binding.sourceAssetId
    || entry.job.sourceDigest !== binding.sourceDigest
    || entry.job.sourcePath !== binding.sourcePath
    || entry.job.sourceStartMs !== binding.sourceStartMs
    || entry.job.sourceEndMs !== binding.sourceEndMs
  ) throw new Error("reference-receipt-binding-mismatch");
  if (
    entry.receipts.length !== entry.job.receiptJobIds.length
    || entry.receipts.some((receipt, index) => receipt.jobId !== entry.job.receiptJobIds[index])
  ) throw new Error("reference-analysis-final-attempt-layer-invalid");

  const expectedIds = entry.job.layers.map((layer, layerIndex) =>
    modelReceiptId(entry.job.id, entry.job.attempt, layerIndex, layer));
  const allReceipts = entry.receipts.slice(-entry.job.layers.length);
  if (
    allReceipts.length !== entry.job.layers.length
    || allReceipts.some((receipt, index) =>
      receipt.jobId !== expectedIds[index] || receipt.layer !== entry.job.layers[index])
  ) throw new Error("reference-analysis-final-attempt-layer-invalid");

  for (const receipt of allReceipts) {
    if (!receiptMatchesParent(receipt, entry.job, receipt.layer)) {
      throw new Error("reference-receipt-binding-mismatch");
    }
  }
  const sceneReceipt = allReceipts[0]!;
  if (sceneReceipt.status !== "succeeded") {
    throw new Error("reference-analysis-scene-not-succeeded");
  }
  const evidenceReceipts = allReceipts.filter((receipt) => receipt.status === "succeeded");
  if (evidenceReceipts.some((receipt) => receipt.synthetic || receipt.providerMode !== "local")) {
    throw new Error("reference-receipt-real-local-evidence-required");
  }
  return clone({ allReceipts, evidenceReceipts });
}

function isLegacySessionlessSourceSessionCancellation(
  job: StudioAnalysisJobRecord,
  state: StudioAnalysisState,
  scope: StudioScope,
  source: StudioAnalysisAssetSnapshot | ReferenceAnalysisAssetSnapshot,
  sourceAuthorization: "output-use-authorized" | "reference-view-authorized",
  layers: readonly StudioAnalysisLayer[],
): boolean {
  return sourceAuthorization === "reference-view-authorized"
    && source.authorization === "reference-view-authorized"
    && source.asset.customerId === scope.customerId
    && source.asset.sessionId === null
    && job.customerId === scope.customerId
    && job.sessionId === scope.sessionId
    && job.actorId === scope.actorId
    && job.sourceAuthorization === "reference-view-authorized"
    && job.sourceAssetId === source.asset.id
    && job.sourceAssetSessionId === job.sessionId
    && job.sourceAssetSessionId === scope.sessionId
    && job.sourceDigest === source.asset.digest
    && job.sourcePath === source.sourcePath
    && job.layers.length === layers.length
    && job.layers.every((layer, index) => layer === layers[index])
    && job.status === "cancelled"
    && job.attempt === 1
    && job.receiptJobIds.length === 0
    && job.runnerInstanceId === null
    && job.failure === null
    && job.cancellationReason === "studio-analysis-source-changed"
    && !state.migrations.some((migration) => migration.jobId === job.id);
}

function receiptMatchesParent(
  receipt: ModelAnalysisReceiptV1,
  job: StudioAnalysisJobRecord,
  layer: StudioAnalysisLayer,
): boolean {
  return receipt.customerId === job.customerId
    && receipt.sessionId === job.sessionId
    && receipt.sourceAssetId === job.sourceAssetId
    && receipt.sourceDigest === job.sourceDigest
    && receipt.sourcePath === job.sourcePath
    && receipt.sourceStartMs === job.sourceStartMs
    && receipt.sourceEndMs === job.sourceEndMs
    && receipt.layer === layer
    && receipt.operationKey === job.operationKey;
}

function failureForReceipt(
  status: "failed-retryable" | "failed-manual-review",
  stageTelemetry: StudioWorkerStageTelemetrySummaryV1 | null = null,
): StudioAnalysisFailure {
  return {
    code: status === "failed-retryable"
      ? "studio-analysis-worker-reported-retryable"
      : "studio-analysis-worker-reported-manual-review",
    message: null,
    ...(stageTelemetry === null ? {} : { stageTelemetry }),
  };
}

function failureForWorkerError(
  error: unknown,
  status: "failed-retryable" | "failed-manual-review",
): StudioAnalysisFailure {
  const code = errorMessage(error);
  const safeCode: Record<string, string> = {
    "studio-model-worker-timeout": "studio-analysis-worker-timeout",
    "studio-model-worker-spawn-failed": "studio-analysis-worker-spawn-failed",
    "studio-model-worker-retryable-failure": "studio-analysis-worker-retryable-failure",
    "studio-model-source-unavailable": "studio-analysis-source-unavailable",
  };
  return {
    code: safeCode[code] ?? (status === "failed-retryable"
      ? "studio-analysis-worker-retryable-failure"
      : "studio-analysis-invalid-receipt"),
    message: null,
    ...(error instanceof StudioModelWorkerTimeoutError && error.stageTelemetry !== null
      ? { stageTelemetry: error.stageTelemetry }
      : {}),
  };
}

function parseLayers(value: readonly StudioAnalysisLayer[]): StudioAnalysisLayer[] {
  const layers = z.array(StudioAnalysisLayerSchema).min(1).parse(value);
  if (new Set(layers).size !== layers.length) throw new Error("studio-analysis-duplicate-layer");
  return layers;
}

function parseSourceInterval(value: StudioSourceInterval | null): StudioSourceInterval {
  if (value === null) throw new Error("studio-analysis-source-interval-unavailable");
  const { sourceStartMs, sourceEndMs, sourceDurationMs } = value;
  if (
    !Number.isSafeInteger(sourceStartMs)
    || !Number.isSafeInteger(sourceEndMs)
    || !Number.isSafeInteger(sourceDurationMs)
    || sourceStartMs < 0
    || sourceEndMs <= sourceStartMs
    || sourceDurationMs <= 0
    || sourceEndMs > sourceDurationMs
  ) {
    throw new Error("studio-analysis-source-interval-invalid");
  }
  return { sourceStartMs, sourceEndMs, sourceDurationMs };
}

function assertScope(scope: StudioScope): void {
  assertId(scope.actorId, "actor-id-required");
  assertId(scope.customerId, "customer-id-required");
  assertId(scope.sessionId, "session-id-required");
}

function assertId(value: string, error: string): void {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
}

function normalizeReason(value: string): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > 1_000) {
    throw new Error("studio-analysis-cancellation-reason-invalid");
  }
  return value.trim();
}

function analysisSourceError(error: unknown): Error {
  const message = errorMessage(error);
  if (["asset-withdrawn", "asset-view-not-allowed", "asset-use-not-allowed", "not-authorized"].includes(message)) {
    return new Error("studio-analysis-permission-blocked");
  }
  if (message === "asset-session-mismatch") return new Error("studio-analysis-session-stale");
  if (message === "source-digest-mismatch") return new Error("studio-analysis-source-changed");
  if (message === "asset-not-found") return new Error("studio-analysis-asset-not-found");
  return new Error("studio-analysis-source-unavailable");
}

function normalizePostRunSourceError(error: unknown): Error | null {
  const message = errorMessage(error);
  if ([
    "asset-withdrawn",
    "asset-view-not-allowed",
    "asset-use-not-allowed",
    "not-authorized",
    "asset-session-mismatch",
    "source-digest-mismatch",
    "asset-not-found",
  ].includes(message)) return analysisSourceError(error);
  return [
    "studio-analysis-permission-blocked",
    "studio-analysis-session-stale",
    "studio-analysis-source-changed",
    "studio-analysis-asset-not-found",
  ].includes(message) ? new Error(message) : null;
}

function isRetryableWorkerError(error: unknown): boolean {
  return [
    "studio-model-worker-retryable-failure",
    "studio-model-worker-timeout",
    "studio-model-worker-spawn-failed",
    "studio-model-source-unavailable",
  ].includes(errorMessage(error));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "studio-analysis-unknown-error";
}

function activeRunsFor(key: string): Map<string, ActiveRun> {
  let active = activeRuns.get(key);
  if (!active) {
    active = new Map();
    activeRuns.set(key, active);
  }
  return active;
}

async function enqueueLock<T>(
  queues: Map<string, Promise<void>>,
  key: string,
  work: () => Promise<T>,
): Promise<T> {
  const previous = queues.get(key) ?? Promise.resolve();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const tail = previous.then(() => gate, () => gate);
  queues.set(key, tail);
  await previous.catch(() => undefined);
  try {
    return await work();
  } finally {
    release();
    if (queues.get(key) === tail) queues.delete(key);
  }
}

function replaceAt<T>(values: readonly T[], index: number, value: T): T[] {
  const next = [...values];
  next[index] = value;
  return next;
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}
