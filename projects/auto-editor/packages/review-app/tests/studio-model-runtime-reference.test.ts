import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ModelAnalysisJobV1, ModelAnalysisReceiptV1 } from "@wearable/contracts";
import {
  CustomerMaterialLibrary,
  type ReferenceAnalysisAssetSnapshot,
  type StudioAnalysisHistoryEntry,
  type StudioModelWorker,
  type StudioScope,
} from "@wearable/mobile-agent";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StudioModelRuntime } from "../src/studio-model-runtime";

const binding = {
  customerId: "customer-a", sessionId: "session-a", sourceAssetId: "asset-a",
  sourceDigest: "a".repeat(64), sourcePath: "/trusted/reference.mov",
  sourceStartMs: 0, sourceEndMs: 2_000,
};

const legacySessionlessStateFixture = fileURLToPath(new URL(
  "./fixtures/legacy-sessionless-analysis-state-v1.json",
  import.meta.url,
));

describe("reference analysis runtime resolution", () => {
  it("does not spend a second retry when a delayed caller observed an older failed attempt", async () => {
    const scope = { actorId: "actor-a", customerId: "customer-a", sessionId: "session-a" };
    const runtime = new StudioModelRuntime({
      scope,
      library: {} as never,
      studio: {} as never,
      worker: {} as never,
    });
    const firstEnqueueReached = deferred<void>();
    const releaseFirstEnqueue = deferred<void>();
    let enqueueCalls = 0;
    let retryChecks = 0;
    let retryAttemptsStarted = 0;
    let currentAttempt = 1;
    const operationKey = `reference-analysis-${createHash("sha256").update(JSON.stringify({
      assetId: binding.sourceAssetId,
      digest: binding.sourceDigest,
      path: binding.sourcePath,
    })).digest("hex")}`;
    const failedAttempt1 = entry([], 1, "failed-retryable", operationKey);
    const failedAttempt2 = entry([], 2, "failed-retryable", operationKey);
    const succeededAttempt3 = entry(layers(3), 3, "succeeded", operationKey);
    (runtime as any).jobs = {
      history: async () => [failedAttempt1],
      enqueueReference: async () => {
        enqueueCalls += 1;
        if (enqueueCalls === 1) {
          firstEnqueueReached.resolve();
          await releaseFirstEnqueue.promise;
          return failedAttempt2;
        }
        return failedAttempt1;
      },
      retryIfCurrent: async (_scope: unknown, _jobId: string, expectedAttempt: number) => {
        retryChecks += 1;
        if (expectedAttempt === currentAttempt) {
          retryAttemptsStarted += 1;
          currentAttempt += 1;
          return retryAttemptsStarted === 1 ? failedAttempt2 : succeededAttempt3;
        }
        return failedAttempt2;
      },
    };
    const source = {
      authorization: "reference-view-authorized",
      asset: {
        id: binding.sourceAssetId,
        customerId: scope.customerId,
        sessionId: null,
        digest: binding.sourceDigest,
      },
      sourcePath: binding.sourcePath,
    } as never;

    const delayed = runtime.produceReferenceAnalysisAttempt(source);
    await firstEnqueueReached.promise;
    const current = runtime.produceReferenceAnalysisAttempt(source);
    await expect(current).rejects.toThrow("reference-analysis-temporarily-unavailable");
    releaseFirstEnqueue.resolve();
    await expect(delayed).rejects.toThrow();

    expect(retryChecks).toBe(2);
    expect(retryAttemptsStarted).toBe(1);
  });
});

