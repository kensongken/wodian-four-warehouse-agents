import { spawn } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rename, rm, stat, symlink, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { createContext, runInContext } from "node:vm";

import type { ContentSegment, CreativeSessionEvent, EditRevision, LibraryAsset } from "@wearable/contracts";
import {
  createLocalDispatchRoster,
  commitPromptRevision,
  approveVariant,
  createExportPackage,
  deliverExportPackage,
  JsonContentStore,
  LocalDispatchMailbox,
  renderRevision,
  reviseWithPrompt,
  type CreativeScriptAgent,
  type DeviceCapability,
  type EditPlanAnalyzer,
  type IntentInterpreter,
  type MaterialMatcher,
  type SegmentTagAnalyzer,
  type ShotPlanningAgent,
  type StoryAnalyzer,
} from "@wearable/mobile-agent";
import supertest from "supertest";
import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeEach, describe, expect, it as vitestIt } from "vitest";

import {
  CreativeOperationRegistry,
  createReviewServer,
  type ReviewAppDependencies,
} from "../src/server";
import {
  creativeApiTestBelongsToShard,
  parseCreativeApiShard,
} from "./helpers/creative-api-shard";

const creativeApiShard = parseCreativeApiShard(process.env.CREATIVE_API_SHARD);
const it = Object.assign(
  (title: string, ...args: any[]) => {
    const runner = creativeApiTestBelongsToShard(title, creativeApiShard)
      ? vitestIt
      : vitestIt.skip;
    return (runner as any)(title, ...args);
  },
  {
    each: (...tableArgs: any[]) => {
      const active = (vitestIt.each as any)(...tableArgs);
      const skipped = (vitestIt.skip.each as any)(...tableArgs);
      return (title: string, ...args: any[]) => (
        creativeApiTestBelongsToShard(title, creativeApiShard) ? active : skipped
      )(title, ...args);
    },
  },
) as typeof vitestIt;

const TOKEN = "creative-test-token";
const CUSTOMER_TOKEN = "creative-customer-test-token";
const SEGMENT_ID = "historical_segment_01";
const listeningServers = new Set<ReturnType<typeof createReviewServer>>();
const serverStartups = new WeakMap<ReturnType<typeof createReviewServer>, Promise<void>>();

function request(app: ReturnType<typeof createReviewServer>): any {
  return new Proxy({}, {
    get: (_target, method) => (path: string) => deferredSupertest(
      app,
      String(method),
      path,
    ),
  });
}

function deferredSupertest(
  app: ReturnType<typeof createReviewServer>,
  method: string,
  path: string,
): any {
  const calls: Array<{ name: string; args: unknown[] }> = [];
  let execution: Promise<unknown> | undefined;
  const run = () => {
    execution ??= ensureTestServerListening(app).then(async () => {
      let operation = (supertest(app) as any)[method](path);
      for (const call of calls) operation = operation[call.name](...call.args);
      return operation;
    });
    return execution;
  };
  const deferred = new Proxy({}, {
    get: (_target, property) => {
      if (["then", "catch", "finally"].includes(String(property))) {
        const promise = run() as any;
        return promise[property].bind(promise);
      }
      return (...args: unknown[]) => {
        calls.push({ name: String(property), args });
        return deferred;
      };
    },
  });
  return deferred;
}

function ensureTestServerListening(
  app: ReturnType<typeof createReviewServer>,
): Promise<void> {
  if (app.address()) return Promise.resolve();
  const existing = serverStartups.get(app);
  if (existing) return existing;
  listeningServers.add(app);
  const startup = new Promise<void>((resolve, reject) => {
    const onError = (error: Error) => reject(error);
    app.once("error", onError);
    app.listen(0, "127.0.0.1", () => {
      app.off("error", onError);
      resolve();
    });
  });
  serverStartups.set(app, startup);
  return startup;
}

