import type { ScenarioDefinition } from "./contracts";

export const IDENTITY_AND_SCAN_SCENARIOS = [
  {
    id: "identity_bead", groupId: "identity_and_scan", label: "身份珠说明", description: "按已核验资料说明展示与核对方式",
    customerIntent: "learn_identity", salesStage: "explain", requiredInputs: ["最关心的部分"], replyStrategy: "explain",
    replyFrame: "我可以按已核验资料说明展示与核对方式；请告诉我您最关心的部分。",
    nextAction: { kind: "ask_clarifying_question", text: "询问客户最关心的部分" }, alwaysReview: false,
  },
  {
    id: "scan_service", groupId: "identity_and_scan", label: "扫码服务", description: "说明扫码是资料核对的一步",
    customerIntent: "verify_service", salesStage: "explain", requiredInputs: ["页面信息", "想核对的问题"], replyStrategy: "explain",
    replyFrame: "扫码是资料核对的一步，不作为真伪、品质、产地或价值的最终判断依据；请通过授权渠道提供页面信息。",
    nextAction: { kind: "share_verified_service_path", text: "请客户通过授权渠道提供页面信息" }, alwaysReview: false,
  },
  {
    id: "record_check", groupId: "identity_and_scan", label: "商品资料核对", description: "收集客户想核对的字段",
    customerIntent: "verify_service", salesStage: "explain", requiredInputs: ["想核对的字段"], replyStrategy: "clarify",
    replyFrame: "请告诉我您想核对的字段；可引用资料需要由同事进一步核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对可引用资料" }, alwaysReview: false,
  },
  {
    id: "service_record_question", groupId: "identity_and_scan", label: "服务记录咨询", description: "收集客户关注的服务节点",
    customerIntent: "verify_service", salesStage: "follow_up", requiredInputs: ["关注的服务节点"], replyStrategy: "clarify",
    replyFrame: "请告诉我您关注的服务节点；适用服务记录需要由同事查询后核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询适用服务记录" }, alwaysReview: false,
  },
] as const satisfies readonly ScenarioDefinition[];
