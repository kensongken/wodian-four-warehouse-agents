import type { ScenarioDefinition } from "./contracts";

export const AFTER_SALES_AND_CARE_SCENARIOS = [
  {
    id: "cleaning_or_care", groupId: "after_sales_and_care", label: "清洗/日常养护", description: "收集材质自述、佩戴情况和当前状态",
    customerIntent: "request_after_sales", salesStage: "explain", requiredInputs: ["材质自述", "佩戴情况", "当前状态"], replyStrategy: "clarify",
    replyFrame: "请先告诉我材质自述、佩戴情况和当前状态；适用的养护说明需要由同事按已核验资料提供。",
    nextAction: { kind: "handoff_to_operator", text: "人工提供适用的已核验养护说明" }, alwaysReview: false,
  },
  {
    id: "polishing_or_refinishing", groupId: "after_sales_and_care", label: "抛光/翻新", description: "收集图片和希望改善的位置",
    customerIntent: "request_after_sales", salesStage: "evaluate", requiredInputs: ["图片", "希望改善的位置"], replyStrategy: "collect_review_material",
    replyFrame: "请先通过授权渠道提供图片和希望改善的位置；适用性和可能变化需要由同事评估。",
    nextAction: { kind: "handoff_to_operator", text: "人工评估适用性和可能变化" }, alwaysReview: false,
  },
  {
    id: "repair", groupId: "after_sales_and_care", label: "维修", description: "收集损坏位置、图片和使用情况",
    customerIntent: "request_after_sales", salesStage: "evaluate", requiredInputs: ["损坏位置", "图片", "使用情况"], replyStrategy: "collect_review_material",
    replyFrame: "请先提供损坏位置、图片和使用情况；是否需要实物检查由人工判断。",
    nextAction: { kind: "handoff_to_operator", text: "人工判断是否需实物检查" }, alwaysReview: false,
  },
  {
    id: "part_replacement", groupId: "after_sales_and_care", label: "补件/更换配件", description: "收集缺失或损坏配件信息",
    customerIntent: "request_after_sales", salesStage: "evaluate", requiredInputs: ["缺失或损坏配件信息", "图片"], replyStrategy: "collect_review_material",
    replyFrame: "请先告诉我缺失或损坏的配件情况；配件和工艺可用性需要由同事核对。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认配件和工艺可用性" }, alwaysReview: false,
  },
  {
    id: "service_eligibility", groupId: "after_sales_and_care", label: "售后资格/退换咨询", description: "承接问题并收集购买凭证类型",
    customerIntent: "request_after_sales", salesStage: "evaluate", requiredInputs: ["购买凭证类型", "咨询问题"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录您的问题和购买凭证类型；适用服务规则需要由同事核对后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对适用服务规则" }, defaultVerificationField: "after_sales_policy", alwaysReview: true,
  },
  {
    id: "service_progress", groupId: "after_sales_and_care", label: "售后进度", description: "收集已脱敏的服务阶段信息",
    customerIntent: "request_after_sales", salesStage: "follow_up", requiredInputs: ["已脱敏的服务阶段信息"], replyStrategy: "collect_review_material",
    replyFrame: "请提供已脱敏的服务阶段信息；真实工单状态需要由同事查询后说明。",
    nextAction: { kind: "handoff_to_operator", text: "人工查询真实工单状态" }, defaultVerificationField: "after_sales_policy", alwaysReview: true,
  },
] as const satisfies readonly ScenarioDefinition[];
