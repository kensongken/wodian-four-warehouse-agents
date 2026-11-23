import { afterEach, describe, expect, it } from "vitest";

import { POST } from "@/app/api/internal/retention/route";
import type { CustomerRecord, VaultAssetRecord } from "@/domain/supervision/contracts";
import { configureSupervisionApiRuntime } from "@/server/supervision/customer-service";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { TestVaultStorage } from "../support/test-vault-storage";

const SECRET = "test-only-usage-token-secret-with-32-bytes";
const RETENTION_SECRET = "dedicated-retention-secret-with-32-bytes";
const NOW = "2026-09-30T00:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-018",
  customerAlias: "客户 18",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T00:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: "2026-08-01T00:00:00.000Z",
};
const asset: VaultAssetRecord = {
  id: "asset-1",
  tenantId: "tenant-a",
  customerId: "customer-1",
  uploadedByUserId: "employee-1",
  kind: "chat_text",
  vaultKey: "private/a/b/asset-1",
  uploadedAt: "2026-08-01T00:00:00.000Z",
  deleteAfter: "2026-08-31T00:00:00.000Z",
  version: 1,
  createdAt: "2026-08-01T00:00:00.000Z",
};

const unusedD1: D1DatabaseLike = {
  prepare(): D1PreparedStatementLike {
    const statement: D1PreparedStatementLike = {
      bind: () => statement,
      first: async () => null,
      all: async () => ({ results: [] }),
      run: async () => ({ success: true }),
    };
    return statement;
  },
};

let restoreRuntime: (() => void) | undefined;

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
});

async function configure() {
  const repository = new MemorySupervisionRepository({
    seed: { customers: [customer], vaultAssets: [asset] },
  });
  const vault = new TestVaultStorage();
  await vault.putText({
    tenantId: asset.tenantId,
    customerId: asset.customerId,
    uploadedByUserId: asset.uploadedByUserId,
    kind: "chat_text",
    vaultKey: asset.vaultKey,
    content: "原始聊天",
    deleteAfter: asset.deleteAfter,
  });
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1: unusedD1,
    repository,
    vault,
    usageTokenSecret: SECRET,
    idempotencyKeyring: [{
      version: "v1",
      secret: "stable-idempotency-secret-v1-with-32-bytes",
    }],
    retentionSchedulerSecret: RETENTION_SECRET,
    now: () => NOW,
    createId: () => "retention-1",
  }));
  return { repository, vault };
}

describe("internal retention route", () => {
  it("rejects a scheduler request without the server secret", async () => {
    const { repository, vault } = await configure();

    const response = await POST(new Request("http://localhost/api/internal/retention", {
      method: "POST",
    }));

    expect(response.status).toBe(401);
    expect(vault.deletedKeys).toEqual([]);
    await expect(repository.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "asset-1",
    })).resolves.toMatchObject({ vaultKey: asset.vaultKey });
  });

  it("runs the cross-tenant purge only for a scheduler holding the secret", async () => {
    const { repository, vault } = await configure();

    const response = await POST(new Request("http://localhost/api/internal/retention", {
      method: "POST",
      headers: { authorization: `Bearer ${RETENTION_SECRET}` },
    }));

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ processed: 1, deleted: 1, failed: 0 });
    expect(vault.deletedKeys).toEqual([asset.vaultKey]);
    await expect(repository.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "asset-1",
    })).resolves.toMatchObject({ deletedAt: NOW, vaultKey: "deleted/asset-1" });
  });

  it("does not authorize retention with the decision usage-token secret", async () => {
    const { repository, vault } = await configure();

    const response = await POST(new Request("http://localhost/api/internal/retention", {
      method: "POST",
      headers: { authorization: `Bearer ${SECRET}` },
    }));

    expect(response.status).toBe(401);
    expect(vault.deletedKeys).toEqual([]);
    await expect(repository.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "asset-1",
    })).resolves.toMatchObject({ vaultKey: asset.vaultKey });
  });
});
