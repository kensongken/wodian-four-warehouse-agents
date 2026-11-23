import { randomUUID } from "node:crypto";

import {
  createCreativeFieldEvent,
  creativeFieldEventIdempotencyKey,
  isSameCreativeActionRequest,
  legacyCreativeFieldEventIdempotencyKey,
  resultingScriptVersionForCreativeFieldEvent,
  type CreativeFieldActionInput,
  type CreativeSession,
  type CreativeSessionEvent,
  type DeviceBrief,
  type DirectorCard,
} from "@wearable/contracts";

import type {
  ContentLibrary,
  CreativeActionCommitResult,
  CreativeBundleCommit,
} from "./content-library";
import {
  prepareScriptPackageRevision,
  type CreativeScriptAgent,
} from "./script-planner";

export type RecordCreativeSessionActionInput = Omit<CreativeFieldActionInput, "taskId">;

export async function recordCreativeSessionAction(
  input: RecordCreativeSessionActionInput,
  scriptAgent: CreativeScriptAgent,
  library: ContentLibrary,
): Promise<CreativeActionCommitResult> {
  const session = await requireSession(input.sessionId, library);
  const idempotencyKey = creativeFieldEventIdempotencyKey(input);
  const legacyIdempotencyKey = legacyCreativeFieldEventIdempotencyKey(input);
  const events = await library.listCreativeSessionEvents();
  const existing = events.find((event) => event.idempotencyKey === idempotencyKey) ??
    events.find((event) =>
      event.sessionId === input.sessionId && event.stepId === input.stepId &&
      event.idempotencyKey === legacyIdempotencyKey,
    );
  if (existing) {
    if (!isSameCreativeActionRequest(existing, { ...input, taskId: "idempotent-replay" })) {
      throw new Error("creative-event-operation-conflict");
    }
    return {
      event: existing,
      resultingScriptVersion: resultingScriptVersionForCreativeFieldEvent(existing),
    };
  }
  if (session.privacyMode === "paused" || session.stage === "paused") {
    throw new Error("privacy-paused");
  }
  // The first returned take moves the shared session to `ingesting`, but the
  // remaining director cards are still live until every planned step is
  // terminal. Keep field actions legal during that overlap.
  if (session.stage !== "shooting" && session.stage !== "ingesting") {
    throw new Error("creative-event-not-allowed");
  }
  if (
    session.currentBriefVersion !== input.briefVersion ||
    session.currentScriptVersion !== input.scriptVersion ||
    session.currentShotPlanVersion !== input.shotPlanVersion
  ) {
    throw new Error("creative-event-version-not-current");
  }
  const cards = (await library.listDirectorCards()).filter(matchesVersionAndStep(input));
  const briefs = (await library.listDeviceBriefs()).filter(matchesVersionAndStep(input));
  const task = requireActionTask(input, session, cards, briefs);
  const actionInput: CreativeFieldActionInput = { ...input, taskId: task.id };
  const card = cards.find((value) => value.status === "active");
  if (!card) throw new Error("creative-event-step-not-current");
  const currentTake = Math.max(
    1,
    ...events
      .filter((event) =>
        event.sessionId === input.sessionId && event.stepId === input.stepId &&
        event.briefVersion === input.briefVersion && event.scriptVersion === input.scriptVersion &&
        event.shotPlanVersion === input.shotPlanVersion,
      )
      .map((event) => event.action === "retake" ? (event.take ?? 1) + 1 : event.take ?? 1),
  );
  const event = createCreativeFieldEvent(actionInput, {
    availableActions: card.availableActions,
    currentTake,
    privacyPaused: false,
  });

  let bundle: CreativeBundleCommit;
  if (input.action === "voice-rewrite") {
    const prepared = await prepareScriptPackageRevision({
      sessionId: input.sessionId,
      prompt: input.transcript!,
      actorId: input.actorId,
      now: input.now,
    }, scriptAgent, library);
    bundle = {
      ...prepared.bundle,
      events: [event],
      returnActionResult: true,
    };
  } else if (input.action === "privacy-paused") {
    const paused: CreativeSession = {
      ...session,
      stage: "paused",
      privacyMode: "paused",
      updatedAt: input.now,
    };
    bundle = {
      session: paused,
      expectedSessionSnapshot: session,
      events: [event],
      returnActionResult: true,
      auditEvents: [privacyPauseAudit(session, input)],
    };
  } else {
    bundle = {
      session,
      expectedSessionSnapshot: session,
      events: [event],
      returnActionResult: true,
      auditEvents: [],
    };
  }
  const committed = await library.commitCreativeBundle(bundle);
  if (!committed) throw new Error("creative-action-result-required");
  return committed;
}

function requireActionTask(
  input: RecordCreativeSessionActionInput,
  session: CreativeSession,
  cards: DirectorCard[],
  briefs: DeviceBrief[],
): DirectorCard | DeviceBrief {
  if ((input.deviceId === null) !== (input.deviceRole === null)) {
    throw new Error("creative-event-device-identity-incomplete");
  }
  if (input.deviceId === null || input.deviceRole === null) {
    const card = cards[0];
    if (!card) throw new Error("creative-event-step-not-current");
    return card;
  }
  if (!session.deviceBindings.some((binding) =>
    binding.deviceId === input.deviceId && binding.deviceRole === input.deviceRole &&
    binding.transport === "simulated",
  )) {
    throw new Error("creative-event-device-binding-mismatch");
  }
  if (input.deviceRole === "watch-director") {
    const card = cards[0];
    if (!card) throw new Error("creative-event-step-not-current");
    return card;
  }
  const brief = briefs.find((value) =>
    value.deviceId === input.deviceId && value.deviceRole === input.deviceRole,
  );
  if (!brief) throw new Error("creative-event-device-task-mismatch");
  return brief;
}

function matchesVersionAndStep(input: RecordCreativeSessionActionInput) {
  return <T extends {
    sessionId: string;
    briefVersion: number;
    scriptVersion: number;
    shotPlanVersion: number;
    stepId: string;
  }>(value: T) => value.sessionId === input.sessionId &&
    value.briefVersion === input.briefVersion &&
    value.scriptVersion === input.scriptVersion &&
    value.shotPlanVersion === input.shotPlanVersion &&
    value.stepId === input.stepId;
}

async function requireSession(sessionId: string, library: ContentLibrary): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((value) => value.id === sessionId);
  if (!session) throw new Error("creative-session-not-found");
  if (session.status !== "active") throw new Error("creative-session-not-active");
  return session;
}

function privacyPauseAudit(
  session: CreativeSession,
  input: RecordCreativeSessionActionInput,
) {
  return {
    id: `audit_${randomUUID()}`,
    entityType: "creative-session" as const,
    entityId: session.id,
    actorId: input.actorId,
    action: "creative-session-transitioned",
    reason: input.reason ?? "customer requested privacy pause",
    before: { stage: session.stage },
    after: { stage: "paused" },
    createdAt: input.now,
  };
}
