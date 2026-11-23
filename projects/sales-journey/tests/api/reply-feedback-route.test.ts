import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { POST as GENERATE_CARD } from "@/app/api/decision-cards/route";
import { GET } from "@/app/api/reply-feedback/review/route";
import { POST } from "@/app/api/reply-feedback/route";
import type { DecisionCard } from "@/domain/decision-card/types";
import type { ReplyFeedbackRequest } from "@/domain/reply-feedback/schema";
import {
  GENERATED_CARD_TTL_MS,
  generatedCardRegistry,
} from "@/server/decision-card/registry";
import { replyFeedbackRepository } from "@/server/reply-feedback/repository";
import type { CustomerRecord } from "@/domain/supervision/contracts";
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

const tenantId = "demo-tenant";
const operatorId = "demo-operator";
const reviewToken = "test-only-review-token";
const now = "2026-08-30T08:00:00.000Z";
const customer: CustomerRecord = {
  id: "customer-demo-1",
  tenantId,
  employeeUserId: operatorId,
  customerNumber: "C-DEMO-1",
  customerAlias: "演示客户",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T08:00:00.000Z",
  status: "pending",
  version: 1,
  createdAt: now,
};

class DemoMembershipD1 implements D1DatabaseLike {
  prepare(): D1PreparedStatementLike {
    const statement: D1PreparedStatementLike = {
      bind: () => statement,
      first: async () => null,
      all: async <T>() => ({ results: [{
        id: operatorId,
        tenant_id: tenantId,
        email: "demo-operator@shop.test",
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
    d1: new DemoMembershipD1(),
    repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
    vault,
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => now,
  }));
});

async function generateCard(requestId: string): Promise<DecisionCard> {
  const response = await GENERATE_CARD(new Request("http://localhost/api/decision-cards", {
    method: "POST",
    headers: {
      "oai-authenticated-user-id": operatorId,
      "oai-authenticated-user-email": "demo-operator@shop.test",
    },
    body: JSON.stringify({
      ...identityBeadRequest,
      requestId,
      tenantId,
      operatorId,
      customerId: customer.id,
      evidence: [],
    }),
  }));
  expect(response.status).toBe(200);
  return (await response.json()).card as DecisionCard;
}

function feedbackFor(
  card: DecisionCard,
  overrides: Partial<ReplyFeedbackRequest> = {},
): ReplyFeedbackRequest {
  return {
    tenantId,
    operatorId,
    cardId: card.cardId,
    agentVersion: "reply-agent.v2",
    rating: "helpful",
    reasons: [],
    scenario: card.scenario,
    salesStage: card.salesStage,
    evidenceIds: card.audit.evidenceIds,
    ...overrides,
  };
}

function feedbackRequest(input: unknown, requestTenantId?: string): Request {
  return new Request("http://localhost/api/reply-feedback", {
    method: "POST",
    headers: requestTenantId === undefined ? {} : { "x-tenant-id": requestTenantId },
    body: JSON.stringify(input),
  });
}

function authorizedReviewRequest(url = "http://localhost/api/reply-feedback/review"): Request {
  vi.stubEnv("REPLY_FEEDBACK_REVIEW_TOKEN", reviewToken);
  return new Request(url, {
    headers: { authorization: `Bearer ${reviewToken}` },
  });
}

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

it("accepts feedback only after the server generated and registered its card", async () => {
  const card = await generateCard("api-feedback-accepted");
  const response = await POST(feedbackRequest(feedbackFor(card, {
    finalReply: "王女士 13800138000，稍后联系您。",
  }), "attacker-tenant"));

  expect(response.status).toBe(201);
  expect(await response.json()).toEqual({
    accepted: true,
    outcome: "created",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });
});

it("fails closed when the legacy feedback route is not in explicit synthetic-local mode", async () => {
  const card = await generateCard("api-feedback-runtime-disabled");
  vi.stubEnv("NODE_ENV", "production");

  const response = await POST(feedbackRequest(feedbackFor(card)));

  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({ error: "REPLY_FEEDBACK_RUNTIME_UNAVAILABLE" });
});

it("fails closed before reviewer access when the legacy review route is not synthetic-local", async () => {
  vi.stubEnv("NODE_ENV", "production");

  const response = await GET(authorizedReviewRequest());

  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({ error: "REPLY_FEEDBACK_RUNTIME_UNAVAILABLE" });
});

it("updates a prior evaluation and treats an exact retry idempotently", async () => {
  const card = await generateCard("api-feedback-idempotent");
  const helpful = feedbackFor(card);
  const improved = feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["too_long"],
  });

  const first = await POST(feedbackRequest(helpful));
  const update = await POST(feedbackRequest(improved));
  const retry = await POST(feedbackRequest(improved));

  expect(first.status).toBe(201);
  expect(await first.json()).toEqual({
    accepted: true,
    outcome: "created",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });
  expect(update.status).toBe(200);
  expect(await update.json()).toEqual({
    accepted: true,
    outcome: "updated",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });
  expect(retry.status).toBe(200);
  expect(await retry.json()).toEqual({
    accepted: true,
    outcome: "unchanged",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });
});

