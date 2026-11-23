import { randomUUID } from "node:crypto";

import type {
  AuditEvent,
  CreativeSession,
  DeviceBrief,
  DeviceRole,
  DirectorCard,
  ScriptPackage,
  ShotPlan,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";

const requiredDeviceRoles = ["watch-director", "main-stone-a", "main-stone-b"] as const;
const recordableDeviceRoles = ["main-stone-a", "main-stone-b"] as const;

export type DeviceCapability = {
  deviceId: string;
  deviceRole: DeviceRole;
  carrier: string;
};

export type ShotPlanDraft = {
  steps: Array<{
    purpose: string;
    expressionSuggestion: string;
    visualDirection: string;
    durationSeconds: number;
    mustCapture: string[];
    optionalGapSuggestions: Array<{ suggestion: string; required: false }>;
    completionCriteria: string[];
    deviceRoles: DeviceRole[];
    privacyReminder: string;
  }>;
};

export interface ShotPlanningAgent {
  plan(input: {
    script: ScriptPackage;
    deviceCapabilities: DeviceCapability[];
  }): Promise<ShotPlanDraft>;
}

export async function createShootingPackage(
  input: {
    sessionId: string;
    deviceCapabilities: DeviceCapability[];
    actorId: string;
    now: string;
  },
  agent: ShotPlanningAgent,
  library: ContentLibrary,
): Promise<{
  shotPlan: ShotPlan;
  directorCards: DirectorCard[];
  deviceBriefs: DeviceBrief[];
}> {
  requireActor(input.actorId);
  const session = await requireSession(input.sessionId, library);
  const script = await requireCurrentScript(session, library);
  if (script.confirmedAt === null) throw new Error("confirmed-script-required");
  if (session.stage !== "ready_to_dispatch") throw new Error("shooting-package-not-allowed");
  if (session.currentShotPlanVersion !== 0) throw new Error("shooting-package-already-created");
  const trustedCapabilities = requireCapabilities(input.deviceCapabilities, session);
  const draft = await agent.plan({
    script,
    deviceCapabilities: cloneDeviceCapabilities(trustedCapabilities),
  });
  validateDraft(draft, trustedCapabilities);

  const shotPlanVersion = session.currentShotPlanVersion + 1;
  const versionKey = {
    sessionId: session.id,
    briefVersion: script.briefVersion,
    scriptVersion: script.scriptVersion,
    shotPlanVersion,
  };
  const steps = draft.steps.map((step, index) => ({
    id: `shot_step_${shotPlanVersion}_${index + 1}_${randomUUID()}`,
    order: index + 1,
    purpose: step.purpose.trim(),
    expressionSuggestion: step.expressionSuggestion.trim(),
    visualDirection: step.visualDirection.trim(),
    durationSeconds: step.durationSeconds,
    mustCapture: normalizedStrings(step.mustCapture),
    optionalGapSuggestions: step.optionalGapSuggestions.map((gap) => ({
      suggestion: gap.suggestion.trim(),
      required: false as const,
    })),
    prohibitedCapture: normalizedStrings([...session.prohibitedAreas, step.privacyReminder]),
    completionCriteria: normalizedStrings(step.completionCriteria),
    deviceRoles: [...step.deviceRoles],
  }));
  const shotPlan: ShotPlan = {
    id: `shot_plan_${randomUUID()}`,
    ...versionKey,
    status: "active",
    steps,
    createdAt: input.now,
  };
  const directorCards: DirectorCard[] = steps.map((step) => ({
    id: `director_card_${randomUUID()}`,
    ...versionKey,
    stepId: step.id,
    status: "active",
    purpose: step.purpose,
    spokenPrompt: step.expressionSuggestion,
    shotDescription: step.visualDirection,
    durationSeconds: step.durationSeconds,
    mustCapture: step.mustCapture,
    prohibitedCapture: step.prohibitedCapture,
    availableActions: ["started", "completed", "retake", "skipped", "voice-rewrite", "note", "privacy-paused"],
    createdAt: input.now,
  }));
  const deviceBriefs: DeviceBrief[] = steps.flatMap((step) => step.deviceRoles
    .filter((role): role is (typeof recordableDeviceRoles)[number] =>
      recordableDeviceRoles.includes(role as (typeof recordableDeviceRoles)[number]),
    )
    .map((role) => {
      const capability = trustedCapabilities.find((item) => item.deviceRole === role)!;
      return {
        id: `device_brief_${randomUUID()}`,
        ...versionKey,
        stepId: step.id,
        status: "active" as const,
        deviceId: capability.deviceId,
        deviceRole: role,
        carrier: capability.carrier,
        startCondition: `director-card-start:${step.order}`,
        endCondition: `director-card-stop:${step.order}`,
        composition: role === "main-stone-a" ? "product-close-up" : "scene-context",
        prohibitedAreas: [...session.prohibitedAreas],
        privacyState: session.privacyMode,
        createdAt: input.now,
      };
    }));
  const next: CreativeSession = {
    ...session,
    currentShotPlanVersion: shotPlanVersion,
    updatedAt: input.now,
  };

  await library.commitCreativeBundle({
    session: next,
    expectedSession: sessionPrecondition(session),
    expectedScript: scriptPrecondition(script),
    shotPlans: [shotPlan],
    directorCards,
    deviceBriefs,
    auditEvents: [sessionMutationAudit({
      sessionId: session.id,
      actorId: input.actorId,
      action: "shooting-package-created",
      reason: "created local A-0 simulated shooting tasks from confirmed script",
      before: { shotPlanVersion: session.currentShotPlanVersion },
      after: { shotPlanVersion },
      now: input.now,
    })],
  });

  return { shotPlan, directorCards, deviceBriefs };
}

function validateDraft(draft: unknown, capabilities: DeviceCapability[]): asserts draft is ShotPlanDraft {
  if (typeof draft !== "object" || draft === null || !Array.isArray((draft as ShotPlanDraft).steps) ||
    (draft as ShotPlanDraft).steps.length === 0) {
    throw new Error("shot-plan-draft-invalid");
  }
  const addressedRoles = new Set<DeviceRole>();
  for (const step of (draft as ShotPlanDraft).steps) {
    if (typeof step !== "object" || step === null || Array.isArray(step) ||
      !nonEmpty(step.purpose) || !nonEmpty(step.expressionSuggestion) || !nonEmpty(step.visualDirection) ||
      !Number.isInteger(step.durationSeconds) || step.durationSeconds <= 0 ||
      !nonEmptyStrings(step.mustCapture) || !nonEmptyStrings(step.completionCriteria) ||
      !nonEmpty(step.privacyReminder) || !Array.isArray(step.deviceRoles) || step.deviceRoles.length === 0 ||
      !step.deviceRoles.includes("watch-director") ||
      !step.deviceRoles.some((role) => recordableDeviceRoles.includes(role as (typeof recordableDeviceRoles)[number])) ||
      !Array.isArray(step.optionalGapSuggestions) ||
      !step.optionalGapSuggestions.every((gap) => typeof gap === "object" && gap !== null &&
        nonEmpty(gap.suggestion) && gap.required === false)) {
      throw new Error("shot-plan-draft-invalid");
    }
    for (const role of step.deviceRoles) {
      if (!requiredDeviceRoles.includes(role as (typeof requiredDeviceRoles)[number]) ||
        !capabilities.some((capability) => capability.deviceRole === role)) {
        throw new Error("shot-plan-draft-invalid");
      }
      addressedRoles.add(role);
    }
  }
  if (requiredDeviceRoles.some((role) => !addressedRoles.has(role))) {
    throw new Error("shot-plan-draft-invalid");
  }
}

function requireCapabilities(
  capabilities: DeviceCapability[],
  session: CreativeSession,
): DeviceCapability[] {
  if (!Array.isArray(capabilities) || capabilities.length === 0 ||
    capabilities.some((capability) => !capability || !nonEmpty(capability.deviceId) ||
      !nonEmpty(capability.carrier) || !requiredDeviceRoles.includes(
        capability.deviceRole as (typeof requiredDeviceRoles)[number],
      )) ||
    new Set(capabilities.map((capability) => capability.deviceRole)).size !== capabilities.length ||
    requiredDeviceRoles.some((role) => !capabilities.some((capability) => capability.deviceRole === role)) ||
    capabilities.some((capability) => !session.deviceBindings.some((binding) =>
      binding.deviceId === capability.deviceId && binding.deviceRole === capability.deviceRole &&
        binding.transport === "simulated",
    ))) {
    throw new Error("simulated-device-capabilities-required");
  }
  return capabilities.map((capability) => ({ ...capability }));
}

function cloneDeviceCapabilities(capabilities: DeviceCapability[]): DeviceCapability[] {
  return capabilities.map((capability) => ({ ...capability }));
}

async function requireSession(sessionId: string, library: ContentLibrary): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error("creative-session-not-found");
  if (session.status !== "active") throw new Error("creative-session-not-active");
  return session;
}