describe("local creative workflow API", () => {
  let directory: string;
  let library: JsonContentStore;
  let extraDirectories: string[];

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "wearable-creative-api-"));
    extraDirectories = [];
    library = await JsonContentStore.open(join(directory, "library.json"));
    const asset = historicalAsset(directory);
    await writeFile(asset.localPath, "synthetic historical material");
    await library.putAsset(asset);
    await library.putSegments([historicalSegment()]);
  });

  afterEach(async () => {
    for (const server of listeningServers) {
      if (!server.listening) continue;
      await new Promise<void>((resolve, reject) => server.close((error) => {
        if (error) reject(error);
        else resolve();
      }));
    }
    listeningServers.clear();
    await rm(directory, { recursive: true, force: true });
    for (const path of extraDirectories) await rm(path, { recursive: true, force: true });
  });

  it("runs the consented workflow through local simulated dispatch", async () => {
    const app = createApp();
    const created = await post(app, "/api/creative-sessions", {
      customerId: "customer_01",
      title: "Turquoise rework story",
      text: "Make a short video about the turquoise ring rework.",
    }).expect(201);
    const sessionId = created.body.session.id as string;
    const suggestionSetId = created.body.materialSuggestion.id as string;

    expect(created.body).toMatchObject({
      session: {
        stage: "awaiting_material_consent",
        allowedActions: ["accept-material-viewing", "decline-material-viewing"],
      },
      intent: { theme: "Turquoise ring rework" },
      materialSuggestion: { count: 1, hasSameProductMatches: true },
    });
    expect(JSON.stringify(created.body)).not.toContain(SEGMENT_ID);

    const listed = await request(app).get("/api/creative-sessions").expect(200);
    const detail = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(JSON.stringify(listed.body)).not.toContain(SEGMENT_ID);
    expect(JSON.stringify(detail.body)).not.toContain(SEGMENT_ID);
    expect(detail.body.session.allowedActions).toEqual([
      "accept-material-viewing",
      "decline-material-viewing",
    ]);

    await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId,
      decision: "accept",
    }).expect(201);
    const suggestions = await request(app)
      .get(`/api/creative-sessions/${sessionId}/material-suggestions`)
      .expect(200);
    expect(suggestions.body.suggestionSet.suggestions).toEqual([
      expect.objectContaining({ segmentId: SEGMENT_ID, permissionState: "allowed" }),
    ]);

    await post(app, `/api/creative-sessions/${sessionId}/material-selection`, {
      suggestionSetId,
      selectedSegmentIds: [SEGMENT_ID],
    }).expect(201);
    const generated = await post(app, `/api/creative-sessions/${sessionId}/scripts`, {})
      .expect(201);
    expect(generated.body.script).toMatchObject({
      scriptVersion: 1,
      confirmedAt: null,
      sections: expect.arrayContaining([
        expect.objectContaining({
          source: "historical-material",
          referencedSegmentIds: [SEGMENT_ID],
        }),
      ]),
    });

    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);
    const shooting = await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
      .expect(201);
    expect(shooting.body.shotPlan).toMatchObject({
      scriptVersion: 1,
      shotPlanVersion: 1,
      status: "active",
    });
    expect(shooting.body.deviceBriefs.every(
      (brief: { privacyState: string }) => brief.privacyState === "creator",
    )).toBe(true);
    const readyDetail = await request(app)
      .get(`/api/creative-sessions/${sessionId}`)
      .expect(200);
    expect(readyDetail.body.session).toMatchObject({
      allowedActions: ["revise-script", "dispatch"],
      devices: expect.arrayContaining([
        expect.objectContaining({ deviceRole: "watch-director", status: "waiting-dispatch" }),
        expect.objectContaining({ deviceRole: "main-stone-a", status: "waiting-dispatch" }),
        expect.objectContaining({ deviceRole: "main-stone-b", status: "waiting-dispatch" }),
      ]),
    });

    const dispatched = await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
      confirmStart: true,
    }).expect(201);
    expect(dispatched.body.receipts).toHaveLength(3);
    expect(dispatched.body.receipts.every(
      (receipt: { transport: string; status: string }) =>
        receipt.transport === "simulated" && receipt.status === "received",
    )).toBe(true);
  });

  it("shares one late create operation across timeout reconciliation and retry", async () => {
    const entered = deferred<void>();
    const release = deferred<void>();
    let clock = 10_000;
    let interpreterCalls = 0;
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          interpreterCalls += 1;
          entered.resolve();
          await release.promise;
          return baseInterpreter.interpret(input);
        },
      },
    }), {
      creativeOperationTtlMs: 1,
      now: () => clock,
    });
    const body = {
      customerId: "customer_timeout",
      title: "Late create",
      text: "Create exactly one session after a lost response.",
    };
    const operationKey = "creative-create-timeout-0001";

    const original = post(app, "/api/creative-sessions", body, operationKey)
      .expect(201).then((value) => value);
    await entered.promise;
    await request(app).get("/api/creative-sessions").expect(200, { sessions: [] });
    clock += 60_000;
    const retry = post(app, "/api/creative-sessions", body, operationKey)
      .expect(201).then((value) => value);
    release.resolve();
    const [originalResponse, retryResponse] = await Promise.all([original, retry]);

    expect(interpreterCalls).toBe(1);
    expect(retryResponse.body).toEqual(originalResponse.body);
    expect((await library.listCreativeSessions()).filter(
      (session) => session.customerId === "customer_timeout",
    )).toHaveLength(1);
  });

  it("returns stable receipt errors and never acknowledges an inflight or retryable operation", async () => {
    const entered = deferred<void>();
    const release = deferred<void>();
    let attempts = 0;
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          attempts += 1;
          if (attempts === 1) {
            entered.resolve();
            await release.promise;
            throw new Error("internal-error");
          }
          return baseInterpreter.interpret(input);
        },
      },
    }));
    const client = await openCreativeClient(app);
    const operationKey = "creative-receipt-state-0001";
    const body = {
      customerId: "customer_receipt_state",
      title: "Receipt state",
      text: "Keep every receipt state honest.",
    };

    await acknowledgeReceipt(app, client, "creative-receipt-missing-0001")
      .expect(404, { error: "creative-operation-receipt-not-found" });
    const pending = postAsClient(app, client, "/api/creative-sessions", body, operationKey)
      .expect(503, { error: "creative-intent-interpreter-retryable" })
      .then((value: unknown) => value);
    await entered.promise;
    const inflight = await readReceipt(app, client, operationKey).expect(200);
    expect(inflight.body.receipt).toMatchObject({ status: "inflight" });
    await acknowledgeReceipt(app, client, operationKey)
      .expect(409, { error: "creative-operation-receipt-inflight" });
    release.resolve();
    await pending;

    const retryable = await readReceipt(app, client, operationKey).expect(200);
    expect(retryable.body.receipt).toMatchObject({
      status: "retryable",
      response: { status: 503, body: { error: "creative-intent-interpreter-retryable" } },
    });
    await acknowledgeReceipt(app, client, operationKey)
      .expect(409, { error: "creative-operation-receipt-retryable" });
    await postAsClient(app, client, "/api/creative-sessions", body, operationKey).expect(201);
    const completed = await readReceipt(app, client, operationKey).expect(200);
    expect(completed.body.receipt).toMatchObject({
      status: "completed",
      response: expect.objectContaining({ status: 201 }),
    });
  });

  it("binds receipt query and acknowledgement to one server-issued client instance", async () => {
    const app = createApp();
    const owner = await openCreativeClient(app);
    const otherClient = await openCreativeClient(app);
    expect(owner).not.toBe(otherClient);
    expect(owner).toMatch(/^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$/);
    const operationKey = "creative-owned-receipt-0001";
    const body = {
      customerId: "customer_owned_receipt",
      title: "Owned receipt",
      text: "Only the originating browser client can inspect or acknowledge this.",
    };
    const created = await postAsClient(
      app,
      owner,
      "/api/creative-sessions",
      body,
      operationKey,
    ).expect(201);

    const receipt = await readReceipt(app, owner, operationKey).expect(200);
    expect(receipt.body.receipt).toMatchObject({
      status: "completed",
      clientInstanceId: owner,
      request: { method: "POST", path: "/api/creative-sessions", body },
      response: { status: 201, body: created.body },
    });
    await readReceipt(app, otherClient, operationKey)
      .expect(404, { error: "creative-operation-receipt-not-found" });
    await acknowledgeReceipt(app, otherClient, operationKey)
      .expect(404, { error: "creative-operation-receipt-not-found" });
    await acknowledgeReceipt(app, owner, operationKey)
      .expect(200, { acknowledged: true });
  });

  it("rotates a server-side client claim so one copied credential cannot stay active twice", async () => {
    const app = createApp();
    const opened = await request(app).get("/api/session").expect(200);
    const clientInstanceId = opened.body.clientInstanceId as string;
    const firstClaim = opened.body.clientClaim as string;
    expect(firstClaim).toEqual(expect.any(String));

    const resumed = await request(app)
      .get("/api/session")
      .set("x-client-instance", clientInstanceId)
      .set("x-client-claim", firstClaim)
      .expect(200);
    expect(resumed.body.clientInstanceId).toBe(clientInstanceId);
    expect(resumed.body.clientClaim).not.toBe(firstClaim);

    await request(app)
      .post("/api/creative-sessions")
      .set("content-type", "application/json")
      .set("x-review-token", TOKEN)
      .set("x-client-instance", clientInstanceId)
      .set("x-client-claim", firstClaim)
      .set("x-operation-key", "stale-client-claim-operation-0001")
      .send({ customerId: "customer_stale_claim", title: "Stale", text: "Must not run." })
      .expect(409, { error: "creative-client-claim-invalid" });
    await request(app)
      .get("/api/creative-operation-receipts/stale-client-claim-operation-0001")
      .set("x-review-token", TOKEN)
      .set("x-client-instance", clientInstanceId)
      .set("x-client-claim", firstClaim)
      .expect(409, { error: "creative-client-claim-invalid" });
    await request(app)
      .delete("/api/creative-operation-receipts/stale-client-claim-operation-0001")
      .set("content-type", "application/json")
      .set("x-review-token", TOKEN)
      .set("x-client-instance", clientInstanceId)
      .set("x-client-claim", firstClaim)
      .send({})
      .expect(409, { error: "creative-client-claim-invalid" });
  });

  it("returns a stable recovery response when a restarted server no longer knows the old owner", async () => {
    const firstApp = createApp();
    const opened = await request(firstApp).get("/api/session").expect(200);
    const restartedApp = createApp();

    await request(restartedApp)
      .get("/api/session")
      .set("x-client-instance", opened.body.clientInstanceId)
      .set("x-client-claim", opened.body.clientClaim)
      .expect(409, {
        error: "creative-client-recovery-required",
        recovery: { reason: "server-owner-state-unavailable", automaticRetryAllowed: false },
      });
  });

  it("makes completion, waiter release, acknowledgement and cleanup one atomic receipt lifecycle", async () => {
    let clock = 1_000;
    const registry = new CreativeOperationRegistry({
      creativeOperationTtlMs: 10,
      now: () => clock,
    });
    const executeEntered = deferred<void>();
    const finishExecution = deferred<void>();
    const leasePromise = registry.run(
      "designer_01",
      "client_instance_atomic_01",
      "creative-atomic-receipt-0001",
      {
        method: "POST",
        path: "/api/creative-sessions",
        body: { customerId: "customer_atomic" },
      },
      async () => {
        executeEntered.resolve();
        await finishExecution.promise;
        return { status: 201, body: { session: { id: "session_atomic" } } };
      },
    );
    await executeEntered.promise;
    expect(registry.acknowledge(
      "designer_01",
      "client_instance_atomic_01",
      "creative-atomic-receipt-0001",
    )).toEqual({ status: 409, body: { error: "creative-operation-receipt-inflight" } });

    finishExecution.resolve();
    const lease = await leasePromise;
    expect(registry.acknowledge(
      "designer_01",
      "client_instance_atomic_01",
      "creative-atomic-receipt-0001",
    )).toEqual({ status: 409, body: { error: "creative-operation-receipt-waiters-active" } });
    lease.release();
    expect(registry.acknowledge(
      "designer_01",
      "client_instance_atomic_01",
      "creative-atomic-receipt-0001",
    )).toEqual({ status: 200, body: { acknowledged: true } });

    clock += 11;
    expect(registry.inspect(
      "designer_01",
      "client_instance_atomic_01",
      "creative-atomic-receipt-0001",
    )).toEqual({ status: 404, body: { error: "creative-operation-receipt-not-found" } });
  });

  it("rejects an operation key reused for a different creative body while the first is in flight", async () => {
    const entered = deferred<void>();
    const release = deferred<void>();
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          entered.resolve();
          await release.promise;
          return baseInterpreter.interpret(input);
        },
      },
    }));
    const operationKey = "creative-create-conflict-0001";
    const original = post(app, "/api/creative-sessions", {
      customerId: "customer_conflict",
      title: "Original title",
      text: "Original body.",
    }, operationKey).expect(201).then((value) => value);
    await entered.promise;

    const conflict = post(app, "/api/creative-sessions", {
      customerId: "customer_conflict",
      title: "Edited title",
      text: "Edited body.",
    }, operationKey).expect(409, { error: "creative-operation-key-conflict" });

    release.resolve();
    await Promise.all([original, conflict]);
  });

  it("retains a completed receipt until reconciliation acknowledges it, then applies the TTL", async () => {
    let clock = 10_000;
    let interpreterCalls = 0;
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          interpreterCalls += 1;
          return baseInterpreter.interpret(input);
        },
      },
    }), {
      creativeOperationTtlMs: 100,
      now: () => clock,
    });
    const operationKey = "creative-create-ttl-0001";
    const originalBody = {
      customerId: "customer_ttl",
      title: "First receipt",
      text: "First completed operation.",
    };
    const original = await post(app, "/api/creative-sessions", originalBody, operationKey)
      .expect(201);
    clock += 101;
    const replay = await post(app, "/api/creative-sessions", {
      text: originalBody.text,
      title: originalBody.title,
      customerId: originalBody.customerId,
    }, operationKey)
      .expect(201);
    expect(replay.body).toEqual(original.body);
    expect(interpreterCalls).toBe(1);

    await acknowledgeReceipt(app, await defaultCreativeClient(app), operationKey).expect(200);
    clock += 101;
    await post(app, "/api/creative-sessions", {
      customerId: "customer_ttl",
      title: "After expiry",
      text: "A new operation after receipt cleanup.",
    }, operationKey).expect(201);

    expect(interpreterCalls).toBe(2);
    expect((await library.listCreativeSessions()).filter(
      (session) => session.customerId === "customer_ttl",
    )).toHaveLength(2);
  });

  it("never evicts 256 completed but unacknowledged receipts to admit a new mutation", async () => {
    let interpreterCalls = 0;
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          interpreterCalls += 1;
          return baseInterpreter.interpret(input);
        },
      },
    }));

    for (let index = 0; index < 256; index += 1) {
      await post(app, "/api/creative-sessions", {
        customerId: `capacity_${index}`,
        title: "",
        text: "Rejected before mutation.",
      }, `creative-capacity-unacked-${index}`)
        .expect(400, { error: "creative-title-required" });
    }

    const blockedBody = {
      customerId: "customer_blocked_by_capacity",
      title: "Must not execute",
      text: "Wait for an acknowledgement slot.",
    };
    const blockedKey = "creative-capacity-blocked-0257";
    await post(app, "/api/creative-sessions", blockedBody, blockedKey)
      .expect(503, { error: "creative-operation-registry-capacity" });
    await post(app, "/api/creative-sessions", blockedBody, blockedKey)
      .expect(503, { error: "creative-operation-registry-capacity" });
    expect(interpreterCalls).toBe(0);
    expect(await library.listCreativeSessions()).toEqual([]);

    await acknowledgeReceipt(
      app,
      await defaultCreativeClient(app),
      "creative-capacity-unacked-0",
    ).expect(200);
    await post(app, "/api/creative-sessions", blockedBody, blockedKey).expect(201);

    expect(interpreterCalls).toBe(1);
    expect((await library.listCreativeSessions()).filter(
      (session) => session.customerId === "customer_blocked_by_capacity",
    )).toHaveLength(1);
  }, 30_000);

  it("rejects an operation key reused on another route with the same body", async () => {
    const app = createApp();
    const { sessionId } = await sessionReadyForScript(app);
    const operationKey = "creative-route-conflict-0001";

    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}, operationKey)
      .expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {}, operationKey)
      .expect(409, { error: "creative-operation-key-conflict" });
  });

  it("releases an unavailable intent dependency receipt so the exact same key can recover", async () => {
    let interpreterCalls = 0;
    const recoveredInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          interpreterCalls += 1;
          if (interpreterCalls === 1) throw new Error("intent-service-unavailable");
          return recoveredInterpreter.interpret(input);
        },
      },
    }));
    const body = {
      customerId: "customer_error",
      title: "Stable failure",
      text: "Replay the first response.",
    };
    const operationKey = "creative-error-replay-0001";

    await post(app, "/api/creative-sessions", body, operationKey)
      .expect(503, { error: "creative-intent-interpreter-retryable" });
    await post(app, "/api/creative-sessions", body, operationKey).expect(201);

    expect(interpreterCalls).toBe(2);
    expect((await library.listCreativeSessions()).filter(
      (session) => session.customerId === "customer_error",
    )).toHaveLength(1);
  });

  it("releases a transient dependency failure after concurrent waiters so the same key can retry", async () => {
    const entered = deferred<void>();
    const release = deferred<void>();
    let interpreterCalls = 0;
    const baseInterpreter = intentInterpreter();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async (input) => {
          interpreterCalls += 1;
          if (interpreterCalls === 1) {
            entered.resolve();
            await release.promise;
            throw new Error("internal-error");
          }
          return baseInterpreter.interpret(input);
        },
      },
    }));
    const body = {
      customerId: "customer_retryable_500",
      title: "Retry after transient failure",
      text: "Create one session on the successful retry.",
    };
    const operationKey = "creative-transient-500-retry";

    await new Promise<void>((resolve, reject) => {
      app.once("error", reject);
      app.listen(0, "127.0.0.1", resolve);
    });
    try {
      const first = post(app, "/api/creative-sessions", body, operationKey)
        .expect(503, { error: "creative-intent-interpreter-retryable" }).then((value) => value);
      const concurrent = post(app, "/api/creative-sessions", body, operationKey)
        .expect(503, { error: "creative-intent-interpreter-retryable" }).then((value) => value);
      await entered.promise;
      await new Promise<void>((resolve) => setTimeout(resolve, 25));
      release.resolve();
      await Promise.all([first, concurrent]);
      expect(interpreterCalls).toBe(1);

      await post(app, "/api/creative-sessions", body, operationKey).expect(201);

      expect(interpreterCalls).toBe(2);
      expect((await library.listCreativeSessions()).filter(
        (session) => session.customerId === "customer_retryable_500",
      )).toHaveLength(1);
    } finally {
      await new Promise<void>((resolve) => app.close(() => resolve()));
    }
  });

  it("restores the persisted creative view at every resumable stage", async () => {
    const app = createApp();
    const created = await createSession(app);
    const sessionId = created.body.session.id as string;
    const suggestionSetId = created.body.materialSuggestion.id as string;

    const consent = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(consent.body.session).toMatchObject({
      stage: "awaiting_material_consent",
      materialSuggestion: { id: suggestionSetId, count: 1 },
      suggestionSet: null,
      materialDecision: null,
    });
    expect(JSON.stringify(consent.body)).not.toContain(SEGMENT_ID);
    expect(JSON.stringify(consent.body)).not.toContain(directory);

    await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId,
      decision: "accept",
    }).expect(201);
    const selection = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(selection.body.session).toMatchObject({
      stage: "reviewing_materials",
      materialDecision: { action: "accept-view", suggestionSetId },
      suggestionSet: {
        id: suggestionSetId,
        suggestions: [expect.objectContaining({
          segmentId: SEGMENT_ID,
          previewUrl: `/api/creative-sessions/${sessionId}/material-suggestions/${SEGMENT_ID}/preview`,
          previewKind: "video",
        })],
      },
    });
    expect(JSON.stringify(selection.body)).not.toContain("thumbnailPath");
    expect(JSON.stringify(selection.body)).not.toContain(directory);

    await post(app, `/api/creative-sessions/${sessionId}/material-selection`, {
      suggestionSetId,
      selectedSegmentIds: [SEGMENT_ID],
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    const script = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(script.body.session).toMatchObject({
      stage: "awaiting_script_confirmation",
      materialDecision: { action: "confirm-selection", selectedSegmentIds: [SEGMENT_ID] },
      creativeBrief: { confirmedSegmentIds: [SEGMENT_ID] },
      script: { scriptVersion: 1, confirmedAt: null },
      shotPlan: null,
    });

    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);
    const confirmed = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(confirmed.body.session).toMatchObject({
      stage: "ready_to_dispatch",
      script: { scriptVersion: 1, confirmedAt: expect.any(String) },
      shotPlan: null,
    });

    await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {}).expect(201);
    const dispatch = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(dispatch.body.session).toMatchObject({
      stage: "ready_to_dispatch",
      shotPlan: { shotPlanVersion: 1, steps: expect.any(Array) },
      directorCards: expect.any(Array),
      deviceBriefs: expect.any(Array),
      receipts: [],
      devices: expect.arrayContaining([
        expect.objectContaining({ deviceRole: "watch-director", status: "waiting-dispatch", battery: { label: "模拟电量", percent: expect.any(Number) } }),
        expect.objectContaining({ deviceRole: "main-stone-a", status: "waiting-dispatch", battery: { label: "模拟电量", percent: expect.any(Number) } }),
        expect.objectContaining({ deviceRole: "main-stone-b", status: "waiting-dispatch", battery: { label: "模拟电量", percent: expect.any(Number) } }),
      ]),
    });

    await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
      confirmStart: true,
    }).expect(201);
    const shooting = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(shooting.body.session).toMatchObject({
      stage: "shooting",
      shotPlan: { shotPlanVersion: 1 },
      receipts: expect.arrayContaining([
        expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
        expect.objectContaining({ deviceRole: "main-stone-a", status: "received" }),
        expect.objectContaining({ deviceRole: "main-stone-b", status: "received" }),
      ]),
      devices: expect.arrayContaining([
        expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
        expect.objectContaining({ deviceRole: "main-stone-a", status: "received" }),
        expect.objectContaining({ deviceRole: "main-stone-b", status: "received" }),
      ]),
    });
  });

  it("serves only accepted current authorized suggestion previews without exposing paths", async () => {
    const app = createApp();
    const created = await createSession(app);
    const sessionId = created.body.session.id as string;
    const suggestionSetId = created.body.materialSuggestion.id as string;
    const previewPath = `/api/creative-sessions/${sessionId}/material-suggestions/${SEGMENT_ID}/preview`;

    await authenticatedPreview(app, previewPath)
      .expect(400, { error: "material-view-acceptance-required" });
    await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId,
      decision: "accept",
    }).expect(201);

    await request(app).get(previewPath)
      .set("host", "127.0.0.1")
      .set("origin", "http://127.0.0.1")
      .expect(400, { error: "review-token-required" });
    await request(app).get(previewPath)
      .set("host", "127.0.0.1")
      .set("origin", "http://attacker.example")
      .set("x-review-token", TOKEN)
      .expect(400, { error: "trusted-origin-required" });
    await request(app).get(previewPath)
      .set("host", "127.0.0.1")
      .set("origin", "http://127.0.0.1")
      .set("x-review-token", "wrong-token")
      .expect(400, { error: "review-token-required" });
    await request(app).get(previewPath)
      .set("host", "127.0.0.1")
      .set("x-review-token", TOKEN)
      .expect(400, { error: "trusted-origin-required" });
    await request(app).get(previewPath)
      .set("host", "attacker.example")
      .set("origin", "http://attacker.example")
      .set("x-review-token", TOKEN)
      .expect(400, { error: "loopback-host-required" });

    const preview = await authenticatedPreview(app, previewPath)
      .expect(200)
      .expect("content-type", "video/mp4");
    expect(Buffer.from(preview.body).toString()).toBe("synthetic historical material");
    await authenticatedPreview(
      app,
      `/api/creative-sessions/${sessionId}/material-suggestions/not-shown/preview`,
    )
      .expect(404, { error: "material-preview-not-found" });
    await authenticatedPreview(
      app,
      `/api/creative-sessions/${sessionId}/material-suggestions/%252e%252e/preview`,
    )
      .expect(404, { error: "material-preview-not-found" });
  });

  it("never reveals candidate identities after declining material viewing", async () => {
    const app = createApp();
    const created = await createSession(app);
    const sessionId = created.body.session.id as string;
    const suggestionSetId = created.body.materialSuggestion.id as string;

    const declined = await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId,
      decision: "decline",
    }).expect(201);
    const detail = await request(app)
      .get(`/api/creative-sessions/${sessionId}`)
      .expect(200);

    expect(declined.body.decision).toMatchObject({ action: "decline-view", suggestionVersion: 1 });
    expect(detail.body.session).toMatchObject({
      stage: "materials_declined",
      suggestionSet: null,
      materialDecision: { action: "decline-view", suggestionVersion: 1 },
    });
    for (const body of [declined.body, detail.body]) {
      const json = JSON.stringify(body);
      expect(json).not.toContain(SEGMENT_ID);
      expect(json).not.toContain("shownSegmentIds");
      expect(json).not.toContain("selectedSegmentIds");
      expect(json).not.toContain("rejectedSegmentIds");
    }
  });

  it("derives each device state from matching current task receipts instead of receipt count", async () => {
    const app = createApp();
    const { sessionId } = await sessionReadyForScript(app);
    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {}).expect(201);
    const watchCard = (await library.listDirectorCards())[0]!;
    const base = {
      sessionId,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      deviceId: "watch_sim_01",
      deviceRole: "watch-director" as const,
      transport: "simulated" as const,
      status: "received" as const,
      createdAt: "2026-09-05T09:00:00.000Z",
    };
    await library.putDispatchReceipt({
      ...base,
      id: `dispatch_${watchCard.id}`,
      idempotencyKey: `receipt:${watchCard.id}`,
    });
    await library.putDispatchReceipt({
      ...base,
      id: "extra_watch_receipt_01",
      idempotencyKey: "receipt:extra-watch-01",
    });
    await library.putDispatchReceipt({
      ...base,
      id: "extra_watch_receipt_02",
      idempotencyKey: "receipt:extra-watch-02",
    });

    const detail = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(detail.body.session.receipts).toHaveLength(3);
    expect(detail.body.session.devices).toEqual(expect.arrayContaining([
      expect.objectContaining({ deviceRole: "watch-director", status: "received" }),
      expect.objectContaining({ deviceRole: "main-stone-a", status: "waiting-dispatch" }),
      expect.objectContaining({ deviceRole: "main-stone-b", status: "waiting-dispatch" }),
    ]));
  });

  it("keeps viewing consent separate from exact segment selection", async () => {
    const app = createApp();
    const created = await createSession(app);
    const sessionId = created.body.session.id as string;
    const suggestionSetId = created.body.materialSuggestion.id as string;

    await request(app)
      .get(`/api/creative-sessions/${sessionId}/material-suggestions`)
      .expect(400, { error: "material-view-acceptance-required" });

    const accepted = await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId,
      decision: "accept",
    }).expect(201);
    expect(JSON.stringify(accepted.body)).not.toContain(SEGMENT_ID);
    await post(app, `/api/creative-sessions/${sessionId}/material-selection`, {
      suggestionSetId,
      selectedSegmentIds: ["unselected_segment"],
    }).expect(400, { error: "material-selection-segment-not-shown" });
  });

  it("requires the exact current script confirmation before planning", async () => {
    const app = createApp();
    const { sessionId, suggestionSetId } = await sessionReadyForScript(app);

    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
      .expect(400, { error: "confirmed-script-required" });
    expect((await library.listCreativeSessions())[0]?.deviceBindings).toEqual([]);
    await post(app, `/api/creative-sessions/${sessionId}/script-revisions`, {
      prompt: "Shorten the opening.",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(400, { error: "script-version-not-current" });

    expect(suggestionSetId).toBeTruthy();
  });

  it("turns a transcript-bearing voice rewrite into a new script before storing the field event", async () => {
    const app = createApp();
    const { sessionId } = await sessionReadyForScript(app);
    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);
    const shooting = await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
      .expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
      confirmStart: true,
    }).expect(201);
    const stepId = shooting.body.shotPlan.steps[0].id as string;

    const revised = await post(app, `/api/creative-sessions/${sessionId}/events`, {
      action: "voice-rewrite",
      transcript: "Make the opening more direct.",
      operationId: "voice-op-01",
      stepId,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
    }).expect(201);

    expect(revised.body).toMatchObject({
      resultingScriptVersion: 2,
      event: {
        action: "voice-rewrite",
        transcript: "Make the opening more direct.",
        scriptVersion: 1,
      },
    });
    expect((await library.listCreativeSessionEvents()).at(-1)).toMatchObject({
      idempotencyKey: expect.stringContaining("voice-op-01"),
      action: "voice-rewrite",
    });
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "awaiting_script_confirmation",
      currentScriptVersion: 2,
      currentShotPlanVersion: 0,
    });
    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
  });

  it("stores a non-rewrite field note without changing the script version", async () => {
    const app = createApp();
    const { sessionId } = await sessionReadyForScript(app);
    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);
    const shooting = await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
      .expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
      confirmStart: true,
    }).expect(201);

    const response = await post(app, `/api/creative-sessions/${sessionId}/events`, {
      action: "note",
      reason: "Light changed.",
      operationId: "note-op-01",
      stepId: shooting.body.shotPlan.steps[0].id,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
    }).expect(201);

    expect(response.body).toMatchObject({
      resultingScriptVersion: 1,
      event: { action: "note", reason: "Light changed." },
    });
  });

  it("replays an ordinary V1 action with its original result after the session reaches V3", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const body = fieldEventBody(stepId, {
      action: "note",
      operationId: "stable-note-op-01",
      reason: "Remember this framing.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });
    const original = await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);
    const v2StepId = await rewriteAndResumeShooting(app, sessionId, stepId, 1, "advance-note-v2");
    await rewriteAtCurrentShooting(app, sessionId, v2StepId, 2, "advance-note-v3");

    const replay = await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);

    expect(original.body.resultingScriptVersion).toBe(1);
    expect(replay.body).toEqual(original.body);
  });

  it("replays an exact legacy key but does not merge a distinct valid operation", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const event: CreativeSessionEvent = {
      id: "legacy_field_event_01",
      sessionId,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      stepId,
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
      action: "note",
      actorId: "designer_01",
      reason: "Legacy local note.",
      transcript: null,
      idempotencyKey: `${sessionId}:${stepId}:legacy-note-op-01`,
      take: 1,
      createdAt: "2026-09-05T08:00:00.000Z",
    };
    await library.putCreativeSessionEvent(event);

    const replay = await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "note",
      operationId: "legacy-note-op-01",
      reason: "Legacy local note.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    })).expect(201);
    const distinct = await post(
      app,
      `/api/creative-sessions/${sessionId}/events`,
      fieldEventBody(stepId, {
        action: "note",
        operationId: "legacy-note-op-01-distinct",
        reason: "Legacy local note.",
        deviceId: "watch_sim_01",
        deviceRole: "watch-director",
      }),
    ).expect(201);

    expect(replay.body).toEqual({ event, resultingScriptVersion: 1 });
    expect(distinct.body.event.id).not.toBe(event.id);
    expect((await library.listCreativeSessionEvents()).filter(
      (stored) => stored.action === "note",
    )).toHaveLength(2);
  });

  it("replays a V1 voice rewrite with result V2 after the session reaches V3", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const body = fieldEventBody(stepId, {
      action: "voice-rewrite",
      operationId: "stable-voice-op-01",
      transcript: "Use the quiet opening.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });
    const original = await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);
    const v2StepId = await resumeShootingAtScript(app, sessionId, 2);
    await rewriteAtCurrentShooting(app, sessionId, v2StepId, 2, "advance-voice-v3");

    const replay = await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);

    expect(original.body.resultingScriptVersion).toBe(2);
    expect(replay.body).toEqual(original.body);
  });

  it("increments a retake from the trusted persisted take", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);

    const retake = await post(app, `/api/creative-sessions/${sessionId}/events`,
      fieldEventBody(stepId, {
        action: "retake",
        operationId: "retake-op-01",
        deviceId: "watch_sim_01",
        deviceRole: "watch-director",
      })).expect(201);
    const note = await post(app, `/api/creative-sessions/${sessionId}/events`,
      fieldEventBody(stepId, {
        action: "note",
        operationId: "note-after-retake",
        reason: "Second take kept the same light.",
        deviceId: "watch_sim_01",
        deviceRole: "watch-director",
      })).expect(201);

    expect(retake.body.event.take).toBe(1);
    expect(note.body.event.take).toBe(2);
    expect((await library.listCreativeSessionEvents()).filter(
      (event) => event.action === "retake",
    )).toEqual([expect.objectContaining({ take: 1 })]);
  });

  it("enters a persisted privacy pause and blocks later field actions", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);

    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "privacy-paused",
      operationId: "pause-op-01",
      reason: "Customer requested privacy.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    })).expect(201);

    const detail = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(detail.body.session).toMatchObject({
      stage: "paused",
      privacyMode: "paused",
      allowedActions: ["resume-privacy"],
    });
    expect(detail.body.session.devices.every(
      (device: { status: string }) => device.status === "privacy-paused",
    )).toBe(true);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed",
      operationId: "completed-after-pause",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    })).expect(400, { error: "privacy-paused" });
  });

  it("keeps a concurrent duplicate result stable when V2 and V3 interleave before dedupe", async () => {
    const firstApp = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(firstApp);
    const secondLibrary = await JsonContentStore.open(join(directory, "library.json"));
    const secondApp = createReviewServer(creativeDependencies({ store: secondLibrary }));
    const secondEntered = deferred<void>();
    const releaseSecond = deferred<void>();
    const secondCommit = secondLibrary.commitCreativeBundle.bind(secondLibrary);
    secondLibrary.commitCreativeBundle = async (value) => {
      if (value.events?.some((event) => event.action === "note")) {
        secondEntered.resolve();
        await releaseSecond.promise;
      }
      return secondCommit(value);
    };
    const body = fieldEventBody(stepId, {
      action: "note",
      operationId: "duplicate-op-01",
      reason: "Same exact operation.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });

    const second = post(secondApp, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201).then((value) => value);
    await secondEntered.promise;
    const firstResponse = await post(firstApp, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);
    const v2StepId = await rewriteAndResumeShooting(
      firstApp,
      sessionId,
      stepId,
      1,
      "interleave-v2",
    );
    await rewriteAtCurrentShooting(firstApp, sessionId, v2StepId, 2, "interleave-v3");
    releaseSecond.resolve();
    const secondResponse = await second;

    expect(firstResponse.body.event).toEqual(secondResponse.body.event);
    expect(firstResponse.body.resultingScriptVersion).toBe(1);
    expect(secondResponse.body.resultingScriptVersion).toBe(1);
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    expect((await reopened.listCreativeSessionEvents()).filter(
      (event) => event.idempotencyKey.includes("duplicate-op-01"),
    )).toHaveLength(1);
  });

  it("rejects reuse of an operation id with a changed device identity at the request boundary", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const base = fieldEventBody(stepId, {
      action: "note",
      operationId: "device-conflict-op-01",
      reason: "Identity must stay exact.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });
    await post(app, `/api/creative-sessions/${sessionId}/events`, base).expect(201);

    await post(app, `/api/creative-sessions/${sessionId}/events`, {
      ...base,
      deviceId: "stone_a_sim_01",
      deviceRole: "main-stone-a",
    }).expect(409, { error: "creative-operation-key-conflict" });
  });

  it("rolls back a transiently failed voice rewrite bundle and retries the same operation safely", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const originalCommit = library.commitCreativeBundle.bind(library);
    let failVoiceCommit = true;
    library.commitCreativeBundle = async (value) => {
      if (failVoiceCommit && value.events?.some((event) => event.action === "voice-rewrite")) {
        failVoiceCommit = false;
        throw new Error("internal-error");
      }
      return originalCommit(value);
    };
    const body = fieldEventBody(stepId, {
      action: "voice-rewrite",
      operationId: "voice-atomic-op-01",
      transcript: "Use a quieter opening.",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });

    await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(500, { error: "internal-error" });
    expect((await library.listScriptPackages()).filter(
      (script) => script.scriptVersion === 2,
    )).toEqual([]);
    expect((await library.listCreativeSessionEvents()).filter(
      (event) => event.action === "voice-rewrite",
    )).toEqual([]);
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "shooting",
      currentScriptVersion: 1,
      currentShotPlanVersion: 1,
    });
    expect((await library.listShotPlans())[0]?.status).toBe("active");

    const retried = await post(app, `/api/creative-sessions/${sessionId}/events`, body)
      .expect(201);
    expect(retried.body.resultingScriptVersion).toBe(2);
    expect((await library.listScriptPackages()).filter(
      (script) => script.scriptVersion === 2,
    )).toHaveLength(1);
    expect((await library.listCreativeSessionEvents()).filter(
      (event) => event.action === "voice-rewrite",
    )).toHaveLength(1);
  });

  it("rejects dispatch before creating a current shooting package without calling the roster", async () => {
    let rosterCalls = 0;
    const dependencies = creativeDependencies({
      onCreateRoster: () => { rosterCalls += 1; },
    });
    const app = createReviewServer(dependencies);
    const { sessionId } = await sessionReadyForScript(app);
    await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
      scriptVersion: 1,
    }).expect(201);

    await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
      confirmStart: true,
    }).expect(400, { error: "current-shot-plan-required" });
    expect(rosterCalls).toBe(0);
    expect(await library.listDispatchReceipts()).toEqual([]);
  });

  it("fails every creative route closed when creative dependencies are absent", async () => {
    const app = createReviewServer({
      library,
      interpreter: { interpret: async () => { throw new Error("unused"); } },
      outputDir: directory,
      designerId: "designer_01",
      reviewToken: TOKEN,
    });

    await request(app)
      .get("/api/creative-sessions")
      .expect(400, { error: "creative-dependencies-required" });
    await post(app, "/api/creative-sessions", {})
      .expect(400, { error: "creative-dependencies-required" });
  });

  it("retains all POST security and routing guards", async () => {
    const app = createApp();
    const unauthorizedOperationKey = "creative-unauthorized-0001";

    await request(app)
      .post("/api/creative-sessions")
      .set("host", "attacker.example")
      .set("content-type", "application/json")
      .set("x-review-token", TOKEN)
      .send({})
      .expect(400, { error: "loopback-host-required" });
    await request(app)
      .post("/api/creative-sessions")
      .set("content-type", "application/json")
      .set("x-operation-key", unauthorizedOperationKey)
      .send({})
      .expect(400, { error: "review-token-required" });
    await post(app, "/api/creative-sessions", {
      customerId: "customer_after_unauthorized",
      title: "Authorized operation",
      text: "The rejected request must not reserve its key.",
    }, unauthorizedOperationKey).expect(201);
    await request(app)
      .post("/api/creative-sessions")
      .set("content-type", "application/json")
      .set("x-review-token", TOKEN)
      .send({ customerId: "customer_01", title: "No key", text: "Rejected before mutation." })
      .expect(400, { error: "creative-operation-key-required" });
    await post(app, "/api/creative-sessions", {
      customerId: "customer_01",
      title: "Large",
      text: "x".repeat(1024 * 1024 + 1),
    }).expect(400, { error: "request-body-too-large" });
    await post(app, "/api/creative-route-that-does-not-exist", {})
      .expect(404, { error: "route-not-found" });
  });

  it("rejects a field event whose header key differs from its body operation id", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const before = await library.listCreativeSessionEvents();

    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      operationId: "field-body-operation-0001",
      reason: "This event must not be recorded.",
    }), "field-header-operation-0002")
      .expect(400, { error: "creative-event-operation-key-mismatch" });

    expect(await library.listCreativeSessionEvents()).toEqual(before);
  });

  it("retries the same create operation after matcher failure without creating a second session", async () => {
    let matcherCalls = 0;
    const app = createReviewServer(creativeDependencies({
      materialMatcher: {
        rank: async ({ segments }) => {
          matcherCalls += 1;
          if (matcherCalls === 1) throw new Error("matcher-temporarily-unavailable");
          return segments.map((segment) => ({ segmentId: segment.id, reason: "recovered", score: 1 }));
        },
      },
    }));
    const body = { customerId: "customer_atomic", title: "Atomic create", text: "One session only" };
    const operationKey = "atomic-create-after-matcher-0001";

    await post(app, "/api/creative-sessions", body, operationKey)
      .expect(503, { error: "creative-material-matcher-retryable" });
    expect(await library.listCreativeSessions()).toHaveLength(1);
    const retried = await post(app, "/api/creative-sessions", body, operationKey).expect(201);

    expect(await library.listCreativeSessions()).toHaveLength(1);
    expect(await library.listCreativeIntents()).toHaveLength(1);
    expect(retried.body.session.id).toBe((await library.listCreativeSessions())[0]?.id);
    expect(matcherCalls).toBe(2);
  });

  it("answers an incomplete intent inside the same creative session", async () => {
    let calls = 0;
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async ({ text }) => {
          calls += 1;
          return calls === 1
            ? { theme: text }
            : { platforms: ["wechat-channels"], audience: "collectors", shootConditions: ["bench"], prohibitedAreas: ["faces"] };
        },
      },
    }));
    const created = await post(app, "/api/creative-sessions", {
      customerId: "customer_followup", title: "Follow-up", text: "想拍一条返工视频",
    }).expect(201);
    const sessionId = created.body.session.id as string;
    expect(created.body).toMatchObject({ session: { stage: "draft_intent" } });
    expect(created.body.questions).toContain("platforms");

    const answered = await post(app, `/api/creative-sessions/${sessionId}/intent-answers`, {
      text: "发视频号，给收藏客看",
      answers: { platforms: "wechat-channels", audience: "collectors" },
    }).expect(201);
    expect(answered.body).toMatchObject({ session: { id: sessionId, stage: "awaiting_material_consent" } });
    expect(await library.listCreativeSessions()).toHaveLength(1);
    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
  });

  it("continues material matching after a stored intent answer without creating version three", async () => {
    let interpretCalls = 0;
    let matcherCalls = 0;
    const baseMatcher = materialMatcher();
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async () => {
          interpretCalls += 1;
          return interpretCalls === 1
            ? { theme: "A recoverable answer", shootConditions: [] }
            : { platforms: ["wechat-channels"], audience: "collectors",
                shootConditions: ["bench"], prohibitedAreas: ["faces"] };
        },
      },
      materialMatcher: {
        rank: async (input) => {
          matcherCalls += 1;
          if (matcherCalls === 1) throw new Error("matcher-temporarily-unavailable");
          return baseMatcher.rank(input);
        },
      },
    }));
    const client = await openCreativeClient(app);
    const created = await postAsClient(app, client, "/api/creative-sessions", {
      customerId: "customer_recoverable_answer", title: "Recoverable answer",
      text: "Start an incomplete draft",
    }, "create-recoverable-answer-01").expect(201);
    const sessionId = created.body.session.id as string;
    const path = `/api/creative-sessions/${sessionId}/intent-answers`;
    const body = { text: "Use the bench", answers: { shootConditions: "bench" } };
    const operationKey = "recoverable-intent-answer-01";

    await postAsClient(app, client, path, body, operationKey)
      .expect(503, { error: "creative-material-matcher-retryable" });
    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    const pending = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(pending.body.session).toMatchObject({ stage: "draft_intent" });
    expect(pending.body.session.allowedActions).not.toContain("accept-material-viewing");
    expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey, creativeSessionId: sessionId, intentVersion: 2, phase: "answer-stored",
    }));

    const retried = await postAsClient(app, client, path, body, operationKey).expect(201);
    expect(retried.body).toMatchObject({
      session: { id: sessionId, stage: "awaiting_material_consent" },
      materialSuggestion: expect.objectContaining({ count: 1 }),
    });
    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    expect(matcherCalls).toBe(2);
    expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey, creativeSessionId: sessionId, intentVersion: 2,
      suggestionSetId: expect.any(String), phase: "complete",
    }));

    await postAsClient(app, client, path, {
      text: "Use another room", answers: { shootConditions: "another room" },
    }, operationKey).expect(409, { error: "creative-operation-key-conflict" });
    expect(await library.listCreativeIntents()).toHaveLength(2);
  });

  it("returns 503 then resumes the same intent answer after eligible catalog metadata races", async () => {
    let interpreterCalls = 0;
    let matcherCalls = 0;
    const matcherEntered = deferred<void>();
    const releaseMatcher = deferred<void>();
    const changedTranscript = "The API catalog transcript changed while matching.";
    const wrappedStore = new Proxy(library, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof library.commitCreativeBundle>[0],
        ) => {
          try {
            return await target.commitCreativeBundle(bundle);
          } catch (cause) {
            throw new Error("catalog-commit-adapter-failed", { cause });
          }
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const app = createReviewServer(creativeDependencies({
      store: wrappedStore,
      intentInterpreter: {
        interpret: async () => {
          interpreterCalls += 1;
          return interpreterCalls === 1
            ? {
                platforms: ["wechat-channels"], audience: "Jewelry collectors",
                theme: "Catalog race", products: [{ id: "product_01", status: "suggested" }],
                shootConditions: [], prohibitedAreas: ["customer faces"],
                tone: "direct", durationSeconds: 30,
              }
            : { shootConditions: ["bench"] };
        },
      },
      materialMatcher: {
        rank: async ({ segments }) => {
          matcherCalls += 1;
          if (matcherCalls === 1) {
            matcherEntered.resolve();
            await releaseMatcher.promise;
          }
          return segments.map((segment) => ({
            segmentId: segment.id, reason: "fresh eligible catalog", score: 1,
          }));
        },
      },
    }));
    const client = await openCreativeClient(app);
    const created = await postAsClient(app, client, "/api/creative-sessions", {
      customerId: "customer_catalog_race", title: "Catalog race", text: "Start incomplete",
    }, "create-catalog-race-answer-01").expect(201);
    const sessionId = created.body.session.id as string;
    const path = `/api/creative-sessions/${sessionId}/intent-answers`;
    const body = { text: "The bench is ready", answers: { shootConditions: "bench" } };
    const operationKey = "recoverable-catalog-race-answer-01";

    const firstResponse = postAsClient(app, client, path, body, operationKey)
      .expect(503, { error: "creative-material-catalog-race-retryable" })
      .then((value: unknown) => value);
    await matcherEntered.promise;
    const writer = await JsonContentStore.open(join(directory, "library.json"));
    const segment = (await writer.listSegments()).find((item) => item.id === SEGMENT_ID)!;
    await writer.putSegments([{
      ...segment,
      tags: { ...segment.tags, topics: ["late API catalog topic"], transcript: changedTranscript },
    }]);
    releaseMatcher.resolve();
    await firstResponse;

    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    expect(await library.listMaterialSuggestionSets()).toEqual([]);
    expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey, creativeSessionId: sessionId, intentVersion: 2,
      suggestionSetId: null, phase: "answer-stored",
    }));

    const retried = await postAsClient(app, client, path, body, operationKey).expect(201);
    expect(retried.body).toMatchObject({
      session: { id: sessionId, currentIntentVersion: 2, stage: "awaiting_material_consent" },
      materialSuggestion: { count: 1 },
    });
    expect(matcherCalls).toBe(2);
    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    expect(await library.listMaterialSuggestionSets()).toEqual([
      expect.objectContaining({
        sessionId,
        suggestions: [expect.objectContaining({
          segmentId: SEGMENT_ID,
        })],
      }),
    ]);
    expect((await library.listSegments()).find((item) => item.id === SEGMENT_ID)?.tags.transcript)
      .toBe(changedTranscript);
    expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey, creativeSessionId: sessionId, intentVersion: 2,
      suggestionSetId: expect.any(String), phase: "complete",
    }));
  });

  it("keeps an adapter's forged material error message terminal at HTTP", async () => {
    const adapter = new Proxy(library, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof library.commitCreativeBundle>[0],
        ) => {
          if ((bundle.materialSuggestionSets?.length ?? 0) > 0) {
            throw new Error("material-no-longer-eligible");
          }
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    let interpreterCalls = 0;
    const app = createReviewServer(creativeDependencies({
      store: adapter,
      intentInterpreter: {
        interpret: async () => {
          interpreterCalls += 1;
          return interpreterCalls === 1
            ? {
                platforms: ["wechat-channels"], audience: "Jewelry collectors",
                theme: "Forged failure", products: [], shootConditions: [],
                prohibitedAreas: ["customer faces"], tone: "direct", durationSeconds: 30,
              }
            : { shootConditions: ["bench"] };
        },
      },
    }));
    const client = await openCreativeClient(app);
    const created = await postAsClient(app, client, "/api/creative-sessions", {
      customerId: "customer_forged_catalog_error", title: "Forged failure", text: "Start incomplete",
    }, "create-forged-catalog-error-01").expect(201);

    await postAsClient(
      app,
      client,
      `/api/creative-sessions/${created.body.session.id}/intent-answers`,
      { text: "Bench ready", answers: { shootConditions: "bench" } },
      "forged-catalog-error-01",
    ).expect(400, { error: "material-no-longer-eligible" });
  });

  it("keeps a throwing cause getter from forging a retryable HTTP receipt", async () => {
    const failure = new Error("ordinary-commit-failure");
    Object.defineProperty(failure, "cause", {
      get() { throw new Error("stale-content-store"); },
    });
    const adapter = new Proxy(library, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof library.commitCreativeBundle>[0],
        ) => {
          if ((bundle.materialSuggestionSets?.length ?? 0) > 0) throw failure;
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    let interpreterCalls = 0;
    const app = createReviewServer(creativeDependencies({
      store: adapter,
      intentInterpreter: {
        interpret: async () => {
          interpreterCalls += 1;
          return interpreterCalls === 1
            ? {
                platforms: ["wechat-channels"], audience: "Jewelry collectors",
                theme: "Getter failure", products: [], shootConditions: [],
                prohibitedAreas: ["customer faces"], tone: "direct", durationSeconds: 30,
              }
            : { shootConditions: ["bench"] };
        },
      },
    }));
    const client = await openCreativeClient(app);
    const created = await postAsClient(app, client, "/api/creative-sessions", {
      customerId: "customer_getter_error", title: "Getter failure", text: "Start incomplete",
    }, "create-getter-error-01").expect(201);
    const operationKey = "getter-error-answer-01";

    await postAsClient(
      app,
      client,
      `/api/creative-sessions/${created.body.session.id}/intent-answers`,
      { text: "Bench ready", answers: { shootConditions: "bench" } },
      operationKey,
    ).expect(400, { error: "ordinary-commit-failure" });
    const receipt = await readReceipt(app, client, operationKey).expect(200);
    expect(receipt.body.receipt).toMatchObject({
      status: "completed",
      response: { status: 400, body: { error: "ordinary-commit-failure" } },
    });
  });

  it("keeps ordinary non-racing material eligibility failures terminal", async () => {
    const app = createReviewServer(creativeDependencies({
      materialMatcher: {
        rank: async () => [{
          segmentId: "not-in-the-eligible-catalog", reason: "invalid matcher output", score: 1,
        }],
      },
    }));

    await post(app, "/api/creative-sessions", {
      customerId: "customer_invalid_match", title: "Invalid match", text: "Complete request",
    }, "ordinary-ineligible-match-01")
      .expect(400, { error: "material-no-longer-eligible" });
  });

  it("keeps a transient intent interpreter failure retryable under the same receipt key", async () => {
    let calls = 0;
    const app = createReviewServer(creativeDependencies({
      intentInterpreter: {
        interpret: async () => {
          calls += 1;
          if (calls === 1) return { theme: "Interpreter retry", shootConditions: [] };
          if (calls === 2) throw new Error("model-timeout");
          return { platforms: ["wechat-channels"], audience: "collectors",
            shootConditions: ["bench"], prohibitedAreas: ["faces"] };
        },
      },
    }));
    const client = await openCreativeClient(app);
    const created = await postAsClient(app, client, "/api/creative-sessions", {
      customerId: "customer_interpreter_retry", title: "Interpreter retry",
      text: "Start incomplete",
    }, "create-interpreter-retry-01").expect(201);
    const path = `/api/creative-sessions/${created.body.session.id}/intent-answers`;
    const body = { text: "Complete it", answers: { shootConditions: "bench" } };

    await postAsClient(app, client, path, body, "interpreter-retry-01")
      .expect(503, { error: "creative-intent-interpreter-retryable" });
    const retry = await postAsClient(app, client, path, body, "interpreter-retry-01").expect(201);

    expect(retry.body.session).toMatchObject({ currentIntentVersion: 2, stage: "awaiting_material_consent" });
    expect((await library.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    expect(calls).toBe(3);
  });

  it("releases a stale content-store receipt so the same operation key can retry", async () => {
    let failOnce = true;
    const staleOnceStore = new Proxy(library, {
      get(target, property) {
        if (property === "listCreativeSessions") return async () => {
          if (failOnce) {
            failOnce = false;
            throw new Error("stale-content-store");
          }
          return target.listCreativeSessions();
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const app = createReviewServer(creativeDependencies({ store: staleOnceStore }));
    const client = await openCreativeClient(app);
    const body = { customerId: "customer_stale_retry", title: "Stale retry", text: "Retry safely" };

    await postAsClient(app, client, "/api/creative-sessions", body, "stale-store-retry-01")
      .expect(503, { error: "stale-content-store" });
    await postAsClient(app, client, "/api/creative-sessions", body, "stale-store-retry-01")
      .expect(201);
    expect((await library.listCreativeSessions()).filter((session) =>
      session.customerId === "customer_stale_retry")).toHaveLength(1);
  });

  it("releases an explicit content-store lock timeout receipt for the exact same-key retry", async () => {
    let failOnce = true;
    const lockedOnceStore = new Proxy(library, {
      get(target, property) {
        if (property === "listCreativeSessions") return async () => {
          if (failOnce) {
            failOnce = false;
            throw new Error("content-store-lock-timeout");
          }
          return target.listCreativeSessions();
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const app = createReviewServer(creativeDependencies({ store: lockedOnceStore }));
    const client = await openCreativeClient(app);
    const body = { customerId: "customer_lock_retry", title: "Lock retry", text: "Retry safely" };

    await postAsClient(app, client, "/api/creative-sessions", body, "lock-timeout-retry-01")
      .expect(503, { error: "content-store-lock-timeout" });
    await postAsClient(app, client, "/api/creative-sessions", body, "lock-timeout-retry-01")
      .expect(201);
    expect((await library.listCreativeSessions()).filter((session) =>
      session.customerId === "customer_lock_retry")).toHaveLength(1);
  });

  it("resumes a privacy-paused session only after an explicit customer safety confirmation", async () => {
    const privacyDevices = [
      testPrivacyDevice("watch_sim_01"),
      testPrivacyDevice("stone_a_sim_01"),
      testPrivacyDevice("stone_b_sim_01"),
    ];
    const app = createReviewServer(creativeDependencies({ privacyDevices }));
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "privacy-paused", operationId: "privacy-pause-final-wave-0001",
      reason: "a person entered the frame",
    })).expect(201);
    expect(privacyDevices.every((device) => device.isPrivacyPaused())).toBe(true);

    await post(app, `/api/creative-sessions/${sessionId}/privacy-resume`, {
      actorRole: "customer", confirmSafe: true,
    }).expect(409, { error: "customer-credential-required" });
    const client = await defaultCreativeClient(app);
    await postAsCustomer(app, client, `/api/creative-sessions/${sessionId}/privacy-resume`, {
      actorRole: "customer", confirmSafe: true,
    }, "privacy-resume-wrong-customer-01", "customer_other", "other-customer-token")
      .expect(409, { error: "customer-credential-mismatch" });
    await postAsCustomer(app, client, `/api/creative-sessions/${sessionId}/privacy-resume`, {
      actorRole: "customer", confirmSafe: true,
    }, "privacy-resume-designer-impersonation-01", "designer_01", TOKEN)
      .expect(409, { error: "customer-credential-invalid" });
    const resumed = await postAsCustomer(
      app, client, `/api/creative-sessions/${sessionId}/privacy-resume`,
      { actorRole: "system", confirmSafe: true }, "privacy-resume-customer-01",
      "customer_01", CUSTOMER_TOKEN,
    ).expect(201);
    expect(resumed.body.session).toMatchObject({ id: sessionId, stage: "shooting", privacyMode: "creator" });
    expect(privacyDevices.every((device) => !device.isPrivacyPaused())).toBe(true);
    expect(await library.listAuditEvents(sessionId)).toContainEqual(expect.objectContaining({
      action: "creative-session-transitioned", actorId: "customer_01",
      after: expect.objectContaining({ stage: "shooting" }),
    }));
    await postAsCustomer(app, client, `/api/creative-sessions/${sessionId}/privacy-resume`, {
      confirmSafe: true,
    }, "privacy-resume-again-01", "customer_01", CUSTOMER_TOKEN)
      .expect(400, { error: "privacy-resume-not-paused" });
  });

  it("requires a fresh exact post-shoot history decision for the current cut", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "post-shoot-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    const baseline = await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    expect(baseline.body.steps).toEqual([
      expect.objectContaining({ stepId, state: "completed-returned" }),
    ]);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {})
      .expect(201);
    const suggestionSetId = proposed.body.materialSuggestion.id as string;
    expect(proposed.body.materialSuggestion).toMatchObject({ purpose: "post-shoot-edit", count: 1 });
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "accept",
    }).expect(201);
    const selected = await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
      suggestionSetId, selectedSegmentIds: [SEGMENT_ID],
    }).expect(201);
    expect(selected.body.decision).toMatchObject({ purpose: "post-shoot-edit", selectedSegmentIds: [SEGMENT_ID] });
    expect(selected.body.decision.selectedSegmentIds).toEqual([SEGMENT_ID]);
  }, 300_000);

  it("turns a completed A-0 capture return into a real library asset and editorial processing task", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "capture-completed-a0-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);

    const returned = await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    expect(returned.body).toMatchObject({
      boundary: "A-0 local simulation",
      assets: [expect.objectContaining({ origin: "new", captureContext: expect.objectContaining({ creativeSessionId: sessionId, stepId }) })],
      processingTask: expect.objectContaining({ creativeSessionId: sessionId, status: "ready-for-editorial" }),
    });
    expect(await library.listAssets()).toHaveLength(2);
    expect(await library.listCreativeProcessingTasks()).toEqual([
      expect.objectContaining({ creativeSessionId: sessionId, status: "ready-for-editorial" }),
    ]);
  });

  it("ends an all-skipped public shooting flow without forcing a reshoot", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "skipped", operationId: "skip-all-a0-0001", reason: "customer chose to end without this shot",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    const ended = await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    expect(ended.body).toMatchObject({ boundary: "A-0 local simulation",
      session: { id: sessionId, stage: "editorial_review",
        editorialBatchId: expect.stringContaining("creative_batch_") },
      reason: "all planned steps skipped; editorial handoff has no captured assets",
      forcedReshootCount: 0 });
  });

  it("builds and replays one persisted reviewable four-video handoff through public HTTP", async () => {
    const app = createApp();
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "http-editorial-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    const captureOne = await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    expect(captureOne.status).toBe(201);

    const endBody = { mode: "a0-simulated-phone-return" };
    const endKey = "http-editorial-end-0001";
    const end = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/end-handoff`, endBody, endKey).expect(201);
    expect(end.body.processingTask.status).toBe("ready-for-editorial");
    expect(end.body).not.toHaveProperty("stories");
    expect(end.body).not.toHaveProperty("artifacts");

    const beforeConsent = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(beforeConsent.body.session.processingTask).toEqual(end.body.processingTask);
    expect(beforeConsent.body.session.stories).toEqual([]);
    const proposed = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {},
      "http-editorial-post-suggest-0001").expect(201);
    const suggestionSetId = proposed.body.materialSuggestion.id as string;
    await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
        suggestionSetId, decision: "accept",
      }, "http-editorial-post-view-0001").expect(201);
    const selected = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
        suggestionSetId, selectedSegmentIds: [SEGMENT_ID],
      }, "http-editorial-post-select-0001").expect(201);
    expect(selected.body).not.toHaveProperty("processingTask");

    const processKey = "http-editorial-process-0001";
    const processed = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/editorial-process`, {}, processKey).expect(201);
    expect(processed.body.processingTask).toMatchObject({ status: "reviewable", publishedClaimed: false });
    const detail = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(detail.body.session.processingTask).toEqual(processed.body.processingTask);
    expect(detail.body.session.stories.length).toBeGreaterThan(0);
    expect(detail.body.session.revisions.length).toBeGreaterThan(0);
    expect(detail.body.session.artifacts).toHaveLength(
      detail.body.session.revisions.length * 4,
    );
    const storedArtifacts = new Map((await library.listAllArtifacts()).map((item) => [item.id, item]));
    for (const artifact of detail.body.session.artifacts) {
      expect(artifact).not.toHaveProperty("videoPath");
      const stored = storedArtifacts.get(artifact.id)!;
      expect((await stat(stored.videoPath)).size).toBeGreaterThan(0);
      await probeHttpVideo(stored.videoPath);
      await request(app)
        .get(artifact.mediaUrl)
        .set("host", "127.0.0.1")
        .set("referer", "http://127.0.0.1/")
        .set("x-review-token", TOKEN)
        .expect(200)
        .expect("content-type", "video/mp4");
    }

    const countsBefore = await persistedEditorialCounts(library);
    const processReplay = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/editorial-process`, {}, processKey).expect(201);
    expect(processReplay.body).toEqual(processed.body);
    expect(await persistedEditorialCounts(library)).toEqual(countsBefore);
    const replay = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/end-handoff`, endBody, endKey).expect(201);
    expect(replay.body).toEqual(end.body);
    expect(await persistedEditorialCounts(library)).toEqual(countsBefore);
  }, 300_000);

  it("accepts an explicit post-shoot no-history decision and produces a new-material-only cut", async () => {
    const app = createApp();
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "decline-history-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app,
      `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
    const suggestionSetId = proposed.body.materialSuggestion.id as string;
    const declined = await post(app,
      `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
        suggestionSetId, decision: "decline",
      }).expect(201);
    expect(declined.body.decision).toMatchObject({
      action: "decline-view",
    });
    for (const key of ["shownSegmentIds", "selectedSegmentIds", "rejectedSegmentIds"]) {
      expect(declined.body.decision).not.toHaveProperty(key);
    }

    const processed = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/editorial-process`, {},
      "decline-history-process-0001").expect(201);
    expect(processed.body.processingTask.status).toBe("reviewable");
    for (const story of processed.body.stories) expect(story.historicalSegmentIds).toEqual([]);
  }, 300_000);

  it("releases a retryable editorial receipt so the same key resumes once after restart", async () => {
    let renderAttempts = 0;
    const renderer: NonNullable<ReviewAppDependencies["renderer"]> = async (...args) => {
      renderAttempts += 1;
      if (renderAttempts === 1) throw new Error("transient-render-failure");
      return renderRevision(...args);
    };
    const app = createReviewServer(creativeDependencies({ renderer }));
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "retry-process-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app,
      `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
    expectCreativeDtoHasNoHostPaths(proposed.body);
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId: proposed.body.materialSuggestion.id, decision: "decline",
    }).expect(201);

    const path = `/api/creative-sessions/${sessionId}/editorial-process`;
    const operationKey = "retry-process-same-key-0001";
    const failed = await postAsClient(app, client, path, {}, operationKey).expect(503);
    expect(failed.body.processingTask).toMatchObject({
      status: "failed-retryable", errorCode: "transient-render-failure",
    });
    expect((await readReceipt(app, client, operationKey).expect(200)).body.receipt.status)
      .toBe("retryable");

    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    const restarted = createReviewServer(creativeDependencies({ store: reopened, renderer }));
    const restartedClient = await defaultCreativeClient(restarted);
    const recovered = await postAsClient(restarted, restartedClient, path, {}, operationKey).expect(201);
    expect(recovered.body.processingTask.status).toBe("reviewable");
    const tasks = (await reopened.listCreativeProcessingTasks())
      .filter((task) => task.operationKey === operationKey);
    expect(tasks).toHaveLength(1);
    const artifacts = await reopened.listAllArtifacts();
    expect(new Set(artifacts.map((artifact) => artifact.id)).size).toBe(artifacts.length);
    expect(artifacts).toHaveLength(recovered.body.revisions.length * 4);
  }, 300_000);

  it("persists deterministic story failures and resumes the exact transient manual restart", async () => {
    let storyAttempts = 0;
    const normalStoryAnalyzer = editorialAnalyzers().storyAnalyzer;
    const storyAnalyzer: StoryAnalyzer = { propose: async (segments) => {
      storyAttempts += 1;
      if (storyAttempts === 1) return [];
      if (storyAttempts === 2) throw new Error("temporarily-unavailable");
      return normalStoryAnalyzer.propose(segments);
    } };
    const app = createReviewServer(creativeDependencies({
      storyAnalyzer,
    }));
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "manual-story-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app,
      `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId: proposed.body.materialSuggestion.id, decision: "decline",
    }).expect(201);

    const path = `/api/creative-sessions/${sessionId}/editorial-process`;
    const operationKey = "manual-story-process-0001";
    const first = await postAsClient(app, client, path, {}, operationKey).expect(409);
    expect(first.body.processingTask).toMatchObject({
      status: "failed-manual-review", errorCode: "creative-editorial-no-supported-story",
    });
    const replay = await postAsClient(app, client, path, {}, operationKey).expect(409);
    expect(replay.body).toEqual(first.body);
    expect(storyAttempts).toBe(1);
    expect((await readReceipt(app, client, operationKey).expect(200)).body.receipt.status)
      .toBe("completed");
    await postAsClient(app, client, path, {}, "manual-story-bypass-0001")
      .expect(409, { error: "creative-editorial-manual-confirmation-required" });

    const resetPath = `/api/creative-sessions/${sessionId}/editorial-manual-reset`;
    const processOperationKey = "manual-story-restarted-process-0001";
    const confirmation = {
      failedTaskId: first.body.processingTask.id,
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: "2026-09-08T09:00:00.000Z",
      processOperationKey,
    };
    await postAsClient(app, client, resetPath, { ...confirmation, confirmedBy: "other_designer" },
      "manual-story-wrong-designer-0001").expect(400, { error: "manual-restart-designer-mismatch" });
    const reset = await postAsClient(app, client, resetPath, confirmation,
      "manual-story-confirm-0001").expect(201);
    const resetReplay = await postAsClient(app, client, resetPath, confirmation,
      "manual-story-confirm-0001").expect(201);
    expect(resetReplay.body).toEqual(reset.body);
    expect(reset.body.authorization).toMatchObject({
      creativeSessionId: sessionId,
      failedTaskId: first.body.processingTask.id,
      confirmedBy: "designer_01",
      processOperationKey,
      status: "authorized",
    });
    expect(reset.body.authorization.confirmedAt).not.toBe(confirmation.confirmedAt);
    await postAsClient(app, client, resetPath, { ...confirmation, failedTaskId: "other_failed_task" },
      "manual-story-confirm-0001").expect(409, { error: "creative-operation-key-conflict" });
    await postAsClient(app, client, resetPath, { ...confirmation, failedTaskId: "other_failed_task",
      processOperationKey: "other-process-key" }, "manual-story-cross-task-0001")
      .expect(409, { error: "creative-editorial-manual-task-not-current" });

    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    const restartedApp = createReviewServer(creativeDependencies({ store: reopened, storyAnalyzer }));
    const restartedClient = await defaultCreativeClient(restartedApp);
    const interrupted = await postAsClient(restartedApp, restartedClient, path,
      { manualRestartAuthorizationId: reset.body.authorization.id }, processOperationKey);
    expect(interrupted.status, JSON.stringify(interrupted.body)).toBe(503);
    expect(interrupted.body.processingTask).toMatchObject({
      status: "failed-retryable",
      phase: "story",
      operationKey: processOperationKey,
    });
    expect(storyAttempts).toBe(2);
    expect((await readReceipt(restartedApp, restartedClient, processOperationKey).expect(200))
      .body.receipt).toMatchObject({
        status: "retryable",
        request: {
          method: "POST",
          path,
          body: { manualRestartAuthorizationId: reset.body.authorization.id },
        },
      });
    expect((await reopened.listAuditEvents(sessionId))).toEqual(expect.arrayContaining([
      expect.objectContaining({ action: "creative-editorial-manual-restart-authorized",
        after: expect.objectContaining({ failedTaskId: first.body.processingTask.id, processOperationKey }) }),
      expect.objectContaining({ action: "creative-editorial-manual-restart-consumed",
        after: expect.objectContaining({ failedTaskId: first.body.processingTask.id,
          restartedTaskId: interrupted.body.processingTask.id }) }),
    ]));
    const reopenedAgain = await JsonContentStore.open(join(directory, "library.json"));
    const restartedAgain = createReviewServer(creativeDependencies({ store: reopenedAgain, storyAnalyzer }));
    const restartedAgainClient = await defaultCreativeClient(restartedAgain);
    const resumed = await postAsClient(restartedAgain, restartedAgainClient, path,
      { manualRestartAuthorizationId: reset.body.authorization.id }, processOperationKey).expect(201);
    expect(resumed.body.processingTask).toMatchObject({
      id: interrupted.body.processingTask.id,
      status: "reviewable",
      operationKey: processOperationKey,
    });
    expect(storyAttempts).toBe(3);
    const lostResponseStore = await JsonContentStore.open(join(directory, "library.json"));
    const lostResponseApp = createReviewServer(creativeDependencies({
      store: lostResponseStore,
      storyAnalyzer: { propose: async () => { throw new Error("must-not-repeat-story-after-reviewable"); } },
    }));
    const lostResponseClient = await defaultCreativeClient(lostResponseApp);
    const lostResponseReplay = await postAsClient(lostResponseApp, lostResponseClient, path,
      { manualRestartAuthorizationId: reset.body.authorization.id }, processOperationKey).expect(201);
    expect(lostResponseReplay.body).toEqual(resumed.body);
    expect(storyAttempts).toBe(3);
    const durableResetReplay = await postAsClient(restartedAgain, restartedAgainClient,
      resetPath, confirmation, "manual-story-confirm-0001").expect(201);
    expect(durableResetReplay.body.authorization).toMatchObject({
      id: reset.body.authorization.id,
      status: "consumed",
      restartedTaskId: resumed.body.processingTask.id,
    });
    expect((await reopenedAgain.listAuditEvents(sessionId)).filter((event) =>
      event.action === "creative-editorial-manual-restart-authorized")).toHaveLength(1);

    const completedReplacement = (await reopenedAgain.listCreativeProcessingTasks()).find((task) =>
      task.id === resumed.body.processingTask.id)!;
    await reopenedAgain.putCreativeProcessingTask({ ...completedReplacement,
      status: "failed-manual-review", phase: "render", errorCode: "creative-editorial-output-review-required",
      updatedAt: "2026-09-08T09:10:00.000Z" });
    const manualReplayStore = await JsonContentStore.open(join(directory, "library.json"));
    const manualReplayApp = createReviewServer(creativeDependencies({ store: manualReplayStore, storyAnalyzer }));
    const manualReplayClient = await defaultCreativeClient(manualReplayApp);
    const manualReplay = await postAsClient(manualReplayApp, manualReplayClient, path,
      { manualRestartAuthorizationId: reset.body.authorization.id }, processOperationKey).expect(409);
    expect(manualReplay.body.processingTask).toMatchObject({
      id: completedReplacement.id,
      status: "failed-manual-review",
      errorCode: "creative-editorial-output-review-required",
    });
    expect(storyAttempts).toBe(3);

    await manualReplayStore.putCreativeProcessingTask({ ...completedReplacement,
      status: "failed-retryable", phase: "render", errorCode: "transient-render-failure",
      updatedAt: "2026-09-08T09:11:00.000Z" });
    const retryableReplayStore = await JsonContentStore.open(join(directory, "library.json"));
    const retryableReplayApp = createReviewServer(creativeDependencies({ store: retryableReplayStore, storyAnalyzer }));
    const retryableReplayClient = await defaultCreativeClient(retryableReplayApp);
    const retryableReplay = await postAsClient(retryableReplayApp, retryableReplayClient, path,
      { manualRestartAuthorizationId: reset.body.authorization.id }, processOperationKey);
    expect(retryableReplay.status, JSON.stringify(retryableReplay.body)).toBe(409);
    expect(retryableReplay.body.processingTask).toMatchObject({
      id: completedReplacement.id,
      status: "failed-manual-review",
      errorCode: "immutable-audit-conflict",
    });
    expect(storyAttempts).toBe(3);
  }, 300_000);

  it("keeps unresolved post-shoot authorization as a non-retryable business failure", async () => {
    const app = createApp();
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "unresolved-process-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const path = `/api/creative-sessions/${sessionId}/editorial-process`;
    const operationKey = "unresolved-process-0001";
    await postAsClient(app, client, path, {}, operationKey)
      .expect(400, { error: "post-shoot-authorization-required" });
    await postAsClient(app, client, path, {}, operationKey)
      .expect(400, { error: "post-shoot-authorization-required" });
    expect((await readReceipt(app, client, operationKey).expect(200)).body.receipt.status)
      .toBe("completed");
  }, 300_000);

  it("keeps creative review, revisions, approval, export, delivery, and media session-scoped without host paths", async () => {
    const publisher = {
      deliver: async () => ({ receiptId: "scoped-local-receipt", receivedAt: new Date().toISOString() }),
    };
    const app = createReviewServer({ ...creativeDependencies(), phonePublisher: publisher });
    const client = await defaultCreativeClient(app);
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "scoped-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    const capture = await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
      deviceRole: "watch-director", carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    expectCreativeDtoHasNoHostPaths(capture.body);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app,
      `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId: proposed.body.materialSuggestion.id, decision: "decline",
    }).expect(201);
    await postAsClient(app, client, `/api/creative-sessions/${sessionId}/editorial-process`, {},
      "scoped-process-0001").expect(201);

    const detail = await request(app).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expectCreativeDtoHasNoHostPaths(detail.body);
    const creativeBatchId = detail.body.session.processingTask.editorialBatchId as string;
    await request(app).get(`/api/batches/${encodeURIComponent(creativeBatchId)}`).expect(404);
    const legacyLibrary = await request(app).get("/api/library").expect(200);
    expectCreativeDtoHasNoHostPaths(legacyLibrary.body);
    expect(legacyLibrary.body.assets).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ batchId: creativeBatchId }),
    ]));
    expect(legacyLibrary.body.segments).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ batchId: creativeBatchId }),
    ]));
    const story = detail.body.session.stories[0];
    const oldArtifact = detail.body.session.artifacts.find(
      (artifact: { kind: string }) => artifact.kind === "wechat-channels");
    await authenticatedPreview(app, oldArtifact.mediaUrl).expect(200);
    const legacyReview = await request(app).get("/api/review").expect(200);
    expect(legacyReview.body.stories).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ id: story.id }),
    ]));

    const other = await createSession(app);
    await authenticatedPreview(app,
      `/api/creative-sessions/${other.body.session.id}/artifacts/${oldArtifact.id}/media`).expect(404);
    await authenticatedPreview(app,
      `/api/creative-sessions/${sessionId}/artifacts/artifact_guessed/media`).expect(404);
    await authenticatedPreview(app, `/api/artifacts/${oldArtifact.id}/media`).expect(404);
    await postAsClient(app, client, `/api/stories/${story.id}/revisions`, {
      prompt: "global route must not edit creative stories",
    }, "global-creative-revise-blocked-0001").expect(404);
    await postAsClient(app, client,
      `/api/creative-sessions/${other.body.session.id}/stories/${story.id}/revisions`, {
        prompt: "another session must not edit this story",
      }, "cross-session-creative-revise-blocked-0001").expect(404);

    const revised = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/stories/${story.id}/revisions`, {
        prompt: "视频号开头先说结果。",
      }, "scoped-revise-0001");
    expect(revised.status, JSON.stringify(revised.body)).toBe(201);
    expectCreativeDtoHasNoHostPaths(revised.body);
    await authenticatedPreview(app, oldArtifact.mediaUrl).expect(404);
    const currentArtifact = revised.body.artifacts.find(
      (artifact: { kind: string }) => artifact.kind === "wechat-channels");
    await authenticatedPreview(app, currentArtifact.mediaUrl).expect(200);
    await postAsClient(app, client, `/api/artifacts/${currentArtifact.id}/approve`, {},
      "global-creative-approve-blocked-0001").expect(404);
    await postAsClient(app, client,
      `/api/creative-sessions/${other.body.session.id}/artifacts/${currentArtifact.id}/approve`, {},
      "cross-session-creative-approve-blocked-0001").expect(404);
    const approvalResponse = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/artifacts/${currentArtifact.id}/approve`, {},
      "scoped-approve-0001");
    expect(approvalResponse.status, JSON.stringify(approvalResponse.body)).toBe(201);
    const approval = approvalResponse.body.approval;
    const legacyApproved = await request(app).get("/api/approved").expect(200);
    expect(legacyApproved.body.approvals).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ id: approval.id }),
    ]));
    await postAsClient(app, client, `/api/approvals/${approval.id}/export`, {},
      "global-creative-export-blocked-0001").expect(404);
    const exported = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/approvals/${approval.id}/export`, {},
      "scoped-export-0001");
    expect(exported.status, JSON.stringify(exported.body)).toBe(201);
    expectCreativeDtoHasNoHostPaths(exported.body);
    await postAsClient(app, client,
      `/api/creative-sessions/${other.body.session.id}/approvals/${approval.id}/export`, {},
      "cross-session-creative-export-blocked-0001").expect(404);
    await postAsClient(app, client, `/api/export-packages/${exported.body.package.id}/deliver`, {},
      "global-creative-deliver-blocked-0001").expect(404);
    await postAsClient(app, client,
      `/api/creative-sessions/${other.body.session.id}/export-packages/${exported.body.package.id}/deliver`, {},
      "cross-session-creative-deliver-blocked-0001").expect(404);
    const delivered = await postAsClient(app, client,
      `/api/creative-sessions/${sessionId}/export-packages/${exported.body.package.id}/deliver`, {},
      "scoped-deliver-0001");
    expect(delivered.status, JSON.stringify(delivered.body)).toBe(201);
    expect(delivered.body.delivery).toMatchObject({ status: "received", publishedClaimed: false });
    expectCreativeDtoHasNoHostPaths(delivered.body);
  }, 120_000);

  it.each(["detail", "candidates", "preview", "selection"] as const)(
    "uses the latest post-shoot decline at the %s boundary after restart", async (boundary) => {
      const app = createApp();
      const { sessionId, suggestionSetId } = await postShootFixture(app);
      await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
        suggestionSetId, decision: "accept",
      }).expect(201);
      const acceptedCandidates = await request(app).get(`/api/creative-sessions/${sessionId}/material-suggestions`);
      expect(acceptedCandidates.status, JSON.stringify(acceptedCandidates.body)).toBe(200);
      await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
        suggestionSetId, selectedSegmentIds: [SEGMENT_ID],
      }).expect(201);
      const declined = await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
        suggestionSetId, decision: "decline",
      }).expect(201);
      const restarted = createReviewServer(creativeDependencies({
        store: await JsonContentStore.open(join(directory, "library.json")),
      }));
      let response;
      if (boundary === "detail") {
        response = await request(restarted).get(`/api/creative-sessions/${sessionId}`).expect(200);
        expect(response.body.session.postShootSuggestionSet).toBeNull();
        expect(response.body.session.postShootMaterialDecision.action).toBe("decline-view");
        // Catches a sibling DTO that prefers stale use consent over the latest exact view decision.
        for (const appInstance of [app, restarted]) {
          const detail = await request(appInstance).get(`/api/creative-sessions/${sessionId}`).expect(200);
          for (const decision of [detail.body.session.materialDecision, detail.body.session.postShootMaterialDecision]) {
            expect(decision.action).toBe("decline-view");
            for (const key of ["shownSegmentIds", "selectedSegmentIds", "rejectedSegmentIds"]) {
              expect(decision).not.toHaveProperty(key);
            }
            expect(JSON.stringify(decision)).not.toContain(SEGMENT_ID);
            expect(JSON.stringify(decision)).not.toContain("thumbnailPath");
          }
          expect(detail.body.session.script.sections).toEqual(expect.arrayContaining([
            expect.objectContaining({ source: "historical-material", referencedSegmentIds: [SEGMENT_ID] }),
          ]));
          const markup = await renderCreativeDetail(detail.body.session);
          expect(markup).not.toContain(SEGMENT_ID);
          expect(markup).not.toContain("data-post-material-confirm");
        }
      } else if (boundary === "candidates") {
        response = await request(restarted).get(`/api/creative-sessions/${sessionId}/material-suggestions`);
        expect(response.status).toBe(400);
      } else if (boundary === "preview") {
        response = await authenticatedPreview(restarted,
          `/api/creative-sessions/${sessionId}/material-suggestions/${SEGMENT_ID}/preview`);
        expect(response.status).toBe(400);
      } else {
        response = await postAsClient(restarted, await defaultCreativeClient(restarted), `/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
          suggestionSetId, selectedSegmentIds: [SEGMENT_ID],
        }, "declined-selection-final-01");
        expect(response.status).toBe(400);
        expect((await library.listMaterialUseDecisions()).filter((decision) =>
          decision.purpose === "post-shoot-edit" && decision.action === "confirm-selection")).toHaveLength(1);
      }
      for (const body of [boundary === "detail" ? response.body.session.postShootMaterialDecision : response.body, declined.body]) {
        expect(JSON.stringify(body)).not.toContain(SEGMENT_ID);
        expect(JSON.stringify(body)).not.toContain("thumbnailPath");
      }
    }, 300_000);

  it.each(["bytes", "matching-digest-undecodable", "source-binding", "missing-kind", "path-binding"] as const)(
    "fails closed on the latest prompt four-video set with %s damage across restart", async (damage) => {
      const app = createApp();
      const { sessionId, suggestionSetId } = await postShootFixture(app);
      await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
        suggestionSetId, decision: "decline",
      }).expect(201);
      const processed = await post(app, `/api/creative-sessions/${sessionId}/editorial-process`, {}).expect(201);
      const story = processed.body.stories[0];
      const revised = await post(app, `/api/creative-sessions/${sessionId}/stories/${story.id}/revisions`, {
        prompt: "视频号开头先说结果。",
      }).expect(201);
      expect(revised.body.revision.revision).toBe(2);
      expect(revised.body.artifacts).toHaveLength(4);
      const current = revised.body.artifacts[0];
      const intactSibling = revised.body.artifacts[1];
      const artifact = (await library.listAllArtifacts()).find((item) => item.id === current.id)!;
      if (damage === "bytes" || damage === "matching-digest-undecodable") {
        await writeFile(artifact.videoPath, "corrupt current video");
      }
      if (damage !== "bytes") {
        const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
        const stored = snapshot.artifacts.find((item: { id: string }) => item.id === current.id);
        if (damage === "matching-digest-undecodable") {
          stored.digest = createHash("sha256").update("corrupt current video").digest("hex");
        } else if (damage === "source-binding") stored.sourceSegmentIds = [SEGMENT_ID];
        else if (damage === "path-binding") {
          stored.videoPath = join(directory, "rebound-current.mp4");
          await writeFile(stored.videoPath, await readFile(artifact.videoPath));
        }
        else snapshot.artifacts = snapshot.artifacts.filter((item: { id: string }) => item.id !== current.id);
        await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      }
      const reopened = await JsonContentStore.open(join(directory, "library.json"));
      const restarted = createReviewServer(creativeDependencies({ store: reopened }));
      const media = await authenticatedPreview(restarted, intactSibling.mediaUrl);
      expect(media.status).toBe(404);
      const detail = await request(restarted).get(`/api/creative-sessions/${sessionId}`).expect(200);
      expect(detail.body.session.processingTask).toMatchObject({
        status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false,
      });
      expect(detail.body.session.revisions).toEqual([]);
      expect(detail.body.session.artifacts).toEqual([]);
      await authenticatedPreview(restarted, current.mediaUrl).expect(404);
      await authenticatedPreview(restarted, processed.body.artifacts[0].mediaUrl).expect(404);
      expect((await reopened.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id)!.status)
        .toBe("failed-manual-review");
      expect(await reopened.listRevisions(story.id)).toHaveLength(2);
      expect((await reopened.listCreativeProcessingTasks()).filter((task) => task.operationKey)).toHaveLength(1);
    }, 300_000);

  // Catches trusting an artifact-derived directory (including a copied set with the identical basename).
  it.each(["alternate-parent", "prefix-collision", "dotdot", "directory-symlink", "file-symlink"] as const)(
    "rejects a relocated current render root with %s across restart", async (relocation) => {
      const { app, sessionId, processed, revised } = await mixedCurrentFixture();
      const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
      const artifacts = snapshot.artifacts.filter((item: { revisionId: string }) => item.revisionId === revised.body.revision.id);
      const originalRoot = dirname(artifacts[0].videoPath);
      const alternateParent = relocation === "prefix-collision" ? `${dirname(originalRoot)}-other` : join(directory, "alternate");
      if (relocation === "prefix-collision") extraDirectories.push(alternateParent);
      const alternateRoot = join(alternateParent, basename(originalRoot));
      await cp(originalRoot, alternateRoot, { recursive: true });
      if (relocation === "directory-symlink") {
        await rename(originalRoot, `${originalRoot}-retained`);
        await symlink(alternateRoot, originalRoot, "dir");
      } else if (relocation === "file-symlink") {
        await rename(artifacts[0].videoPath, `${artifacts[0].videoPath}.retained`);
        await symlink(join(alternateRoot, basename(artifacts[0].videoPath)), artifacts[0].videoPath);
      } else {
        if (relocation === "dotdot") await mkdir(join(dirname(originalRoot), "detour"));
        const root = relocation === "dotdot"
          ? `${dirname(originalRoot)}/detour/../${basename(originalRoot)}` : alternateRoot;
        for (const artifact of artifacts) {
          artifact.videoPath = `${root}/${basename(artifact.videoPath)}`;
          artifact.coverPath = `${root}/${basename(artifact.coverPath)}`;
          artifact.stillPaths = artifact.stillPaths.map((path: string) => `${root}/${basename(path)}`);
        }
      }
      await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      await expectCurrentOutputFailure(sessionId, processed, revised);
      // A formerly valid sibling must also close immediately on the existing server.
      await authenticatedPreview(app, revised.body.artifacts[1].mediaUrl).expect(404);
    }, 300_000);

  // Catches base-checkpoint output validation escaping the coordinated current-set failure transition.
  it.each(["missing-record", "missing-file", "corrupt-file", "missing-root", "wrong-root-owner", "wrong-root-path"] as const)(
    "persists recovery for a mixed-current V1 artifact with %s", async (damage) => {
      const { sessionId, processed, revised } = await mixedCurrentFixture();
      const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
      const currentV1 = processed.body.revisions.find((item: { storyId: string }) => item.storyId !== revised.body.revision.storyId);
      const damaged = snapshot.artifacts.find((item: { revisionId: string }) => item.revisionId === currentV1.id);
      if (damage === "missing-root" || damage === "wrong-root-owner" || damage === "wrong-root-path") {
        if (damage === "missing-root") {
          snapshot.creativeRenderBindings = snapshot.creativeRenderBindings.filter((item: { id: string }) => item.id !== currentV1.id);
        } else {
          const binding = snapshot.creativeRenderBindings.find((item: { id: string }) => item.id === currentV1.id);
          if (damage === "wrong-root-owner") binding.taskId = "different-processing-task";
          else binding.outputRoot = join(directory, "missing-render-root");
        }
        await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      } else if (damage === "missing-record") {
        snapshot.artifacts = snapshot.artifacts.filter((item: { id: string }) => item.id !== damaged.id);
        await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      } else if (damage === "missing-file") await rm(damaged.videoPath);
      else await writeFile(damaged.videoPath, "broken still-current V1 video");
      await expectCurrentOutputFailure(sessionId, processed, revised);
    }, 300_000);

  // Catches output rejection that fails to persist recovery until a later HTTP read,
  // and prompt consumption that trusts active records without inspecting actual media.
  it.each(["missing-root", "wrong-root-owner", "wrong-root-path", "invalidated-records",
    "missing-file", "corrupt-file", "matching-digest-undecodable", "missing-cover",
    "identical-timelines", "missing-head"] as const)(
    "persists current-output recovery before direct prompt returns for %s", async (damage) => {
      const { sessionId, processed, revised } = await mixedCurrentFixture();
      const path = join(directory, "library.json");
      const snapshot = JSON.parse(await readFile(path, "utf8"));
      // Damage the still-current sibling to prove the whole current set is required.
      const sibling = processed.body.revisions.find((item: { storyId: string }) => item.storyId !== revised.body.revision.storyId);
      const artifact = snapshot.artifacts.find((item: { revisionId: string }) => item.revisionId === sibling.id);
      if (damage === "invalidated-records") {
        const writer = await JsonContentStore.open(path);
        for (const item of (await writer.listAllArtifacts()).filter((item) => item.revisionId === sibling.id)) {
          await writer.putArtifact({ ...item, status: "invalidated" });
        }
      } else if (damage === "missing-file" || damage === "missing-cover") {
        const missingPath = damage === "missing-file" ? artifact.videoPath : artifact.coverPath;
        await rename(missingPath, `${missingPath}.retained`);
      } else if (damage === "corrupt-file" || damage === "matching-digest-undecodable") {
        const bytes = Buffer.from("R4 damaged current media");
        await writeFile(artifact.videoPath, bytes);
        if (damage === "matching-digest-undecodable") {
          artifact.digest = createHash("sha256").update(bytes).digest("hex");
          await writeFile(path, JSON.stringify(snapshot));
        }
      } else {
        const binding = snapshot.creativeRenderBindings.find((item: { id: string }) => item.id === sibling.id);
        if (damage === "missing-root") snapshot.creativeRenderBindings = snapshot.creativeRenderBindings.filter((item: { id: string }) => item.id !== sibling.id);
        else if (damage === "wrong-root-owner") binding.creativeSessionId = "different-creative-session";
        else if (damage === "wrong-root-path") binding.outputRoot = join(directory, "unavailable-render-root");
        else if (damage === "missing-head") snapshot.creativeRevisionHeads = snapshot.creativeRevisionHeads.filter((item: { revisionId: string }) => item.revisionId !== sibling.id);
        else {
          const revision = snapshot.revisions.find((item: { id: string }) => item.id === sibling.id);
          revision.variants[1].timeline = revision.variants[0].timeline;
        }
        await writeFile(path, JSON.stringify(snapshot));
      }
      const before = JSON.parse(await readFile(path, "utf8"));
      const reopened = await JsonContentStore.open(path);
      const revision = (await reopened.listRevisions(revised.body.revision.storyId)).find((item) => item.id === revised.body.revision.id)!;
      let interpreterCalls = 0;
      await expect.soft(reviseWithPrompt({ revision, prompt: "Continue this cut", allowedSegments: [],
        designerId: "designer_01", deferPersistence: true,
      }, { interpret: async () => {
        interpreterCalls += 1;
        return { targetVariants: ["wechat-channels"], replacementTimelines: {}, removePeople: [], explanation: "Unexpected model consumption" };
      } }, reopened)).rejects.toThrow("creative-editorial-current-output-invalid");
      expect.soft(interpreterCalls).toBe(0);
      // Read a fresh real store before any HTTP GET can repair the task for us.
      const persisted = await JsonContentStore.open(path);
      expect.soft((await persisted.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false });
      const after = JSON.parse(await readFile(path, "utf8"));
      for (const key of ["revisions", "creativeRevisionHeads", "creativeRenderBindings", "artifacts", "approvals", "exportPackages", "deliveries", "auditEvents"]) {
        expect.soft(after[key], key).toEqual(before[key]);
      }
      await expectCurrentOutputFailure(sessionId, processed, revised);
    }, 300_000);

  it.each(["export", "delivery"] as const)(
    "persists recovery before direct %s reads a damaged current video", async (boundary) => {
      const { app, sessionId, processed, revised } = await mixedCurrentFixture();
      const root = `/api/creative-sessions/${sessionId}`;
      const artifact = revised.body.artifacts[0];
      const approved = await post(app, `${root}/artifacts/${artifact.id}/approve`, {}).expect(201);
      const exported = await post(app, `${root}/approvals/${approved.body.approval.id}/export`, {}).expect(201);
      const path = join(directory, "library.json");
      const before = JSON.parse(await readFile(path, "utf8"));
      const damaged = before.artifacts.find((item: { id: string }) => item.id === artifact.id);
      await rename(damaged.videoPath, `${damaged.videoPath}.retained`);
      const reopened = await JsonContentStore.open(path);
      let deliveries = 0;
      const operation = boundary === "export"
        ? createExportPackage({ approvalId: approved.body.approval.id, artifactId: artifact.id,
          revisionId: revised.body.revision.id, creativeSessionId: sessionId }, reopened)
        : deliverExportPackage(exported.body.package.id, { deliver: async () => {
          deliveries += 1;
          return { receiptId: "must-not-deliver", receivedAt: new Date().toISOString() };
        } }, reopened, sessionId);
      await expect.soft(operation).rejects.toThrow("creative-editorial-current-output-invalid");
      const persisted = await JsonContentStore.open(path);
      expect.soft((await persisted.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid" });
      expect.soft(deliveries).toBe(0);
      const after = JSON.parse(await readFile(path, "utf8"));
      for (const key of ["revisions", "creativeRevisionHeads", "artifacts", "approvals", "exportPackages", "deliveries"]) {
        expect.soft(after[key], key).toEqual(before[key]);
      }
    }, 300_000);

  // Catches revision-first lookup skipping durable recovery, including raw file reads
  // and atomic commit callers that do not pass through the HTTP detail validator.
  it.each(["commitApproval", "approveVariant", "export", "delivery", "commitExport", "commitDelivery"].flatMap((boundary) =>
    [false, true].map((missingVideo) => ({ boundary, missingVideo }))))(
    "recovers a missing exact current revision at direct $boundary with missingVideo=$missingVideo", async ({ boundary, missingVideo }) => {
      const { sessionId, processed, revised, artifact, approval, exported, before, path } =
        await missingCurrentRevisionFixture(missingVideo);
      let deliveries = 0;
      const adapter = { deliver: async () => {
        deliveries += 1;
        return { receiptId: "must-not-deliver-missing-revision", receivedAt: new Date().toISOString() };
      } };
      const execute = (store: JsonContentStore) => {
        if (boundary === "commitApproval") return store.commitApproval({ ...approval, id: "missing-revision-new-approval" });
        if (boundary === "approveVariant") return approveVariant({ storyId: revised.body.revision.storyId,
          revisionId: revised.body.revision.id, revision: 2, artifactId: artifact.id,
          variant: artifact.kind, reviewerId: "designer_01" }, store);
        if (boundary === "export") return createExportPackage({ approvalId: approval.id, artifactId: artifact.id,
          revisionId: revised.body.revision.id, creativeSessionId: sessionId }, store);
        if (boundary === "delivery") return deliverExportPackage(exported.id, adapter, store, sessionId);
        if (boundary === "commitExport") return store.commitExportPackage(exported, sessionId);
        return store.commitExportDelivery({ packageId: exported.id, expectedPackage: exported, creativeSessionId: sessionId,
          packageStatus: "delivered", delivery: { packageId: exported.id, status: "received", receiptId: "must-not-commit",
            receivedAt: new Date().toISOString(), publishedAt: null, publishedClaimed: false } });
      };
      await expect.soft(execute(await JsonContentStore.open(path))).rejects.toThrow(/^creative-editorial-current-output-invalid$/);
      const after = JSON.parse(await readFile(path, "utf8"));
      const failed = after.creativeProcessingTasks.find((task: { id: string }) => task.id === processed.body.processingTask.id);
      expect.soft(failed).toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false });
      expect.soft({ ...after, creativeProcessingTasks: before.creativeProcessingTasks }).toEqual(before);
      // Catches a fresh domain handle replacing the persisted exact recovery
      // contract with the generic task-not-reviewable fallback.
      await expect.soft(execute(await JsonContentStore.open(path))).rejects.toThrow(/^creative-editorial-current-output-invalid$/);
      expect.soft(JSON.parse(await readFile(path, "utf8"))).toEqual(after);
      expect.soft(deliveries).toBe(0);
      await expectCurrentOutputFailure(sessionId, processed, revised, 1);
    }, 300_000);

  // Catches the second path-coordinated caller observing the first caller's
  // durable transition but replacing its exact error with task-not-reviewable.
  it("stabilizes both concurrent direct calls after current-output recovery", async () => {
    const { processed, approval, before, path } = await missingCurrentRevisionFixture(true);
    const [first, second] = await Promise.allSettled([
      (await JsonContentStore.open(path)).commitApproval({ ...approval, id: "concurrent-recovery-approval-a" }),
      (await JsonContentStore.open(path)).commitApproval({ ...approval, id: "concurrent-recovery-approval-b" }),
    ]);
    for (const result of [first, second]) {
      expect.soft(result.status).toBe("rejected");
      if (result.status === "rejected") {
        expect.soft(result.reason).toMatchObject({ message: "creative-editorial-current-output-invalid" });
      }
    }
    const after = JSON.parse(await readFile(path, "utf8"));
    expect.soft(after.creativeProcessingTasks.find((task: { id: string }) => task.id === processed.body.processingTask.id))
      .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid",
        publishedClaimed: false });
    expect.soft({ ...after, creativeProcessingTasks: before.creativeProcessingTasks }).toEqual(before);
    expect.soft(after.creativeProcessingTasks).toHaveLength(before.creativeProcessingTasks.length);
  }, 300_000);

  // Shipping mutations must expose the persisted failure consistently through the
  // same operation key and receipt, and preserve it when a new server opens the store.
  it.each(["approval", "export", "delivery"].flatMap((boundary) =>
    [false, true].map((missingVideo) => ({ boundary, missingVideo }))))(
    "replays HTTP missing current revision recovery for $boundary with missingVideo=$missingVideo", async ({ boundary, missingVideo }) => {
      const { sessionId, processed, revised, artifact, approval, exported, before, path } =
        await missingCurrentRevisionFixture(missingVideo);
      let deliveries = 0;
      const app = createReviewServer({ ...creativeDependencies({ store: await JsonContentStore.open(path) }),
        phonePublisher: { deliver: async () => {
          deliveries += 1;
          return { receiptId: "must-not-deliver-http", receivedAt: new Date().toISOString() };
        } } });
      const client = await defaultCreativeClient(app);
      const root = `/api/creative-sessions/${sessionId}`;
      const route = boundary === "approval" ? `${root}/artifacts/${artifact.id}/approve`
        : boundary === "export" ? `${root}/approvals/${approval.id}/export`
          : `${root}/export-packages/${exported.id}/deliver`;
      const key = `missing-current-revision-${boundary}-${missingVideo}`;
      const failed = await postAsClient(app, client, route, {}, key).expect(400);
      expect(failed.body).toEqual({ error: "creative-editorial-current-output-invalid" });
      const after = JSON.parse(await readFile(path, "utf8"));
      expect(after.creativeProcessingTasks.find((task: { id: string }) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false });
      expect({ ...after, creativeProcessingTasks: before.creativeProcessingTasks }).toEqual(before);
      expect((await postAsClient(app, client, route, {}, key).expect(400)).body).toEqual(failed.body);
      const receipt = await readReceipt(app, client, key).expect(200);
      expect(receipt.body.receipt.response).toMatchObject({ status: 400, body: failed.body });
      const restarted = createReviewServer(creativeDependencies({ store: await JsonContentStore.open(path) }));
      const restartedClient = await defaultCreativeClient(restarted);
      // Same-key replay must survive restart through the durable receipt.
      expect((await postAsClient(restarted, restartedClient, route, {}, key).expect(400)).body).toEqual(failed.body);
      // Catches requireCreativeReviewContext returning review-not-found before
      // projecting the exact persisted current-output failure for a fresh key.
      const freshKey = `${key}-after-restart`;
      expect((await postAsClient(restarted, restartedClient, route, {}, freshKey).expect(400)).body).toEqual(failed.body);
      expect((await readReceipt(restarted, restartedClient, freshKey).expect(200)).body.receipt.response)
        .toMatchObject({ status: 400, body: failed.body });
      const detail = await request(restarted).get(root).expect(200);
      expect(detail.body.session.processingTask).toMatchObject({ status: "failed-manual-review",
        errorCode: "creative-editorial-current-output-invalid" });
      expect(detail.body.session.artifacts).toEqual([]);
      expect(detail.body.session.revisions).toEqual([]);
      expect(JSON.parse(await readFile(path, "utf8"))).toEqual(after);
      expect(deliveries).toBe(0);
    }, 300_000);

  it.each(["unknown-id", "wrong-story", "wrong-number", "obsolete-revision"] as const)(
    "does not classify an unrelated %s as missing current output", async (identity) => {
      const { revised, artifact, approval, before, path } = await missingCurrentRevisionFixture(false);
      const input = { storyId: revised.body.revision.storyId, revisionId: revised.body.revision.id,
        revision: 2, artifactId: artifact.id, variant: artifact.kind, reviewerId: "designer_01" };
      if (identity === "unknown-id") input.revisionId = "nonexistent-current-revision";
      else if (identity === "wrong-story") input.storyId = "unrelated-story";
      else if (identity === "wrong-number") input.revision = 1;
      else {
        const obsolete = before.revisions.find((revision: { storyId: string }) => revision.storyId === input.storyId);
        input.revisionId = obsolete.id;
        input.revision = obsolete.revision;
        before.revisions = before.revisions.filter((revision: { id: string }) => revision.id !== obsolete.id);
        await writeFile(path, JSON.stringify(before));
      }
      const store = await JsonContentStore.open(path);
      await expect(approveVariant(input, store)).rejects.toThrow(/^exact-revision-required$/);
      await expect(store.commitApproval({ ...approval, id: "unrelated-identity-approval", storyId: input.storyId,
        revisionId: input.revisionId, revision: input.revision })).rejects.toThrow(/^exact-revision-required$/);
      expect(JSON.parse(await readFile(path, "utf8"))).toEqual(before);
    }, 300_000);

  it("preserves wrong-session export errors when the requested current row is missing", async () => {
    const { revised, artifact, approval, exported, before, path } = await missingCurrentRevisionFixture(false);
    const store = await JsonContentStore.open(path);
    await expect(createExportPackage({ approvalId: approval.id, artifactId: artifact.id,
      revisionId: revised.body.revision.id, creativeSessionId: "unrelated-creative-session" }, store))
      .rejects.toThrow(/^approved-revision-not-found$/);
    let deliveries = 0;
    await expect(deliverExportPackage(exported.id, { deliver: async () => {
      deliveries += 1;
      return { receiptId: "wrong-session-must-not-deliver", receivedAt: new Date().toISOString() };
    } }, store, "unrelated-creative-session")).rejects.toThrow(/^active-approved-artifact-required$/);
    expect(deliveries).toBe(0);
    expect(JSON.parse(await readFile(path, "utf8"))).toEqual(before);
  }, 300_000);

  async function missingCurrentRevisionFixture(missingVideo: boolean) {
    const { app, sessionId, processed, revised } = await mixedCurrentFixture();
    const root = `/api/creative-sessions/${sessionId}`;
    const artifact = revised.body.artifacts[0];
    const approved = await post(app, `${root}/artifacts/${artifact.id}/approve`, {}).expect(201);
    const exported = await post(app, `${root}/approvals/${approved.body.approval.id}/export`, {}).expect(201);
    const path = join(directory, "library.json");
    const before = JSON.parse(await readFile(path, "utf8"));
    const value = before.exportPackages.find((item: { id: string }) => item.id === exported.body.package.id);
    before.revisions = before.revisions.filter((item: { id: string }) => item.id !== revised.body.revision.id);
    if (missingVideo) {
      const stored = before.artifacts.find((item: { id: string }) => item.id === artifact.id);
      await rename(stored.videoPath, `${stored.videoPath}.retained`);
    }
    await writeFile(path, JSON.stringify(before));
    return { sessionId, processed, revised, artifact, approval: approved.body.approval, exported: value, before, path };
  }

  it("serves all eight artifacts in a complete mixed-current V2 and V1 set after restart", async () => {
    const { sessionId, processed, revised } = await mixedCurrentFixture();
    const restarted = createReviewServer(creativeDependencies({
      store: await JsonContentStore.open(join(directory, "library.json")),
    }));
    const detail = await request(restarted).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(detail.body.session.processingTask).toMatchObject({ id: processed.body.processingTask.id,
      status: "reviewable", publishedClaimed: false });
    expect(detail.body.session.revisions.map((item: { revision: number }) => item.revision).sort()).toEqual([1, 2]);
    expect(detail.body.session.artifacts).toHaveLength(8);
    for (const artifact of detail.body.session.artifacts) await authenticatedPreview(restarted, artifact.mediaUrl).expect(200);
    const superseded = processed.body.artifacts.find((item: { revisionId: string }) => item.revisionId === revised.body.revision.parentRevisionId);
    await authenticatedPreview(restarted, superseded.mediaUrl).expect(404);
  }, 300_000);

  // Catches library-wide model inputs; the forbidden transcript contains no catalog identifier.
  it.each([false, true])("restricts prompt interpreter transcripts to current exact story authority: history=%s", async (history) => {
    const received: ContentSegment[][] = [];
    const deps = creativeDependencies();
    const app = createReviewServer({ ...deps, interpreter: { interpret: async (input) => {
      received.push(input.allowedSegments);
      return { targetVariants: ["wechat-channels"], replacementTimelines: {}, removePeople: [],
        explanation: input.allowedSegments.map((segment) => segment.tags.transcript).join(" | ") };
    } } });
    const { sessionId, suggestionSetId, historyId } = await promptSourceFixture(app, history);
    const root = `/api/creative-sessions/${sessionId}`;
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    const story = processed.body.stories.find((item: { historicalSegmentIds: string[] }) =>
      !history || item.historicalSegmentIds.includes(historyId));
    expect(story).toBeDefined();
    // Extra same-batch material is also not in this story/checkpoint's lineage.
    const anchor = (await library.listSegments()).find((item) => item.id === story.anchorSegmentIds[0])!;
    const capture = (await library.listAssets()).find((asset) => asset.id === anchor.assetId)!;
    const foreign = { ...capture, id: "foreign_capture_asset", batchId: "foreign_batch", sessionId: "foreign_session",
      captureContext: { ...capture.captureContext!, creativeSessionId: "foreign_creative_session" } };
    await library.putAsset(foreign);
    await library.putSegments([{ ...anchor, id: "foreign_capture_segment", assetId: foreign.id, batchId: foreign.batchId,
      tags: { ...anchor.tags, transcript: "Foreign session private transcript" } }]);
    await library.putSegments([{ ...anchor, id: "other_story_segment", tags: {
      ...anchor.tags, transcript: "Off-story private strategy discussion",
    } }]);
    const body = { prompt: "Tighten the opening without adding footage." };
    const key = `final-source-scope-${history}`;
    const path = `${root}/stories/${story.id}/revisions`;
    const client = await defaultCreativeClient(app);
    const revised = await postAsClient(app, client, path, body, key).expect(201);
    expect.soft(received[0].map((item) => item.id).sort()).toEqual(
      [...story.anchorSegmentIds, ...story.historicalSegmentIds].sort());
    for (const output of [revised.body, (await request(app).get(root).expect(200)).body,
      (await readReceipt(app, client, key).expect(200)).body]) {
      for (const text of ["Synthetic prior rework.", "Off-story private strategy discussion", "Foreign session private transcript"])
        expect.soft(JSON.stringify(output)).not.toContain(text);
      if (history) expect.soft(JSON.stringify(output)).toContain("Authorized prior craftsmanship");
      else expect.soft(JSON.stringify(output)).not.toContain("Authorized prior craftsmanship");
    }
    expect((await postAsClient(app, client, path, body, key).expect(201)).body).toEqual(revised.body);
    expect(received).toHaveLength(1);
    expect(processed.body.processingTask.publishedClaimed).toBe(false);
    expect(suggestionSetId).toBeTruthy();
  }, 300_000);

  // Catches consent/source validation occurring only before the async model call.
  it.each(["decline", "source-change"] as const)("rejects prompt commit after concurrent %s without advancing heads", async (change) => {
    const entered = deferred<void>();
    const release = deferred<void>();
    const deps = creativeDependencies();
    const app = createReviewServer({ ...deps, interpreter: { interpret: async (input) => {
      entered.resolve();
      await release.promise;
      return { targetVariants: ["wechat-channels"], replacementTimelines: {}, removePeople: [],
        explanation: input.allowedSegments.map((segment) => segment.tags.transcript).join(" | ") };
    } } });
    const { sessionId, suggestionSetId, historyId } = await promptSourceFixture(app, true);
    const root = `/api/creative-sessions/${sessionId}`;
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    const story = processed.body.stories.find((item: { historicalSegmentIds: string[] }) => item.historicalSegmentIds.includes(historyId));
    const beforeHeads = await library.listCreativeRevisionHeads();
    const beforeArtifacts = await library.listAllArtifacts();
    const client = await defaultCreativeClient(app);
    const pending = postAsClient(app, client, `${root}/stories/${story.id}/revisions`,
      { prompt: "Tighten the opening." }, `final-concurrent-${change}`).then((value: any) => value);
    await entered.promise;
    try {
      if (change === "decline") await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
      else {
        const segment = (await library.listSegments()).find((item) => item.id === historyId)!;
        await library.putSegments([{ ...segment, tags: { ...segment.tags, transcript: "Changed source after analysis" } }]);
      }
    } finally { release.resolve(); }
    const response = await pending;
    expect.soft([400, 403, 409]).toContain(response.status);
    expect.soft(await library.listCreativeRevisionHeads()).toEqual(beforeHeads);
    expect.soft(await library.listAllArtifacts()).toEqual(beforeArtifacts);
    expect.soft(await library.listRevisions(story.id)).toHaveLength(1);
    expect.soft(JSON.stringify(response.body)).not.toContain("Authorized prior craftsmanship");
  }, 300_000);

  it("rechecks withdrawn source authority on a direct commit replay of an existing prompt revision", async () => {
    const app = createApp();
    const { sessionId, suggestionSetId, historyId } = await promptSourceFixture(app, true);
    const root = `/api/creative-sessions/${sessionId}`;
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    const story = processed.body.stories.find((item: { historicalSegmentIds: string[] }) => item.historicalSegmentIds.includes(historyId));
    const revised = await post(app, `${root}/stories/${story.id}/revisions`, { prompt: "Tighten the opening." }).expect(201);
    const previous = processed.body.revisions.find((item: { storyId: string }) => item.storyId === story.id);
    const artifacts = await library.listArtifacts(revised.body.revision.id);
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    const before = await library.listAuditEvents();
    await expect.soft(commitPromptRevision({ previous, next: revised.body.revision, artifacts,
      renderOutputRoot: directory, designerId: "designer_01" }, library)).rejects.toThrow();
    expect.soft(await library.listAuditEvents()).toEqual(before);
  }, 300_000);

  // Catches four files passing proposal, approval and delivery despite identical ordered edits.
  it("rejects identical prompt timelines before rendering, approval, export or local delivery", async () => {
    let renders = 0;
    let deliveries = 0;
    const deps = creativeDependencies({ renderer: async (...args) => { renders += 1; return renderRevision(...args); } });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async ({ revision }) => ({
      targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
      replacementTimelines: { "editorial-master": revision.variants[0].timeline,
        xiaohongshu: revision.variants[0].timeline, douyin: revision.variants[0].timeline,
        "wechat-channels": revision.variants[0].timeline },
      removePeople: [], explanation: "Use identical cuts with three labels",
    }) }, phonePublisher: { deliver: async () => {
      deliveries += 1; return { receiptId: "local-only", receivedAt: new Date().toISOString() };
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    const heads = await library.listCreativeRevisionHeads();
    const artifacts = await library.listAllArtifacts();
    const rendersBefore = renders;
    const client = await defaultCreativeClient(app);
    const revised = await postAsClient(app, client, `${root}/stories/${processed.body.stories[0].id}/revisions`,
      { prompt: "Use this same edit everywhere." }, "final-identical-prompt");
    expect.soft(revised.status).toBe(400);
    expect.soft(revised.body.error).toBe("variant-timelines-must-differ");
    expect.soft(renders).toBe(rendersBefore);
    expect.soft(await library.listCreativeRevisionHeads()).toEqual(heads);
    expect.soft(await library.listAllArtifacts()).toEqual(artifacts);
    if (revised.status === 201) {
      const artifact = revised.body.artifacts[0];
      const approved = await postAsClient(app, client, `${root}/artifacts/${artifact.id}/approve`, {}, "final-identical-approve");
      expect.soft(approved.status).not.toBe(201);
      if (approved.status === 201) {
        const exported = await postAsClient(app, client, `${root}/approvals/${approved.body.approval.id}/export`, {}, "final-identical-export");
        expect.soft(exported.status).not.toBe(201);
        if (exported.status === 201) {
          const delivered = await postAsClient(app, client, `${root}/export-packages/${exported.body.package.id}/deliver`, {}, "final-identical-deliver");
          expect.soft(delivered.status).not.toBe(201);
        }
      }
    }
    expect.soft(deliveries).toBe(0);
    expect.soft(await library.listDeliveries()).toEqual([]);
  }, 300_000);

  // Simulate an old persisted current revision with valid media/digests but one timeline.
  it.each(["detail", "approval", "export", "delivery", "domain-approval", "domain-export", "domain-delivery"] as const)("persists manual recovery for legacy identical current timelines at %s", async (boundary) => {
    const { app, sessionId, processed, revised } = await mixedCurrentFixture();
    const root = `/api/creative-sessions/${sessionId}`;
    const artifact = revised.body.artifacts[0];
    const approved = await post(app, `${root}/artifacts/${artifact.id}/approve`, {}).expect(201);
    const exported = await post(app, `${root}/approvals/${approved.body.approval.id}/export`, {}).expect(201);
    const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
    const revision = snapshot.revisions.find((item: { id: string }) => item.id === revised.body.revision.id);
    revision.variants = revision.variants.map((variant: any) => ({ ...variant, timeline: revision.variants[0].timeline }));
    const rendered = await renderRevision(revision, snapshot.segments, snapshot.assets, directory);
    snapshot.artifacts = snapshot.artifacts.filter((item: { revisionId: string }) => item.revisionId !== revision.id).concat(rendered.artifacts);
    const changedArtifact = rendered.artifacts.find((item) => item.id === artifact.id)!;
    const approval = snapshot.approvals.find((item: { id: string }) => item.id === approved.body.approval.id);
    Object.assign(approval, { artifactDigest: changedArtifact.digest, coverDigest: changedArtifact.coverDigest,
      stillDigests: changedArtifact.stillDigests, renderInputDigest: changedArtifact.renderInputDigest });
    const value = snapshot.exportPackages.find((item: { id: string }) => item.id === exported.body.package.id);
    Object.assign(value, { videoPath: changedArtifact.videoPath, videoDigest: changedArtifact.digest,
      coverPath: changedArtifact.coverPath, coverDigest: changedArtifact.coverDigest,
      stillPaths: changedArtifact.stillPaths, stillDigests: changedArtifact.stillDigests,
      renderInputDigest: changedArtifact.renderInputDigest, idempotencyKey: `${approval.id}:${changedArtifact.digest}` });
    await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    let deliveries = 0;
    const restarted = createReviewServer({ ...creativeDependencies({ store: reopened }), phonePublisher: { deliver: async () => {
      deliveries += 1; return { receiptId: "legacy-local-only", receivedAt: new Date().toISOString() };
    } } });
    if (boundary.startsWith("domain-")) {
      const operation = boundary === "domain-approval" ? approveVariant({ storyId: revision.storyId,
        revisionId: revision.id, revision: revision.revision, artifactId: artifact.id,
        variant: artifact.kind, reviewerId: "designer_01" }, reopened)
        : boundary === "domain-export" ? createExportPackage({ approvalId: approval.id, artifactId: artifact.id,
          revisionId: revision.id, creativeSessionId: sessionId }, reopened)
          : deliverExportPackage(value.id, { deliver: async () => {
            deliveries += 1; return { receiptId: "direct-local-only", receivedAt: new Date().toISOString() };
          } }, reopened, sessionId);
      await expect.soft(operation).rejects.toThrow();
      expect.soft((await reopened.listCreativeProcessingTasks()).find((item) => item.id === processed.body.processingTask.id)?.status)
        .toBe("failed-manual-review");
      if (boundary === "domain-approval") {
        const sibling = processed.body.revisions.find((item: { storyId: string }) => item.storyId !== revision.storyId);
        const siblingArtifact = processed.body.artifacts.find((item: { revisionId: string }) => item.revisionId === sibling.id);
        const approvalsBefore = await reopened.listApprovals();
        await expect.soft(approveVariant({ storyId: sibling.storyId, revisionId: sibling.id, revision: sibling.revision,
          artifactId: siblingArtifact.id, variant: siblingArtifact.kind, reviewerId: "designer_01" }, reopened)).rejects.toThrow();
        expect.soft(await reopened.listApprovals()).toEqual(approvalsBefore);
      }
    } else if (boundary !== "detail") {
      const path = boundary === "approval" ? `${root}/artifacts/${artifact.id}/approve`
        : boundary === "export" ? `${root}/approvals/${approval.id}/export` : `${root}/export-packages/${value.id}/deliver`;
      const response = await postAsClient(restarted, await defaultCreativeClient(restarted), path, {}, `final-legacy-${boundary}`);
      expect.soft(response.status).not.toBe(201);
    }
    const detail = await request(restarted).get(root).expect(200);
    expect.soft(detail.body.session.processingTask).toMatchObject({ status: "failed-manual-review",
      errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false });
    expect.soft(detail.body.session.artifacts).toEqual([]);
    expect.soft(detail.body.session.revisions).toEqual([]);
    expect.soft((await reopened.listCreativeProcessingTasks()).find((item) => item.id === processed.body.processingTask.id)?.status)
      .toBe("failed-manual-review");
    expect.soft(deliveries).toBe(0);
    expect.soft(await reopened.listDeliveries()).toEqual([]);
  }, 300_000);

  it("fails closed for approval, export and delivery replays and receipts after a captured source is withdrawn", async () => {
    const deps = creativeDependencies();
    const app = createReviewServer({ ...deps, phonePublisher: {
      deliver: async () => ({ receiptId: "withdrawal-public-receipt", receivedAt: "2026-09-08T10:00:00.000Z" }),
    } });
    const current = await deliveredCurrentCaptureFixture(app);
    const withdrawer = await JsonContentStore.open(join(directory, "library.json"));
    const auditsBefore = await withdrawer.listAuditEvents();
    await withdrawer.commitPrivacyInvalidation({
      segmentId: current.sourceSegmentId,
      reason: "customer withdrew this captured source",
      actorId: "customer_01",
    });

    for (const operation of current.operations) {
      const replay = await postAsClient(app, current.client, operation.path, operation.body, operation.key);
      expect.soft([400, 403, 404, 409]).toContain(replay.status);
      expect.soft(JSON.stringify(replay.body)).not.toContain(operation.successIdentity);
      const receipt = await readReceipt(app, current.client, operation.key);
      expect.soft([400, 403, 404, 409]).toContain(receipt.status);
      expect.soft(JSON.stringify(receipt.body)).not.toContain(operation.successIdentity);
    }
    await authenticatedPreview(app,
      `/api/creative-sessions/${current.sessionId}/artifacts/${current.artifactId}/media`).expect(404);

    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect((await persisted.listSegments()).find((item) => item.id === current.sourceSegmentId)?.permissionState)
      .toBe("withdrawn");
    expect((await persisted.listAllArtifacts()).find((item) => item.id === current.artifactId)?.status)
      .toBe("invalidated");
    expect((await persisted.listApprovals()).find((item) => item.id === current.approvalId)?.status)
      .toBe("invalidated");
    expect((await persisted.listExportPackages()).find((item) => item.id === current.packageId)?.status)
      .toBe("invalidated");
    expect(await persisted.listDeliveries()).toContainEqual(expect.objectContaining({
      packageId: current.packageId, status: "received", publishedClaimed: false,
    }));
    expect(await persisted.listAuditEvents()).toEqual(expect.arrayContaining([
      ...auditsBefore,
      expect.objectContaining({ action: "segment-permission-withdrawn", entityId: current.sourceSegmentId }),
    ]));
  }, 300_000);

  it("withholds an editorial-process replay when its exact V1 lineage is invalidated but current V2 remains valid", async () => {
    const segmentAnalyzer = { analyze: async () => [
      { startMs: 0, endMs: 500, transcript: "我觉得手工应该留下痕迹。", signals: [] as [] },
      { startMs: 500, endMs: 1_000, transcript: "我决定坚持再做一次。", signals: [] as [] },
    ] };
    const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
      const cut = (outMs: number) => ({
        timeline: story.anchorSegmentIds.map((segmentId) => ({
          segmentId, inMs: 0, outMs, role: "anchor" as const,
        })),
        title: "A-0 exact-lineage cut", caption: "Synthetic fixture",
        cover: { segmentId: story.anchorSegmentIds[0]!, atMs: 50 },
      });
      return { master: cut(100), xiaohongshu: cut(200), douyin: cut(250), wechatChannels: cut(300), stills: [] };
    } };
    let keepSegmentId = "";
    const deps = creativeDependencies({ segmentAnalyzer, editAnalyzer });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async ({ revision }) => {
      const variant = (kind: "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels", outMs: number) => ({
        kind,
        timeline: [{ segmentId: keepSegmentId, inMs: 0, outMs, role: "anchor" as const }],
        title: `${kind} V2`, caption: "Current second-segment-only cut",
        cover: { segmentId: keepSegmentId, atMs: 100 },
      });
      return {
        targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
        replacementTimelines: {
          "editorial-master": variant("editorial-master", 300).timeline,
          xiaohongshu: variant("xiaohongshu", 400).timeline,
          douyin: variant("douyin", 450).timeline,
          "wechat-channels": variant("wechat-channels", 500).timeline,
        },
        removePeople: [], explanation: `V2 retains ${revision.storyId} from the second capture only.`,
      };
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "decline",
    }).expect(201);
    const client = await defaultCreativeClient(app);
    const processKey = "r4-exact-v1-process";
    const processed = await postAsClient(app, client, `${root}/editorial-process`, {}, processKey);
    expect(processed.status, JSON.stringify(processed.body)).toBe(201);
    expect(processed.body.stories).toHaveLength(2);
    const capturedIds: string[] = [...new Set<string>((processed.body.stories as Array<{ anchorSegmentIds: string[] }>).flatMap(
      (story: { anchorSegmentIds: string[] }) => story.anchorSegmentIds,
    ))];
    expect(capturedIds).toHaveLength(2);
    const [withdrawnSegmentId, retainedSegmentId] = capturedIds;
    keepSegmentId = retainedSegmentId!;
    const v1ArtifactIds = processed.body.artifacts.map((item: { id: string }) => item.id);

    const v2Artifacts: Array<{ id: string; sourceSegmentIds: string[] }> = [];
    for (const story of processed.body.stories) {
      const revised = await postAsClient(app, client, `${root}/stories/${story.id}/revisions`, {
        prompt: "Use only the second captured segment in this current version.",
      }, `r4-v2-${story.id}`);
      expect(revised.status, JSON.stringify(revised.body)).toBe(201);
      expect(revised.body.revision.revision).toBe(2);
      expect(revised.body.artifacts).toHaveLength(4);
      v2Artifacts.push(...revised.body.artifacts);
    }
    expect(v2Artifacts).toHaveLength(8);
    expect(v2Artifacts.every((artifact) =>
      artifact.sourceSegmentIds.includes(retainedSegmentId!) &&
      !artifact.sourceSegmentIds.includes(withdrawnSegmentId!),
    )).toBe(true);

    const v1Artifact = (await library.listAllArtifacts()).find((item) => item.id === v1ArtifactIds[0])!;
    const originalV1Bytes = await readFile(v1Artifact.videoPath);
    await writeFile(v1Artifact.videoPath, Buffer.from("corrupt historical V1 render"));
    for (const response of [
      await postAsClient(app, client, `${root}/editorial-process`, {}, processKey),
      await readReceipt(app, client, processKey),
    ]) {
      expect.soft(response.status).toBe(403);
      expect.soft(JSON.stringify(response.body)).not.toContain(v1ArtifactIds[0]);
    }
    const currentWhileV1Corrupt = await request(app).get(root).expect(200);
    expect(currentWhileV1Corrupt.body.session.artifacts).toHaveLength(8);
    await writeFile(v1Artifact.videoPath, originalV1Bytes);
    expect((await postAsClient(app, client, `${root}/editorial-process`, {}, processKey).expect(201)).body)
      .toEqual(processed.body);
    await readReceipt(app, client, processKey).expect(200);

    const approvedBeforeWithdrawal = await postAsClient(app, client,
      `${root}/artifacts/${v2Artifacts[0]!.id}/approve`, {}, "r4-v2-approval-before-withdrawal");
    expect(approvedBeforeWithdrawal.status, JSON.stringify(approvedBeforeWithdrawal.body)).toBe(201);

    const withdrawer = await JsonContentStore.open(join(directory, "library.json"));
    const internalTasksBefore = await withdrawer.listCreativeProcessingTasks();
    const auditsBefore = await withdrawer.listAuditEvents(sessionId);
    await withdrawer.commitPrivacyInvalidation({
      segmentId: withdrawnSegmentId!,
      reason: "customer withdrew only the first captured segment",
      actorId: "customer_01",
    });

    for (const response of [
      await postAsClient(app, client, `${root}/editorial-process`, {}, processKey),
      await readReceipt(app, client, processKey),
    ]) {
      expect.soft(response.status).toBe(403);
      expect.soft(response.body).toEqual({ error: "creative-current-output-no-longer-authorized" });
      for (const id of v1ArtifactIds) expect.soft(JSON.stringify(response.body)).not.toContain(id);
    }
    const detail = await request(app).get(root);
    expect(detail.status, JSON.stringify(detail.body)).toBe(200);
    expect(detail.body.session.processingTask).toMatchObject({ status: "reviewable", publishedClaimed: false });
    expect(detail.body.session.revisions.map((item: { revision: number }) => item.revision)).toEqual([2, 2]);
    expect(detail.body.session.artifacts).toHaveLength(8);
    expect(detail.body.session.artifacts.every((artifact: { sourceSegmentIds: string[] }) =>
      artifact.sourceSegmentIds.includes(retainedSegmentId!) &&
      !artifact.sourceSegmentIds.includes(withdrawnSegmentId!),
    )).toBe(true);
    await postAsClient(app, client, `${root}/artifacts/${v2Artifacts[1]!.id}/approve`, {},
      "r4-v2-approval-after-withdrawal").expect(201);
    await postAsClient(app, client,
      `${root}/approvals/${approvedBeforeWithdrawal.body.approval.id}/export`, {},
      "r4-v2-export-after-withdrawal").expect(201);
    const continued = await postAsClient(app, client,
      `${root}/stories/${processed.body.stories[0].id}/revisions`, {
        prompt: "Continue using only the second captured segment.",
      }, "r4-v2-prompt-after-withdrawal").expect(201);
    expect(continued.body.revision.revision).toBe(3);
    expect(continued.body.artifacts).toHaveLength(4);
    for (const id of v1ArtifactIds) {
      await authenticatedPreview(app,
        `${root}/artifacts/${id}/media`).expect(404);
    }
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect(await persisted.listCreativeProcessingTasks()).toEqual(internalTasksBefore);
    expect((await persisted.listAuditEvents(sessionId)).slice(0, auditsBefore.length)).toEqual(auditsBefore);
    expect((await persisted.listSegments()).find((item) => item.id === withdrawnSegmentId)?.permissionState)
      .toBe("withdrawn");
  }, 300_000);

  it.each(["transcript", "segment-permission", "parent-asset"] as const)(
    "rejects current V2 operations when another store changes %s evidence for a still-used source", async (mutation) => {
    const segmentAnalyzer = { analyze: async () => [
      { startMs: 0, endMs: 500, transcript: "我觉得手工应该留下痕迹。", signals: [] as [] },
      { startMs: 500, endMs: 1_000, transcript: "我决定坚持再做一次。", signals: [] as [] },
    ] };
    const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
      const cut = (outMs: number) => ({
        timeline: story.anchorSegmentIds.map((segmentId) => ({
          segmentId, inMs: 0, outMs, role: "anchor" as const,
        })),
        title: "A-0 current-evidence cut", caption: "Synthetic fixture",
        cover: { segmentId: story.anchorSegmentIds[0]!, atMs: 50 },
      });
      return { master: cut(100), xiaohongshu: cut(200), douyin: cut(250), wechatChannels: cut(300), stills: [] };
    } };
    let keepSegmentId = "";
    let interpreterCalls = 0;
    let interpreterTranscripts: string[] = [];
    const deps = creativeDependencies({ segmentAnalyzer, editAnalyzer });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async ({ revision, allowedSegments }) => {
      interpreterCalls += 1;
      interpreterTranscripts = allowedSegments.map((segment) => segment.tags.transcript);
      const timeline = (outMs: number) => [{
        segmentId: keepSegmentId, inMs: 0, outMs, role: "anchor" as const,
      }];
      return {
        targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
        replacementTimelines: {
          "editorial-master": timeline(300), xiaohongshu: timeline(400), douyin: timeline(450), "wechat-channels": timeline(500),
        },
        removePeople: [], explanation: `Current evidence for ${revision.storyId}.`,
      };
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "decline",
    }).expect(201);
    const client = await defaultCreativeClient(app);
    const processed = await postAsClient(app, client, `${root}/editorial-process`, {}, "r4-current-evidence-process");
    expect(processed.status, JSON.stringify(processed.body)).toBe(201);
    const capturedIds: string[] = [...new Set<string>((processed.body.stories as Array<{ anchorSegmentIds: string[] }>).flatMap(
      (story: { anchorSegmentIds: string[] }) => story.anchorSegmentIds,
    ))];
    expect(capturedIds).toHaveLength(2);
    keepSegmentId = capturedIds[1]!;
    const v2Artifacts: Array<{ id: string }> = [];
    for (const story of processed.body.stories) {
      const revised = await postAsClient(app, client, `${root}/stories/${story.id}/revisions`, {
        prompt: "Use only the second captured segment in this current version.",
      }, `r4-current-evidence-v2-${story.id}`);
      expect(revised.status, JSON.stringify(revised.body)).toBe(201);
      v2Artifacts.push(...revised.body.artifacts);
    }
    expect(v2Artifacts).toHaveLength(8);
    const approved = await postAsClient(app, client, `${root}/artifacts/${v2Artifacts[0]!.id}/approve`, {},
      "r4-current-evidence-preapproval").expect(201);

    const writer = await JsonContentStore.open(join(directory, "library.json"));
    const stillUsed = (await writer.listSegments()).find((segment) => segment.id === keepSegmentId)!;
    if (mutation === "transcript") {
      await writer.putSegments([{ ...stillUsed, tags: {
        ...stillUsed.tags, transcript: "R4_CHANGED_CURRENT_TRANSCRIPT",
      } }]);
    } else if (mutation === "segment-permission") {
      await writer.putSegments([{ ...stillUsed, permissionState: "review-required" }]);
    } else {
      const parentAsset = (await writer.listAssets()).find((asset) => asset.id === stillUsed.assetId)!;
      await writer.putAsset({ ...parentAsset,
        technicalFlags: [...parentAsset.technicalFlags, "R4_CHANGED_PARENT_ASSET"],
      });
    }
    const countsBefore = {
      approvals: (await writer.listApprovals()).length,
      exports: (await writer.listExportPackages()).length,
      revisions: (await writer.listRevisions(processed.body.stories[0].id)).length,
    };
    interpreterCalls = 0;
    interpreterTranscripts = [];
    const responses = [
      await postAsClient(app, client, `${root}/artifacts/${v2Artifacts[1]!.id}/approve`, {},
        "r4-current-evidence-approval"),
      await postAsClient(app, client, `${root}/approvals/${approved.body.approval.id}/export`, {},
        "r4-current-evidence-export"),
      await postAsClient(app, client, `${root}/stories/${processed.body.stories[0].id}/revisions`, {
        prompt: "Continue with the same second segment.",
      }, "r4-current-evidence-prompt"),
    ];
    for (const response of responses) {
      expect.soft(response.status).toBe(400);
      expect.soft(response.body).toEqual({ error: "creative-editorial-evidence-changed" });
    }
    expect.soft(interpreterCalls).toBe(0);
    expect.soft(interpreterTranscripts).not.toContain("R4_CHANGED_CURRENT_TRANSCRIPT");
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect.soft((await persisted.listApprovals()).length).toBe(countsBefore.approvals);
    expect.soft((await persisted.listExportPackages()).length).toBe(countsBefore.exports);
    expect.soft((await persisted.listRevisions(processed.body.stories[0].id)).length).toBe(countsBefore.revisions);
  }, 300_000);

  it.each([
    ["changed-source", "creative-editorial-evidence-changed"],
    ["unchanged-source", "creative-editorial-current-output-invalid"],
  ] as const)("does not let zero active current artifacts erase the %s prompt gate", async (mode, expectedError) => {
    const segmentAnalyzer = { analyze: async () => [
      { startMs: 0, endMs: 500, transcript: "我觉得手工应该留下痕迹。", signals: [] as [] },
      { startMs: 500, endMs: 1_000, transcript: "我决定坚持再做一次。", signals: [] as [] },
    ] };
    const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
      const cut = (outMs: number) => ({
        timeline: story.anchorSegmentIds.map((segmentId) => ({
          segmentId, inMs: 0, outMs, role: "anchor" as const,
        })),
        title: "A-0 zero-active evidence cut", caption: "Synthetic fixture",
        cover: { segmentId: story.anchorSegmentIds[0]!, atMs: 50 },
      });
      return { master: cut(100), xiaohongshu: cut(200), douyin: cut(250), wechatChannels: cut(300), stills: [] };
    } };
    let keepSegmentId = "";
    const timeline = (outMs: number) => [{
      segmentId: keepSegmentId, inMs: 0, outMs, role: "anchor" as const,
    }];
    const deps = creativeDependencies({ segmentAnalyzer, editAnalyzer });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async ({ revision }) => ({
      targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
      replacementTimelines: {
        "editorial-master": timeline(300), xiaohongshu: timeline(400), douyin: timeline(450), "wechat-channels": timeline(500),
      },
      removePeople: [], explanation: `V2 current evidence for ${revision.storyId}.`,
    }) } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "decline",
    }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    const capturedIds: string[] = [...new Set<string>((processed.body.stories as Array<{ anchorSegmentIds: string[] }>).flatMap(
      (story: { anchorSegmentIds: string[] }) => story.anchorSegmentIds,
    ))];
    expect(capturedIds).toHaveLength(2);
    keepSegmentId = capturedIds[1]!;
    const currentRevisions: EditRevision[] = [];
    const currentArtifactIds = new Set<string>();
    for (const story of processed.body.stories) {
      const revised = await post(app, `${root}/stories/${story.id}/revisions`, {
        prompt: "Use only the second captured segment in this current version.",
      }).expect(201);
      currentRevisions.push(revised.body.revision);
      for (const artifact of revised.body.artifacts) currentArtifactIds.add(artifact.id);
    }
    expect(currentArtifactIds.size).toBe(8);

    const writer = await JsonContentStore.open(join(directory, "library.json"));
    const stillUsed = (await writer.listSegments()).find((segment) => segment.id === keepSegmentId)!;
    if (mode === "changed-source") {
      await writer.putSegments([{ ...stillUsed, tags: {
        ...stillUsed.tags, transcript: "R4_ZERO_ACTIVE_CHANGED_TRANSCRIPT",
      } }]);
    }
    for (const artifact of (await writer.listAllArtifacts()).filter((item) => currentArtifactIds.has(item.id))) {
      await writer.putArtifact({ ...artifact, status: "invalidated" });
    }
    const before = {
      revisions: await writer.listRevisions(currentRevisions[0]!.storyId),
      heads: await writer.listCreativeRevisionHeads(),
      artifacts: await writer.listAllArtifacts(),
    };
    let interpreterCalls = 0;
    const interpreterTranscripts: string[] = [];
    let operationError: unknown;
    try {
      const next = await reviseWithPrompt({
        revision: currentRevisions[0]!, prompt: "Continue the current cut.", allowedSegments: [],
        designerId: "designer_01", deferPersistence: true,
      }, { interpret: async ({ allowedSegments }) => {
        interpreterCalls += 1;
        interpreterTranscripts.push(...allowedSegments.map((segment) => segment.tags.transcript));
        return { targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
          replacementTimelines: { "editorial-master": timeline(250), xiaohongshu: timeline(350),
            douyin: timeline(400), "wechat-channels": timeline(450) }, removePeople: [], explanation: "Should never be consumed" };
      } }, library);
      const snapshot = await JsonContentStore.open(join(directory, "library.json"));
      const rendered = await renderRevision(next, await snapshot.listSegments(), await snapshot.listAssets(), directory);
      await commitPromptRevision({ previous: currentRevisions[0]!, next, artifacts: rendered.artifacts,
        renderOutputRoot: directory, designerId: "designer_01" }, library);
    } catch (error) {
      operationError = error;
    }

    expect.soft(interpreterCalls).toBe(0);
    expect.soft(interpreterTranscripts).not.toContain("R4_ZERO_ACTIVE_CHANGED_TRANSCRIPT");
    expect.soft(operationError).toEqual(expect.objectContaining({ message: expectedError }));
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect.soft(await persisted.listRevisions(currentRevisions[0]!.storyId)).toEqual(before.revisions);
    expect.soft(await persisted.listCreativeRevisionHeads()).toEqual(before.heads);
    expect.soft(await persisted.listAllArtifacts()).toEqual(before.artifacts);
    if (mode === "unchanged-source") {
      expect.soft((await persisted.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid" });
      const detail = await request(app).get(root).expect(200);
      expect.soft(detail.body.session.processingTask).toMatchObject({
        status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid",
      });
      expect.soft(detail.body.session.revisions).toEqual([]);
      expect.soft(detail.body.session.artifacts).toEqual([]);
    } else {
      expect.soft((await persisted.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "reviewable", errorCode: null });
    }
  }, 300_000);

  it("rejects a same-bundle stale completion before HTTP capture can bypass the retake", async () => {
    const app = createApp();
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/events`, fieldEventBody(stepId, {
      action: "retake", operationId: "r4-bundle-first-retake",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    const completed = await post(app, `${root}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "r4-bundle-completed-take-2",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    expect(completed.body.event.take).toBe(2);
    const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId)!;
    let accepted = false;
    try {
      await library.commitCreativeBundle({ session, auditEvents: [], events: [
        { ...completed.body.event, action: "retake", id: "r4_bundle_retake_take_2",
          idempotencyKey: "r4-bundle-retake-take-2" },
        { ...completed.body.event, id: "r4_bundle_stale_completed_take_2",
          idempotencyKey: "r4-bundle-stale-completed-take-2" },
      ] });
      accepted = true;
    } catch (error) {
      expect(error).toEqual(expect.objectContaining({ message: "creative-capture-return-not-current" }));
    }
    if (accepted) {
      const capture = await post(app, `${root}/capture-handoff`, {
        mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01",
        deviceRole: "watch-director", carrier: "watch", take: 2, stepOutcome: "completed",
      }).expect(201);
      await post(app, `${root}/end-handoff`, { mode: "a0-simulated-phone-return" }).expect(201);
      const suggestion = await post(app, `${root}/post-shoot-material-suggestions`, {}).expect(201);
      await post(app, `${root}/post-shoot-material-viewing`, {
        suggestionSetId: suggestion.body.materialSuggestion.id, decision: "decline",
      }).expect(201);
      const output = await post(app, `${root}/editorial-process`, {}).expect(201);
      expect.soft(capture.body.steps).not.toContainEqual({
        stepId, state: "completed-returned", acceptedTake: 2,
      });
      expect.soft(output.body.processingTask.status).not.toBe("reviewable");
      expect.soft(output.body.artifacts).toHaveLength(0);
      for (const artifact of output.body.artifacts) {
        await authenticatedPreview(app, artifact.mediaUrl).expect(404);
      }
    }
    expect(accepted).toBe(false);
    expect((await library.listCreativeSessionEvents()).filter((event) =>
      event.sessionId === sessionId && event.take === 3)).toHaveLength(0);
  }, 300_000);

  it("uses the final coordinated snapshot when withdrawal lands after staged detail reads", async () => {
    let withdrawBeforeProjection = false;
    let withdrawn = false;
    let sourceSegmentId = "";
    let withdrawalStore: JsonContentStore;
    const staged = new Proxy(library, {
      get(target, property) {
        if (property === "projectCreativeMaterialState") return async (
          project: Parameters<typeof target.projectCreativeMaterialState>[0],
        ) => {
          if (withdrawBeforeProjection && !withdrawn) {
            withdrawn = true;
            await withdrawalStore.commitPrivacyInvalidation({
              segmentId: sourceSegmentId,
              reason: "withdraw immediately before final public projection",
              actorId: "customer_01",
            });
          }
          return target.projectCreativeMaterialState(project);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const deps = creativeDependencies({ store: staged as JsonContentStore });
    const app = createReviewServer({ ...deps, phonePublisher: {
      deliver: async () => ({ receiptId: "detail-race-receipt", receivedAt: "2026-09-08T10:05:00.000Z" }),
    } });
    const current = await deliveredCurrentCaptureFixture(app);
    sourceSegmentId = current.sourceSegmentId;
    withdrawalStore = await JsonContentStore.open(join(directory, "library.json"));
    withdrawBeforeProjection = true;

    const detail = await request(app).get(`/api/creative-sessions/${current.sessionId}`);
    expect(detail.status, JSON.stringify(detail.body)).toBe(200);
    expect(withdrawn).toBe(true);
    expect(detail.body.session.processingTask).toMatchObject({
      status: "failed-manual-review",
      errorCode: "creative-current-public-state-invalid",
      publishedClaimed: false,
    });
    expect(detail.body.session).toMatchObject({
      stories: [], revisions: [], artifacts: [], approvals: [], exportPackages: [], deliveries: [],
    });
    const publicEditorial = {
      processingTask: detail.body.session.processingTask,
      stories: detail.body.session.stories,
      revisions: detail.body.session.revisions,
      artifacts: detail.body.session.artifacts,
      approvals: detail.body.session.approvals,
      exportPackages: detail.body.session.exportPackages,
      deliveries: detail.body.session.deliveries,
    };
    expect(JSON.stringify(publicEditorial)).not.toContain('"status":"reviewable"');
    expect(JSON.stringify(publicEditorial)).not.toContain('"status":"active"');
    expect(JSON.stringify(publicEditorial)).not.toContain('"status":"delivered"');
    await authenticatedPreview(app,
      `/api/creative-sessions/${current.sessionId}/artifacts/${current.artifactId}/media`).expect(404);
  }, 300_000);

  async function deliveredCurrentCaptureFixture(app: ReturnType<typeof createReviewServer>) {
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "decline",
    }).expect(201);
    const client = await defaultCreativeClient(app);
    const root = `/api/creative-sessions/${sessionId}`;
    const processed = await postAsClient(app, client, `${root}/editorial-process`, {},
      "captured-withdrawal-process").expect(201);
    const artifact = processed.body.artifacts.find((item: { kind: string }) => item.kind === "wechat-channels");
    const approvalPath = `${root}/artifacts/${artifact.id}/approve`;
    const approved = await postAsClient(app, client, approvalPath, {},
      "captured-withdrawal-approve").expect(201);
    const exportPath = `${root}/approvals/${approved.body.approval.id}/export`;
    const exported = await postAsClient(app, client, exportPath, {},
      "captured-withdrawal-export").expect(201);
    const deliveryPath = `${root}/export-packages/${exported.body.package.id}/deliver`;
    const delivered = await postAsClient(app, client, deliveryPath, {},
      "captured-withdrawal-deliver").expect(201);
    return {
      sessionId, client, artifactId: artifact.id as string,
      sourceSegmentId: artifact.sourceSegmentIds[0] as string,
      approvalId: approved.body.approval.id as string,
      packageId: exported.body.package.id as string,
      operations: [
        { path: approvalPath, body: {}, key: "captured-withdrawal-approve",
          successIdentity: approved.body.approval.id as string },
        { path: exportPath, body: {}, key: "captured-withdrawal-export",
          successIdentity: exported.body.package.id as string },
        { path: deliveryPath, body: {}, key: "captured-withdrawal-deliver",
          successIdentity: delivered.body.delivery.receiptId as string },
      ],
    };
  }

  async function promptSourceFixture(app: ReturnType<typeof createReviewServer>, history: boolean) {
    const { sessionId } = await postShootFixture(app);
    const capture = (await library.listAssets()).find((asset) => asset.origin === "new")!;
    const historyId = "final_authorized_history";
    const asset = { ...historicalAsset(directory), id: "final_history_asset", localPath: join(directory, "final-history.mp4"),
      digest: createHash("sha256").update(await readFile(capture.localPath)).digest("hex") };
    await cp(capture.localPath, asset.localPath);
    await library.putAsset(asset);
    await library.putSegments([{ ...historicalSegment(), id: historyId, assetId: asset.id,
      tags: { ...historicalSegment().tags, transcript: "Authorized prior craftsmanship" } }]);
    const root = `/api/creative-sessions/${sessionId}`;
    const proposed = await post(app, `${root}/post-shoot-material-suggestions`, {}).expect(201);
    const suggestionSetId = proposed.body.materialSuggestion.id;
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: history ? "accept" : "decline" }).expect(201);
    if (history) await post(app, `${root}/post-shoot-material-selection`, { suggestionSetId, selectedSegmentIds: [historyId] }).expect(201);
    return { sessionId, suggestionSetId, historyId };
  }

  async function mixedCurrentFixture() {
    const app = createApp();
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId, decision: "decline",
    }).expect(201);
    const processed = await post(app, `/api/creative-sessions/${sessionId}/editorial-process`, {}).expect(201);
    expect(processed.body.stories).toHaveLength(2);
    const revised = await post(app, `/api/creative-sessions/${sessionId}/stories/${processed.body.stories[0].id}/revisions`, {
      prompt: "视频号开头先说结果。",
    }).expect(201);
    expect(revised.body.revision.revision).toBe(2);
    return { app, sessionId, processed, revised };
  }

  // Catches an instance projecting its cached acceptance after another legal handle durably withdraws it.
  it.each(["observer-first", "withdrawer-first", "restarted-observer"].flatMap((order) =>
    ["accept-replay", "selection-replay", "accept-receipt", "selection-receipt", "detail", "candidates", "preview", "use", "editorial-use"]
      .map((boundary) => ({ order, boundary }))))(
    "reads durable consent across handles for $boundary with $order", async ({ order, boundary }) => {
      const { sessionId, suggestionSetId } = await postShootFixture(createApp());
      const openServer = async () => createReviewServer(creativeDependencies({
        store: await JsonContentStore.open(join(directory, "library.json")),
      }));
      let withdrawer = order === "withdrawer-first" ? await openServer() : null;
      const observer = await openServer();
      const client = await defaultCreativeClient(observer);
      const acceptPath = `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`;
      const selectionPath = `/api/creative-sessions/${sessionId}/post-shoot-material-selection`;
      const acceptBody = { suggestionSetId, decision: "accept" };
      const selectionBody = { suggestionSetId, selectedSegmentIds: [SEGMENT_ID] };
      const acceptKey = "r2-fix2-original-accept-0001";
      const selectionKey = "r2-fix2-original-selection-0001";
      const accepted = await postAsClient(observer, client, acceptPath, acceptBody, acceptKey).expect(201);
      const selected = await postAsClient(observer, client, selectionPath, selectionBody, selectionKey).expect(201);
      const receipt = await readReceipt(observer, client, selectionKey).expect(200);
      expect(receipt.body.receipt.request.body).toEqual(selectionBody);
      expect(receipt.body.receipt.response.body).toEqual(selected.body);
      expect((await postAsClient(observer, client, acceptPath, acceptBody, acceptKey).expect(201)).body).toEqual(accepted.body);
      expect((await postAsClient(observer, client, selectionPath, selectionBody, selectionKey).expect(201)).body).toEqual(selected.body);
      withdrawer ??= await openServer();
      await post(withdrawer, acceptPath, { suggestionSetId, decision: "decline" }, "r2-fix2-decline-0001").expect(201);
      await request(withdrawer).get(`/api/creative-sessions/${sessionId}/material-suggestions`).expect(400);
      const durableBefore = await JsonContentStore.open(join(directory, "library.json"));
      const decisions = await durableBefore.listMaterialUseDecisions();
      const audits = await durableBefore.listAuditEvents(sessionId);
      const target = order === "restarted-observer" ? await openServer() : observer;
      const targetClient = order === "restarted-observer" ? await defaultCreativeClient(target) : client;
      const response = boundary.endsWith("replay")
        ? await postAsClient(target, targetClient, boundary.startsWith("accept") ? acceptPath : selectionPath,
          boundary.startsWith("accept") ? acceptBody : selectionBody, boundary.startsWith("accept") ? acceptKey : selectionKey)
        : boundary.endsWith("receipt") ? await readReceipt(target, targetClient, boundary.startsWith("accept") ? acceptKey : selectionKey)
          : boundary === "editorial-use" ? await postAsClient(target, targetClient, `/api/creative-sessions/${sessionId}/editorial-process`, {}, "r2-fix2-editorial-use-0001")
            : boundary === "use" ? await postAsClient(target, targetClient, selectionPath, selectionBody, "r2-fix2-new-use-0001")
            : boundary === "preview" ? await authenticatedPreview(target, `/api/creative-sessions/${sessionId}/material-suggestions/${SEGMENT_ID}/preview`)
              : await request(target).get(`/api/creative-sessions/${sessionId}${boundary === "candidates" ? "/material-suggestions" : ""}`);
      let projected = response.body;
      if (boundary === "detail") {
        expect(response.status).toBe(200);
        expect(projected.session.postShootMaterialDecision.action).toBe("decline-view");
        projected = { materialDecision: projected.session.materialDecision, suggestionSet: projected.session.suggestionSet,
          postShootMaterialDecision: projected.session.postShootMaterialDecision, postShootSuggestionSet: projected.session.postShootSuggestionSet };
      } else if (boundary === "editorial-use") {
        expect(response.status).toBe(201);
        expect(projected.processingTask).toMatchObject({ status: "reviewable", publishedClaimed: false });
        expect(projected.stories.every((story: { historicalSegmentIds: string[] }) => story.historicalSegmentIds.length === 0)).toBe(true);
      } else expect([400, 403, 404]).toContain(response.status);
      for (const forbidden of [SEGMENT_ID, "shownSegmentIds", "selectedSegmentIds", "rejectedSegmentIds", "thumbnailPath", directory]) {
        expect(JSON.stringify(projected)).not.toContain(forbidden);
      }
      const durableAfter = await JsonContentStore.open(join(directory, "library.json"));
      expect(await durableAfter.listMaterialUseDecisions()).toEqual(decisions);
      if (boundary === "editorial-use") expect(await durableAfter.listAuditEvents(sessionId)).toEqual(expect.arrayContaining(audits));
      else expect(await durableAfter.listAuditEvents(sessionId)).toEqual(audits);
      if (boundary === "selection-replay") {
        await post(withdrawer, acceptPath, acceptBody, "r2-fix2-fresh-accept-0001").expect(201);
        expect((await postAsClient(observer, client, selectionPath, selectionBody, selectionKey).expect(201)).body).toEqual(selected.body);
        expect((await readReceipt(observer, client, selectionKey).expect(200)).body).toEqual(receipt.body);
      }
    }, 300_000);

  // Catches historical output escaping through cached success/receipt bodies anywhere in the export chain.
  it.each([true, false])("projects cached editorial-chain responses by their actual historical lineage: history=%s", async (history) => {
    const metadata = "private-history-caption-fix3";
    const originalPlanner = editorialAnalyzers().editAnalyzer;
    const editAnalyzer: EditPlanAnalyzer = { plan: async (input) => {
      const plan = await originalPlanner.plan(input);
      const historicalId = input.story.historicalSegmentIds[0];
      if (!historicalId) return plan;
      const includeHistory = (variant: typeof plan.master) => ({ ...variant,
        title: metadata, caption: metadata,
        timeline: [...variant.timeline, { ...variant.timeline[0]!, segmentId: historicalId }],
        cover: { segmentId: historicalId, atMs: 100 },
      });
      return { ...plan, master: includeHistory(plan.master), xiaohongshu: includeHistory(plan.xiaohongshu),
        wechatChannels: includeHistory(plan.wechatChannels) };
    } };
    let holdPrompt = false;
    let reachedPrompt!: () => void;
    let releasePrompt!: () => void;
    const interpreting = new Promise<void>((resolve) => { reachedPrompt = resolve; });
    const continuePrompt = new Promise<void>((resolve) => { releasePrompt = resolve; });
    const deps = creativeDependencies({ editAnalyzer });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async (...args) => {
      if (holdPrompt) {
        reachedPrompt();
        await continuePrompt;
        throw new Error("test-prompt-cancelled");
      }
      return deps.interpreter.interpret(...args);
    } }, phonePublisher: {
      deliver: async () => ({ receiptId: "fix3-local-only-delivery", receivedAt: new Date().toISOString() }),
    } });
    const { sessionId } = await postShootFixture(app);
    const capture = (await library.listAssets()).find((asset) => asset.origin === "new")!;
    const historyId = "history_rework_fix3_chain";
    const historyAsset = { ...historicalAsset(directory), id: "historical_asset_fix3_chain",
      localPath: join(directory, "chain-history.mp4"), digest: createHash("sha256")
        .update(await readFile(capture.localPath)).digest("hex") };
    await cp(capture.localPath, historyAsset.localPath);
    await library.putAsset(historyAsset);
    await library.putSegments([{ ...historicalSegment(), id: historyId, assetId: historyAsset.id }]);
    const root = `/api/creative-sessions/${sessionId}`;
    const proposed = await post(app, `${root}/post-shoot-material-suggestions`, {}).expect(201);
    const suggestionSetId = proposed.body.materialSuggestion.id;
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: history ? "accept" : "decline" }).expect(201);
    if (history) await post(app, `${root}/post-shoot-material-selection`, { suggestionSetId, selectedSegmentIds: [historyId] }).expect(201);
    const client = await defaultCreativeClient(app);
    const operations: Array<{ path: string; body: Record<string, unknown>; key: string; response: any; receipt?: any }> = [];
    const execute = async (path: string, body: Record<string, unknown>, key: string) => {
      const response = await postAsClient(app, client, path, body, key).expect(201);
      operations.push({ path, body, key, response });
      return response;
    };
    const processed = await execute(`${root}/editorial-process`, {}, "fix3-chain-process-original");
    const story = processed.body.stories.find((value: { historicalSegmentIds: string[] }) =>
      history ? value.historicalSegmentIds.includes(historyId) : value.historicalSegmentIds.length === 0);
    expect(story).toBeDefined();
    const revised = await execute(`${root}/stories/${story.id}/revisions`, { prompt: "Tighten this current cut." }, "fix3-chain-revision-original");
    const artifact = revised.body.artifacts.find((value: { kind: string }) => value.kind === "wechat-channels");
    expect(artifact.sourceSegmentIds.includes(historyId)).toBe(history);
    const approved = await execute(`${root}/artifacts/${artifact.id}/approve`, {}, "fix3-chain-approval-original");
    const exported = await execute(`${root}/approvals/${approved.body.approval.id}/export`, {}, "fix3-chain-export-original");
    const delivered = await execute(`${root}/export-packages/${exported.body.package.id}/deliver`, {}, "fix3-chain-delivery-original");
    expect(delivered.body.delivery.publishedClaimed).toBe(false);
    for (const operation of operations) {
      expect((await postAsClient(app, client, operation.path, operation.body, operation.key).expect(201)).body).toEqual(operation.response.body);
      operation.receipt = (await readReceipt(app, client, operation.key).expect(200)).body;
      expect(operation.receipt.receipt.response.body).toEqual(operation.response.body);
    }
    holdPrompt = true;
    const pendingKey = "fix3-chain-inflight-revision";
    const pendingBody = { prompt: history ? `Remove ${historyId}: ${metadata}` : "Tighten only the new footage." };
    const pending = Promise.resolve(postAsClient(app, client, `${root}/stories/${story.id}/revisions`, pendingBody, pendingKey));
    try {
      await Promise.race([interpreting, pending.then(() => { throw new Error("prompt-finished-before-schedule"); })]);
      const withdrawingStore = await JsonContentStore.open(join(directory, "library.json"));
      const withdrawer = createReviewServer(creativeDependencies({ store: withdrawingStore }));
      await post(withdrawer, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
      const audit = await withdrawingStore.listAuditEvents(sessionId);
      const decisions = await withdrawingStore.listMaterialUseDecisions();
      const inflight = await readReceipt(app, client, pendingKey);
      if (history) {
        expect.soft([400, 403]).toContain(inflight.status);
        expect.soft(JSON.stringify(inflight.body)).not.toContain(historyId);
        expect.soft(JSON.stringify(inflight.body)).not.toContain(metadata);
      } else {
        expect(inflight.status).toBe(200);
        expect(inflight.body.receipt).toMatchObject({ status: "inflight", request: { body: pendingBody } });
      }
      for (const operation of operations) {
        const replay = await postAsClient(app, client, operation.path, operation.body, operation.key);
        const receipt = await readReceipt(app, client, operation.key);
        if (history) {
          for (const response of [replay, receipt]) {
            expect.soft([400, 403], operation.path).toContain(response.status);
            for (const forbidden of [historyId, metadata, artifact.id, revised.body.revision.id,
              approved.body.approval.id, exported.body.package.id, directory]) {
              expect.soft(JSON.stringify(response.body), operation.path).not.toContain(forbidden);
            }
          }
        } else {
          expect(replay.status).toBe(201);
          expect(replay.body).toEqual(operation.response.body);
          expect(receipt.status).toBe(200);
          expect(receipt.body).toEqual(operation.receipt);
        }
      }
      releasePrompt();
      expect((await pending).status).toBe(400);
      const failedReceipt = await readReceipt(app, client, pendingKey);
      if (history) {
        expect.soft([400, 403]).toContain(failedReceipt.status);
        expect.soft(JSON.stringify(failedReceipt.body)).not.toContain(historyId);
        expect.soft(JSON.stringify(failedReceipt.body)).not.toContain(metadata);
      } else {
        expect(failedReceipt.status).toBe(200);
        expect(failedReceipt.body.receipt.response.status).toBe(400);
        expect(failedReceipt.body.receipt.request.body).toEqual(pendingBody);
      }
      const durable = await JsonContentStore.open(join(directory, "library.json"));
      expect(await durable.listAuditEvents(sessionId)).toEqual(audit);
      expect(await durable.listMaterialUseDecisions()).toEqual(decisions);
    } finally { releasePrompt(); }
  }, 300_000);

  // Catches quoted historical identities escaping even when the rendered timeline uses only new footage.
  it("requires current viewing for historical identities quoted in a new-only revision prompt", async () => {
    const app = createApp();
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    expect(processed.body.stories.every((story: { historicalSegmentIds: string[] }) => story.historicalSegmentIds.length === 0)).toBe(true);
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "accept" }).expect(201);
    const client = await defaultCreativeClient(app);
    const storyId = processed.body.stories[0].id;
    const path = `${root}/stories/${storyId}/revisions`;
    const key = "fix3-quoted-identity-new-only";
    const body = { prompt: `Exclude ${SEGMENT_ID} and asset historical_asset_01 from this cut.` };
    const revised = await postAsClient(app, client, path, body, key).expect(201);
    expect(revised.body.revision.designerPrompt).toBe(body.prompt);
    expect(revised.body.artifacts.every((artifact: { sourceSegmentIds: string[] }) => !artifact.sourceSegmentIds.includes(SEGMENT_ID))).toBe(true);
    expect((await library.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id)?.checkpoint?.authorization.selectedSegmentIds).toEqual([]);
    expect((await postAsClient(app, client, path, body, key).expect(201)).body).toEqual(revised.body);
    const originalReceipt = (await readReceipt(app, client, key).expect(200)).body;
    expect(originalReceipt.receipt.request.body).toEqual(body);
    expect(originalReceipt.receipt.response.body).toEqual(revised.body);
    await request(app).get(root).expect(200);
    const withdrawingStore = await JsonContentStore.open(join(directory, "library.json"));
    const withdrawer = createReviewServer(creativeDependencies({ store: withdrawingStore }));
    await post(withdrawer, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    for (const response of [await postAsClient(app, client, path, body, key), await readReceipt(app, client, key),
      await request(app).get(root)]) {
      expect.soft([400, 403]).toContain(response.status);
      for (const id of [SEGMENT_ID, "historical_asset_01"]) expect.soft(JSON.stringify(response.body)).not.toContain(id);
    }
    const restartedStore = await JsonContentStore.open(join(directory, "library.json"));
    const restarted = createReviewServer(creativeDependencies({ store: restartedStore }));
    rememberCreativeClientClaim(restarted, client, creativeClientClaim(app, client));
    const restartedReceipt = await readReceipt(restarted, client, key);
    expect.soft([400, 403]).toContain(restartedReceipt.status);
    expect.soft(JSON.stringify(restartedReceipt.body)).not.toContain(body.prompt);
    expect((await restartedStore.listRevisions(storyId)).find((revision) => revision.id === revised.body.revision.id)?.designerPrompt).toBe(body.prompt);
    await post(withdrawer, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "accept" }).expect(201);
    expect((await postAsClient(app, client, path, body, key).expect(201)).body).toEqual(revised.body);
    expect((await readReceipt(app, client, key).expect(200)).body).toEqual(originalReceipt);
    await expect((await JsonContentStore.open(join(directory, "library.json"))).listRevisions(storyId)).resolves.toHaveLength(2);
  }, 300_000);

  // Catches quadratic rescanning of repeated exact IDs in a legal near-limit public receipt.
  it("bounds repeated-ID public receipt growth and event-loop blocking near the request limit", async () => {
    await library.putSegments([{ ...historicalSegment(), id: "h7" }]);
    const deps = creativeDependencies();
    const app = createReviewServer({ ...deps, interpreter: { interpret: async () => {
      throw new Error("synthetic-prompt-rejected");
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    const viewingPath = `${root}/post-shoot-material-viewing`;
    await post(app, viewingPath, { suggestionSetId, decision: "decline" }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    await post(app, viewingPath, { suggestionSetId, decision: "accept" }).expect(201);
    const client = await defaultCreativeClient(app);
    const path = `${root}/stories/${processed.body.stories[0].id}/revisions`;
    const samples: Array<{ bytes: number; elapsedMs: number; maxLoopGapMs: number }> = [];
    for (const repetitions of [1_000, 32_000, 320_000]) {
      const body = { prompt: "h7 ".repeat(repetitions) };
      const bytes = Buffer.byteLength(JSON.stringify(body));
      expect(bytes).toBeLessThan(1024 * 1024);
      const key = `fix5-repeated-id-${repetitions}`;
      // The real POST accepts the whole JSON; an interpreter error gives a completed receipt
      // without spending the benchmark on video rendering or mutating the current revision.
      const failed = await postAsClient(app, client, path, body, key).expect(400);
      expect(failed.body).toEqual({ error: "synthetic-prompt-rejected" });
      let previousTick = performance.now();
      let maxLoopGapMs = 0;
      const tick = () => {
        const now = performance.now();
        maxLoopGapMs = Math.max(maxLoopGapMs, now - previousTick);
        previousTick = now;
      };
      const heartbeat = setInterval(tick, 5);
      const started = performance.now();
      let receipt: any;
      let elapsedMs: number;
      try {
        receipt = await readReceipt(app, client, key).expect(200);
        elapsedMs = performance.now() - started;
        // Allow the heartbeat delayed by the synchronous projection to actually fire.
        await new Promise<void>((resolve) => setTimeout(resolve, 10));
        tick();
      } finally {
        clearInterval(heartbeat);
      }
      expect(receipt.body.receipt.request.body).toEqual(body);
      expect(receipt.body.receipt.response).toEqual({ status: 400, body: failed.body });
      if (repetitions !== 1_000) samples.push({ bytes, elapsedMs: elapsedMs!, maxLoopGapMs });
    }
    console.info("repeated-ID public receipt performance", JSON.stringify(samples));
    const [small, large] = samples;
    expect(large.bytes / small.bytes).toBeGreaterThan(9.9);
    // Ten times the input permits 25 times the cost plus 1 s scheduling/GC noise.
    // O(n log n) has ample headroom; the old O(n²) scan grows about 100 times.
    expect.soft(large.elapsedMs, "receipt growth must stay below quadratic").toBeLessThan(small.elapsedMs * 25 + 1_000);
    expect.soft(large.maxLoopGapMs, "the event loop must not suffer quadratic blocking").toBeLessThan(small.maxLoopGapMs * 25 + 1_000);
    await post(app, viewingPath, { suggestionSetId, decision: "decline" }).expect(201);
    const denied = await readReceipt(app, client, "fix5-repeated-id-320000");
    expect([400, 403]).toContain(denied.status);
    expect(JSON.stringify(denied.body)).not.toContain("h7");
    expect((await library.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
      .toMatchObject({ status: "reviewable", publishedClaimed: false });
  }, 300_000);

  // Catches treating overlapping intervals as one containing identity, or ignoring JSON keys.
  it("preserves literal containment and crossing identities throughout nested public receipts", async () => {
    const ids = ["clip", "clip.01", "a.b.c", "c.d.e", "b.c.d", "b.c", "历史甲"];
    await library.putSegments(ids.map((id) => ({ ...historicalSegment(), id })));
    const visible = new Set([SEGMENT_ID, "clip.01", "a.b.c", "c.d.e", "历史甲"]);
    const deps = creativeDependencies({ materialMatcher: { rank: async ({ segments }) => segments
      .filter((segment) => visible.has(segment.id))
      .map((segment) => ({ segmentId: segment.id, reason: "synthetic exact match", score: 90 })) } });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async () => {
      throw new Error("synthetic-prompt-rejected");
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "accept" }).expect(201);
    const client = await defaultCreativeClient(app);
    const path = `${root}/stories/${processed.body.stories[0].id}/revisions`;
    const cases = [
      { note: ["clip.01", { "clip.01": "a.b.c" }], allowed: true },
      { note: ["clip.01", { clip: "new-only" }], allowed: false },
      { note: ["clip.01", { nested: ["clip"] }], allowed: false },
      { note: ["a.b.c.d.e"], allowed: false },
      { note: ["a.b.c", "c.d.e", "a.b.c"], allowed: true },
      { note: { "请看历史甲素材": ["🎥历史甲\n", "历史甲\t"] }, allowed: true },
    ];
    for (const [index, { note, allowed }] of cases.entries()) {
      const body = { prompt: "Only new footage.", note };
      const key = `fix5-nested-identity-${index}`;
      const failed = await postAsClient(app, client, path, body, key).expect(400);
      const receipt = await readReceipt(app, client, key);
      if (allowed) {
        expect(receipt.status).toBe(200);
        expect(receipt.body.receipt.request.body).toEqual(body);
        expect(receipt.body.receipt.response).toEqual({ status: 400, body: failed.body });
      } else {
        expect([400, 403]).toContain(receipt.status);
        expect(receipt.body).toEqual({ error: "historical-authorization-invalid" });
      }
    }
    await post(app, `${root}/post-shoot-material-viewing`, { suggestionSetId, decision: "decline" }).expect(201);
    for (const index of [0, 4, 5]) {
      const receipt = await readReceipt(app, client, `fix5-nested-identity-${index}`);
      expect([400, 403]).toContain(receipt.status);
      expect(receipt.body).toEqual({ error: "historical-authorization-invalid" });
    }
  }, 300_000);

  // Catches missed literal identities in multilingual text and false positives inside other identifiers.
  // Each case drives the real prompt/render/receipt/detail routes; expected identities are hand-written.
  it.each([
    { name: "Chinese both sides", prompt: "请不要使用history_rework_01素材和history_asset_rework素材，只保留新拍摄内容。", ids: ["history_rework_01", "history_asset_rework"] },
    { name: "Chinese before", prompt: "不要使用history_rework_01", ids: ["history_rework_01"] },
    { name: "Chinese after", prompt: "history_asset_rework素材不用", ids: ["history_asset_rework"] },
    { name: "Japanese", prompt: "このhistory_rework_01素材とhistory_asset_rework素材を使わない", ids: ["history_rework_01", "history_asset_rework"] },
    { name: "emoji", prompt: "🎥history_rework_01🚫history_asset_rework✨", ids: ["history_rework_01", "history_asset_rework"] },
    { name: "punctuation", prompt: "（history_rework_01），【history_asset_rework】。", ids: ["history_rework_01", "history_asset_rework"] },
    { name: "newlines and tabs", prompt: "仅用新素材\nhistory_rework_01\n\thistory_asset_rework\t", ids: ["history_rework_01", "history_asset_rework"] },
    { name: "short exact ID", prompt: "不要用h7素材", ids: ["h7"] },
    { name: "known long ID", prompt: "不要用history_rework_01_extended素材", ids: ["history_rework_01_extended"] },
    { name: "mixed case exact ID", prompt: "不要用History_Mixed_01素材", ids: ["History_Mixed_01"] },
    { name: "literal metacharacters", prompt: "不要用history[part].01素材", ids: ["history[part].01"] },
    { name: "Unicode exact ID", prompt: "不要用旧素材甲画面", ids: ["旧素材甲"] },
    { name: "visible longer ID with hidden short prefix", prompt: "仅排除clip.01。", ids: ["clip.01"] },
    { name: "ASCII embedded IDs", prompt: "prehistory_rework_01 history_rework_01suffix _history_asset_rework history_asset_rework-2", ids: [] },
    { name: "ordinary words around a short ID", prompt: "Keep the craftsmanship and heart of the new shot.", ids: [] },
    { name: "unknown longer ID", prompt: "只用history_rework_01_extended_extra和h77镜头", ids: [] },
    { name: "case-sensitive non-identities", prompt: "排除HISTORY_REWORK_01和history_mixed_01镜头", ids: [] },
    { name: "new-only text", prompt: "只保留新拍摄内容，开头更短。", ids: [] },
  ])("projects multilingual historical identities without substring false positives: $name", async ({ prompt, ids }) => {
    const catalogIds = ["history_rework_01", "h7", "art", "history_rework_01_extended", "History_Mixed_01", "history[part].01", "旧素材甲", "clip", "clip.01"];
    await library.putAsset({ ...historicalAsset(directory), id: "history_asset_rework", digest: "synthetic-fix4-history" });
    await library.putSegments(catalogIds.map((id) => ({ ...historicalSegment(), id, assetId: "history_asset_rework" })));
    const matcher: MaterialMatcher = { rank: async ({ segments }) => segments.filter((segment) => segment.id !== "clip")
      .map((segment, index) => ({ segmentId: segment.id, reason: "same confirmed product", score: 100 - index })) };
    let rejectPrompt = false;
    const deps = creativeDependencies({ materialMatcher: matcher });
    const app = createReviewServer({ ...deps, interpreter: { interpret: async (...args) => {
      if (rejectPrompt) throw new Error("synthetic-prompt-rejected");
      return deps.interpreter.interpret(...args);
    } } });
    const { sessionId, suggestionSetId } = await postShootFixture(app);
    const root = `/api/creative-sessions/${sessionId}`;
    const viewingPath = `${root}/post-shoot-material-viewing`;
    await post(app, viewingPath, { suggestionSetId, decision: "decline" }).expect(201);
    const processed = await post(app, `${root}/editorial-process`, {}).expect(201);
    expect(processed.body.stories.every((story: { historicalSegmentIds: string[] }) => story.historicalSegmentIds.length === 0)).toBe(true);
    await post(app, viewingPath, { suggestionSetId, decision: "accept" }).expect(201);
    const client = await defaultCreativeClient(app);
    const storyId = processed.body.stories[0].id;
    const path = `${root}/stories/${storyId}/revisions`;
    const body = { prompt };
    const successKey = "fix4-multilingual-success";
    const failedKey = "fix4-multilingual-failed";
    const revised = await postAsClient(app, client, path, body, successKey).expect(201);
    expect(revised.body.revision.designerPrompt).toBe(prompt);
    expect(revised.body.artifacts.every((artifact: { sourceSegmentIds: string[] }) =>
      artifact.sourceSegmentIds.every((id) => !catalogIds.includes(id)))).toBe(true);
    rejectPrompt = true;
    const failed = await postAsClient(app, client, path, body, failedKey).expect(400);
    const successReceipt = (await readReceipt(app, client, successKey).expect(200)).body;
    const failedReceipt = (await readReceipt(app, client, failedKey).expect(200)).body;
    expect(successReceipt.receipt.request.body).toEqual(body);
    expect(successReceipt.receipt.response.body).toEqual(revised.body);
    expect(failedReceipt.receipt.request.body).toEqual(body);
    expect(failedReceipt.receipt.response).toEqual({ status: 400, body: failed.body });
    const authorizedDetail = (await request(app).get(root).expect(200)).body.session;
    const withdrawer = createReviewServer(creativeDependencies({ store: await JsonContentStore.open(join(directory, "library.json")) }));
    await post(withdrawer, viewingPath, { suggestionSetId, decision: "decline" }).expect(201);
    const durableBefore = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
    const responses = [
      { label: "success replay", result: await postAsClient(app, client, path, body, successKey), status: 201, original: revised.body },
      { label: "success receipt", result: await readReceipt(app, client, successKey), status: 200, original: successReceipt },
      { label: "failed receipt", result: await readReceipt(app, client, failedKey), status: 200, original: failedReceipt },
    ];
    for (const { label, result, status, original } of responses) {
      if (ids.length > 0) {
        for (const id of ids) expect.soft(JSON.stringify(result.body), label).not.toContain(id);
        expect.soft([400, 403], label).toContain(result.status);
      } else {
        expect.soft(result.status, label).toBe(status);
        expect.soft(result.body, label).toEqual(original);
      }
    }
    // The safe failed response itself contains no prompt; its same-key status/body stays exact.
    const failedReplay = await postAsClient(app, client, path, body, failedKey);
    expect(failedReplay.status).toBe(400);
    expect(failedReplay.body).toEqual(failed.body);
    const restartedStore = await JsonContentStore.open(join(directory, "library.json"));
    const restarted = createReviewServer(creativeDependencies({ store: restartedStore }));
    for (const target of [app, restarted]) {
      const detail = await request(target).get(root);
      if (ids.length > 0) {
        for (const id of ids) expect.soft(JSON.stringify(detail.body), "detail").not.toContain(id);
        expect.soft([400, 403], "detail").toContain(detail.status);
      } else {
        expect(detail.status).toBe(200);
        for (const field of ["stories", "revisions", "artifacts", "approvals", "exportPackages", "deliveries"]) {
          expect(detail.body.session[field], field).toEqual(authorizedDetail[field]);
        }
      }
    }
    expect(JSON.parse(await readFile(join(directory, "library.json"), "utf8"))).toEqual(durableBefore);
    await post(withdrawer, viewingPath, { suggestionSetId, decision: "accept" }).expect(201);
    expect((await postAsClient(app, client, path, body, successKey).expect(201)).body).toEqual(revised.body);
    expect((await readReceipt(app, client, successKey).expect(200)).body).toEqual(successReceipt);
    expect((await readReceipt(app, client, failedKey).expect(200)).body).toEqual(failedReceipt);
    for (const target of [app, restarted]) {
      const restored = (await request(target).get(root).expect(200)).body.session;
      expect(restored.revisions).toEqual(authorizedDetail.revisions);
    }
    const durable = await JsonContentStore.open(join(directory, "library.json"));
    const task = (await durable.listCreativeProcessingTasks()).find((value) => value.id === processed.body.processingTask.id)!;
    expect(task).toMatchObject({ status: "reviewable", publishedClaimed: false });
    expect(task.checkpoint?.authorization.selectedSegmentIds).toEqual([]);
    expect((await durable.listRevisions(storyId)).map((revision) => revision.designerPrompt)).toContain(prompt);
    expect(await durable.listRevisions(storyId)).toHaveLength(2);
  }, 300_000);

  // Catches an already-read editorial bundle escaping after another handle changes its consent.
  it.each(["observer-first", "withdrawer-first"].flatMap((order) => [false, true].flatMap((alias) =>
    ["unchanged", "decline", "decline-reaccept"].map((change) => ({ order, alias, change })))))(
    "keeps the whole detail consent-consistent across an editorial read race: $order alias=$alias $change",
    async ({ order, alias, change }) => {
      const setup = createApp();
      const { sessionId } = await postShootFixture(setup);
      const historyId = "history_rework_fix3_post_shoot_only";
      await library.putSegments([{ ...historicalSegment(), id: historyId }]);
      const proposed = await post(setup, `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
      const suggestionSetId = proposed.body.materialSuggestion.id as string;
      const viewingPath = `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`;
      await post(setup, viewingPath, { suggestionSetId, decision: "accept" }).expect(201);
      await post(setup, `/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
        suggestionSetId, selectedSegmentIds: [historyId],
      }).expect(201);
      const processed = await post(setup, `/api/creative-sessions/${sessionId}/editorial-process`, {}).expect(201);
      expect(processed.body.stories.some((story: { historicalSegmentIds: string[] }) =>
        story.historicalSegmentIds.includes(historyId))).toBe(true);
      expect(processed.body.artifacts).toHaveLength(8);
      const sharedPath = join(directory, "library.json");
      const aliasPath = join(directory, "detail-consent-alias.json");
      if (alias) await symlink(sharedPath, aliasPath);
      const openWithdrawer = async () => createReviewServer(creativeDependencies({
        store: await JsonContentStore.open(alias ? aliasPath : sharedPath),
      }));
      let withdrawer = order === "withdrawer-first" ? await openWithdrawer() : null;
      const observerStore = await JsonContentStore.open(sharedPath);
      const observer = createReviewServer(creativeDependencies({ store: observerStore }));
      withdrawer ??= await openWithdrawer();
      const detailPath = `/api/creative-sessions/${sessionId}`;
      const authorized = await request(observer).get(detailPath).expect(200);
      expect(JSON.stringify(authorized.body.session.script)).toContain(SEGMENT_ID);
      expect(JSON.stringify(authorized.body.session.script)).not.toContain(historyId);
      expect(JSON.stringify(authorized.body.session.stories)).toContain(historyId);
      expect(authorized.body.session.artifacts).toHaveLength(8);

      let reached!: () => void;
      let release!: () => void;
      const alreadyRead = new Promise<void>((resolve) => { reached = resolve; });
      const continueProjection = new Promise<void>((resolve) => { release = resolve; });
      const actualRead = observerStore.readCreativeEditorialBundle.bind(observerStore);
      observerStore.readCreativeEditorialBundle = async (...args) => {
        const bundle = await actualRead(...args);
        reached();
        await continueProjection;
        return bundle;
      };
      const pending = Promise.resolve(request(observer).get(detailPath));
      try {
        await Promise.race([alreadyRead, pending.then(() => {
          throw new Error("detail-finished-before-editorial-read-schedule");
        })]);
        if (change !== "unchanged") {
          await post(withdrawer, viewingPath, { suggestionSetId, decision: "decline" }).expect(201);
          await request(withdrawer).get(`${detailPath}/material-suggestions`).expect(400);
          if (change === "decline-reaccept") {
            await post(withdrawer, viewingPath, { suggestionSetId, decision: "accept" }).expect(201);
          }
        }
      } finally { release(); }
      const response = await pending;
      observerStore.readCreativeEditorialBundle = actualRead;
      if (change === "unchanged") {
        expect(response.status).toBe(200);
        expect(response.body).toEqual(authorized.body);
      } else {
        for (const forbidden of [historyId, "historical_asset_01", directory, "historicalSegmentIds",
          ...processed.body.revisions.map((revision: { id: string }) => revision.id),
          ...processed.body.artifacts.map((artifact: { id: string }) => artifact.id)]) {
          expect(JSON.stringify(response.body)).not.toContain(forbidden);
        }
        expect([400, 403]).toContain(response.status);
        const restarted = createReviewServer(creativeDependencies({ store: await JsonContentStore.open(sharedPath) }));
        const afterRestart = await request(restarted).get(detailPath);
        expect([400, 403]).toContain(afterRestart.status);
        expect(JSON.stringify(afterRestart.body)).not.toContain(historyId);
      }
      const durable = await JsonContentStore.open(sharedPath);
      expect((await durable.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
        .toMatchObject({ status: "reviewable", publishedClaimed: false });
      expect(await durable.listStories()).toEqual(await library.listStories());
    }, 300_000);

  // Catches raw cached disclosure and restart replay that incorrectly restores withdrawn viewing authority.
  it.each([false, true].flatMap((legacy) => (legacy ? [true] : [false, true]).flatMap((restart) =>
    ["accept-replay", "selection-replay", "accept-receipt", "selection-receipt"].map((boundary) => ({ legacy, restart, boundary })))))(
    "rechecks withdrawn consent for $boundary with restart=$restart legacy=$legacy", async ({ legacy, restart, boundary }) => {
      const app = createApp();
      const { sessionId, suggestionSetId } = await postShootFixture(app);
      const client = await defaultCreativeClient(app);
      const acceptPath = `/api/creative-sessions/${sessionId}/post-shoot-material-viewing`;
      const selectionPath = `/api/creative-sessions/${sessionId}/post-shoot-material-selection`;
      const acceptBody = { suggestionSetId, decision: "accept" };
      const selectionBody = { suggestionSetId, selectedSegmentIds: [SEGMENT_ID] };
      const acceptKey = "r2-fix1-accept-original-0001";
      const selectionKey = "r2-fix1-select-original-0001";
      const accepted = await postAsClient(app, client, acceptPath, acceptBody, acceptKey).expect(201);
      const selected = await postAsClient(app, client, selectionPath, selectionBody, selectionKey).expect(201);
      expect((await postAsClient(app, client, acceptPath, acceptBody, acceptKey).expect(201)).body).toEqual(accepted.body);
      expect((await postAsClient(app, client, selectionPath, selectionBody, selectionKey).expect(201)).body).toEqual(selected.body);
      const receipt = await readReceipt(app, client, selectionKey).expect(200);
      expect(receipt.body.receipt.request.body).toEqual(selectionBody);
      expect(receipt.body.receipt.response.body).toEqual(selected.body);
      await postAsClient(app, client, acceptPath, { suggestionSetId, decision: "decline" }, "r2-fix1-decline-0001").expect(201);
      if (legacy) {
        // Upgrade fixture: pre-fix decisions persisted random IDs, with no operation-key binding.
        const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
        const session = snapshot.creativeSessions.find((item: { id: string }) => item.id === sessionId);
        for (const decision of snapshot.materialUseDecisions.filter((item: { suggestionSetId: string }) => item.suggestionSetId === suggestionSetId)) {
          const priorId = decision.id;
          decision.id = `material_use_decision_legacy_${decision.action}`;
          session.materialViewDecisionIds = session.materialViewDecisionIds.map((id: string) => id === priorId ? decision.id : id);
          session.materialUseDecisionIds = session.materialUseDecisionIds.map((id: string) => id === priorId ? decision.id : id);
        }
        await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      }
      const beforeReplay = await JsonContentStore.open(join(directory, "library.json"));
      const decisions = await beforeReplay.listMaterialUseDecisions();
      const audits = await beforeReplay.listAuditEvents(sessionId);
      const target = restart ? createReviewServer(creativeDependencies({
        store: await JsonContentStore.open(join(directory, "library.json")),
      })) : app;
      const targetClient = restart ? await defaultCreativeClient(target) : client;
      const key = boundary.startsWith("accept") ? acceptKey : selectionKey;
      const response = boundary.endsWith("receipt") ? await readReceipt(target, targetClient, key)
        : await postAsClient(target, targetClient, boundary.startsWith("accept") ? acceptPath : selectionPath,
          boundary.startsWith("accept") ? acceptBody : selectionBody, key);
      expect([400, 403, 404]).toContain(response.status);
      for (const forbidden of [SEGMENT_ID, "shownSegmentIds", "selectedSegmentIds", "rejectedSegmentIds", "thumbnailPath", directory]) {
        expect(JSON.stringify(response.body)).not.toContain(forbidden);
      }
      const detail = await request(target).get(`/api/creative-sessions/${sessionId}`).expect(200);
      expect(detail.body.session.postShootMaterialDecision.action).toBe("decline-view");
      expect(detail.body.session.postShootSuggestionSet).toBeNull();
      await authenticatedPreview(target, `/api/creative-sessions/${sessionId}/material-suggestions/${SEGMENT_ID}/preview`).expect(400);
      const reopened = await JsonContentStore.open(join(directory, "library.json"));
      expect(await reopened.listMaterialUseDecisions()).toEqual(decisions);
      expect(await reopened.listAuditEvents(sessionId)).toEqual(audits);
      if (legacy) {
        const proposed = await postAsClient(target, targetClient, `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`,
          {}, "r2-legacy-new-proposal-0001").expect(201);
        const nextSetId = proposed.body.materialSuggestion.id;
        expect(nextSetId).not.toBe(suggestionSetId);
        await postAsClient(target, targetClient, acceptPath, { suggestionSetId: nextSetId, decision: "accept" },
          "r2-legacy-fresh-accept-0001").expect(201);
        const confirmed = await postAsClient(target, targetClient, selectionPath,
          { suggestionSetId: nextSetId, selectedSegmentIds: [SEGMENT_ID] }, "r2-legacy-fresh-selection-0001").expect(201);
        expect(confirmed.body.decision.selectedSegmentIds).toEqual([SEGMENT_ID]);
      }
    }, 300_000);

  // Catches recomputing max from surviving rows instead of requiring the independently committed current head.
  it.each(["latest-revision", "current-head"] as const)(
    "never falls back when the mixed-current set loses its %s", async (missing) => {
      const { sessionId, processed, revised } = await mixedCurrentFixture();
      const snapshot = JSON.parse(await readFile(join(directory, "library.json"), "utf8"));
      if (missing === "latest-revision") snapshot.revisions = snapshot.revisions.filter((item: { id: string }) => item.id !== revised.body.revision.id);
      else delete snapshot.creativeRevisionHeads;
      await writeFile(join(directory, "library.json"), JSON.stringify(snapshot));
      await expectCurrentOutputFailure(sessionId, processed, revised, missing === "latest-revision" ? 1 : 2);
      const restarted = createReviewServer(creativeDependencies({ store: await JsonContentStore.open(join(directory, "library.json")) }));
      const processOperationKey = "r2-head-recovery-process-0001";
      const reset = await post(restarted, `/api/creative-sessions/${sessionId}/editorial-manual-reset`, {
        failedTaskId: processed.body.processingTask.id, confirmedBy: "designer_01",
        confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART", confirmationText: "人工核对完成，确认重新开始剪辑",
        processOperationKey,
      }).expect(201);
      const recovered = await postAsClient(restarted, await defaultCreativeClient(restarted),
        `/api/creative-sessions/${sessionId}/editorial-process`, { manualRestartAuthorizationId: reset.body.authorization.id },
        processOperationKey).expect(201);
      expect(recovered.body.revisions.map((item: { revision: number }) => item.revision).sort()).toEqual([2, 3]);
      expect(recovered.body.processingTask).toMatchObject({ status: "reviewable", publishedClaimed: false });
      expect(recovered.body.artifacts).toHaveLength(8);
      for (const artifact of recovered.body.artifacts) await authenticatedPreview(restarted, artifact.mediaUrl).expect(200);
    }, 300_000);

  async function expectCurrentOutputFailure(sessionId: string, processed: any, revised: any, revisionCount = 2) {
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    const restarted = createReviewServer(creativeDependencies({ store: reopened }));
    const detail = await request(restarted).get(`/api/creative-sessions/${sessionId}`).expect(200);
    expect(detail.body.session.processingTask).toMatchObject({ id: processed.body.processingTask.id,
      status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false });
    expect(detail.body.session.revisions).toEqual([]);
    expect(detail.body.session.artifacts).toEqual([]);
    const markup = await renderCreativeDetail(detail.body.session);
    expect(markup).toContain("data-reset-manual-editorial");
    expect(markup).not.toContain("<video");
    expect(markup).not.toContain("data-approve");
    for (const artifact of [...processed.body.artifacts, ...revised.body.artifacts]) {
      const response = await authenticatedPreview(restarted, artifact.mediaUrl).expect(404);
      expect(JSON.stringify(response.body)).not.toContain(directory);
    }
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect((await persisted.listCreativeProcessingTasks()).find((task) => task.id === processed.body.processingTask.id))
      .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-current-output-invalid" });
    expect((await persisted.listCreativeProcessingTasks()).filter((task) => task.operationKey)).toHaveLength(1);
    expect(await persisted.listRevisions(revised.body.revision.storyId)).toHaveLength(revisionCount);
  }

  async function postShootFixture(app: ReturnType<typeof createReviewServer>) {
    const { sessionId, stepId } = await sessionReadyForShooting(app);
    await post(app, `/api/creative-sessions/${sessionId}/events`, fieldEventBody(stepId, {
      action: "completed", operationId: "final-wave-completed-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director",
    })).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId, deviceId: "watch_sim_01", deviceRole: "watch-director",
      carrier: "watch", take: 1, stepOutcome: "completed",
    }).expect(201);
    await post(app, `/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }).expect(201);
    const proposed = await post(app, `/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {}).expect(201);
    return { sessionId, suggestionSetId: proposed.body.materialSuggestion.id as string };
  }

  function createApp() {
    return createReviewServer(dependencies());
  }

  function dependencies(): ReviewAppDependencies {
    return creativeDependencies();
  }

  function creativeDependencies(options: {
    onCreateRoster?: () => void;
    store?: JsonContentStore;
    intentInterpreter?: IntentInterpreter;
    materialMatcher?: MaterialMatcher;
    privacyDevices?: ReturnType<typeof testPrivacyDevice>[];
    renderer?: ReviewAppDependencies["renderer"];
    storyAnalyzer?: StoryAnalyzer;
    editAnalyzer?: EditPlanAnalyzer;
    segmentAnalyzer?: ReturnType<typeof editorialAnalyzers>["segmentAnalyzer"];
  } = {}): ReviewAppDependencies {
    const capabilities: DeviceCapability[] = [
      { deviceId: "watch_sim_01", deviceRole: "watch-director", carrier: "watch" },
      { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", carrier: "ring" },
      { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", carrier: "pendant" },
    ];
    const mailboxes = {
      "watch-director": new LocalDispatchMailbox("watch_sim_01"),
      "main-stone-a": new LocalDispatchMailbox("stone_a_sim_01"),
      "main-stone-b": new LocalDispatchMailbox("stone_b_sim_01"),
    };
    return {
      library: options.store ?? library,
      interpreter: { interpret: async () => ({
        targetVariants: ["wechat-channels" as const],
        replacementTimelines: {},
        removePeople: [],
        explanation: "Applied the scoped designer prompt.",
      }) },
      outputDir: directory,
      designerId: "designer_01",
      reviewToken: TOKEN,
      customerCredentials: { customer_01: CUSTOMER_TOKEN, customer_other: "other-customer-token" },
      renderer: options.renderer,
      creative: {
        intentInterpreter: options.intentInterpreter ?? intentInterpreter(),
        materialMatcher: options.materialMatcher ?? materialMatcher(),
        scriptAgent: scriptAgent(),
        shotPlanningAgent: shotPlanningAgent(),
        editorial: { ...editorialAnalyzers(), segmentAnalyzer: options.segmentAnalyzer ?? editorialAnalyzers().segmentAnalyzer,
          storyAnalyzer: options.storyAnalyzer ?? editorialAnalyzers().storyAnalyzer,
          editAnalyzer: options.editAnalyzer ?? editorialAnalyzers().editAnalyzer },
        creativeDeviceAdapters: {
          deviceCapabilities: capabilities,
          privacyDevices: options.privacyDevices ?? capabilities.map((value) => testPrivacyDevice(value.deviceId)),
          createRoster: (version) => {
            options.onCreateRoster?.();
            return createLocalDispatchRoster({ mailboxes, version }).roster;
          },
        },
      },
    };
  }
});

// Execute the shipping UI against an HTTP detail response, with only browser DOM/storage supplied here.
async function renderCreativeDetail(view: unknown): Promise<string> {
  const element = (): any => ({ innerHTML: "", textContent: "", dataset: {}, value: "",
    classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, setAttribute() {}, click() {},
    querySelectorAll: () => [], querySelector: () => element(), closest: () => element(), focus() {} });
  const workspace = element();
  const context = createContext({
    document: { querySelector: (selector: string) => selector === "#workspace" ? workspace : element(),
      querySelectorAll: () => [] },
    window: { confirm: () => true }, Intl, Date, encodeURIComponent, setTimeout, clearTimeout,
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    crypto: { randomUUID: () => "dom-test-operation" },
    fetch: async () => ({ ok: true, json: async () => ({}) }), view,
  });
  runInContext(await readFile(join(process.cwd(), "packages/review-app/public/app.js"), "utf8"), context);
  runInContext("renderCreativeSession(view)", context);
  return workspace.innerHTML;
}

async function createSession(app: ReturnType<typeof createReviewServer>) {
  return post(app, "/api/creative-sessions", {
    customerId: "customer_01",
    title: "Turquoise rework story",
    text: "Make a short video about the turquoise ring rework.",
  }).expect(201);
}

async function sessionReadyForScript(app: ReturnType<typeof createReviewServer>) {
  const created = await createSession(app);
  const sessionId = created.body.session.id as string;
  const suggestionSetId = created.body.materialSuggestion.id as string;
  await post(app, `/api/creative-sessions/${sessionId}/material-viewing`, {
    suggestionSetId,
    decision: "accept",
  }).expect(201);
  await post(app, `/api/creative-sessions/${sessionId}/material-selection`, {
    suggestionSetId,
    selectedSegmentIds: [SEGMENT_ID],
  }).expect(201);
  return { sessionId, suggestionSetId };
}

async function sessionReadyForShooting(app: ReturnType<typeof createReviewServer>) {
  const { sessionId } = await sessionReadyForScript(app);
  await post(app, `/api/creative-sessions/${sessionId}/scripts`, {}).expect(201);
  await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
    scriptVersion: 1,
  }).expect(201);
  const shooting = await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
    .expect(201);
  await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
    confirmStart: true,
  }).expect(201);
  return { sessionId, stepId: shooting.body.shotPlan.steps[0].id as string };
}

async function rewriteAndResumeShooting(
  app: ReturnType<typeof createReviewServer>,
  sessionId: string,
  stepId: string,
  scriptVersion: number,
  operationId: string,
): Promise<string> {
  await rewriteAtCurrentShooting(app, sessionId, stepId, scriptVersion, operationId);
  return resumeShootingAtScript(app, sessionId, scriptVersion + 1);
}

async function rewriteAtCurrentShooting(
  app: ReturnType<typeof createReviewServer>,
  sessionId: string,
  stepId: string,
  scriptVersion: number,
  operationId: string,
) {
  return post(app, `/api/creative-sessions/${sessionId}/events`, {
    action: "voice-rewrite",
    transcript: `Advance to script V${scriptVersion + 1}.`,
    operationId,
    stepId,
    briefVersion: 1,
    scriptVersion,
    shotPlanVersion: 1,
  }).expect(201);
}

async function resumeShootingAtScript(
  app: ReturnType<typeof createReviewServer>,
  sessionId: string,
  scriptVersion: number,
): Promise<string> {
  await post(app, `/api/creative-sessions/${sessionId}/script-confirmation`, {
    scriptVersion,
  }).expect(201);
  const shooting = await post(app, `/api/creative-sessions/${sessionId}/shooting-package`, {})
    .expect(201);
  await post(app, `/api/creative-sessions/${sessionId}/dispatch`, {
    confirmStart: true,
  }).expect(201);
  return shooting.body.shotPlan.steps[0].id as string;
}

function fieldEventBody(
  stepId: string,
  overrides: Record<string, unknown>,
): Record<string, unknown> {
  return {
    action: "note",
    operationId: "field-op-01",
    stepId,
    briefVersion: 1,
    scriptVersion: 1,
    shotPlanVersion: 1,
    ...overrides,
  };
}

type Deferred<T> = {
  promise: Promise<T>;
  resolve(value?: T): void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value?: T) => void;
  const promise = new Promise<T>((done) => { resolve = done as (value?: T) => void; });
  return { promise, resolve };
}

