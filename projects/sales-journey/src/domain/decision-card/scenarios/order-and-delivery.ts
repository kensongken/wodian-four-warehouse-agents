import type { ScenarioDefinition } from "./contracts";

export const ORDER_AND_DELIVERY_SCENARIOS = [
  {
    id: "stock_availability", groupId: "order_and_delivery", label: "库存咨询", description: "收集款式/规格，不判断现货",
    customerIntent: "confirm_purchase_or_order", salesStage: "evaluate", requiredInputs: ["款式", "规格"], replyStrategy: "collect_review_material",
    replyFrame: "请告诉我您想了解的款式和规格；当前库存需要由同事查询后确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询当前库存" }, defaultVerificationField: "inventory_or_timeline", alwaysReview: true,
  },
  {
    id: "order_detail_confirmation", groupId: "order_and_delivery", label: "订单信息确认", description: "提醒人工核对款式、尺寸、刻字和收货信息",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["订单识别信息", "待核对内容"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您希望核对的订单信息；款式、尺寸、刻字和收货信息需要员工在授权订单系统中核对。",
    nextAction: { kind: "handoff_to_operator", text: "员工在授权订单系统核对" }, defaultVerificationField: "order_or_delivery", alwaysReview: true,
  },
  {
    id: "change_before_production", groupId: "order_and_delivery", label: "制作前修改", description: "收集希望修改的内容",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["希望修改的内容", "订单识别信息"], replyStrategy: "collect_review_material",
    replyFrame: "请告诉我希望修改的内容；是否已进入制作及能否调整，需要由同事核对后确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认是否已进入制作及能否调整" }, defaultVerificationField: "order_or_delivery", alwaysReview: true,
  },
  {
    id: "production_progress", groupId: "order_and_delivery", label: "制作进度", description: "承接进度追问，不承诺完成日期",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["订单识别信息", "想了解的进度"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您想了解的制作进度；完成日期和真实制作状态需要由同事查询后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询真实制作状态" }, defaultVerificationField: "inventory_or_timeline", alwaysReview: true,
  },
  {
    id: "shipping_arrangement", groupId: "order_and_delivery", label: "发货安排", description: "收集客户希望了解的配送问题",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["订单识别信息", "配送问题"], replyStrategy: "collect_review_material",
    replyFrame: "请告诉我您希望了解的配送问题；发货规则和时效需要由同事按当前情况核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对发货规则和时效" }, defaultVerificationField: "order_or_delivery", alwaysReview: true,
  },
  {
    id: "logistics_or_receipt", groupId: "order_and_delivery", label: "物流/收货", description: "承接物流或签收问题",
    customerIntent: "confirm_purchase_or_order", salesStage: "follow_up", requiredInputs: ["订单识别信息", "物流或签收问题"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您的物流或签收问题；真实物流记录需要由同事查询后核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询真实物流记录" }, defaultVerificationField: "order_or_delivery", alwaysReview: true,
  },
  {
    id: "deposit_or_payment", groupId: "order_and_delivery", label: "定金/付款", description: "收集定金或付款问题，不确认金额、方式或到账",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["订单识别信息", "付款问题"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您的订金或付款问题；金额、付款方式和到账状态需要同事核对后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对付款规则和订单记录" }, defaultVerificationField: "price_or_payment", alwaysReview: true,
  },
  {
    id: "delivery_deadline", groupId: "order_and_delivery", label: "交付时效", description: "承接送礼或使用时间追问，不承诺到货日期",
    customerIntent: "confirm_purchase_or_order", salesStage: "advance", requiredInputs: ["订单识别信息", "期望使用时间"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您的期望使用时间；真实制作和配送进度需要同事查询后说明，不能在这里承诺到货日期。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询制作与配送进度" }, defaultVerificationField: "inventory_or_timeline", alwaysReview: true,
  },
] as const satisfies readonly ScenarioDefinition[];
