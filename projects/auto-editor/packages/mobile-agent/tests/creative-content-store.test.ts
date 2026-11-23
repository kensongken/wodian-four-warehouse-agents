import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  AuditEvent,
  ContentSegment,
  CreativeIntent,
  CreativeSession,
  CreativeSessionEvent,
  MaterialUseDecision,
  ScriptPackage,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JsonContentStore } from "../src/json-content-store";

describe("creative content store", () => {
  let testDirectory: string;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-creative-content-store-"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("reopens an old ten-array library snapshot with empty creative collections", async () => {
    const path = join(testDirectory, "library.json");
    await writeFile(path, `${JSON.stringify(oldSnapshot())}\n`);

    const store = await JsonContentStore.open(path);

    await expect(store.listCreativeSessions()).resolves.toEqual([]);
    await expect(store.listCreativeIntents()).resolves.toEqual([]);
  });

  it("persists capture settlement and its processing task in one capture bundle", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "capture-bundle.json"));
    const session: CreativeSession = { ...creativeSession(), stage: "shooting",
      currentBriefVersion: 1, currentScriptVersion: 1, currentShotPlanVersion: 1 };
    await store.putCreativeSession(session);
    await store.putShotPlan({ id: "capture_plan_01", sessionId: session.id, briefVersion: 1,
      scriptVersion: 1, shotPlanVersion: 1, status: "active", createdAt: session.createdAt,
      steps: [{ id: "step_01", order: 1, purpose: "Capture", expressionSuggestion: "Capture",
        visualDirection: "Close", durationSeconds: 1, mustCapture: ["asset"], optionalGapSuggestions: [],
        prohibitedCapture: [], completionCriteria: ["usable"], deviceRoles: ["watch-director"] }] });

    await store.commitCreativeCaptureBundle({
      session,
      expectedSession: {
        id: session.id, stage: session.stage, currentBriefVersion: session.currentBriefVersion,
        currentScriptVersion: session.currentScriptVersion, currentShotPlanVersion: session.currentShotPlanVersion,
      },
      assets: [], events: [], auditEvents: [],
      processingTask: {
        id: "editorial_task_creative_01", creativeSessionId: session.id,
        editorialBatchId: "creative_batch_creative_01", status: "collecting", phase: "capture",
        errorCode: null, updatedAt: "2026-09-07T08:00:00.000Z",
      },
    });

    await expect(store.listCreativeProcessingTasks()).resolves.toEqual([
      expect.objectContaining({ creativeSessionId: session.id, status: "collecting" }),
    ]);
  });

  it("normalizes legacy string gap suggestions on reopen and persists objects on the next write", async () => {
    const path = join(testDirectory, "legacy-shot-plan.json");
    await writeFile(path, `${JSON.stringify(legacyShotPlanSnapshot())}\n`);

    const store = await JsonContentStore.open(path);

    await expect(store.listShotPlans()).resolves.toEqual([expect.objectContaining({
      steps: [expect.objectContaining({
        optionalGapSuggestions: [{ suggestion: "Try a close-up if time allows", required: false }],
      })],
    })]);
    await store.putAuditEvent({
      id: "audit_legacy-normalized",
      entityType: "shot-plan",
      entityId: "shot_plan_legacy",
      actorId: "system_01",
      action: "legacy-normalized",
      reason: null,
      before: null,
      after: null,
      createdAt: "2026-09-05T08:01:00.000Z",
    });

    const persisted = JSON.parse(await readFile(path, "utf8"));
    expect(persisted.shotPlans[0].steps[0].optionalGapSuggestions).toEqual([
      { suggestion: "Try a close-up if time allows", required: false },
    ]);
  });

  it("rejects changed content when an immutable creative ID is reused", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const intent = creativeIntent();
    await store.putCreativeIntent(intent);

    await expect(store.putCreativeIntent({ ...intent, theme: "A different theme" }))
      .rejects.toThrow("immutable-creative-object-conflict");
  });

  it("enforces creative event operation identity in the low-level store put", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const event: CreativeSessionEvent = {
      id: "creative_event_operation_01",
      sessionId: "creative_01",
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      stepId: "step_01",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
      action: "note",
      actorId: "designer_01",
      reason: "Keep this light.",
      transcript: null,
      idempotencyKey: "creative_01:step_01:operation_01",
      take: 1,
      createdAt: "2026-09-05T08:00:00.000Z",
    };

    await store.putCreativeSessionEvent(event);
    await store.putCreativeSessionEvent(event);
    await expect(store.putCreativeSessionEvent({
      ...event,
      id: "creative_event_operation_01_changed",
      deviceId: "stone_a_sim_01",
      deviceRole: "main-stone-a",
    })).rejects.toThrow("creative-event-operation-conflict");
    expect(await store.listCreativeSessionEvents()).toEqual([event]);
  });

  it("rejects an action bundle whose stored result version does not match its event", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session: CreativeSession = {
      ...creativeSession(),
      stage: "shooting",
      currentBriefVersion: 1,
      currentScriptVersion: 3,
      currentShotPlanVersion: 1,
    };
    const event: CreativeSessionEvent = {
      id: "creative_event_result_mismatch",
      sessionId: session.id,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      stepId: "step_01",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
      action: "note",
      actorId: "designer_01",
      reason: "Original V1 note.",
      transcript: null,
      idempotencyKey: "creative-field-event:v1|11:creative_01|7:step_01|15:mismatched-note",
      take: 1,
      createdAt: "2026-09-05T08:00:00.000Z",
    };

    await expect(store.commitCreativeBundle({
      session,
      events: [event],
      returnActionResult: true,
      auditEvents: [],
    })).rejects.toThrow("creative-action-result-version-mismatch");
    await expect(store.listCreativeSessionEvents()).resolves.toEqual([]);
  });

  it("requires a voice action bundle to include its resulting script revision", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session: CreativeSession = {
      ...creativeSession(),
      stage: "awaiting_script_confirmation",
      currentBriefVersion: 1,
      currentScriptVersion: 2,
      currentShotPlanVersion: 0,
    };
    const event: CreativeSessionEvent = {
      id: "creative_event_voice_without_script",
      sessionId: session.id,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      stepId: "step_01",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
      action: "voice-rewrite",
      actorId: "designer_01",
      reason: null,
      transcript: "Use a quieter opening.",
      idempotencyKey: "creative-field-event:v1|11:creative_01|7:step_01|20:voice-without-script",
      take: 1,
      createdAt: "2026-09-05T08:00:00.000Z",
    };

    await expect(store.commitCreativeBundle({
      session,
      events: [event],
      returnActionResult: true,
      auditEvents: [],
    })).rejects.toThrow("creative-action-result-script-required");
    await expect(store.listCreativeSessionEvents()).resolves.toEqual([]);
  });

  it("does not allow the low-level session put to bypass audited stage changes", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    await store.putCreativeSession(session);

    await expect(store.putCreativeSession({
      ...session,
      stage: "shooting",
      updatedAt: "2026-09-04T08:01:00.000Z",
    })).rejects.toThrow("immutable-creative-object-conflict");
    await expect(store.listCreativeSessions()).resolves.toEqual([session]);
  });

  it("requires a matching transition audit before a bundle changes session stage", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = { ...creativeSession(), stage: "ready_to_dispatch" as const };
    await store.putCreativeSession(session);

    await expect(store.commitCreativeBundle({
      session: { ...session, stage: "shooting", updatedAt: "2026-09-04T08:01:00.000Z" },
      auditEvents: [],
    })).rejects.toThrow("creative-session-audit-required");
  });

  it("rejects an illegal stage change even with a forged matching audit", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    await store.putCreativeSession(session);

    await expect(store.commitCreativeBundle({
      session: { ...session, stage: "shooting", updatedAt: "2026-09-04T08:01:00.000Z" },
      auditEvents: [transitionAudit(session.id, "draft_intent", "shooting")],
    })).rejects.toThrow("creative-session-transition-not-allowed");
  });

  it("requires an audit for a same-stage session state update", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    await store.putCreativeSession(session);

    await expect(store.commitCreativeBundle({
      session: {
        ...session,
        currentIntentVersion: 1,
        updatedAt: "2026-09-04T08:01:00.000Z",
      },
      auditEvents: [],
    })).rejects.toThrow("creative-session-audit-required");
  });

  it("accepts a legal audited session stage update", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = { ...creativeSession(), stage: "ready_to_dispatch" as const };
    await store.putCreativeSession(session);

    const shooting = { ...session, stage: "shooting" as const, updatedAt: "2026-09-04T08:01:00.000Z" };
    await store.commitCreativeBundle({
      session: shooting,
      auditEvents: [transitionAudit(session.id, "ready_to_dispatch", "shooting")],
    });

    await expect(store.listCreativeSessions()).resolves.toEqual([shooting]);
  });

  it("rejects a replayed transition audit and preserves the ready session", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const ready = { ...creativeSession(), stage: "ready_to_dispatch" as const };
    const shootingAudit = transitionAudit(ready.id, "ready_to_dispatch", "shooting");
    await store.putCreativeSession(ready);
    const shooting = { ...ready, stage: "shooting" as const, updatedAt: "2026-09-04T08:01:00.000Z" };
    await store.commitCreativeBundle({ session: shooting, auditEvents: [shootingAudit] });
    const paused = { ...shooting, stage: "paused" as const, updatedAt: "2026-09-04T08:02:00.000Z" };
    await store.commitCreativeBundle({
      session: paused,
      auditEvents: [transitionAudit(ready.id, "shooting", "paused")],
    });
    const readyAgain = { ...paused, stage: "ready_to_dispatch" as const, updatedAt: "2026-09-04T08:03:00.000Z" };
    await store.commitCreativeBundle({
      session: readyAgain,
      auditEvents: [transitionAudit(ready.id, "paused", "ready_to_dispatch")],
    });

    await expect(store.commitCreativeBundle({
      session: { ...readyAgain, stage: "shooting", updatedAt: "2026-09-04T08:04:00.000Z" },
      auditEvents: [shootingAudit],
    })).rejects.toThrow("creative-session-audit-required");
    await expect(store.listCreativeSessions()).resolves.toEqual([readyAgain]);
    await expect(store.listAuditEvents(ready.id)).resolves.toHaveLength(3);
  });

  it("rejects a replayed same-stage audit and preserves the prior version pointer", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    const firstAudit = sameStageAudit(session.id);
    await store.putCreativeSession(session);
    const firstUpdate = { ...session, currentIntentVersion: 1, updatedAt: "2026-09-04T08:01:00.000Z" };
    await store.commitCreativeBundle({ session: firstUpdate, auditEvents: [firstAudit] });

    await expect(store.commitCreativeBundle({
      session: { ...firstUpdate, currentIntentVersion: 2, updatedAt: "2026-09-04T08:02:00.000Z" },
      auditEvents: [firstAudit],
    })).rejects.toThrow("creative-session-audit-required");
    await expect(store.listCreativeSessions()).resolves.toEqual([firstUpdate]);
    await expect(store.listAuditEvents(session.id)).resolves.toHaveLength(1);
  });

  it("rejects a creative bundle that would leave two active shooting sessions", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const first = { ...creativeSession(), stage: "ready_to_dispatch" as const };
    const second = { ...first, id: "creative_02", title: "Second story" };
    await store.putCreativeSession(first);
    await store.putCreativeSession(second);
    await store.commitCreativeBundle({
      session: { ...first, stage: "shooting", updatedAt: "2026-09-04T08:01:00.000Z" },
      auditEvents: [transitionAudit(first.id, "ready_to_dispatch", "shooting")],
    });

    await expect(store.commitCreativeBundle({
      session: { ...second, stage: "shooting", updatedAt: "2026-09-04T08:01:00.000Z" },
      auditEvents: [transitionAudit(second.id, "ready_to_dispatch", "shooting")],
    })).rejects.toThrow("active-shooting-session-exists");
  });

  it("commits a session, confirmed script, and audit event into one persisted snapshot", async () => {
    const path = join(testDirectory, "library.json");
    const store = await JsonContentStore.open(path);
    const session = creativeSession();
    const script = scriptPackage(session.id);
    const auditEvent = audit(session.id);

    await store.commitCreativeBundle({ session, scripts: [script], auditEvents: [auditEvent] });

    const reopened = await JsonContentStore.open(path);
    await expect(reopened.listCreativeSessions()).resolves.toEqual([session]);
    await expect(reopened.listScriptPackages()).resolves.toEqual([script]);
    await expect(reopened.listAuditEvents(session.id)).resolves.toEqual([auditEvent]);
  });

  it("restores the pre-commit snapshot when persistence fails", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const original = creativeSession();
    await store.putCreativeSession(original);
    const persistence = store as unknown as { persist: () => Promise<void> };
    vi.spyOn(persistence, "persist").mockRejectedValueOnce(new Error("simulated-persist-failure"));

    await expect(store.commitCreativeBundle({
      session: original,
      auditEvents: [audit(original.id)],
    })).rejects.toThrow("simulated-persist-failure");

    await expect(store.listCreativeSessions()).resolves.toEqual([original]);
  });

  it("serializes full creative transactions so a failed first operation cannot roll back the second", async () => {
    const path = join(testDirectory, "library.json");
    const store = await JsonContentStore.open(path);
    const original = creativeSession();
    await store.putCreativeSession(original);
    const persistence = store as unknown as { persist: () => Promise<void> };
    const realPersist = persistence.persist.bind(store);
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist")
      .mockImplementationOnce(async () => {
        persistEntered.resolve();
        await releasePersist.promise;
        throw new Error("first-transaction-failed");
      })
      .mockImplementation(() => realPersist());
    const firstSession = {
      ...original,
      currentIntentVersion: 1,
      updatedAt: "2026-09-04T08:01:00.000Z",
    };
    const secondSession = {
      ...original,
      currentIntentVersion: 2,
      updatedAt: "2026-09-04T08:02:00.000Z",
    };

    const first = store.commitCreativeBundle({
      session: firstSession,
      expectedSession: creativeSessionPrecondition(original),
      auditEvents: [sameStageAuditWithId(original.id, "audit_first")],
    });
    const firstFailure = first.catch((error: unknown) => error);
    await persistEntered.promise;
    const second = store.commitCreativeBundle({
      session: secondSession,
      expectedSession: creativeSessionPrecondition(original),
      auditEvents: [sameStageAuditWithId(original.id, "audit_second")],
    });
    releasePersist.resolve();

    await expect(firstFailure).resolves.toEqual(expect.objectContaining({
      message: "first-transaction-failed",
    }));
    await expect(second).resolves.toBeUndefined();
    const memorySession = (await store.listCreativeSessions())[0];
    const diskSession = (await (await JsonContentStore.open(path)).listCreativeSessions())[0];
    expect(memorySession).toEqual(secondSession);
    expect(diskSession).toEqual(secondSession);
    expect((await store.listAuditEvents(original.id)).map((item) => item.id))
      .toEqual(["audit_second"]);
  });

  it("rejects two different script IDs with the same session, brief, and script version", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    const first = { ...scriptPackage(session.id), id: "script_duplicate_a" };
    const second = { ...scriptPackage(session.id), id: "script_duplicate_b" };

    await expect(store.commitCreativeBundle({
      session,
      scripts: [first, second],
      auditEvents: [],
    })).rejects.toThrow("duplicate-script-version");
    expect(await store.listScriptPackages()).toEqual([]);
  });

  it("rejects a script version key already persisted under another ID", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    const first = { ...scriptPackage(session.id), id: "script_persisted_a" };
    const second = { ...scriptPackage(session.id), id: "script_persisted_b" };
    await store.commitCreativeBundle({ session, scripts: [first], auditEvents: [] });

    await expect(store.commitCreativeBundle({
      session,
      scripts: [second],
      auditEvents: [],
    })).rejects.toThrow("duplicate-script-version");
    expect(await store.listScriptPackages()).toEqual([first]);
  });

  it("enforces script business-key uniqueness in low-level script puts", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const first = { ...scriptPackage("creative_01"), id: "script_low_level_a" };
    const second = { ...first, id: "script_low_level_b" };
    await store.putScriptPackage(first);

    await expect(store.putScriptPackage(second)).rejects.toThrow("duplicate-script-version");
    expect(await store.listScriptPackages()).toEqual([first]);
  });

  it("waits for a failing mutation and returns only its rolled-back snapshot", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const original = creativeSession();
    await store.putCreativeSession(original);
    const persistence = store as unknown as { persist: () => Promise<void> };
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist").mockImplementationOnce(async () => {
      persistEntered.resolve();
      await releasePersist.promise;
      throw new Error("read-boundary-persist-failed");
    });
    const updated = {
      ...original,
      currentIntentVersion: 1,
      updatedAt: "2026-09-04T08:01:00.000Z",
    };
    const mutation = store.commitCreativeBundle({
      session: updated,
      auditEvents: [sameStageAudit(original.id)],
    });
    const mutationFailure = mutation.catch((error: unknown) => error);
    await persistEntered.promise;
    let readSettled = false;
    const read = store.listCreativeSessions().then((value) => {
      readSettled = true;
      return value;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(readSettled).toBe(false);

    releasePersist.resolve();
    await expect(mutationFailure).resolves.toEqual(expect.objectContaining({
      message: "read-boundary-persist-failed",
    }));
    await expect(read).resolves.toEqual([original]);
  });

  it("rejects cross-session task-lineage invalidation", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = {
      ...creativeSession(),
      stage: "ready_to_dispatch" as const,
      currentBriefVersion: 1,
      currentScriptVersion: 1,
    };
    const script = { ...scriptPackage(session.id), id: "script_authorized" };
    await store.putCreativeSession(session);
    await store.putScriptPackage(script);

    await expect(store.commitCreativeBundle({
      session,
      expectedSession: creativeSessionPrecondition(session),
      expectedScript: scriptPrecondition(script),
      invalidateTaskLineage: [{
        sessionId: "creative_other",
        briefVersion: script.briefVersion,
        scriptVersion: script.scriptVersion,
      }],
      auditEvents: [],
    })).rejects.toThrow("task-lineage-invalidation-not-authorized");
  });

  it("rejects task-lineage invalidation authorized by an unconfirmed script", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = {
      ...creativeSession(),
      stage: "awaiting_script_confirmation" as const,
      currentBriefVersion: 1,
      currentScriptVersion: 1,
    };
    const script = {
      ...scriptPackage(session.id),
      id: "script_unconfirmed_authority",
      confirmedAt: null,
    };
    await store.putCreativeSession(session);
    await store.putScriptPackage(script);

    await expect(store.commitCreativeBundle({
      session,
      expectedSession: creativeSessionPrecondition(session),
      expectedScript: scriptPrecondition(script),
      invalidateTaskLineage: [{
        sessionId: session.id,
        briefVersion: script.briefVersion,
        scriptVersion: script.scriptVersion,
      }],
      auditEvents: [],
    })).rejects.toThrow("task-lineage-invalidation-not-authorized");
  });

  it("rejects a bundle before mutation when a child belongs to another session", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));

    await expect(store.commitCreativeBundle({
      session: creativeSession(),
      decisions: [{ ...materialDecision(), sessionId: "creative_other" }],
      auditEvents: [],
    })).rejects.toThrow("creative-bundle-session-mismatch");

    await expect(store.listCreativeSessions()).resolves.toEqual([]);
  });

  it("requires eligible historical segments as atomic creative bundle preconditions", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "library.json"));
    const session = creativeSession();
    const segment = contentSegment();
    await store.putSegments([segment]);

    await expect(store.commitCreativeBundle({
      session,
      requiredEligibleHistoricalSegmentIds: [segment.id, segment.id],
      auditEvents: [],
    })).rejects.toThrow("duplicate-material-precondition-id");

    await store.putSegments([{ ...segment, permissionState: "withdrawn" }]);

    await expect(store.commitCreativeBundle({
      session,
      requiredEligibleHistoricalSegmentIds: [segment.id],
      auditEvents: [],
    })).rejects.toThrow("material-no-longer-eligible");
    await expect(store.listCreativeSessions()).resolves.toEqual([]);
  });

  it("keeps stale-content-store protection between creative store instances", async () => {
    const path = join(testDirectory, "library.json");
    const first = await JsonContentStore.open(path);
    const second = await JsonContentStore.open(path);
    await first.putCreativeSession(creativeSession());

    await expect(second.putCreativeSession({ ...creativeSession(), id: "creative_02" }))
      .rejects.toThrow("stale-content-store");
  });

  it("does not let a forged dispatch reservation opt a stale writer into ledger merging", async () => {
    const path = join(testDirectory, "library.json");
    const current = await JsonContentStore.open(path);
    const stale = await JsonContentStore.open(path);
    const first = creativeSession();
    await current.putCreativeSession(first);

    await expect(stale.commitCreativeBundle({
      session: { ...first, id: "creative_02", title: "Forged merge" },
      dispatchReservationId: "forged-reservation",
      auditEvents: [],
    })).rejects.toThrow("creative-dispatch-reservation-required");
    await expect((await JsonContentStore.open(path)).listCreativeSessions()).resolves.toEqual([first]);
  });
});

