import type { ScenarioDefinition } from "./contracts";

const base = {
  groupId: "defect_and_dispute",
  customerIntent: "report_product_issue",
  salesStage: "follow_up",
  replyStrategy: "collect_review_material",
  defaultVerificationField: "condition_or_defect",
  alwaysReview: true,
} as const;

export const DEFECT_AND_DISPUTE_SCENARIOS = [
  {
    ...base, id: "appearance_difference", label: "色差/外观差异", description: "表达重视并收集自然光图片、包装和收货状态",
    requiredInputs: ["自然光图片", "包装", "收货状态"], replyFrame: "收到，我先帮您把情况记录清楚。请通过授权渠道提供自然光图片、包装和收货状态，人工核对后说明下一步。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对商品记录与图片" },
  },
  {
    ...base, id: "scratch_or_chip", label: "划痕/缺口", description: "收集多角度图片和发现时间",
    requiredInputs: ["多角度图片", "发现时间"], replyFrame: "收到，我先帮您记录划痕或缺口的位置。请通过授权渠道提供多角度图片和发现时间，人工核对后说明下一步。",
    nextAction: { kind: "handoff_to_operator", text: "人工判断是否需要实物检查" },
  },
  {
    ...base, id: "loose_or_fallen_stone", label: "松动/掉石", description: "提醒暂停佩戴并妥善保管配件",
    requiredInputs: ["问题位置", "多角度图片", "当前状态"], replyFrame: "收到，我先帮您把情况记录清楚。请先暂停佩戴并妥善保管商品和配件，再通过授权渠道提供问题位置和多角度图片，人工核对后说明下一步。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认检查和送修路径" },
  },
  {
    ...base, id: "breakage", label: "断裂/损坏", description: "收集损坏位置和当前状态",
    requiredInputs: ["损坏位置", "当前状态", "图片"], replyFrame: "收到，我先帮您记录损坏位置和当前状态。请通过授权渠道提供图片，人工核对后说明安全的下一步。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认安全收件与检查方式" },
  },
  {
    ...base, id: "wrong_size", label: "尺寸不合", description: "收集实际佩戴情况和目标尺寸",
    requiredInputs: ["实际佩戴情况", "目标尺寸"], replyFrame: "收到，我先帮您记录实际佩戴情况和目标尺寸；是否可调整需要由人工核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对是否可调整" },
  },
  {
    ...base, id: "missing_or_mismatch", label: "配件缺失/实物不一致", description: "收集开箱、包装和实物信息",
    requiredInputs: ["开箱信息", "包装", "实物信息"], replyFrame: "收到，我先帮您把开箱、包装和实物情况记录清楚。请通过授权渠道补充相关信息，人工核对后说明下一步。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对发货记录与处理规则" },
  },
  {
    ...base, id: "complaint_escalation", label: "投诉升级", description: "完整记录投诉要点并交由负责人核对处理",
    requiredInputs: ["订单或商品识别信息", "投诉经过", "客户希望解决的问题"],
    replyFrame: "我先完整记录您的问题和希望解决的方向，并交由负责人核对订单、实物和适用处理规则后回复。",
    nextAction: { kind: "handoff_to_operator", text: "负责人核对事实和适用处理规则" },
  },
] as const satisfies readonly ScenarioDefinition[];
