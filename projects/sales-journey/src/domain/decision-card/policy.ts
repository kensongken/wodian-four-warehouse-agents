import type {
  DecisionCard,
  DecisionCardRequest,
  SafetyRuleId,
} from "./types";
import { scenarioDefinitionFor } from "./scenarios/catalog";
import type { VerificationField } from "./types";

type VerificationNeeded = DecisionCard["verificationNeeded"][number];

export type SafetyEvaluation = {
  status: "safe" | "reviewRequired";
  rules: SafetyRuleId[];
  reasons: string[];
  verificationNeeded: VerificationNeeded[];
  nextAction?: DecisionCard["nextAction"];
};

type FactCheck = {
  field: Exclude<VerificationNeeded["field"], "reuse_or_return_terms">;
  pattern: RegExp;
  evidenceFields?: DecisionCardRequest["evidence"][number]["verifiedFields"][number][];
  rule: SafetyRuleId;
  reason: string;
};

type CustomerQuestionCheck = Pick<FactCheck, "field" | "pattern" | "rule"> & {
  reason: string;
};

const FACT_CHECKS: FactCheck[] = [
  {
    field: "material",
    pattern: /材质|天然|沉香|绿松(?:石)?|南红|和田玉|翡翠|彩宝/i,
    evidenceFields: ["material"],
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "材质说法必须引用已批准且已核验的资料。",
  },
  {
    field: "quality",
    pattern: /品质|等级|高瓷|无瑕|顶级|完美/i,
    evidenceFields: ["quality"],
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "品质说法必须引用已批准且已核验的资料。",
  },
  {
    field: "origin",
    pattern: /产地|产自|产于|原产|来自|哪(?:里|儿)产|保山|缅甸|新疆|云南/i,
    evidenceFields: ["origin"],
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "产地说法必须引用已批准且已核验的资料。",
  },
  {
    field: "certificate",
    pattern: /证书|鉴定(?:证书)?|国检/i,
    evidenceFields: ["certificate"],
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "证书说法必须引用已批准且已核验的资料。",
  },
  {
    field: "service_policy",
    pattern: /扫码.{0,16}(?:证明|鉴定|判断)?.{0,8}(?:真伪|正品|真货)|(?:真伪|正品|真货).{0,16}扫码/i,
    rule: "SERVICE_POLICY_VERIFICATION",
    reason: "扫码不能作为真伪或品质的唯一判断依据，必须人工核对。",
  },
  {
    field: "service_policy",
    pattern: /官方服务|售后|终身服务|养护服务|改款服务|服务资格|服务范围|(?:七天|7天|七日|7日)无理由退货|支持.{0,12}(?:退货|换货|维修|养护|改款|服务)/i,
    evidenceFields: ["scan_service", "reuse_or_return_process"],
    rule: "SERVICE_POLICY_VERIFICATION",
    reason: "服务说法必须引用已批准的服务规则。",
  },
  {
    field: "service_policy",
    pattern: /平台合作|平台认证|平台背书|官方(?:认证|背书)|与(?:京东|淘宝|天猫|抖音|小红书|微信|闲鱼)合作|(?:京东|淘宝|天猫|抖音|小红书|微信|闲鱼)(?:官方店|旗舰店|认证|背书)/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "平台或官方背书不属于 v1 可自动确认的事实，必须人工复核。",
  },
  {
    field: "certificate",
    pattern: /保真|真伪|正品|真货|假一赔/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "真伪不能由系统自动承诺，必须转人工核验。",
  },
];

const CUSTOMER_QUESTION_CHECKS: CustomerQuestionCheck[] = [
  {
    field: "material",
    pattern: /材质|天然|什么料|是什么料|原矿|优化处理|注胶|染色|烧过|烧色/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "客户正在询问材质，需要人工核验可引用资料。",
  },
  {
    field: "quality",
    pattern: /品质|等级|高瓷|无瑕|顶级|完美/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "客户正在询问品质，需要人工核验可引用资料。",
  },
  {
    field: "origin",
    pattern: /产地|产自|产于|原产|来自|哪(?:里|儿)产|保山|缅甸|新疆|云南/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "客户正在询问产地，需要人工核验可引用资料。",
  },
  {
    field: "certificate",
    pattern: /证书|鉴定(?:证书)?|国检|保真|真伪|正品|真货|假一赔|假货|a\s*货/i,
    rule: "EVIDENCE_REQUIRED_FOR_FACT",
    reason: "客户正在询问真伪或证书，需要人工核验可引用资料。",
  },
  {
    field: "service_policy",
    pattern: /包退|包换|无理由退货|保修|终身服务|售后期限|(?:支持|可以|能).{0,6}(?:退货|换货)/i,
    rule: "SERVICE_POLICY_VERIFICATION",
    reason: "客户正在询问退换或售后规则，需要人工核验适用服务规则。",
  },
  {
    field: "customization_feasibility",
    pattern: /(?:定制|改款|重新镶嵌).{0,12}(?:保证|可行|完成|效果)|保证.{0,12}(?:定制|改款|重新镶嵌|效果)/i,
    rule: "CUSTOMIZATION_FEASIBILITY_VERIFICATION",
    reason: "客户正在询问定制、改款或重新镶嵌的可行性或结果，需要人工核验。",
  },
];

