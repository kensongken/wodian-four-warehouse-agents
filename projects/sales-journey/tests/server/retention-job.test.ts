import { describe, expect, it } from "vitest";

import type { CustomerRecord, VaultAssetRecord } from "@/domain/supervision/contracts";
import { purgeExpiredVaultAssets } from "@/server/supervision/retention-job";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import type { FinalizeVaultAssetDeletionInput } from "@/server/supervision/repository";
import { TestVaultStorage } from "../support/test-vault-storage";

const NOW = "2026-09-30T00:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-c1",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-018",
  customerAlias: "客户代号 18",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T00:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: "2026-08-01T00:00:00.000Z",
};
const expiredAsset: VaultAssetRecord = {
  id: "a1",
  tenantId: "tenant-a",
  customerId: "customer-c1",
  uploadedByUserId: "employee-1",
  kind: "chat_text",
  vaultKey: "tenant-a/customer-c1/a1",
  byteSize: 24,
  uploadedAt: "2026-08-01T00:00:00.000Z",
  deleteAfter: "2026-08-31T00:00:00.000Z",
  version: 1,
  createdAt: "2026-08-01T00:00:00.000Z",
};

async function seededVault(): Promise<TestVaultStorage> {
  const vault = new TestVaultStorage();
  await vault.putText({
    tenantId: expiredAsset.tenantId,
    customerId: expiredAsset.customerId,
    uploadedByUserId: expiredAsset.uploadedByUserId,
    kind: "chat_text",
    vaultKey: expiredAsset.vaultKey,
    content: "原始客户聊天",
    deleteAfter: expiredAsset.deleteAfter,
  });
  return vault;
}

class FailingDeleteVault extends TestVaultStorage {
  override async deleteObject(): Promise<void> {
    throw new Error("object store unavailable");
  }
}

class CommitThenThrowRetentionRepository extends MemorySupervisionRepository {
  override async finalizeVaultAssetDeletion(
    input: FinalizeVaultAssetDeletionInput,
  ): Promise<void> {
    await super.finalizeVaultAssetDeletion(input);
    throw new Error("commit acknowledgement lost");
  }
}

