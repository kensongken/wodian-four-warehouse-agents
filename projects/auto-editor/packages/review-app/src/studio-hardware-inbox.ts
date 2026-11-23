import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import type { IncomingMessage } from "node:http";
import { join } from "node:path";

import type { MaterialCard, StudioContentType } from "@wearable/contracts";
import type { StudioScope } from "@wearable/mobile-agent";

import {
  readStudioMaterialMultipart,
  type HardwareCaptureMetadata,
  type StudioMaterialImportApi,
  type StudioMaterialUpload,
} from "./studio-material-import";

export type StudioHardwareInboxStatus = "received" | "failed" | "delivered";

export type StudioHardwareInboxItem = Readonly<{
  id: string;
  fileName: string;
  byteLength: number;
  contentType: StudioContentType;
  captureMetadata: HardwareCaptureMetadata;
  status: StudioHardwareInboxStatus;
  receivedAt: string;
  deliveredAt: string | null;
  materialCardId: string | null;
  lastError: string | null;
}>;

type HardwareBinding = Readonly<{
  pairingId: string;
  deviceId: string;
  deviceName: string;
}>;

type InboxRecord = StudioHardwareInboxItem & Readonly<{
  schemaVersion: 1;
  customerId: string;
  sessionId: string;
  pairingId: string;
  gatewayDeviceId: string;
  operationKey: string;
  sourceDigest: string;
}>;

export class StudioHardwareInboxStore {
  constructor(
    private readonly root: string,
    private readonly materialImport: StudioMaterialImportApi,
    private readonly now: () => string = () => new Date().toISOString(),
  ) {
    if (!root.trim()) throw new Error("hardware-inbox-root-required");
  }

