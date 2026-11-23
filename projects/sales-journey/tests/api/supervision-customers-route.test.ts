import { afterEach, describe, expect, it } from "vitest";

import {
  GET,
  POST,
} from "@/app/api/supervision/customers/route";
import {
  configureSupervisionApiRuntime,
  type PrivateTextVault,
} from "@/server/supervision/customer-service";
import type {
  D1DatabaseLike,
  D1PreparedStatementLike,
} from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const NOW = "2026-08-30T08:00:00.000Z";
const FUTURE = "2026-08-31T08:00:00.000Z";

class MembershipD1 implements D1DatabaseLike {
  readonly calls: Array<{ sql: string; bindings: readonly unknown[] }> = [];

  prepare(sql: string): D1PreparedStatementLike {
    const call = { sql, bindings: [] as unknown[] };
    this.calls.push(call);
    const statement: D1PreparedStatementLike = {
      bind: (...bindings) => {
        call.bindings = bindings;
        return statement;
      },
      first: async () => null,
      all: async <T>() => ({
        results: call.bindings[0] === "employee-1" ? [{
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

const vault: PrivateTextVault = {
  async putText() {},
  async deleteText() {},
};

let restoreRuntime: (() => void) | undefined;

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
});

function employeeRequest(body?: unknown): Request {
  return new Request("http://localhost/api/supervision/customers", {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "content-type": "application/json",
      "oai-authenticated-user-id": "employee-1",
      "oai-authenticated-user-email": "employee-1@shop.test",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

describe("/api/supervision/customers", () => {
  it("creates and lists only customers for the server-authenticated employee", async () => {
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));

    const response = await POST(employeeRequest({
      requestId: "create-customer-api-1",
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    }));

    expect(response.status).toBe(201);
    const createdBody = await response.json();
    expect(createdBody.customer).toEqual({
      id: expect.any(String),
      customerNumber: expect.any(String),
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
      status: "pending",
    });
    for (const internalKey of [
      "tenantId",
      "employeeUserId",
      "idempotencyKey",
      "requestFingerprint",
      "customerFingerprint",
      "version",
      "createdAt",
    ]) {
      expect(JSON.stringify(createdBody)).not.toContain(`"${internalKey}"`);
    }
    expect(d1.calls[0]?.sql).toContain("FROM users");
    expect(d1.calls[0]?.bindings).toEqual(["employee-1", "employee-1@shop.test"]);

    const listResponse = await GET(employeeRequest());
    expect(listResponse.status).toBe(200);
    expect((await listResponse.json()).customers).toEqual([createdBody.customer]);
    expect(d1.calls.filter((call) => call.sql.includes("FROM users"))).toHaveLength(2);
  });

  it("fails closed when no trusted D1 runtime binding is configured", async () => {
    const response = await POST(employeeRequest({
      requestId: "create-customer-api-2",
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    }));

    expect(response.status).toBe(503);
    expect((await response.json()).error).toBe("SUPERVISION_RUNTIME_UNAVAILABLE");
  });
});
