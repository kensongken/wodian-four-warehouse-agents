import type { ReplyFeedbackRequest } from "@/domain/reply-feedback/schema";

export const validFeedback: ReplyFeedbackRequest = {
  tenantId: "tenant-a",
  operatorId: "operator-1",
  cardId: "card-1",
  agentVersion: "reply-agent.v1",
  rating: "helpful",
  reasons: [],
  scenario: "identity_bead",
  salesStage: "evaluate",
  evidenceIds: [],
};
