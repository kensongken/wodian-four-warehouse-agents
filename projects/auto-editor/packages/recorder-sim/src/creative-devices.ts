import type {
  CreativeSessionAction,
  CreativeSessionEvent,
  CreativeVersionKey,
  DeviceBrief,
  DeviceRole,
  DirectorCard,
} from "@wearable/contracts";
import {
  createCreativeFieldEvent,
  creativeFieldEventIdempotencyKey,
  CreativeSessionEventSchema,
  DeviceBriefSchema,
  DirectorCardSchema,
  isSameCreativeActionRequest,
} from "@wearable/contracts";

export interface CreativeDeviceAdapter {
  deliver(task: DirectorCard | DeviceBrief, idempotencyKey: string): Promise<{
    deviceId: string;
    receivedAt: string;
  }>;
}

export type LocalSimulatorDelivery = Readonly<{
  deviceId: string;
  deviceRole: DeviceRole;
  task: DirectorCard | DeviceBrief;
  idempotencyKey: string;
}>;

export type SimulatedDeviceAction = CreativeVersionKey & {
  stepId: string;
  action: Extract<CreativeSessionAction, "completed" | "retake" | "skipped" | "voice-rewrite" | "note" | "privacy-paused">;
  actorId: string;
  reason: string | null;
  now: string;
  operationId: string;
  transcript?: string;
};

type InboxEntry = CreativeVersionKey & {
  taskId: string;
  stepId: string;
  idempotencyKey: string;
  availableActions: readonly CreativeSessionAction[] | null;
};

/**
 * A local A-0 mailbox for device contracts. It stores identifiers and versions
 * only, never media or a full content library.
 */
export class SimulatedCreativeDevice implements CreativeDeviceAdapter {
  readonly transport = "simulated" as const;
  readonly #eventLog: CreativeSessionEvent[] = [];
  private connected = true;
  #privacyPaused = false;
  private currentVersion: CreativeVersionKey | null = null;
  private readonly receipts = new Map<string, { deviceId: string; receivedAt: string }>();
  private readonly inboxByIdempotencyKey = new Map<string, InboxEntry>();
  private readonly takes = new Map<string, number>();
  private readonly eventsByIdempotencyKey = new Map<string, CreativeSessionEvent>();

  constructor(readonly deviceId: string, readonly role: DeviceRole) {}

  get inbox(): InboxEntry[] {
    return deepFreeze([...this.inboxByIdempotencyKey.values()].map((entry) => ({
      ...entry,
      availableActions: entry.availableActions === null ? null : [...entry.availableActions],
    })));
  }

