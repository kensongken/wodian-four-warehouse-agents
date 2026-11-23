import { describe, expect, it } from "vitest";
import { File as NodeFile } from "node:buffer";

import type { CustomerRecord } from "@/domain/supervision/contracts";
import type { CurrentUser } from "@/server/auth/current-user";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import {
  detectVaultImageMediaType,
  uploadVaultImages,
} from "@/server/supervision/vault";
import { TestVaultStorage } from "../support/test-vault-storage";

const NOW = "2026-08-30T08:00:00.000Z";
const FUTURE = "2026-08-31T08:00:00.000Z";

const employee: CurrentUser = {
  accountUserId: "employee-1",
  email: "employee-1@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};

const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-018",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: FUTURE,
  status: "pending",
  version: 1,
  createdAt: NOW,
};

const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);
const pngBytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const webpBytes = new Uint8Array([
  0x52, 0x49, 0x46, 0x46, 0x04, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50,
]);

function repository() {
  return new MemorySupervisionRepository({ seed: { customers: [customer] } });
}

function imageFile(bytes: Uint8Array, name: string, type: string): File {
  return new NodeFile([bytes], name, { type }) as unknown as File;
}

class StoreThenThrowVault extends TestVaultStorage {
  constructor(
    private readonly failRecoveryRead = false,
    private readonly failCleanup = false,
  ) {
    super();
  }

  override async putObject(input: Parameters<TestVaultStorage["putObject"]>[0]): Promise<void> {
    await super.putObject(input);
    throw new Error("object-store acknowledgement lost");
  }

  override async getObject(vaultKey: string) {
    if (this.failRecoveryRead) throw new Error("object-store read unavailable");
    return super.getObject(vaultKey);
  }

  override async deleteObject(vaultKey: string): Promise<void> {
    if (this.failCleanup) throw new Error("object-store cleanup unavailable");
    return super.deleteObject(vaultKey);
  }
}

class ReserveThenThrowRepository extends MemorySupervisionRepository {
  override async reserveVaultUploadBundle(
    input: Parameters<MemorySupervisionRepository["reserveVaultUploadBundle"]>[0],
  ): Promise<never> {
    await super.reserveVaultUploadBundle(input);
    throw new Error("quota reservation acknowledgement lost");
  }
}

class RejectCommitAndAssetRecoveryRepository extends MemorySupervisionRepository {
  override async createVaultUploadBundle(): Promise<never> {
    throw new Error("metadata commit unavailable");
  }

  override async getVaultAsset(): Promise<never> {
    throw new Error("metadata recovery read unavailable");
  }
}

class CommitThenLoseRecoveryRepository extends MemorySupervisionRepository {
  private failAssetReads = true;

  override async createVaultUploadBundle(
    input: Parameters<MemorySupervisionRepository["createVaultUploadBundle"]>[0],
  ): Promise<never> {
    await super.createVaultUploadBundle(input);
    throw new Error("metadata commit acknowledgement lost");
  }

  override async getVaultAsset(
    input: Parameters<MemorySupervisionRepository["getVaultAsset"]>[0],
  ) {
    if (this.failAssetReads) throw new Error("metadata recovery read unavailable");
    return super.getVaultAsset(input);
  }

  allowAssetReads(): void {
    this.failAssetReads = false;
  }
}

