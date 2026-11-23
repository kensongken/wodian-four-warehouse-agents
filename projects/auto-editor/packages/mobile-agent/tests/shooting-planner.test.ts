import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { AuditEvent, CreativeSession, ScriptPackage } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  createShootingPackage,
  JsonContentStore,
  type DeviceCapability,
  type ShotPlanDraft,
  type ShotPlanningAgent,
} from "../src";

const SESSION_ID = "creative_shooting_01";
const NOW = "2026-09-05T08:00:00.000Z";

describe("three-terminal shooting planner", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-shooting-planner-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("creates no shooting tasks before the current script is confirmed", async () => {
    await seedConfirmedScript(library, null);
    let agentCalled = false;
    const agent: ShotPlanningAgent = {
      plan: async () => {
        agentCalled = true;
        return validDraft();
      },
    };

    await expect(createShootingPackage(shootingInput(), agent, library))
      .rejects.toThrow("confirmed-script-required");

    expect(agentCalled).toBe(false);
    expect(await library.listShotPlans()).toEqual([]);
    expect(await library.listDirectorCards()).toEqual([]);
    expect(await library.listDeviceBriefs()).toEqual([]);
    expect(await library.listDispatchReceipts()).toEqual([]);
  });

  it("creates locally versioned watch cards and minimal main-stone briefs", async () => {
    await seedConfirmedScript(library, NOW);

    const result = await createShootingPackage(shootingInput(), planningAgent(), library);
    const session = (await library.listCreativeSessions())[0]!;
    const allTasks = [result.shotPlan, ...result.directorCards, ...result.deviceBriefs];

    expect(result.shotPlan).toMatchObject({
      sessionId: SESSION_ID,
      briefVersion: 1,
      scriptVersion: 1,
      shotPlanVersion: 1,
      status: "active",
      steps: expect.arrayContaining([
        expect.objectContaining({
          optionalGapSuggestions: [{ suggestion: "Try a close-up if time allows", required: false }],
        }),
      ]),
    });
    expect(allTasks).toHaveLength(7);
    expect(allTasks.every((task) => task.sessionId === SESSION_ID &&
      task.briefVersion === 1 && task.scriptVersion === 1 && task.shotPlanVersion === 1)).toBe(true);
    expect(result.directorCards).toHaveLength(result.shotPlan.steps.length);
    expect(new Set(result.directorCards.map((card) => card.stepId))).toEqual(
      new Set(result.shotPlan.steps.map((step) => step.id)),
    );
    for (const step of result.shotPlan.steps) {
      const mainStoneBriefs = result.deviceBriefs.filter((brief) =>
        brief.stepId === step.id && brief.deviceRole.startsWith("main-stone"),
      );
      expect(mainStoneBriefs.length).toBeGreaterThan(0);
    }
    expect(result.directorCards.flatMap((card) => card.prohibitedCapture))
      .toContain("customer faces");
    expect(result.deviceBriefs.flatMap((brief) => brief.prohibitedAreas))
      .toContain("customer faces");
    expect(result.deviceBriefs).toEqual(expect.arrayContaining([
      expect.objectContaining({
        startCondition: "director-card-start:1",
        endCondition: "director-card-stop:1",
        composition: "product-close-up",
        prohibitedAreas: ["customer faces"],
      }),
      expect.objectContaining({ composition: "scene-context" }),
    ]));
    expect(session).toMatchObject({
      stage: "ready_to_dispatch",
      currentShotPlanVersion: 1,
    });
    expect(await library.listDispatchReceipts()).toEqual([]);
    expect(await library.listAuditEvents(SESSION_ID)).toContainEqual(expect.objectContaining({
      actorId: "director_01",
      action: "shooting-package-created",
      before: { shotPlanVersion: 0 },
      after: { shotPlanVersion: 1 },
    }));
  });

  it("rejects incomplete or forced planner output before persisting any task", async () => {
    await seedConfirmedScript(library, NOW);
    const invalidDrafts = [
      invalidStep({ purpose: "" }),
      invalidStep({ durationSeconds: 0 }),
      invalidStep({ completionCriteria: [] }),
      invalidStep({ deviceRoles: [] }),
      invalidStep({ privacyReminder: "" }),
      invalidStep({ optionalGapSuggestions: [{ suggestion: "Must retake this", required: true }] }),
    ];

    for (const invalid of invalidDrafts) {
      await expect(createShootingPackage(shootingInput(), planningAgent(invalid), library))
        .rejects.toThrow("shot-plan-draft-invalid");
    }

    expect(await library.listShotPlans()).toEqual([]);
    expect(await library.listDirectorCards()).toEqual([]);
    expect(await library.listDeviceBriefs()).toEqual([]);
  });

  it("keeps malicious planner text out of every device brief", async () => {
    await seedConfirmedScript(library, NOW);
    const result = await createShootingPackage(
      shootingInput(),
      planningAgent(maliciousDraft()),
      library,
    );
    const serializedBriefs = JSON.stringify(result.deviceBriefs);

    for (const forbidden of [
      "A pendant begins again.",
      "segment_historical_01",
      "visitor-address-99",
      "full-script-echo",
    ]) {
      expect(serializedBriefs).not.toContain(forbidden);
    }
  });

  it("keeps trusted device capabilities when the planner mutates its copy", async () => {
    await seedConfirmedScript(library, NOW);
    const injected = "A pendant begins again. segment_historical_01 visitor-address-99";
    const agent: ShotPlanningAgent = {
      plan: async ({ deviceCapabilities }) => {
        for (const capability of deviceCapabilities) {
          capability.deviceId = injected;
          capability.carrier = injected;
        }
        return validDraft();
      },
    };

    const result = await createShootingPackage(shootingInput(), agent, library);
    const serializedBriefs = JSON.stringify(result.deviceBriefs);

    expect(result.deviceBriefs.map((brief) => brief.deviceId)).toEqual(expect.arrayContaining([
      "stone_a_sim_01",
      "stone_b_sim_01",
    ]));
    expect(result.deviceBriefs.map((brief) => brief.carrier)).toEqual(
      expect.arrayContaining(["pendant"]),
    );
    expect(serializedBriefs).not.toContain(injected);
  });

  it("rejects null and primitive planner steps without persisting tasks", async () => {
    await seedConfirmedScript(library, NOW);

    for (const step of [null, "not-a-step", 42]) {
      await expect(createShootingPackage(
        shootingInput(),
        planningAgent({ steps: [step] } as unknown as ShotPlanDraft),
        library,
      )).rejects.toThrow("shot-plan-draft-invalid");
    }

    expect(await library.listShotPlans()).toEqual([]);
    expect(await library.listDirectorCards()).toEqual([]);
    expect(await library.listDeviceBriefs()).toEqual([]);
  });

  it("rejects deferred planner output when the captured session becomes stale", async () => {
    await seedConfirmedScript(library, NOW);
    const entered = deferred<void>();
    const release = deferred<ShotPlanDraft>();
    const agent: ShotPlanningAgent = {
      plan: async () => {
        entered.resolve();
        return release.promise;
      },
    };

    const planning = createShootingPackage(shootingInput(), agent, library);
    await entered.promise;
    const current = (await library.listCreativeSessions())[0]!;
    await library.commitCreativeBundle({
      session: { ...current, currentShotPlanVersion: 9, updatedAt: "2026-09-05T08:01:00.000Z" },
      auditEvents: [sameStageAudit(current, "another plan pointer was written")],
    });
    release.resolve(validDraft());

    await expect(planning).rejects.toThrow("creative-session-precondition-failed");
    expect(await library.listShotPlans()).toEqual([]);
    expect(await library.listDirectorCards()).toEqual([]);
    expect(await library.listDeviceBriefs()).toEqual([]);
  });

  it("rejects deferred planner output when the current script changes", async () => {
    await seedConfirmedScript(library, NOW);
    const entered = deferred<void>();
    const release = deferred<ShotPlanDraft>();
    const agent: ShotPlanningAgent = {
      plan: async () => {
        entered.resolve();
        return release.promise;
      },
    };

    const planning = createShootingPackage(shootingInput(), agent, library);
    await entered.promise;
    const currentScript = (await library.listScriptPackages())[0]!;
    await library.putScriptPackage({ ...currentScript, status: "invalidated" });
    release.resolve(validDraft());

    await expect(planning).rejects.toThrow("script-precondition-failed");
    expect(await library.listShotPlans()).toEqual([]);
    expect(await library.listDirectorCards()).toEqual([]);
    expect(await library.listDeviceBriefs()).toEqual([]);
  });
});