function testPrivacyDevice(deviceId: string) {
  let paused = false;
  return {
    deviceId,
    pausePrivacy() { paused = true; },
    resumePrivacy() { paused = false; },
    isPrivacyPaused() { return paused; },
  };
}

function post(
  app: ReturnType<typeof createReviewServer>,
  path: string,
  body: Record<string, unknown>,
  operationKey?: string,
) {
  const resolvedOperationKey = operationKey ?? (
    path.endsWith("/events") && typeof body.operationId === "string"
      ? body.operationId
      : `creative-test-operation-${nextOperationSequence++}`
  );
  return {
    async expect(status: number, responseBody?: unknown) {
      const clientInstanceId = await defaultCreativeClient(app);
      const operation = postAsClient(
        app,
        clientInstanceId,
        path,
        body,
        resolvedOperationKey,
      );
      const response = await operation;
      expect(
        response.status,
        `unexpected response for POST ${path} (${resolvedOperationKey}): ${JSON.stringify(response.body)}`,
      ).toBe(status);
      if (responseBody !== undefined) expect(response.body).toEqual(responseBody);
      return response;
    },
  };
}

function postAsClient(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
  path: string,
  body: Record<string, unknown>,
  operationKey: string,
) {
  return request(app)
    .post(path)
    .set("content-type", "application/json")
    .set("x-review-token", TOKEN)
    .set("x-client-instance", clientInstanceId)
    .set("x-client-claim", creativeClientClaim(app, clientInstanceId))
    .set("x-operation-key", operationKey)
    .send(body);
}

