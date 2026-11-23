import type {
  VaultAssetKind,
  VaultAssetRecord,
  VaultPendingUploadRecord,
} from "@/domain/supervision/contracts";
import { calculateDeleteAfter } from "@/domain/supervision/retention";
import type { CurrentUser } from "@/server/auth/current-user";
import {
  VaultDailyQuotaExceededError,
  type SupervisionRepository,
} from "./repository";

export type VaultUploadLimits = Readonly<{
  maxFileBytes: number;
  maxFilesPerRequest: number;
  maxTenantBytesPerDay: number;
}>;

export const DEFAULT_VAULT_UPLOAD_LIMITS: VaultUploadLimits = Object.freeze({
  maxFileBytes: 10 * 1024 * 1024,
  maxFilesPerRequest: 5,
  maxTenantBytesPerDay: 100 * 1024 * 1024,
});

/** Five maximum-size images plus one MiB for multipart field/header overhead. */
export const MAX_VAULT_UPLOAD_REQUEST_BYTES = (
  DEFAULT_VAULT_UPLOAD_LIMITS.maxFileBytes
  * DEFAULT_VAULT_UPLOAD_LIMITS.maxFilesPerRequest
) + (1024 * 1024);

export type PrivateTextVaultWrite = Readonly<{
  tenantId: string;
  customerId: string;
  uploadedByUserId: string;
  kind: Extract<VaultAssetKind, "customer_identity" | "chat_text">;
  vaultKey: string;
  content: string;
  deleteAfter: string;
}>;

export interface PrivateTextVault {
  putText(input: PrivateTextVaultWrite): Promise<void>;
  deleteText(vaultKey: string): Promise<void>;
}

export type VaultObjectWrite = Readonly<{
  vaultKey: string;
  body: Uint8Array;
  contentType: "image/jpeg" | "image/png" | "image/webp" | string;
}>;

export type VaultObject = Readonly<{
  body: Uint8Array<ArrayBuffer> | ReadableStream<Uint8Array<ArrayBuffer>>;
  contentType: string;
  byteSize: number;
}>;

/** Private object-store port shared by text records, product images, owner reads, and retention. */
export interface VaultStorage extends PrivateTextVault {
  putObject(input: VaultObjectWrite): Promise<void>;
  getObject(vaultKey: string): Promise<VaultObject | undefined>;
  deleteObject(vaultKey: string): Promise<void>;
}

export type VaultErrorCode =
  | "VAULT_CUSTOMER_NOT_FOUND"
  | "VAULT_ASSET_NOT_FOUND"
  | "VAULT_EMPLOYEE_REQUIRED"
  | "INVALID_VAULT_UPLOAD"
  | "UNSUPPORTED_VAULT_IMAGE"
  | "VAULT_FILE_TOO_LARGE"
  | "VAULT_REQUEST_LIMIT_EXCEEDED"
  | "VAULT_DAILY_LIMIT_EXCEEDED"
  | "VAULT_STORAGE_UNAVAILABLE";

export class VaultOperationError extends Error {
  constructor(
    readonly code: VaultErrorCode,
    readonly status: number,
  ) {
    super(code);
    this.name = "VaultOperationError";
  }
}

export class VaultUploadError extends VaultOperationError {
  constructor(code: Exclude<VaultErrorCode, "VAULT_ASSET_NOT_FOUND">, status: number) {
    super(code, status);
    this.name = "VaultUploadError";
  }
}

export function vaultErrorResponse(error: unknown): Response | undefined {
  if (!(error instanceof VaultOperationError)) return undefined;
  return Response.json({ error: error.code }, {
    status: error.status,
    headers: { "Cache-Control": "no-store" },
  });
}

export function requireVaultStorage(vault: PrivateTextVault): VaultStorage {
  const candidate = vault as Partial<VaultStorage>;
  if (
    typeof candidate.putObject !== "function"
    || typeof candidate.getObject !== "function"
    || typeof candidate.deleteObject !== "function"
  ) {
    throw new VaultOperationError("VAULT_STORAGE_UNAVAILABLE", 503);
  }
  return candidate as VaultStorage;
}

