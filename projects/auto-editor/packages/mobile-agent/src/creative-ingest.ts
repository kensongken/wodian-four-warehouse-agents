import type {
  AuditEvent,
  CaptureSession,
  CreativeSession,
  CreativeSessionEvent,
  DeviceRole,
  LibraryAsset,
  ShotPlan,
} from "@wearable/contracts";

import { prepareNewBatchAssets } from "./batch-ingest";
import {
  canonicalCreativeCaptureProvenanceKey,
  hasSameLibraryAssetIdentity,
  type CreativeCaptureProvenance,
} from "./asset-identity";
import type { ContentLibrary } from "./content-library";
import type { ImportedMedia } from "./glass3-import-adapter";

export type CreativeCaptureInput = Readonly<{
  creativeSessionId: string;
  stepId: string;
  deviceId: string;
  deviceRole: DeviceRole;
  carrier: string;
  take: number;
  stepOutcome: "completed" | "retake" | "skipped";
  captureSession: CaptureSession;
  batchId: string;
  media: readonly ImportedMedia[];
  mediaKinds: Readonly<Record<string, "video" | "image" | "audio">>;
  actorId: string;
  now: string;
}>;

/**
 * Returns verified, phone-local captures through the existing content library.
 * A retry keeps the same context and is idempotent; a new take gets its own
 * immutable asset provenance even when its bytes are identical.
 */
export async function ingestCreativeCapture(
  input: CreativeCaptureInput,
  library: ContentLibrary,
): Promise<LibraryAsset[]> {
  return ingestCreativeCaptureAttempt(input, library, 0);
}

/** Shared preflight used by the handoff boundary before it writes an original. */
export async function validateCreativeCaptureContext(
  input: Pick<CreativeCaptureInput,
    "creativeSessionId" | "stepId" | "deviceId" | "deviceRole" | "carrier" | "take" |
    "stepOutcome" | "captureSession" | "batchId" | "actorId">,
  library: ContentLibrary,
): Promise<Readonly<{ session: CreativeSession; plan: ShotPlan; carrier: string }>> {
  if (!input.creativeSessionId.trim() || !input.stepId.trim() || !input.deviceId.trim() ||
    !input.carrier.trim() || !input.batchId.trim() || !input.actorId.trim()) {
    throw new Error("creative-capture-context-required");
  }
  if (!Number.isInteger(input.take) || input.take < 1) throw new Error("creative-capture-take-required");
  const captureInput = input as CreativeCaptureInput;
  const session = await findCreativeSession(captureInput.creativeSessionId, library);
  assertCapturePrivacy(session, captureInput);
  assertCurrentDeviceBinding(session, captureInput);
  const plan = await findCurrentPlan(session, library);
  const carrier = await assertStepAssignment(session, plan, captureInput, library);
  await assertRecordedAction(session, captureInput, library);
  assertCaptureStage(session);
  return { session, plan, carrier };
}

