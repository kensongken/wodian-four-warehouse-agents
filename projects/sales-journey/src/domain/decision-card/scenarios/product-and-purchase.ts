import type { ScenarioDefinition } from "./contracts";

export const PRODUCT_AND_PURCHASE_SCENARIOS = [
  {
    id: "first_consultation", groupId: "product_and_purchase", label: "首次咨询",
    description: "了解用途、对象和关注点", customerIntent: "explore_purchase", salesStage: "discover",
    requiredInputs: ["用途", "对象", "最关注的问题"], replyStrategy: "clarify",
    replyFrame: "可以先告诉我这次主要是日常佩戴、送礼还是其他用途，以及您更在意的方向；我整理后再帮您缩小选择范围。",
    nextAction: { kind: "ask_clarifying_question", text: "询问佩戴/送礼/收藏等用途" }, alwaysReview: false,
  },
  {
    id: "needs_discovery", groupId: "product_and_purchase", label: "需求了解",
    description: "收集风格、尺寸、使用频率和时间安排", customerIntent: "explore_purchase", salesStage: "discover",
    requiredInputs: ["风格", "尺寸", "使用频率", "时间安排"], replyStrategy: "clarify",
    replyFrame: "为了更准确地整理方向，请您说明风格、尺寸和时间安排中最优先的三项偏好。",
    nextAction: { kind: "ask_clarifying_question", text: "请客户说明三项优先偏好" }, alwaysReview: false,
  },
  {
    id: "product_selection", groupId: "product_and_purchase", label: "帮客户选款",
    description: "根据客户明确提供的偏好整理选款方向", customerIntent: "explore_purchase", salesStage: "evaluate",
    requiredInputs: ["明确偏好", "用途", "尺寸或佩戴习惯"], replyStrategy: "clarify",
    replyFrame: "我可以根据您明确提供的偏好整理选款方向，具体可选范围再由同事按现有商品核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工按现有商品范围推荐" }, alwaysReview: false,
  },
  {
    id: "product_comparison", groupId: "product_and_purchase", label: "款式对比",
    description: "帮客户梳理尺寸、佩戴感、风格差异", customerIntent: "compare_options", salesStage: "evaluate",
    requiredInputs: ["想比较的款式", "最看重的维度"], replyStrategy: "clarify",
    replyFrame: "我可以先帮您梳理尺寸、佩戴感和风格等差异；请告诉我您最看重的比较维度。",
    nextAction: { kind: "ask_clarifying_question", text: "确认客户最看重的比较维度" }, alwaysReview: false,
  },
  {
    id: "wearing_or_gifting", groupId: "product_and_purchase", label: "佩戴搭配/送礼",
    description: "询问场合、对象、风格和尺寸", customerIntent: "explore_purchase", salesStage: "discover",
    requiredInputs: ["场合", "对象", "风格", "尺寸"], replyStrategy: "clarify",
    replyFrame: "可以先告诉我佩戴或送礼的场合、对象、风格和尺寸偏好，我再帮您整理适合了解的方向。",
    nextAction: { kind: "ask_clarifying_question", text: "补齐使用场景和时间" }, alwaysReview: false,
  },
  {
    id: "size_and_fit", groupId: "product_and_purchase", label: "尺寸与佩戴",
    description: "收集手围、圈口、长度和佩戴偏好", customerIntent: "explore_purchase", salesStage: "evaluate",
    requiredInputs: ["手围或圈口", "长度", "佩戴偏好"], replyStrategy: "clarify",
    replyFrame: "请先提供手围、圈口或长度，以及偏松或偏合手的佩戴偏好；具体款式适配性需要同事核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对具体款式适配性" }, alwaysReview: false,
  },
  {
    id: "budget_or_price", groupId: "product_and_purchase", label: "预算与价格",
    description: "询问预算区间并安全承接", customerIntent: "price_or_value_question", salesStage: "evaluate",
    requiredInputs: ["预算区间", "用途", "偏好"], replyStrategy: "clarify",
    replyFrame: "可以先告诉我您的预算区间和主要偏好；当前商品与价格需要由同事按实际情况核对后说明。",
    nextAction: { kind: "handoff_to_operator", text: "由人工核对当前商品与价格" }, defaultVerificationField: "price_or_payment", alwaysReview: true,
  },
  {
    id: "purchase_confirmation", groupId: "product_and_purchase", label: "购买前确认",
    description: "汇总客户已确认的款式、尺寸和需求", customerIntent: "confirm_purchase_or_order", salesStage: "advance",
    requiredInputs: ["款式", "尺寸", "已确认需求"], replyStrategy: "collect_review_material",
    replyFrame: "我先帮您汇总已确认的款式、尺寸和需求，库存、价格及交易规则请由同事按当前情况核对后确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认库存、价格及交易规则" }, defaultVerificationField: "price_or_payment", alwaysReview: true,
  },
  {
    id: "store_visit_appointment", groupId: "product_and_purchase", label: "到店预约",
    description: "收集到店时间、人数和想看的方向，不承诺接待安排", customerIntent: "confirm_purchase_or_order", salesStage: "advance",
    requiredInputs: ["希望到店时间", "人数", "想了解的方向"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您的到店时间、人数和想了解的方向；门店接待安排需要同事按实际情况确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认门店接待安排" }, defaultVerificationField: "service_policy", alwaysReview: true,
  },
  {
    id: "promotion_or_coupon", groupId: "product_and_purchase", label: "活动/优惠咨询",
    description: "承接活动、优惠券和适用范围问题，不承诺优惠", customerIntent: "price_or_value_question", salesStage: "evaluate",
    requiredInputs: ["关注的活动或优惠", "商品方向"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您关注的活动和商品方向；活动规则、适用范围和最终价格需要同事核对后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对活动规则与适用范围" }, defaultVerificationField: "price_or_payment", alwaysReview: true,
  },
] as const satisfies readonly ScenarioDefinition[];
