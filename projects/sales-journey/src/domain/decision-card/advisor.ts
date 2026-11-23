import { buildSafeContinuation, selectReviewFocus } from "./continuation";
import { customerCategoryContext } from "./customer-context";
import { evaluateSafety } from "./policy";
import { DecisionCardSchema } from "./schema";
import { scenarioDefinitionFor } from "./scenarios/catalog";
import { resolveOperatorStyle } from "./style";
import type {
  CustomerIntent,
  DecisionCard,
  DecisionCardRequest,
  ReplyOption,
  SalesScenario,
  SafetyRuleId,
  SalesStage,
} from "./types";

type Verification = DecisionCard["verificationNeeded"][number];

type ScenarioTemplate = {
  customerIntent: CustomerIntent;
  salesStage: SalesStage;
  summary: string;
  reply: Omit<ReplyOption, "id">;
  nextAction: DecisionCard["nextAction"];
  verificationNeeded?: Verification[];
  additionalRules?: SafetyRuleId[];
  additionalReasons?: string[];
};

function withCustomerCategoryContext(request: DecisionCardRequest, text: string): string {
  return `${customerCategoryContext(request)}${text}`;
}

function approvedEvidenceId(
  request: DecisionCardRequest,
  field: DecisionCardRequest["evidence"][number]["verifiedFields"][number],
): string | undefined {
  return request.evidence.find(
    (evidence) => evidence.status === "approved" && evidence.verifiedFields.includes(field),
  )?.id;
}

function applyOperatorStyle(text: string, request: DecisionCardRequest): string {
  const style = resolveOperatorStyle(request);
  const reply =
    style.preferredGreeting === undefined ? text : `${style.preferredGreeting}，${text}`;

  if (reply.length <= style.maxReplyCharacters) {
    return reply;
  }

  return "请人工核验后再确认。";
}

function identityBeadTemplate(request: DecisionCardRequest): ScenarioTemplate {
  const identityEvidenceId = approvedEvidenceId(request, "identity_bead");
  if (identityEvidenceId === undefined) {
    return {
      customerIntent: "needs_human_review",
      salesStage: "discover",
      summary: "先收集客户关注点，再由人工补充资料。",
      reply: {
        strategy: "clarify",
        text: withCustomerCategoryContext(
          request,
          "我先了解您想核对的内容，再由人工补充可引用的资料。",
        ),
        evidenceIds: [],
      },
      nextAction: {
        kind: "ask_clarifying_question",
        text: "您更关心辨识方式、佩戴效果，还是来源资料？",
      },
    };
  }

  return {
    customerIntent: "learn_identity",
    salesStage: "explain",
    summary: "依据已批准资料说明身份珠的核对方式。",
    reply: {
      strategy: "explain",
      text: withCustomerCategoryContext(
        request,
        "我可以按已批准资料为您说明身份珠的展示与核对方式；您也可以告诉我最关心的部分。",
      ),
      evidenceIds: [identityEvidenceId],
    },
    nextAction: {
      kind: "ask_clarifying_question",
      text: "您更关心辨识方式、佩戴效果，还是来源资料？",
    },
  };
}

function scanServiceTemplate(request: DecisionCardRequest): ScenarioTemplate {
  const scanEvidenceId = approvedEvidenceId(request, "scan_service");
  if (scanEvidenceId === undefined) {
    const reason = "缺少可引用的扫码资料，需要人工核对。";
    return {
      customerIntent: "needs_human_review",
      salesStage: "discover",
      summary: "扫码资料待人工核对。",
      reply: {
        strategy: "clarify",
        text: withCustomerCategoryContext(
          request,
          "请把扫码页面内容或截图发我，我先整理后由人工核对下一步。",
        ),
        evidenceIds: [],
      },
      nextAction: {
        kind: "ask_clarifying_question",
        text: "请把扫码页面内容或截图发我，方便人工核对。",
      },
      verificationNeeded: [{ field: "service_policy", reason }],
      additionalRules: ["SERVICE_POLICY_VERIFICATION"],
      additionalReasons: [reason],
    };
  }

  return {
    customerIntent: "verify_service",
    salesStage: "explain",
    summary: "提供扫码资料的核对建议。",
    reply: {
      strategy: "offer_service_path",
      text: withCustomerCategoryContext(
        request,
        "扫码信息可以作为核对资料的一步，不是单独的最终判断依据。请把页面内容或截图发我，我整理后由人工核对下一步。",
      ),
      evidenceIds: [scanEvidenceId],
    },
    nextAction: {
      kind: "share_verified_service_path",
      text: "请把扫码页面内容或截图发我，人工核对后确认下一步。",
    },
  };
}

const SPECIAL_TEMPLATE_BUILDERS: Partial<
  Record<SalesScenario, (request: DecisionCardRequest) => ScenarioTemplate>
> = {
  identity_bead: identityBeadTemplate,
  scan_service: scanServiceTemplate,
};

const PRESERVED_NEXT_ACTIONS: Partial<Record<SalesScenario, DecisionCard["nextAction"]>> = {
  swap_or_redesign: {
    kind: "ask_clarifying_question",
    text: "请发送原物图片、尺寸、喜欢的风格和预算范围。",
  },
  controlled_reuse_or_return: {
    kind: "collect_review_material",
    text: "请提交原物图片、规格和当前状态；送回仓库核验后由人工确认下一步。",
  },
};

