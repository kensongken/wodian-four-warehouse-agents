import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ModelAnalysisJobV1,
  ModelAnalysisReceiptV1,
  StudioAnalysisStatus,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CustomerMaterialLibrary,
  ReferenceSampleService,
  StudioAnalysisJobService,
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
  type StudioSourceIntervalResolver,
  type StudioModelWorker,
  type StudioScope,
} from "../src";

const scopeA: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};

const scopeB: StudioScope = {
  actorId: "actor-b",
  customerId: "customer-b",
  sessionId: "session-b",
};

describe("studio analysis job service", () => {
  let root: string;
  let library: CustomerMaterialLibrary;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "studio-analysis-jobs-"));
    library = new CustomerMaterialLibrary(root, () => true);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("replays the same scoped operation without a second worker call", async () => {
    const asset = await importAsset(library, scopeA, "idempotent");
    const worker = successfulWorker();
    const service = createService(library, worker);

    const first = await service.enqueue(scopeA, asset.id, ["technical"], "op-1");
    const replay = await service.enqueue(scopeA, asset.id, ["technical"], "op-1");

    expect(replay).toEqual(first);
    expect(first.job.status).toBe("succeeded");
    expect(worker.run).toHaveBeenCalledTimes(1);
  });

  it("runs an idempotent reference-view analysis without granting output-use permission", async () => {
    const asset = await importAsset(library, scopeA, "reference-view", "review-required");
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );
    const inspectOrdinary = vi.spyOn(library, "inspectAnalysisAsset");
    const worker = successfulWorker();
    const service = createService(library, worker);

    const first = await service.enqueueReference(
      scopeA, source, ["scene", "transcript", "vision"], "reference-view-analysis",
    );
    const replay = await service.enqueueReference(
      scopeA, source, ["scene", "transcript", "vision"], "reference-view-analysis",
    );

    expect(replay).toEqual(first);
    expect(first.job.status).toBe("succeeded");
    expect(worker.run).toHaveBeenCalledTimes(3);
    expect(inspectOrdinary).not.toHaveBeenCalled();
    expect((await library.listAssets(scopeA.actorId, scopeA.customerId))
      .find((candidate) => candidate.id === asset.id)?.usePermission).toBe("review-required");
  });

  it("registers and analyzes a sessionless historical asset with an exact nullable source binding", async () => {
    const asset = await library.importOriginal(scopeA.actorId, scopeA.customerId, {
      requestId: "sessionless-history-source",
      bytes: Buffer.from("sessionless-reference-source"),
      mediaKind: "video",
      fileName: "sessionless.mp4",
      capturedAt: null,
      sessionId: null,
      viewPermission: "allowed",
      usePermission: "review-required",
    });
    const sampleService = new ReferenceSampleService(library, join(root, "reference-store"), {
      now: () => "2026-09-12T04:00:00.000Z",
    });
    const registration = await sampleService.registerHistorical(scopeA, {
      assetId: asset.id,
      sourceKind: "self-owned",
      operationKey: "sessionless-history-pointer",
    });
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, registration.asset.id,
    );
    const worker = successfulWorker();
    const service = createService(library, worker);

    const result = await service.enqueueReference(
      scopeA, source, ["scene", "transcript", "vision"], "sessionless-history-analysis",
    );

    expect(result.job.status).toBe("succeeded");
    expect(result.job.sourceAssetSessionId).toBeNull();
    expect(result.job.attempt).toBe(1);
    expect(worker.run).toHaveBeenCalledTimes(3);
  });

  it("finishes only the canonical reference job after valid optional-layer failure receipts", async () => {
    const asset = await importAsset(library, scopeA, "reference-optional-failures", "review-required");
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );
    const worker = fakeWorker(async (job) => receiptFor(
      job,
      job.layer === "scene"
        ? "succeeded"
        : job.layer === "transcript" ? "failed-manual-review" : "failed-retryable",
    ));
    const service = createService(library, worker);

    const result = await service.enqueueReference(
      scopeA,
      source,
      ["scene", "transcript", "vision"],
      "reference-optional-failures",
    );

    expect(result.job).toMatchObject({ status: "succeeded", attempt: 1, failure: null });
    expect(result.receipts.map(({ layer, status }) => ({ layer, status }))).toEqual([
      { layer: "scene", status: "succeeded" },
      { layer: "transcript", status: "failed-manual-review" },
      { layer: "vision", status: "failed-retryable" },
    ]);
    expect(worker.run).toHaveBeenCalledTimes(3);
  });

  it("fails fast when the required scene receipt fails in a canonical reference job", async () => {
    const asset = await importAsset(library, scopeA, "reference-scene-failure", "review-required");
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );
    const worker = fakeWorker(async (job) => receiptFor(job, "failed-retryable"));
    const service = createService(library, worker);

    const result = await service.enqueueReference(
      scopeA,
      source,
      ["scene", "transcript", "vision"],
      "reference-scene-failure",
    );

    expect(result.job.status).toBe("failed-retryable");
    expect(result.receipts.map(({ layer, status }) => ({ layer, status }))).toEqual([
      { layer: "scene", status: "failed-retryable" },
    ]);
    expect(worker.run).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["ordinary output-use job", false, ["scene", "transcript", "vision"] as const],
    ["reordered reference job", true, ["scene", "vision", "transcript"] as const],
  ])("keeps %s fail-fast when a later layer returns a failure receipt", async (
    _name,
    referenceView,
    layers,
  ) => {
    const asset = await importAsset(
      library,
      scopeA,
      `noncanonical-${referenceView ? "reference" : "ordinary"}`,
      referenceView ? "review-required" : "allowed",
    );
    const worker = fakeWorker(async (job) => receiptFor(
      job,
      job.layer === layers[1] ? "failed-manual-review" : "succeeded",
    ));
    const service = createService(library, worker);

    const result = referenceView
      ? await service.enqueueReference(
          scopeA,
          await library.inspectReferenceAnalysisAsset(
            scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
          ),
          layers,
          `noncanonical-${referenceView ? "reference" : "ordinary"}`,
        )
      : await service.enqueue(
          scopeA,
          asset.id,
          layers,
          `noncanonical-${referenceView ? "reference" : "ordinary"}`,
        );

    expect(result.job.status).toBe("failed-manual-review");
    expect(result.receipts).toHaveLength(2);
    expect(worker.run).toHaveBeenCalledTimes(2);
  });

  it.each(["throw", "invalid-receipt"] as const)(
    "does not forge an optional-layer failure receipt when the worker returns %s",
    async (failureMode) => {
      const asset = await importAsset(library, scopeA, `reference-${failureMode}`, "review-required");
      const source = await library.inspectReferenceAnalysisAsset(
        scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
      );
      const worker = fakeWorker(async (job) => {
        if (job.layer === "scene") return receiptFor(job);
        if (failureMode === "throw") throw new Error("studio-model-worker-spawn-failed");
        return { ...receiptFor(job), sourceDigest: "f".repeat(64) };
      });
      const service = createService(library, worker);

      const result = await service.enqueueReference(
        scopeA,
        source,
        ["scene", "transcript", "vision"],
        `reference-${failureMode}`,
      );

      expect(result.job.status).toBe(
        failureMode === "throw" ? "failed-retryable" : "failed-manual-review",
      );
      expect(result.receipts).toEqual([]);
      expect(worker.run).toHaveBeenCalledTimes(2);
    },
  );

  it("cancels reference-view receipts when view permission is withdrawn before commit", async () => {
    const asset = await importAsset(library, scopeA, "reference-withdraw", "review-required");
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );
    const inspectOrdinary = vi.spyOn(library, "inspectAnalysisAsset");
    const gate = deferred<ModelAnalysisReceiptV1>();
    let job!: ModelAnalysisJobV1;
    const worker = fakeWorker(async (input) => {
      job = input;
      return gate.promise;
    });
    const service = createService(library, worker);

    const running = service.enqueueReference(scopeA, source, ["scene"], "reference-withdraw-analysis");
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(1));
    await library.withdraw(scopeA.actorId, scopeA.customerId, asset.id, "reference-withdrawn");
    gate.resolve(receiptFor(job));

    await expect(running).resolves.toMatchObject({
      job: { status: "cancelled", cancellationReason: "studio-analysis-permission-blocked" },
      receipts: [],
    });
    expect(inspectOrdinary).not.toHaveBeenCalled();
  });

  it("fails closed when one actor reuses an operation key with another asset or layer set", async () => {
    const firstAsset = await importAsset(library, scopeA, "conflict-a");
    const secondAsset = await importAsset(library, scopeA, "conflict-b");
    const worker = successfulWorker();
    const service = createService(library, worker);
    await service.enqueue(scopeA, firstAsset.id, ["technical"], "shared-operation");

    await expect(service.enqueue(scopeA, secondAsset.id, ["technical"], "shared-operation"))
      .rejects.toThrow("studio-analysis-operation-key-conflict");
    await expect(service.enqueue(scopeA, firstAsset.id, ["vision"], "shared-operation"))
      .rejects.toThrow("studio-analysis-operation-key-conflict");
    expect(worker.run).toHaveBeenCalledTimes(1);
  });

  it("authorizes the asset before looking up an operation key", async () => {
    let blockedActor = false;
    library = new CustomerMaterialLibrary(root, ({ actorId, action }) =>
      !(blockedActor && actorId === "actor-b" && action === "use"));
    const asset = await importAsset(library, scopeA, "authorization-before-operation");
    const worker = successfulWorker();
    const service = createService(library, worker);
    await service.enqueue(scopeA, asset.id, ["technical"], "shared-authorized-operation");
    blockedActor = true;

    await expect(service.enqueue(
      { ...scopeA, actorId: "actor-b" },
      asset.id,
      ["technical"],
      "shared-authorized-operation",
    )).rejects.toThrow("studio-analysis-permission-blocked");
    expect(worker.run).toHaveBeenCalledTimes(1);
  });

  it("scopes operation keys, history, and receipts to the actor", async () => {
    const assetA = await importAsset(library, scopeA, "actor-a-operation");
    const actorB = { ...scopeA, actorId: "actor-b" };
    const assetB = await importAsset(library, actorB, "actor-b-operation");
    const worker = successfulWorker();
    const service = createService(library, worker);

    const resultA = await service.enqueue(scopeA, assetA.id, ["technical"], "same-key");
    await expect(service.history(actorB)).resolves.toEqual([]);
    const resultB = await service.enqueue(actorB, assetB.id, ["vision"], "same-key");

    await expect(service.history(scopeA)).resolves.toEqual([resultA]);
    await expect(service.history(actorB)).resolves.toEqual([resultB]);
    expect(resultA.job.id).not.toBe(resultB.job.id);
    expect(worker.run).toHaveBeenCalledTimes(2);
  });

  it("does not let another actor cancel an active job in the same customer session", async () => {
    const asset = await importAsset(library, scopeA, "actor-bound-cancel");
    const gate = deferred<ModelAnalysisReceiptV1>();
    let runningJob!: ModelAnalysisJobV1;
    let runningSignal!: AbortSignal;
    const worker = fakeWorker(async (job, signal) => {
      runningJob = job;
      runningSignal = signal;
      return gate.promise;
    });
    const service = createService(library, worker);

    const running = service.enqueue(scopeA, asset.id, ["technical"], "actor-bound-cancel");
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(1));
    await expect(service.cancelForAsset(
      { ...scopeA, actorId: "actor-b" },
      asset.id,
      "other-actor-cancel",
    )).resolves.toEqual([]);
    expect(runningSignal.aborted).toBe(false);
    gate.resolve(receiptFor(runningJob));
    await expect(running).resolves.toMatchObject({ job: { status: "succeeded" } });
  });

  it("rejects withdrawn work before reading or spawning the worker", async () => {
    const asset = await importAsset(library, scopeA, "withdrawn-before-run");
    await library.withdraw(scopeA.actorId, scopeA.customerId, asset.id, "withdraw-before-analysis");
    const worker = successfulWorker();
    const service = createService(library, worker);

    await expect(service.enqueue(scopeA, asset.id, ["vision"], "op-withdrawn"))
      .rejects.toThrow("studio-analysis-permission-blocked");
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("rejects an asset bound to another session before spawning", async () => {
    const asset = await importAsset(library, scopeA, "stale-session");
    const worker = successfulWorker();
    const service = createService(library, worker);

    await expect(service.enqueue(
      { ...scopeA, sessionId: "another-session" },
      asset.id,
      ["scene"],
      "op-stale-session",
    )).rejects.toThrow("studio-analysis-session-stale");
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("fails closed when no verified source interval is available", async () => {
    const asset = await importAsset(library, scopeA, "missing-source-interval");
    const worker = successfulWorker();
    const service = createService(library, worker, async () => null);

    await expect(service.enqueue(scopeA, asset.id, ["technical"], "missing-source-interval"))
      .rejects.toThrow("studio-analysis-source-interval-unavailable");
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("maps a resolver failure without exposing its path or raw output", async () => {
    const asset = await importAsset(library, scopeA, "failed-source-interval");
    const worker = successfulWorker();
    const service = createService(library, worker, async () => {
      throw new Error(`${root}/private.mp4: raw ffprobe output`);
    });

    const rejection = service.enqueue(scopeA, asset.id, ["technical"], "failed-source-interval");
    await expect(rejection).rejects.toThrow(/^studio-analysis-source-interval-unavailable$/);
    await expect(rejection).rejects.not.toThrow(root);
    await expect(rejection).rejects.not.toThrow("ffprobe");
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("rejects a source interval outside its verified full duration", async () => {
    const asset = await importAsset(library, scopeA, "invalid-source-interval");
    const worker = successfulWorker();
    for (const [index, interval] of [
      { sourceStartMs: -1, sourceEndMs: 5_000, sourceDurationMs: 10_000 },
      { sourceStartMs: 0, sourceEndMs: 0, sourceDurationMs: 10_000 },
      { sourceStartMs: 0, sourceEndMs: 10_001, sourceDurationMs: 10_000 },
    ].entries()) {
      const service = createService(library, worker, async () => interval);
      await expect(service.enqueue(scopeA, asset.id, ["scene"], `invalid-source-interval-${index}`))
        .rejects.toThrow("studio-analysis-source-interval-invalid");
    }
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("binds the job and raw receipt to the trusted full-source interval", async () => {
    const asset = await importAsset(library, scopeA, "full-source-interval");
    const worker = successfulWorker();
    const service = createService(library, worker, async () => ({
      sourceStartMs: 0,
      sourceEndMs: 12_345,
      sourceDurationMs: 12_345,
    }));

    const result = await service.enqueue(scopeA, asset.id, ["technical"], "full-source-interval");

    expect(worker.run).toHaveBeenCalledWith(
      expect.objectContaining({ sourceStartMs: 0, sourceEndMs: 12_345 }),
      expect.any(AbortSignal),
    );
    expect(result.receipts[0]).toMatchObject({ sourceStartMs: 0, sourceEndMs: 12_345 });
  });

  it("serializes workers within one customer session but permits another session to run", async () => {
    const firstAsset = await importAsset(library, scopeA, "serial-first");
    const secondAsset = await importAsset(library, scopeA, "serial-second");
    const otherScope = { ...scopeA, sessionId: "session-other" };
    const otherAsset = await importAsset(library, otherScope, "parallel-other-session");
    const gates = new Map<string, ReturnType<typeof deferred<ModelAnalysisReceiptV1>>>();
    const worker = fakeWorker(async (job) => {
      const gate = deferred<ModelAnalysisReceiptV1>();
      gates.set(job.operationKey, gate);
      return gate.promise;
    });
    const service = createService(library, worker);

    const first = service.enqueue(scopeA, firstAsset.id, ["technical"], "serial-1");
    await vi.waitFor(() => expect(gates.has("serial-1")).toBe(true));
    const second = service.enqueue(scopeA, secondAsset.id, ["technical"], "serial-2");
    const other = service.enqueue(otherScope, otherAsset.id, ["technical"], "parallel-1");
    await vi.waitFor(() => expect(gates.has("parallel-1")).toBe(true));
    expect(gates.has("serial-2")).toBe(false);

    const firstJob = worker.run.mock.calls.find(([job]) => job.operationKey === "serial-1")![0];
    gates.get("serial-1")!.resolve(receiptFor(firstJob));
    await first;
    await vi.waitFor(() => expect(gates.has("serial-2")).toBe(true));
    const secondJob = worker.run.mock.calls.find(([job]) => job.operationKey === "serial-2")![0];
    const otherJob = worker.run.mock.calls.find(([job]) => job.operationKey === "parallel-1")![0];
    gates.get("serial-2")!.resolve(receiptFor(secondJob));
    gates.get("parallel-1")!.resolve(receiptFor(otherJob));
    await Promise.all([second, other]);
  });

  it("cancels a completed worker result when permission is withdrawn before commit", async () => {
    const asset = await importAsset(library, scopeA, "withdrawn-after-run");
    const gate = deferred<ModelAnalysisReceiptV1>();
    let job!: ModelAnalysisJobV1;
    const worker = fakeWorker(async (input) => {
      job = input;
      return gate.promise;
    });
    const service = createService(library, worker);

    const running = service.enqueue(scopeA, asset.id, ["vision"], "withdraw-during-run");
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(1));
    await library.withdraw(scopeA.actorId, scopeA.customerId, asset.id, "withdraw-mid-analysis");
    gate.resolve(receiptFor(job));

    const result = await running;
    expect(result.job).toMatchObject({
      status: "cancelled",
      cancellationReason: "studio-analysis-permission-blocked",
    });
    expect(result.receipts).toEqual([]);
  });

  it("cancels and discards a result when the source digest changes before commit", async () => {
    const asset = await importAsset(library, scopeA, "digest-change");
    const gate = deferred<ModelAnalysisReceiptV1>();
    let job!: ModelAnalysisJobV1;
    const worker = fakeWorker(async (input) => {
      job = input;
      return gate.promise;
    });
    const service = createService(library, worker);

    const running = service.enqueue(scopeA, asset.id, ["scene"], "digest-change-run");
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(1));
    await writeFile(job.sourcePath, Buffer.from("tampered-synthetic-source"));
    gate.resolve(receiptFor(job));

    const result = await running;
    expect(result.job).toMatchObject({
      status: "cancelled",
      cancellationReason: "studio-analysis-source-changed",
    });
    expect(result.receipts).toEqual([]);
  });

  it("aborts active work through cancelForAsset and never commits its receipt", async () => {
    const asset = await importAsset(library, scopeA, "explicit-cancel");
    const worker = fakeWorker((_job, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("studio-model-worker-aborted")), { once: true });
    }));
    const service = createService(library, worker);

    const running = service.enqueue(scopeA, asset.id, ["transcript"], "cancel-active");
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(1));
    const cancelled = service.cancelForAsset(scopeA, asset.id, "operator-cancelled");

    const [result, cancelledJobs] = await Promise.all([running, cancelled]);
    expect(result.job.status).toBe("cancelled");
    expect(result.job.cancellationReason).toBe("operator-cancelled");
    expect(result.receipts).toEqual([]);
    expect(cancelledJobs).toEqual([result]);
    await expect(service.retry(scopeA, result.job.id))
      .rejects.toThrow("studio-analysis-retry-not-allowed");
  });

  it("spawns under the final authorization lock before a queued withdrawal can interleave", async () => {
    const events: string[] = [];
    let useChecks = 0;
    let withdrawPromise: Promise<unknown> | undefined;
    library = new CustomerMaterialLibrary(root, ({ action, phase }) => {
      if (action === "use" && phase === "after" && ++useChecks === 3) {
        queueMicrotask(() => {
          withdrawPromise = library.withdraw(scopeA.actorId, scopeA.customerId, asset.id, "race-withdraw")
            .then(() => events.push("withdrawn"));
        });
      }
      return true;
    });
    const asset = await importAsset(library, scopeA, "spawn-handoff-race");
    const gate = deferred<ModelAnalysisReceiptV1>();
    let job!: ModelAnalysisJobV1;
    const worker = fakeWorker((input) => {
      job = input;
      events.push("spawn");
      return gate.promise;
    });
    const service = createService(library, worker);

    const running = service.enqueue(scopeA, asset.id, ["vision"], "spawn-handoff-race");
    await vi.waitFor(() => expect(events).toContain("spawn"));
    await vi.waitFor(() => expect(withdrawPromise).toBeDefined());
    await withdrawPromise;
    expect(events).toEqual(["spawn", "withdrawn"]);
    gate.resolve(receiptFor(job));
    await expect(running).resolves.toMatchObject({ job: { status: "cancelled" }, receipts: [] });
  });

  it("rechecks authorization inside the atomic final state write", async () => {
    const asset = await importAsset(library, scopeA, "atomic-final-write");
    const original = library.writeStudioAnalysisStateIfCurrent.bind(library);
    vi.spyOn(library, "writeStudioAnalysisStateIfCurrent").mockImplementation(async (binding, value, canCommit) => {
      await library.withdraw(scopeA.actorId, scopeA.customerId, asset.id, "withdraw-before-atomic-write");
      return original(binding, value, canCommit);
    });
    const service = createService(library, successfulWorker());

    const result = await service.enqueue(scopeA, asset.id, ["technical"], "atomic-final-write");

    expect(result.job).toMatchObject({
      status: "cancelled",
      cancellationReason: "studio-analysis-permission-blocked",
    });
    expect(result.receipts).toEqual([]);
  });

  it("treats a cancellation after atomic rename is invoked as later than receipt commit", async () => {
    let service!: StudioAnalysisJobService;
    let analysisRenames = 0;
    let lateCancellation: Promise<unknown> | undefined;
    const atomicRename = vi.fn((from: string, to: string) => {
        const committed = rename(from, to);
        if (to.endsWith("analysis-state.json") && ++analysisRenames === 3) {
          lateCancellation = service.cancelForAsset(scopeA, asset.id, "late-cancel");
        }
        return committed;
      });
    library = new CustomerMaterialLibrary(root, () => true, {
      atomicRename,
    });
    const asset = await importAsset(library, scopeA, "late-cancel-linearization");
    service = createService(library, successfulWorker());

    const result = await service.enqueue(scopeA, asset.id, ["technical"], "late-cancel-linearization");
    await lateCancellation;

    expect(result.job.status).toBe("succeeded");
    expect(result.receipts).toHaveLength(1);
    expect(atomicRename).toHaveBeenCalledTimes(3);
    await expect(service.history(scopeA)).resolves.toEqual([result]);
  });

  it("invokes the final predicate and atomic rename without a microtask gap", async () => {
    let cancellationQueued = false;
    const observedAtRename: boolean[] = [];
    library = new CustomerMaterialLibrary(root, () => true, {
      atomicRename: (from, to) => {
        observedAtRename.push(cancellationQueued);
        return rename(from, to);
      },
    });
    const asset = await importAsset(library, scopeA, "predicate-rename-same-turn");
    const snapshot = await library.inspectAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );

    await library.writeStudioAnalysisStateIfCurrent({
      actorId: scopeA.actorId,
      customerId: scopeA.customerId,
      sessionId: scopeA.sessionId,
      sourceAssetSessionId: scopeA.sessionId,
      assetId: asset.id,
      sourceDigest: asset.digest,
      sourcePath: snapshot.sourcePath,
    }, { marker: "committed" }, () => {
      queueMicrotask(() => { cancellationQueued = true; });
      return true;
    });

    expect(observedAtRename).toEqual([false]);
    expect(cancellationQueued).toBe(true);
    await expect(library.readStudioAnalysisState(scopeA.customerId, scopeA.sessionId))
      .resolves.toEqual({ marker: "committed" });
  });

  it("blocks receipt commit when cancellation lands before the final synchronous predicate", async () => {
    let service!: StudioAnalysisJobService;
    let useChecks = 0;
    let cancellation: Promise<unknown> | undefined;
    const committedStatuses: string[] = [];
    const atomicRename = vi.fn((from: string, to: string) => {
      committedStatuses.push(JSON.parse(readFileSync(from, "utf8")).jobs[0].status);
      return rename(from, to);
    });
    library = new CustomerMaterialLibrary(root, ({ action, phase }) => {
      if (action === "use" && phase === "after" && ++useChecks === 4) {
        queueMicrotask(() => {
          cancellation = service.cancelForAsset(scopeA, asset.id, "cancel-before-commit-predicate");
        });
      }
      return true;
    }, { atomicRename });
    const asset = await importAsset(library, scopeA, "cancel-before-commit-predicate");
    service = createService(library, successfulWorker());

    const result = await service.enqueue(scopeA, asset.id, ["technical"], "cancel-before-commit-predicate");
    await cancellation;

    expect(result.job).toMatchObject({
      status: "cancelled",
      cancellationReason: "cancel-before-commit-predicate",
    });
    expect(result.receipts).toEqual([]);
    expect(atomicRename).toHaveBeenCalledTimes(3);
    expect(committedStatuses).toEqual(["queued", "running", "cancelled"]);
  });

  it("does not spawn when cancellation lands after running is persisted but before worker registration", async () => {
    const asset = await importAsset(library, scopeA, "cancel-before-registration");
    const worker = successfulWorker();
    let service!: StudioAnalysisJobService;
    const inspect = library.inspectAnalysisAsset.bind(library);
    let inspections = 0;
    vi.spyOn(library, "inspectAnalysisAsset").mockImplementation(async (...args) => {
      const snapshot = await inspect(...args);
      inspections += 1;
      if (inspections === 2) {
        await service.cancelForAsset(scopeA, asset.id, "cancelled-in-registration-window");
      }
      return snapshot;
    });
    service = createService(library, worker);

    const result = await service.enqueue(scopeA, asset.id, ["vision"], "cancel-before-registration");

    expect(result.job).toMatchObject({
      status: "cancelled",
      cancellationReason: "cancelled-in-registration-window",
    });
    expect(result.receipts).toEqual([]);
    expect(worker.run).not.toHaveBeenCalled();
  });

  it("retries only a retryable job and preserves raw receipts as separate attempts", async () => {
    const asset = await importAsset(library, scopeA, "retryable");
    let attempt = 0;
    const worker = fakeWorker(async (job) => {
      attempt += 1;
      return receiptFor(job, attempt === 1 ? "failed-retryable" : "succeeded");
    });
    const service = createService(library, worker);

    const failed = await service.enqueue(scopeA, asset.id, ["embedding"], "retry-operation");
    expect(failed.job.status).toBe("failed-retryable");
    const succeeded = await service.retry(scopeA, failed.job.id);

    expect(succeeded.job.status).toBe("succeeded");
    expect(succeeded.job.attempt).toBe(2);
    expect(succeeded.receipts).toHaveLength(2);
    expect(succeeded.receipts.map((receipt) => receipt.status))
      .toEqual(["failed-retryable", "succeeded"]);
    await expect(service.retry(scopeA, failed.job.id))
      .rejects.toThrow("studio-analysis-retry-not-allowed");
  });

  it("claims one exact failed attempt atomically across concurrent retry callers", async () => {
    const asset = await importAsset(library, scopeA, "atomic-retry");
    const retryGate = deferred<ModelAnalysisReceiptV1>();
    let calls = 0;
    let retryJob!: ModelAnalysisJobV1;
    const worker = fakeWorker(async (job) => {
      calls += 1;
      if (calls === 1) return receiptFor(job, "failed-retryable");
      retryJob = job;
      return retryGate.promise;
    });
    const service = createService(library, worker);
    const failed = await service.enqueue(scopeA, asset.id, ["embedding"], "atomic-retry-operation");

    const first = service.retryIfCurrent(scopeA, failed.job.id, failed.job.attempt);
    const second = service.retryIfCurrent(scopeA, failed.job.id, failed.job.attempt);
    await vi.waitFor(() => expect(worker.run).toHaveBeenCalledTimes(2));
    retryGate.resolve(receiptFor(retryJob));
    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(firstResult).toEqual(secondResult);
    expect(firstResult.job).toMatchObject({ status: "succeeded", attempt: 2 });
    expect(worker.run).toHaveBeenCalledTimes(2);
    const stale = await service.retryIfCurrent(scopeA, failed.job.id, failed.job.attempt);
    expect(stale.job).toMatchObject({ status: "succeeded", attempt: 2 });
    expect(worker.run).toHaveBeenCalledTimes(2);
  });

  it("does not retry manual-review, successful, or cancelled terminal states", async () => {
    for (const status of ["failed-manual-review", "succeeded"] as const) {
      const asset = await importAsset(library, scopeA, `terminal-${status}`);
      const service = createService(library, fakeWorker(async (job) => receiptFor(job, status)));
      const result = await service.enqueue(scopeA, asset.id, ["technical"], `terminal-${status}`);
      await expect(service.retry(scopeA, result.job.id))
        .rejects.toThrow("studio-analysis-retry-not-allowed");
    }
  });

  it.each([
    ["studio-model-worker-timeout", "failed-retryable", "studio-analysis-worker-timeout"],
    ["studio-model-worker-spawn-failed", "failed-retryable", "studio-analysis-worker-spawn-failed"],
    ["studio-model-worker-invalid-receipt", "failed-manual-review", "studio-analysis-invalid-receipt"],
  ] as const)("maps %s to a bounded safe job failure", async (workerError, status, code) => {
    const asset = await importAsset(library, scopeA, `safe-failure-${code}`);
    const worker = fakeWorker(async () => { throw new Error(workerError); });
    const service = createService(library, worker);

    const result = await service.enqueue(scopeA, asset.id, ["technical"], `safe-failure-${code}`);

    expect(result.job.status).toBe(status);
    expect(result.job.failure).toEqual({ code, message: null });
    expect(JSON.stringify(result.job.failure)).not.toContain(root);
    expect(JSON.stringify(result.job.failure)).not.toContain("raw transcript");
  });

  it("persists a bounded worker stage when a telemetry-enabled run times out", async () => {
    const asset = await importAsset(library, scopeA, "stage-timeout");
    const telemetry = {
      schemaVersion: 1 as const,
      lastStage: "model-generation" as const,
      completedStageMs: {
        "runtime-preaccept": 12,
        "frontstage-extraction": 24,
        "model-load": 36,
        "processor-prepare": 48,
      },
      activeStageElapsedMs: 5_001,
    };
    const worker = fakeWorker(async () => {
      throw new StudioModelWorkerTimeoutError(telemetry);
    });
    const service = createService(library, worker);

    const result = await service.enqueue(scopeA, asset.id, ["vision"], "stage-timeout");
    const restored = (await service.history(scopeA)).find((entry) => entry.job.id === result.job.id);

    expect(result.job.failure).toEqual({
      code: "studio-analysis-worker-timeout",
      message: null,
      stageTelemetry: telemetry,
    });
    expect(restored?.job.failure).toEqual(result.job.failure);
  });

  it("keeps a failed worker receipt while persisting its terminal stage", async () => {
    const asset = await importAsset(library, scopeA, "stage-receipt");
    const telemetry = {
      schemaVersion: 1 as const,
      lastStage: "model-generation" as const,
      completedStageMs: { "runtime-preaccept": 10 },
      activeStageElapsedMs: 250,
    };
    const worker = fakeWorker(async (job) => {
      throw new StudioModelWorkerFailureError(
        receiptFor(job, "failed-retryable"),
        telemetry,
      );
    });
    const service = createService(library, worker);

    const result = await service.enqueue(scopeA, asset.id, ["vision"], "stage-receipt");

    expect(result.job.status).toBe("failed-retryable");
    expect(result.receipts).toHaveLength(1);
    expect(result.job.failure?.stageTelemetry).toEqual(telemetry);
  });

  it("does not persist an unexpected worker error message or path", async () => {
    const asset = await importAsset(library, scopeA, "private-worker-error");
    const worker = fakeWorker(async () => {
      throw new Error(`decoder exploded at ${root}/private-source: raw transcript`);
    });
    const service = createService(library, worker);

    const result = await service.enqueue(scopeA, asset.id, ["technical"], "private-worker-error");

    expect(result.job.failure).toEqual({ code: "studio-analysis-invalid-receipt", message: null });
    expect(JSON.stringify(result.job)).not.toContain("decoder exploded");
    expect(JSON.stringify(result.job)).not.toContain("raw transcript");
  });

  it("recovers abandoned queued and running jobs once as retryable interrupted failures", async () => {
    const asset = await importAsset(library, scopeA, "interrupted-reload");
    const service = createService(library, successfulWorker());
    const created = await service.enqueue(scopeA, asset.id, ["technical"], "interrupted-seed");
    const [statePath] = await findFiles(root, "analysis-state.json");
    const state = JSON.parse(await readFile(statePath!, "utf8"));
    state.jobs.push(
      { ...created.job, id: analysisJobIdFor(scopeA, "queued-abandoned"), operationKey: "queued-abandoned", requestDigest: "a".repeat(64), status: "queued", receiptJobIds: [], runnerInstanceId: "old-runner", failure: null },
      { ...created.job, id: analysisJobIdFor(scopeA, "running-abandoned"), operationKey: "running-abandoned", requestDigest: "b".repeat(64), status: "running", receiptJobIds: [], runnerInstanceId: "old-runner", failure: null },
    );
    await writeFile(statePath!, `${JSON.stringify(state, null, 2)}\n`);

    const reopened = createService(new CustomerMaterialLibrary(root, () => true), successfulWorker());
    const first = await reopened.history(scopeA);
    const recovered = first.filter(({ job }) => job.operationKey.endsWith("abandoned"));
    expect(recovered.map(({ job }) => job.status)).toEqual(["failed-retryable", "failed-retryable"]);
    expect(recovered.map(({ job }) => job.failure)).toEqual([
      { code: "studio-analysis-interrupted", message: null },
      { code: "studio-analysis-interrupted", message: null },
    ]);
    const afterFirst = await readFile(statePath!, "utf8");
    await reopened.history(scopeA);
    expect(await readFile(statePath!, "utf8")).toBe(afterFirst);
  });

  it("returns empty history across customers and rejects cross-customer retry", async () => {
    const asset = await importAsset(library, scopeA, "tenant-a");
    const service = createService(library, successfulWorker());
    const result = await service.enqueue(scopeA, asset.id, ["technical"], "tenant-a-op");

    await expect(service.history(scopeB)).resolves.toEqual([]);
    await expect(service.retry(scopeB, result.job.id))
      .rejects.toThrow("studio-analysis-job-not-found");
  });

  it("does not let another actor retry a job in the same customer session", async () => {
    const asset = await importAsset(library, scopeA, "actor-bound-retry");
    const worker = fakeWorker(async (job) => receiptFor(job, "failed-retryable"));
    const service = createService(library, worker);
    const failed = await service.enqueue(scopeA, asset.id, ["technical"], "actor-bound-retry");

    await expect(service.retry({ ...scopeA, actorId: "actor-b" }, failed.job.id))
      .rejects.toThrow("studio-analysis-job-not-found");
    expect(worker.run).toHaveBeenCalledTimes(1);
  });

  it("atomically reloads jobs and raw receipts without embedding model output in the job", async () => {
    const asset = await importAsset(library, scopeA, "reload");
    const firstWorker = successfulWorker();
    const firstService = createService(library, firstWorker);
    const created = await firstService.enqueue(scopeA, asset.id, ["technical", "scene"], "reload-op");

    const reopenedLibrary = new CustomerMaterialLibrary(root, () => true);
    const reopenedWorker = successfulWorker();
    const reopened = createService(reopenedLibrary, reopenedWorker);
    await expect(reopened.history(scopeA)).resolves.toEqual([created]);
    await expect(reopened.enqueue(scopeA, asset.id, ["technical", "scene"], "reload-op"))
      .resolves.toEqual(created);
    expect(reopenedWorker.run).not.toHaveBeenCalled();
    expect(created.job).not.toHaveProperty("receipt");
    expect(created.job).not.toHaveProperty("observations");

    const stateFiles = await findFiles(root, "analysis-state.json");
    expect(stateFiles).toHaveLength(1);
    const persisted = JSON.parse(await readFile(stateFiles[0]!, "utf8"));
    expect(persisted.jobs).toHaveLength(1);
    expect(persisted.receipts).toHaveLength(2);
    expect(persisted.jobs[0]).not.toHaveProperty("receipt");
    expect((await findFiles(root, ".next-", true))).toEqual([]);
  });

  it("rejects same-actor receipt exchange between two jobs on reload", async () => {
    const firstAsset = await importAsset(library, scopeA, "same-actor-swap-a");
    const secondAsset = await importAsset(library, scopeA, "same-actor-swap-b");
    const service = createService(library, successfulWorker());
    await service.enqueue(scopeA, firstAsset.id, ["technical"], "same-actor-swap-a");
    await service.enqueue(scopeA, secondAsset.id, ["vision"], "same-actor-swap-b");
    const { path, state } = await readAnalysisState(root);
    [state.jobs[0].receiptJobIds, state.jobs[1].receiptJobIds] = [
      state.jobs[1].receiptJobIds,
      state.jobs[0].receiptJobIds,
    ];
    await writeFile(path, `${JSON.stringify(state, null, 2)}\n`);

    await expect(createService(new CustomerMaterialLibrary(root, () => true), successfulWorker()).history(scopeA))
      .rejects.toThrow("studio-analysis-state-invalid");
  });

  it("rejects cross-actor receipt exchange inside one customer session on reload", async () => {
    const actorB = { ...scopeA, actorId: "actor-b" };
    const assetA = await importAsset(library, scopeA, "cross-actor-swap-a");
    const assetB = await importAsset(library, actorB, "cross-actor-swap-b");
    const service = createService(library, successfulWorker());
    await service.enqueue(scopeA, assetA.id, ["technical"], "cross-actor-swap-a");
    await service.enqueue(actorB, assetB.id, ["vision"], "cross-actor-swap-b");
    const { path, state } = await readAnalysisState(root);
    [state.jobs[0].receiptJobIds, state.jobs[1].receiptJobIds] = [
      state.jobs[1].receiptJobIds,
      state.jobs[0].receiptJobIds,
    ];
    await writeFile(path, `${JSON.stringify(state, null, 2)}\n`);

    await expect(createService(new CustomerMaterialLibrary(root, () => true), successfulWorker()).history(scopeA))
      .rejects.toThrow("studio-analysis-state-invalid");
  });

  it("rejects orphan, multiply-owned, or field-mismatched receipts on reload", async () => {
    const asset = await importAsset(library, scopeA, "receipt-binding-corruption");
    const service = createService(library, successfulWorker());
    await service.enqueue(scopeA, asset.id, ["technical"], "receipt-binding-corruption");
    const { path, state: pristine } = await readAnalysisState(root);
    const corruptions = [
      (state: any) => { state.jobs[0].receiptJobIds = []; },
      (state: any) => { state.jobs.push({ ...state.jobs[0], id: analysisJobIdFor(scopeA, "duplicate-owner"), operationKey: "duplicate-owner", requestDigest: "f".repeat(64), receiptJobIds: [...state.jobs[0].receiptJobIds] }); },
      (state: any) => {
        const forgedId = `model_${"f".repeat(64)}`;
        state.receipts[0].jobId = forgedId;
        state.jobs[0].receiptJobIds = [forgedId];
      },
      (state: any) => { state.receipts[0].customerId = "another-customer"; },
      (state: any) => { state.receipts[0].sessionId = "another-session"; },
      (state: any) => { state.receipts[0].sourceAssetId = "another-asset"; },
      (state: any) => { state.receipts[0].sourceDigest = "b".repeat(64); },
      (state: any) => { state.receipts[0].sourcePath = "/different/source.mp4"; },
      (state: any) => { state.receipts[0].sourceStartMs += 1; },
      (state: any) => { state.receipts[0].sourceEndMs -= 1; },
      (state: any) => { state.receipts[0].layer = "vision"; },
      (state: any) => { state.receipts[0].operationKey = "different-operation"; },
    ];
    for (const corrupt of corruptions) {
      const state = structuredClone(pristine);
      corrupt(state);
      await writeFile(path, `${JSON.stringify(state, null, 2)}\n`);
      await expect(createService(new CustomerMaterialLibrary(root, () => true), successfulWorker()).history(scopeA))
        .rejects.toThrow("studio-analysis-state-invalid");
    }
  });

  it("reloads a valid multi-layer job with receipts from multiple attempts", async () => {
    const asset = await importAsset(library, scopeA, "valid-multi-attempt");
    let calls = 0;
    const service = createService(library, fakeWorker(async (job) => {
      calls += 1;
      return receiptFor(job, calls === 1 ? "failed-retryable" : "succeeded");
    }));
    const first = await service.enqueue(scopeA, asset.id, ["technical", "scene"], "valid-multi-attempt");
    const completed = await service.retry(scopeA, first.job.id);

    expect(completed.receipts).toHaveLength(3);
    await expect(createService(new CustomerMaterialLibrary(root, () => true), successfulWorker()).history(scopeA))
      .resolves.toEqual([completed]);
  });

  it("rejects a forged successful state that violates the final-attempt degradation policy", async () => {
    const asset = await importAsset(library, scopeA, "forged-degraded-success", "review-required");
    const source = await library.inspectReferenceAnalysisAsset(
      scopeA.actorId, scopeA.customerId, scopeA.sessionId, asset.id,
    );
    const service = createService(library, fakeWorker(async (job) => receiptFor(
      job,
      job.layer === "scene" ? "succeeded" : "failed-manual-review",
    )));
    await service.enqueueReference(
      scopeA,
      source,
      ["scene", "transcript", "vision"],
      "forged-degraded-success",
    );
    const { path, state: pristine } = await readAnalysisState(root);
    const corruptions = [
      (state: any) => { state.jobs[0].sourceAuthorization = "output-use-authorized"; },
      (state: any) => {
        state.jobs[0].receiptJobIds.pop();
        state.receipts.pop();
      },
      (state: any) => {
        state.receipts[0].status = "failed-manual-review";
        state.receipts[0].failure = { code: "forged-scene-failure" };
        state.receipts[0].confidence = 0;
      },
    ];

    for (const corrupt of corruptions) {
      const state = structuredClone(pristine);
      corrupt(state);
      await writeFile(path, `${JSON.stringify(state, null, 2)}\n`);
      await expect(createService(
        new CustomerMaterialLibrary(root, () => true),
        successfulWorker(),
      ).history(scopeA)).rejects.toThrow("studio-analysis-state-invalid");
    }
  });
});

function createService(
  library: CustomerMaterialLibrary,
  worker: StudioModelWorker,
  resolveSourceInterval: StudioSourceIntervalResolver = async () => ({
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    sourceDurationMs: 10_000,
  }),
) {
  return new StudioAnalysisJobService(library, worker, {
    now: () => "2026-09-12T04:00:00.000Z",
    resolveSourceInterval,
  });
}

async function importAsset(
  library: CustomerMaterialLibrary,
  scope: StudioScope,
  suffix: string,
  usePermission: "allowed" | "review-required" = "allowed",
) {
  return library.importOriginal(scope.actorId, scope.customerId, {
    requestId: `request-${suffix}`,
    bytes: Buffer.from(`synthetic-analysis-source-${suffix}`),
    mediaKind: "video",
    fileName: `${suffix}.mp4`,
    capturedAt: "2026-09-12T03:00:00.000Z",
    sessionId: scope.sessionId,
    viewPermission: "allowed",
    usePermission,
  });
}

function fakeWorker(run: StudioModelWorker["run"]) {
  return {
    run: vi.fn(run),
    health: vi.fn(async () => ({
      mode: "offline-fallback" as const,
      installed: false as const,
      modelName: null,
      checkpointDigest: null,
    })),
  };
}

function successfulWorker() {
  return fakeWorker(async (job) => receiptFor(job));
}

function receiptFor(
  job: ModelAnalysisJobV1,
  status: Extract<StudioAnalysisStatus, "succeeded" | "failed-retryable" | "failed-manual-review"> = "succeeded",
): ModelAnalysisReceiptV1 {
  const { requestedAt: _requestedAt, ...binding } = job;
  return {
    ...binding,
    adapterName: "fixture-analysis-adapter",
    modelName: "fixture-local-model",
    modelVersion: "fixture-v1",
    checkpointDigest: "c".repeat(64),
    inputDigest: job.sourceDigest,
    promptVersion: "fixture-prompt-v1",
    observations: status === "succeeded" ? ["synthetic observation"] : [],
    suggestedTags: [],
    qualitySignals: { fixture: true },
    confidence: status === "succeeded" ? 0.75 : 0,
    warnings: ["synthetic-test-only"],
    startedAt: "2026-09-12T04:00:00.000Z",
    completedAt: "2026-09-12T04:00:00.100Z",
    runtimeMs: 100,
    status,
    failure: status === "succeeded" ? null : { code: `fixture-${status}` },
    synthetic: true,
    providerMode: "offline-fallback",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function findFiles(root: string, match: string, includes = false): Promise<string[]> {
  const found: string[] = [];
  for (const entry of await readdir(root, { withFileTypes: true }).catch(() => [])) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) found.push(...await findFiles(path, match, includes));
    if (entry.isFile() && (includes ? entry.name.includes(match) : entry.name === match)) found.push(path);
  }
  return found;
}

async function readAnalysisState(root: string): Promise<{ path: string; state: any }> {
  const [path] = await findFiles(root, "analysis-state.json");
  return { path: path!, state: JSON.parse(await readFile(path!, "utf8")) };
}

function analysisJobIdFor(scope: StudioScope, operationKey: string): string {
  return `analysis_${createHash("sha256")
    .update(`${scope.customerId}\0${scope.sessionId}\0${scope.actorId}\0${operationKey}`)
    .digest("hex")}`;
}
