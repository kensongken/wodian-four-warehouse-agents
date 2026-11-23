import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ContentSegment,
  CreativeBrief,
  CreativeIntent,
  CreativeSession,
  DeviceBrief,
  DirectorCard,
  DispatchReceipt,
  MaterialUseDecision,
  ScriptPackage,
  ShotPlan,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmScriptPackage,
  generateScriptPackage,
  JsonContentStore,
  reviseScriptPackage,
  withdrawSegmentPermission,
  type CreativeScriptAgent,
  type ScriptDraft,
} from "../src";

type RevisionMetadata = {
  parentScriptId: string | null;
  designerPrompt: string | null;
  changeExplanation: string | null;
};

describe("provenance-aware script planner", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-script-planner-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("generates no historical section after the customer declines history", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({
      sections: [section("customer-input", [])],
    });

    const script = await generateScriptPackage(scriptInput(), agent, library);

    expect(script.sections.filter((item) => item.source === "historical-material"))
      .toEqual([]);
    expect((await library.listCreativeBriefs())[0]?.confirmedSegmentIds).toEqual([]);
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "awaiting_script_confirmation",
      currentBriefVersion: 1,
      currentScriptVersion: 1,
    });
  });

  it("requires an empty confirm-selection after viewing without choosing anything", async () => {
    const accept = materialDecision("accept-view", []);
    await seedSession(library, {
      stage: "reviewing_materials",
      materialViewDecisionIds: [accept.id],
    }, [accept]);

    await expect(generateScriptPackage(scriptInput(), scriptAgent(), library))
      .rejects.toThrow("script-material-decision-required");

    const confirmedEmpty = materialDecision("confirm-selection", [], "decision_empty");
    const confirmedLibrary = await JsonContentStore.open(join(testDirectory, "confirmed-empty.json"));
    await seedSession(confirmedLibrary, {
      stage: "awaiting_script_confirmation",
      materialViewDecisionIds: [accept.id],
      materialUseDecisionIds: [confirmedEmpty.id],
    }, [accept, confirmedEmpty]);

    const script = await generateScriptPackage(scriptInput(), scriptAgent({
      sections: [section("customer-input", [])],
    }), confirmedLibrary);

    expect(script.sections.flatMap((item) => item.referencedSegmentIds)).toEqual([]);
  });

  it("does not accept a confirm-selection decision stored only as a viewing pointer", async () => {
    const misplaced = materialDecision("confirm-selection", [], "decision_misplaced");
    await seedSession(library, {
      stage: "awaiting_script_confirmation",
      materialViewDecisionIds: [misplaced.id],
    }, [misplaced]);

    await expect(generateScriptPackage(scriptInput(), scriptAgent(), library))
      .rejects.toThrow("script-material-decision-required");
  });

  it("passes and permits only the exact selected historical segments", async () => {
    const selected = segment("segment_selected");
    const unselected = segment("segment_unselected");
    await library.putAsset(parentAsset(selected));
    await library.putAsset(parentAsset(unselected));
    await library.putSegments([selected, unselected]);
    const decision = materialDecision("confirm-selection", [selected.id]);
    await seedSession(library, {
      stage: "awaiting_script_confirmation",
      materialUseDecisionIds: [decision.id],
    }, [decision]);
    let receivedIds: string[] = [];
    const agent = scriptAgent({
      sections: [section("historical-material", [selected.id])],
      onDraftSegments: (segments) => {
        receivedIds = segments.map((item) => item.id);
      },
    });

    const script = await generateScriptPackage(scriptInput(), agent, library);

    expect(receivedIds).toEqual([selected.id]);
    expect(script.sections[0]?.referencedSegmentIds).toEqual([selected.id]);
    expect((await library.listCreativeBriefs())[0]?.confirmedSegmentIds)
      .toEqual([selected.id]);
  });

  it("rejects historical references that were not selected", async () => {
    const selected = segment("segment_selected");
    const unselected = segment("segment_unselected");
    await library.putSegments([selected, unselected]);
    const decision = materialDecision("confirm-selection", [selected.id]);
    await seedSession(library, {
      stage: "awaiting_script_confirmation",
      materialUseDecisionIds: [decision.id],
    }, [decision]);

    await expect(generateScriptPackage(scriptInput(), scriptAgent({
      sections: [section("historical-material", [unselected.id])],
    }), library)).rejects.toThrow("script-material-not-selected");

    expect(await library.listScriptPackages()).toEqual([]);
  });

  for (const source of ["customer-input", "agent-suggestion", "shoot-suggestion"] as const) {
    it(`rejects historical IDs smuggled through ${source} sections`, async () => {
      const selected = segment("segment_selected");
      await library.putSegments([selected]);
      const decision = materialDecision("confirm-selection", [selected.id]);
      await seedSession(library, {
        stage: "awaiting_script_confirmation",
        materialUseDecisionIds: [decision.id],
      }, [decision]);

      await expect(generateScriptPackage(scriptInput(), scriptAgent({
        sections: [section(source, [selected.id])],
      }), library)).rejects.toThrow("script-material-not-selected");
    });
  }

  it("requires one valid provenance per section and source IDs for historical sections", async () => {
    const selected = segment("segment_selected");
    await library.putSegments([selected]);
    const decision = materialDecision("confirm-selection", [selected.id]);
    await seedSession(library, {
      stage: "awaiting_script_confirmation",
      materialUseDecisionIds: [decision.id],
    }, [decision]);

    await expect(generateScriptPackage(scriptInput(), scriptAgent({
      sections: [section("historical-material", [])],
    }), library)).rejects.toThrow("script-historical-source-required");

    const invalidSections = [{
      ...section("customer-input", []),
      source: ["customer-input", "agent-suggestion"],
    }] as unknown as ScriptPackage["sections"];
    await expect(generateScriptPackage(scriptInput(), scriptAgent({
      sections: invalidSections,
    }), library)).rejects.toThrow("script-section-source-invalid");
  });

  it("rejects a malformed untrusted agent draft before persistence", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const malformedAgent = {
      draft: async () => ({
        title: "Malformed",
        platformDirection: "Xiaohongshu",
        opening: "Opening",
        narrativeOrder: ["missing"],
      }),
      revise: async () => scriptAgent().revise({
        current: scriptPackage("unused", SESSION_ID, 1, []),
        prompt: "unused",
        selectedHistoricalSegments: [],
      }),
    } as unknown as CreativeScriptAgent;

    await expect(generateScriptPackage(scriptInput(), malformedAgent, library))
      .rejects.toThrow("script-draft-invalid");
    expect(await library.listScriptPackages()).toEqual([]);
  });

  it("rejects generation when the captured session changes while the Agent is drafting", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const entered = deferred<void>();
    const draft = deferred<ScriptDraft>();
    const baseAgent = scriptAgent();
    const agent: CreativeScriptAgent = {
      draft: async () => {
        entered.resolve();
        return draft.promise;
      },
      revise: baseAgent.revise,
    };

    const generation = generateScriptPackage(scriptInput(), agent, library);
    await entered.promise;
    const current = (await library.listCreativeSessions())[0]!;
    await library.commitCreativeBundle({
      session: {
        ...current,
        currentIntentVersion: 2,
        updatedAt: "2026-09-04T08:03:30.000Z",
      },
      intents: [{
        ...creativeIntent(current.id),
        id: "intent_creative_01_v2",
        version: 2,
        theme: "A newer intent",
      }],
      auditEvents: [sessionAudit(current.stage, current.stage, "intent changed during draft")],
    });
    draft.resolve(await baseAgent.draft({
      intent: creativeIntent(SESSION_ID),
      selectedHistoricalSegments: [],
    }));

    await expect(generation).rejects.toThrow("creative-session-precondition-failed");
    expect(await library.listScriptPackages()).toEqual([]);
    expect((await library.listCreativeSessions())[0]?.currentIntentVersion).toBe(2);
  });

  it("preserves version 1 and records revision lineage, prompt, and explanation on version 2", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({ changeExplanation: "Tightened the opening." });
    const first = await generateScriptPackage(scriptInput(), agent, library);

    const second = await reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Make the opening shorter.",
      actorId: "designer_01",
      now: "2026-09-04T08:05:00.000Z",
    }, agent, library) as ScriptPackage & RevisionMetadata;

    const stored = await library.listScriptPackages() as Array<ScriptPackage & RevisionMetadata>;
    expect(stored).toHaveLength(2);
    expect(stored[0]).toEqual(first);
    expect(second).toMatchObject({
      scriptVersion: 2,
      parentScriptId: first.id,
      designerPrompt: "Make the opening shorter.",
      changeExplanation: "Tightened the opening.",
      confirmedAt: null,
    });
  });

  it("confirms only the current script version and advances with a fresh audit", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({ changeExplanation: "Updated." });
    await generateScriptPackage(scriptInput(), agent, library);
    await reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Revise.",
      actorId: "designer_01",
      now: "2026-09-04T08:05:00.000Z",
    }, agent, library);

    await expect(confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:06:00.000Z",
    }, library)).rejects.toThrow("script-version-not-current");

    const confirmed = await confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 2,
      actorId: "customer_01",
      now: "2026-09-04T08:06:00.000Z",
    }, library);
    expect(confirmed.confirmedAt).toBe("2026-09-04T08:06:00.000Z");
    expect((await library.listCreativeSessions())[0]?.stage).toBe("ready_to_dispatch");
    expect(await library.listAuditEvents(SESSION_ID)).toContainEqual(expect.objectContaining({
      actorId: "customer_01",
      action: "script-confirmed",
      after: { scriptVersion: 2 },
    }));
  });

  it("prevents a paused stale V1 confirmation from restoring V1 after a concurrent revision", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const initialAgent = scriptAgent({ changeExplanation: "Concurrent revision." });
    const first = await generateScriptPackage(scriptInput(), initialAgent, library);

    const confirmationEntered = deferred<void>();
    const releaseConfirmation = deferred<void>();
    const revisionCommitEntered = deferred<void>();
    const releaseRevisionCommit = deferred<void>();
    const originalCommit = library.commitCreativeBundle.bind(library);
    library.commitCreativeBundle = async (value) => {
      const script = value.scripts?.[0];
      if (script?.id === first.id && script.confirmedAt !== null) {
        confirmationEntered.resolve();
        await releaseConfirmation.promise;
      }
      if (script?.scriptVersion === 2) {
        revisionCommitEntered.resolve();
        await releaseRevisionCommit.promise;
      }
      return originalCommit(value);
    };

    const confirmation = confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:06:00.000Z",
    }, library);
    await confirmationEntered.promise;

    const agentEntered = deferred<void>();
    const revisedDraft = deferred<ScriptDraft>();
    const revisionAgent: CreativeScriptAgent = {
      draft: initialAgent.draft,
      revise: async () => {
        agentEntered.resolve();
        return revisedDraft.promise;
      },
    };
    const revision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Revise while confirmation is pending.",
      actorId: "designer_01",
      now: "2026-09-04T08:07:00.000Z",
    }, revisionAgent, library);
    await agentEntered.promise;
    revisedDraft.resolve({
      title: "Pendant origin story V2",
      platformDirection: "Xiaohongshu",
      opening: "A revised opening.",
      narrativeOrder: ["section_customer-input"],
      sections: [section("customer-input", [])],
      changeExplanation: "Concurrent revision.",
    });
    await revisionCommitEntered.promise;

    releaseRevisionCommit.resolve();
    await revision;

    releaseConfirmation.resolve();
    await expect(confirmation).rejects.toThrow("creative-session-precondition-failed");
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "awaiting_script_confirmation",
      currentScriptVersion: 2,
    });
    expect((await library.listScriptPackages()).find((item) => item.id === first.id))
      .toMatchObject({ confirmedAt: null, status: "active" });
  });

  it("persists only one V2 when two revisions race from the same V1", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const initialAgent = scriptAgent();
    await generateScriptPackage(scriptInput(), initialAgent, library);
    const firstEntered = deferred<void>();
    const secondEntered = deferred<void>();
    const firstDraft = deferred<ScriptDraft>();
    const secondDraft = deferred<ScriptDraft>();
    const revisionAgent = (entered: Deferred<void>, pending: Deferred<ScriptDraft>): CreativeScriptAgent => ({
      draft: initialAgent.draft,
      revise: async () => {
        entered.resolve();
        return pending.promise;
      },
    });

    const firstRevision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "First concurrent revision.",
      actorId: "designer_01",
      now: "2026-09-04T08:07:00.000Z",
    }, revisionAgent(firstEntered, firstDraft), library);
    const secondRevision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Second concurrent revision.",
      actorId: "designer_02",
      now: "2026-09-04T08:07:01.000Z",
    }, revisionAgent(secondEntered, secondDraft), library);
    await Promise.all([firstEntered.promise, secondEntered.promise]);
    firstDraft.resolve(revisionDraft("First result."));
    secondDraft.resolve(revisionDraft("Second result."));

    const results = await Promise.allSettled([firstRevision, secondRevision]);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    const rejected = results.find((result) => result.status === "rejected") as PromiseRejectedResult;
    expect(rejected.reason).toEqual(expect.objectContaining({
      message: "creative-session-precondition-failed",
    }));
    expect((await library.listScriptPackages()).filter((item) => item.scriptVersion === 2))
      .toHaveLength(1);
  });

  it("invalidates downstream plans when revising a confirmed script but keeps receipts as evidence", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({ changeExplanation: "Reframed the close." });
    await generateScriptPackage(scriptInput(), agent, library);
    await confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:04:00.000Z",
    }, library);
    const currentSession = (await library.listCreativeSessions())[0]!;
    const plan = shotPlan(SESSION_ID, 1, 1, 1);
    const card = directorCard(plan);
    const brief = deviceBrief(plan);
    const receipt = dispatchReceipt(plan);
    await library.commitCreativeBundle({
      session: { ...currentSession, currentShotPlanVersion: 1 },
      shotPlans: [plan],
      directorCards: [card],
      deviceBriefs: [brief],
      receipts: [receipt],
      auditEvents: [sessionAudit(currentSession.stage, currentSession.stage, "shot plan recorded")],
    });
    expect((await library.listShotPlans())[0]?.status).toBe("active");
    expect((await library.listDirectorCards())[0]?.status).toBe("active");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("active");

    const revised = await reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Change the closing line.",
      actorId: "designer_01",
      now: "2026-09-04T08:05:00.000Z",
    }, agent, library);

    expect(revised).toMatchObject({ scriptVersion: 2, confirmedAt: null });
    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
    expect(await library.listDispatchReceipts()).toEqual([receipt]);
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "awaiting_script_confirmation",
      currentScriptVersion: 2,
      currentShotPlanVersion: 0,
    });
    expect((await library.listScriptPackages()).find((item) => item.scriptVersion === 1))
      .toMatchObject({
        status: "active",
        confirmedAt: "2026-09-04T08:04:00.000Z",
      });
  });

  it("invalidates confirmed-script descendants created while the revision Agent is waiting", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const initialAgent = scriptAgent();
    await generateScriptPackage(scriptInput(), initialAgent, library);
    await confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:04:00.000Z",
    }, library);

    const agentEntered = deferred<void>();
    const pendingDraft = deferred<ScriptDraft>();
    const revision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Revise after new task objects appear.",
      actorId: "designer_01",
      now: "2026-09-04T08:07:00.000Z",
    }, {
      draft: initialAgent.draft,
      revise: async () => {
        agentEntered.resolve();
        return pendingDraft.promise;
      },
    }, library);
    await agentEntered.promise;

    const latePlan = shotPlan(SESSION_ID, 1, 1, 1);
    const lateCard = directorCard(latePlan);
    const lateBrief = deviceBrief(latePlan);
    await library.putShotPlan(latePlan);
    await library.putDirectorCard(lateCard);
    await library.putDeviceBrief(lateBrief);
    pendingDraft.resolve(revisionDraft("Invalidate late task lineage."));

    await revision;
    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
  });

  it("queues a low-level plan put behind revision persistence and stores it invalidated", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({ changeExplanation: "Supersede V1." });
    await generateScriptPackage(scriptInput(), agent, library);
    await confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:04:00.000Z",
    }, library);
    const persistence = library as unknown as { persist: () => Promise<void> };
    const realPersist = persistence.persist.bind(library);
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist").mockImplementationOnce(async () => {
      persistEntered.resolve();
      await releasePersist.promise;
      return realPersist();
    });

    const revision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Create V2 while V1 plan arrives.",
      actorId: "designer_01",
      now: "2026-09-04T08:07:00.000Z",
    }, agent, library);
    await persistEntered.promise;
    let putSettled = false;
    const plan = shotPlan(SESSION_ID, 1, 1, 1);
    const put = library.putShotPlan(plan).then(() => {
      putSettled = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(putSettled).toBe(false);

    releasePersist.resolve();
    await Promise.all([revision, put]);
    expect((await library.listCreativeSessions())[0]?.currentScriptVersion).toBe(2);
    expect((await library.listShotPlans())[0]).toEqual({
      ...plan,
      status: "invalidated",
    });
  });

  it("normalizes a V1 task bundle queued behind V2 revision persistence", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const agent = scriptAgent({ changeExplanation: "Supersede V1 via queued bundle." });
    await generateScriptPackage(scriptInput(), agent, library);
    await confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:04:00.000Z",
    }, library);
    const confirmedSession = (await library.listCreativeSessions())[0]!;
    const v2Session: CreativeSession = {
      ...confirmedSession,
      stage: "awaiting_script_confirmation",
      currentScriptVersion: 2,
      currentShotPlanVersion: 0,
      updatedAt: "2026-09-04T08:07:00.000Z",
    };
    const persistence = library as unknown as { persist: () => Promise<void> };
    const realPersist = persistence.persist.bind(library);
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist").mockImplementationOnce(async () => {
      persistEntered.resolve();
      await releasePersist.promise;
      return realPersist();
    });

    const revision = reviseScriptPackage({
      sessionId: SESSION_ID,
      prompt: "Create V2 before queued task bundle.",
      actorId: "designer_01",
      now: v2Session.updatedAt,
    }, agent, library);
    await persistEntered.promise;
    const plan = shotPlan(SESSION_ID, 1, 1, 1);
    const bundle = library.commitCreativeBundle({
      session: v2Session,
      expectedSession: creativeSessionPrecondition(v2Session),
      shotPlans: [plan],
      directorCards: [directorCard(plan)],
      deviceBriefs: [deviceBrief(plan)],
      auditEvents: [],
    });
    let bundleSettled = false;
    void bundle.then(() => {
      bundleSettled = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(bundleSettled).toBe(false);

    releasePersist.resolve();
    await Promise.all([revision, bundle]);
    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
  });

  it("rejects confirmation if the captured script state changes before commit", async () => {
    const decision = materialDecision("decline-view", []);
    await seedSession(library, {
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
    }, [decision]);
    const first = await generateScriptPackage(scriptInput(), scriptAgent(), library);
    const commitEntered = deferred<void>();
    const releaseCommit = deferred<void>();
    const originalCommit = library.commitCreativeBundle.bind(library);
    library.commitCreativeBundle = async (value) => {
      if (value.scripts?.[0]?.confirmedAt !== null) {
        commitEntered.resolve();
        await releaseCommit.promise;
      }
      return originalCommit(value);
    };
    const confirmation = confirmScriptPackage({
      sessionId: SESSION_ID,
      scriptVersion: 1,
      actorId: "customer_01",
      now: "2026-09-04T08:06:00.000Z",
    }, library);
    await commitEntered.promise;
    await library.putScriptPackage({ ...first, confirmedAt: "2026-09-04T08:05:00.000Z" });
    releaseCommit.resolve();

    await expect(confirmation).rejects.toThrow("script-precondition-failed");
  });

  it("allows only confirmation and invalidation transitions for a stored script", async () => {
    const original = scriptPackage("script_transition", SESSION_ID, 1, []);
    await library.putScriptPackage(original);
    const confirmed = { ...original, confirmedAt: "2026-09-04T08:06:00.000Z" };

    await library.putScriptPackage(confirmed);
    await expect(library.putScriptPackage({
      ...confirmed,
      confirmedAt: "2026-09-04T08:07:00.000Z",
    })).rejects.toThrow("immutable-creative-object-conflict");
    await expect(library.putScriptPackage({
      ...confirmed,
      title: "Mutated title",
    })).rejects.toThrow("immutable-creative-object-conflict");

    await library.putScriptPackage({ ...confirmed, status: "invalidated" });
    await expect(library.putScriptPackage(confirmed))
      .rejects.toThrow("immutable-creative-object-conflict");

    const unconfirmed = scriptPackage("script_unconfirmed_transition", SESSION_ID, 2, []);
    await library.putScriptPackage(unconfirmed);
    await expect(library.putScriptPackage({
      ...unconfirmed,
      status: "invalidated",
      confirmedAt: "2026-09-04T08:08:00.000Z",
    })).rejects.toThrow("immutable-creative-object-conflict");
    await library.putScriptPackage({ ...unconfirmed, status: "invalidated" });
    await expect(library.putScriptPackage({
      ...unconfirmed,
      status: "invalidated",
      confirmedAt: "2026-09-04T08:08:00.000Z",
    })).rejects.toThrow("immutable-creative-object-conflict");
  });
});