describe("legacy sessionless reference analysis recovery", () => {
  const roots: string[] = [];
  const scope: StudioScope = {
    actorId: "actor-a",
    customerId: "customer-a",
    sessionId: "session-a",
  };

  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("returns full audit receipts but only successful real-local grammar evidence", async () => {
    const root = await mkdtemp(join(tmpdir(), "degraded-reference-analysis-"));
    roots.push(root);
    const library = new CustomerMaterialLibrary(root, () => true);
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: "degraded-reference-source",
      bytes: Buffer.from("degraded-reference-source"),
      mediaKind: "video",
      fileName: "degraded-reference.mp4",
      capturedAt: null,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "review-required",
    });
    const source = await library.inspectReferenceAnalysisAsset(
      scope.actorId, scope.customerId, scope.sessionId, asset.id,
    );
    const worker = successfulReferenceWorker((job) => job.layer === "scene"
      ? receiptForWorkerJob(job)
      : {
          ...receiptForWorkerJob(job),
          status: "failed-manual-review",
          failure: { code: "studio-model-adapter-not-configured" },
          confidence: 0,
          synthetic: true,
          providerMode: "offline-fallback",
        });
    const runtime = referenceRuntime(scope, library, worker);

    const selection = await runtime.produceReferenceAnalysisAttempt(source);

    expect(selection.allReceipts.map(({ layer, status }) => ({ layer, status }))).toEqual([
      { layer: "scene", status: "succeeded" },
      { layer: "transcript", status: "failed-manual-review" },
      { layer: "vision", status: "failed-manual-review" },
    ]);
    expect(selection.evidenceReceipts.map((receipt) => receipt.layer)).toEqual(["scene"]);
    expect(worker.run).toHaveBeenCalledTimes(3);
  });

  it("reopens the frozen 6d0ff5c cancellation and runs one replacement attempt", async () => {
    const seeded = await seedLegacyScenario(scope, null);
    roots.push(seeded.root);
    await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
    const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
    const reopenedSource = await reopenedLibrary.inspectReferenceAnalysisAsset(
      scope.actorId, scope.customerId, scope.sessionId, seeded.assetId,
    );
    const worker = successfulReferenceWorker();
    const runtime = referenceRuntime(scope, reopenedLibrary, worker);

    const recovery = runtime.produceReferenceAnalysisAttempt(reopenedSource);
    await recovery.catch(() => undefined);
    expect(worker.run).toHaveBeenCalledTimes(3);
    await expect(recovery).resolves.toMatchObject({
      allReceipts: [
        { layer: "scene", status: "succeeded" },
        { layer: "transcript", status: "succeeded" },
        { layer: "vision", status: "succeeded" },
      ],
      evidenceReceipts: [
        { layer: "scene", status: "succeeded" },
        { layer: "transcript", status: "succeeded" },
        { layer: "vision", status: "succeeded" },
      ],
    });
    const persisted = await reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId) as {
      jobs: Array<Record<string, unknown>>;
      migrations: Array<Record<string, unknown>>;
    };
    expect(persisted.jobs).toHaveLength(1);
    expect(persisted.jobs[0]).toMatchObject({
      status: "succeeded",
      attempt: 2,
      sourceAssetSessionId: null,
    });
    expect(persisted.migrations).toEqual([{
      schemaVersion: 1,
      kind: "legacy-sessionless-source-session-repair",
      jobId: analysisJobId(scope, seeded.operationKey),
      previousSourceAssetSessionId: scope.sessionId,
      previousStatus: "cancelled",
      previousCancellationReason: "studio-analysis-source-changed",
      migratedAt: "2026-09-21T00:00:00.000Z",
    }]);

    const replayWorker = successfulReferenceWorker();
    const replayRuntime = referenceRuntime(scope, reopenedLibrary, replayWorker);
    await expect(replayRuntime.produceReferenceAnalysisAttempt(reopenedSource))
      .resolves.toMatchObject({ allReceipts: { length: 3 }, evidenceReceipts: { length: 3 } });
    expect(replayWorker.run).not.toHaveBeenCalled();
    await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
      .resolves.toEqual(persisted);
  });

  it("does not repair a legacy cancellation after the source is withdrawn", async () => {
    const seeded = await seedLegacyScenario(scope, null);
    roots.push(seeded.root);
    await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
    await seeded.library.withdraw(scope.actorId, scope.customerId, seeded.assetId, "withdraw-legacy-source");
    const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
    const worker = successfulReferenceWorker();
    const runtime = referenceRuntime(scope, reopenedLibrary, worker);

    await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
      .rejects.toThrow("studio-analysis-permission-blocked");
    expect(worker.run).not.toHaveBeenCalled();
    await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
      .resolves.toEqual(seeded.state);
  });

  it.each(["digest", "path"] as const)(
    "does not repair a legacy cancellation after the exact source %s changes",
    async (changed) => {
      const seeded = await seedLegacyScenario(scope, null);
      roots.push(seeded.root);
      if (changed === "digest") seeded.state.jobs[0]!.sourceDigest = "f".repeat(64);
      else seeded.state.jobs[0]!.sourcePath = "/different/reference-source.mov";
      await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
      const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
      const worker = successfulReferenceWorker();
      const runtime = referenceRuntime(scope, reopenedLibrary, worker);

      await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
        .rejects.toThrow("reference-analysis-final-attempt-not-succeeded");
      expect(worker.run).not.toHaveBeenCalled();
      await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
        .resolves.toEqual(seeded.state);
    },
  );

  it("does not repair a cancelled job for a normal session-bound asset", async () => {
    const seeded = await seedLegacyScenario(scope, scope.sessionId);
    roots.push(seeded.root);
    await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
    const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
    const worker = successfulReferenceWorker();
    const runtime = referenceRuntime(scope, reopenedLibrary, worker);

    await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
      .rejects.toThrow("reference-analysis-final-attempt-not-succeeded");
    expect(worker.run).not.toHaveBeenCalled();
    await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
      .resolves.toEqual(seeded.state);
  });

  it("does not repair a cancellation with a permission-revocation reason", async () => {
    const seeded = await seedLegacyScenario(scope, null);
    roots.push(seeded.root);
    seeded.state.jobs[0]!.cancellationReason = "studio-analysis-permission-blocked";
    await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
    const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
    const worker = successfulReferenceWorker();
    const runtime = referenceRuntime(scope, reopenedLibrary, worker);

    await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
      .rejects.toThrow("reference-analysis-final-attempt-not-succeeded");
    expect(worker.run).not.toHaveBeenCalled();
    await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
      .resolves.toEqual(seeded.state);
  });

  it("does not repair a cancellation recorded under output-use authorization", async () => {
    const seeded = await seedLegacyScenario(scope, null);
    roots.push(seeded.root);
    seeded.state.jobs[0]!.sourceAuthorization = "output-use-authorized";
    await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
    const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
    const worker = successfulReferenceWorker();
    const runtime = referenceRuntime(scope, reopenedLibrary, worker);

    await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
      .rejects.toThrow("reference-analysis-final-attempt-not-succeeded");
    expect(worker.run).not.toHaveBeenCalled();
    await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
      .resolves.toEqual(seeded.state);
  });

  it.each(["customer", "session"] as const)(
    "rejects a legacy cancellation persisted under a different %s scope",
    async (changedScope) => {
      const seeded = await seedLegacyScenario(scope, null);
      roots.push(seeded.root);
      const foreignScope = {
        ...scope,
        customerId: changedScope === "customer" ? "customer-b" : scope.customerId,
        sessionId: changedScope === "session" ? "session-b" : scope.sessionId,
      };
      seeded.state.customerId = foreignScope.customerId;
      seeded.state.sessionId = foreignScope.sessionId;
      seeded.state.jobs[0]!.customerId = foreignScope.customerId;
      seeded.state.jobs[0]!.sessionId = foreignScope.sessionId;
      seeded.state.jobs[0]!.sourceAssetSessionId = foreignScope.sessionId;
      seeded.state.jobs[0]!.id = analysisJobId(foreignScope, seeded.operationKey);
      await seeded.library.writeStudioAnalysisState(scope.customerId, scope.sessionId, seeded.state);
      const reopenedLibrary = new CustomerMaterialLibrary(seeded.root, () => true);
      const worker = successfulReferenceWorker();
      const runtime = referenceRuntime(scope, reopenedLibrary, worker);

      await expect(runtime.produceReferenceAnalysisAttempt(seeded.source))
        .rejects.toThrow("studio-analysis-state-scope-mismatch");
      expect(worker.run).not.toHaveBeenCalled();
      await expect(reopenedLibrary.readStudioAnalysisState(scope.customerId, scope.sessionId))
        .resolves.toEqual(seeded.state);
    },
  );
});

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function legacySessionlessState(
  scope: StudioScope,
  source: ReferenceAnalysisAssetSnapshot,
  operationKey: string,
): Promise<MutableLegacyAnalysisState> {
  const replacements = new Map<string, string>([
    ["__CUSTOMER_ID__", scope.customerId],
    ["__WORKFLOW_SESSION_ID__", scope.sessionId],
    ["__ACTOR_ID__", scope.actorId],
    ["__SOURCE_ASSET_ID__", source.asset.id],
    ["__SOURCE_DIGEST__", source.asset.digest],
    ["__SOURCE_PATH__", source.sourcePath],
    ["__OPERATION_KEY__", operationKey],
    ["__REQUEST_DIGEST__", createHash("sha256").update(JSON.stringify({
      actorId: scope.actorId,
      assetId: source.asset.id,
      layers: ["scene", "transcript", "vision"],
      sourceAuthorization: "reference-view-authorized",
    })).digest("hex")],
    ["__JOB_ID__", analysisJobId(scope, operationKey)],
  ]);
  let fixture = await readFile(legacySessionlessStateFixture, "utf8");
  for (const [token, value] of replacements) {
    fixture = fixture.replaceAll(JSON.stringify(token), JSON.stringify(value));
  }
  return JSON.parse(fixture) as MutableLegacyAnalysisState;
}