it("records high-risk feedback but reports that its final reply was quarantined", async () => {
  const card = await generateCard("api-feedback-quarantined");
  const privateReply = "收货地址：幸福花园 3 栋 201 室";
  const response = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: privateReply,
  })));

  expect(response.status).toBe(201);
  expect(await response.json()).toEqual({
    accepted: true,
    outcome: "created",
    privacyStatus: "quarantined",
    privacyReasonCodes: ["residual_address"],
  });

  const reviewResponse = await GET(authorizedReviewRequest());
  const reviewText = JSON.stringify(await reviewResponse.json());
  expect(reviewText).not.toContain(privateReply);
  expect(reviewText).not.toContain("幸福花园");
});

it.each([
  ["client-email-placeholder", "邮箱：[邮箱] alice", "residual_email", "alice"],
  ["client-address-placeholder", "地址：[地址] 幸福花园3栋201", "residual_address", "幸福花园"],
  ["client-name-placeholder", "姓名：[姓名] Alice", "residual_name", "Alice"],
  ["client-card-placeholder", "卡号：[敏感数字] jade88", "residual_payment_card", "jade88"],
  ["zero-width-mr", "张三\u200b先生", "residual_name", "张三"],
  ["zero-width-ms", "李\u200b女士", "residual_name", "李"],
  ["invisible-function-application", "赵一\u2062先生", "residual_name", "赵一"],
  ["invisible-left-to-right-mark", "钱二\u200e女士", "residual_name", "钱二"],
  ["invisible-separator", "孙三\u2063先生", "residual_name", "孙三"],
  ["invisible-soft-hyphen", "周四\u00ad女士", "residual_name", "周四"],
  ["invisible-grapheme-joiner", "吴五\u034f先生", "residual_name", "吴五"],
  ["invisible-email-label-2062", "邮\u2062箱：mailbox2062", "residual_email", "mailbox2062"],
  ["invisible-email-label-034f", "邮\u034f箱：mailbox034f", "residual_email", "mailbox034f"],
  ["invisible-placeholder-label-2062", "型号：[邮\u2062箱] hidden2062", "residual_email", "hidden2062"],
  ["invisible-placeholder-label-034f", "型号：[邮\u034f箱] hidden034f", "residual_email", "hidden034f"],
] as const)(
  "keeps placeholder and zero-width bypass text out of storage and review: %s",
  async (requestId, finalReply, reasonCode, privateFragment) => {
    const card = await generateCard(`api-feedback-${requestId}`);
    const response = await POST(feedbackRequest(feedbackFor(card, {
      rating: "needs_improvement",
      reasons: ["other"],
      finalReply,
    })));

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({
      accepted: true,
      outcome: "created",
      privacyStatus: "quarantined",
      privacyReasonCodes: [reasonCode],
    });

    const reviewResponse = await GET(authorizedReviewRequest());
    const reviewText = JSON.stringify(await reviewResponse.json());
    expect(reviewText).not.toContain(finalReply);
    expect(reviewText).not.toContain(privateFragment);
  },
);

