import { describe, expect, expectTypeOf, it } from "vitest";

import {
  EvidenceCardSchema,
  EvidenceDecisionSchema,
  HumanFactDeclarationSchema,
  RejectedEvidenceSuggestionSchema,
} from "../src";

const digest = "a".repeat(64);

const card = {
  id: "evidence-card-1",
  customerId: "customer-a",
  creativeSessionId: "session-a",
  runId: "analysis-run-1",
  sourceId: "asset-a",
  sourceDigest: digest,
  sourceStartMs: 1_000,
  sourceEndMs: 2_000,
  kind: "transcript-quote" as const,
  origin: "new" as const,
  value: "我想让首饰保留手工的温度",
  modelReceiptId: "receipt-a",
  confidence: 0.91,
  reviewStatus: "pending" as const,
  reviewedBy: null,
  reviewedAt: null,
  correctionReason: null,
};

const declaration = {
  id: "declaration-1",
  customerId: "customer-a",
  creativeSessionId: "session-a",
  kind: "opinion" as const,
  text: "我更在意佩戴者和首饰之间的关系。",
  declaredBy: "customer-a",
  confirmedBy: "customer-a",
  confirmedAt: "2026-09-23T01:00:00.000Z",
  allowedUses: ["persona-story", "on-screen-text"] as const,
  voiceClaimed: false as const,
  status: "active" as const,
};

describe("persona editorial contracts", () => {
  it("requires every evidence card to remain bound to a concrete source interval", () => {
    expect(() => EvidenceCardSchema.parse({
      ...card,
      sourceStartMs: null,
      sourceEndMs: null,
    })).toThrow("evidence-time-range-required");
    expect(() => EvidenceCardSchema.parse({
      ...card,
      sourceStartMs: 2_000,
      sourceEndMs: 2_000,
    })).toThrow("evidence-time-range-required");
  });

  it("keeps pending and reviewed card metadata internally consistent", () => {
    expect(EvidenceCardSchema.parse(card)).toEqual(card);
    expect(() => EvidenceCardSchema.parse({
      ...card,
      reviewStatus: "accepted",
    })).toThrow("reviewed-evidence-requires-review-metadata");
    expect(() => EvidenceCardSchema.parse({
      ...card,
      reviewedBy: "designer-a",
      reviewedAt: "2026-09-23T01:02:00.000Z",
    })).toThrow("pending-evidence-cannot-have-review-metadata");
    expect(() => EvidenceCardSchema.parse({
      ...card,
      reviewStatus: "corrected",
      reviewedBy: "designer-a",
      reviewedAt: "2026-09-23T01:02:00.000Z",
      correctionReason: null,
    })).toThrow("corrected-evidence-requires-reason");
  });

  it("allows only a signed written opinion and never a voice claim", () => {
    const parsed = HumanFactDeclarationSchema.parse(declaration);
    expect(parsed).toEqual(declaration);
    expectTypeOf(parsed.voiceClaimed).toEqualTypeOf<false>();
    expect(() => HumanFactDeclarationSchema.parse({
      ...declaration,
      voiceClaimed: true,
    })).toThrow("human-declaration-cannot-claim-voice");
    expect(() => HumanFactDeclarationSchema.parse({
      ...declaration,
      allowedUses: ["persona-story"],
    })).toThrow();
  });

  it("binds an evidence decision to one card, source digest, receipt and operation", () => {
    const decision = {
      id: "decision-1",
      cardId: card.id,
      customerId: card.customerId,
      creativeSessionId: card.creativeSessionId,
      sourceDigest: card.sourceDigest,
      modelReceiptId: card.modelReceiptId,
      operationKey: "review-card-1",
      action: "corrected" as const,
      correctedValue: "我想让这件首饰保留手工的温度",
      correctionReason: "补足指代对象",
      reviewerId: "designer-a",
      reviewedAt: "2026-09-23T01:02:00.000Z",
    };
    expect(EvidenceDecisionSchema.parse(decision)).toEqual(decision);
    expect(() => EvidenceDecisionSchema.parse({
      ...decision,
      action: "accepted",
    })).toThrow("accepted-evidence-cannot-change-value");
    expect(() => EvidenceDecisionSchema.parse({
      ...decision,
      action: "rejected",
      correctedValue: null,
      correctionReason: null,
    })).toThrow("rejected-evidence-requires-reason");
  });

  it("stores unsupported model claims as terminal rejected suggestions", () => {
    const suggestion = {
      id: "rejected-suggestion-1",
      customerId: "customer-a",
      creativeSessionId: "session-a",
      runId: "analysis-run-1",
      sourceId: "asset-a",
      sourceDigest: digest,
      sourceStartMs: 1_000,
      sourceEndMs: 2_000,
      modelReceiptId: "receipt-a",
      value: "她看起来很后悔",
      rejectionCode: "unsupported-emotion-claim" as const,
      status: "rejected" as const,
      createdAt: "2026-09-23T01:00:00.000Z",
    };
    expect(RejectedEvidenceSuggestionSchema.parse(suggestion)).toEqual(suggestion);
    expect(() => RejectedEvidenceSuggestionSchema.parse({
      ...suggestion,
      status: "pending",
    })).toThrow();
  });
});
