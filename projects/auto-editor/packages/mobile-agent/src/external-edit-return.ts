import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

import { DraftPackageSchema, ExternalEditHandoffSchema, ExternalEditReturnCandidateSchema, type DraftPackage, type ExternalEditReturnCandidate } from "@wearable/contracts";
import { z } from "zod";

import { ExternalEditHandoffService } from "./external-edit-handoff";
import { ExternalEditInbox } from "./external-edit-inbox";
import type { PrivateContentStudio } from "./private-content-studio";
import type { StudioScope } from "./studio-capabilities";

const ReturnReceiptSchema = z.object({
  receiptId: z.string().min(1),
  actorId: z.string().min(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  handoffId: z.string().min(1),
  candidateId: z.string().min(1),
  draftId: z.string().min(1),
  operationKey: z.string().min(1),
  createdAt: z.string().datetime(),
}).strict();

type ReturnReceipt = z.infer<typeof ReturnReceiptSchema>;
const IgnoredReceiptSchema = z.object({
  actorId: z.string().min(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  handoffId: z.string().min(1),
  candidateId: z.string().min(1),
  operationKey: z.string().min(1),
  createdAt: z.string().datetime(),
}).strict();
type IgnoredReceipt = z.infer<typeof IgnoredReceiptSchema>;
type ReturnOptions = Readonly<{
  root: string;
  inbox: ExternalEditInbox;
  handoffs: ExternalEditHandoffService;
  studio: Pick<PrivateContentStudio, "registerExternalEditReturnDraft" | "getDraft">;
  now?: () => string;
}>;

function hash(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function operationKeyForTransition(operationKey: string): string {
  return `external-return:${hash(operationKey).slice(0, 48)}`;
}

export class ExternalEditReturnService {
  private readonly root: string;
  private readonly receiptPath: string;
  private readonly ignoredPath: string;
  private readonly inbox: ExternalEditInbox;
  private readonly handoffs: ExternalEditHandoffService;
  private readonly studio: ReturnOptions["studio"];
  private readonly now: () => string;
  private readonly accepted = new Map<string, ReturnReceipt>();
  private readonly ignored = new Map<string, IgnoredReceipt>();
  private loadPromise: Promise<void> | null = null;
  private serial = Promise.resolve();

  constructor(options: ReturnOptions) {
    this.root = resolve(options.root);
    this.receiptPath = join(this.root, "returns.jsonl");
    this.ignoredPath = join(this.root, "ignored.jsonl");
    this.inbox = options.inbox;
    this.handoffs = options.handoffs;
    this.studio = options.studio;
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async listCandidates(scope: StudioScope, handoffId: string): Promise<ExternalEditReturnCandidate[]> {
    await this.enqueue(async () => this.ensureLoaded());
    const handoff = await this.handoffs.get(scope, handoffId);
    if (!["editing", "candidate-found", "degraded"].includes(handoff.status)) {
      if (handoff.status === "returned") return [];
      throw new Error("external-edit-handoff-not-awaiting-return");
    }
    const candidates = await this.inbox.scan(handoff);
    if (candidates.length > 0 && handoff.status !== "candidate-found") {
      const detectionKey = `candidate-found:${hash(candidates.map((candidate) => candidate.id).sort().join("\0")).slice(0, 48)}`;
      await this.handoffs.transition(scope, {
        handoffId,
        status: "candidate-found",
        operationKey: detectionKey,
      });
    }
    return candidates.map((candidate) => this.ignored.has(`${handoffId}\0${candidate.id}`)
      ? ExternalEditReturnCandidateSchema.parse({ ...candidate, status: "ignored" })
      : candidate);
  }

  async ignore(scope: StudioScope, handoffId: string, candidateId: string, operationKey: string): Promise<void> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      const handoff = await this.handoffs.get(scope, handoffId);
      const candidates = await this.inbox.scan(handoff);
      const candidate = candidates.find((item) => item.id === candidateId);
      if (!candidate) throw new Error("external-edit-candidate-not-found");
      const key = `${handoffId}\0${candidateId}`;
      const prior = this.ignored.get(key);
      if (prior) {
        if (prior.actorId !== scope.actorId || prior.operationKey !== operationKey) {
          throw new Error("external-edit-ignore-operation-conflict");
        }
        return;
      }
      if (this.accepted.has(handoffId)) throw new Error("external-edit-already-returned");
      const receipt = IgnoredReceiptSchema.parse({
        actorId: scope.actorId,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        handoffId,
        candidateId,
        operationKey,
        createdAt: this.now(),
      });
      const handle = await open(
        this.ignoredPath,
        constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | (constants.O_NOFOLLOW ?? 0),
        0o600,
      );
      try {
        const info = await handle.stat();
        if (!info.isFile() || info.nlink !== 1) throw new Error("external-edit-return-store-file-invalid");
        await handle.chmod(0o600);
        await handle.writeFile(`${JSON.stringify(receipt)}\n`, "utf8");
        await handle.sync();
      } finally {
        await handle.close();
      }
      this.ignored.set(key, receipt);
    });
  }

  async accept(scope: StudioScope, handoffId: string, candidateId: string, operationKey: string): Promise<DraftPackage> {
    return this.enqueue(async () => {
      await this.ensureLoaded();
      const handoff = ExternalEditHandoffSchema.parse(await this.handoffs.get(scope, handoffId));
      const accepted = this.accepted.get(handoffId);
      if (accepted) {
        if (accepted.candidateId !== candidateId || accepted.actorId !== scope.actorId) {
          throw new Error("external-edit-already-returned");
        }
        const existingDraft = DraftPackageSchema.parse(await this.studio.getDraft(scope, accepted.draftId));
        const currentHandoff = await this.handoffs.get(scope, handoffId);
        if (currentHandoff.status !== "returned") {
          await this.handoffs.transition(scope, {
            handoffId,
            status: "returned",
            operationKey: operationKeyForTransition(accepted.operationKey),
          });
        }
        return existingDraft;
      }
      if (!["editing", "candidate-found", "degraded"].includes(handoff.status)) {
        throw new Error(handoff.status === "returned" ? "external-edit-already-returned" : "external-edit-handoff-not-awaiting-return");
      }
      const candidates = await this.inbox.scan(handoff);
      if (this.ignored.has(`${handoffId}\0${candidateId}`)) throw new Error("external-edit-candidate-ignored");
      const candidate = candidates.find((item) => item.id === candidateId);
      if (!candidate) throw new Error("external-edit-candidate-not-found");
      if (handoff.status !== "candidate-found") {
        await this.handoffs.transition(scope, {
          handoffId,
          status: "candidate-found",
          operationKey: `candidate-found:${hash(candidate.id).slice(0, 48)}`,
        });
      }
      const draft = await this.studio.registerExternalEditReturnDraft(scope, {
        handoff,
        candidate: ExternalEditReturnCandidateSchema.parse(candidate),
        operationKey,
        copyToOutput: async (destinationPath) => {
          await this.inbox.copyCandidateTo(handoff, candidate, destinationPath);
        },
      });
      const receipt = ReturnReceiptSchema.parse({
        receiptId: `external_return_${randomUUID()}`,
        actorId: scope.actorId,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        handoffId,
        candidateId,
        draftId: draft.id,
        operationKey,
        createdAt: this.now(),
      });
      await this.appendReceipt(receipt);
      await this.handoffs.transition(scope, {
        handoffId,
        status: "returned",
        operationKey: operationKeyForTransition(operationKey),
      });
      return draft;
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
    const info = await lstat(this.root);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("external-edit-return-store-root-invalid");
    await chmod(this.root, 0o700);
    try {
      const handle = await open(this.receiptPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const fileInfo = await handle.stat();
        if (!fileInfo.isFile() || fileInfo.nlink !== 1) throw new Error("external-edit-return-store-file-invalid");
        const text = await handle.readFile("utf8");
        for (const line of text.split("\n").filter(Boolean)) {
          const receipt = ReturnReceiptSchema.parse(JSON.parse(line));
          const prior = this.accepted.get(receipt.handoffId);
          if (prior && (prior.candidateId !== receipt.candidateId || prior.draftId !== receipt.draftId)) {
            throw new Error("external-edit-return-receipt-conflict");
          }
          this.accepted.set(receipt.handoffId, receipt);
        }
      } finally {
        await handle.close();
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    try {
      const handle = await open(this.ignoredPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const fileInfo = await handle.stat();
        if (!fileInfo.isFile() || fileInfo.nlink !== 1) throw new Error("external-edit-return-store-file-invalid");
        const text = await handle.readFile("utf8");
        for (const line of text.split("\n").filter(Boolean)) {
          const receipt = IgnoredReceiptSchema.parse(JSON.parse(line));
          this.ignored.set(`${receipt.handoffId}\0${receipt.candidateId}`, receipt);
        }
      } finally {
        await handle.close();
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  private async appendReceipt(receiptValue: ReturnReceipt): Promise<void> {
    const receipt = ReturnReceiptSchema.parse(receiptValue);
    const handle = await open(
      this.receiptPath,
      constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | (constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.nlink !== 1) throw new Error("external-edit-return-store-file-invalid");
      await handle.chmod(0o600);
      await handle.writeFile(`${JSON.stringify(receipt)}\n`, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    this.accepted.set(receipt.handoffId, receipt);
  }
}