it("upgrades a quarantined reply to a safe review example through the API", async () => {
  const card = await generateCard("api-feedback-safe-upgrade");
  const first = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "联系方式：jade_sale88",
  })));
  const correction = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "这款纹理细腻，我会再说明保养方式。",
  })));

  expect((await first.json()).privacyStatus).toBe("quarantined");
  expect(correction.status).toBe(200);
  expect(await correction.json()).toEqual({
    accepted: true,
    outcome: "updated",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });

  const review = await GET(authorizedReviewRequest());
  expect(JSON.stringify(await review.json())).toContain("这款纹理细腻，我会再说明保养方式。");
});

it("rejects feedback whose body tenant or operator differs from server identity", async () => {
  const card = await generateCard("api-feedback-identity");

  for (const input of [
    feedbackFor(card, { tenantId: "attacker-tenant" }),
    feedbackFor(card, { operatorId: "attacker-operator" }),
  ]) {
    const response = await POST(feedbackRequest(input));
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: "TENANT_SCOPE_MISMATCH" });
  }
});

it("returns gone for an unknown card without a matching quarantined correction", async () => {
  const card = await generateCard("api-feedback-forged-base");
  const response = await POST(feedbackRequest(feedbackFor({
    ...card,
    cardId: "card-never-generated",
  })));

  expect(response.status).toBe(410);
  expect(await response.json()).toEqual({ error: "EXPIRED_DECISION_CARD" });
});

it("never replaces a quarantined record when the card registry has no proof", async () => {
  const unknownFeedback: ReplyFeedbackRequest = {
    tenantId,
    operatorId,
    cardId: `card-unregistered-${crypto.randomUUID()}`,
    agentVersion: "reply-agent.v1",
    rating: "needs_improvement",
    reasons: ["other"],
    scenario: "identity_bead",
    salesStage: "discover",
    evidenceIds: [],
    finalReply: "请寄到北京市朝阳区望京SOHO T1",
  };
  const quarantined = await replyFeedbackRepository.saveWithOutcome(unknownFeedback);
  expect(quarantined.record.privacyStatus).toBe("quarantined");
  expect(generatedCardRegistry.matchFeedback(unknownFeedback)).toBe("unknown");

  const response = await POST(feedbackRequest({
    ...unknownFeedback,
    finalReply: "这款纹理细腻，我会再说明保养方式。",
  }));

  expect(response.status).toBe(410);
  expect(await response.json()).toEqual({ error: "EXPIRED_DECISION_CARD" });
  const stored = (await replyFeedbackRepository.listForTenant(tenantId))
    .find((record) => record.cardId === unknownFeedback.cardId);
  expect(stored?.privacyStatus).toBe("quarantined");
});

it("rejects feedback fields that do not match the registered card", async () => {
  const card = await generateCard("api-feedback-mismatch");
  const response = await POST(feedbackRequest(feedbackFor(card, {
    scenario: "scan_service",
  })));

  expect(response.status).toBe(409);
  expect(await response.json()).toEqual({ error: "DECISION_CARD_FEEDBACK_MISMATCH" });
});

it("rejects feedback for an expired generated card", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-30T00:00:00.000Z"));
  const card = await generateCard("api-feedback-expired");
  vi.advanceTimersByTime(GENERATED_CARD_TTL_MS + 1);

  const response = await POST(feedbackRequest(feedbackFor(card)));

  expect(response.status).toBe(410);
  expect(await response.json()).toEqual({ error: "EXPIRED_DECISION_CARD" });
});

it("allows a safe correction of matching quarantined feedback after card expiry", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-30T00:00:00.000Z"));
  const card = await generateCard("api-feedback-expired-correction");
  const quarantined = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "请寄到北京市朝阳区望京SOHO T1",
  })));
  expect((await quarantined.json()).privacyStatus).toBe("quarantined");
  vi.advanceTimersByTime(GENERATED_CARD_TTL_MS + 1);

  const correction = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "这款纹理细腻，我会再说明保养方式。",
  })));

  expect(correction.status).toBe(200);
  expect(await correction.json()).toEqual({
    accepted: true,
    outcome: "updated",
    privacyStatus: "safe",
    privacyReasonCodes: [],
  });
});

