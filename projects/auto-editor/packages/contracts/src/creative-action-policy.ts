import { createHash } from "node:crypto";

import {
  CreativeSessionEventSchema,
  type CreativeSessionAction,
  type CreativeSessionEvent,
  type CreativeVersionKey,
  type DeviceRole,
} from "./creative-director";

export type CreativeFieldAction = Extract<
  CreativeSessionAction,
  "started" | "completed" | "retake" | "skipped" | "voice-rewrite" | "note" | "privacy-paused"
>;

export type CreativeFieldActionInput = CreativeVersionKey & Readonly<{
  taskId: string;
  stepId: string;
  action: CreativeFieldAction;
  actorId: string;
  deviceId: string | null;
  deviceRole: DeviceRole | null;
  reason: string | null;
  transcript?: string | null;
  operationId: string;
  now: string;
}>;

export function createCreativeFieldEvent(
  input: CreativeFieldActionInput,
  context: Readonly<{
    availableActions: readonly CreativeSessionAction[] | null;
    currentTake: number;
    privacyPaused: boolean;
  }>,
): CreativeSessionEvent {
  if (!nonEmpty(input.operationId)) throw new Error("device-action-operation-required");
  if (context.privacyPaused) throw new Error("privacy-paused");
  if (context.availableActions !== null && !context.availableActions.includes(input.action)) {
    throw new Error("device-action-not-available");
  }
  if (input.action === "skipped" && !nonEmpty(input.reason)) {
    throw new Error("skipped-reason-required");
  }
  if (input.action === "voice-rewrite" && !nonEmpty(input.transcript)) {
    throw new Error("voice-rewrite-transcript-required");
  }
  if (input.action !== "voice-rewrite" && input.transcript !== undefined && input.transcript !== null) {
    throw new Error("unexpected-action-transcript");
  }
  if (!Number.isInteger(context.currentTake) || context.currentTake < 1) {
    throw new Error("creative-event-current-take-invalid");
  }
  const idempotencyKey = creativeFieldEventIdempotencyKey(input);
  return CreativeSessionEventSchema.parse({
    id: `creative_event_${createHash("sha256").update(idempotencyKey).digest("hex")}`,
    sessionId: input.sessionId,
    briefVersion: input.briefVersion,
    scriptVersion: input.scriptVersion,
    shotPlanVersion: input.shotPlanVersion,
    stepId: input.stepId,
    deviceId: input.deviceId,
    deviceRole: input.deviceRole,
    action: input.action,
    actorId: input.actorId,
    reason: normalizedNullable(input.reason),
    transcript: input.action === "voice-rewrite" ? input.transcript!.trim() : null,
    idempotencyKey,
    take: context.currentTake,
    createdAt: input.now,
  });
}

export function creativeFieldEventIdempotencyKey(
  input: Pick<CreativeFieldActionInput, "sessionId" | "stepId" | "operationId">,
): string {
  return `creative-field-event:v1|${canonicalLengthPrefixedTuple([
    input.sessionId,
    input.stepId,
    input.operationId,
  ])}`;
}

export function legacyCreativeFieldEventIdempotencyKey(
  input: Pick<CreativeFieldActionInput, "sessionId" | "stepId" | "operationId">,
): string {
  return `${input.sessionId}:${input.stepId}:${input.operationId}`;
}

export function resultingScriptVersionForCreativeFieldEvent(
  event: Pick<CreativeSessionEvent, "action" | "scriptVersion">,
): number {
  const result = event.action === "voice-rewrite"
    ? event.scriptVersion + 1
    : event.scriptVersion;
  if (!Number.isSafeInteger(result) || result < 1) {
    throw new Error("creative-action-result-version-invalid");
  }
  return result;
}

export function isSameCreativeEventIdentity(
  left: CreativeSessionEvent,
  right: CreativeSessionEvent,
): boolean {
  return left.id === right.id &&
    left.sessionId === right.sessionId &&
    left.briefVersion === right.briefVersion &&
    left.scriptVersion === right.scriptVersion &&
    left.shotPlanVersion === right.shotPlanVersion &&
    left.stepId === right.stepId &&
    left.deviceId === right.deviceId &&
    left.deviceRole === right.deviceRole &&
    left.action === right.action &&
    left.actorId === right.actorId &&
    left.reason === right.reason &&
    (left.transcript ?? null) === (right.transcript ?? null) &&
    left.idempotencyKey === right.idempotencyKey &&
    left.take === right.take;
}

export function isSameCreativeActionRequest(
  event: CreativeSessionEvent,
  input: CreativeFieldActionInput,
): boolean {
  return hasMatchingOperationKey(event, input) &&
    event.sessionId === input.sessionId &&
    event.briefVersion === input.briefVersion &&
    event.scriptVersion === input.scriptVersion &&
    event.shotPlanVersion === input.shotPlanVersion &&
    event.stepId === input.stepId &&
    event.deviceId === input.deviceId &&
    event.deviceRole === input.deviceRole &&
    event.action === input.action &&
    event.actorId === input.actorId &&
    event.reason === normalizedNullable(input.reason) &&
    (event.transcript ?? null) === (
      input.action === "voice-rewrite" ? input.transcript?.trim() ?? null : null
    );
}

function hasMatchingOperationKey(
  event: CreativeSessionEvent,
  input: CreativeFieldActionInput,
): boolean {
  if (event.idempotencyKey === creativeFieldEventIdempotencyKey(input)) return true;
  return event.sessionId === input.sessionId && event.stepId === input.stepId &&
    event.idempotencyKey === legacyCreativeFieldEventIdempotencyKey(input);
}

function canonicalLengthPrefixedTuple(values: readonly string[]): string {
  return values.map((value) => `${value.length}:${value}`).join("|");
}

function normalizedNullable(value: string | null): string | null {
  const normalized = value?.trim();
  return normalized ? normalized : null;
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