async function ingestCreativeCaptureAttempt(
  input: CreativeCaptureInput,
  library: ContentLibrary,
  attempt: number,
): Promise<LibraryAsset[]> {
  assertInput(input);
  const session = await findCreativeSession(input.creativeSessionId, library);
  assertCapturePrivacy(session, input);
  assertCurrentDeviceBinding(session, input);
  const plan = await findCurrentPlan(session, library);
  const carrier = await assertStepAssignment(session, plan, input, library);
  await assertRecordedAction(session, input, library);
  const preparedAssets = input.stepOutcome === "skipped"
    ? []
    : await prepareNewBatchAssets({
      batchId: input.batchId,
      session: input.captureSession,
      media: input.media,
      mediaKinds: input.mediaKinds,
      captureContext: captureContextFor(session, input, carrier),
    });
  const existingAssets = await library.listAssets();
  const currentEvents = await library.listCreativeSessionEvents();
  const returnEvent = createReturnEvent(session, input, carrier);
  const hasRecordedReturn = currentEvents.some((event) =>
    event.idempotencyKey === returnEvent.idempotencyKey);
  const exactRetry = preparedAssets.map((asset) => existingAssets.find((item) => item.id === asset.id));
  if (preparedAssets.length > 0 && exactRetry.some((asset) => asset !== undefined)) {
    if (!exactRetry.every((asset, index) => asset !== undefined &&
      hasSameLibraryAssetIdentity(asset, preparedAssets[index]!))) {
      throw new Error("creative-capture-retry-identity-mismatch");
    }
    if (hasRecordedReturn && session.captureBatchIds.includes(input.batchId) &&
      session.stage === "editorial_review") return exactRetry as LibraryAsset[];
    if (hasRecordedReturn && session.captureBatchIds.includes(input.batchId) &&
      session.stage === "ingesting" &&
      !allStepsResolved(plan, session, currentEvents, existingAssets)) {
      return exactRetry as LibraryAsset[];
    }
  }
  if (input.stepOutcome === "skipped" && input.media.length === 0 && hasRecordedReturn) {
    if (session.stage === "editorial_review") return [];
    if (session.stage === "ingesting" &&
      !allStepsResolved(plan, session, currentEvents, existingAssets)) return [];
  }
  assertCaptureStage(session);

  if (input.stepOutcome === "skipped" && input.media.length > 0) {
    throw new Error("skipped-step-media-not-accepted");
  }

  const assets = preparedAssets;

  const events = hasRecordedReturn
    ? []
    : [returnEvent];
  const effectiveEvents = [...currentEvents, ...events];
  const allAssets = existingAssets;
  const stepsResolved = allStepsResolved(plan, session, effectiveEvents, [...allAssets, ...assets]);
  const targetStage = session.stage === "shooting" && (
    (assets.length > 0 && input.stepOutcome === "completed") || stepsResolved
  )
    ? "ingesting"
    : session.stage;
  const readyForReview = targetStage === "ingesting" && stepsResolved;
  const nextStage = session.stage === "shooting" && targetStage === "ingesting"
    ? "ingesting" as const
    : readyForReview ? "editorial_review" as const : targetStage;
  const next: CreativeSession = {
    ...session,
    stage: nextStage,
    captureBatchIds: assets.length > 0
      ? unique([...session.captureBatchIds, input.batchId])
      : session.captureBatchIds,
    editorialBatchId: nextStage === "editorial_review"
      ? (session.editorialBatchId ?? (
          [...allAssets, ...assets].some((asset) =>
            asset.captureContext?.creativeSessionId === session.id)
            ? input.batchId
            : `no-assets:${input.batchId}`
        ))
      : session.editorialBatchId,
    updatedAt: input.now,
  };
  const auditEvents = events.length === 0 && next.stage === session.stage &&
    JSON.stringify(next.captureBatchIds) === JSON.stringify(session.captureBatchIds) &&
    next.editorialBatchId === session.editorialBatchId
    ? []
    : [createSessionAudit(session, next, input, carrier)];

  try {
    await library.commitCreativeBundle({
      session: next,
      expectedSession: sessionPrecondition(session),
      expectedSessionSnapshot: session,
      assets,
      events,
      auditEvents,
    });
  } catch (error) {
    if (attempt < 2 && error instanceof Error && error.message === "creative-session-precondition-failed") {
      return ingestCreativeCaptureAttempt(input, library, attempt + 1);
    }
    throw error;
  }
  if (session.stage === "shooting" && readyForReview) {
    return ingestCreativeCaptureAttempt(input, library, attempt + 1);
  }
  return assets;
}

function assertInput(input: CreativeCaptureInput): void {
  if (!input.creativeSessionId.trim() || !input.stepId.trim() || !input.deviceId.trim() ||
    !input.carrier.trim() || !input.batchId.trim() || !input.actorId.trim()) {
    throw new Error("creative-capture-context-required");
  }
  if (!Number.isInteger(input.take) || input.take < 1) throw new Error("creative-capture-take-required");
  if (input.stepOutcome !== "skipped" && input.media.length === 0) {
    throw new Error("creative-capture-media-required");
  }
}