function oldSnapshot() {
  return {
    assets: [],
    untypedSegments: [],
    segments: [],
    stories: [],
    revisions: [],
    artifacts: [],
    approvals: [],
    auditEvents: [],
    exportPackages: [],
    deliveries: [],
  };
}

function legacyShotPlanSnapshot() {
  return {
    ...oldSnapshot(),
    shotPlans: [{
      id: "shot_plan_legacy",
      sessionId: "creative_legacy",
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      status: "active",
      steps: [{
        id: "step_legacy",
        order: 1,
        purpose: "Open the repair story",
        expressionSuggestion: "Show the maker at the bench.",
        visualDirection: "Wide bench shot.",
        durationSeconds: 10,
        mustCapture: ["maker hands"],
        optionalGapSuggestions: ["Try a close-up if time allows"],
        prohibitedCapture: ["customer faces"],
        completionCriteria: ["maker hands visible"],
        deviceRoles: ["watch-director", "main-stone-a"],
      }],
      createdAt: "2026-09-05T08:00:00.000Z",
    }],
  };
}

function creativeSession(): CreativeSession {
  return {
    id: "creative_01",
    customerId: "customer_01",
    title: "Pendant origin story",
    stage: "draft_intent",
    currentIntentVersion: 0,
    currentBriefVersion: 0,
    currentScriptVersion: 0,
    currentShotPlanVersion: 0,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: [],
    status: "active",
    createdAt: "2026-09-04T08:00:00.000Z",
    updatedAt: "2026-09-04T08:00:00.000Z",
  };
}