describe("vault retention job", () => {
  it("deletes raw object and identity while preserving the business ledger", async () => {
    const repository = new MemorySupervisionRepository({
      seed: { customers: [customer], vaultAssets: [expiredAsset] },
    });
    const vault = await seededVault();

    const result = await purgeExpiredVaultAssets({ now: NOW, repository, vault });

    expect(result).toEqual({ processed: 1, deleted: 1, failed: 0 });
    expect(vault.deletedKeys).toEqual(["tenant-a/customer-c1/a1"]);
    await expect(repository.getCustomer({
      tenantId: "tenant-a",
      customerId: "customer-c1",
    })).resolves.toMatchObject({ customerNumber: "C-018", status: "pending" });
    await expect(repository.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "a1",
    })).resolves.toMatchObject({
      deletedAt: NOW,
      vaultKey: "deleted/a1",
    });
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: "a1",
    })).resolves.toEqual([
      expect.objectContaining({ action: "vault.deleted", actorUserId: null }),
    ]);

    const rerun = await purgeExpiredVaultAssets({ now: NOW, repository, vault });
    expect(rerun).toEqual({ processed: 0, deleted: 0, failed: 0 });
    expect(vault.deletedKeys).toEqual(["tenant-a/customer-c1/a1"]);
  });

  it("upserts one safe failure alert without marking the asset deleted", async () => {
    const repository = new MemorySupervisionRepository({
      createId: () => "alert-1",
      seed: { customers: [customer], vaultAssets: [expiredAsset] },
    });
    const vault = new FailingDeleteVault();

    await purgeExpiredVaultAssets({ now: NOW, repository, vault });
    await purgeExpiredVaultAssets({ now: NOW, repository, vault });

    const retainedAsset = await repository.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "a1",
    });
    expect(retainedAsset).toMatchObject({ vaultKey: expiredAsset.vaultKey });
    expect(retainedAsset?.deletedAt).toBeUndefined();
    const dashboard = await repository.getOwnerDashboard({ tenantId: "tenant-a", now: NOW });
    expect(dashboard.alerts).toEqual([
      expect.objectContaining({
        id: "alert-1",
        kind: "retention_failed",
        status: "open",
        version: 2,
        customerId: "customer-c1",
      }),
    ]);
    expect(JSON.stringify(dashboard.alerts)).not.toContain(expiredAsset.vaultKey);
    expect(JSON.stringify(dashboard.alerts)).not.toContain("object store unavailable");
  });

  it("recovers an ambiguous committed finalization without deleting the object twice", async () => {
    const repository = new CommitThenThrowRetentionRepository({
      seed: { customers: [customer], vaultAssets: [expiredAsset] },
    });
    const vault = await seededVault();

    const first = await purgeExpiredVaultAssets({ now: NOW, repository, vault });
    const second = await purgeExpiredVaultAssets({ now: NOW, repository, vault });

    expect(first).toEqual({ processed: 1, deleted: 1, failed: 0 });
    expect(second).toEqual({ processed: 0, deleted: 0, failed: 0 });
    expect(vault.deletedKeys).toEqual([expiredAsset.vaultKey]);
    expect((await repository.getOwnerDashboard({ tenantId: "tenant-a", now: NOW })).alerts)
      .toEqual([]);
  });

  it("drains every expired asset even when the first batch reaches one hundred", async () => {
    const assets = Array.from({ length: 101 }, (_, index): VaultAssetRecord => ({
      ...expiredAsset,
      id: `asset-${String(index).padStart(3, "0")}`,
      vaultKey: `private/asset-${String(index).padStart(3, "0")}`,
    }));
    const repository = new MemorySupervisionRepository({
      seed: { customers: [customer], vaultAssets: assets },
    });
    const vault = new TestVaultStorage();
    for (const asset of assets) {
      await vault.putText({
        tenantId: asset.tenantId,
        customerId: asset.customerId,
        uploadedByUserId: asset.uploadedByUserId,
        kind: "chat_text",
        vaultKey: asset.vaultKey,
        content: "raw",
        deleteAfter: asset.deleteAfter,
      });
    }

    await expect(purgeExpiredVaultAssets({ now: NOW, repository, vault }))
      .resolves.toEqual({ processed: 101, deleted: 101, failed: 0 });
    expect(vault.deletedKeys).toHaveLength(101);
    await expect(repository.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: NOW,
      limit: 500,
    })).resolves.toEqual([]);
  });

  it("writes exactly one deletion audit when two purge invocations overlap", async () => {
    const repository = new MemorySupervisionRepository({
      seed: { customers: [customer], vaultAssets: [expiredAsset] },
    });
    const vault = await seededVault();

    const results = await Promise.all([
      purgeExpiredVaultAssets({ now: NOW, repository, vault }),
      purgeExpiredVaultAssets({ now: NOW, repository, vault }),
    ]);

    expect(results.reduce((total, result) => total + result.deleted, 0)).toBeGreaterThanOrEqual(1);
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: expiredAsset.id,
    })).resolves.toEqual([
      expect.objectContaining({ action: "vault.deleted" }),
    ]);
  });

  it("purges an expired pending upload and releases its reserved daily allowance", async () => {
    const repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
    const vault = new TestVaultStorage();
    await repository.reserveVaultUploadBundle({
      pendingUploads: [{
        id: "pending-a",
        tenantId: "tenant-a",
        customerId: customer.id,
        uploadedByUserId: "employee-1",
        vaultKey: "private/pending-a",
        byteSize: 24,
        uploadedAt: "2026-08-01T00:00:00.000Z",
        deleteAfter: "2026-08-31T00:00:00.000Z",
        uploadDay: "2026-08-01",
      }],
      dailyByteLimit: 100,
    });
    await vault.putText({
      tenantId: "tenant-a",
      customerId: customer.id,
      uploadedByUserId: "employee-1",
      kind: "chat_text",
      vaultKey: "private/pending-a",
      content: "raw",
      deleteAfter: "2026-08-31T00:00:00.000Z",
    });

    await expect(purgeExpiredVaultAssets({ now: NOW, repository, vault }))
      .resolves.toEqual({ processed: 1, deleted: 1, failed: 0 });
    expect(vault.deletedKeys).toEqual(["private/pending-a"]);
    await expect(repository.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-01",
    })).resolves.toBe(0);
    await expect(repository.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: NOW,
      limit: 10,
    })).resolves.toEqual([]);
  });
});