  get events(): CreativeSessionEvent[] {
    return deepFreeze(this.#eventLog.map((event) =>
      CreativeSessionEventSchema.parse(structuredClone(event)),
    ));
  }

  setConnected(value: boolean): void {
    this.connected = value;
  }

  setCurrentVersion(value: CreativeVersionKey): void {
    this.currentVersion = { ...value };
    for (const [key, entry] of this.inboxByIdempotencyKey) {
      if (!hasSameVersion(entry, value)) this.inboxByIdempotencyKey.delete(key);
    }
  }

  async deliver(
    task: DirectorCard | DeviceBrief,
    idempotencyKey: string,
  ): Promise<{ deviceId: string; receivedAt: string }> {
    if (this.#privacyPaused) throw new Error("privacy-paused");
    if (!this.connected) throw new Error("device-offline");
    const checkedTask = parseClonedTask(task);
    if (checkedTask.status !== "active") throw new Error("invalidated-device-task");
    if (!isTaskForRole(checkedTask, this.role)) throw new Error("device-role-mismatch");
    if (this.currentVersion === null) throw new Error("current-device-version-required");
    if (!hasSameVersion(checkedTask, this.currentVersion)) {
      throw new Error("stale-device-task");
    }

    const receipt = this.receipts.get(idempotencyKey);
    if (receipt) return { ...receipt };

    const nextReceipt = { deviceId: this.deviceId, receivedAt: new Date().toISOString() };
    this.receipts.set(idempotencyKey, nextReceipt);
    this.inboxByIdempotencyKey.set(idempotencyKey, {
      taskId: checkedTask.id,
      sessionId: checkedTask.sessionId,
      briefVersion: checkedTask.briefVersion,
      scriptVersion: checkedTask.scriptVersion,
      shotPlanVersion: checkedTask.shotPlanVersion,
      stepId: checkedTask.stepId,
      idempotencyKey,
      availableActions: "availableActions" in checkedTask
        ? Object.freeze([...checkedTask.availableActions])
        : null,
    });
    this.takes.set(checkedTask.stepId, this.takes.get(checkedTask.stepId) ?? 1);
    return { ...nextReceipt };
  }

  async syncLocalDeliveries(deliveries: readonly LocalSimulatorDelivery[]): Promise<void> {
    if (this.#privacyPaused) throw new Error("privacy-paused");
    if (this.currentVersion === null) throw new Error("current-device-version-required");
    for (const delivery of deliveries) {
      if (delivery.deviceId !== this.deviceId) throw new Error("device-delivery-device-mismatch");
      if (delivery.deviceRole !== this.role) throw new Error("device-role-mismatch");
      const task = parseClonedTask(delivery.task);
      if (!hasSameVersion(task, this.currentVersion)) continue;
      await this.deliver(task, delivery.idempotencyKey);
    }
  }

  pausePrivacy(): void {
    this.#privacyPaused = true;
  }

  resumePrivacy(): void {
    if (!this.connected) throw new Error("device-offline");
    this.#privacyPaused = false;
  }

  isPrivacyPaused(): boolean {
    return this.#privacyPaused;
  }

  emitAction(input: SimulatedDeviceAction): CreativeSessionEvent {
    const entry = [...this.inboxByIdempotencyKey.values()].find((candidate) =>
      candidate.sessionId === input.sessionId &&
      candidate.briefVersion === input.briefVersion &&
      candidate.scriptVersion === input.scriptVersion &&
      candidate.shotPlanVersion === input.shotPlanVersion &&
      candidate.stepId === input.stepId,
    );
    if (!entry) throw new Error("device-event-task-mismatch");
    const actionInput = {
      ...input,
      taskId: entry.taskId,
      deviceId: this.deviceId,
      deviceRole: this.role,
      transcript: input.transcript ?? null,
    };
    const idempotencyKey = creativeFieldEventIdempotencyKey(actionInput);
    const existing = this.eventsByIdempotencyKey.get(idempotencyKey);
    if (existing) {
      if (!isSameCreativeActionRequest(existing, actionInput)) {
        throw new Error("device-action-operation-conflict");
      }
      return deepFreeze(CreativeSessionEventSchema.parse(structuredClone(existing)));
    }
    const event = createCreativeFieldEvent(actionInput, {
      availableActions: entry.availableActions,
      currentTake: this.takes.get(input.stepId) ?? 1,
      privacyPaused: this.#privacyPaused,
    });
    this.takes.set(input.stepId, input.action === "retake" ? event.take! + 1 : event.take!);
    this.eventsByIdempotencyKey.set(idempotencyKey, event);
    this.#eventLog.push(event);
    if (input.action === "privacy-paused") this.#privacyPaused = true;
    return deepFreeze(CreativeSessionEventSchema.parse(structuredClone(event)));
  }

  takeFor(stepId: string): number {
    return this.takes.get(stepId) ?? 0;
  }
}

function isTaskForRole(task: DirectorCard | DeviceBrief, role: DeviceRole): boolean {
  return role === "watch-director"
    ? !("deviceRole" in task)
    : "deviceRole" in task && task.deviceRole === role;
}

function hasSameVersion(value: CreativeVersionKey, current: CreativeVersionKey): boolean {
  return value.sessionId === current.sessionId && value.briefVersion === current.briefVersion &&
    value.scriptVersion === current.scriptVersion && value.shotPlanVersion === current.shotPlanVersion;
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
