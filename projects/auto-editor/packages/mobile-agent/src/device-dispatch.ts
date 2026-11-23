import { randomUUID } from "node:crypto";

import type {
  CreativeSession,
  CreativeSessionEvent,
  CreativeVersionKey,
  DeviceBrief,
  DeviceRole,
  DirectorCard,
  DispatchReceipt,
} from "@wearable/contracts";
import {
  CreativeVersionKeySchema,
  DeviceBriefSchema,
  DirectorCardSchema,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";

const requiredDeviceRoles = ["watch-director", "main-stone-a", "main-stone-b"] as const;

type LocalMailboxState = {
  deviceId: string;
  deliveries: Array<{
    task: DirectorCard | DeviceBrief;
    deviceRole: DeviceRole;
    idempotencyKey: string;
    status: "attempting" | "available" | "failed" | "consumed" | "invalidated";
  }>;
  outcomes: Array<string | null>;
  gate: { entered: Promise<void>; enter(): void; released: Promise<void>; release(): void } | null;
};

type LocalDispatchRosterState = {
  mailboxes: ReadonlyMap<DeviceRole, LocalDispatchMailbox>;
  version: CreativeVersionKey;
};

type LocalSimulatorPullCapabilityState = {
  mailbox: LocalDispatchMailbox;
  deviceId: string;
  deviceRole: DeviceRole;
  version: CreativeVersionKey;
  consumed: boolean;
};

export type LocalSimulatorDelivery = Readonly<{
  deviceId: string;
  deviceRole: DeviceRole;
  task: DirectorCard | DeviceBrief;
  idempotencyKey: string;
}>;

// A WeakMap membership check is an unforgeable module-private brand. In
// particular, a Proxy around a mailbox is a different object and is rejected
// without invoking any of its traps.
const localMailboxStates = new WeakMap<object, LocalMailboxState>();
const localDispatchRosterStates = new WeakMap<object, LocalDispatchRosterState>();
const localSimulatorPullCapabilityStates = new WeakMap<object, LocalSimulatorPullCapabilityState>();

export class LocalDispatchRoster {
  private constructor() {}
}

export class LocalSimulatorPullCapability {
  private constructor() {}
}

export class LocalDispatchMailbox {
  constructor(deviceId: string) {
    localMailboxStates.set(this, { deviceId, deliveries: [], outcomes: [], gate: null });
    // This object is deliberately sealed: callers may configure only local failure
    // and timing behavior, never replace delivery with an arbitrary side effect.
    Object.seal(this);
  }

  get deviceId(): string {
    return requireLocalMailboxState(this).deviceId;
  }

  get deliveries(): ReadonlyArray<Readonly<{ taskId: string; idempotencyKey: string }>> {
    return Object.freeze(requireLocalMailboxState(this).deliveries.map((delivery) => Object.freeze({
      taskId: delivery.task.id,
      idempotencyKey: delivery.idempotencyKey,
    })));
  }

  queueOutcomes(outcomes: Array<string | null>): void {
    requireLocalMailboxState(this).outcomes.push(...outcomes);
  }

  holdNextDelivery(): { entered: Promise<void>; release(): void } {
    const state = requireLocalMailboxState(this);
    if (state.gate) throw new Error("local-mailbox-delivery-already-held");
    let enter!: () => void;
    let release!: () => void;
    const entered = new Promise<void>((resolve) => { enter = resolve; });
    const released = new Promise<void>((resolve) => { release = resolve; });
    state.gate = { entered, enter, released, release };
    return { entered, release };
  }

}

export function createLocalDispatchRoster(input: {
  mailboxes: Record<DeviceRole, LocalDispatchMailbox>;
  version: CreativeVersionKey;
}): Readonly<{
  roster: LocalDispatchRoster;
  pulls: Readonly<Record<DeviceRole, LocalSimulatorPullCapability>>;
}> {
  const source = input.mailboxes;
  const captured = requiredDeviceRoles.map((role) => [role, source[role]] as const);
  if (captured.some(([, mailbox]) => !isTrustedLocalMailbox(mailbox))) {
    throw new Error("trusted-local-adapter-required");
  }
  if (new Set(captured.map(([, mailbox]) => mailbox)).size !== requiredDeviceRoles.length) {
    throw new Error("distinct-local-mailboxes-required");
  }
  const version = CreativeVersionKeySchema.parse(structuredClone(input.version));
  const roster = Object.create(LocalDispatchRoster.prototype) as LocalDispatchRoster;
  localDispatchRosterStates.set(roster, {
    mailboxes: new Map(captured),
    version: Object.freeze({ ...version }),
  });
  Object.seal(roster);
  const pulls = Object.fromEntries(captured.map(([deviceRole, mailbox]) => {
    const capability = Object.create(LocalSimulatorPullCapability.prototype) as LocalSimulatorPullCapability;
    const mailboxState = requireLocalMailboxState(mailbox);
    localSimulatorPullCapabilityStates.set(capability, {
      mailbox,
      deviceId: mailboxState.deviceId,
      deviceRole,
      version: Object.freeze({ ...version }),
      consumed: false,
    });
    Object.seal(capability);
    return [deviceRole, capability];
  })) as Record<DeviceRole, LocalSimulatorPullCapability>;
  return Object.freeze({ roster, pulls: Object.freeze(pulls) });
}

export function consumeLocalSimulatorDeliveries(
  capability: LocalSimulatorPullCapability,
): readonly LocalSimulatorDelivery[] {
  if (typeof capability !== "object" || capability === null ||
    !localSimulatorPullCapabilityStates.has(capability)) {
    throw new Error("trusted-local-pull-capability-required");
  }
  const pull = localSimulatorPullCapabilityStates.get(capability)!;
  if (pull.consumed) throw new Error("local-pull-capability-consumed");
  const mailbox = requireLocalMailboxState(pull.mailbox);
  const selected: Array<{ delivery: LocalMailboxState["deliveries"][number]; task: DirectorCard | DeviceBrief }> = [];
  for (const delivery of mailbox.deliveries) {
    if (delivery.status !== "available" || delivery.deviceRole !== pull.deviceRole) continue;
    if (!hasSameTaskVersion(delivery.task, pull.version)) {
      if (isOlderTaskVersion(delivery.task, pull.version)) delivery.status = "invalidated";
      continue;
    }
    selected.push({ delivery, task: parseClonedTask(delivery.task) });
  }
  pull.consumed = true;
  for (const item of selected) item.delivery.status = "consumed";
  return deepFreeze(selected.map(({ delivery, task }) => ({
    deviceId: pull.deviceId,
    deviceRole: pull.deviceRole,
    task,
    idempotencyKey: delivery.idempotencyKey,
  })));
}

function requireLocalMailboxState(mailbox: object): LocalMailboxState {
  const state = localMailboxStates.get(mailbox);
  if (!state) throw new Error("trusted-local-adapter-required");
  return state;
}

async function deliverLocalMailbox(
  mailbox: LocalDispatchMailbox,
  task: DirectorCard | DeviceBrief,
  deviceRole: DeviceRole,
  expectedDeviceId: string,
  idempotencyKey: string,
): Promise<{ deviceId: string; receivedAt: string }> {
  const state = requireLocalMailboxState(mailbox);
  if (state.deviceId !== expectedDeviceId ||
    ("deviceId" in task && task.deviceId !== expectedDeviceId)) {
    throw new Error("device-receipt-device-mismatch");
  }
  const delivery: LocalMailboxState["deliveries"][number] = {
    task: parseClonedTask(task),
    deviceRole,
    idempotencyKey,
    status: "attempting",
  };
  state.deliveries.push(delivery);
  const gate = state.gate;
  state.gate = null;
  if (gate) {
    gate.enter();
    await gate.released;
  }
  const failure = state.outcomes.shift();
  if (failure) {
    delivery.status = "failed";
    throw new Error(failure);
  }
  delivery.status = "available";
  return { deviceId: state.deviceId, receivedAt: new Date().toISOString() };
}

// A-0 is a dispatcher-owned local command queue. Freezing the prototype stops a
// caller from replacing its command implementation; sealing instances prevents
// an own `deliver` callback. recorder-sim remains an independent device model.
Object.freeze(LocalDispatchMailbox.prototype);
Object.freeze(LocalDispatchRoster.prototype);
Object.freeze(LocalSimulatorPullCapability.prototype);

export async function preflightShootingPackageDispatch(
  sessionId: string,
  library: ContentLibrary,
): Promise<CreativeVersionKey> {
  const session = await requireDispatchableSession(sessionId, library);
  await requireCurrentTasks(session, library);
  return {
    sessionId: session.id,
    briefVersion: session.currentBriefVersion,
    scriptVersion: session.currentScriptVersion,
    shotPlanVersion: session.currentShotPlanVersion,
  };
}

export async function dispatchShootingPackage(
  input: {
    sessionId: string;
    confirmStart: true;
    roster: LocalDispatchRoster;
    actorId: string;
    now: string;
  },
  library: ContentLibrary,
): Promise<DispatchReceipt[]> {
  if (input.confirmStart !== true) throw new Error("dispatch-confirmation-required");
  if (!nonEmpty(input.actorId)) throw new Error("dispatch-actor-required");
  const rosterCandidate: unknown = input.roster;
  if (typeof rosterCandidate !== "object" || rosterCandidate === null ||
    !localDispatchRosterStates.has(rosterCandidate)) {
    throw new Error("trusted-local-roster-required");
  }
  const roster = localDispatchRosterStates.get(rosterCandidate)!;
  const reservation = await library.acquireCreativeDispatchReservation(input.sessionId);
  try {
  const session = await requireDispatchableSession(input.sessionId, library);
  const tasks = await requireCurrentTasks(session, library);
  if (tasks.some((item) => !hasSameTaskVersion(item.task, roster.version))) {
    throw new Error("local-roster-version-mismatch");
  }
  const existingReceipts = await library.listDispatchReceipts();
  const existingEvents = await library.listCreativeSessionEvents();
  const receipts: DispatchReceipt[] = [];
  const events: CreativeSessionEvent[] = [];
  let fatalError: string | null = null;

  for (const item of tasks) {
    const expected = dispatchReceipt(item, session.id, input.now);
    const sameId = existingReceipts.find((receipt) => receipt.id === expected.id);
    if (sameId && !hasSameReceiptIdentity(sameId, expected)) {
      throw new Error("dispatch-receipt-identity-conflict");
    }
    const previous = sameId;
    if (previous?.status === "received") {
      receipts.push(previous);
      continue;
    }
    if (previous?.status === "failed") {
      receipts.push(previous);
      continue;
    }

    const receipt = previous ?? expected;
    if (reservation.isInterrupted()) {
      receipts.push({ ...receipt, status: "pending" as const });
      appendEvent(events, existingEvents, deviceEvent(item, "disconnected", "creative-dispatch-interrupted", input));
      fatalError ??= "creative-dispatch-interrupted";
      continue;
    }
    try {
      const adapter = roster.mailboxes.get(item.role)!;
      const delivered = await deliverLocalMailbox(
        adapter,
        item.task,
        item.role,
        item.deviceId,
        receipt.idempotencyKey,
      );
      if (delivered.deviceId !== item.deviceId) throw new Error("device-receipt-device-mismatch");
      const received = { ...receipt, status: "received" as const };
      receipts.push(received);
      appendEvent(events, existingEvents, deviceEvent(item, "received", null, input));
      if (reservation.isInterrupted()) fatalError ??= "creative-dispatch-interrupted";
    } catch (error) {
      receipts.push({ ...receipt, status: "pending" as const });
      appendEvent(events, existingEvents, deviceEvent(
        item,
        "disconnected",
        error instanceof Error ? error.message : "device-delivery-failed",
        input,
      ));
    }
  }

  if (reservation.isInterrupted()) fatalError ??= "creative-dispatch-interrupted";
  const everyTaskReceived = tasks.every((item) => receipts.some((receipt) =>
    receipt.id === `dispatch_${item.task.id}` && receipt.status === "received",
  ));
  const allCurrentReceipts = everyTaskReceived && fatalError === null
    ? await reservation.tryFinalize()
    : false;
  if (everyTaskReceived && !allCurrentReceipts && reservation.isInterrupted()) {
    fatalError ??= "creative-dispatch-interrupted";
  }
  const next: CreativeSession = allCurrentReceipts
    ? { ...session, stage: "shooting", updatedAt: input.now }
    : session;
  try {
    await library.commitCreativeBundle({
      session: next,
      expectedSession: sessionPrecondition(session),
      dispatchReservationId: reservation.id,
      receipts,
      events,
      auditEvents: allCurrentReceipts ? [sessionTransitionAudit(session, input)] : [],
    });
  } catch (error) {
    if (!(error instanceof Error) || error.message !== "creative-session-precondition-failed") throw error;
    for (const item of tasks) {
      appendEvent(events, existingEvents, deviceEvent(
        item,
        "disconnected",
        "creative-dispatch-lineage-changed",
        input,
      ));
    }
    const latest = (await library.listCreativeSessions()).find((item) => item.id === input.sessionId);
    if (!latest) throw new Error("creative-dispatch-lineage-changed");
    await library.commitCreativeBundle({
      session: latest,
      expectedSession: sessionPrecondition(latest),
      dispatchReservationId: reservation.id,
      receipts,
      events,
      auditEvents: [],
    });
    throw new Error("creative-dispatch-lineage-changed");
  }

  if (fatalError) throw new Error(fatalError);
  return receipts;
  } finally {
    reservation.release();
  }
}

type DispatchTask = {
  task: DirectorCard | DeviceBrief;
  role: DeviceRole;
  deviceId: string;
};

async function requireDispatchableSession(
  sessionId: string,
  library: ContentLibrary,
): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error("creative-session-not-found");
  if (session.status !== "active") throw new Error("creative-session-not-active");
  if (session.stage !== "ready_to_dispatch") throw new Error("shooting-dispatch-not-allowed");
  if (session.currentShotPlanVersion < 1) throw new Error("current-shot-plan-required");
  return session;
}

