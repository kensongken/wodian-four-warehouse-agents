import type { ScenarioDefinition } from "./contracts";

const base = {
  groupId: "consignment_and_return",
  customerIntent: "request_consignment",
  salesStage: "evaluate",
  replyStrategy: "collect_review_material",
  defaultVerificationField: "consignment_terms",
  alwaysReview: true,
} as const;

export const CONSIGNMENT_AND_RETURN_SCENARIOS = [
  {
    ...base, id: "consignment_inquiry", label: "寄售初步咨询", description: "了解品类、状态及客户期望",
    requiredInputs: ["品类", "状态", "客户期望"], replyFrame: "我可以先帮您整理寄售咨询需要的基础信息；是否提供此服务需要由人工按当前规则确认。",
    nextAction: { kind: "handoff_to_operator", text: "转人工说明当前是否提供此服务" },
  },
  {
    ...base, id: "consignment_materials", label: "寄售资料准备", description: "提示通过授权渠道准备图片、购买资料和证书信息",
    requiredInputs: ["图片", "购买资料", "证书信息"], replyFrame: "请通过授权渠道准备图片、购买资料和证书信息；最小资料清单需要由同事确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认最小资料清单" },
  },
  {
    ...base, id: "consignment_preliminary_review", label: "初步评估进度", description: "说明线上信息只能用于初步了解",
    requiredInputs: ["已提交资料", "想了解的进度"], replyFrame: "线上信息只能用于初步了解；专业人员核对前，我不能提供估价或结论。",
    nextAction: { kind: "handoff_to_operator", text: "等待专业人员核对，不给估价" },
  },
  {
    ...base, id: "consignment_physical_inspection", label: "实物验收", description: "说明是否送检/送仓必须由人工确认",
    requiredInputs: ["实物状态", "已有资料"], replyFrame: "是否送检或送仓必须由人工确认；在确认前请不要自行寄送或交付实物。",
    nextAction: { kind: "handoff_to_operator", text: "人工提供受控收件路径" },
  },
  {
    ...base, id: "consignment_eligibility", label: "是否受理", description: "不确认一定受理、上架或成交",
    requiredInputs: ["品类", "实物状态", "已有资料"], replyFrame: "是否受理、上架或成交都需要专业人员依据规则和实物状态决定，我不能在此确认。",
    nextAction: { kind: "handoff_to_operator", text: "人工依据规则和实物状态决定" },
  },
  {
    ...base, id: "consignment_listing_or_unsold", label: "上架进度/未售处理", description: "承接状态问题，不虚构买家或进度",
    requiredInputs: ["寄售识别信息", "想了解的状态"], replyFrame: "我先记录您想了解的状态；真实进度和适用规则需要由同事查询，不会虚构买家或进度。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询真实状态和适用规则" },
  },
  {
    ...base, id: "consignment_settlement", label: "成交与结算咨询", description: "不确认成交金额、费用、付款日或到账",
    requiredInputs: ["寄售识别信息", "想核对的结算问题"], replyFrame: "成交金额、费用、付款日和到账都需要由人工核对协议、成交与结算记录后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对协议、成交与结算记录" },
  },
  {
    ...base, id: "controlled_reuse_or_return", label: "送回、仓验与受控再利用", description: "收集送回和仓验所需资料",
    requiredInputs: ["送回资料", "仓验资料"], replyFrame: "请先提供送回和仓验所需资料；养护、改款或受控再利用需要在仓验后由人工确认。",
    nextAction: { kind: "handoff_to_operator", text: "仓验后由人工确认养护、改款或受控再利用" }, defaultVerificationField: "reuse_or_return_terms",
  },
] as const satisfies readonly ScenarioDefinition[];
