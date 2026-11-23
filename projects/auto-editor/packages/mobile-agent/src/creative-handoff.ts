import { createHash, randomUUID } from "node:crypto";

import type {
  AuditEvent,
  CaptureSession,
  CreativeHandoffStatus,
  CreativeProcessingTask,
  CreativeSession,
  CreativeSessionEvent,
  DeviceRole,
  LibraryAsset,
  ShotPlan,
} from "@wearable/contracts";

export { processCreativeEditorial, type CreativeEditorialDependencies } from "./creative-editorial";

import { prepareNewBatchAssets } from "./batch-ingest";
import type { ContentLibrary } from "./content-library";
import { validateCreativeCaptureContext } from "./creative-ingest";
import type { ImportedMedia } from "./glass3-import-adapter";

export type CreativeStepResolution = Readonly<{
  stepId: string;
  state: "pending" | "retake-pending" | "completed-returned" | "skipped";
  acceptedTake: number | null;
  returnableTake?: number;
}>;

export interface CreativePrivacyDevice {
  readonly deviceId: string;
  pausePrivacy(): void;
  resumePrivacy(): void;
  isPrivacyPaused(): boolean;
}

const privacyResumeQueues = new Map<string, Promise<void>>();

export async function resumeCreativePrivacy(
  input: Readonly<{
    creativeSessionId: string;
    actorId: string;
    actorRole: "customer";
    confirmSafe: true;
    operationKey: string;
    now: string;
  }>,
  dependencies: Readonly<{
    library: ContentLibrary;
    devices: readonly CreativePrivacyDevice[];
  }>,
): Promise<CreativeSession> {
  return serializePrivacyResume(input.creativeSessionId, () =>
    resumeCreativePrivacySerialized(input, dependencies));
}

async function resumeCreativePrivacySerialized(
  input: Readonly<{
    creativeSessionId: string;
    actorId: string;
    actorRole: "customer";
    confirmSafe: true;
    operationKey: string;
    now: string;
  }>,
  dependencies: Readonly<{
    library: ContentLibrary;
    devices: readonly CreativePrivacyDevice[];
  }>,
): Promise<CreativeSession> {
  if (input.actorRole !== "customer") throw new Error("privacy-resume-customer-required");
  if (input.confirmSafe !== true) throw new Error("privacy-resume-safety-confirmation-required");
  if (!input.operationKey.trim()) throw new Error("privacy-resume-operation-required");
  const session = (await dependencies.library.listCreativeSessions()).find(
    (item) => item.id === input.creativeSessionId,
  );
  if (!session) throw new Error(`creative-session-not-found:${input.creativeSessionId}`);
  if (input.actorId !== session.customerId) throw new Error("privacy-resume-customer-mismatch");
  const auditId = `audit_creative_privacy_resume_${createHash("sha256")
    .update(`${session.id}:${input.operationKey}`).digest("hex").slice(0, 24)}`;
  const audits = await dependencies.library.listAuditEvents(session.id);
  if (audits.some((audit) => audit.id === auditId && audit.action === "creative-session-transitioned")) {
    return session;
  }
  if (session.stage !== "paused" || session.privacyMode !== "paused") {
    throw new Error("privacy-resume-not-paused");
  }
  const pauseAudit = [...audits].reverse().find((audit) =>
    audit.action === "creative-session-transitioned" &&
    (audit.after as { stage?: string } | null)?.stage === "paused",
  );
  const resumeStage = (pauseAudit?.before as { stage?: string } | null)?.stage;
  if (resumeStage !== "shooting" && resumeStage !== "ingesting") {
    throw new Error("privacy-resume-origin-invalid");
  }
  const devices = resolveBoundPrivacyDevices(session, dependencies.devices);
  const resumed: CreativePrivacyDevice[] = [];
  const failedDeviceIds: string[] = [];
  for (const device of devices) {
    try {
      device.resumePrivacy();
      if (device.isPrivacyPaused()) throw new Error("device-remains-privacy-paused");
      resumed.push(device);
    } catch {
      failedDeviceIds.push(device.deviceId);
    }
  }
  if (failedDeviceIds.length > 0) {
    const compensationFailedDeviceIds: string[] = [];
    for (const device of devices) {
      try { device.pausePrivacy(); }
      catch { compensationFailedDeviceIds.push(device.deviceId); }
    }
    await dependencies.library.commitCreativeBundle({
      session,
      expectedSessionSnapshot: session,
      auditEvents: [{
        id: `audit_creative_privacy_resume_failed_${randomUUID()}`,
        entityType: "creative-session",
        entityId: session.id,
        actorId: input.actorId,
        action: "creative-privacy-resume-failed",
        reason: "one or more bound simulated devices could not resume",
        before: { stage: "paused" },
        after: { stage: "paused", failedDeviceIds, compensationFailedDeviceIds },
        createdAt: input.now,
      }],
    });
    throw new Error("creative-privacy-device-resume-failed");
  }
  const next: CreativeSession = {
    ...session,
    stage: resumeStage,
    privacyMode: "creator",
    updatedAt: input.now,
  };
  try {
    await dependencies.library.commitCreativeBundle({
      session: next,
      expectedSessionSnapshot: session,
      auditEvents: [{
        id: auditId,
        entityType: "creative-session",
        entityId: session.id,
        actorId: input.actorId,
        action: "creative-session-transitioned",
        reason: "customer confirmed safety and every bound simulated device resumed",
        before: { stage: "paused" },
        after: { stage: resumeStage, resumedDeviceIds: resumed.map((device) => device.deviceId) },
        createdAt: input.now,
      }],
    });
  } catch (error) {
    const persisted = await dependencies.library.listCreativeSessions()
      .then((sessions) => sessions.find((item) => item.id === session.id))
      .catch(() => undefined);
    if (persisted?.stage === resumeStage && persisted.privacyMode === "creator") {
      return persisted;
    }
    const compensationFailedDeviceIds = pauseDevicesBestEffort(resumed);
    if (compensationFailedDeviceIds.length > 0) {
      await dependencies.library.commitCreativeBundle({
        session,
        expectedSessionSnapshot: session,
        auditEvents: [{
          id: `audit_creative_privacy_resume_compensation_failed_${randomUUID()}`,
          entityType: "creative-session",
          entityId: session.id,
          actorId: input.actorId,
          action: "creative-privacy-resume-compensation-failed",
          reason: "session commit failed and one or more simulated devices could not be paused again",
          before: { stage: "paused" },
          after: { stage: "paused", failedDeviceIds: compensationFailedDeviceIds },
          createdAt: input.now,
        }],
      }).catch(() => undefined);
      throw new Error("creative-privacy-compensation-failed", { cause: error });
    }
    throw error;
  }
  return next;
}