async function findCreativeSession(sessionId: string, library: ContentLibrary): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error(`creative-session-not-found:${sessionId}`);
  return session;
}

function assertCapturePrivacy(session: CreativeSession, input: CreativeCaptureInput): void {
  if (session.privacyMode === "paused" || input.captureSession.privacyState === "paused") {
    throw new Error("privacy-paused-session-not-ingestable");
  }
}

function assertCurrentDeviceBinding(session: CreativeSession, input: CreativeCaptureInput): void {
  if (!session.deviceBindings.some((binding) =>
    binding.deviceId === input.deviceId && binding.deviceRole === input.deviceRole)) {
    throw new Error("creative-capture-device-not-assigned");
  }
}

function assertCaptureStage(session: CreativeSession): void {
  if (session.stage !== "shooting" && session.stage !== "ingesting") {
    throw new Error("creative-capture-not-accepted");
  }
}

async function findCurrentPlan(session: CreativeSession, library: ContentLibrary): Promise<ShotPlan> {
  const plan = (await library.listShotPlans()).find((item) =>
    item.sessionId === session.id &&
    item.briefVersion === session.currentBriefVersion &&
    item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion &&
    item.status === "active");
  if (!plan) throw new Error("creative-capture-shot-plan-not-found");
  return plan;
}

async function assertStepAssignment(
  session: CreativeSession,
  plan: ShotPlan,
  input: CreativeCaptureInput,
  library: ContentLibrary,
): Promise<string> {
  const step = plan.steps.find((item) => item.id === input.stepId);
  if (!step || !step.deviceRoles.includes(input.deviceRole)) {
    throw new Error("creative-capture-step-not-assigned");
  }
  if (input.deviceRole === "watch-director") {
    const card = (await library.listDirectorCards()).find((item) =>
      item.sessionId === session.id && item.stepId === input.stepId &&
      item.briefVersion === session.currentBriefVersion &&
      item.scriptVersion === session.currentScriptVersion &&
      item.shotPlanVersion === session.currentShotPlanVersion && item.status === "active");
    if (!card) throw new Error("creative-capture-director-card-not-found");
    if (input.carrier !== "watch") throw new Error("creative-capture-carrier-mismatch");
    return "watch";
  }
  const briefs = await library.listDeviceBriefs();
  const brief = briefs.find((item) =>
    item.sessionId === session.id && item.stepId === input.stepId &&
    item.deviceId === input.deviceId && item.deviceRole === input.deviceRole &&
    item.briefVersion === session.currentBriefVersion &&
    item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion && item.status === "active");
  if (!brief) throw new Error("creative-capture-device-brief-not-found");
  if (brief.carrier !== input.carrier) throw new Error("creative-capture-carrier-mismatch");
  return brief.carrier;
}

async function assertRecordedAction(
  session: CreativeSession,
  input: CreativeCaptureInput,
  library: ContentLibrary,
): Promise<void> {
  const recorded = (await library.listCreativeSessionEvents()).some((event) =>
    event.sessionId === session.id &&
    event.briefVersion === session.currentBriefVersion &&
    event.scriptVersion === session.currentScriptVersion &&
    event.shotPlanVersion === session.currentShotPlanVersion &&
    event.stepId === input.stepId &&
    event.deviceId === input.deviceId &&
    event.deviceRole === input.deviceRole &&
    event.action === input.stepOutcome &&
    event.take === input.take);
  if (!recorded) throw new Error("creative-capture-action-not-recorded");
}

