import type {
  VaultAssetRecord,
  VaultPendingUploadRecord,
} from "@/domain/supervision/contracts";
import type { SupervisionRepository } from "./repository";
import type { VaultStorage } from "./vault";

const DEFAULT_RETENTION_BATCH_LIMIT = 100;

function defaultCreateId(): string {
  return crypto.randomUUID();
}

export type PurgeExpiredVaultAssetsInput = Readonly<{
  now: string;
  repository: SupervisionRepository;
  vault: VaultStorage;
  tenantId?: string;
  limit?: number;
  createId?: () => string;
}>;

export type RetentionJobResult = Readonly<{
  processed: number;
  deleted: number;
  failed: number;
}>;

async function recordFailure(
  repository: SupervisionRepository,
  asset: VaultAssetRecord | VaultPendingUploadRecord,
  now: string,
): Promise<void> {
  await repository.upsertAlert({
    tenantId: asset.tenantId,
    customerId: asset.customerId,
    employeeUserId: asset.uploadedByUserId,
    detectedByUserId: null,
    kind: "retention_failed",
    status: "open",
    resolutionNote: "原始资料自动删除失败，请人工检查保留任务。",
    createdAt: now,
  });
}

/**
 * Deletes the private object first, then atomically tombstones metadata and
 * appends its safe audit. A committed-but-unacknowledged finalization is read
 * back before any failure alert is raised.
 */
export async function purgeExpiredVaultAssets(
  input: PurgeExpiredVaultAssetsInput,
): Promise<RetentionJobResult> {
  const tenantIds = input.tenantId
    ? [input.tenantId]
    : await input.repository.listVaultTenantIds();
  const limit = input.limit ?? DEFAULT_RETENTION_BATCH_LIMIT;
  const createId = input.createId ?? defaultCreateId;
  let processed = 0;
  let deleted = 0;
  let failed = 0;

  for (const tenantId of tenantIds) {
    let assetCursor: { deleteAfter: string; id: string } | undefined;
    while (true) {
      const assets = await input.repository.listExpiredVaultAssets({
        tenantId,
        now: input.now,
        limit,
        after: assetCursor,
      });
      if (assets.length === 0) break;
      for (const asset of assets) {
        processed += 1;
        try {
          await input.vault.deleteObject(asset.vaultKey);
        } catch {
          failed += 1;
          await recordFailure(input.repository, asset, input.now);
          continue;
        }

        try {
          await input.repository.finalizeVaultAssetDeletion({
            tenantId: asset.tenantId,
            vaultAssetId: asset.id,
            deletedAt: input.now,
            audit: {
              id: `audit-${createId()}`,
              tenantId: asset.tenantId,
              actorUserId: null,
              action: "vault.deleted",
              targetKind: "vault_asset",
              targetId: asset.id,
              occurredAt: input.now,
              createdAt: input.now,
            },
          });
          deleted += 1;
        } catch {
          const recovered = await input.repository.getVaultAsset({
            tenantId: asset.tenantId,
            vaultAssetId: asset.id,
          });
          if (recovered?.deletedAt !== undefined) {
            deleted += 1;
            continue;
          }
          failed += 1;
          await recordFailure(input.repository, asset, input.now);
        }
      }
      const last = assets.at(-1)!;
      assetCursor = { deleteAfter: last.deleteAfter, id: last.id };
    }

    let pendingCursor: { deleteAfter: string; id: string } | undefined;
    while (true) {
      const pendingUploads = await input.repository.listExpiredVaultPendingUploads({
        tenantId,
        now: input.now,
        limit,
        after: pendingCursor,
      });
      if (pendingUploads.length === 0) break;
      for (const pending of pendingUploads) {
        processed += 1;
        try {
          await input.vault.deleteObject(pending.vaultKey);
          await input.repository.resolveVaultPendingUpload({
            tenantId: pending.tenantId,
            pendingUploadId: pending.id,
          });
          deleted += 1;
        } catch {
          const recovered = await input.repository.getVaultPendingUpload({
            tenantId: pending.tenantId,
            pendingUploadId: pending.id,
          });
          if (!recovered) {
            deleted += 1;
            continue;
          }
          failed += 1;
          await recordFailure(input.repository, pending, input.now);
        }
      }
      const last = pendingUploads.at(-1)!;
      pendingCursor = { deleteAfter: last.deleteAfter, id: last.id };
    }
  }

  return { processed, deleted, failed };
}
