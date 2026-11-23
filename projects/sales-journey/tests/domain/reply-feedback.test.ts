import { describe, expect, it } from "vitest";
import {
  redactFeedbackText,
  sanitizeFeedbackFinalReply,
} from "@/domain/reply-feedback/redaction";
import {
  ReplyFeedbackRequestSchema,
  StoredReplyFeedbackSchema,
} from "@/domain/reply-feedback/schema";

describe("reply feedback", () => {
  it("requires a reason when feedback needs improvement", () => {
    expect(() => ReplyFeedbackRequestSchema.parse({
      tenantId: "tenant-a",
      operatorId: "operator-1",
      cardId: "card-1",
      agentVersion: "reply-agent.v1",
      rating: "needs_improvement",
      reasons: [],
      scenario: "identity_bead",
      salesStage: "evaluate",
      evidenceIds: [],
    })).toThrow();
  });

  it("rejects duplicate improvement reasons", () => {
    expect(() => ReplyFeedbackRequestSchema.parse({
      tenantId: "tenant-a",
      operatorId: "operator-1",
      cardId: "card-1",
      agentVersion: "reply-agent.v1",
      rating: "needs_improvement",
      reasons: ["too_long", "too_long"],
      scenario: "identity_bead",
      salesStage: "evaluate",
      evidenceIds: [],
    })).toThrow();
  });

  it("rejects client-controlled privacy decisions", () => {
    expect(ReplyFeedbackRequestSchema.safeParse({
      tenantId: "tenant-a",
      operatorId: "operator-1",
      cardId: "card-1",
      agentVersion: "reply-agent.v1",
      rating: "helpful",
      reasons: [],
      scenario: "identity_bead",
      salesStage: "evaluate",
      evidenceIds: [],
      privacyStatus: "safe",
      privacyReasonCodes: [],
    }).success).toBe(false);
  });

  it("rejects final reply text on a quarantined stored record", () => {
    expect(StoredReplyFeedbackSchema.safeParse({
      tenantId: "tenant-a",
      operatorId: "operator-1",
      cardId: "card-1",
      agentVersion: "reply-agent.v1",
      rating: "helpful",
      reasons: [],
      scenario: "identity_bead",
      salesStage: "evaluate",
      evidenceIds: [],
      feedbackId: "feedback-1",
      createdAt: "2026-08-30T00:00:00.000Z",
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_address"],
      finalReply: "不应落库的文本",
    }).success).toBe(false);
  });

  it("removes customer identity data from the final reply", () => {
    expect(redactFeedbackText("王女士 13800138000，邮箱 a@example.com，卡号 6222021234567890"))
      .toBe("[姓名] [手机号]，邮箱 [邮箱]，卡号 [敏感数字]");
  });

  it("redacts addresses and payment-card numbers with separators", () => {
    expect(redactFeedbackText("地址：上海市静安区南京西路 88 号；卡 6222 0212 3456 7890；备卡 6222-0212-3456-7890"))
      .toBe("地址：[地址]；卡 [敏感数字]；备卡 [敏感数字]");
  });

  it("keeps ordinary short numbers and prices unchanged", () => {
    expect(redactFeedbackText("价格 88 元，订单尾号 1234，优惠 1,299 元"))
      .toBe("价格 88 元，订单尾号 1234，优惠 1,299 元");
  });

  it("redacts common Chinese street-address forms", () => {
    expect(redactFeedbackText("北京市东城区东四北大街 12 号；广东省广州市天河区天河大道 66 号；浙江省杭州市西湖区灵隐道 9 号；北京市东城区南锣鼓巷胡同 5 号"))
      .toBe("[地址]；[地址]；[地址]；[地址]");
  });

  it("does not redact ordinary product wording that mentions a road form", () => {
    expect(redactFeedbackText("大街系列手链，采用大道纹理设计，街巷主题限定款"))
      .toBe("大街系列手链，采用大道纹理设计，街巷主题限定款");
  });

  it("redacts plain Chinese names only in labeled identity contexts", () => {
    expect(redactFeedbackText("客户姓名：王小明；收件人 李娜；客户叫欧阳娜娜"))
      .toBe("客户姓名：[姓名]；收件人 [姓名]；客户叫[姓名]");
    expect(redactFeedbackText("王者系列手链，客户喜欢南红，型号李娜同款"))
      .toBe("王者系列手链，客户喜欢南红，型号李娜同款");
  });

  it("replaces contact-bearing semicolon clauses before redacting other fields", () => {
    expect(redactFeedbackText(
      "地址：朝阳区建国路 88 号；手机 138 0013 8000；卡号 6222 0212 3456 7890 123；微信号：jade_sale88",
    )).toBe(
      "地址：[地址]；[联系信息]；卡号 [敏感数字]；[联系信息]",
    );
  });

  it("keeps product codes and ordinary prices that merely resemble labels", () => {
    expect(redactFeedbackText("型号 jade_sale88，价格 19,999 元，朝阳系列建国路纹理款"))
      .toBe("型号 jade_sale88，价格 19,999 元，朝阳系列建国路纹理款");
  });

  it("replaces a clause containing an unlisted surname and WeChat marker", () => {
    expect(redactFeedbackText("请联系严小明，微信号 jade_sale88"))
      .toBe("[联系信息]");
  });

  it("replaces a clause containing natural contact wording", () => {
    expect(redactFeedbackText("麻烦联系王小明，微信号 jade_sale88"))
      .toBe("[联系信息]");
  });

  it("replaces a mineral product clause that contains a WeChat marker", () => {
    expect(redactFeedbackText("石榴石 微信 jade-sale88"))
      .toBe("[联系信息]");
  });

  it("replaces a jewelry product clause that contains a WeChat marker", () => {
    expect(redactFeedbackText("金丝玉 微信 jade-sale88"))
      .toBe("[联系信息]");
  });

  it("replaces a clause containing a WeChat token longer than sixty-five characters", () => {
    expect(redactFeedbackText("微信号 jade_sale88_customer_20260830_abcdefghijklmnopqrstuvwxyz_1234567890"))
      .toBe("[联系信息]");
  });

  it("replaces a clause containing a spaced phone marker", () => {
    expect(redactFeedbackText("请联系王小明，手机 138 0013 8000"))
      .toBe("[联系信息]");
  });

  it("replaces only the contact-bearing sentence while preserving surrounding product sentences", () => {
    expect(redactFeedbackText("绿松石手串采用手工打磨。请联系严小明，微信号 jade_sale88；翡翠手镯采用素面设计。"))
      .toBe("绿松石手串采用手工打磨。[联系信息]；翡翠手镯采用素面设计。");
  });

  it("keeps ordinary price and product text unchanged outside a contact context", () => {
    expect(redactFeedbackText("绿松石手串价格 1,299 元，型号 jade_sale88"))
      .toBe("绿松石手串价格 1,299 元，型号 jade_sale88");
  });

  it("intentionally replaces product wording in a clause with a contact marker", () => {
    expect(redactFeedbackText("王者系列手链，型号 jade_sale88，微信青瓷工艺"))
      .toBe("[联系信息]");
  });

  it("replaces a tight wx identifier as contact information", () => {
    expect(redactFeedbackText("新品信息。wx123。保养建议。"))
      .toBe("新品信息。[联系信息]。保养建议。");
  });

  it("uses ASCII periods as contact-segment boundaries in both directions", () => {
    expect(redactFeedbackText("请联系销售，wx123.这款纹理细腻。"))
      .toBe("[联系信息].这款纹理细腻。");
    expect(redactFeedbackText("这款纹理细腻.请联系销售，wx123。"))
      .toBe("这款纹理细腻.[联系信息]。");
  });

  it("keeps semantic uses of 联系 that are not contact instructions", () => {
    expect(redactFeedbackText("纹理之间有联系。光泽会随角度变化。"))
      .toBe("纹理之间有联系。光泽会随角度变化。");
  });

  it.each([
    ["邮箱：[邮箱]", "residual_email"],
    ["邮箱：[邮箱] alice", "residual_email"],
    ["地址：[地址] 幸福花园3栋201", "residual_address"],
    ["姓名：[姓名] Alice", "residual_name"],
    ["卡号：[敏感数字] jade88", "residual_payment_card"],
  ] as const)("does not trust a client-supplied privacy placeholder: %s", (finalReply, reasonCode) => {
    expect(sanitizeFeedbackFinalReply(finalReply)).toEqual({
      privacyStatus: "quarantined",
      privacyReasonCodes: [reasonCode],
    });
  });

  it.each([
    ["邮箱：alice@example.com alice", "residual_email"],
    ["地址：北京市朝阳区建国路88号 幸福花园3栋201", "residual_address"],
    ["姓名：王小明 Alice", "residual_name"],
    ["卡号：6222021234567890 jade88", "residual_payment_card"],
  ] as const)("quarantines extra text after a server-generated placeholder: %s", (finalReply, reasonCode) => {
    expect(sanitizeFeedbackFinalReply(finalReply)).toEqual({
      privacyStatus: "quarantined",
      privacyReasonCodes: [reasonCode],
    });
  });

  it.each([
    ["张三\u200b先生", "residual_name"],
    ["李\u200b女士", "residual_name"],
  ] as const)("detects a title separated by a zero-width character: %s", (finalReply, reasonCode) => {
    expect(sanitizeFeedbackFinalReply(finalReply)).toEqual({
      privacyStatus: "quarantined",
      privacyReasonCodes: [reasonCode],
    });
  });

  it.each([
    ["赵一\u2062先生", "residual_name"],
    ["钱二\u200e女士", "residual_name"],
    ["孙三\u2063先生", "residual_name"],
    ["周四\u00ad女士", "residual_name"],
    ["吴五\u034f先生", "residual_name"],
    ["邮\u2062箱：mailbox2062", "residual_email"],
    ["邮\u034f箱：mailbox034f", "residual_email"],
    ["型号：[邮\u2062箱] hidden2062", "residual_email"],
    ["型号：[邮\u034f箱] hidden034f", "residual_email"],
  ] as const)("removes every default-ignorable privacy separator before checks: %s", (finalReply, reasonCode) => {
    expect(sanitizeFeedbackFinalReply(finalReply)).toEqual({
      privacyStatus: "quarantined",
      privacyReasonCodes: [reasonCode],
    });
  });
});
