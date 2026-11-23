import type { ScenarioDefinition } from "./contracts";

export const FOLLOW_UP_AND_RETENTION_SCENARIOS = [
  {
    id: "hesitation_followup", groupId: "follow_up_and_retention", label: "客户犹豫", description: "询问尚未解决的顾虑",
    customerIntent: "follow_up_or_repurchase", salesStage: "follow_up", requiredInputs: ["尚未解决的顾虑"], replyStrategy: "clarify",
    replyFrame: "想请您告诉我目前还有哪些顾虑没有解决，我会帮您回到对应业务场景继续整理。",
    nextAction: { kind: "ask_clarifying_question", text: "根据顾虑回到对应业务场景" }, alwaysReview: false,
  },
  {
    id: "silent_customer_followup", groupId: "follow_up_and_retention", label: "客户暂未回复", description: "生成一次温和、不施压的跟进草稿",
    customerIntent: "follow_up_or_repurchase", salesStage: "follow_up", requiredInputs: ["上次沟通的待确认点"], replyStrategy: "clarify",
    replyFrame: "想温和跟进一下，若您还有想了解或需要补充的地方，随时告诉我；不着急，您方便时回复即可。",
    nextAction: { kind: "ask_clarifying_question", text: "等待客户回应，不连续催促" }, alwaysReview: false,
  },
  {
    id: "post_discussion_followup", groupId: "follow_up_and_retention", label: "沟通后跟进", description: "总结已讨论内容和待确认点",
    customerIntent: "follow_up_or_repurchase", salesStage: "follow_up", requiredInputs: ["已讨论内容", "待确认点"], replyStrategy: "clarify",
    replyFrame: "我可以先整理已讨论内容和待确认点，具体事实由同事核对后再跟进。",
    nextAction: { kind: "handoff_to_operator", text: "人工核对事实后跟进" }, alwaysReview: false,
  },
  {
    id: "repeat_purchase", groupId: "follow_up_and_retention", label: "老客复购", description: "询问本次用途和偏好变化",
    customerIntent: "follow_up_or_repurchase", salesStage: "discover", requiredInputs: ["本次用途", "偏好变化"], replyStrategy: "clarify",
    replyFrame: "可以先告诉我这次的用途和偏好变化，我会重新为您做需求了解，不假定您过去的偏好。",
    nextAction: { kind: "ask_clarifying_question", text: "重新做需求了解，不假定历史偏好" }, alwaysReview: false,
  },
  {
    id: "festival_or_referral", groupId: "follow_up_and_retention", label: "节日回访/客户推荐", description: "生成非夸大、非骚扰式问候",
    customerIntent: "follow_up_or_repurchase", salesStage: "follow_up", requiredInputs: ["问候场景", "客户当前意愿"], replyStrategy: "clarify",
    replyFrame: "想向您送上一句节日问候；若您近期有需要了解的方向，随时告诉我即可。",
    nextAction: { kind: "handoff_to_operator", text: "人工选择适当客户并发送" }, alwaysReview: false,
  },
  {
    id: "referral_tracking", groupId: "follow_up_and_retention", label: "转介绍跟进", description: "记录介绍关系和客户意愿，不自动联系或承诺优惠",
    customerIntent: "follow_up_or_repurchase", salesStage: "follow_up", requiredInputs: ["介绍关系", "新客户意愿", "希望了解的方向"], replyStrategy: "collect_review_material",
    replyFrame: "我先记录介绍关系和想了解的方向；是否联系、如何联系以及适用活动都需要人工确认，不会自动发送消息。",
    nextAction: { kind: "handoff_to_operator", text: "人工确认联系方式与适用规则" }, defaultVerificationField: "service_policy", alwaysReview: true,
  },
] as const satisfies readonly ScenarioDefinition[];