  async receiveMultipart(
    request: IncomingMessage,
    scope: StudioScope,
    hardware: HardwareBinding,
  ): Promise<StudioHardwareInboxItem> {
    const upload = await readStudioMaterialMultipart(request);
    if (!upload.captureMetadata) throw new Error("hardware-capture-metadata-required");
    const sourceDigest = digest(upload.bytes);
    const id = `hardware_inbox_${digest(Buffer.from(
      `${scope.customerId}\0${scope.sessionId}\0${hardware.deviceId}\0${upload.operationKey}`,
    ))}`;
    const record: InboxRecord = {
      schemaVersion: 1,
      id,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      pairingId: hardware.pairingId,
      gatewayDeviceId: hardware.deviceId,
      operationKey: upload.operationKey,
      sourceDigest,
      fileName: upload.fileName,
      byteLength: upload.bytes.byteLength,
      contentType: upload.contentType,
      captureMetadata: upload.captureMetadata,
      status: "received",
      receivedAt: this.now(),
      deliveredAt: null,
      materialCardId: null,
      lastError: null,
    };
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const target = join(this.root, id);
    const temporary = join(this.root, `.incoming-${randomUUID()}`);
    await mkdir(temporary, { mode: 0o700 });
    try {
      await Promise.all([
        writeFile(join(temporary, "source.bin"), upload.bytes, { flag: "wx", mode: 0o600 }),
        writeFile(join(temporary, "record.json"), `${JSON.stringify(record)}\n`, { flag: "wx", mode: 0o600 }),
      ]);
      try {
        await rename(temporary, target);
      } catch (error) {
        const existing = await this.readRecord(id);
        if (!sameDelivery(existing, record)) throw new Error("hardware-inbox-operation-key-reused");
        return publicItem(existing);
      }
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
    return publicItem(record);
  }

  async list(scope: StudioScope, hardware: HardwareBinding): Promise<readonly StudioHardwareInboxItem[]> {
    let names: string[];
    try {
      names = await import("node:fs/promises").then(({ readdir }) => readdir(this.root));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
    const records = await Promise.all(names
      .filter((name) => name.startsWith("hardware_inbox_"))
      .map(async (name) => this.readRecord(name)));
    return records
      .filter((record) => ownsRecord(record, scope, hardware))
      .sort((left, right) => right.receivedAt.localeCompare(left.receivedAt))
      .map(publicItem);
  }

  async confirm(
    id: string,
    scope: StudioScope,
    hardware: HardwareBinding,
  ): Promise<Readonly<{ item: StudioHardwareInboxItem; material: MaterialCard }>> {
    if (!/^hardware_inbox_[a-f0-9]{64}$/u.test(id)) throw new Error("hardware-inbox-item-not-found");
    let record = await this.readRecord(id);
    if (!ownsRecord(record, scope, hardware)) throw new Error("hardware-inbox-item-not-found");
    const bytes = await readFile(join(this.root, id, "source.bin"));
    if (digest(bytes) !== record.sourceDigest || bytes.byteLength !== record.byteLength) {
      throw new Error("hardware-inbox-source-integrity-failed");
    }
    const upload: StudioMaterialUpload = {
      bytes,
      fileName: record.fileName,
      operationKey: record.operationKey,
      contentType: record.contentType,
      captureMetadata: record.captureMetadata,
    };
    try {
      const material = await this.materialImport.importHardwareUpload(upload, scope, hardware);
      record = {
        ...record,
        status: "delivered",
        deliveredAt: record.deliveredAt ?? this.now(),
        materialCardId: material.id,
        lastError: null,
      };
      await this.writeRecord(record);
      return { item: publicItem(record), material };
    } catch (error) {
      record = {
        ...record,
        status: "failed",
        lastError: stableErrorCode(error),
      };
      await this.writeRecord(record);
      throw error;
    }
  }

  private async readRecord(id: string): Promise<InboxRecord> {
    let value: unknown;
    try {
      value = JSON.parse(await readFile(join(this.root, id, "record.json"), "utf8")) as unknown;
    } catch {
      throw new Error("hardware-inbox-item-not-found");
    }
    if (!isInboxRecord(value) || value.id !== id) throw new Error("hardware-inbox-record-invalid");
    return value;
  }

  private async writeRecord(record: InboxRecord): Promise<void> {
    const directory = join(this.root, record.id);
    const temporary = join(directory, `record.${randomUUID()}.tmp`);
    try {
      await writeFile(temporary, `${JSON.stringify(record)}\n`, { flag: "wx", mode: 0o600 });
      await rename(temporary, join(directory, "record.json"));
    } finally {
      await rm(temporary, { force: true });
    }
  }
}

function ownsRecord(record: InboxRecord, scope: StudioScope, hardware: HardwareBinding): boolean {
  return record.customerId === scope.customerId &&
    record.sessionId === scope.sessionId &&
    record.pairingId === hardware.pairingId &&
    record.gatewayDeviceId === hardware.deviceId;
}

function sameDelivery(left: InboxRecord, right: InboxRecord): boolean {
  return left.customerId === right.customerId && left.sessionId === right.sessionId &&
    left.pairingId === right.pairingId && left.gatewayDeviceId === right.gatewayDeviceId &&
    left.operationKey === right.operationKey && left.sourceDigest === right.sourceDigest &&
    left.fileName === right.fileName && left.byteLength === right.byteLength &&
    left.contentType === right.contentType &&
    JSON.stringify(left.captureMetadata) === JSON.stringify(right.captureMetadata);
}

function publicItem(record: InboxRecord): StudioHardwareInboxItem {
  return {
    id: record.id,
    fileName: record.fileName,
    byteLength: record.byteLength,
    contentType: record.contentType,
    captureMetadata: record.captureMetadata,
    status: record.status,
    receivedAt: record.receivedAt,
    deliveredAt: record.deliveredAt,
    materialCardId: record.materialCardId,
    lastError: record.lastError,
  };
}

function digest(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function stableErrorCode(error: unknown): string {
  const message = error instanceof Error ? error.message : "hardware-inbox-confirm-failed";
  return /^[a-z0-9-]+/u.exec(message)?.[0] ?? "hardware-inbox-confirm-failed";
}

function isInboxRecord(value: unknown): value is InboxRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  const capture = record.captureMetadata;
  return record.schemaVersion === 1 && typeof record.id === "string" &&
    typeof record.customerId === "string" && typeof record.sessionId === "string" &&
    typeof record.pairingId === "string" && typeof record.gatewayDeviceId === "string" &&
    typeof record.operationKey === "string" && /^[a-f0-9]{64}$/u.test(String(record.sourceDigest)) &&
    typeof record.fileName === "string" && Number.isSafeInteger(record.byteLength) &&
    typeof record.contentType === "string" &&
    (record.status === "received" || record.status === "failed" || record.status === "delivered") &&
    typeof record.receivedAt === "string" &&
    (record.deliveredAt === null || typeof record.deliveredAt === "string") &&
    (record.materialCardId === null || typeof record.materialCardId === "string") &&
    (record.lastError === null || typeof record.lastError === "string") &&
    !!capture && typeof capture === "object" && !Array.isArray(capture) &&
    typeof (capture as Record<string, unknown>).captureId === "string" &&
    typeof (capture as Record<string, unknown>).deviceId === "string" &&
    typeof (capture as Record<string, unknown>).deviceName === "string" &&
    typeof (capture as Record<string, unknown>).capturedAt === "string" &&
    typeof (capture as Record<string, unknown>).simulated === "boolean";
}