function pauseDevicesBestEffort(devices: readonly CreativePrivacyDevice[]): string[] {
  const failed: string[] = [];
  for (const device of devices) {
    try { device.pausePrivacy(); }
    catch { failed.push(device.deviceId); }
  }
  return failed;
}

async function serializePrivacyResume<T>(
  sessionId: string,
  execute: () => Promise<T>,
): Promise<T> {
  const prior = privacyResumeQueues.get(sessionId) ?? Promise.resolve();
  const result = prior.catch(() => undefined).then(execute);
  const settled = result.then(() => undefined, () => undefined);
  privacyResumeQueues.set(sessionId, settled);
  try {
    return await result;
  } finally {
    if (privacyResumeQueues.get(sessionId) === settled) privacyResumeQueues.delete(sessionId);
  }
}

function resolveBoundPrivacyDevices(
  session: CreativeSession,
  devices: readonly CreativePrivacyDevice[],
): CreativePrivacyDevice[] {
  const byId = new Map<string, CreativePrivacyDevice>();
  for (const device of devices) {
    if (!device.deviceId.trim() || byId.has(device.deviceId)) {
      throw new Error("creative-privacy-device-roster-invalid");
    }
    byId.set(device.deviceId, device);
  }
  const bound = session.deviceBindings.map((binding) => byId.get(binding.deviceId));
  if (bound.some((device) => device === undefined)) {
    throw new Error("creative-privacy-device-roster-invalid");
  }
  return bound as CreativePrivacyDevice[];
}

export type CreativeCaptureUpload = Readonly<{
  bytes: Buffer;
  kind: "video" | "image" | "audio";
  extension: ".mp4" | ".png" | ".jpg" | ".m4a";
  deviceModel: string;
}>;

export type CreativeCaptureCommand = Readonly<{
  creativeSessionId: string;
  stepId: string;
  deviceId: string;
  deviceRole: DeviceRole;
  carrier: string;
  take: number;
  stepOutcome: "completed" | "retake" | "skipped";
  captureSession: CaptureSession;
  batchId: string;
  uploads: readonly CreativeCaptureUpload[];
  actorId: string;
  operationKey: string;
  now: string;
}>;

