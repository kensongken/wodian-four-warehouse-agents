import { describe, expect, it } from "vitest";

import { evaluateSafety } from "@/domain/decision-card/policy";
import type { DecisionCard } from "@/domain/decision-card/types";
import { cardWithReply, identityBeadRequest, safeCard } from "../support/decision-card-fixtures";

function cardFor(text: string): DecisionCard {
  const reply = cardWithReply(text).replyOptions[0];

  return {
    ...cardWithReply(text),
    requestId: identityBeadRequest.requestId,
    scenario: identityBeadRequest.scenario,
    replyOptions: [{ ...reply, evidenceIds: [] }],
    audit: { ...safeCard.audit, evidenceIds: [] },
  };
}

describe("customer category context policy", () => {
  it("keeps exact quoted customer category context safe", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor("就您提到的沉香，我先整理您关心的核对资料。"),
    );

    expect(result.status).toBe("safe");
  });

  it("requires review for an assistant material assertion", () => {
    const result = evaluateSafety(identityBeadRequest, cardFor("这件就是沉香。"));

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "material" }),
    );
  });
});