function postAsCustomer(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
  path: string,
  body: Record<string, unknown>,
  operationKey: string,
  customerId: string,
  customerToken: string,
) {
  return request(app)
    .post(path)
    .set("content-type", "application/json")
    .set("x-review-token", TOKEN)
    .set("x-client-instance", clientInstanceId)
    .set("x-client-claim", creativeClientClaim(app, clientInstanceId))
    .set("x-operation-key", operationKey)
    .set("x-customer-id", customerId)
    .set("x-customer-token", customerToken)
    .send(body);
}

async function openCreativeClient(
  app: ReturnType<typeof createReviewServer>,
): Promise<string> {
  const session = await request(app).get("/api/session").expect(200);
  expect(session.body.clientInstanceId).toEqual(expect.any(String));
  expect(session.body.clientClaim).toEqual(expect.any(String));
  rememberCreativeClientClaim(
    app,
    session.body.clientInstanceId as string,
    session.body.clientClaim as string,
  );
  return session.body.clientInstanceId as string;
}

function readReceipt(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
  operationKey: string,
) {
  return request(app)
    .get(`/api/creative-operation-receipts/${operationKey}`)
    .set("x-review-token", TOKEN)
    .set("x-client-instance", clientInstanceId)
    .set("x-client-claim", creativeClientClaim(app, clientInstanceId));
}