export interface CreativeOriginalStore {
  prepare(input: Readonly<{
    identity: string;
    operationKey: string;
    uploads: readonly CreativeCaptureUpload[];
  }>): Promise<Readonly<{
    importedMedia: readonly ImportedMedia[];
    commit(): Promise<void>;
    rollback(): Promise<void>;
  }>>;
}

export async function acceptCreativeCapture(
  command: CreativeCaptureCommand,
  dependencies: Readonly<{ library: ContentLibrary; originals: CreativeOriginalStore }>,
): Promise<Readonly<{
  status: CreativeHandoffStatus;
  assets: LibraryAsset[];
  steps: CreativeStepResolution[];
  editorialBatchId: string | null;
}>> {
  assertUploads(command);
  const { session, plan, carrier } = await validateCreativeCaptureContext(command, dependencies.library);
  const existingAssets = await dependencies.library.listAssets();
  const existingEvents = await dependencies.library.listCreativeSessionEvents();
  if (command.stepOutcome === "retake") {
    const result = resultFor(session, plan, existingEvents, existingAssets, command.batchId);
    const next = nextSession(session, command.batchId, result.status, false, command.now);
    await dependencies.library.commitCreativeCaptureBundle({
      session: next,
      expectedSession: sessionPrecondition(session),
      assets: [], events: [],
      processingTask: processingTask(next, command.batchId, result.status, command.now),
      auditEvents: sessionAudit(session, next, command, result.status),
    });
    return { ...result, editorialBatchId: next.editorialBatchId };
  }
  const replayEvent = returnEvent(session, command);
  const replayAssets = existingAssets.filter((asset) => {
      const context = asset.captureContext;
      return context?.creativeSessionId === session.id && context.stepId === command.stepId &&
        context.deviceId === command.deviceId && context.take === command.take &&
        context.stepOutcome === "completed";
    });
  if (existingEvents.some((item) => item.idempotencyKey === replayEvent.idempotencyKey) &&
    (command.stepOutcome === "skipped" || replayAssets.length > 0)) {
    return { ...resultFor(session, plan, existingEvents, existingAssets, command.batchId), assets: replayAssets };
  }
  if (command.stepOutcome === "completed") {
    const current = resolveOneStep(plan, command.stepId, existingEvents, existingAssets);
    if (current.state !== "pending" || current.returnableTake !== command.take) {
      throw new Error("creative-capture-return-not-current");
    }
  }
  assertNoAcceptedCaptureCollision(session, command, existingAssets);
  const identity = [command.creativeSessionId, command.stepId, command.deviceId, String(command.take)]
    .map(encodeURIComponent).join("--");
  const prepared = await dependencies.originals.prepare({ identity, operationKey: command.operationKey, uploads: command.uploads });
  try {
    await prepared.commit();
    const assets = command.stepOutcome === "skipped" ? [] : await prepareNewBatchAssets({
      batchId: command.batchId,
      session: command.captureSession,
      media: prepared.importedMedia,
      mediaKinds: Object.fromEntries(command.uploads.map((upload, index) => [
        prepared.importedMedia[index]!.id, upload.kind,
      ])),
      captureContext: {
        creativeSessionId: session.id, stepId: command.stepId, deviceId: command.deviceId,
        deviceRole: command.deviceRole, carrier, scriptVersion: session.currentScriptVersion,
        shotPlanVersion: session.currentShotPlanVersion, take: command.take,
        stepOutcome: command.stepOutcome,
      },
    });
    const event = replayEvent;
    const events = existingEvents.some((item) => item.idempotencyKey === event.idempotencyKey) ? [] : [event];
    const steps = resolveCreativeSteps(plan, [...existingEvents, ...events], [...existingAssets, ...assets]);
    const status = statusFor(steps, [...existingAssets, ...assets], session.id);
    const next = nextSession(session, command.batchId, status, assets.length > 0, command.now);
    await dependencies.library.commitCreativeCaptureBundle({
      session: next,
      expectedSession: sessionPrecondition(session),
      assets,
      events,
      processingTask: processingTask(next, command.batchId, status, command.now),
      auditEvents: sessionAudit(session, next, command, status),
    });
    return { status, assets, steps, editorialBatchId: next.editorialBatchId };
  } catch (error) {
    // A persisted bundle owns its original. On uncertainty we retain the file
    // for safe replay rather than risk deleting evidence already in the ledger.
    const referenced = await dependencies.library.listAssets().then((stored) =>
      prepared.importedMedia.some((media) => stored.some((asset) => asset.localPath === media.localPath)),
    ).catch(() => true);
    if (!referenced) await prepared.rollback();
    throw error;
  }
}

