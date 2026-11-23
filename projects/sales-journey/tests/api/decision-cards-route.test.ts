import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/decision-cards/route";
import { identityBeadRequest, requestFor } from "../support/decision-card-fixtures";
import { SALES_SCENARIOS } from "@/domain/decision-card/types";
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
import { configureProductStrategyRuntime } from "@/server/product-strategy/runtime";
import { MemoryProductStrategyReceiptRepository } from "@/server/product-strategy/repository";
import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const NOW = "2026-08-30T08:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-a",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: NOW,
};
const priorFollowUp: FollowUpRecord = {
  id: "follow-up-server-owned",
  tenantId: "tenant-a",
  customerId: "customer-a",
  employeeUserId: "employee-1",
  customerMessageVaultAssetId: "vault-private-history",
  customerMessageSummary: "RAW-SERVER-HISTORY",
  plannedFollowUpAt: "2026-08-30T07:00:00.000Z",
  actualFollowUpAt: "2026-08-30T07:00:00.000Z",
  result: "interested",
  nextAction: "PRIVATE-NEXT-ACTION",
  decisionCardId: "card-old",
  replyUsage: "edited",
  finalEditedText: "RAW-EDITED-HISTORY",
  version: 1,
  createdAt: "2026-08-30T07:00:00.000Z",
};

class MembershipD1 implements D1DatabaseLike {
  prepare(): D1PreparedStatementLike {
    const statement: D1PreparedStatementLike = {
      bind: () => statement,
      first: async () => null,
      all: async <T>() => ({ results: [{
        id: "employee-1",
        tenant_id: "tenant-a",
        email: "employee-1@shop.test",
        role: "employee",
      } as T] }),
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

beforeEach(() => {
  vi.stubEnv("NODE_ENV", "development");
  vi.stubEnv("SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE", "desensitized-only");
  restoreRuntime = configureSupervisionApiRuntime(() => ({
    d1: new MembershipD1(),
    repository: new MemorySupervisionRepository({
      seed: { customers: [customer], followUps: [priorFollowUp] },
    }),
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => NOW,
  }));
});

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  vi.unstubAllEnvs();
});

function decisionRequest(body: unknown): Request {
  return new Request("http://localhost/api/decision-cards", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "oai-authenticated-user-id": "employee-1",
      "oai-authenticated-user-email": "employee-1@shop.test",
    },
    body: JSON.stringify({ ...(body as object), customerId: "customer-a" }),
  });
}

function decodeUsagePayload(token: string): Record<string, unknown> {
  const encodedPayload = token.split(".")[0];
  if (!encodedPayload) throw new Error("test token has no payload");
  return JSON.parse(Buffer.from(encodedPayload, "base64url").toString("utf8")) as Record<
    string,
    unknown
  >;
}

const demoRequest = {
  ...identityBeadRequest,
  tenantId: "demo-tenant",
  operatorId: "demo-operator",
  evidence: [],
};

