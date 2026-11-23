import { describe, expect, it } from "vitest";

import { evaluateSafety } from "@/domain/decision-card/policy";
import type { DecisionCard, DecisionCardRequest } from "@/domain/decision-card/types";
import {
  cardWithReply,
  controlledReuseOrReturnRequest,
  identityBeadRequest,
  safeCard,
} from "../support/decision-card-fixtures";

function cardFor(
  request: DecisionCardRequest,
  text: string,
  evidenceIds: string[] = [],
): DecisionCard {
  return {
    ...cardWithReply(text),
    requestId: request.requestId,
    scenario: request.scenario,
    replyOptions: [{ ...cardWithReply(text).replyOptions[0], evidenceIds }],
    audit: { ...safeCard.audit, evidenceIds },
  };
}

describe("evaluateSafety", () => {
  it("requires review for a fixed reuse-or-return price", () => {
    const result = evaluateSafety(
      controlledReuseOrReturnRequest,
      cardFor(controlledReuseOrReturnRequest, "我们可以按固定 3000 元处理"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("REUSE_OR_RETURN_NO_FIXED_PRICE");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "reuse_or_return_terms" }),
    );
  });

  it("requires review for every reuse-or-return card even when its text has no price", () => {
    const result = evaluateSafety(
      controlledReuseOrReturnRequest,
      cardFor(
        controlledReuseOrReturnRequest,
        "请先送回仓库核验，人工确认后再和您确定下一步。",
      ),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toEqual(
      expect.arrayContaining([
        "REUSE_OR_RETURN_NO_FIXED_PRICE",
        "REUSE_OR_RETURN_HUMAN_REVIEW",
      ]),
    );
    expect(result.nextAction).toEqual(
      expect.objectContaining({ kind: "handoff_to_operator" }),
    );
  });

  it("requires approved origin evidence before asserting origin", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "这颗来自保山"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("EVIDENCE_REQUIRED_FOR_FACT");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "origin" }),
    );
  });

  it("strips only quoted customer category context before fact scanning", () => {
    const contextOnly = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "就您提到的沉香，我先整理您关心的核对资料。"),
    );
    const assistantAssertion = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "就您提到的沉香，这件就是沉香。"),
    );

    expect(contextOnly.status).toBe("safe");
    expect(assistantAssertion.status).toBe("reviewRequired");
    expect(assistantAssertion.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "material" }),
    );
  });

  it("treats a quoted customer category as context rather than a material assertion", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "就您提到的沉香，我先整理您关心的核对资料。"),
    );

    expect(result.status).toBe("safe");
    expect(result.rules).not.toContain("EVIDENCE_REQUIRED_FOR_FACT");
  });

  it("routes a customer's origin and quality question to human review", () => {
    const request = {
      ...identityBeadRequest,
      productDescription: "客户说这次卖的是彩宝",
      latestCustomerMessage: "这颗彩宝产自哪里，品质怎么样？",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "就您提到的彩宝，我先整理需要人工核验的资料。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field: "origin" }),
        expect.objectContaining({ field: "quality" }),
      ]),
    );
  });

  it("routes a customer's authenticity, certificate, and material question to human review", () => {
    const request = {
      ...identityBeadRequest,
      productDescription: "客户说这次卖的是翡翠",
      latestCustomerMessage: "这件能保真吗？有证书吗？材质怎么核对？",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "就您提到的翡翠，我先整理需要人工核验的资料。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field: "certificate" }),
        expect.objectContaining({ field: "material" }),
      ]),
    );
  });

  it("routes a re-setting guarantee request to human feasibility review", () => {
    const request: DecisionCardRequest = {
      ...identityBeadRequest,
      scenario: "resetting",
      latestCustomerMessage: "重新镶嵌后能保证效果吗？",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "需要工艺人员实物检查后确认。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("CUSTOMIZATION_FEASIBILITY_VERIFICATION");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "customization_feasibility" }),
    );
  });

  it("uses the selected re-setting scenario to review a short guarantee request", () => {
    const request: DecisionCardRequest = {
      ...identityBeadRequest,
      scenario: "resetting",
      latestCustomerMessage: "能保证效果吗？",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "需要工艺人员实物检查后确认。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("CUSTOMIZATION_FEASIBILITY_VERIFICATION");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "customization_feasibility" }),
    );
  });

  it("routes relevant customer questions from recent conversation to human review", () => {
    const request = {
      ...identityBeadRequest,
      latestCustomerMessage: "那下一步怎么办？",
      conversation: [
        { speaker: "customer" as const, text: "这件是什么产地，品质等级怎么核对？" },
        { speaker: "operator" as const, text: "我先帮您整理。" },
      ],
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "我先整理您关心的核对资料。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toEqual(expect.arrayContaining([
      expect.objectContaining({ field: "origin" }),
      expect.objectContaining({ field: "quality" }),
    ]));
  });

  it("does not treat an operator-only factual question as the customer's question", () => {
    const request = {
      ...identityBeadRequest,
      latestCustomerMessage: "我想先了解核对步骤。",
      conversation: [
        { speaker: "operator" as const, text: "您要问产地和品质等级吗？" },
      ],
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "我先整理您关心的核对步骤。"),
    );

    expect(result.status).toBe("safe");
  });

  it("keeps a neutral customer request with quoted category context safe", () => {
    const request = {
      ...identityBeadRequest,
      productDescription: "客户说这次卖的是彩宝",
      latestCustomerMessage: "就您提到的彩宝，我想先了解身份珠怎么核对。",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "就您提到的彩宝，我先整理您关心的核对资料。"),
    );

    expect(result.status).toBe("safe");
    expect(result.verificationNeeded).toEqual([]);
  });

  it("keeps a neutral customer request without factual claims safe", () => {
    const request = {
      ...identityBeadRequest,
      latestCustomerMessage: "我想先了解身份珠的核对步骤。",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "我先整理您关心的核对步骤。"),
    );

    expect(result.status).toBe("safe");
    expect(result.verificationNeeded).toEqual([]);
  });

  it("still requires review when the assistant itself asserts a category", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "这件就是沉香。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "material" }),
    );
  });

  it("routes an unsupported 产自产地说法 to human review", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "这颗产自阿富汗"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "origin" }),
    );
  });

  it("routes unverified service and platform claims to human review", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "支持七天无理由退货，并且与京东合作"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "service_policy" }),
    );
  });

  it("accepts an origin claim only when an approved referenced record verifies origin", () => {
    const request: DecisionCardRequest = {
      ...identityBeadRequest,
      evidence: [
        ...identityBeadRequest.evidence,
        {
          id: "evidence-origin-1",
          kind: "product_record",
          title: "已批准的产地资料",
          verifiedFields: ["origin"],
          status: "approved",
        },
      ],
    };

    const result = evaluateSafety(
      request,
      cardFor(request, "这颗来自保山", ["evidence-origin-1"]),
    );

    expect(result.status).toBe("safe");
    expect(result.rules).not.toContain("EVIDENCE_REQUIRED_FOR_FACT");
  });

  it("does not accept an origin record that was not cited by the reply", () => {
    const request: DecisionCardRequest = {
      ...identityBeadRequest,
      evidence: [
        ...identityBeadRequest.evidence,
        {
          id: "evidence-origin-1",
          kind: "product_record",
          title: "已批准的产地资料",
          verifiedFields: ["origin"],
          status: "approved",
        },
      ],
    };

    const result = evaluateSafety(request, cardFor(request, "这颗来自保山"));

    expect(result.status).toBe("reviewRequired");
    expect(result.verificationNeeded).toContainEqual(
      expect.objectContaining({ field: "origin" }),
    );
  });

  it("requires manual copy delivery", () => {
    const result = evaluateSafety(identityBeadRequest, {
      ...safeCard,
      deliveryMode: "send_now" as never,
    });

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("COPY_ONLY_DELIVERY");
  });

  it("routes authenticity claims to a human review", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "这颗一定保真"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("EVIDENCE_REQUIRED_FOR_FACT");
    expect(result.nextAction).toEqual(
      expect.objectContaining({ kind: "handoff_to_operator" }),
    );
  });

  it("routes a quote request to a human review", () => {
    const request = {
      ...identityBeadRequest,
      latestCustomerMessage: "这颗多少钱？",
    };
    const result = evaluateSafety(
      request,
      cardFor(request, "价格需要结合具体规格由人工确认。"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("EVIDENCE_REQUIRED_FOR_FACT");
  });

  it("routes value-preservation claims to a human review", () => {
    const result = evaluateSafety(
      identityBeadRequest,
      cardFor(identityBeadRequest, "这颗很保值，以后一定升值"),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("EVIDENCE_REQUIRED_FOR_FACT");
  });

  it("blocks reuse-or-return promises of repurchase, swap, platform matching, and immediate payment", () => {
    const result = evaluateSafety(
      controlledReuseOrReturnRequest,
      cardFor(
        controlledReuseOrReturnRequest,
        "我们马上回购，也可以置换，并且平台会帮您匹配，当天到账。",
      ),
    );

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain("REUSE_OR_RETURN_NO_FIXED_PRICE");
    expect(result.reasons.join(" ")).toMatch(/回购|置换|平台|到账/);
  });
});