function acknowledgeReceipt(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
  operationKey: string,
) {
  return request(app)
    .delete(`/api/creative-operation-receipts/${operationKey}`)
    .set("content-type", "application/json")
    .set("x-review-token", TOKEN)
    .set("x-client-instance", clientInstanceId)
    .set("x-client-claim", creativeClientClaim(app, clientInstanceId))
    .send({});
}

let nextOperationSequence = 1;
const defaultCreativeClients = new WeakMap<object, Promise<string>>();
const creativeClientClaims = new WeakMap<object, Map<string, string>>();

function rememberCreativeClientClaim(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
  clientClaim: string,
): void {
  const claims = creativeClientClaims.get(app) ?? new Map<string, string>();
  claims.set(clientInstanceId, clientClaim);
  creativeClientClaims.set(app, claims);
}

function creativeClientClaim(
  app: ReturnType<typeof createReviewServer>,
  clientInstanceId: string,
): string {
  const claim = creativeClientClaims.get(app)?.get(clientInstanceId);
  if (!claim) throw new Error(`test-client-claim-missing:${clientInstanceId}`);
  return claim;
}

function defaultCreativeClient(
  app: ReturnType<typeof createReviewServer>,
): Promise<string> {
  const existing = defaultCreativeClients.get(app);
  if (existing) return existing;
  const created = openCreativeClient(app);
  defaultCreativeClients.set(app, created);
  return created;
}