function createReturnEvent(
  session: CreativeSession,
  input: CreativeCaptureInput,
  carrier: string,
): CreativeSessionEvent {
  const action = input.stepOutcome === "skipped" ? "skipped" : "media-returned";
  const provenance = captureProvenance(session, input, carrier, action);
  const idempotencyKey = canonicalCreativeCaptureProvenanceKey(provenance);
  return {
    id: `creative_capture_event_${encodeURIComponent(idempotencyKey)}`,
    sessionId: session.id,
    briefVersion: session.currentBriefVersion,
    scriptVersion: session.currentScriptVersion,
    shotPlanVersion: session.currentShotPlanVersion,
    stepId: input.stepId,
    deviceId: input.deviceId,
    deviceRole: input.deviceRole,
    action,
    actorId: input.actorId,
    reason: input.stepOutcome === "skipped" ? "step-skipped-without-media" : null,
    idempotencyKey,
    take: input.take,
    createdAt: input.now,
  };
}

function allStepsResolved(
  plan: ShotPlan,
  session: CreativeSession,
  events: CreativeSessionEvent[],
  assets: LibraryAsset[],
): boolean {
  return plan.steps.every((step) => {
    const matchingEvents = events.filter((event) =>
      event.sessionId === session.id && event.stepId === step.id &&
      event.briefVersion === session.currentBriefVersion &&
      event.scriptVersion === session.currentScriptVersion &&
      event.shotPlanVersion === session.currentShotPlanVersion);
    const returned = assets.some((asset) =>
        asset.captureContext?.stepOutcome === "completed" &&
        asset.captureContext.creativeSessionId === session.id &&
        asset.captureContext.stepId === step.id &&
        asset.captureContext.scriptVersion === session.currentScriptVersion &&
        asset.captureContext.shotPlanVersion === session.currentShotPlanVersion);
    const skipped = matchingEvents.some((event) => event.action === "skipped");
    const unresolved = matchingEvents.some((event) =>
      (event.action === "note" || event.action === "disconnected") &&
      event.reason?.trim().toLowerCase() === "unresolved");
    return returned || skipped || unresolved;
  });
}

function createSessionAudit(
  before: CreativeSession,
  after: CreativeSession,
  input: CreativeCaptureInput,
  carrier: string,
): AuditEvent {
  const transitioned = before.stage !== after.stage;
  const provenance = captureProvenance(before, input, carrier,
    input.stepOutcome === "skipped" ? "skipped" : "media-returned");
  const allSkipped = after.stage === "editorial_review" &&
    after.editorialBatchId?.startsWith("no-assets:");
  return {
    id: `audit_creative_capture_${encodeURIComponent(canonicalCreativeCaptureProvenanceKey(provenance))}_${before.stage}_${after.stage}`,
    entityType: "creative-session",
    entityId: before.id,
    actorId: input.actorId,
    action: transitioned ? "creative-session-transitioned" : "creative-capture-returned",
    reason: allSkipped
      ? "all planned steps skipped; editorial handoff has no captured assets"
      : transitioned ? "creative-capture-returned" : "creative capture returned",
    before: transitioned ? { stage: before.stage } : { captureBatchIds: before.captureBatchIds },
    after: transitioned ? { stage: after.stage } : { captureBatchIds: after.captureBatchIds },
    createdAt: input.now,
  };
}

function captureProvenance(
  session: CreativeSession,
  input: CreativeCaptureInput,
  carrier: string,
  action: string,
): CreativeCaptureProvenance {
  return {
    creativeSessionId: session.id,
    briefVersion: session.currentBriefVersion,
    scriptVersion: session.currentScriptVersion,
    shotPlanVersion: session.currentShotPlanVersion,
    stepId: input.stepId,
    deviceId: input.deviceId,
    deviceRole: input.deviceRole,
    carrier,
    action,
    take: input.take,
    batchId: input.batchId,
  };
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

function captureContextFor(
  session: CreativeSession,
  input: CreativeCaptureInput,
  carrier: string,
): NonNullable<LibraryAsset["captureContext"]> {
  return {
    creativeSessionId: input.creativeSessionId,
    stepId: input.stepId,
    deviceId: input.deviceId,
    deviceRole: input.deviceRole,
    carrier,
    scriptVersion: session.currentScriptVersion,
    shotPlanVersion: session.currentShotPlanVersion,
    take: input.take,
    stepOutcome: input.stepOutcome,
  };
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
