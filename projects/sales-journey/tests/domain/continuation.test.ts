import { describe, expect, it } from "vitest";

import { createDecisionCard } from "@/domain/decision-card/advisor";
import {
  buildSafeContinuation,
  selectReviewFocus,
} from "@/domain/decision-card/continuation";
import { resolveOperatorStyle } from "@/domain/decision-card/style";
import {
  identityBeadRequest,
  scanServiceRequest,
} from "../support/decision-card-fixtures";

describe("高风险问题安全承接", () => {
  it("对受控处理追问生成不断话草稿并突出首要核验点", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      latestCustomerMessage: "之后你们还能收吗？",
    });

    expect(card.replyOptions[0].text).toContain("我先把您关心的这一点和现有资料整理好");
    expect(card.replyOptions[0].text).not.toMatch(/回购|收购|置换|固定价|保值/);
    expect(card.reviewFocus).toEqual({
      field: "service_policy",
      operatorPrompt: "请核对当前适用的服务规则。",
    });
  });

  it("保留全部核验项，同时把客户当前问题作为首要核验点", () => {
    const card = createDecisionCard({
      ...scanServiceRequest,
      latestCustomerMessage: "可以包退吗？",
    });

    expect(card.verificationNeeded).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field: "service_policy" }),
      ]),
    );
    expect(card.reviewFocus).toEqual({
      field: "service_policy",
      operatorPrompt: "请核对当前适用的服务规则。",
    });
    expect(card.deliveryMode).toBe("copy_only");
  });

  it("只在需要人工复核时给出首要核验点", () => {
    const card = createDecisionCard(identityBeadRequest);

    expect(card.safety.status).toBe("safe");
    expect(card.reviewFocus).toBeUndefined();
  });

  it("不把商品描述中的品类归为客户自述", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      productDescription: "沉香身份珠",
      latestCustomerMessage: "之后你们还能收吗？",
      conversation: [{ speaker: "operator", text: "您好，我先帮您整理。" }],
    });

    expect(card.replyOptions[0].text).not.toContain("就您提到的沉香");
  });

  it("保留客户在当前对话中实际提及的品类语境", () => {
    const card = createDecisionCard({
      ...identityBeadRequest,
      latestCustomerMessage: "之后你们还能收吗？",
      conversation: [{ speaker: "customer", text: "这串沉香之后怎么处理？" }],
    });

    expect(card.replyOptions[0].text).toContain("就您提到的沉香");
  });

  it("按已解析风格生成承接草稿并选择首个核验项", () => {
    const verification = [
      { field: "certificate" as const, reason: "客户正在询问真伪或证书，需要人工核验。" },
      { field: "service_policy" as const, reason: "客户正在询问服务规则，需要人工核验。" },
    ];

    expect(
      buildSafeContinuation(
        identityBeadRequest,
        verification,
        resolveOperatorStyle({
          conversation: [{ speaker: "operator", text: "您好，我先帮您整理。" }],
        }),
      ),
    ).toBe("您好，我先把您关心的这一点和现有资料整理好，核对后给您明确答复；您可以先把相关图片或页面截图发我。");
    expect(selectReviewFocus(verification)).toEqual({
      field: "certificate",
      operatorPrompt: "请核对真伪或证书相关的可引用资料。",
    });
  });
});
