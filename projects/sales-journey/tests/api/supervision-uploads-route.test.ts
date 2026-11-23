// @vitest-environment node

import { afterEach, describe, expect, it } from "vitest";

import { POST } from "@/app/api/supervision/uploads/route";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import { configureSupervisionApiRuntime } from "@/server/supervision/customer-service";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { TestVaultStorage } from "../support/test-vault-storage";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const NOW = "2026-08-30T08:00:00.000Z";
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
  createdAt: NOW,
};
const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);
const pngBytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

class MembershipD1 implements D1DatabaseLike {
  prepare(sql: string): D1PreparedStatementLike {
    let bindings: unknown[] = [];
    const statement: D1PreparedStatementLike = {
      bind: (...values) => {
        bindings = values;
        return statement;
      },
      first: async () => null,
      all: async <T>() => ({
        results: bindings[0] === "employee-1" ? [{
          id: "employee-1",
          tenant_id: "tenant-a",
          email: "employee-1@shop.test",
          role: "employee",
        } as T] : [],
      }),
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

function uploadRequest(bytes: Uint8Array, type: string, contentLength?: string): Request {
  const boundary = "vault-upload-test-boundary";
  const encoder = new TextEncoder();
  const beforeFile = encoder.encode(
    `--${boundary}\r\nContent-Disposition: form-data; name="customerId"\r\n\r\n${customer.id}\r\n`
    + `--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="customer-real-name.jpg"\r\n`
    + `Content-Type: ${type}\r\n\r\n`,
  );
  const afterFile = encoder.encode(`\r\n--${boundary}--\r\n`);
  const body = new Uint8Array(beforeFile.byteLength + bytes.byteLength + afterFile.byteLength);
  body.set(beforeFile);
  body.set(bytes, beforeFile.byteLength);
  body.set(afterFile, beforeFile.byteLength + bytes.byteLength);
  return new Request("http://localhost/api/supervision/uploads", {
    method: "POST",
    headers: {
      "content-type": `multipart/form-data; boundary=${boundary}`,
      "oai-authenticated-user-id": "employee-1",
      "oai-authenticated-user-email": "employee-1@shop.test",
      ...(contentLength ? { "content-length": contentLength } : {}),
    },
    body: body.buffer,
  });
}

describe("/api/supervision/uploads", () => {
  it("authenticates through D1 and returns private metadata without any object URL or key", async () => {
    const vault = new TestVaultStorage();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1: new MembershipD1(),
      repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
      createId: () => "upload-asset-1",
    }));

    const response = await POST(uploadRequest(jpegBytes, "image/jpeg"));
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(body.assets).toEqual([
      {
        id: "upload-asset-1",
        kind: "product_image",
        uploadedAt: NOW,
        deleteAfter: "2026-09-29T08:00:00.000Z",
      },
    ]);
    expect(JSON.stringify(body)).not.toContain("vaultKey");
    expect(JSON.stringify(body)).not.toContain("customer-real-name");
    expect(JSON.stringify(body)).not.toMatch(/https?:\/\//u);
    expect(vault.keys()).toHaveLength(1);
  });

  it("rejects forged MIME metadata and leaves no private object behind", async () => {
    const vault = new TestVaultStorage();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1: new MembershipD1(),
      repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));

    const response = await POST(uploadRequest(pngBytes, "image/jpeg"));

    expect(response.status).toBe(415);
    expect(await response.json()).toEqual({ error: "UNSUPPORTED_VAULT_IMAGE" });
    expect(vault.keys()).toEqual([]);
  });

  it("rejects an oversized declared request before multipart storage work", async () => {
    const vault = new TestVaultStorage();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1: new MembershipD1(),
      repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));
    const fiveFilesPlusOneMiB = (10 * 1024 * 1024 * 5) + (1024 * 1024);

    const response = await POST(uploadRequest(
      jpegBytes,
      "image/jpeg",
      String(fiveFilesPlusOneMiB + 1),
    ));

    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "VAULT_REQUEST_LIMIT_EXCEEDED" });
    expect(vault.keys()).toEqual([]);
  });
});