async function requireCurrentTasks(
  session: CreativeSession,
  library: ContentLibrary,
): Promise<DispatchTask[]> {
  const current = <T extends { sessionId: string; briefVersion: number; scriptVersion: number; shotPlanVersion: number; status: string }>(task: T) =>
    task.sessionId === session.id && task.briefVersion === session.currentBriefVersion &&
    task.scriptVersion === session.currentScriptVersion &&
    task.shotPlanVersion === session.currentShotPlanVersion && task.status === "active";
  const allPlans = (await library.listShotPlans()).filter((plan) =>
    plan.sessionId === session.id && plan.status === "active",
  );
  const currentPlans = allPlans.filter(current);
  if (currentPlans.length === 0) throw new Error("current-shot-plan-required");
  if (allPlans.some((plan) => !current(plan))) {
    throw new Error("shooting-package-coverage-invalid");
  }
  const shotPlans = currentPlans;
  if (shotPlans.length !== 1) throw new Error("current-shot-plan-required");
  const allCards = (await library.listDirectorCards()).filter((task) =>
    task.sessionId === session.id && task.status === "active",
  );
  const allBriefs = (await library.listDeviceBriefs()).filter((task) =>
    task.sessionId === session.id && task.status === "active",
  );
  if (allCards.some((task) => !current(task)) || allBriefs.some((task) => !current(task))) {
    throw new Error("shooting-package-coverage-invalid");
  }
  const watchCards = allCards;
  const briefs = allBriefs;
  const bindings = new Map(session.deviceBindings.map((binding) => [binding.deviceRole, binding]));
  if (requiredDeviceRoles.some((role) => bindings.get(role)?.transport !== "simulated")) {
    throw new Error("all-device-roles-required");
  }
  if (requiredDeviceRoles.filter((role) => role !== "watch-director").some((role) =>
    !briefs.some((brief) => brief.deviceRole === role),
  )) throw new Error("all-device-roles-required");
  validateTaskCoverage(shotPlans[0]!, watchCards, briefs, bindings);
  const roleTasks: DispatchTask[] = [
    ...watchCards.map((task) => ({
      task,
      role: "watch-director" as const,
      deviceId: bindings.get("watch-director")?.deviceId ?? "",
    })),
    ...briefs.map((task) => ({ task, role: task.deviceRole, deviceId: task.deviceId })),
  ];
  if (requiredDeviceRoles.some((role) => !roleTasks.some((item) => item.role === role))) {
    throw new Error("all-device-roles-required");
  }
  if (roleTasks.some((item) => !nonEmpty(item.deviceId))) throw new Error("all-device-roles-required");
  return roleTasks;
}

