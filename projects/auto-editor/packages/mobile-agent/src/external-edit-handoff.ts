import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, realpath } from "node:fs/promises";
import { join, resolve } from "node:path";

import {
  ExternalEditHandoffSchema,
  ExternalEditHandoffStatusSchema,
  StudioOperationKeySchema,
  type DraftPackage,
  type ExternalEditHandoff,
  type ExternalEditHandoffStatus,
  type ExternalEditSourceAssetBinding,
} from "@wearable/contracts";
import { z } from "zod";

import type { StudioScope } from "./studio-capabilities";

const CreateInputSchema = z.object({
  parentDraftId: z.string().trim().min(1),
  parentDraftVersion: z.number().int().positive(),
  parentDraftDigest: z.string().regex(/^[a-f0-9]{64}$/),
  platformVariant: z.enum(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"]),
  revisionInstruction: z.string().trim().min(1).max(2_000),
  mustKeep: z.array(z.string().trim().min(1).max(200)).max(20),
  includeSources: z.boolean(),
  sourceAssetBindings: z.array(z.object({
    sourceAssetId: z.string().trim().min(1),
    digest: z.string().regex(/^[a-f0-9]{64}$/),
  }).strict()).max(100),
  operationKey: StudioOperationKeySchema,
}).strict().superRefine((value, ctx) => {
  if (value.includeSources !== (value.sourceAssetBindings.length > 0)) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceAssetBindings"], message: "source-consent-binding-mismatch" });
  }
  if (new Set(value.sourceAssetBindings.map((item) => item.sourceAssetId)).size !== value.sourceAssetBindings.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["sourceAssetBindings"], message: "duplicate-source-asset-id" });
  }
  if (new Set(value.mustKeep).size !== value.mustKeep.length) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: ["mustKeep"], message: "duplicate-must-keep" });
  }
});

export type CreateExternalEditHandoffInput = Readonly<z.infer<typeof CreateInputSchema>>;

export type TransitionExternalEditHandoffInput = Readonly<{
  handoffId: string;
  status: ExternalEditHandoffStatus;
  operationKey: string;
  packageDigest?: string;
}>;

type HandoffOptions = Readonly<{
  root: string;
  now?: () => string;
  createId?: () => string;
  resolveParentDraft: (scope: StudioScope, draftId: string) => Promise<DraftPackage | null>;
}>;

