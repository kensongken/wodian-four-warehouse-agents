import { describe, expect, it } from "vitest";
import {
  JourneyWriteBackSchema,
  SalesJourneyContextSchema,
} from "@/domain/sales-journey/schema";

describe("sales journey contract", () => {
  it("accepts a tenant-scoped journey snapshot", () => {
    expect(SalesJourneyContextSchema.parse({
      tenantId: "tenant-a",
      journeyId: "journey-1",
      customerId: "customer-1",
      subjectId: "subject-1",
      operatorId: "operator-1",
      currentStage: "evaluate",
      customerCategory: "repeat_customer",
      confirmedNeeds: ["佩戴效果"],
      pendingQuestions: ["产地资料"],
      commitments: [],
      openTasks: ["核对证书"],
      recentConversation: [{ speaker: "customer", text: "有证书吗？" }],
    }).tenantId).toBe("tenant-a");
  });

  it("requires an explicit operator confirmation for write-back", () => {
    expect(() => JourneyWriteBackSchema.parse({
      journeyId: "journey-1",
      tenantId: "tenant-a",
      operatorId: "operator-1",
      proposedStage: "advance",
      nextAction: "约定复核时间",
      confirmedByOperator: false,
    })).toThrow();
  });
});