function authenticatedPreview(
  app: ReturnType<typeof createReviewServer>,
  path: string,
) {
  return request(app)
    .get(path)
    .set("host", "127.0.0.1")
    .set("referer", "http://127.0.0.1/")
    .set("x-review-token", TOKEN);
}

function intentInterpreter(): IntentInterpreter {
  return {
    interpret: async () => ({
      platforms: ["wechat-channels"],
      audience: "Jewelry collectors",
      theme: "Turquoise ring rework",
      products: [{ id: "product_01", status: "suggested" }],
      shootConditions: ["Synthetic workbench available"],
      prohibitedAreas: ["customer faces"],
      tone: "direct",
      durationSeconds: 30,
    }),
  };
}

function materialMatcher(): MaterialMatcher {
  return {
    rank: async ({ segments }) => segments.map((segment, index) => ({
      segmentId: segment.id,
      reason: "same confirmed product",
      score: 100 - index,
    })),
  };
}

function scriptAgent(): CreativeScriptAgent {
  const draft = (opening: string, selectedIds: string[], changeExplanation?: string) => ({
    title: "Turquoise ring rework",
    platformDirection: "wechat-channels",
    opening,
    narrativeOrder: ["customer", ...(selectedIds.length > 0 ? ["history"] : [])],
    sections: [
      {
        id: "customer",
        expression: opening,
        source: "customer-input" as const,
        referencedSegmentIds: [],
      },
      ...selectedIds.map((segmentId) => ({
        id: "history",
        expression: "Show the prior rework step.",
        source: "historical-material" as const,
        referencedSegmentIds: [segmentId],
      })),
    ],
    changeExplanation,
  });
  return {
    draft: async ({ selectedHistoricalSegments }) =>
      draft("Three reworks made this ring clearer.", selectedHistoricalSegments.map((item) => item.id)),
    revise: async ({ prompt, selectedHistoricalSegments }) =>
      draft(prompt, selectedHistoricalSegments.map((item) => item.id), "Applied the customer's rewrite."),
  };
}

