import { describe, expect, it } from "vitest";

import { evaluateSafety } from "@/domain/decision-card/policy";
import type { SafetyRuleId, SalesScenario } from "@/domain/decision-card/types";
import { cardWithReply, requestFor } from "../support/decision-card-fixtures";

type UnsafeCase = readonly [SalesScenario, string, SafetyRuleId];
type AllowedCase = readonly [SalesScenario, string];

const UNSAFE_CASES: readonly UnsafeCase[] = [
  ["identity_bead", "这颗一定是顶级料", "EVIDENCE_REQUIRED_FOR_FACT"],
  ["identity_bead", "这颗产自阿富汗", "EVIDENCE_REQUIRED_FOR_FACT"],
  ["scan_service", "扫码就能证明真伪", "SERVICE_POLICY_VERIFICATION"],
  ["scan_service", "我们支持七天无理由退货", "SERVICE_POLICY_VERIFICATION"],
  ["swap_or_redesign", "旧珠肯定可以抵扣一半", "EVIDENCE_REQUIRED_FOR_FACT"],
  ["swap_or_redesign", "这次改款绝对保值", "EVIDENCE_REQUIRED_FOR_FACT"],
  ["controlled_reuse_or_return", "我们保底按 3000 元处理", "REUSE_OR_RETURN_NO_FIXED_PRICE"],
  ["controlled_reuse_or_return", "我们会马上回购并当天到账", "REUSE_OR_RETURN_NO_FIXED_PRICE"],
];

const ALLOWED_CASES: readonly AllowedCase[] = [
  ["identity_bead", "我可以先按已批准资料帮您核对展示方式。"],
  ["identity_bead", "您更关心辨识方式、佩戴效果还是来源资料？"],
  ["scan_service", "请把扫码页面内容或截图发我，我先整理资料。"],
  ["scan_service", "扫码信息只是核对资料的一步，需要人工一起核对。"],
  ["swap_or_redesign", "请把原物图片、尺寸和喜欢的风格发我。"],
  ["swap_or_redesign", "我先整理预算范围和改款偏好，再由人工确认下一步。"],
  ["controlled_reuse_or_return", "请先送回仓库核验，人工确认后再决定下一步。"],
  ["controlled_reuse_or_return", "请提供原物图片、规格和当前状态，方便仓验后安排养护或改款。"],
];

describe("销售决策卡安全语料", () => {
  it.each(UNSAFE_CASES)("拦截不安全的 %s 文案：%s", (scenario, text, rule) => {
    const result = evaluateSafety(requestFor(scenario), cardWithReply(text));

    expect(result.status).toBe("reviewRequired");
    expect(result.rules).toContain(rule);
  });

  it.each(ALLOWED_CASES)("允许不承诺的 %s 引导文案：%s", (scenario, text) => {
    const result = evaluateSafety(requestFor(scenario), cardWithReply(text));

    if (scenario === "controlled_reuse_or_return") {
      expect(result.rules).toContain("REUSE_OR_RETURN_HUMAN_REVIEW");
      expect(result.rules).not.toContain("EVIDENCE_REQUIRED_FOR_FACT");
    } else {
      expect(result.status).toBe("safe");
    }
  });
});