function validateTaskCoverage(
  plan: Awaited<ReturnType<ContentLibrary["listShotPlans"]>>[number],
  cards: DirectorCard[],
  briefs: DeviceBrief[],
  bindings: Map<DeviceRole, CreativeSession["deviceBindings"][number]>,
): void {
  const stepById = new Map(plan.steps.map((step) => [step.id, step]));
  if (cards.some((card) => !stepById.get(card.stepId)?.deviceRoles.includes("watch-director")) ||
    briefs.some((brief) => !stepById.get(brief.stepId)?.deviceRoles.includes(brief.deviceRole) ||
      bindings.get(brief.deviceRole)?.deviceId !== brief.deviceId)) {
    throw new Error("shooting-package-coverage-invalid");
  }
  for (const step of plan.steps) {
    const watchCount = cards.filter((card) => card.stepId === step.id).length;
    if ((step.deviceRoles.includes("watch-director") && watchCount !== 1) ||
      (!step.deviceRoles.includes("watch-director") && watchCount !== 0)) {
      throw new Error("shooting-package-coverage-invalid");
    }
    for (const role of step.deviceRoles.filter((role) => role !== "watch-director")) {
      if (briefs.filter((brief) => brief.stepId === step.id && brief.deviceRole === role).length !== 1) {
        throw new Error("shooting-package-coverage-invalid");
      }
    }
  }
}