function shotPlanningAgent(): ShotPlanningAgent {
  return {
    plan: async () => ({
      steps: [{
        purpose: "Tell the rework story",
        expressionSuggestion: "Explain why the ring returned to the bench.",
        visualDirection: "Show a synthetic ring close-up.",
        durationSeconds: 20,
        mustCapture: ["ring"],
        optionalGapSuggestions: [{ suggestion: "Add a close-up if convenient.", required: false }],
        completionCriteria: ["One usable explanation"],
        deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
        privacyReminder: "Do not capture customer faces.",
      }],
    }),
  };
}

function historicalAsset(directory: string): LibraryAsset {
  return {
    id: "historical_asset_01",
    batchId: "historical_batch_01",
    sessionId: "historical_session_01",
    digest: "synthetic-digest",
    localPath: join(directory, "historical.mp4"),
    mediaKind: "video",
    importedAt: "2026-09-04T00:00:00.000Z",
    permissionState: "allowed",
    origin: "historical",
    technicalFlags: [],
  };
}

function historicalSegment(): ContentSegment {
  return {
    id: SEGMENT_ID,
    assetId: "historical_asset_01",
    batchId: "historical_batch_01",
    startMs: 0,
    endMs: 1_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["handmade"],
      people: [],
      products: [{ id: "product_01", status: "confirmed" }],
      processStages: ["rework"],
      topics: ["craft"],
      emotions: [],
      transcript: "Synthetic prior rework.",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}

function editorialAnalyzers(): {
  segmentAnalyzer: { analyze(): Promise<Array<{ startMs: number; endMs: number; transcript: string; signals: [] }>> };
  tagAnalyzer: SegmentTagAnalyzer;
  storyAnalyzer: StoryAnalyzer;
  editAnalyzer: EditPlanAnalyzer;
} {
  const segmentAnalyzer = { analyze: async () => [{
    startMs: 0, endMs: 1_200,
    transcript: "我觉得手工应该留下痕迹，我决定坚持再做一次。",
    signals: [] as [],
  }] };
  const tagAnalyzer: SegmentTagAnalyzer = { suggest: async (segment) => ({
    baseTypes: ["persona-copy", "handmade"], people: [], products: [],
    processStages: ["rework"], topics: ["craft"], emotions: ["hesitation", "persistence"],
    transcript: segment.transcript, technicalFlags: [],
    usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
  }) };
  const storyAnalyzer: StoryAnalyzer = { propose: async (segments) => [
    { type: "persona-monologue", anchorSegmentIds: segments.map((item) => item.id) },
    { type: "inner-journey", anchorSegmentIds: segments.map((item) => item.id) },
    { type: "product-journey", anchorSegmentIds: segments.map((item) => item.id) },
  ] };
  const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
    const timeline = (outMs: number) => story.anchorSegmentIds.map((segmentId) => ({
      segmentId, inMs: 0, outMs, role: "anchor" as const,
    }));
    const cover = { segmentId: story.anchorSegmentIds[0]!, atMs: 100 };
    return {
      master: { timeline: timeline(400), title: "A-0 原视频", caption: "本地模拟", cover },
      xiaohongshu: { timeline: timeline(600), title: "A-0 小红书", caption: "本地模拟", cover },
      douyin: { timeline: timeline(700), title: "A-0 抖音", caption: "本地模拟", cover },
      wechatChannels: { timeline: timeline(800), title: "A-0 视频号", caption: "本地模拟", cover },
      stills: [],
    };
  } };
  return { segmentAnalyzer, tagAnalyzer, storyAnalyzer, editAnalyzer };
}

async function persistedEditorialCounts(library: JsonContentStore) {
  return {
    tasks: (await library.listCreativeProcessingTasks()).length,
    stories: (await library.listStories()).length,
    revisions: (await Promise.all((await library.listStories()).map(
      (story) => library.listRevisions(story.id),
    ))).flat().length,
    artifacts: (await library.listAllArtifacts()).length,
  };
}

async function probeHttpVideo(path: string): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const binary = ffmpegPath;
  await new Promise<void>((resolve, reject) => {
    const child = spawn(binary, ["-v", "error", "-xerror", "-i", path,
      "-map", "0:v:0", "-f", "null", "-"], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (data) => { stderr += String(data); });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(stderr)));
  });
}

function expectCreativeDtoHasNoHostPaths(value: unknown): void {
  const encoded = JSON.stringify(value);
  for (const forbidden of ["localPath", "videoPath", "coverPath", "stillPaths", "thumbnailPath"]) {
    expect(encoded).not.toContain(`\"${forbidden}\"`);
  }
  expect(encoded).not.toContain("/Users/");
  expect(encoded).not.toContain("/var/folders/");
}
