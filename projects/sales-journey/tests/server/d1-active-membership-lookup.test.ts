import { describe, expect, it } from "vitest";

import {
  createD1ActiveMembershipLookup,
  getCurrentUserFromD1,
} from "@/server/auth/current-user";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";

class RecordingD1 implements D1DatabaseLike {
  readonly calls: Array<{ sql: string; bindings: readonly unknown[] }> = [];

  constructor(private readonly rows: readonly Record<string, unknown>[]) {}

  prepare(sql: string): D1PreparedStatementLike {
    const call = { sql, bindings: [] as unknown[] };
    this.calls.push(call);
    const statement: D1PreparedStatementLike = {
      bind: (...bindings) => {
        call.bindings = bindings;
        return statement;
      },
      first: async <T>() => (this.rows[0] ?? null) as T | null,
      all: async <T>() => ({ results: this.rows as T[] }),
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

function headersFor(email: string): Headers {
  return new Headers({
    "oai-authenticated-user-id": "employee-1",
    "oai-authenticated-user-email": email,
  });
}

describe("D1 active membership lookup", () => {
  it("looks up an active membership by both platform id and normalized email", async () => {
    const d1 = new RecordingD1([{
      id: "employee-1",
      email: "Employee@Shop.Test",
      tenant_id: "tenant-a",
      role: "employee",
    }]);
    const memberships = createD1ActiveMembershipLookup(d1);

    await expect(memberships.findActiveMembership({
      accountUserId: "employee-1",
      email: "employee@shop.test",
    })).resolves.toEqual({
      accountUserId: "employee-1",
      email: "employee@shop.test",
      tenantId: "tenant-a",
      role: "employee",
    });
    expect(d1.calls[0]?.sql).toContain("id = ?");
    expect(d1.calls[0]?.sql).toContain("LOWER(email) = ?");
    expect(d1.calls[0]?.sql).toContain("active = 1");
    expect(d1.calls[0]?.bindings).toEqual(["employee-1", "employee@shop.test"]);
  });

  it("uses an explicitly injected D1 binding for the production adapter", async () => {
    const d1 = new RecordingD1([{
      id: "employee-1",
      email: "employee@shop.test",
      tenant_id: "tenant-a",
      role: "employee",
    }]);

    await expect(getCurrentUserFromD1(headersFor("employee@shop.test"), d1)).resolves.toMatchObject({
      accountUserId: "employee-1",
      tenantId: "tenant-a",
      role: "employee",
    });
  });

  it("rejects an employee through the D1-backed owner adapter", async () => {
    const d1 = new RecordingD1([{
      id: "employee-1",
      email: "employee@shop.test",
      tenant_id: "tenant-a",
      role: "employee",
    }]);

    await expect(requireOwnerFromD1(headersFor("employee@shop.test"), d1))
      .rejects.toMatchObject({ status: 403 });
  });

  it("fails closed when the configured D1 row is malformed", async () => {
    const d1 = new RecordingD1([{
      id: "employee-1",
      email: "employee@shop.test",
      tenant_id: 42,
      role: "employee",
    }]);

    await expect(createD1ActiveMembershipLookup(d1).findActiveMembership({
      accountUserId: "employee-1",
      email: "employee@shop.test",
    })).resolves.toBeUndefined();
  });

  it("rejects ambiguous active memberships instead of choosing a tenant", async () => {
    const d1 = new RecordingD1([
      {
        id: "employee-1",
        email: "employee@shop.test",
        tenant_id: "tenant-a",
        role: "employee",
      },
      {
        id: "employee-1",
        email: "employee@shop.test",
        tenant_id: "tenant-b",
        role: "owner",
      },
    ]);

    await expect(getCurrentUserFromD1(headersFor("employee@shop.test"), d1))
      .rejects.toMatchObject({ status: 401 });
    expect(d1.calls[0]?.sql).not.toContain("LIMIT 1");
  });
});