export function detectVaultImageMediaType(
  bytes: Uint8Array,
): "image/jpeg" | "image/png" | "image/webp" | undefined {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  const png = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  if (bytes.length >= png.length && png.every((byte, index) => bytes[index] === byte)) {
    return "image/png";
  }
  if (
    bytes.length >= 12
    && String.fromCharCode(...bytes.slice(0, 4)) === "RIFF"
    && String.fromCharCode(...bytes.slice(8, 12)) === "WEBP"
  ) {
    return "image/webp";
  }
  return undefined;
}

function defaultNow(): string {
  return new Date().toISOString();
}

function defaultCreateId(): string {
  return crypto.randomUUID();
}

function validateLimits(limits: VaultUploadLimits): void {
  if (
    !Number.isSafeInteger(limits.maxFileBytes)
    || limits.maxFileBytes <= 0
    || !Number.isSafeInteger(limits.maxFilesPerRequest)
    || limits.maxFilesPerRequest <= 0
    || !Number.isSafeInteger(limits.maxTenantBytesPerDay)
    || limits.maxTenantBytesPerDay <= 0
  ) {
    throw new RangeError("vault upload limits must be positive integers");
  }
}

async function opaquePathSegment(label: "tenant" | "customer", value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify([label, value])),
  );
  return [...new Uint8Array(digest)]
    .slice(0, 12)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function utcDayWindow(timestamp: string): { start: string; end: string } {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) throw new RangeError("now must be an ISO timestamp");
  const start = new Date(Date.UTC(
    date.getUTCFullYear(),
    date.getUTCMonth(),
    date.getUTCDate(),
  ));
  return {
    start: start.toISOString(),
    end: new Date(start.getTime() + 24 * 60 * 60 * 1_000).toISOString(),
  };
}

export type UploadVaultImagesInput = Readonly<{
  actor: CurrentUser;
  customerId: string;
  files: readonly File[];
  repository: SupervisionRepository;
  vault: VaultStorage;
  limits?: VaultUploadLimits;
  now?: () => string;
  createId?: () => string;
}>;

type VaultUploadCandidate = Readonly<{
  record: VaultAssetRecord;
  body: Uint8Array;
  contentType: "image/jpeg" | "image/png" | "image/webp";
}>;

function pendingUploadFor(candidate: VaultUploadCandidate, uploadDay: string): VaultPendingUploadRecord {
  return {
    id: candidate.record.id,
    tenantId: candidate.record.tenantId,
    customerId: candidate.record.customerId,
    uploadedByUserId: candidate.record.uploadedByUserId,
    vaultKey: candidate.record.vaultKey,
    byteSize: candidate.record.byteSize ?? 0,
    uploadedAt: candidate.record.uploadedAt,
    deleteAfter: candidate.record.deleteAfter,
    uploadDay,
    version: 1,
    createdAt: candidate.record.createdAt,
  };
}

async function recordPendingUploadFailure(
  repository: SupervisionRepository,
  pending: VaultPendingUploadRecord,
  now: string,
): Promise<void> {
  await repository.upsertAlert({
    tenantId: pending.tenantId,
    customerId: pending.customerId,
    employeeUserId: pending.uploadedByUserId,
    detectedByUserId: null,
    kind: "retention_failed",
    status: "open",
    resolutionNote: "保险柜上传清理待重试，请人工检查保留任务。",
    createdAt: now,
  });
}

async function cleanupPendingCandidates(input: {
  candidates: readonly VaultUploadCandidate[];
  repository: SupervisionRepository;
  vault: VaultStorage;
  now: string;
}): Promise<void> {
  for (const candidate of input.candidates) {
    const pending = pendingUploadFor(candidate, candidate.record.uploadedAt.slice(0, 10));
    try {
      await input.vault.deleteObject(candidate.record.vaultKey);
    } catch {
      await recordPendingUploadFailure(input.repository, pending, input.now);
      continue;
    }
    try {
      await input.repository.resolveVaultPendingUpload({
        tenantId: candidate.record.tenantId,
        pendingUploadId: candidate.record.id,
      });
    } catch {
      await recordPendingUploadFailure(input.repository, pending, input.now);
    }
  }
}