function shootingInput(): {
  sessionId: string;
  deviceCapabilities: DeviceCapability[];
  actorId: string;
  now: string;
} {
  return {
    sessionId: SESSION_ID,
    deviceCapabilities: capabilities(),
    actorId: "director_01",
    now: NOW,
  };
}

function planningAgent(draft = validDraft()): ShotPlanningAgent {
  return { plan: async () => draft };
}

function validDraft(): ShotPlanDraft {
  return {
    steps: [
      {
        purpose: "Open the repair story",
        expressionSuggestion: "Show the maker returning the pendant to the bench.",
        visualDirection: "Wide bench shot, then hands in frame.",
        durationSeconds: 10,
        mustCapture: ["maker hands", "pendant"],
        optionalGapSuggestions: [{ suggestion: "Try a close-up if time allows", required: false }],
        completionCriteria: ["hands and pendant are both visible"],
        deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
        privacyReminder: "Do not capture customer faces.",
      },
      {
        purpose: "Show the renewed detail",
        expressionSuggestion: "Let the light pass across the finished pendant.",
        visualDirection: "Stable close-up of the pendant edge.",
        durationSeconds: 8,
        mustCapture: ["finished pendant detail"],
        optionalGapSuggestions: [],
        completionCriteria: ["the repaired edge is in focus"],
        deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
        privacyReminder: "Do not capture customer faces.",
      },
    ],
  };
}

