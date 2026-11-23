import { describe, expect, it } from "vitest";

import {
  EmployeeReplyFeedbackRequestSchema,
  ReplyFeedbackRecordSchema,
} from "@/domain/reply-feedback/schema";
import type {
  CreateReplyFeedbackInput,
  OwnerReplyQualityQuery,
  OwnerReplyQualityView,
  ReplyFeedbackSaveResult,
} from "@/domain/supervision/contracts";

const validBrowserRequest = {
  usageToken: "usage-token-001",
  agentVersion: "reply-agent.v2",
  rating: "needs_improvement",
  reasonCodes: ["too_long"],
  finalReply: "这款绿松石的纹理很有层次，我把已核实的细节整理给您。",
} as const;

const feedbackMetadata = {
  id: "feedback-001",
  tenantId: "tenant-001",
  employeeUserId: "employee-001",
  customerId: "customer-001",
  decisionCardId: "card-001",
  cardInstanceId: "card-instance-001",
  cardDigest: "sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  agentVersion: "reply-agent.v2",
  rating: "needs_improvement",
  reasonCodes: ["too_long"],
  scenario: "product_selection",
  salesStage: "evaluate",
  evidenceIds: ["evidence-001"],
  createdAt: "2026-08-30T00:00:00.000Z",
  updatedAt: "2026-08-30T00:00:00.000Z",
} as const;

describe("supervision reply feedback contracts", () => {
  it("does not accept browser-owned tenant or employee identity", () => {
    expect(EmployeeReplyFeedbackRequestSchema.safeParse({
      ...validBrowserRequest,
      tenantId: "forged-tenant",
      employeeUserId: "forged-user",
      customerId: "forged-customer",
      decisionCardId: "forged-card",
      cardInstanceId: "forged-card-instance",
      cardDigest: "sha256_forged",
      scenario: "identity_bead",
      salesStage: "discover",
      evidenceIds: ["forged-evidence"],
    }).success).toBe(false);
  });

  it("requires the server-derived tenant, employee, customer, card, and evidence context on records", () => {
    expect(ReplyFeedbackRecordSchema.safeParse({
      ...feedbackMetadata,
      privacyStatus: "safe",
      privacyReasonCodes: [],
      sanitizedFinalReply: "这款绿松石的纹理很有层次。",
    }).success).toBe(true);

    const { tenantId: _tenantId, ...recordWithoutTenant } = feedbackMetadata;
    expect(ReplyFeedbackRecordSchema.safeParse({
      ...recordWithoutTenant,
      privacyStatus: "safe",
      privacyReasonCodes: [],
    }).success).toBe(false);
  });

  it("forbids text on quarantined feedback", () => {
    expect(ReplyFeedbackRecordSchema.safeParse({
      ...feedbackMetadata,
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_phone"],
      sanitizedFinalReply: "[手机号]",
    }).success).toBe(false);
  });

  it("makes quarantined feedback text-free in each durable and owner-facing contract", () => {
    const quarantinedInput = {
      tenantId: "tenant-001",
      employeeUserId: "employee-001",
      customerId: "customer-001",
      decisionCardId: "card-001",
      cardInstanceId: "card-instance-001",
      cardDigest: "sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      agentVersion: "reply-agent.v2",
      rating: "needs_improvement",
      reasonCodes: ["too_long"],
      scenario: "product_selection",
      salesStage: "evaluate",
      evidenceIds: ["evidence-001"],
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_phone"],
    } satisfies CreateReplyFeedbackInput;

    const saveResult = {
      outcome: "created",
      record: {
        ...feedbackMetadata,
        privacyStatus: "quarantined",
        privacyReasonCodes: ["residual_phone"],
      },
    } satisfies ReplyFeedbackSaveResult;

    const ownerQuery = {
      tenantId: "tenant-001",
      employeeUserId: "employee-001",
      scenario: "product_selection",
      salesStage: "evaluate",
      agentVersion: "reply-agent.v2",
    } satisfies OwnerReplyQualityQuery;

    const ownerView = {
      totals: { helpful: 0, needsImprovement: 1, quarantined: 1 },
      reasons: [{ code: "too_long", count: 1 }],
      samples: [{
        feedbackRef: "feedback-001",
        employeeRef: "employee-001",
        customerRef: "C-001",
        scenario: "product_selection",
        salesStage: "evaluate",
        agentVersion: "reply-agent.v2",
        rating: "needs_improvement",
        privacyStatus: "quarantined",
        privacyReasonCodes: ["residual_phone"],
      }],
    } satisfies OwnerReplyQualityView;

    expect(quarantinedInput.privacyStatus).toBe("quarantined");
    expect(saveResult.record.privacyStatus).toBe("quarantined");
    expect(ownerQuery.tenantId).toBe("tenant-001");
    expect(ownerView.samples[0]).not.toHaveProperty("sanitizedFinalReply");
  });
});