export async function uploadVaultImages(
  input: UploadVaultImagesInput,
): Promise<VaultAssetRecord[]> {
  const limits = input.limits ?? DEFAULT_VAULT_UPLOAD_LIMITS;
  validateLimits(limits);
  if (input.actor.role !== "employee") {
    throw new VaultUploadError("VAULT_EMPLOYEE_REQUIRED", 403);
  }
  if (input.files.length === 0) throw new VaultUploadError("INVALID_VAULT_UPLOAD", 400);
  if (input.files.length > limits.maxFilesPerRequest) {
    throw new VaultUploadError("VAULT_REQUEST_LIMIT_EXCEEDED", 413);
  }

  const customer = await input.repository.getCustomer({
    tenantId: input.actor.tenantId,
    customerId: input.customerId,
  });
  if (!customer || customer.employeeUserId !== input.actor.accountUserId) {
    throw new VaultUploadError("VAULT_CUSTOMER_NOT_FOUND", 404);
  }

  const uploadedAt = (input.now ?? defaultNow)();
  const dayWindow = utcDayWindow(uploadedAt);
  const createId = input.createId ?? defaultCreateId;
  const [tenantPrefix, customerPrefix] = await Promise.all([
    opaquePathSegment("tenant", input.actor.tenantId),
    opaquePathSegment("customer", customer.id),
  ]);
  const candidates: VaultUploadCandidate[] = [];

  for (const file of input.files) {
    if (file.size > limits.maxFileBytes) {
      throw new VaultUploadError("VAULT_FILE_TOO_LARGE", 413);
    }
    const body = new Uint8Array(await file.arrayBuffer());
    if (body.byteLength > limits.maxFileBytes) {
      throw new VaultUploadError("VAULT_FILE_TOO_LARGE", 413);
    }
    const contentType = detectVaultImageMediaType(body);
    if (!contentType || file.type.toLowerCase() !== contentType) {
      throw new VaultUploadError("UNSUPPORTED_VAULT_IMAGE", 415);
    }
    const id = createId();
    candidates.push({
      body,
      contentType,
      record: {
        id,
        tenantId: input.actor.tenantId,
        customerId: customer.id,
        uploadedByUserId: input.actor.accountUserId,
        kind: "product_image",
        vaultKey: `private/${tenantPrefix}/${customerPrefix}/${id}`,
        byteSize: body.byteLength,
        uploadedAt,
        deleteAfter: calculateDeleteAfter(uploadedAt),
        version: 1,
        createdAt: uploadedAt,
      },
    });
  }

  const pendingUploads = candidates.map((candidate) => (
    pendingUploadFor(candidate, dayWindow.start.slice(0, 10))
  ));
  try {
    await input.repository.reserveVaultUploadBundle({
      pendingUploads,
      dailyByteLimit: limits.maxTenantBytesPerDay,
    });
  } catch (error) {
    let recovered: Array<VaultPendingUploadRecord | undefined> = [];
    try {
      recovered = await Promise.all(pendingUploads.map((pending) => (
        input.repository.getVaultPendingUpload({
          tenantId: pending.tenantId,
          pendingUploadId: pending.id,
        })
      )));
    } catch {
      throw error;
    }
    if (!recovered.every((record, index) => (
      record?.vaultKey === pendingUploads[index]!.vaultKey
      && record.byteSize === pendingUploads[index]!.byteSize
    ))) {
      if (error instanceof VaultDailyQuotaExceededError) {
        throw new VaultUploadError("VAULT_DAILY_LIMIT_EXCEEDED", 429);
      }
      throw error;
    }
  }

  for (const candidate of candidates) {
    try {
      await input.vault.putObject({
        vaultKey: candidate.record.vaultKey,
        body: candidate.body,
        contentType: candidate.contentType,
      });
    } catch (error) {
      let confirmed = false;
      try {
        const object = await input.vault.getObject(candidate.record.vaultKey);
        confirmed = object?.byteSize === candidate.body.byteLength
          && object.contentType === candidate.contentType;
      } catch {
        confirmed = false;
      }
      if (confirmed) continue;
      await cleanupPendingCandidates({
        candidates,
        repository: input.repository,
        vault: input.vault,
        now: uploadedAt,
      });
      throw error;
    }
  }

  try {
    return await input.repository.createVaultUploadBundle({
      assets: candidates.map((candidate) => candidate.record),
      audits: candidates.map((candidate) => ({
        id: `audit-${createId()}`,
        tenantId: input.actor.tenantId,
        actorUserId: input.actor.accountUserId,
        action: "vault.uploaded",
        targetKind: "vault_asset",
        targetId: candidate.record.id,
        occurredAt: uploadedAt,
        createdAt: uploadedAt,
      })),
    });
  } catch (error) {
    let recovered: Array<VaultAssetRecord | undefined> = [];
    try {
      recovered = await Promise.all(candidates.map((candidate) => (
        input.repository.getVaultAsset({
          tenantId: candidate.record.tenantId,
          vaultAssetId: candidate.record.id,
        })
      )));
    } catch {
      recovered = [];
    }
    const committedKeys = new Set(recovered
      .filter((record, index): record is VaultAssetRecord => (
        record?.vaultKey === candidates[index]!.record.vaultKey
      ))
      .map((record) => record.vaultKey));
    if (committedKeys.size === candidates.length) return recovered as VaultAssetRecord[];

    let pendingRecovery: Array<VaultPendingUploadRecord | undefined> = [];
    try {
      pendingRecovery = await Promise.all(pendingUploads.map((pending) => (
        input.repository.getVaultPendingUpload({
          tenantId: pending.tenantId,
          pendingUploadId: pending.id,
        })
      )));
    } catch {
      for (const pending of pendingUploads) {
        await recordPendingUploadFailure(input.repository, pending, uploadedAt);
      }
      throw error;
    }
    const cleanupCandidates = candidates.filter((candidate, index) => (
      !committedKeys.has(candidate.record.vaultKey)
      && pendingRecovery[index]?.vaultKey === candidate.record.vaultKey
    ));
    await cleanupPendingCandidates({
      candidates: cleanupCandidates,
      repository: input.repository,
      vault: input.vault,
      now: uploadedAt,
    });
    throw error;
  }
}