const PRICE_OR_VALUE_PATTERN = /多少钱|报价|价格|固定(?:价|价格)?|保底(?:价|价格)?|最低(?:多少|价)?|一口价|抵扣|折抵|折价|回购|收购|置换|(?:还能|能|会)收(?:吗|不|购)?|\d+(?:\.\d+)?\s*(?:元|块|rmb|人民币|%|％)|[¥￥]/i;
const VALUE_PRESERVATION_PATTERN = /保值|升值|增值|保本|稳赚|价值保证|还能涨|会涨|上涨/i;
const REUSE_OR_RETURN_PROHIBITED_PATTERN =
  /回购|收购|置换|固定(?:价|价格)?|保底(?:价|价格)?|保证(?:处理|成交|通过)|必然(?:处理|成交)|平台(?:撮合|匹配)|立即(?:付款|到账)|当天到账|即时到账|马上到账|马上付款|百分之|\d+(?:\.\d+)?\s*(?:元|块|rmb|人民币|%|％)|[¥￥]/i;
const CUSTOMER_CATEGORY_CONTEXT_PATTERN = /就您提到的(?:沉香|绿松石|南红|和田玉|彩宝|翡翠)，?/g;
const SCENARIO_REVIEW_RULE: Record<VerificationField, SafetyRuleId> = {
  material: "EVIDENCE_REQUIRED_FOR_FACT",
  quality: "EVIDENCE_REQUIRED_FOR_FACT",
  origin: "EVIDENCE_REQUIRED_FOR_FACT",
  certificate: "EVIDENCE_REQUIRED_FOR_FACT",
  service_policy: "SERVICE_POLICY_VERIFICATION",
  reuse_or_return_terms: "REUSE_OR_RETURN_HUMAN_REVIEW",
  price_or_payment: "PRICE_OR_PAYMENT_HUMAN_REVIEW",
  inventory_or_timeline: "ORDER_STATUS_VERIFICATION",
  order_or_delivery: "ORDER_STATUS_VERIFICATION",
  customization_feasibility: "CUSTOMIZATION_FEASIBILITY_VERIFICATION",
  condition_or_defect: "DEFECT_OR_DISPUTE_HUMAN_REVIEW",
  after_sales_policy: "AFTER_SALES_POLICY_VERIFICATION",
  consignment_terms: "CONSIGNMENT_HUMAN_REVIEW",
};

function includesAny(text: string, pattern: RegExp): boolean {
  pattern.lastIndex = 0;
  return pattern.test(text);
}

function withoutCustomerCategoryContext(text: string): string {
  return text.replace(CUSTOMER_CATEGORY_CONTEXT_PATTERN, "");
}

function hasApprovedReferencedEvidence(
  request: DecisionCardRequest,
  evidenceIds: string[],
  verifiedFields: NonNullable<FactCheck["evidenceFields"]>,
): boolean {
  return evidenceIds.some((evidenceId) =>
    request.evidence.some(
      (evidence) =>
        evidence.id === evidenceId &&
        evidence.status === "approved" &&
        verifiedFields.some((field) => evidence.verifiedFields.includes(field)),
    ),
  );
}

/**
 * Applies deterministic guardrails before a decision card can be shown for manual copying.
 * It does not send, modify, or persist a message.
 */
