import { afterEach, describe, expect, it } from "vitest";

import { GET } from "@/app/api/supervision/owner/reply-quality/route";
import type { ReplyFeedbackRecord } from "@/domain/supervision/contracts";
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

const feedbackBase = {
  decisionCardId: "card-a",
  cardDigest: `sha256_${"a".repeat(64)}`,
  scenario: "product_selection",
  salesStage: "evaluate",
  evidenceIds: ["evidence-a"],
  createdAt: "2026-08-30T08:00:00.000Z",
} as const;

const feedback: ReplyFeedbackRecord[] = [
  {
    ...feedbackBase,
    id: "feedback-safe-a",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerId: "customer-a",
    cardInstanceId: "instance-safe-a",
    agentVersion: "reply-agent.v2",
    rating: "helpful",
    reasonCodes: [],
    privacyStatus: "safe",
    privacyReasonCodes: [],
    sanitizedFinalReply: "SAFE_TENANT_A_REPLY",
    updatedAt: "2026-08-30T08:03:00.000Z",
  },
  {
    ...feedbackBase,
    id: "feedback-quarantine-a",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerId: "customer-a",
    cardInstanceId: "instance-quarantine-a",
    agentVersion: "reply-agent.v3",
    rating: "needs_improvement",
    reasonCodes: ["too_long"],
    privacyStatus: "quarantined",
    privacyReasonCodes: ["residual_phone"],
    updatedAt: "2026-08-30T08:02:00.000Z",
  },
  {
    ...feedbackBase,
    id: "feedback-safe-b",
    tenantId: "tenant-b",
    employeeUserId: "employee-b",
    customerId: "customer-b",
    cardInstanceId: "instance-safe-b",
    agentVersion: "reply-agent.v2",
    rating: "needs_improvement",
    reasonCodes: ["other"],
    privacyStatus: "safe",
    privacyReasonCodes: [],
    sanitizedFinalReply: "OTHER_TENANT_REPLY",
    updatedAt: "2026-08-30T08:04:00.000Z",
  },
];

class MembershipD1 implements D1DatabaseLike {
  prepare(sql: string): D1PreparedStatementLike {
    let bindings: readonly unknown[] = [];
    const statement: D1PreparedStatementLike = {
      bind: (...values) => {
        bindings = values;
        return statement;
      },
      first: async () => null,
      all: async <T>() => {
        if (sql.includes("FROM users") && sql.includes("active = 1") && bindings.length === 1) {
          return { results: [{ id: "employee-a", email: "employee-a@shop.test" } as T] };
        }
        if (sql.includes("FROM customers")) {
          return { results: [{
            id: "customer-a",
            customer_number: "C-018",
            customer_alias: "林女士",
            employee_user_id: "employee-a",
            category: "沉香",
            planned_follow_up_at: "2026-08-30T10:00:00.000Z",
            status: "pending",
          } as T] };
        }
        const id = String(bindings[0]);
        return { results: [{
          id,
          tenant_id: id.endsWith("-b") ? "tenant-b" : "tenant-a",
          email: `${id}@shop.test`,
          role: id.startsWith("owner") ? "owner" : "employee",
        } as T] };
      },
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

const vault: PrivateTextVault = { async putText() {}, async deleteText() {} };
let restoreRuntime: (() => void) | undefined;

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
});

function requestAs(id: string, query = ""): Request {
  return new Request(`http://localhost/api/supervision/owner/reply-quality${query}`, {
    headers: {
      "oai-authenticated-user-id": id,
      "oai-authenticated-user-email": `${id}@shop.test`,
    },
  });
}

function configure(): void {
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1: new MembershipD1(),
    repository: new MemorySupervisionRepository({ seed: { replyFeedback: feedback } }),
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
  }));
}

describe("owner reply-quality route", () => {
  it("rejects employees before returning tenant quality data", async () => {
    configure();
    await expect(GET(requestAs("employee-a"))).resolves.toMatchObject({ status: 403 });
  });

  it("returns only the authenticated owner's tenant with safe directory refs", async () => {
    configure();

    const response = await GET(requestAs("owner-a"));
    const body = await response.json();
    const serialized = JSON.stringify(body);

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store, private");
    expect(body.quality.totals).toEqual({ helpful: 1, needsImprovement: 1, quarantined: 1 });
    expect(body.quality.samples[0]).toMatchObject({
      employeeRef: expect.stringMatching(/^employee-view-/u),
      customerRef: expect.stringMatching(/^customer-view-/u),
    });
    expect(serialized).not.toMatch(/tenant-a|employee-a(?!@)|customer-a|feedback-safe-a/u);
    expect(serialized).not.toContain("OTHER_TENANT_REPLY");
    const quarantined = body.quality.samples.find(
      (sample: { privacyStatus: string }) => sample.privacyStatus === "quarantined",
    );
    expect(quarantined).not.toHaveProperty("sanitizedFinalReply");
  });

  it("supports safe employee, scenario, stage, and version filters", async () => {
    configure();
    const initial = await GET(requestAs("owner-a"));
    const initialBody = await initial.json();
    const employeeRef = initialBody.quality.samples[0].employeeRef as string;

    const filtered = await GET(requestAs(
      "owner-a",
      `?employeeRef=${employeeRef}&scenario=product_selection&salesStage=evaluate&agentVersion=reply-agent.v3`,
    ));

    expect(filtered.status).toBe(200);
    expect((await filtered.json()).quality).toMatchObject({
      totals: { helpful: 0, needsImprovement: 1, quarantined: 1 },
      samples: [{ agentVersion: "reply-agent.v3", privacyStatus: "quarantined" }],
    });
    await expect(GET(requestAs("owner-a", "?employeeRef=employee-view-tampered")))
      .resolves.toMatchObject({ status: 400 });
    await expect(GET(requestAs("owner-a", "?tenantId=tenant-b")))
      .resolves.toMatchObject({ status: 400 });
  });

  it("ignores empty known form filters while applying the selected scenario", async () => {
    configure();

    const response = await GET(requestAs(
      "owner-a",
      "?employeeRef=&scenario=product_selection&salesStage=&agentVersion=",
    ));

    expect(response.status).toBe(200);
    expect((await response.json()).quality).toMatchObject({
      totals: { helpful: 1, needsImprovement: 1, quarantined: 1 },
      samples: [
        { scenario: "product_selection", privacyStatus: "safe" },
        { scenario: "product_selection", privacyStatus: "quarantined" },
      ],
    });
  });
});