export type ReadOwnerVaultAssetInput = Readonly<{
  actor: CurrentUser;
  vaultAssetId: string;
  repository: SupervisionRepository;
  vault: VaultStorage;
  now?: () => string;
  createId?: () => string;
}>;

export async function readOwnerVaultAsset(
  input: ReadOwnerVaultAssetInput,
): Promise<{ asset: VaultAssetRecord; object: VaultObject }> {
  if (input.actor.role !== "owner") throw new VaultOperationError("VAULT_ASSET_NOT_FOUND", 404);
  const asset = await input.repository.getVaultAsset({
    tenantId: input.actor.tenantId,
    vaultAssetId: input.vaultAssetId,
  });
  const viewedAt = (input.now ?? defaultNow)();
  if (!asset || asset.deletedAt !== undefined || asset.deleteAfter <= viewedAt) {
    throw new VaultOperationError("VAULT_ASSET_NOT_FOUND", 404);
  }
  const object = await input.vault.getObject(asset.vaultKey);
  if (!object) throw new VaultOperationError("VAULT_ASSET_NOT_FOUND", 404);

  const createId = input.createId ?? defaultCreateId;
  await input.repository.appendAudit({
    id: createId(),
    tenantId: input.actor.tenantId,
    actorUserId: input.actor.accountUserId,
    action: "vault.viewed",
    targetKind: "vault_asset",
    targetId: asset.id,
    occurredAt: viewedAt,
    createdAt: viewedAt,
  });
  return { asset, object };
}