describe("creative lineage privacy invalidation", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-script-withdrawal-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("atomically invalidates only the creative lineage that references a withdrawn segment", async () => {
    const withdrawn = segment("segment_withdrawn");
    const safe = segment("segment_safe");
    await library.putSegments([withdrawn, safe]);
    const affectedSession = creativeSession("creative_affected", "awaiting_script_confirmation");
    const safeSession = creativeSession("creative_safe", "awaiting_script_confirmation");
    await library.putCreativeSession(affectedSession);
    await library.putCreativeSession(safeSession);
    const affectedBrief = creativeBrief(affectedSession.id, 1, [withdrawn.id]);
    const safeBrief = creativeBrief(safeSession.id, 1, [safe.id]);
    await library.putCreativeBrief(affectedBrief);
    await library.putCreativeBrief(safeBrief);
    const affectedScript = scriptPackage("script_affected", affectedSession.id, 1, [withdrawn.id]);
    const safeScript = scriptPackage("script_safe", safeSession.id, 1, [safe.id]);
    await library.putScriptPackage(affectedScript);
    await library.putScriptPackage(safeScript);
    const affectedPlan = shotPlan(affectedSession.id, 1, 1, 1);
    const safePlan = shotPlan(safeSession.id, 1, 1, 1);
    const affectedCard = directorCard(affectedPlan);
    const safeCard = directorCard(safePlan);
    const affectedDeviceBrief = deviceBrief(affectedPlan);
    const safeDeviceBrief = deviceBrief(safePlan);
    const affectedReceipt = dispatchReceipt(affectedPlan);
    await library.putShotPlan(affectedPlan);
    await library.putShotPlan(safePlan);
    await library.putDirectorCard(affectedCard);
    await library.putDirectorCard(safeCard);
    await library.putDeviceBrief(affectedDeviceBrief);
    await library.putDeviceBrief(safeDeviceBrief);
    await library.putDispatchReceipt(affectedReceipt);

    const result = await withdrawSegmentPermission({
      segmentId: withdrawn.id,
      reason: "customer-withdrew",
      actorId: "designer_01",
    }, library);

    expect(result).toMatchObject({
      invalidatedCreativeBriefIds: [affectedBrief.id],
      invalidatedScriptPackageIds: [affectedScript.id],
      invalidatedShotPlanIds: [affectedPlan.id],
      invalidatedDirectorCardIds: [affectedCard.id],
      invalidatedDeviceBriefIds: [affectedDeviceBrief.id],
    });
    expect((await library.listScriptPackages()).find((item) => item.id === safeScript.id)?.status)
      .toBe("active");
    expect((await library.listShotPlans()).find((item) => item.id === safePlan.id)?.status)
      .toBe("active");
    expect((await library.listDirectorCards()).find((item) => item.id === safeCard.id)?.status)
      .toBe("active");
    expect((await library.listDeviceBriefs()).find((item) => item.id === safeDeviceBrief.id)?.status)
      .toBe("active");
    expect(await library.listCreativeBriefs()).toEqual([
      { ...affectedBrief, status: "invalidated" },
      { ...safeBrief, status: "active" },
    ]);
    expect(await library.listDispatchReceipts()).toEqual([affectedReceipt]);
  });

  it("recomputes creative dependencies that appear while withdrawal is waiting to commit", async () => {
    const withdrawn = segment("segment_late_dependency");
    await library.putSegments([withdrawn]);
    const session = creativeSession("creative_late", "awaiting_script_confirmation");
    await library.putCreativeSession(session);
    const commitEntered = deferred<void>();
    const releaseCommit = deferred<void>();
    const originalCommit = library.commitPrivacyInvalidation.bind(library);
    library.commitPrivacyInvalidation = async (value) => {
      commitEntered.resolve();
      await releaseCommit.promise;
      return originalCommit(value);
    };

    const withdrawal = withdrawSegmentPermission({
      segmentId: withdrawn.id,
      reason: "customer-withdrew-during-race",
      actorId: "designer_01",
    }, library);
    await commitEntered.promise;
    const lateScript = scriptPackage("script_late", session.id, 1, [withdrawn.id]);
    const latePlan = shotPlan(session.id, 1, 1, 1);
    const lateCard = directorCard(latePlan);
    const lateBrief = deviceBrief(latePlan);
    await library.putScriptPackage(lateScript);
    await library.putShotPlan(latePlan);
    await library.putDirectorCard(lateCard);
    await library.putDeviceBrief(lateBrief);
    releaseCommit.resolve();

    const result = await withdrawal;
    expect(result).toMatchObject({
      invalidatedScriptPackageIds: [lateScript.id],
      invalidatedShotPlanIds: [latePlan.id],
      invalidatedDirectorCardIds: [lateCard.id],
      invalidatedDeviceBriefIds: [lateBrief.id],
    });
    expect((await library.listScriptPackages())[0]?.status).toBe("invalidated");
    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
  });

  it("queues a low-level plan put behind withdrawal persistence and stores it invalidated", async () => {
    const withdrawn = segment("segment_withdrawal_queue");
    await library.putSegments([withdrawn]);
    const session = creativeSession("creative_withdrawal_queue", "awaiting_script_confirmation");
    await library.putCreativeSession(session);
    const affectedScript = scriptPackage(
      "script_withdrawal_queue",
      session.id,
      1,
      [withdrawn.id],
    );
    await library.putScriptPackage(affectedScript);
    const persistence = library as unknown as { persist: () => Promise<void> };
    const realPersist = persistence.persist.bind(library);
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist").mockImplementationOnce(async () => {
      persistEntered.resolve();
      await releasePersist.promise;
      return realPersist();
    });

    const withdrawal = withdrawSegmentPermission({
      segmentId: withdrawn.id,
      reason: "customer-withdrew-before-plan-write",
      actorId: "designer_01",
    }, library);
    await persistEntered.promise;
    let putSettled = false;
    const plan = shotPlan(session.id, 1, 1, 1);
    const put = library.putShotPlan(plan).then(() => {
      putSettled = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(putSettled).toBe(false);

    releasePersist.resolve();
    await Promise.all([withdrawal, put]);
    expect((await library.listSegments())[0]?.permissionState).toBe("withdrawn");
    expect((await library.listScriptPackages())[0]?.status).toBe("invalidated");
    expect((await library.listShotPlans())[0]).toEqual({
      ...plan,
      status: "invalidated",
    });
  });

  it("normalizes active task objects inserted by a bundle after permission withdrawal", async () => {
    const withdrawn = segment("segment_withdrawn_before_bundle");
    await library.putSegments([withdrawn]);
    const session = creativeSession("creative_bundle_after_withdrawal", "awaiting_script_confirmation");
    await library.putCreativeSession(session);
    await library.putScriptPackage(scriptPackage(
      "script_bundle_after_withdrawal",
      session.id,
      1,
      [withdrawn.id],
    ));
    await withdrawSegmentPermission({
      segmentId: withdrawn.id,
      reason: "customer-withdrew-before-bundle",
      actorId: "designer_01",
    }, library);
    const plan = shotPlan(session.id, 1, 1, 1);

    await library.commitCreativeBundle({
      session,
      shotPlans: [plan],
      directorCards: [directorCard(plan)],
      deviceBriefs: [deviceBrief(plan)],
      auditEvents: [],
    });

    expect((await library.listShotPlans())[0]?.status).toBe("invalidated");
    expect((await library.listDirectorCards())[0]?.status).toBe("invalidated");
    expect((await library.listDeviceBriefs())[0]?.status).toBe("invalidated");
  });

  it("rolls back failed withdrawal before a queued low-level plan put in memory and disk", async () => {
    const path = join(testDirectory, "library.json");
    const withdrawn = segment("segment_failed_withdrawal");
    await library.putSegments([withdrawn]);
    const session = creativeSession("creative_failed_withdrawal", "awaiting_script_confirmation");
    await library.putCreativeSession(session);
    const affectedScript = scriptPackage(
      "script_failed_withdrawal",
      session.id,
      1,
      [withdrawn.id],
    );
    await library.putScriptPackage(affectedScript);
    const persistence = library as unknown as { persist: () => Promise<void> };
    const realPersist = persistence.persist.bind(library);
    const persistEntered = deferred<void>();
    const releasePersist = deferred<void>();
    vi.spyOn(persistence, "persist")
      .mockImplementationOnce(async () => {
        persistEntered.resolve();
        await releasePersist.promise;
        throw new Error("privacy-persist-failed");
      })
      .mockImplementation(() => realPersist());

    const withdrawal = withdrawSegmentPermission({
      segmentId: withdrawn.id,
      reason: "failed-withdrawal",
      actorId: "designer_01",
    }, library);
    const withdrawalFailure = withdrawal.catch((error: unknown) => error);
    await persistEntered.promise;
    const plan = shotPlan(session.id, 1, 1, 1);
    const put = library.putShotPlan(plan);
    releasePersist.resolve();

    await expect(withdrawalFailure).resolves.toEqual(expect.objectContaining({
      message: "privacy-persist-failed",
    }));
    await put;
    const reopened = await JsonContentStore.open(path);
    expect(await library.listSegments()).toEqual(await reopened.listSegments());
    expect(await library.listScriptPackages()).toEqual(await reopened.listScriptPackages());
    expect(await library.listShotPlans()).toEqual(await reopened.listShotPlans());
    expect((await library.listSegments())[0]?.permissionState).toBe("allowed");
    expect((await library.listScriptPackages())[0]?.status).toBe("active");
    expect((await library.listShotPlans())[0]).toEqual(plan);
  });
});

