import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { CreativeSession, DeviceBrief, DirectorCard, ScriptPackage, ShotPlan } from "@wearable/contracts";
import {
  consumeLocalSimulatorDeliveries,
  createLocalDispatchRoster,
  dispatchShootingPackage,
  JsonContentStore,
  LocalDispatchMailbox,
} from "@wearable/mobile-agent";
import { SimulatedCreativeDevice } from "@wearable/recorder-sim";
import { afterEach, describe, expect, it } from "vitest";

const SESSION_ID = "creative_bridge_01";
const NOW = "2026-09-05T08:00:00.000Z";
const VERSION = { sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1 };

describe("local dispatcher-to-simulator bridge", () => {
  let directory: string | undefined;

  afterEach(async () => {
    if (directory) await rm(directory, { recursive: true, force: true });
  });

  it("lets a simulator pull a dispatched task and emit a versioned action", async () => {
    directory = await mkdtemp(join(tmpdir(), "wearable-creative-bridge-"));
    const library = await JsonContentStore.open(join(directory, "library.json"));
    await seedShootingPackage(library);
    const mailboxes = {
      "watch-director": new LocalDispatchMailbox("watch_sim_01"),
      "main-stone-a": new LocalDispatchMailbox("stone_a_sim_01"),
      "main-stone-b": new LocalDispatchMailbox("stone_b_sim_01"),
    };
    const setup = createLocalDispatchRoster({ mailboxes, version: VERSION });

    await dispatchShootingPackage({
      sessionId: SESSION_ID,
      confirmStart: true,
      roster: setup.roster,
      actorId: "director_01",
      now: NOW,
    }, library);

    const watch = new SimulatedCreativeDevice("watch_sim_01", "watch-director");
    watch.setCurrentVersion(VERSION);
    await watch.syncLocalDeliveries(consumeLocalSimulatorDeliveries(
      setup.pulls["watch-director"],
    ));

    expect(watch.inbox).toEqual([expect.objectContaining({ taskId: "card_01" })]);
    expect(watch.emitAction({
      ...VERSION,
      stepId: "step_01",
      action: "voice-rewrite",
      actorId: "director_01",
      reason: null,
      transcript: "Use a calmer opening.",
      operationId: "voice_01",
      now: NOW,
    })).toMatchObject({ action: "voice-rewrite", transcript: "Use a calmer opening." });
  });
});

async function seedShootingPackage(library: JsonContentStore): Promise<void> {
  const session: CreativeSession = {
    id: SESSION_ID,
    customerId: "customer_01",
    title: "Local simulator bridge",
    stage: "ready_to_dispatch",
    currentIntentVersion: 1,
    currentBriefVersion: 1,
    currentScriptVersion: 1,
    currentShotPlanVersion: 1,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [
      { deviceId: "watch_sim_01", deviceRole: "watch-director", transport: "simulated" },
      { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", transport: "simulated" },
      { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", transport: "simulated" },
    ],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: ["customer faces"],
    status: "active",
    createdAt: NOW,
    updatedAt: NOW,
  };
  const script: ScriptPackage = {
    id: "script_01", sessionId: SESSION_ID, briefVersion: 1, scriptVersion: 1,
    status: "active", title: "Bridge", platformDirection: "Xiaohongshu",
    opening: "Open.", narrativeOrder: ["section_01"],
    sections: [{ id: "section_01", expression: "Show it.", source: "customer-input", referencedSegmentIds: [] }],
    confirmedAt: NOW, createdAt: NOW,
  };
  const plan: ShotPlan = {
    id: "plan_01", ...VERSION, status: "active", createdAt: NOW,
    steps: [{
      id: "step_01", order: 1, purpose: "Open", expressionSuggestion: "Speak.",
      visualDirection: "Show the bench.", durationSeconds: 8, mustCapture: ["maker hands"],
      optionalGapSuggestions: [], prohibitedCapture: ["customer faces"],
      completionCriteria: ["maker hands visible"],
      deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
    }],
  };
  const card: DirectorCard = {
    id: "card_01", ...VERSION, stepId: "step_01", status: "active", purpose: "Open",
    spokenPrompt: "Speak.", shotDescription: "Show the bench.", durationSeconds: 8,
    mustCapture: ["maker hands"], prohibitedCapture: ["customer faces"],
    availableActions: ["completed", "retake", "skipped", "voice-rewrite", "note", "privacy-paused"],
    createdAt: NOW,
  };
  const briefs: DeviceBrief[] = (["main-stone-a", "main-stone-b"] as const).map((deviceRole) => ({
    id: `brief_${deviceRole}`, ...VERSION, stepId: "step_01", status: "active",
    deviceId: deviceRole === "main-stone-a" ? "stone_a_sim_01" : "stone_b_sim_01",
    deviceRole, carrier: "pendant", startCondition: "director-card-start:1",
    endCondition: "director-card-stop:1", composition: "product-close-up",
    prohibitedAreas: ["customer faces"], privacyState: "creator", createdAt: NOW,
  }));
  await library.commitCreativeBundle({
    session,
    scripts: [script],
    shotPlans: [plan],
    directorCards: [card],
    deviceBriefs: briefs,
    auditEvents: [],
  });
}
