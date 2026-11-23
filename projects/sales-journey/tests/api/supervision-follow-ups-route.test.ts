import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { POST as CREATE_CARD } from "@/app/api/decision-cards/route";
import {
  GET,
  POST,
} from "@/app/api/supervision/customers/[customerId]/follow-ups/route";
import { POST as CORRECT } from "@/app/api/supervision/customers/[customerId]/corrections/route";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";
import { MemoryProductStrategyReceiptRepository } from "@/server/product-strategy/repository";
import { configureProductStrategyRuntime } from "@/server/product-strategy/runtime";
import {
  configureSupervisionApiRuntime,
  type PrivateTextVault,
} from "@/server/supervision/customer-service";
import type {
  D1DatabaseLike,
  D1PreparedStatementLike,
} from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { identityBeadRequest } from "../support/decision-card-fixtures";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const NOW = "2026-08-30T08:00:00.000Z";
const FUTURE = "2026-08-31T08:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "tenant-a",
  employeeUserId: "employee-1",
  customerNumber: "C-001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: FUTURE,
  status: "pending",
  version: 1,
  createdAt: NOW,
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
        const id = String(call.bindings[0]);
        return {
          results: id === "employee-1" || id === "employee-2" ? [{
            id,
            tenant_id: "tenant-a",
            email: `${id}@shop.test`,
            role: "employee",
          } as T] : [],
        };
      },
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

class RecordingTextVault implements PrivateTextVault {
  readonly contents: string[] = [];

  async putText(input: { content: string }): Promise<void> {
    this.contents.push(input.content);
  }

  async deleteText() {}
}

let restoreRuntime: (() => void) | undefined;
let restoreProductRuntime: (() => void) | undefined;

beforeEach(() => {
  vi.stubEnv("NODE_ENV", "development");
  vi.stubEnv("SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE", "desensitized-only");
});

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  restoreProductRuntime?.();
  restoreProductRuntime = undefined;
  vi.unstubAllEnvs();
});

