import { afterEach, describe, expect, it } from "vitest";

import { GET } from "@/app/api/supervision/owner/vault/[assetId]/route";
import type { CustomerRecord, VaultAssetRecord } from "@/domain/supervision/contracts";
import { configureSupervisionApiRuntime } from "@/server/supervision/customer-service";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import type { VaultObject } from "@/server/supervision/vault";
import { TestVaultStorage } from "../support/test-vault-storage";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const UPLOADED_AT = "2026-08-30T08:00:00.000Z";
const BEFORE_EXPIRY = "2026-09-29T07:59:59.999Z";
const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-018",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: UPLOADED_AT,
};
const asset: VaultAssetRecord = {
  id: "asset-1",
  tenantId: "tenant-a",
  customerId: "customer-1",
  uploadedByUserId: "employee-1",
  kind: "product_image",
  vaultKey: "private/a/b/asset-1",
  byteSize: 8,
  uploadedAt: UPLOADED_AT,
  deleteAfter: "2026-09-29T08:00:00.000Z",
  version: 1,
  createdAt: UPLOADED_AT,
};
const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);

class StreamingVaultStorage extends TestVaultStorage {
  override async getObject(vaultKey: string): Promise<VaultObject | undefined> {
    this.readKeys.push(vaultKey);
    const stream = new ReadableStream<Uint8Array<ArrayBuffer>>({
      start(controller) {
        controller.enqueue(jpegBytes);
        controller.close();
      },
    });
    return {
      body: stream,
      contentType: "image/jpeg",
      byteSize: jpegBytes.byteLength,
    };
  }
}

class MembershipD1 implements D1DatabaseLike {
  prepare(): D1PreparedStatementLike {
    let bindings: unknown[] = [];
    const statement: D1PreparedStatementLike = {
      bind: (...values) => {
        bindings = values;
        return statement;
      },
      first: async () => null,
      all: async <T>() => {
        const id = String(bindings[0]);
        if (id === "owner-a") {
          return { results: [{
            id,
            tenant_id: "tenant-a",
            email: "owner-a@shop.test",
            role: "owner",
          } as T] };
        }
        if (id === "owner-b") {
          return { results: [{
            id,
            tenant_id: "tenant-b",
            email: "owner-b@shop.test",
            role: "owner",
          } as T] };
        }
        if (id === "employee-1") {
          return { results: [{
            id,
            tenant_id: "tenant-a",
            email: "employee-1@shop.test",
            role: "employee",
          } as T] };
        }
        return { results: [] };
      },
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

let restoreRuntime: (() => void) | undefined;

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
});

function ownerRequest(id: "owner-a" | "owner-b" | "employee-1"): Request {
  return new Request("http://localhost/api/supervision/owner/vault/asset-1", {
    headers: {
      "oai-authenticated-user-id": id,
      "oai-authenticated-user-email": `${id}@shop.test`,
    },
  });
}

const routeContext = { params: Promise.resolve({ assetId: "asset-1" }) };

async function configure(
  now = BEFORE_EXPIRY,
  vault: TestVaultStorage = new TestVaultStorage(),
) {
  const repository = new MemorySupervisionRepository({
    seed: { customers: [customer], vaultAssets: [asset] },
  });
  await vault.putObject({
    vaultKey: asset.vaultKey,
    body: jpegBytes,
    contentType: "image/jpeg",
  });
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1: new MembershipD1(),
    repository,
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => now,
    createId: () => "audit-view-1",
  }));
  return { repository, vault };
}

describe("owner vault route", () => {
  it("streams a tenant-owned object with no-store and writes a view audit", async () => {
    const { repository } = await configure();

    const response = await GET(ownerRequest("owner-a"), routeContext);

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("image/jpeg");
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(jpegBytes);
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: "asset-1",
    })).resolves.toEqual([
      expect.objectContaining({ action: "vault.viewed", actorUserId: "owner-a" }),
    ]);
  });

  it("passes a private object stream through without requiring a full in-memory buffer", async () => {
    const { repository } = await configure(BEFORE_EXPIRY, new StreamingVaultStorage());

    const response = await GET(ownerRequest("owner-a"), routeContext);

    expect(response.status).toBe(200);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(jpegBytes);
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: "asset-1",
    })).resolves.toHaveLength(1);
  });

  it("denies employees before reading the private object", async () => {
    const { vault } = await configure();

    const response = await GET(ownerRequest("employee-1"), routeContext);

    expect(response.status).toBe(403);
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(vault.readKeys).toEqual([]);
  });

  it("returns 404 for a wrong tenant without touching the private object", async () => {
    const { vault } = await configure();

    const response = await GET(ownerRequest("owner-b"), routeContext);

    expect(response.status).toBe(404);
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(vault.readKeys).toEqual([]);
  });

  it("returns 404 at the exact expiry instant and does not audit a view", async () => {
    const { repository, vault } = await configure(asset.deleteAfter);

    const response = await GET(ownerRequest("owner-a"), routeContext);

    expect(response.status).toBe(404);
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(vault.readKeys).toEqual([]);
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "vault_asset",
      targetId: "asset-1",
    })).resolves.toEqual([]);
  });
});