it("rejects an expired correction when immutable feedback fields do not match the registry snapshot", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-30T00:00:00.000Z"));
  const card = await generateCard("api-feedback-expired-mismatch");
  await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "请寄到北京市朝阳区望京SOHO T1",
  })));
  vi.advanceTimersByTime(GENERATED_CARD_TTL_MS + 1);

  const correction = await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["other"],
    finalReply: "这款纹理细腻，我会再说明保养方式。",
    evidenceIds: ["forged-evidence"],
  })));

  expect(correction.status).toBe(409);
  expect(await correction.json()).toEqual({ error: "DECISION_CARD_FEEDBACK_MISMATCH" });
});

it("rejects client-forged privacy decisions", async () => {
  const card = await generateCard("api-feedback-forged-privacy");
  const response = await POST(feedbackRequest({
    ...feedbackFor(card),
    privacyStatus: "safe",
    privacyReasonCodes: [],
  }));

  expect(response.status).toBe(400);
  expect((await response.json()).error).toBe("INVALID_REPLY_FEEDBACK");
});

it("requires a configured server-owned reviewer token", async () => {
  vi.stubEnv("REPLY_FEEDBACK_REVIEW_TOKEN", "");
  const response = await GET(new Request("http://localhost/api/reply-feedback/review", {
    headers: {
      authorization: "Bearer caller-controlled-value",
      "x-tenant-id": tenantId,
    },
  }));

  expect(response.status).toBe(403);
  expect(await response.json()).toEqual({ error: "REVIEWER_ACCESS_DENIED" });
});

it("rejects a mismatched reviewer token without returning the configured secret", async () => {
  vi.stubEnv("REPLY_FEEDBACK_REVIEW_TOKEN", reviewToken);
  const response = await GET(new Request("http://localhost/api/reply-feedback/review", {
    headers: {
      authorization: "Bearer wrong-value",
      "x-tenant-id": tenantId,
    },
  }));
  const body = await response.json();

  expect(response.status).toBe(403);
  expect(body).toEqual({ error: "REVIEWER_ACCESS_DENIED" });
  expect(JSON.stringify(body)).not.toContain(reviewToken);
});

it("returns only server-identity review data despite forged header and query values", async () => {
  const card = await generateCard("api-feedback-review");
  await POST(feedbackRequest(feedbackFor(card, {
    rating: "needs_improvement",
    reasons: ["too_long", "no_next_step"],
    finalReply: "王女士 13800138000，稍后联系您。",
  }), "attacker-tenant"));
  vi.stubEnv("REPLY_FEEDBACK_REVIEW_TOKEN", reviewToken);

  const response = await GET(
    new Request("http://localhost/api/reply-feedback/review?tenantId=attacker-tenant", {
      headers: {
        authorization: `Bearer ${reviewToken}`,
        "x-tenant-id": "attacker-tenant",
      },
    }),
  );

  expect(response.status).toBe(200);
  const body = await response.json();
  expect(body.groups).toEqual(expect.arrayContaining([
    expect.objectContaining({
      scenario: "identity_bead",
      salesStage: "discover",
      agentVersion: "reply-agent.v2",
      rating: "needs_improvement",
      reason: "too_long",
      finalReplyExamples: expect.arrayContaining(["[联系信息]。"]),
    }),
    expect.objectContaining({ reason: "no_next_step" }),
  ]));
  expect(JSON.stringify(body)).not.toContain(operatorId);
  expect(JSON.stringify(body)).not.toContain("王女士");
  expect(JSON.stringify(body)).not.toContain("13800138000");
  expect(JSON.stringify(body)).not.toContain("attacker-tenant");
});

it("reports malformed feedback as a validation error", async () => {
  const response = await POST(new Request("http://localhost/api/reply-feedback", {
    method: "POST",
    body: JSON.stringify({ tenantId, operatorId }),
  }));

  expect(response.status).toBe(400);
  expect((await response.json()).error).toBe("INVALID_REPLY_FEEDBACK");
});