function invalidStep(
  patch: Record<string, unknown>,
): ShotPlanDraft {
  const draft = validDraft();
  draft.steps[0] = { ...draft.steps[0]!, ...patch } as ShotPlanDraft["steps"][number];
  return draft;
}

function maliciousDraft(): ShotPlanDraft {
  const sensitiveEcho = "A pendant begins again. segment_historical_01 visitor-address-99 full-script-echo";
  const draft = validDraft();
  draft.steps = draft.steps.map((step) => ({
    ...step,
    purpose: sensitiveEcho,
    expressionSuggestion: sensitiveEcho,
    visualDirection: sensitiveEcho,
    mustCapture: [sensitiveEcho],
    optionalGapSuggestions: [{ suggestion: sensitiveEcho, required: false }],
    completionCriteria: [sensitiveEcho],
    privacyReminder: sensitiveEcho,
  }));
  return draft;
}

function capabilities(): DeviceCapability[] {
  return [
    { deviceId: "watch_sim_01", deviceRole: "watch-director", carrier: "watch" },
    { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", carrier: "pendant" },
    { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", carrier: "pendant" },
  ];
}

async function seedConfirmedScript(
  library: JsonContentStore,
  confirmedAt: string | null,
): Promise<void> {
  const session: CreativeSession = {
    id: SESSION_ID,
    customerId: "customer_01",
    title: "Pendant repair story",
    stage: "ready_to_dispatch",
    currentIntentVersion: 1,
    currentBriefVersion: 1,
    currentScriptVersion: 1,
    currentShotPlanVersion: 0,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: capabilities().map((capability) => ({
      deviceId: capability.deviceId,
      deviceRole: capability.deviceRole,
      transport: "simulated" as const,
    })),
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: ["customer faces"],
    status: "active",
    createdAt: "2026-09-05T07:50:00.000Z",
    updatedAt: "2026-09-05T07:55:00.000Z",
  };
  const script: ScriptPackage = {
    id: "script_01",
    sessionId: SESSION_ID,
    briefVersion: 1,
    scriptVersion: 1,
    status: "active",
    title: "Pendant repair story",
    platformDirection: "Xiaohongshu",
    opening: "A pendant begins again.",
    narrativeOrder: ["section_01"],
    sections: [{
      id: "section_01",
      expression: "Show the careful return to the bench.",
      source: "historical-material",
      referencedSegmentIds: ["segment_historical_01"],
    }],
    confirmedAt,
    createdAt: "2026-09-05T07:55:00.000Z",
  };
  await library.commitCreativeBundle({ session, scripts: [script], auditEvents: [] });
}

function sameStageAudit(session: CreativeSession, reason: string): AuditEvent {
  return {
    id: `audit_${reason}`,
    entityType: "creative-session",
    entityId: session.id,
    actorId: "director_01",
    action: "shooting-package-created",
    reason,
    before: { shotPlanVersion: session.currentShotPlanVersion },
    after: { shotPlanVersion: 9 },
    createdAt: "2026-09-05T08:01:00.000Z",
  };
}

type Deferred<T> = { promise: Promise<T>; resolve(value: T): void };

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
