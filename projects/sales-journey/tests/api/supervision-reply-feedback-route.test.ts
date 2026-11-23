import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/supervision/customers/[customerId]/reply-feedback/route";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import type { CurrentUser } from "@/server/auth/current-user";
import {
  configureSupervisionApiRuntime,
  createCustomerService,
  type PrivateTextVault,
} from "@/server/supervision/customer-service";
import type {
  D1DatabaseLike,
  D1PreparedStatementLike,
} from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { safeCard } from "../support/decision-card-fixtures";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const NOW = "2026-08-30T08:00:00.000Z";
const TOKEN_SECRET = "test-only-usage-token-secret-with-32-bytes";
const employeeA: CurrentUser = {
  accountUserId: "employee-a",
  email: "employee-a@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};
const customer: CustomerRecord = {
  id: "customer-a",
  tenantId: "tenant-a",
  employeeUserId: "employee-a",
  customerNumber: "C-A",
  customerAlias: "脱敏客户A",
  category: "绿松石",
  plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: NOW,
};

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
        const [id, email] = bindings;
        const users = [
          { id: "employee-a", email: "employee-a@shop.test" },
          { id: "employee-b", email: "employee-b@shop.test" },
        ];
        const match = users.find((user) => user.id === id && user.email === email);
        return { results: match ? [{
          id: match.id,
          tenant_id: "tenant-a",
          email: match.email,
          role: "employee",
        } as T] : [] };
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
let repository: MemorySupervisionRepository;
let usageToken: string;

function requestFor(user: "employee-a" | "employee-b", body: unknown): Request {
  return new Request("http://localhost/api/supervision/customers/customer-a/reply-feedback", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "oai-authenticated-user-id": user,
      "oai-authenticated-user-email": `${user}@shop.test`,
    },
    body: JSON.stringify(body),
  });
}

function context(customerId = "customer-a") {
  return { params: Promise.resolve({ customerId }) };
}

function validBody(overrides: Record<string, unknown> = {}) {
  return {
    usageToken,
    rating: "helpful",
    reasonCodes: [],
    finalReply: "这款纹理清楚，我把已核实的信息整理给您。",
    ...overrides,
  };
}

beforeEach(async () => {
  vi.stubEnv("NODE_ENV", "test");
  vi.unstubAllEnvs();
  repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
  const runtime = {
    d1: new MembershipD1(),
    repository,
    vault,
    usageTokenSecret: TOKEN_SECRET,
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => NOW,
  };
  restoreRuntime = configureSupervisionApiRuntime(() => runtime);
  usageToken = (await createCustomerService(runtime).issueDecisionUsageToken(
    employeeA,
    customer.id,
    safeCard,
  )).token;
});

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  vi.unstubAllEnvs();
});

describe("POST /api/supervision/customers/:customerId/reply-feedback", () => {
  it("accepts employee feedback with a minimum response and no internal identifiers", async () => {
    const response = await POST(requestFor("employee-a", validBody()), context());

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({
      accepted: true,
      outcome: "created",
      privacyStatus: "safe",
      privacyReasonCodes: [],
    });
  });

  it("returns the same not-found response for another employee's and an unknown customer", async () => {
    const forbidden = await POST(requestFor("employee-b", validBody()), context());
    const unknown = await POST(requestFor("employee-b", validBody()), context("customer-unknown"));

    expect(forbidden.status).toBe(404);
    expect(unknown.status).toBe(404);
    expect(await forbidden.json()).toEqual(await unknown.json());
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it.each(["tenantId", "employeeUserId"])(
    "rejects browser-provided %s before any write",
    async (field) => {
      const response = await POST(requestFor("employee-a", validBody({
        [field]: "browser-forged",
      })), context());

      expect(response.status).toBe(400);
      expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
    },
  );

  it("returns only quarantine metadata and never echoes or stores unsafe reply text", async () => {
    const privateReply = "收货地址：幸福花园 3 栋 201 室";
    const response = await POST(requestFor("employee-a", validBody({
      rating: "needs_improvement",
      reasonCodes: ["other"],
      finalReply: privateReply,
    })), context());
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(body).toEqual({
      accepted: true,
      outcome: "created",
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_address"],
    });
    expect(JSON.stringify(body)).not.toContain(privateReply);
    const rows = await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" });
    expect(rows[0]).not.toHaveProperty("sanitizedFinalReply");
    expect(JSON.stringify(rows)).not.toContain(privateReply);
  });

  it("fails closed with 503 in production when D1/runtime is absent", async () => {
    restoreRuntime?.();
    restoreRuntime = undefined;
    vi.stubEnv("NODE_ENV", "production");

    const response = await POST(requestFor("employee-a", validBody()), context());

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "SUPERVISION_RUNTIME_UNAVAILABLE" });
  });
});