function dispatchReceipt(
  item: DispatchTask,
  sessionId: string,
  now: string,
): DispatchReceipt {
  return {
    id: `dispatch_${item.task.id}`,
    sessionId,
    briefVersion: item.task.briefVersion,
    scriptVersion: item.task.scriptVersion,
    shotPlanVersion: item.task.shotPlanVersion,
    deviceId: item.deviceId,
    deviceRole: item.role,
    transport: "simulated",
    idempotencyKey: `${sessionId}:${item.task.id}:${item.task.scriptVersion}`,
    status: "pending",
    createdAt: now,
  };
}

function hasSameReceiptIdentity(left: DispatchReceipt, right: DispatchReceipt): boolean {
  return left.sessionId === right.sessionId && left.briefVersion === right.briefVersion &&
    left.scriptVersion === right.scriptVersion && left.shotPlanVersion === right.shotPlanVersion &&
    left.deviceId === right.deviceId && left.deviceRole === right.deviceRole &&
    left.transport === right.transport && left.idempotencyKey === right.idempotencyKey;
}

function isTrustedLocalMailbox(value: unknown): value is LocalDispatchMailbox {
  return typeof value === "object" && value !== null && localMailboxStates.has(value);
}

function hasSameTaskVersion(
  task: Pick<DirectorCard, "sessionId" | "briefVersion" | "scriptVersion" | "shotPlanVersion">,
  version: Pick<DirectorCard, "sessionId" | "briefVersion" | "scriptVersion" | "shotPlanVersion">,
): boolean {
  return task.sessionId === version.sessionId && task.briefVersion === version.briefVersion &&
    task.scriptVersion === version.scriptVersion && task.shotPlanVersion === version.shotPlanVersion;
}

