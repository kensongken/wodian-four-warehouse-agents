import { describe, expect, it } from "vitest";

import {
  DecisionCardRequestSchema,
  DecisionCardSchema,
} from "@/domain/decision-card/schema";
import { safeCard } from "../support/decision-card-fixtures";

describe("决策卡 v1 合同", () => {
  it("拒绝超过十二轮的对话上下文", () => {
    const result = DecisionCardRequestSchema.safeParse({
      contractVersion: "decision-card.v1",
      requestId: "req-1",
      tenantId: "tenant-a",
      operatorId: "operator-a",
      scenario: "identity_bead",
      productDescription: "身份珠",
      latestCustomerMessage: "这个是什么？",
      conversation: Array.from({ length: 13 }, () => ({
        speaker: "customer",
        text: "你好",
      })),
      evidence: [],
    });

    expect(result.success).toBe(false);
  });

  it("拒绝非人工复制的发送方式", () => {
    expect(() =>
      DecisionCardSchema.parse({ ...safeCard, deliveryMode: "send_now" }),
    ).toThrow();
  });

  it("接受风险卡中的一个优先核对点", () => {
    const card = DecisionCardSchema.parse({
      ...safeCard,
      verificationNeeded: [
        {
          field: "certificate",
          reason: "缺少可引用的证书资料，需要人工核对。",
        },
      ],
      reviewFocus: {
        field: "certificate",
        operatorPrompt: "请核对真伪或证书相关的可引用资料。",
      },
      safety: {
        ...safeCard.safety,
        status: "reviewRequired",
      },
    });

    expect(card.reviewFocus?.operatorPrompt).toBe("请核对真伪或证书相关的可引用资料。");
  });

  it("接受没有优先核对点的安全卡", () => {
    expect(DecisionCardSchema.parse(safeCard).reviewFocus).toBeUndefined();
  });

  it("接受严格的仅预览商品策略封条", () => {
    expect(DecisionCardSchema.parse({
      ...safeCard,
      productStrategySeal: {
        productCode: "SYNTHETIC-001",
        strategyInputVersion: 2,
        contentSha256: "a".repeat(64),
        status: "preview_only",
      },
    }).productStrategySeal).toMatchObject({ strategyInputVersion: 2 });
  });

  it.each([
    { strategyInputVersion: 0 },
    { contentSha256: "not-a-sha" },
    { status: "sales_ready" },
    { extra: true },
  ])("拒绝无效的商品策略封条 %#", (override) => {
    expect(() => DecisionCardSchema.parse({
      ...safeCard,
      productStrategySeal: {
        productCode: "SYNTHETIC-001",
        strategyInputVersion: 1,
        contentSha256: "a".repeat(64),
        status: "preview_only",
        ...override,
      },
    })).toThrow();
  });

  it("拒绝安全卡中的优先核对点", () => {
    expect(() =>
      DecisionCardSchema.parse({
        ...safeCard,
        reviewFocus: {
          field: "certificate",
          operatorPrompt: "请核对真伪或证书相关的可引用资料。",
        },
      }),
    ).toThrow();
  });

  it("拒绝不在核验清单中的优先核对点", () => {
    expect(() =>
      DecisionCardSchema.parse({
        ...safeCard,
        verificationNeeded: [
          {
            field: "certificate",
            reason: "缺少可引用的证书资料，需要人工核对。",
          },
        ],
        reviewFocus: {
          field: "service_policy",
          operatorPrompt: "请核对当前适用的服务规则。",
        },
        safety: {
          ...safeCard.safety,
          status: "reviewRequired",
        },
      }),
    ).toThrow();
  });

  it("拒绝空白的商品描述和客户消息", () => {
    const result = DecisionCardRequestSchema.safeParse({
      contractVersion: "decision-card.v1",
      requestId: "req-2",
      tenantId: "tenant-a",
      operatorId: "operator-a",
      scenario: "scan_service",
      productDescription: "  ",
      latestCustomerMessage: "  ",
      conversation: [],
      evidence: [],
    });

    expect(result.success).toBe(false);
  });

  it("接受首次咨询和寄售结算场景", () => {
    for (const scenario of ["first_consultation", "consignment_settlement"]) {
      const result = DecisionCardRequestSchema.safeParse({
        contractVersion: "decision-card.v1",
        requestId: `req-${scenario}`,
        tenantId: "tenant-a",
        operatorId: "operator-a",
        scenario,
        productDescription: "客户咨询的珠宝",
        latestCustomerMessage: "请协助了解下一步。",
        conversation: [],
        evidence: [],
      });

      expect(result.success).toBe(true);
    }
  });

  it("拒绝未知场景", () => {
    const result = DecisionCardRequestSchema.safeParse({
      contractVersion: "decision-card.v1",
      requestId: "req-unknown-scenario",
      tenantId: "tenant-a",
      operatorId: "operator-a",
      scenario: "unknown_scenario",
      productDescription: "客户咨询的珠宝",
      latestCustomerMessage: "请协助了解下一步。",
      conversation: [],
      evidence: [],
    });

    expect(result.success).toBe(false);
  });
});