const SESSION_ID = "creative_01";

function scriptInput() {
  return {
    sessionId: SESSION_ID,
    actorId: "designer_01",
    now: "2026-09-04T08:04:00.000Z",
  };
}

function scriptAgent(patch: {
  sections?: ScriptPackage["sections"];
  changeExplanation?: string;
  onDraftSegments?: (segments: ContentSegment[]) => void;
} = {}): CreativeScriptAgent {
  const makeDraft = () => ({
    title: "Pendant origin story",
    platformDirection: "Xiaohongshu",
    opening: "A pendant begins again.",
    narrativeOrder: (patch.sections ?? [section("customer-input", [])]).map((item) => item.id),
    sections: patch.sections ?? [section("customer-input", [])],
    changeExplanation: patch.changeExplanation ?? "No material change.",
  });
  return {
    draft: async ({ selectedHistoricalSegments }) => {
      patch.onDraftSegments?.(selectedHistoricalSegments);
      return makeDraft();
    },
    revise: async () => makeDraft(),
  };
}

function revisionDraft(changeExplanation: string): ScriptDraft {
  return {
    title: "Pendant origin story revised",
    platformDirection: "Xiaohongshu",
    opening: "A revised opening.",
    narrativeOrder: ["section_customer-input"],
    sections: [section("customer-input", [])],
    changeExplanation,
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

function section(
  source: ScriptPackage["sections"][number]["source"],
  referencedSegmentIds: string[],
): ScriptPackage["sections"][number] {
  return {
    id: `section_${source}`,
    expression: "Tell the story clearly.",
    source,
    referencedSegmentIds,
  };
}

async function seedSession(
  library: JsonContentStore,
  patch: Partial<CreativeSession>,
  decisions: MaterialUseDecision[],
): Promise<void> {
  const session = { ...creativeSession(SESSION_ID, "awaiting_script_confirmation"), ...patch };
  await library.commitCreativeBundle({
    session,
    intents: [creativeIntent(session.id)],
    decisions,
    auditEvents: [],
  });
}

function creativeSession(id: string, stage: CreativeSession["stage"]): CreativeSession {
  return {
    id,
    customerId: `customer_${id}`,
    title: "Pendant origin story",
    stage,
    currentIntentVersion: 1,
    currentBriefVersion: 0,
    currentScriptVersion: 0,
    currentShotPlanVersion: 0,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: ["customer faces"],
    status: "active",
    createdAt: "2026-09-04T08:00:00.000Z",
    updatedAt: "2026-09-04T08:03:00.000Z",
  };
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

function creativeIntent(sessionId: string): CreativeIntent {
  return {
    id: `intent_${sessionId}`,
    sessionId,
    version: 1,
    platforms: ["Xiaohongshu"],
    audience: "Jewelry collectors",
    theme: "A pendant returns to the bench",
    products: [{ id: "product_01", status: "confirmed" }],
    shootConditions: ["maker available"],
    prohibitedAreas: ["customer faces"],
    tone: "Quiet and crafted",
    durationSeconds: 30,
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function materialDecision(
  action: MaterialUseDecision["action"],
  selectedSegmentIds: string[],
  id = `decision_${action}`,
): MaterialUseDecision {
  const shownSegmentIds = selectedSegmentIds.length > 0
    ? [...selectedSegmentIds, "segment_unselected"]
    : [];
  return {
    id,
    sessionId: SESSION_ID,
    suggestionSetId: "suggestion_01",
    suggestionVersion: 1,
    action,
    shownSegmentIds,
    selectedSegmentIds,
    rejectedSegmentIds: action === "decline-view" ? shownSegmentIds : [],
    actorId: "customer_01",
    createdAt: action === "accept-view"
      ? "2026-09-04T08:01:00.000Z"
      : "2026-09-04T08:02:00.000Z",
  };
}

function segment(id: string): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId: "batch_history",
    startMs: 0,
    endMs: 1_000,
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

function parentAsset(value: ContentSegment) {
  return {
    id: value.assetId,
    batchId: value.batchId,
    sessionId: "historical_session",
    digest: `digest_${value.id}`,
    localPath: `/library/${value.id}.mp4`,
    mediaKind: "video" as const,
    importedAt: "2026-09-03T08:00:00.000Z",
    permissionState: "allowed" as const,
    origin: "historical" as const,
    technicalFlags: [],
  };
}

function scriptPackage(
  id: string,
  sessionId: string,
  scriptVersion: number,
  referencedSegmentIds: string[],
): ScriptPackage {
  return {
    id,
    sessionId,
    briefVersion: 1,
    scriptVersion,
    status: "active",
    title: "Pendant origin story",
    platformDirection: "Xiaohongshu",
    opening: "A pendant begins again.",
    narrativeOrder: ["section_01"],
    sections: [{
      id: "section_01",
      expression: "Show the maker's careful return.",
      source: referencedSegmentIds.length > 0 ? "historical-material" : "customer-input",
      referencedSegmentIds,
    }],
    confirmedAt: null,
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function creativeBrief(sessionId: string, briefVersion: number, confirmedSegmentIds: string[]): CreativeBrief {
  return {
    id: `brief_${sessionId}_${briefVersion}`,
    sessionId,
    intentVersion: 1,
    briefVersion,
    title: "Pendant origin story",
    platformDirection: "Xiaohongshu",
    audience: "Jewelry collectors",
    theme: "A pendant returns to the bench",
    tone: "Quiet and crafted",
    durationSeconds: 30,
    confirmedSegmentIds,
    status: "active",
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function shotPlan(
  sessionId: string,
  briefVersion: number,
  scriptVersion: number,
  shotPlanVersion: number,
): ShotPlan {
  return {
    id: `shot_${sessionId}_${scriptVersion}_${shotPlanVersion}`,
    sessionId,
    briefVersion,
    scriptVersion,
    shotPlanVersion,
    status: "active",
    steps: [],
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function directorCard(plan: ShotPlan): DirectorCard {
  return {
    ...versionKey(plan),
    id: `card_${plan.id}`,
    stepId: "step_01",
    status: "active",
    purpose: "Open the story",
    spokenPrompt: "Introduce the pendant",
    shotDescription: "Maker at the bench",
    durationSeconds: 10,
    mustCapture: [],
    prohibitedCapture: ["customer faces"],
    availableActions: ["completed"],
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function deviceBrief(plan: ShotPlan): DeviceBrief {
  return {
    ...versionKey(plan),
    id: `device_brief_${plan.id}`,
    stepId: "step_01",
    status: "active",
    deviceId: "watch_01",
    deviceRole: "watch-director",
    carrier: "maker",
    startCondition: "step starts",
    endCondition: "step completes",
    composition: "medium shot",
    prohibitedAreas: ["customer faces"],
    privacyState: "creator",
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function dispatchReceipt(plan: ShotPlan): DispatchReceipt {
  return {
    ...versionKey(plan),
    id: `receipt_${plan.id}`,
    deviceId: "watch_01",
    deviceRole: "watch-director",
    transport: "simulated",
    idempotencyKey: `dispatch:${plan.id}`,
    status: "pending",
    createdAt: "2026-09-04T08:04:00.000Z",
  };
}

function versionKey(plan: ShotPlan) {
  return {
    sessionId: plan.sessionId,
    briefVersion: plan.briefVersion,
    scriptVersion: plan.scriptVersion,
    shotPlanVersion: plan.shotPlanVersion,
  };
}

function sessionAudit(
  beforeStage: CreativeSession["stage"],
  afterStage: CreativeSession["stage"],
  reason: string,
) {
  return {
    id: `audit_${reason.replaceAll(" ", "_")}`,
    entityType: "creative-session" as const,
    entityId: SESSION_ID,
    actorId: "designer_01",
    action: "creative-session-transitioned",
    reason,
    before: { stage: beforeStage },
    after: { stage: afterStage },
    createdAt: "2026-09-04T08:04:30.000Z",
  };
}