const EventSchema = z.object({
  eventId: z.string().min(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  actorId: z.string().min(1),
  operationKey: StudioOperationKeySchema,
  requestDigest: z.string().regex(/^[a-f0-9]{64}$/),
  value: ExternalEditHandoffSchema,
}).strict();

type HandoffEvent = z.infer<typeof EventSchema>;
type OperationReceipt = Readonly<{ requestDigest: string; value: ExternalEditHandoff }>;

const activeStatuses = new Set<ExternalEditHandoffStatus>([
  "preparing", "ready", "editing", "candidate-found", "degraded",
]);

const allowedTransitions: Readonly<Record<ExternalEditHandoffStatus, ReadonlySet<ExternalEditHandoffStatus>>> = {
  preparing: new Set(["ready", "degraded", "failed", "cancelled"]),
  ready: new Set(["editing", "degraded", "failed", "cancelled"]),
  editing: new Set(["ready", "candidate-found", "degraded", "failed", "cancelled"]),
  "candidate-found": new Set(["editing", "returned", "failed", "cancelled"]),
  returned: new Set(),
  degraded: new Set(["editing", "candidate-found", "returned", "failed", "cancelled"]),
  failed: new Set(),
  cancelled: new Set(),
};

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function operationIdentity(scope: StudioScope, operationKey: string): string {
  return `${scope.customerId}\u0000${scope.sessionId}\u0000${operationKey}`;
}

export class ExternalEditHandoffService {
  private readonly root: string;
  private readonly eventPath: string;
  private readonly now: () => string;
  private readonly createId: () => string;
  private readonly resolveParentDraft: HandoffOptions["resolveParentDraft"];
  private readonly handoffs = new Map<string, ExternalEditHandoff>();
  private readonly operations = new Map<string, OperationReceipt>();
  private loadPromise: Promise<void> | null = null;
  private serial = Promise.resolve();

  constructor(options: HandoffOptions) {
    this.root = resolve(options.root);
    this.eventPath = join(this.root, "handoffs.jsonl");
    this.now = options.now ?? (() => new Date().toISOString());
    this.createId = options.createId ?? (() => `handoff_${randomUUID()}`);
    this.resolveParentDraft = options.resolveParentDraft;
  }

  async create(scope: StudioScope, inputValue: CreateExternalEditHandoffInput): Promise<ExternalEditHandoff> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      const input = CreateInputSchema.parse(inputValue);
      const opId = operationIdentity(scope, input.operationKey);
      const requestDigest = digest({
        actorId: scope.actorId,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        ...input,
      });
      const prior = this.operations.get(opId);
      if (prior) {
        if (prior.requestDigest !== requestDigest) throw new Error("external-edit-operation-key-reused");
        return prior.value;
      }

      const parent = await this.resolveParentDraft(scope, input.parentDraftId);
      if (!parent || parent.customerId !== scope.customerId || parent.sessionId !== scope.sessionId) {
        throw new Error("external-edit-parent-not-found");
      }
      if (!["reviewable", "adopted"].includes(parent.status)
        || parent.version !== input.parentDraftVersion
        || parent.outputDigest !== input.parentDraftDigest) {
        throw new Error("external-edit-parent-changed");
      }
      if ([...this.handoffs.values()].some((item) =>
        item.customerId === scope.customerId
        && item.sessionId === scope.sessionId
        && item.parentDraftId === parent.id
        && activeStatuses.has(item.status))) {
        throw new Error("external-edit-active-handoff-exists");
      }

      const timestamp = this.now();
      const value = ExternalEditHandoffSchema.parse({
        id: this.createId(),
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        parentDraftId: parent.id,
        parentDraftVersion: parent.version,
        parentDraftDigest: parent.outputDigest,
        platformVariant: input.platformVariant,
        revisionInstruction: input.revisionInstruction,
        mustKeep: input.mustKeep,
        includeSources: input.includeSources,
        sourceAssetBindings: input.sourceAssetBindings,
        mode: "file-package",
        adapterId: null,
        adapterVersion: null,
        status: "preparing",
        operationKey: input.operationKey,
        packageDigest: null,
        publishClaimed: false,
        createdAt: timestamp,
        updatedAt: timestamp,
      });
      await this.appendEvent({
        eventId: randomUUID(),
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        actorId: scope.actorId,
        operationKey: input.operationKey,
        requestDigest,
        value,
      });
      return value;
    });
  }

  async get(scope: StudioScope, handoffId: string): Promise<ExternalEditHandoff> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      const value = this.handoffs.get(handoffId);
      if (!value || value.customerId !== scope.customerId || value.sessionId !== scope.sessionId) {
        throw new Error("external-edit-handoff-not-found");
      }
      return value;
    });
  }

  async list(scope: StudioScope): Promise<ExternalEditHandoff[]> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      return [...this.handoffs.values()].filter((value) =>
        value.customerId === scope.customerId && value.sessionId === scope.sessionId,
      );
    });
  }

  async transition(scope: StudioScope, input: TransitionExternalEditHandoffInput): Promise<ExternalEditHandoff> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      const parsed = z.object({
        handoffId: z.string().trim().min(1),
        status: ExternalEditHandoffStatusSchema,
        operationKey: StudioOperationKeySchema,
        packageDigest: z.string().regex(/^[a-f0-9]{64}$/).optional(),
      }).strict().parse(input);
      const opId = operationIdentity(scope, parsed.operationKey);
      const requestDigest = digest({ actorId: scope.actorId, ...parsed });
      const prior = this.operations.get(opId);
      if (prior) {
        if (prior.requestDigest !== requestDigest) throw new Error("external-edit-operation-key-reused");
        return prior.value;
      }
      const current = this.handoffs.get(parsed.handoffId);
      if (!current || current.customerId !== scope.customerId || current.sessionId !== scope.sessionId) {
        throw new Error("external-edit-handoff-not-found");
      }
      if (!allowedTransitions[current.status].has(parsed.status)) {
        throw new Error("external-edit-transition-not-allowed");
      }
      if (parsed.status === "ready" && !parsed.packageDigest && !current.packageDigest) {
        throw new Error("external-edit-package-digest-required");
      }
      const value = ExternalEditHandoffSchema.parse({
        ...current,
        status: parsed.status,
        packageDigest: parsed.packageDigest ?? current.packageDigest,
        updatedAt: this.now(),
      });
      await this.appendEvent({
        eventId: randomUUID(),
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        actorId: scope.actorId,
        operationKey: parsed.operationKey,
        requestDigest,
        value,
      });
      return value;
    });
  }

  async cancel(scope: StudioScope, handoffId: string, actorId: string): Promise<ExternalEditHandoff> {
    if (actorId !== scope.actorId) throw new Error("external-edit-actor-mismatch");
    return this.transition(scope, {
      handoffId,
      status: "cancelled",
      operationKey: `cancel:${handoffId}:${actorId}`,
    });
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.serial.then(operation, operation);
    this.serial = result.then(() => undefined, () => undefined);
    return result;
  }

  private async ensureLoaded(): Promise<void> {
    if (!this.loadPromise) this.loadPromise = this.load();
    await this.loadPromise;
  }

  private async load(): Promise<void> {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const rootInfo = await lstat(this.root);
    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) throw new Error("external-edit-store-root-invalid");
    await realpath(this.root);
    try {
      const handle = await open(this.eventPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const info = await handle.stat();
        if (!info.isFile() || info.nlink !== 1) throw new Error("external-edit-store-file-invalid");
        const content = await handle.readFile("utf8");
        for (const line of content.split("\n").filter(Boolean)) this.applyEvent(EventSchema.parse(JSON.parse(line)));
      } finally {
        await handle.close();
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
  }

  private async appendEvent(eventValue: HandoffEvent): Promise<void> {
    const event = EventSchema.parse(eventValue);
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const handle = await open(
      this.eventPath,
      constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | (constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.nlink !== 1) throw new Error("external-edit-store-file-invalid");
      await handle.chmod(0o600);
      await handle.writeFile(`${JSON.stringify(event)}\n`, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    this.applyEvent(event);
  }

  private applyEvent(event: HandoffEvent): void {
    if (event.value.customerId !== event.customerId || event.value.sessionId !== event.sessionId) {
      throw new Error("external-edit-event-scope-mismatch");
    }
    const opId = operationIdentity({ actorId: event.actorId, customerId: event.customerId, sessionId: event.sessionId }, event.operationKey);
    const existingOperation = this.operations.get(opId);
    if (existingOperation) {
      if (existingOperation.requestDigest !== event.requestDigest) throw new Error("external-edit-operation-key-reused");
      throw new Error("external-edit-duplicate-operation-event");
    }
    const current = this.handoffs.get(event.value.id);
    if (current && (!allowedTransitions[current.status].has(event.value.status)
      || current.customerId !== event.value.customerId
      || current.sessionId !== event.value.sessionId
      || current.parentDraftId !== event.value.parentDraftId)) {
      throw new Error("external-edit-event-transition-invalid");
    }
    this.handoffs.set(event.value.id, event.value);
    this.operations.set(opId, { requestDigest: event.requestDigest, value: event.value });
  }
}