export async function settleCreativeCapture(
  input: Readonly<{ creativeSessionId: string; actorId: string; operationKey: string; now: string }>,
  library: ContentLibrary,
): Promise<Readonly<{
  status: "collecting" | "ready-for-editorial" | "no-assets";
  steps: CreativeStepResolution[];
  editorialBatchId: string | null;
}>> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === input.creativeSessionId);
  if (!session) throw new Error(`creative-session-not-found:${input.creativeSessionId}`);
  const plan = (await library.listShotPlans()).find((item) => item.sessionId === session.id &&
    item.briefVersion === session.currentBriefVersion && item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion && item.status === "active");
  if (!plan) throw new Error("creative-capture-shot-plan-not-found");
  const assets = await library.listAssets();
  const steps = resolveCreativeSteps(plan, await library.listCreativeSessionEvents(), assets);
  const status = statusFor(steps, assets, session.id) as "collecting" | "ready-for-editorial" | "no-assets";
  if (status === "collecting") return { status, steps, editorialBatchId: session.editorialBatchId };
  if (session.stage === "shooting") {
    const ingesting: CreativeSession = { ...session, stage: "ingesting", updatedAt: input.now };
    await library.commitCreativeCaptureBundle({
      session: ingesting,
      expectedSession: sessionPrecondition(session),
      assets: [], events: [], processingTask: processingTask(ingesting,
        session.editorialBatchId ?? `creative_batch_${session.id}`, status, input.now),
      auditEvents: sessionAudit(session, ingesting, input, status),
    });
    return settleCreativeCapture(input, library);
  }
  const batchId = session.editorialBatchId ?? `creative_batch_${session.id}`;
  const next: CreativeSession = { ...session, stage: "editorial_review", editorialBatchId: batchId, updatedAt: input.now };
  await library.commitCreativeCaptureBundle({
    session: next,
    expectedSession: sessionPrecondition(session),
    assets: [], events: [], processingTask: processingTask(next, batchId, status, input.now),
    auditEvents: sessionAudit(session, next, input, status),
  });
  return { status, steps, editorialBatchId: batchId };
}

export function resolveCreativeSteps(
  plan: ShotPlan,
  events: readonly CreativeSessionEvent[],
  assets: readonly LibraryAsset[],
): CreativeStepResolution[] {
  return plan.steps.map((step) => resolveOneStep(plan, step.id, events, assets));
}

function resolveOneStep(plan: ShotPlan, stepId: string, events: readonly CreativeSessionEvent[], assets: readonly LibraryAsset[]): CreativeStepResolution {
  const relevant = events.filter((event) => event.sessionId === plan.sessionId && event.stepId === stepId &&
    event.briefVersion === plan.briefVersion && event.scriptVersion === plan.scriptVersion &&
    event.shotPlanVersion === plan.shotPlanVersion);
  // Only a director action can choose the current take. A late transport return from an
  // older take must never override a newer retake/completed/skipped instruction.
  let directiveIndex = -1;
  for (let index = relevant.length - 1; index >= 0; index -= 1) {
    const action = relevant[index]!.action;
    if (action === "retake" || action === "skipped" || action === "completed") {
      directiveIndex = index;
      break;
    }
  }
  const latest = directiveIndex === -1 ? undefined : relevant[directiveIndex];
  if (!latest) return { stepId, state: "pending", acceptedTake: null };
  if (latest.action === "skipped") return { stepId, state: "skipped", acceptedTake: null };
  if (latest.action === "retake") return { stepId, state: "retake-pending", acceptedTake: null };
  if (latest.action === "completed" && latest.take) {
    const returned = relevant.slice(directiveIndex + 1).some((event) =>
      event.action === "media-returned" && event.deviceId === latest.deviceId &&
      event.deviceRole === latest.deviceRole && event.take === latest.take);
    if (returned && latest.deviceId && latest.deviceRole && assets.some((asset) => {
      const context = asset.captureContext;
      return context?.creativeSessionId === plan.sessionId && context.stepId === stepId &&
        context.deviceId === latest.deviceId && context.deviceRole === latest.deviceRole &&
        context.take === latest.take && context.stepOutcome === "completed" &&
        context.scriptVersion === plan.scriptVersion && context.shotPlanVersion === plan.shotPlanVersion;
    })) return { stepId, state: "completed-returned", acceptedTake: latest.take };
    return { stepId, state: "pending", acceptedTake: null, returnableTake: latest.take };
  }
  return { stepId, state: "pending", acceptedTake: null };
}