export function evaluateSafety(
  request: DecisionCardRequest,
  card: DecisionCard,
): SafetyEvaluation {
  const rules: SafetyRuleId[] = ["COPY_ONLY_DELIVERY"];
  const reasons = ["仅供人工复制发送，不会自动发送消息。"];
  const verificationNeeded: VerificationNeeded[] = [];
  let reviewRequired = false;

  const addRule = (rule: SafetyRuleId, reason: string): void => {
    if (!rules.includes(rule)) {
      rules.push(rule);
    }
    if (!reasons.includes(reason)) {
      reasons.push(reason);
    }
    reviewRequired = true;
  };

  const addVerification = (field: VerificationNeeded["field"], reason: string): void => {
    if (!verificationNeeded.some((item) => item.field === field)) {
      verificationNeeded.push({ field, reason });
    }
  };

  if (card.deliveryMode !== "copy_only") {
    addRule("COPY_ONLY_DELIVERY", "发现非人工复制的发送方式，必须停止并人工处理。");
    addVerification("service_policy", "请确认消息仅由人工复制发送。");
  }

  const scenario = scenarioDefinitionFor(request.scenario);
  if (scenario.alwaysReview) {
    const field = scenario.defaultVerificationField ?? "service_policy";
    const reason = field === "reuse_or_return_terms"
      ? "请人工确认送回资料、仓验结果及适用服务规则。"
      : `${scenario.label}场景按业务规则必须由人工核验：${scenario.nextAction.text}。`;
    addRule(SCENARIO_REVIEW_RULE[field], reason);
    addVerification(field, reason);
  }

  if (request.scenario === "controlled_reuse_or_return") {
    addRule(
      "REUSE_OR_RETURN_NO_FIXED_PRICE",
      "送回、仓验、养护、改款或受控再利用不得承诺价格、置换或处理结果。",
    );
    addRule(
      "REUSE_OR_RETURN_HUMAN_REVIEW",
      "受控再利用或送回须由人工依据仓验结果和服务规则确认。",
    );
    addVerification(
      "reuse_or_return_terms",
      "请人工确认送回资料、仓验结果及适用服务规则。",
    );
  }

  const customerQuestionText = withoutCustomerCategoryContext([
    request.latestCustomerMessage,
    ...request.conversation
      .filter((turn) => turn.speaker === "customer")
      .map((turn) => turn.text),
  ].join("\n"));
  for (const check of CUSTOMER_QUESTION_CHECKS) {
    if (!includesAny(customerQuestionText, check.pattern)) {
      continue;
    }

    addRule(check.rule, check.reason);
    addVerification(check.field, check.reason);
  }

  for (const reply of card.replyOptions) {
    const claimText = withoutCustomerCategoryContext(reply.text);
    for (const check of FACT_CHECKS) {
      if (!includesAny(claimText, check.pattern)) {
        continue;
      }

      const supported =
        check.evidenceFields !== undefined &&
        hasApprovedReferencedEvidence(request, reply.evidenceIds, check.evidenceFields);
      if (!supported) {
        addRule(check.rule, check.reason);
        addVerification(check.field, check.reason);
      }
    }
  }

  const visibleText = card.replyOptions.map((reply) => reply.text).join("\n");
  const priceOrValueRequested =
    includesAny(customerQuestionText, PRICE_OR_VALUE_PATTERN) ||
    includesAny(visibleText, PRICE_OR_VALUE_PATTERN) ||
    includesAny(customerQuestionText, VALUE_PRESERVATION_PATTERN) ||
    includesAny(visibleText, VALUE_PRESERVATION_PATTERN);

  if (priceOrValueRequested) {
    addRule(
      "EVIDENCE_REQUIRED_FOR_FACT",
      "报价、固定价格、保值或回购相关内容不能由系统自动答复，必须人工确认。",
    );
    addVerification(
      "service_policy",
      "请人工确认当前适用规则后，再回复价格或价值问题。",
    );
  }

  if (
    request.scenario === "controlled_reuse_or_return" &&
    includesAny(visibleText, REUSE_OR_RETURN_PROHIBITED_PATTERN)
  ) {
    addRule(
      "REUSE_OR_RETURN_NO_FIXED_PRICE",
      "检测到回购、置换、固定价、保证处理、平台撮合或即时付款等禁说项。",
    );
    addVerification(
      "reuse_or_return_terms",
      "请删除禁说项，改为送回、仓验、人工确认和服务规则另行决定的说明。",
    );
  }

  return {
    status: reviewRequired ? "reviewRequired" : "safe",
    rules,
    reasons,
    verificationNeeded,
    nextAction: reviewRequired
      ? {
          kind: "handoff_to_operator",
          text: "请人工核验所需资料和服务规则后，再决定回复内容。",
        }
      : undefined,
  };
}