type MutableLegacyAnalysisState = {
  schemaVersion: number;
  customerId: string;
  sessionId: string;
  jobs: Array<Record<string, unknown>>;
  receipts: unknown[];
};

async function seedLegacyScenario(scope: StudioScope, assetSessionId: string | null) {
  const root = await mkdtemp(join(tmpdir(), "legacy-sessionless-analysis-"));
  const library = new CustomerMaterialLibrary(root, () => true);
  const asset = await library.importOriginal(scope.actorId, scope.customerId, {
    requestId: `legacy-source-${assetSessionId ?? "none"}`,
    bytes: Buffer.from(`legacy-source-${assetSessionId ?? "none"}`),
    mediaKind: "video",
    fileName: "legacy-reference.mp4",
    capturedAt: null,
    sessionId: assetSessionId,
    viewPermission: "allowed",
    usePermission: "review-required",
  });
  const source = await library.inspectReferenceAnalysisAsset(
    scope.actorId, scope.customerId, scope.sessionId, asset.id,
  );
  const operationKey = referenceOperationKey(source);
  return {
    root,
    library,
    assetId: asset.id,
    source,
    operationKey,
    state: await legacySessionlessState(scope, source, operationKey),
  };
}

function referenceRuntime(
  scope: StudioScope,
  library: CustomerMaterialLibrary,
  worker: StudioModelWorker,
): StudioModelRuntime {
  return new StudioModelRuntime({
    scope,
    library,
    studio: emptyStudioHistory() as never,
    worker,
    now: () => "2026-09-21T00:00:00.000Z",
    resolveSourceInterval: async () => ({
      sourceStartMs: 0,
      sourceEndMs: 10_000,
      sourceDurationMs: 10_000,
    }),
  });
}