describe("private vault uploads", () => {
  it("detects JPEG, PNG, and WebP from bytes rather than the filename", () => {
    expect(detectVaultImageMediaType(jpegBytes)).toBe("image/jpeg");
    expect(detectVaultImageMediaType(pngBytes)).toBe("image/png");
    expect(detectVaultImageMediaType(webpBytes)).toBe("image/webp");
    expect(detectVaultImageMediaType(new Uint8Array([0x47, 0x49, 0x46, 0x38])))
      .toBeUndefined();
  });

  it("stores an opaque private object, exact expiry metadata, and an upload audit", async () => {
    const repo = repository();
    const vault = new TestVaultStorage();

    const [asset] = await uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "customer-real-name.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-random-1",
    });

    expect(asset).toMatchObject({
      id: "asset-random-1",
      tenantId: "tenant-a",
      customerId: "customer-1",
      kind: "product_image",
      byteSize: jpegBytes.byteLength,
      uploadedAt: NOW,
      deleteAfter: "2026-09-29T08:00:00.000Z",
    });
    expect(asset!.vaultKey).not.toContain("tenant-a");
    expect(asset!.vaultKey).not.toContain("customer-1");
    expect(asset!.vaultKey).not.toContain("customer-real-name");
    await expect(vault.getObject(asset!.vaultKey)).resolves.toMatchObject({
      contentType: "image/jpeg",
      byteSize: jpegBytes.byteLength,
    });
    await expect(repo.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: asset!.id,
    })).resolves.toMatchObject({ id: asset!.id, deleteAfter: "2026-09-29T08:00:00.000Z" });
    await expect(repo.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: asset!.id,
    })).resolves.toEqual([
      expect.objectContaining({ action: "vault.uploaded", actorUserId: "employee-1" }),
    ]);
  });

  it("rejects a declared MIME type that disagrees with the magic bytes", async () => {
    const vault = new TestVaultStorage();

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(pngBytes, "renamed.jpg", "image/jpeg")],
      repository: repository(),
      vault,
      now: () => NOW,
    })).rejects.toMatchObject({
      code: "UNSUPPORTED_VAULT_IMAGE",
      status: 415,
    });
    expect(vault.keys()).toEqual([]);
  });

  it("enforces per-file, per-request, and daily tenant limits", async () => {
    const repo = repository();
    const vault = new TestVaultStorage();
    const limits = { maxFileBytes: 12, maxFilesPerRequest: 1, maxTenantBytesPerDay: 12 };

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [
        imageFile(jpegBytes, "one.jpg", "image/jpeg"),
        imageFile(jpegBytes, "two.jpg", "image/jpeg"),
      ],
      repository: repo,
      vault,
      limits,
      now: () => NOW,
    })).rejects.toMatchObject({ code: "VAULT_REQUEST_LIMIT_EXCEEDED", status: 413 });

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(new Uint8Array(13).fill(0xff), "large.jpg", "image/jpeg")],
      repository: repo,
      vault,
      limits,
      now: () => NOW,
    })).rejects.toMatchObject({ code: "VAULT_FILE_TOO_LARGE", status: 413 });

    await uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "first.jpg", "image/jpeg")],
      repository: repo,
      vault,
      limits,
      now: () => NOW,
      createId: () => "asset-first",
    });
    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "second.jpg", "image/jpeg")],
      repository: repo,
      vault,
      limits,
      now: () => NOW,
      createId: () => "asset-second",
    })).rejects.toMatchObject({ code: "VAULT_DAILY_LIMIT_EXCEEDED", status: 429 });
    expect(vault.keys()).toHaveLength(1);
  });

  it("recovers a stored object when the object-store acknowledgement is lost", async () => {
    const repo = repository();
    const vault = new StoreThenThrowVault();

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-store-then-throw",
    })).resolves.toEqual([
      expect.objectContaining({ id: "asset-store-then-throw" }),
    ]);
    expect(vault.keys()).toHaveLength(1);
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([]);
  });

  it("keeps a governed retry ledger when an ambiguous upload cannot be cleaned up", async () => {
    const repo = repository();
    const vault = new StoreThenThrowVault(true, true);

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-cleanup-pending",
    })).rejects.toThrow("object-store acknowledgement lost");

    expect(vault.keys()).toHaveLength(1);
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([
      expect.objectContaining({
        id: "asset-cleanup-pending",
        vaultKey: expect.stringContaining("asset-cleanup-pending"),
        byteSize: jpegBytes.byteLength,
        deleteAfter: "2026-09-29T08:00:00.000Z",
      }),
    ]);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(jpegBytes.byteLength);
    const dashboard = await repo.getOwnerDashboard({ tenantId: "tenant-a", now: NOW });
    expect(dashboard.alerts).toEqual([
      expect.objectContaining({ kind: "retention_failed", customerId: customer.id }),
    ]);
    expect(JSON.stringify(dashboard.alerts)).not.toContain("asset-cleanup-pending");
  });

  it("releases the reservation after an ambiguous upload is successfully cleaned up", async () => {
    const repo = repository();
    const vault = new StoreThenThrowVault(true, false);

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-cleanup-success",
    })).rejects.toThrow("object-store acknowledgement lost");

    expect(vault.keys()).toEqual([]);
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([]);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(0);
  });

  it("atomically reserves the tenant day allowance across concurrent uploads", async () => {
    const repo = repository();
    const vault = new TestVaultStorage();
    const limits = { maxFileBytes: 12, maxFilesPerRequest: 1, maxTenantBytesPerDay: 12 };

    const results = await Promise.allSettled([
      uploadVaultImages({
        actor: employee,
        customerId: customer.id,
        files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
        repository: repo,
        vault,
        limits,
        now: () => NOW,
        createId: () => "asset-concurrent-a",
      }),
      uploadVaultImages({
        actor: employee,
        customerId: customer.id,
        files: [imageFile(jpegBytes, "two.jpg", "image/jpeg")],
        repository: repo,
        vault,
        limits,
        now: () => NOW,
        createId: () => "asset-concurrent-b",
      }),
    ]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => (
      result.status === "rejected"
      && result.reason instanceof Error
      && "code" in result.reason
      && result.reason.code === "VAULT_DAILY_LIMIT_EXCEEDED"
    ))).toHaveLength(1);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(jpegBytes.byteLength);
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([]);
    expect(vault.keys()).toHaveLength(1);
  });

  it("recovers a committed quota reservation before writing the object", async () => {
    const repo = new ReserveThenThrowRepository({ seed: { customers: [customer] } });
    const vault = new TestVaultStorage();

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-reservation-recovered",
    })).resolves.toEqual([
      expect.objectContaining({ id: "asset-reservation-recovered" }),
    ]);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(jpegBytes.byteLength);
  });

  it("still cleans the object when metadata commit and its recovery read both fail", async () => {
    const repo = new RejectCommitAndAssetRecoveryRepository({ seed: { customers: [customer] } });
    const vault = new TestVaultStorage();

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-metadata-failed",
    })).rejects.toThrow("metadata commit unavailable");
    expect(vault.keys()).toEqual([]);
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([]);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(0);
  });

  it("keeps a committed asset when both commit acknowledgement and recovery read are lost", async () => {
    const repo = new CommitThenLoseRecoveryRepository({ seed: { customers: [customer] } });
    const vault = new TestVaultStorage();

    await expect(uploadVaultImages({
      actor: employee,
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repo,
      vault,
      now: () => NOW,
      createId: () => "asset-metadata-committed",
    })).rejects.toThrow("metadata commit acknowledgement lost");
    repo.allowAssetReads();
    expect(vault.keys()).toHaveLength(1);
    await expect(repo.getVaultAsset({
      tenantId: "tenant-a",
      vaultAssetId: "asset-metadata-committed",
    })).resolves.toMatchObject({ id: "asset-metadata-committed" });
    await expect(repo.listExpiredVaultPendingUploads({
      tenantId: "tenant-a",
      now: "2026-10-01T00:00:00.000Z",
      limit: 10,
    })).resolves.toEqual([]);
  });

  it("hides another employee's customer behind a tenant-scoped not-found result", async () => {
    await expect(uploadVaultImages({
      actor: { ...employee, accountUserId: "employee-2", email: "employee-2@shop.test" },
      customerId: customer.id,
      files: [imageFile(jpegBytes, "one.jpg", "image/jpeg")],
      repository: repository(),
      vault: new TestVaultStorage(),
      now: () => NOW,
    })).rejects.toMatchObject({ code: "VAULT_CUSTOMER_NOT_FOUND", status: 404 });
  });
});