describe("POST /api/decision-cards", () => {
  it("adds the latest product snapshot as non-verified preview evidence", async () => {
    const strategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
      process.cwd(),
      "fixtures/integrations/sales-strategy-input.synthetic.json",
    ), "utf8")));
    const repository = new MemoryProductStrategyReceiptRepository();
    await repository.accept({
      tenantId: "tenant-a",
      outboxEventId: "66666666-6666-4666-8666-666666666666",
      payloadSha256: "a".repeat(64),
      sourceEventId: "55555555-5555-4555-8555-555555555555",
      productCode: strategyInput.product_code,
      strategyInputVersion: strategyInput.strategy_input_version,
      productDataVersion: strategyInput.product_data_version,
      contentSha256: strategyInput.content_sha256,
      status: "preview_only",
      receivedAt: NOW,
      strategyInput: {
        ...strategyInput,
        tenant_id: "tenant-a",
        visual_observations: ["红色珠串，可见自然纹理"],
        selling_point_candidates: ["适合日常佩戴"],
        service_rule_summaries: ["尺寸调整需门店确认"],
        unverified_items: ["材质待人工确认"],
        forbidden_expressions: ["官方保真"],
      },
    });
    const restoreProductRuntime = configureProductStrategyRuntime(() => ({
      repository,
      fetcher: { fetch: async () => strategyInput },
      signingSecret: "synthetic-sales-delivery-secret-with-32-bytes",
      nowSeconds: () => 1_788_112_000,
    }));
    try {
      const { productDescription: _browserDescription, ...requestWithoutDescription } = identityBeadRequest;
      const response = await POST(decisionRequest({
        ...requestWithoutDescription,
        evidence: [],
        productCode: strategyInput.product_code,
      }));

      expect(response.status).toBe(200);
      const body = await response.json();
      // Candidate facts are available to the card builder, but are not cited as verified proof.
      expect(body.card.audit.evidenceIds).toEqual([]);
      expect(body.evidenceTitles["product-strategy-SYNTHETIC-001-v1"])
        .toContain("仅供人工预览");
      expect(body.card.summary).toContain("可用于观察：红色珠串，可见自然纹理");
      expect(body.card.summary).toContain("仅作候选：材质 南红、形态 手串");
      expect(body.card.summary).toContain("适合日常佩戴");
      expect(body.card.summary).toContain("禁止对客表达：官方保真");
      expect(body.card.productStrategySeal).toEqual({
        productCode: strategyInput.product_code,
        strategyInputVersion: strategyInput.strategy_input_version,
        contentSha256: strategyInput.content_sha256,
        status: "preview_only",
      });
      const customerFacingReply = body.card.replyOptions
        .map((reply: { text: string }) => reply.text)
        .join("\n");
      expect(customerFacingReply).not.toContain("适合日常佩戴");
      expect(customerFacingReply).not.toContain("官方保真");
      expect(customerFacingReply).not.toContain("材质待人工确认");
    } finally {
      restoreProductRuntime();
    }
  });

  it("returns a copy-only card for a valid request", async () => {
    const response = await POST(
      decisionRequest(identityBeadRequest),
    );

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.card.deliveryMode).toBe("copy_only");
    expect(body.usageToken).toEqual(expect.any(String));
    expect(body.usageTokenExpiresAt).toBe("2026-08-30T08:15:00.000Z");
  });

  it.each(SALES_SCENARIOS)("returns the requested %s scenario in copy-only mode", async (scenario) => {
    const response = await POST(
      decisionRequest(requestFor(scenario)),
    );

    expect(response.status).toBe(200);
    expect((await response.json()).card).toMatchObject({ scenario, deliveryMode: "copy_only" });
  });

  it("uses the authenticated tenant while keeping approved evidence isolated per request", async () => {
    const requestA = {
      ...identityBeadRequest,
      requestId: "tenant-a-request",
      tenantId: "tenant-a",
      evidence: [{ ...identityBeadRequest.evidence[0], id: "local-agarwood-identity_bead" }],
    };
    const requestB = {
      ...identityBeadRequest,
      requestId: "tenant-b-request",
      tenantId: "tenant-b",
      evidence: [{ ...identityBeadRequest.evidence[0], id: "local-turquoise-identity_bead" }],
    };

    const [responseA, responseB] = await Promise.all(
      [requestA, requestB].map((request) =>
        POST(decisionRequest(request)),
      ),
    );

    expect((await responseA.json()).card.audit).toMatchObject({
      tenantId: "tenant-a",
      evidenceIds: ["local-agarwood-identity_bead"],
    });
    expect((await responseB.json()).card.audit).toMatchObject({
      tenantId: "tenant-a",
      evidenceIds: ["local-turquoise-identity_bead"],
    });
  });

  it("does not require meaningless tenant or operator placeholders from authenticated employees", async () => {
    const { tenantId: _tenantId, operatorId: _operatorId, ...employeeInput } = identityBeadRequest;
    const response = await POST(decisionRequest(employeeInput));

    expect(response.status).toBe(200);
    expect((await response.json()).card.audit).toMatchObject({
      tenantId: "tenant-a",
      operatorId: "employee-1",
    });
  });

  it("ignores browser-selected tenant or operator identities", async () => {
    for (const body of [
      { ...demoRequest, tenantId: "forged-tenant" },
      { ...demoRequest, operatorId: "forged-operator" },
    ]) {
      const response = await POST(decisionRequest(body));

      expect(response.status).toBe(200);
      expect((await response.json()).card.audit).toMatchObject({
        tenantId: "tenant-a",
        operatorId: "employee-1",
      });
    }
  });

  it("ignores forged browser journey state and signs the server-derived ordered snapshot", async () => {
    const response = await POST(decisionRequest({
      ...identityBeadRequest,
      tenantId: "forged-tenant",
      operatorId: "forged-employee",
      currentStage: "advance",
      history: [{ text: "FORGED-BROWSER-HISTORY" }],
      conversation: [
        { speaker: "customer", text: "FORGED-BROWSER-CONVERSATION" },
      ],
      evidence: [
        { ...identityBeadRequest.evidence[0], id: "local-agarwood-material" },
        { ...identityBeadRequest.evidence[0], id: "local-agarwood-identity_bead" },
      ],
    }));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.card).toMatchObject({
      scenario: "identity_bead",
      salesStage: "evaluate",
      deliveryMode: "copy_only",
    });
    expect(decodeUsagePayload(body.usageToken)).toMatchObject({
      version: "decision-usage.v3",
      tenantId: "tenant-a",
      customerId: "customer-a",
      scenario: "identity_bead",
      salesStage: "evaluate",
      evidenceIds: [
        "local-agarwood-material",
        "local-agarwood-identity_bead",
      ],
      deliveryMode: "copy_only",
    });
    const serialized = JSON.stringify(body);
    expect(serialized).not.toContain("FORGED-BROWSER-HISTORY");
    expect(serialized).not.toContain("FORGED-BROWSER-CONVERSATION");
    expect(serialized).not.toContain("RAW-SERVER-HISTORY");
    expect(serialized).not.toContain("PRIVATE-NEXT-ACTION");
    expect(serialized).not.toContain("RAW-EDITED-HISTORY");
    expect(serialized).not.toContain("vault-private-history");
  });

  it("replaces browser evidence metadata with the server-approved record", async () => {
    const response = await POST(decisionRequest({
      ...demoRequest,
      evidence: [{
          id: "local-agarwood-identity_bead",
          kind: "certificate",
          title: "浏览器伪造的批准资料",
          verifiedFields: ["origin"],
          status: "withdrawn",
      }],
    }));

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.card.safety.status).toBe("safe");
    expect(body.card.audit.evidenceIds).toEqual(["local-agarwood-identity_bead"]);
    expect(body.evidenceTitles).toEqual({
      "local-agarwood-identity_bead": "沉香身份珠说明（本地已批准演示资料）",
    });
    expect(JSON.stringify(body)).not.toContain("浏览器伪造的批准资料");
  });

  it("fails closed for demo evidence outside explicit synthetic-local mode", async () => {
    vi.stubEnv("NODE_ENV", "production");

    const response = await POST(decisionRequest({
      ...demoRequest,
      evidence: [{
        id: "local-agarwood-identity_bead",
        kind: "approved_copy",
        title: "浏览器请求演示资料",
        verifiedFields: ["identity_bead"],
        status: "approved",
      }],
    }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "UNAPPROVED_EVIDENCE" });
  });

  it("rejects evidence that is absent from the server-approved registry", async () => {
    const response = await POST(decisionRequest({
      ...demoRequest,
      evidence: [{
          id: "forged-approved-evidence",
          kind: "approved_copy",
          title: "伪造资料",
          verifiedFields: ["certificate"],
          status: "approved",
      }],
    }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "UNAPPROVED_EVIDENCE" });
  });

  it("returns a structured 400 for an invalid request", async () => {
    const response = await POST(
      decisionRequest({ scenario: "unknown" }),
    );

    expect(response.status).toBe(400);
    expect((await response.json()).error).toBe("INVALID_DECISION_CARD_REQUEST");
  });

  it("returns a structured 400 when the body is not JSON", async () => {
    const response = await POST(
      new Request("http://localhost/api/decision-cards", {
        method: "POST",
        headers: {
          "oai-authenticated-user-id": "employee-1",
          "oai-authenticated-user-email": "employee-1@shop.test",
        },
        body: "not-json",
      }),
    );

    expect(response.status).toBe(400);
    expect((await response.json()).error).toBe("INVALID_DECISION_CARD_REQUEST");
  });

  it("ignores forged tenant and operator identity in favor of the authenticated membership", async () => {
    const response = await POST(decisionRequest({
      ...identityBeadRequest,
      tenantId: "tenant-forged",
      operatorId: "employee-forged",
    }));

    expect(response.status).toBe(200);
    expect((await response.json()).card.audit).toMatchObject({
      tenantId: "tenant-a",
      operatorId: "employee-1",
    });
  });
});
