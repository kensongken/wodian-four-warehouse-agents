import { AFTER_SALES_AND_CARE_SCENARIOS } from "./after-sales-and-care";
import { CONSIGNMENT_AND_RETURN_SCENARIOS } from "./consignment-and-return";
import type { ScenarioDefinition, ScenarioGroupId } from "./contracts";
import { CUSTOMIZATION_AND_ADJUSTMENT_SCENARIOS } from "./customization-and-adjustment";
import { DEFECT_AND_DISPUTE_SCENARIOS } from "./defect-and-dispute";
import { FOLLOW_UP_AND_RETENTION_SCENARIOS } from "./follow-up-and-retention";
import { IDENTITY_AND_SCAN_SCENARIOS } from "./identity-and-scan";
import { ORDER_AND_DELIVERY_SCENARIOS } from "./order-and-delivery";
import { PRODUCT_AND_PURCHASE_SCENARIOS } from "./product-and-purchase";
import type { SalesScenario } from "../types";

export const SCENARIO_GROUPS = [
  { id: "product_and_purchase", label: "商品咨询与购买", description: "从首次咨询到购买前确认。" },
  { id: "order_and_delivery", label: "订单与交付", description: "处理库存、订单、制作、发货和收货咨询。" },
  { id: "customization_and_adjustment", label: "定制与调整", description: "处理定制、刻字、尺寸、换款、改款和重新镶嵌。" },
  { id: "defect_and_dispute", label: "瑕疵与争议", description: "安全承接外观、损坏、掉石、尺寸和实物差异问题。" },
  { id: "after_sales_and_care", label: "售后与养护", description: "处理养护、抛光、维修、补件、资格和进度咨询。" },
  { id: "identity_and_scan", label: "身份珠与扫码", description: "处理身份珠、扫码和服务记录核对。" },
  { id: "consignment_and_return", label: "寄售与送回", description: "处理寄售咨询、实物检查、状态、结算和受控送回。" },
  { id: "follow_up_and_retention", label: "客户跟进与复购", description: "处理犹豫、暂未回复、回访、复购和推荐。" },
] as const;

export const GLOBAL_FALLBACK_SCENARIO = {
  id: "other_needs_review",
  groupId: null,
  label: "其他/不确定",
  description: "先整理需求，再由相关同事核对适用场景和规则。",
  customerIntent: "needs_human_review",
  salesStage: "discover",
  requiredInputs: ["客户最希望解决的问题"],
  replyStrategy: "clarify",
  replyFrame: "我先把您的需求整理清楚，再由相关同事核对后给您明确答复；您可以先告诉我最希望解决的是购买、订单、改款、售后还是寄售问题。",
  nextAction: {
    kind: "handoff_to_operator",
    text: "请人工判断业务场景并核对当前适用规则。",
  },
  defaultVerificationField: "service_policy",
  alwaysReview: true,
} as const satisfies ScenarioDefinition;

export const SCENARIO_DEFINITIONS = [
  ...PRODUCT_AND_PURCHASE_SCENARIOS,
  ...ORDER_AND_DELIVERY_SCENARIOS,
  ...CUSTOMIZATION_AND_ADJUSTMENT_SCENARIOS,
  ...DEFECT_AND_DISPUTE_SCENARIOS,
  ...AFTER_SALES_AND_CARE_SCENARIOS,
  ...IDENTITY_AND_SCAN_SCENARIOS,
  ...CONSIGNMENT_AND_RETURN_SCENARIOS,
  ...FOLLOW_UP_AND_RETENTION_SCENARIOS,
  GLOBAL_FALLBACK_SCENARIO,
] as const satisfies readonly ScenarioDefinition[];

const byId = new Map(SCENARIO_DEFINITIONS.map((item) => [item.id, item]));

export function scenarioDefinitionFor(scenario: SalesScenario): ScenarioDefinition {
  const definition = byId.get(scenario);
  if (definition === undefined) throw new Error(`未注册的销售场景：${scenario}`);
  return definition;
}

export function scenariosForGroup(groupId: ScenarioGroupId): readonly ScenarioDefinition[] {
  return SCENARIO_DEFINITIONS.filter((item) => item.groupId === groupId);
}

export function scenarioOptionsForGroup(groupId: ScenarioGroupId): readonly ScenarioDefinition[] {
  return [...scenariosForGroup(groupId), GLOBAL_FALLBACK_SCENARIO];
}

export function scenarioLabelFor(scenario: SalesScenario): string {
  return scenarioDefinitionFor(scenario).label;
}