function creativeIntent(): CreativeIntent {
  return {
    id: "intent_01",
    sessionId: "creative_01",
    version: 1,
    platforms: ["Xiaohongshu"],
    audience: "Jewelry collectors",
    theme: "A pendant returns to the bench",
    products: [{ id: "product_01", status: "confirmed" }],
    shootConditions: [],
    prohibitedAreas: [],
    tone: "Quiet and crafted",
    durationSeconds: 30,
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function materialDecision(): MaterialUseDecision {
  return {
    id: "decision_01",
    sessionId: "creative_01",
    suggestionSetId: "suggestion_01",
    suggestionVersion: 1,
    action: "confirm-selection",
    shownSegmentIds: ["segment_01"],
    selectedSegmentIds: ["segment_01"],
    rejectedSegmentIds: [],
    actorId: "designer_01",
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function contentSegment(): ContentSegment {
  return {
    id: "segment_01",
    assetId: "asset_01",
    batchId: "batch_history",
    startMs: 0,
    endMs: 10_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["scene"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}

function scriptPackage(sessionId: string): ScriptPackage {
  return {
    id: "script_01",
    sessionId,
    briefVersion: 1,
    scriptVersion: 1,
    status: "active",
    title: "Pendant origin story",
    platformDirection: "Xiaohongshu",
    opening: "A pendant begins again.",
    narrativeOrder: ["section_01"],
    sections: [{
      id: "section_01",
      expression: "Show the maker's careful return.",
      source: "customer-input",
      referencedSegmentIds: [],
    }],
    confirmedAt: "2026-09-04T08:00:00.000Z",
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function audit(sessionId: string): AuditEvent {
  return {
    id: "audit_01",
    entityType: "creative-session",
    entityId: sessionId,
    actorId: "designer_01",
    action: "script-confirmed",
    reason: null,
    before: null,
    after: { scriptVersion: 1 },
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function transitionAudit(
  sessionId: string,
  beforeStage: CreativeSession["stage"],
  afterStage: CreativeSession["stage"],
): AuditEvent {
  return {
    id: `audit_${sessionId}_${afterStage}`,
    entityType: "creative-session",
    entityId: sessionId,
    actorId: "designer_01",
    action: "creative-session-transitioned",
    reason: "advance approved session",
    before: { stage: beforeStage },
    after: { stage: afterStage },
    createdAt: "2026-09-04T08:01:00.000Z",
  };
}

function sameStageAudit(sessionId: string): AuditEvent {
  return {
    id: `audit_${sessionId}_intent-update`,
    entityType: "creative-session",
    entityId: sessionId,
    actorId: "designer_01",
    action: "creative-intent-answered",
    reason: "customer answered a missing fact",
    before: { stage: "draft_intent" },
    after: { stage: "draft_intent" },
    createdAt: "2026-09-04T08:01:00.000Z",
  };
}

function sameStageAuditWithId(sessionId: string, id: string): AuditEvent {
  return { ...sameStageAudit(sessionId), id };
}

function creativeSessionPrecondition(session: CreativeSession) {
  return {
    id: session.id,
    stage: session.stage,
    currentIntentVersion: session.currentIntentVersion,
    currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion,
    currentShotPlanVersion: session.currentShotPlanVersion,
  };
}

function scriptPrecondition(script: ScriptPackage) {
  return {
    id: script.id,
    sessionId: script.sessionId,
    briefVersion: script.briefVersion,
    scriptVersion: script.scriptVersion,
    status: script.status,
    confirmedAt: script.confirmedAt,
  };
}

type Deferred<T> = {
  promise: Promise<T>;
  resolve(value?: T): void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value?: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = (value) => done(value as T);
  });
  return { promise, resolve };
}
