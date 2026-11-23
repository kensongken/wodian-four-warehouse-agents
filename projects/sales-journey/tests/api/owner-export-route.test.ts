import { afterEach, describe, expect, it } from "vitest";

import { GET } from "@/app/api/supervision/owner/export/route";
import type {
  CustomerRecord,
  FollowUpRecord,
  VaultAssetRecord,
} from "@/domain/supervision/contracts";
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
const customer: CustomerRecord = {
  id: "customer-a",
  tenantId: "tenant-a",
  employeeUserId: "employee-a",
  customerNumber: "C-018",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
  status: "won",
  version: 1,
  createdAt: "2026-08-30T08:00:00.000Z",
};
const followUp: FollowUpRecord = {
  id: "follow-up-a",
  tenantId: "tenant-a",
  customerId: customer.id,
  employeeUserId: "employee-a",
  customerMessageVaultAssetId: "image-secret-reference",
  customerMessageSummary: "SECRET_RAW_CHAT",
  plannedFollowUpAt: "2026-08-30T10:00:00.000Z",
  actualFollowUpAt: "2026-08-30T10:30:00.000Z",
  result: "won",
  nextAction: "成交后回访",
  decisionCardId: "card-a",
  replyUsage: "edited",
  version: 1,
  createdAt: "2026-08-30T08:00:00.000Z",
};
const vaultAsset: VaultAssetRecord = {
  id: "image-secret-reference",
  tenantId: "tenant-a",
  customerId: customer.id,
  uploadedByUserId: "employee-a",
  kind: "customer_identity",
  vaultKey: "private/realName/Zhang-San/image-original.jpg",
  uploadedAt: "2026-08-30T08:00:00.000Z",
  deleteAfter: "2026-09-29T08:00:00.000Z",
  version: 1,
  createdAt: "2026-08-30T08:00:00.000Z",
};

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
        if (sql.includes("FROM customers")) return { results: [{ id: "customer-a", customer_number: "C-018", customer_alias: "林女士", employee_user_id: "employee-a", category: "沉香", planned_follow_up_at: "2026-08-30T10:00:00.000Z", status: "won" } as T] };
        const id = String(call.bindings[0]);
        return { results: [{
          id,
          tenant_id: "tenant-a",
          email: `${id}@shop.test`,
          role: id.startsWith("owner") ? "owner" : "employee",
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
  return new Request(`http://localhost/api/supervision/owner/export${query}`, {
    headers: {
      "oai-authenticated-user-id": id,
      "oai-authenticated-user-email": `${id}@shop.test`,
    },
  });
}

function configure(): { d1: MembershipD1; repository: MemorySupervisionRepository } {
  const d1 = new MembershipD1();
  const repository = new MemorySupervisionRepository({
    seed: { customers: [customer], followUps: [followUp], vaultAssets: [vaultAsset] },
  });
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1,
    repository,
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => NOW,
    createId: () => "export-1",
  }));
  return { d1, repository };
}

describe("owner CSV export route", () => {
  it("keeps the default export free of reply-feedback text", async () => {
    const { repository } = configure();
    await repository.saveReplyFeedback({
      tenantId: "tenant-a",
      employeeUserId: "employee-a",
      customerId: "customer-a",
      decisionCardId: "card-feedback-a",
      cardInstanceId: "card-instance-feedback-a",
      cardDigest: `sha256_${"a".repeat(64)}`,
      agentVersion: "reply-agent.v2",
      rating: "needs_improvement",
      reasonCodes: ["too_long"],
      scenario: "product_selection",
      salesStage: "evaluate",
      evidenceIds: ["evidence-a"],
      privacyStatus: "safe",
      privacyReasonCodes: [],
      sanitizedFinalReply: "SAFE_REPLY_MUST_NOT_EXPORT",
    });

    const response = await GET(requestAs("owner-a"));
    const csv = await response.text();

    expect(response.status).toBe(200);
    expect(csv).not.toContain("SAFE_REPLY_MUST_NOT_EXPORT");
    expect(csv.split("\n")[0]).not.toMatch(/reply|feedback|text/iu);
  });

  it.each(["?customerQuery=林女", "?customerQuery=018", "?customerQuery=%E5%A5%B3%E5%A3%AB%20C-"])("exports the same partial customer match as the screen: %s", async (query) => {
    configure();
    const response = await GET(requestAs("owner-a", query));
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("C-018,林女士");
  });
  it("exports only the allowed de-identified ledger fields and appends a scoped audit", async () => {
    const { d1, repository } = configure();

    const response = await GET(requestAs("owner-a", "?employeeUserId=employee-a"));
    const csv = await response.text();

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/csv");
    expect(response.headers.get("content-disposition")).toContain("attachment");
    expect(csv.split("\n")[0]).toBe([
      "customerNumber",
      "customerAlias",
      "employeeLabel",
      "category",
      "submittedAt",
      "plannedFollowUpAt",
      "actualFollowUpAt",
      "decisionUsage",
      "nextAction",
      "result",
      "riskLabels",
    ].join(","));
    expect(csv).toContain("C-018");
    expect(csv).toContain("林女士");
    expect(csv).toContain("employee@shop.test");
    expect(csv).not.toContain("employee-a");
    expect(csv).not.toMatch(/realName|SECRET_RAW_CHAT|vaultKey|private\/|image|jpg/i);
    expect(d1.calls[0]?.sql).toContain("FROM users");

    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "owner_export",
      targetId: "export-1",
    })).resolves.toEqual([
      expect.objectContaining({
        actorUserId: "owner-a",
        action: "owner.exported",
        details: {
          filters: { employeeUserId: "employee-a" },
          rowCount: 1,
        },
      }),
    ]);
  });

  it("denies employees before producing or auditing an export", async () => {
    const { repository } = configure();

    const response = await GET(requestAs("employee-a"));

    expect(response.status).toBe(403);
    await expect(repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "owner_export",
      targetId: "export-1",
    })).resolves.toEqual([]);
  });

  it("exports duplicate-customer labels for affected rows without exposing customer IDs", async () => {
    const fingerprint = `fp_${"e".repeat(64)}`;
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository({
      seed: {
        customers: [
          { ...customer, customerFingerprint: fingerprint },
          {
            ...customer,
            id: "customer-b",
            employeeUserId: "employee-b",
            customerNumber: "C-019",
            customerAlias: "陈女士",
            customerFingerprint: fingerprint,
          },
        ],
        followUps: [followUp],
      },
    });
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
      createId: () => "export-duplicate",
    }));

    const response = await GET(requestAs("owner-a"));
    const csv = await response.text();

    expect(response.status).toBe(200);
    expect(csv).toContain("duplicate_customer");
    expect(csv).not.toContain("customer-a");
    expect(csv).not.toContain("customer-b");
  });
});