function isOlderTaskVersion(
  task: Pick<DirectorCard, "sessionId" | "briefVersion" | "scriptVersion" | "shotPlanVersion">,
  version: Pick<DirectorCard, "sessionId" | "briefVersion" | "scriptVersion" | "shotPlanVersion">,
): boolean {
  if (task.sessionId !== version.sessionId) return false;
  if (task.briefVersion !== version.briefVersion) return task.briefVersion < version.briefVersion;
  if (task.scriptVersion !== version.scriptVersion) return task.scriptVersion < version.scriptVersion;
  return task.shotPlanVersion < version.shotPlanVersion;
}

function parseClonedTask(task: DirectorCard | DeviceBrief): DirectorCard | DeviceBrief {
  const cloned: unknown = structuredClone(task);
  return typeof cloned === "object" && cloned !== null && "availableActions" in cloned
    ? DirectorCardSchema.parse(cloned)
    : DeviceBriefSchema.parse(cloned);
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const item of Object.values(value)) deepFreeze(item);
  return Object.freeze(value);
}

function deviceEvent(
  item: DispatchTask,
  action: "received" | "disconnected",
  reason: string | null,
  input: { sessionId: string; actorId: string; now: string },
): CreativeSessionEvent {
  const idempotencyKey = `${input.sessionId}:${item.task.id}:${action}:${item.task.scriptVersion}`;
  return {
    id: `device_event_${item.task.id}_${action}`,
    sessionId: input.sessionId,
    briefVersion: item.task.briefVersion,
    scriptVersion: item.task.scriptVersion,
    shotPlanVersion: item.task.shotPlanVersion,
    stepId: item.task.stepId,
    deviceId: item.deviceId,
    deviceRole: item.role,
    action,
    actorId: input.actorId,
    reason,
    idempotencyKey,
    createdAt: input.now,
  };
}

function appendEvent(
  events: CreativeSessionEvent[],
  existing: CreativeSessionEvent[],
  event: CreativeSessionEvent,
): void {
  if (!existing.some((item) => item.idempotencyKey === event.idempotencyKey) &&
    !events.some((item) => item.idempotencyKey === event.idempotencyKey)) events.push(event);
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

function sessionTransitionAudit(
  session: CreativeSession,
  input: { actorId: string; now: string },
) {
  return {
    id: `audit_${randomUUID()}`,
    entityType: "creative-session" as const,
    entityId: session.id,
    actorId: input.actorId,
    action: "creative-session-transitioned",
    reason: "all local simulated device tasks received",
    before: { stage: session.stage },
    after: { stage: "shooting" },
    createdAt: input.now,
  };
}

function nonEmpty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
