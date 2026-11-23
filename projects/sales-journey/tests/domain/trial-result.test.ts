import { describe, expect, it } from "vitest";

import { formatTrialResult } from "@/domain/decision-card/trial-result";

describe("OPC 试用结果记录", () => {
  it("只格式化去身份化的人工标签，并声明不计入经营成果", () => {
    const text = formatTrialResult({
      cardId: "card-demo-01",
      scenario: "identity_bead",
      replyUsage: "adopted",
      customerOutcome: "follow_up",
      recordedAt: "2026-08-17T12:00:00.000Z",
    });

    expect(text).toContain("决策卡：card-demo-01");
    expect(text).toContain("场景：身份珠说明");
    expect(text).toContain("回复使用：采纳原文");
    expect(text).toContain("客户结果：待跟进");
    expect(text).toContain("待人工汇总，不计入经营成果。");
    expect(text).not.toMatch(/王女士|13800138000|8888/);
  });

  it("使用场景目录中的重新镶嵌标签", () => {
    const text = formatTrialResult({
      cardId: "card-resetting-01",
      scenario: "resetting",
      replyUsage: "edited",
      customerOutcome: "follow_up",
      recordedAt: "2026-08-17T12:00:00.000Z",
    });

    expect(text).toContain("场景：重新镶嵌");
  });
});