function assertUploads(command: CreativeCaptureCommand): void {
  if (!command.operationKey.trim()) throw new Error("creative-capture-operation-required");
  if (command.stepOutcome === "completed" && command.uploads.length === 0) throw new Error("creative-capture-media-required");
  if (command.stepOutcome === "skipped" && command.uploads.length > 0) throw new Error("skipped-step-media-not-accepted");
}

function assertNoAcceptedCaptureCollision(
  session: CreativeSession,
  command: CreativeCaptureCommand,
  assets: readonly LibraryAsset[],
): void {
  if (command.stepOutcome !== "completed") return;
  if (assets.some((asset) => {
    const context = asset.captureContext;
    return context?.creativeSessionId === session.id && context.stepId === command.stepId &&
      context.take === command.take && context.scriptVersion === session.currentScriptVersion &&
      context.shotPlanVersion === session.currentShotPlanVersion && context.stepOutcome === "completed";
  })) throw new Error("creative-capture-take-already-returned");
}

function resultFor(session: CreativeSession, plan: ShotPlan, events: readonly CreativeSessionEvent[], assets: readonly LibraryAsset[], batchId: string) {
  const steps = resolveCreativeSteps(plan, events, assets);
  return { status: statusFor(steps, assets, session.id), assets: [], steps, editorialBatchId: session.editorialBatchId ?? batchId };
}

function statusFor(steps: readonly CreativeStepResolution[], assets: readonly LibraryAsset[], sessionId: string): "collecting" | "ready-for-editorial" | "no-assets" {
  if (steps.some((step) => step.state === "pending" || step.state === "retake-pending")) return "collecting";
  return assets.some((asset) => asset.captureContext?.creativeSessionId === sessionId) ? "ready-for-editorial" : "no-assets";
}

function returnEvent(session: CreativeSession, command: CreativeCaptureCommand): CreativeSessionEvent {
  const action = command.stepOutcome === "skipped" ? "skipped" : "media-returned";
  return { id: `creative_capture_${encodeURIComponent(command.operationKey)}`, sessionId: session.id,
    briefVersion: session.currentBriefVersion, scriptVersion: session.currentScriptVersion,
    shotPlanVersion: session.currentShotPlanVersion, stepId: command.stepId, deviceId: command.deviceId,
    deviceRole: command.deviceRole, action, actorId: command.actorId,
    reason: action === "skipped" ? "step-skipped-without-media" : null,
    idempotencyKey: `creative-capture-v2:${command.operationKey}`, take: command.take, createdAt: command.now };
}

function nextSession(session: CreativeSession, batchId: string, status: string, addedAssets: boolean, now: string): CreativeSession {
  const stage = session.stage === "shooting" ? "ingesting" : session.stage;
  return { ...session, stage, captureBatchIds: addedAssets ? [...new Set([...session.captureBatchIds, batchId])] : session.captureBatchIds,
    editorialBatchId: session.editorialBatchId ?? batchId, updatedAt: now };
}

function processingTask(session: CreativeSession, batchId: string, status: "collecting" | "ready-for-editorial" | "no-assets", updatedAt: string): CreativeProcessingTask {
  return { id: `editorial_task_${session.id}`, creativeSessionId: session.id, editorialBatchId: batchId,
    status, phase: status === "collecting" ? "capture" : "complete", errorCode: null, updatedAt };
}

function sessionAudit(before: CreativeSession, after: CreativeSession, input: { actorId: string; now: string }, status: string): AuditEvent[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  const transitioned = before.stage !== after.stage;
  return [{ id: `audit_creative_handoff_${after.id}_${after.updatedAt}_${before.stage}_${after.stage}`,
    entityType: "creative-session", entityId: after.id, actorId: input.actorId,
    action: transitioned ? "creative-session-transitioned" : "creative-capture-returned",
    reason: status,
    before: transitioned ? { stage: before.stage } : { captureBatchIds: before.captureBatchIds },
    after: transitioned ? { stage: after.stage } : { captureBatchIds: after.captureBatchIds },
    createdAt: input.now }];
}

function sessionPrecondition(session: CreativeSession) {
  return { id: session.id, stage: session.stage, currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion, currentShotPlanVersion: session.currentShotPlanVersion };
}