function referenceOperationKey(source: ReferenceAnalysisAssetSnapshot): string {
  return `reference-analysis-${createHash("sha256").update(JSON.stringify({
    assetId: source.asset.id,
    digest: source.asset.digest,
    path: source.sourcePath,
  })).digest("hex")}`;
}

function analysisJobId(scope: StudioScope, operationKey: string): string {
  return `analysis_${createHash("sha256")
    .update(`${scope.customerId}\0${scope.sessionId}\0${scope.actorId}\0${operationKey}`)
    .digest("hex")}`;
}

function successfulReferenceWorker(
  produce: (job: ModelAnalysisJobV1) => ModelAnalysisReceiptV1 = receiptForWorkerJob,
): StudioModelWorker & { run: ReturnType<typeof vi.fn> } {
  const run = vi.fn(async (job: ModelAnalysisJobV1) => produce(job));
  return {
    run,
    health: vi.fn(async () => ({
      mode: "offline-fallback" as const,
      installed: false as const,
      modelName: null,
      checkpointDigest: null,
    })),
  };
}

function receiptForWorkerJob(job: ModelAnalysisJobV1): ModelAnalysisReceiptV1 {
  const { requestedAt: _requestedAt, ...jobBinding } = job;
  return {
    ...jobBinding,
    adapterName: `fixture-${job.layer}`,
    modelName: `fixture-${job.layer}`,
    modelVersion: "v1",
    checkpointDigest: null,
    inputDigest: job.sourceDigest,
    promptVersion: "fixture-v1",
    observations: [],
    suggestedTags: [],
    qualitySignals: job.layer === "transcript" ? { spans: [] } : {},
    confidence: 0.9,
    warnings: [],
    startedAt: "2026-09-21T00:00:00.000Z",
    completedAt: "2026-09-21T00:00:00.001Z",
    runtimeMs: 1,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
  };
}

