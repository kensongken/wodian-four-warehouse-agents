import type { DecisionCard, ReplyOption } from "../schema";
import type {
  CustomerIntent,
  SalesScenario,
  SalesStage,
  VerificationField,
} from "../types";

export const SCENARIO_GROUP_IDS = [
  "product_and_purchase", "order_and_delivery", "customization_and_adjustment",
  "defect_and_dispute", "after_sales_and_care", "identity_and_scan",
  "consignment_and_return", "follow_up_and_retention",
] as const;

export type ScenarioGroupId = (typeof SCENARIO_GROUP_IDS)[number];

export type ScenarioDefinition = {
  id: SalesScenario;
  groupId: ScenarioGroupId | null;
  label: string;
  description: string;
  customerIntent: CustomerIntent;
  salesStage: SalesStage;
  requiredInputs: readonly string[];
  replyStrategy: ReplyOption["strategy"];
  replyFrame: string;
  nextAction: DecisionCard["nextAction"];
  defaultVerificationField?: VerificationField;
  alwaysReview: boolean;
};
