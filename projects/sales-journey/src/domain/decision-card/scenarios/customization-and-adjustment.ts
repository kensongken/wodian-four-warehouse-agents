import type { ScenarioDefinition } from "./contracts";

export const CUSTOMIZATION_AND_ADJUSTMENT_SCENARIOS = [
  {
    id: "custom_design", groupId: "customization_and_adjustment", label: "个性化定制", description: "收集用途、风格、尺寸、预算范围和时间",
    customerIntent: "request_customization", salesStage: "discover", requiredInputs: ["用途", "风格", "尺寸", "预算范围", "时间"], replyStrategy: "clarify",
    replyFrame: "请先告诉我用途、风格、尺寸、预算范围和时间安排；具体方案需要交由设计和工艺人员评估。",
    nextAction: { kind: "handoff_to_operator", text: "交设计/工艺人员评估" }, alwaysReview: false,
  },
  {
    id: "engraving", groupId: "customization_and_adjustment", label: "刻字", description: "收集内容、字符类型和位置偏好",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["刻字内容", "字符类型", "位置偏好"], replyStrategy: "clarify",
    replyFrame: "请提供刻字内容、字符类型和位置偏好；是否支持及具体限制需要由同事核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对款式是否支持及限制" }, alwaysReview: false,
  },
  {
    id: "resizing", groupId: "customization_and_adjustment", label: "改尺寸", description: "收集原尺寸、目标尺寸、图片和佩戴情况",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["原尺寸", "目标尺寸", "图片", "佩戴情况"], replyStrategy: "collect_review_material",
    replyFrame: "请先提供原尺寸、目标尺寸、图片和佩戴情况；是否可行需要工艺人员评估。",
    nextAction: { kind: "handoff_to_operator", text: "工艺人员评估可行性" }, alwaysReview: false,
  },
  {
    id: "bead_add_or_remove", groupId: "customization_and_adjustment", label: "增减珠/调整长度", description: "收集当前数量、目标长度和图片",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["当前数量", "目标长度", "图片"], replyStrategy: "collect_review_material",
    replyFrame: "请先提供当前数量、目标长度和图片；配件和工艺情况需要由同事核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对配件和工艺" }, alwaysReview: false,
  },
  {
    id: "exchange_style", groupId: "customization_and_adjustment", label: "换款", description: "先区分订单制作前换款还是旧件改款",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["换款类型", "订单或旧件情况"], replyStrategy: "clarify",
    replyFrame: "我先帮您区分订单制作前换款还是旧件改款；适用规则需要由同事确认，暂不计算差价。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认适用规则，不计算差价" }, alwaysReview: false,
  },
  {
    id: "swap_or_redesign", groupId: "customization_and_adjustment", label: "旧件改款/重新设计", description: "收集原物图片、尺寸、风格和预算范围",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["原物图片", "尺寸", "风格", "预算范围"], replyStrategy: "collect_review_material",
    replyFrame: "请先提供原物图片、尺寸、风格和预算范围；改款方向需要由同事进一步评估。",
    nextAction: { kind: "handoff_to_operator", text: "人工评估改款方向" }, alwaysReview: false,
  },
  {
    id: "resetting", groupId: "customization_and_adjustment", label: "重新镶嵌", description: "收集主石/原件图片、当前状态和目标款式",
    customerIntent: "request_customization", salesStage: "evaluate", requiredInputs: ["主石或原件图片", "当前状态", "目标款式"], replyStrategy: "collect_review_material",
    replyFrame: "可以先通过授权渠道提供原件和主石的多角度图片、当前状态及喜欢的款式方向；是否适合重新镶嵌，需要工艺人员实物检查后确认。",
    nextAction: { kind: "handoff_to_operator", text: "工艺人员实物检查后确认" }, alwaysReview: false,
  },
] as const satisfies readonly ScenarioDefinition[];