function emptyStudioHistory() {
  return {
    listStudioHistory: vi.fn(async () => ({ demands: [], assessments: [] })),
  };
}

function entry(
  receipts: ModelAnalysisReceiptV1[],
  attempt: number,
  status: StudioAnalysisHistoryEntry["job"]["status"],
  operationKey = "reference-analysis",
): StudioAnalysisHistoryEntry {
  return {
    job: {
      schemaVersion: 1, id: "parent-job", actorId: "actor-a", ...binding,
      sourceDurationMs: 2_000, sourceAuthorization: "reference-view-authorized",
      layers: ["scene", "transcript", "vision"],
      operationKey, requestDigest: "b".repeat(64), status,
      attempt, receiptJobIds: receipts.map((row) => row.jobId), runnerInstanceId: null,
      failure: status === "succeeded" ? null : { code: "failed", message: null },
      cancellationReason: null, createdAt: "2026-09-20T00:00:00.000Z",
      updatedAt: "2026-09-20T00:00:01.000Z",
    },
    receipts,
  };
}

function layers(attempt: number): ModelAnalysisReceiptV1[] {
  return [receipt("scene", attempt), receipt("transcript", attempt), receipt("vision", attempt)];
}

function receipt(
  layer: ModelAnalysisReceiptV1["layer"],
  attempt: number,
  status: ModelAnalysisReceiptV1["status"] = "succeeded",
  duplicate = 0,
): ModelAnalysisReceiptV1 {
  return {
    schemaVersion: 1, jobId: `receipt-${attempt}-${layer}-${duplicate}`, ...binding, layer,
    operationKey: "reference-analysis", adapterName: `adapter-${layer}`,
    modelName: `model-${layer}`, modelVersion: "v1", checkpointDigest: null,
    inputDigest: createHash("sha256").update(`${attempt}-${layer}-${duplicate}`).digest("hex"),
    promptVersion: "v1", observations: [], suggestedTags: [], qualitySignals: {},
    confidence: status === "succeeded" ? 0.9 : 0, warnings: [],
    startedAt: "2026-09-20T00:00:00.000Z", completedAt: "2026-09-20T00:00:01.000Z",
    runtimeMs: 1, status, failure: status === "succeeded" ? null : { code: "failed" },
    synthetic: false, providerMode: "local",
  };
}