function employeeRequest(
  path: string,
  body?: unknown,
  employeeId = "employee-1",
): Request {
  return new Request(`http://localhost${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "content-type": "application/json",
      "oai-authenticated-user-id": employeeId,
      "oai-authenticated-user-email": `${employeeId}@shop.test`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const routeContext = { params: Promise.resolve({ customerId: "customer-1" }) };

describe("customer follow-up routes", () => {
  it("rejects a product-bound card after a newer strategy version arrives", async () => {
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
    const productRepository = new MemoryProductStrategyReceiptRepository();
    const strategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
      process.cwd(),
      "fixtures/integrations/sales-strategy-input.synthetic.json",
    ), "utf8")));
    await productRepository.accept({
      tenantId: "tenant-a",
      outboxEventId: "66666666-6666-4666-8666-666666666666",
      payloadSha256: "a".repeat(64),
      sourceEventId: "55555555-5555-4555-8555-555555555555",
      productCode: strategyInput.product_code,
      strategyInputVersion: 1,
      productDataVersion: strategyInput.product_data_version,
      contentSha256: strategyInput.content_sha256,
      status: "preview_only",
      receivedAt: NOW,
      strategyInput: { ...strategyInput, tenant_id: "tenant-a" },
    });
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault: new RecordingTextVault(),
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));
    restoreProductRuntime = configureProductStrategyRuntime(() => ({
      repository: productRepository,
      fetcher: { fetch: async () => strategyInput },
      signingSecret: "synthetic-sales-delivery-secret-with-32-bytes",
      nowSeconds: () => 1_788_112_000,
    }));
    const { productDescription: _description, ...requestWithoutDescription } = identityBeadRequest;
    const cardResponse = await CREATE_CARD(employeeRequest("/api/decision-cards", {
      ...requestWithoutDescription,
      evidence: [],
      customerId: "customer-1",
      productCode: strategyInput.product_code,
    }));
    const cardBody = await cardResponse.json();
    expect(cardResponse.status).toBe(200);
    await productRepository.accept({
      tenantId: "tenant-a",
      outboxEventId: "77777777-7777-4777-8777-777777777777",
      payloadSha256: "b".repeat(64),
      sourceEventId: "88888888-8888-4888-8888-888888888888",
      productCode: strategyInput.product_code,
      strategyInputVersion: 2,
      productDataVersion: strategyInput.product_data_version + 1,
      contentSha256: "c".repeat(64),
      status: "preview_only",
      receivedAt: "2026-08-30T08:01:00.000Z",
      strategyInput: {
        ...strategyInput,
        tenant_id: "tenant-a",
        strategy_input_version: 2,
        product_data_version: strategyInput.product_data_version + 1,
        content_sha256: "c".repeat(64),
      },
    });

    const response = await POST(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      {
        requestId: "stale-product-api",
        usageToken: cardBody.usageToken,
        customerMessage: "请再确认商品资料。",
        plannedFollowUpAt: FUTURE,
        result: "pending",
        nextAction: "重新生成建议",
        decisionCardId: cardBody.card.cardId,
        replyUsage: "not_used",
      },
    ), routeContext);

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "STALE_PRODUCT_STRATEGY" });
    await expect(repository.listTimeline({ tenantId: "tenant-a", customerId: customer.id }))
      .resolves.toHaveLength(0);
  });

  it("does not expose an employee's customer to another employee", async () => {
    const d1 = new MembershipD1();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
      vault: new RecordingTextVault(),
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));

    const response = await GET(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      undefined,
      "employee-2",
    ), routeContext);

    expect(response.status).toBe(404);
    expect(d1.calls[0]?.sql).toContain("FROM users");
  });

  it("uses a signed card token to append a privacy-safe follow-up and correction", async () => {
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
    const vault = new RecordingTextVault();
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault,
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));

    const cardResponse = await CREATE_CARD(employeeRequest("/api/decision-cards", {
      ...identityBeadRequest,
      customerId: "customer-1",
    }));
    const cardBody = await cardResponse.json();
    expect(cardResponse.status).toBe(200);
    expect(cardBody.card.deliveryMode).toBe("copy_only");
    expect(cardBody.usageToken).toEqual(expect.any(String));
    expect(d1.calls.filter((call) => call.sql.includes("FROM users"))).toHaveLength(1);

    const followUpResponse = await POST(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      {
        requestId: "follow-up-api-1",
        usageToken: cardBody.usageToken,
        customerMessage: "我是林敏，预算两万元。",
        recentContext: "住址在某小区。",
        plannedFollowUpAt: FUTURE,
        actualFollowUpAt: NOW,
        result: "interested",
        nextAction: "明天确认到店",
        decisionCardId: cardBody.card.cardId,
        replyUsage: "edited",
        chosenReplyId: cardBody.card.replyOptions[0].id,
        finalEditedText: "您好，我们明天再确认。",
      },
    ), routeContext);
    const followUpBody = await followUpResponse.json();

    expect(followUpResponse.status).toBe(201);
    expect(followUpBody).toEqual({
      followUp: { id: expect.any(String) },
    });
    for (const internalKey of [
      "tenantId",
      "employeeUserId",
      "idempotencyKey",
      "requestFingerprint",
      "customerMessageVaultAssetId",
      "cardInstanceId",
      "cardDigest",
      "providedReplyIds",
      "chosenReplyId",
      "finalEditedText",
      "version",
      "vaultAsset",
      "decisionUsage",
    ]) {
      expect(JSON.stringify(followUpBody)).not.toContain(`"${internalKey}"`);
    }
    expect(vault.contents.join(" ")).toContain("林敏");
    expect(d1.calls.filter((call) => call.sql.includes("FROM users"))).toHaveLength(2);

    const correctionResponse = await CORRECT(employeeRequest(
      "/api/supervision/customers/customer-1/corrections",
      {
        requestId: "correction-api-1",
        followUpId: followUpBody.followUp.id,
        reason: "结果选错，补充说明。",
      },
    ), routeContext);
    expect(correctionResponse.status).toBe(201);
    const correctionBody = await correctionResponse.json();
    expect(correctionBody.correction).toEqual({
      kind: "correction",
      id: expect.any(String),
      actorLabel: "employee-1@shop.test",
      createdAt: NOW,
      followUpId: followUpBody.followUp.id,
      reason: "结果选错，补充说明。",
    });
    for (const internalKey of [
      "tenantId",
      "customerId",
      "actorUserId",
      "idempotencyKey",
      "requestFingerprint",
      "version",
    ]) {
      expect(JSON.stringify(correctionBody)).not.toContain(`"${internalKey}"`);
    }
    expect(d1.calls.filter((call) => call.sql.includes("FROM users"))).toHaveLength(3);

    const correctionRetry = await CORRECT(employeeRequest(
      "/api/supervision/customers/customer-1/corrections",
      {
        requestId: "correction-api-1",
        followUpId: followUpBody.followUp.id,
        reason: "结果选错，补充说明。",
      },
    ), routeContext);
    expect(correctionRetry.status).toBe(201);
    expect((await correctionRetry.json()).correction.id)
      .toBe((await repository.listTimeline({
        tenantId: "tenant-a",
        customerId: "customer-1",
      })).find((entry) => entry.kind === "correction")?.record.id);
  });

  it("never lets a client request id containing personal data enter structured records", async () => {
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault: new RecordingTextVault(),
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));
    const unsafeRequestId = "林敏-13800138000";

    const cardResponse = await CREATE_CARD(employeeRequest("/api/decision-cards", {
      ...identityBeadRequest,
      requestId: unsafeRequestId,
      customerId: "customer-1",
    }));
    const cardBody = await cardResponse.json();
    expect(cardResponse.status).toBe(200);
    expect(JSON.stringify(cardBody)).not.toContain("林敏");
    expect(JSON.stringify(cardBody)).not.toContain("13800138000");
    const encodedPayload = String(cardBody.usageToken).split(".")[0]!
      .replaceAll("-", "+")
      .replaceAll("_", "/");
    const decodedPayload = new TextDecoder().decode(Uint8Array.from(
      atob(encodedPayload.padEnd(Math.ceil(encodedPayload.length / 4) * 4, "=")),
      (character) => character.charCodeAt(0),
    ));
    expect(decodedPayload).not.toContain("林敏");
    expect(decodedPayload).not.toContain("13800138000");

    const followUpResponse = await POST(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      {
        requestId: unsafeRequestId,
        usageToken: cardBody.usageToken,
        customerMessage: "想再了解一下。",
        plannedFollowUpAt: FUTURE,
        result: "pending",
        nextAction: "两天后回访",
        decisionCardId: cardBody.card.cardId,
        replyUsage: "not_used",
      },
    ), routeContext);
    const followUpBody = await followUpResponse.json();
    expect(followUpResponse.status).toBe(201);

    const timelineResponse = await GET(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
    ), routeContext);
    expect(timelineResponse.status).toBe(200);
    const timelineBody = await timelineResponse.json();
    expect(timelineBody.customer).toEqual({
      id: "customer-1",
      customerNumber: "C-001",
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
      status: "pending",
    });
    expect(timelineBody.timeline).toEqual(expect.arrayContaining([
      expect.objectContaining({
        kind: "follow_up",
        actorLabel: "employee-1@shop.test",
        customerMessageSummary: "品类：沉香；意图：learn_identity。",
      }),
      expect.objectContaining({
        kind: "decision_usage",
        actorLabel: "employee-1@shop.test",
      }),
    ]));
    const structured = JSON.stringify({
      followUpBody,
      timeline: timelineBody,
    });
    expect(structured).not.toContain("林敏");
    expect(structured).not.toContain("13800138000");
    for (const internalKey of [
      "tenantId",
      "employeeUserId",
      "actorUserId",
      "idempotencyKey",
      "requestFingerprint",
      "customerFingerprint",
      "customerMessageVaultAssetId",
      "cardInstanceId",
      "cardDigest",
      "providedReplyIds",
      "chosenReplyId",
      "finalEditedText",
      "version",
      "vaultAsset",
    ]) {
      expect(structured).not.toContain(`"${internalKey}"`);
    }
  });

  it("rejects a token issued for a different card instance and content", async () => {
    const d1 = new MembershipD1();
    const repository = new MemorySupervisionRepository({ seed: { customers: [customer] } });
    restoreRuntime = configureSupervisionApiRuntime(() => ({
      d1,
      repository,
      vault: new RecordingTextVault(),
      usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
      ...TEST_RUNTIME_SEPARATE_SECRETS,
      now: () => NOW,
    }));
    const sharedRequestId = "same-client-request";
    const cardAResponse = await CREATE_CARD(employeeRequest("/api/decision-cards", {
      ...identityBeadRequest,
      requestId: sharedRequestId,
      customerId: "customer-1",
    }));
    const cardBResponse = await CREATE_CARD(employeeRequest("/api/decision-cards", {
      ...identityBeadRequest,
      requestId: sharedRequestId,
      latestCustomerMessage: "请用更温暖的方式说明。",
      operatorStyle: { tone: "warm", maxReplyCharacters: 80, preferredGreeting: "您好" },
      evidence: [{ ...identityBeadRequest.evidence[0], id: "local-turquoise-identity_bead" }],
      customerId: "customer-1",
    }));
    const cardA = await cardAResponse.json();
    const cardB = await cardBResponse.json();

    expect(cardA.cardInstanceId).toEqual(expect.any(String));
    expect(cardB.cardInstanceId).toEqual(expect.any(String));
    expect(cardB.cardInstanceId).not.toBe(cardA.cardInstanceId);
    expect(cardB.cardDigest).not.toBe(cardA.cardDigest);

    const mismatchedResponse = await POST(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      {
        requestId: "follow-up-card-binding",
        usageToken: cardA.usageToken,
        cardInstanceId: cardB.cardInstanceId,
        customerMessage: "想再了解一下。",
        plannedFollowUpAt: FUTURE,
        result: "pending",
        nextAction: "两天后回访",
        decisionCardId: cardB.card.cardId,
        replyUsage: "not_used",
      },
    ), routeContext);
    expect(mismatchedResponse.status).toBe(400);
    expect((await mismatchedResponse.json()).error).toBe("INVALID_USAGE_TOKEN");

    const acceptedResponse = await POST(employeeRequest(
      "/api/supervision/customers/customer-1/follow-ups",
      {
        requestId: "follow-up-card-binding-b",
        usageToken: cardB.usageToken,
        cardInstanceId: cardB.cardInstanceId,
        cardDigest: cardB.cardDigest,
        customerMessage: "想再了解一下。",
        plannedFollowUpAt: FUTURE,
        result: "pending",
        nextAction: "两天后回访",
        decisionCardId: cardB.card.cardId,
        replyUsage: "not_used",
      },
    ), routeContext);
    const accepted = await acceptedResponse.json();
    expect(acceptedResponse.status).toBe(201);
    expect(accepted).toEqual({ followUp: { id: expect.any(String) } });
  });
});
