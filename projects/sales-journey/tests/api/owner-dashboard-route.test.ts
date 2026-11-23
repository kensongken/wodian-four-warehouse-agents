import { afterEach, describe, expect, it } from "vitest";

import { GET } from "@/app/api/supervision/owner/dashboard/route";
import type { CustomerRecord, FollowUpRecord } from "@/domain/supervision/contracts";
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

const NOW = "2026-08-30T12:00:00.000Z";

const customers: CustomerRecord[] = [
  {
    id: "customer-a",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerNumber: "C-018",
    customerAlias: "林女士",
    category: "沉香",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    status: "pending",
    version: 1,
    createdAt: "2026-08-30T08:00:00.000Z",
  },
  {
    id: "customer-b",
    tenantId: "tenant-b",
    employeeUserId: "employee-b",
    customerNumber: "C-OTHER",
    customerAlias: "其他店客户",
    category: "绿松石",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    status: "won",
    version: 1,
    createdAt: "2026-08-30T08:00:00.000Z",
  },
];

const followUps: FollowUpRecord[] = [
  {
    id: "follow-up-a",
    tenantId: "tenant-a",
    customerId: "customer-a",
    employeeUserId: "employee-a",
    customerMessageVaultAssetId: "vault-chat-a",
    customerMessageSummary: "脱敏摘要",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    result: "pending",
    nextAction: "电话确认",
    decisionCardId: "card-a",
    replyUsage: "edited",
    version: 1,
    createdAt: "2026-08-30T08:00:00.000Z",
  },
  {
    id: "follow-up-b",
    tenantId: "tenant-b",
    customerId: "customer-b",
    employeeUserId: "employee-b",
    customerMessageVaultAssetId: "vault-chat-b",
    customerMessageSummary: "不应跨店出现",
    plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
    actualFollowUpAt: "2026-08-30T10:30:00.000Z",
    result: "won",
    nextAction: "已完成",
    decisionCardId: "card-b",
    replyUsage: "adopted",
    version: 1,
    createdAt: "2026-08-30T08:00:00.000Z",
  },
];

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
      all: async <T>() => {
        if (sql.includes("FROM users") && sql.includes("active = 1") && call.bindings.length === 1) return { results: [{ id: "employee-a", email: "employee@shop.test" } as T] };
        if (sql.includes("FROM customers")) return { results: [{ id: "customer-a", customer_number: "C-018", customer_alias: "林女士", employee_user_id: "employee-a", category: "沉香", planned_follow_up_at: "2026-08-30T10:00:00.000Z", status: "pending" } as T] };
        const id = String(call.bindings[0]);
        const role = id.startsWith("owner") ? "owner" : "employee";
        const tenantId = id.endsWith("-b") ? "tenant-b" : "tenant-a";
        return { results: [{
          id,
          tenant_id: tenantId,
          email: `${id}@shop.test`,
          role,
        } as T] };
      },
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

function requestAs(id: string, query = ""): Request {
  return new Request(`http://localhost/api/supervision/owner/dashboard${query}`, {
    headers: {
      "oai-authenticated-user-id": id,
      "oai-authenticated-user-email": `${id}@shop.test`,
    },
  });
}

function configure(): { d1: MembershipD1 } {
  const d1 = new MembershipD1();
  const repository = new MemorySupervisionRepository({ seed: { customers, followUps } });
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1,
    repository,
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => NOW,
  }));
  return { d1 };
}

describe("owner dashboard route", () => {
  it("authenticates the owner through D1 and scopes every metric to that tenant", async () => {
    const { d1 } = configure();

    const response = await GET(requestAs("owner-a"));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(body.dashboard).toMatchObject({
      summaryMetrics: expect.any(Array),
    });
    expect(body.dashboard.records.map((entry: { customerNumber: string }) => entry.customerNumber))
      .toEqual(["C-018"]);
    expect(JSON.stringify(body.dashboard)).not.toMatch(/tenant-a|employee-a|follow-up-a|alerts|recordIds/u);
    expect(d1.calls[0]?.sql).toContain("FROM users");
    expect(d1.calls[0]?.bindings).toEqual(["owner-a", "owner-a@shop.test"]);
  });

  it("rejects employees and client-selected tenant scope", async () => {
    configure();

    await expect(GET(requestAs("employee-a"))).resolves.toMatchObject({ status: 403 });
    await expect(GET(requestAs("owner-a", "?tenantId=tenant-b")))
      .resolves.toMatchObject({ status: 400 });
  });

  it("validates date ranges before querying the dashboard", async () => {
    configure();

    await expect(GET(requestAs(
      "owner-a",
      "?from=2026-08-31T00%3A00%3A00.000Z&to=2026-08-30T00%3A00%3A00.000Z",
    ))).resolves.toMatchObject({ status: 400 });
  });

  it("resolves a tenant-bound employeeRef and rejects invalid or cross-tenant refs", async () => {
    configure();
    const initial = await GET(requestAs("owner-a"));
    const view = await initial.json() as { dashboard: { employees: Array<{ navigationId: string }> } };
    const employeeRef = view.dashboard.employees[0]!.navigationId;
    const filtered = await GET(requestAs("owner-a", `?employeeRef=${employeeRef}`));
    expect(filtered.status).toBe(200);
    expect((await filtered.json()).dashboard.records).toHaveLength(1);
    await expect(GET(requestAs("owner-a", "?employeeRef=employee-view-tampered"))).resolves.toMatchObject({ status: 400 });
    await expect(GET(requestAs("owner-b", `?employeeRef=${employeeRef}`))).resolves.toMatchObject({ status: 400 });
  });
});
