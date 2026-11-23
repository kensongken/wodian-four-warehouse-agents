import { describe, expect, it } from "vitest";

import { createDecisionCard } from "@/domain/decision-card/advisor";
import { DecisionCardSchema } from "@/domain/decision-card/schema";
import { SCENARIO_DEFINITIONS } from "@/domain/decision-card/scenarios/catalog";
import { requestFor } from "../support/decision-card-fixtures";

describe("全场景决策卡生成", () => {
  it.each(SCENARIO_DEFINITIONS)("$id 生成完整 copy_only 卡片", (definition) => {
    const card = createDecisionCard(requestFor(definition.id));

    expect(DecisionCardSchema.parse(card)).toEqual(card);
    expect(card.scenario).toBe(definition.id);
    expect(card.deliveryMode).toBe("copy_only");
    expect(card.replyOptions).toHaveLength(1);
    expect(card.replyOptions[0].text.trim()).not.toBe("");
    expect(card.nextAction.text.trim()).not.toBe("");

    if (definition.alwaysReview) {
      expect(card.safety.status).toBe("reviewRequired");
      expect(card.verificationNeeded).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ field: definition.defaultVerificationField }),
        ]),
      );
    }
  });

  it("相同新场景请求稳定生成相同卡片", () => {
    const request = requestFor("resetting");

    expect(createDecisionCard(request)).toEqual(createDecisionCard(request));
  });

  it.each(SCENARIO_DEFINITIONS.filter((definition) => definition.alwaysReview))(
    "$id 遵守场景目录的强制人工复核与默认核验字段",
    (definition) => {
      const card = createDecisionCard(requestFor(definition.id));

      expect(definition.defaultVerificationField).toBeDefined();
      expect(card.safety.status).toBe("reviewRequired");
      expect(card.verificationNeeded).toContainEqual(
        expect.objectContaining({ field: definition.defaultVerificationField }),
      );
      expect(card.reviewFocus?.field).toBe(definition.defaultVerificationField);
    },
  );
});
