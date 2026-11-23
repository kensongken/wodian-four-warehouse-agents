import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { CurrentUser } from "@/server/auth/current-user";
import type { CustomerRecord, CreateReplyFeedbackInput } from "@/domain/supervision/contracts";
import {
  createCustomerService,
  CustomerNotFoundError,
  type PrivateTextVault,
} from "@/server/supervision/customer-service";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { safeCard } from "../support/decision-card-fixtures";

const NOW = "2026-08-30T08:00:00.000Z";
const EXPIRED_NOW = "2026-08-30T08:16:00.000Z";
const TOKEN_SECRET = "test-only-usage-token-secret-with-32-bytes";

const employeeA: CurrentUser = {
  accountUserId: "employee-a",
  email: "employee-a@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};

const employeeB: CurrentUser = {
  accountUserId: "employee-b",
  email: "employee-b@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};

const customers: CustomerRecord[] = [
  {
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
  },
  {
    id: "customer-a-2",
    tenantId: "tenant-a",
    employeeUserId: "employee-a",
    customerNumber: "C-A-2",
    customerAlias: "脱敏客户A2",
    category: "绿松石",
    plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
    status: "pending",
    version: 1,
    createdAt: NOW,
  },
];

const vault: PrivateTextVault = {
  async putText() {},
  async deleteText() {},
};

function createHarness() {
  let clock = NOW;
  let sequence = 0;
  const repository = new MemorySupervisionRepository({
    seed: { customers },
    now: () => clock,
    createId: () => `generated-${++sequence}`,
  });
  const service = createCustomerService({
    repository,
    vault,
    usageTokenSecret: TOKEN_SECRET,
    idempotencyKeyring: [{
      version: "v1",
      secret: "stable-idempotency-secret-v1-with-32-bytes",
    }],
    now: () => clock,
    createId: () => `service-${++sequence}`,
  });
  return {
    repository,
    service,
    expireToken: () => { clock = EXPIRED_NOW; },
    setClock: (value: string) => { clock = value; },
  };
}

function decodePayload(token: string): Record<string, unknown> {
  const [encodedPayload] = token.split(".");
  if (!encodedPayload) throw new Error("test token has no payload");
  return JSON.parse(Buffer.from(encodedPayload, "base64url").toString("utf8")) as Record<
    string,
    unknown
  >;
}

async function signedTokenWith(
  token: string,
  overrides: Record<string, unknown>,
): Promise<string> {
  const payload = { ...decodePayload(token), ...overrides };
  const encodedPayload = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(TOKEN_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(encodedPayload),
  );
  return `${encodedPayload}.${Buffer.from(signature).toString("base64url")}`;
}

function feedbackRequest(usageToken: string, overrides: Record<string, unknown> = {}) {
  return {
    usageToken,
    rating: "helpful",
    reasonCodes: [],
    finalReply: "这款纹理清楚，我把已核实的信息整理给您。",
    ...overrides,
  };
}

describe("authenticated employee reply feedback service", () => {
  it("derives every immutable feedback snapshot field from the signed card proof", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);

    const verified = await service.verifyDecisionUsageContext(
      employeeA,
      "customer-a",
      usage.token,
    );
    expect(verified).toEqual({
      tenantId: "tenant-a",
      employeeUserId: "employee-a",
      customerId: "customer-a",
      decisionCardId: safeCard.cardId,
      cardInstanceId: usage.cardInstanceId,
      cardDigest: usage.cardDigest,
      agentVersion: "reply-agent.v2",
      scenario: safeCard.scenario,
      salesStage: safeCard.salesStage,
      evidenceIds: safeCard.audit.evidenceIds,
    });

    const result = await service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    );

    expect(result).toMatchObject({
      outcome: "created",
      record: {
        tenantId: "tenant-a",
        employeeUserId: "employee-a",
        customerId: "customer-a",
        decisionCardId: safeCard.cardId,
        cardInstanceId: usage.cardInstanceId,
        cardDigest: usage.cardDigest,
        scenario: safeCard.scenario,
        salesStage: safeCard.salesStage,
        evidenceIds: safeCard.audit.evidenceIds,
        privacyStatus: "safe",
      },
    });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toHaveLength(1);
  });

  it("rejects a browser-forged agent version even when the decision proof is fresh", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token, { agentVersion: "reply-agent.forged" }),
    )).rejects.toBeInstanceOf(z.ZodError);
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("does not reveal whether another employee's customer exists", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);

    await expect(service.saveReplyFeedback(
      employeeB,
      "customer-a",
      feedbackRequest(usage.token),
    )).rejects.toBeInstanceOf(CustomerNotFoundError);
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("rejects a signed proof for a different owned customer with no write", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a-2",
      feedbackRequest(usage.token),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("rejects an expired proof for first creation and never writes", async () => {
    const { expireToken, repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    expireToken();

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("never writes feedback with a missing card proof", async () => {
    const { repository, service } = createHarness();

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest("placeholder", { usageToken: undefined }),
    )).rejects.toBeInstanceOf(z.ZodError);
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("never writes feedback with an unknown card proof", async () => {
    const { repository, service } = createHarness();

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest("unknown.signed-proof"),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("accepts only the v2 signed proof shape and rejects a correctly signed v1 proof", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const v1Token = await signedTokenWith(usage.token, { version: "decision-usage.v1" });

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(v1Token),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    )).resolves.toMatchObject({ outcome: "created" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toHaveLength(1);
  });

  it.each([
    [
      "a future instant with a negative offset",
      "2026-08-30T00:15:00.000-08:00",
      "2026-08-30T08:14:59.999Z",
      undefined,
    ],
    [
      "an expired instant with a positive offset",
      "2026-08-30T16:15:00.000+08:00",
      "2026-08-30T08:15:00.001Z",
      "EXPIRED_USAGE_TOKEN",
    ],
    [
      "the exact expiry instant",
      "2026-08-30T16:15:00.000+08:00",
      "2026-08-30T08:15:00.000Z",
      "EXPIRED_USAGE_TOKEN",
    ],
  ] as const)("compares expiry by epoch milliseconds for %s", async (
    _case,
    expiresAt,
    trustedNow,
    expectedError,
  ) => {
    const { service, setClock } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const offsetToken = await signedTokenWith(usage.token, { expiresAt });
    setClock(trustedNow);

    const verification = service.verifyDecisionUsageContext(
      employeeA,
      "customer-a",
      offsetToken,
    );
    if (expectedError) {
      await expect(verification).rejects.toMatchObject({ code: expectedError });
    } else {
      await expect(verification).resolves.toMatchObject({ cardInstanceId: usage.cardInstanceId });
    }
  });

  it("fails closed at the exact expiry boundary before first feedback creation", async () => {
    const { repository, service, setClock } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const boundaryToken = await signedTokenWith(usage.token, {
      expiresAt: "2026-08-30T16:15:00.000+08:00",
    });
    setClock("2026-08-30T08:15:00.000Z");

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(boundaryToken),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("fails closed when the trusted server clock is malformed", async () => {
    const { repository, service, setClock } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    setClock("not-a-timestamp");

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    )).rejects.toMatchObject({ status: 503, code: "SUPERVISION_RUNTIME_UNAVAILABLE" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("fails closed when a correctly signed proof contains a malformed expiry", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const malformedExpiryToken = await signedTokenWith(usage.token, {
      expiresAt: "not-a-timestamp",
    });

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(malformedExpiryToken),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("rejects a real issued token after its signature is tampered and writes no row", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const [payload, signature] = usage.token.split(".") as [string, string];
    const tamperedSignature = `${signature[0] === "A" ? "B" : "A"}${signature.slice(1)}`;

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(`${payload}.${tamperedSignature}`),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toEqual([]);
  });

  it("uses an expired proof only to replace its exact quarantined immutable snapshot", async () => {
    const { expireToken, repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const quarantined = await service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token, {
        rating: "needs_improvement",
        reasonCodes: ["other"],
        finalReply: "收货地址：幸福花园 3 栋 201 室",
      }),
    );
    expect(quarantined.record.privacyStatus).toBe("quarantined");
    expireToken();

    const replaced = await service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    );

    expect(replaced).toMatchObject({
      outcome: "updated",
      record: { id: quarantined.record.id, privacyStatus: "safe" },
    });
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toHaveLength(1);
  });

  it.each([
    ["decision card", { decisionCardId: "card-other" }],
    ["card digest", { cardDigest: `sha256_${"b".repeat(64)}` }],
  ] as const)("does not let an expired proof bypass a changed stored %s", async (
    _field,
    mismatch,
  ) => {
    const { expireToken, repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    await repository.saveReplyFeedback({
      tenantId: employeeA.tenantId,
      employeeUserId: employeeA.accountUserId,
      customerId: "customer-a",
      decisionCardId: safeCard.cardId,
      cardInstanceId: usage.cardInstanceId,
      cardDigest: usage.cardDigest,
      agentVersion: "reply-agent.v2",
      rating: "needs_improvement",
      reasonCodes: ["other"],
      scenario: safeCard.scenario,
      salesStage: safeCard.salesStage,
      evidenceIds: safeCard.audit.evidenceIds,
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_address"],
      ...mismatch,
    } satisfies CreateReplyFeedbackInput);
    expireToken();

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token),
    )).rejects.toMatchObject({ status: 409, code: "REPLY_FEEDBACK_PROOF_CONFLICT" });
    const records = await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" });
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({ ...mismatch, privacyStatus: "quarantined" });
  });

  it("does not let an expired proof request a replacement under a browser version", async () => {
    const { expireToken, repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    await service.saveReplyFeedback(employeeA, "customer-a", feedbackRequest(usage.token, {
      rating: "needs_improvement",
      reasonCodes: ["other"],
      finalReply: "收货地址：幸福花园 3 栋 201 室",
    }));
    expireToken();

    await expect(service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token, { agentVersion: "reply-agent.v3" }),
    )).rejects.toBeInstanceOf(z.ZodError);
    const records = await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" });
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({ agentVersion: "reply-agent.v2" });
  });

  it("quarantines unresolved private text without retaining any reply text", async () => {
    const { repository, service } = createHarness();
    const usage = await service.issueDecisionUsageToken(employeeA, "customer-a", safeCard);
    const privateReply = "收货地址：幸福花园 3 栋 201 室";

    const saved = await service.saveReplyFeedback(
      employeeA,
      "customer-a",
      feedbackRequest(usage.token, {
        rating: "needs_improvement",
        reasonCodes: ["other"],
        finalReply: privateReply,
      }),
    );

    expect(saved.record).toMatchObject({
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_address"],
    });
    expect(saved.record).not.toHaveProperty("sanitizedFinalReply");
    expect(JSON.stringify(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })))
      .not.toContain(privateReply);
  });
});