function templateFromDefinition(request: DecisionCardRequest): ScenarioTemplate {
  const definition = scenarioDefinitionFor(request.scenario);
  const requiresManualReview = definition.alwaysReview;
  const reviewReason = "该业务场景需要人工核对当前资料和适用规则后，再决定回复内容。";

  return {
    customerIntent: definition.customerIntent,
    salesStage: definition.salesStage,
    summary: definition.description,
    reply: {
      strategy: definition.replyStrategy,
      text: withCustomerCategoryContext(request, definition.replyFrame),
      evidenceIds: [],
    },
    nextAction: PRESERVED_NEXT_ACTIONS[request.scenario] ?? definition.nextAction,
    additionalRules: requiresManualReview ? ["SCENARIO_HUMAN_REVIEW"] : undefined,
    additionalReasons: requiresManualReview ? [reviewReason] : undefined,
  };
}

function templateFor(request: DecisionCardRequest): ScenarioTemplate {
  return SPECIAL_TEMPLATE_BUILDERS[request.scenario]?.(request) ?? templateFromDefinition(request);
}

function unique<T>(items: readonly T[]): T[] {
  return [...new Set(items)];
}

function uniqueVerification(items: readonly Verification[]): Verification[] {
  return items.filter(
    (item, index) => items.findIndex((candidate) => candidate.field === item.field) === index,
  );
}

/**
 * Builds a deterministic, tenant-scoped draft for a human to copy manually.
 * The function has no delivery, persistence, or external-system side effects.
 */
export function createDecisionCard(request: DecisionCardRequest): DecisionCard {
  const definition = scenarioDefinitionFor(request.scenario);
  const template = templateFor(request);
  const style = resolveOperatorStyle(request);
  const reply: ReplyOption = {
    ...template.reply,
    id: `reply-${request.requestId}-1`,
    text: applyOperatorStyle(template.reply.text, request),
  };
  const draft: DecisionCard = {
    contractVersion: "decision-card.v1",
    cardId: `card-${request.requestId}-${request.scenario}`,
    requestId: request.requestId,
    deliveryMode: "copy_only",
    scenario: request.scenario,
    customerIntent: template.customerIntent,
    salesStage: template.salesStage,
    summary: template.summary,
    replyOptions: [reply],
    nextAction: template.nextAction,
    verificationNeeded: template.verificationNeeded ?? [],
    safety: {
      status: "safe",
      rules: ["COPY_ONLY_DELIVERY"],
      reasons: ["仅供人工复制发送，不会自动发送消息。"],
    },
    audit: {
      tenantId: request.tenantId,
      operatorId: request.operatorId,
      evidenceIds: reply.evidenceIds,
    },
  };
  const evaluation = evaluateSafety(request, draft);
  const scenarioVerification =
    definition.alwaysReview && definition.defaultVerificationField !== undefined
      ? [{
          field: definition.defaultVerificationField,
          reason: "该业务场景需要人工核对当前资料和适用规则后，再决定回复内容。",
        }]
      : [];
  const verificationNeeded = [
    ...draft.verificationNeeded,
    ...evaluation.verificationNeeded,
    ...scenarioVerification,
  ];
  const rules = unique([
    ...evaluation.rules,
    ...(template.additionalRules ?? []),
  ]);
  const reasons = unique([
    ...evaluation.reasons,
    ...(template.additionalReasons ?? []),
  ]);
  const reviewRequired =
    evaluation.status === "reviewRequired" || template.additionalRules !== undefined;
  const card: DecisionCard = {
    ...draft,
    verificationNeeded: uniqueVerification(verificationNeeded),
    safety: {
      status: reviewRequired ? "reviewRequired" : "safe",
      rules,
      reasons,
    },
  };

  if (card.safety.status === "safe") {
    return DecisionCardSchema.parse(card);
  }

  const continuedDraft: DecisionCard = {
    ...card,
    replyOptions: [
      {
        ...reply,
        text: buildSafeContinuation(request, card.verificationNeeded, style),
      },
    ],
  };
  const continuationEvaluation = evaluateSafety(request, continuedDraft);
  const continuedVerificationNeeded = uniqueVerification([
    ...card.verificationNeeded,
    ...continuationEvaluation.verificationNeeded,
  ]);
  const continuedRules = unique([
    ...card.safety.rules,
    ...continuationEvaluation.rules,
  ]);
  const continuedReasons = unique([
    ...card.safety.reasons,
    ...continuationEvaluation.reasons,
  ]);
  const continuedReviewRequired =
    continuationEvaluation.status === "reviewRequired" ||
    card.safety.status === "reviewRequired";
  const continuedCard: DecisionCard = {
    ...continuedDraft,
    verificationNeeded: continuedVerificationNeeded,
    reviewFocus: continuedReviewRequired
      ? selectReviewFocus(continuedVerificationNeeded)
      : undefined,
    safety: {
      status: continuedReviewRequired ? "reviewRequired" : "safe",
      rules: continuedRules,
      reasons: continuedReasons,
    },
  };

  return DecisionCardSchema.parse(continuedCard);
}