async function requireCurrentScript(
  session: CreativeSession,
  library: ContentLibrary,
): Promise<ScriptPackage> {
  const script = (await library.listScriptPackages()).find((item) =>
    item.sessionId === session.id && item.briefVersion === session.currentBriefVersion &&
      item.scriptVersion === session.currentScriptVersion,
  );
  if (!script) throw new Error("script-not-found");
  if (script.status !== "active") throw new Error("script-not-active");
  return script;
}

function sessionPrecondition(session: CreativeSession) {
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

function sessionMutationAudit(input: {
  sessionId: string;
  actorId: string;
  action: string;
  reason: string;
  before: unknown;
  after: unknown;
  now: string;
}): AuditEvent {
  return {
    id: `audit_${randomUUID()}`,
    entityType: "creative-session",
    entityId: input.sessionId,
    actorId: input.actorId,
    action: input.action,
    reason: input.reason,
    before: input.before,
    after: input.after,
    createdAt: input.now,
  };
}

function requireActor(actorId: string): void {
  if (!nonEmpty(actorId)) throw new Error("shooting-package-actor-required");
}

function normalizedStrings(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()))];
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function nonEmptyStrings(values: unknown): values is string[] {
  return Array.isArray(values) && values.length > 0 && values.every(nonEmpty);
}
