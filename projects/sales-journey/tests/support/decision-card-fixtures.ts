import type {
  DecisionCard,
  DecisionCardRequest,
  EvidenceRef,
  SalesScenario,
} from "@/domain/decision-card/types";
import { scenarioLabelFor } from "@/domain/decision-card/scenarios/catalog";

const approvedEvidence: EvidenceRef = {
  id: "evidence-identity-1",
  kind: "product_record",
  title: "已批准的身份珠说明",
  verifiedFields: ["identity_bead"],
  status: "approved",
};

export const identityBeadRequest: DecisionCardRequest = {
  contractVersion: "decision-card.v1",
  requestId: "req-identity-1",
  tenantId: "tenant-a",
  operatorId: "operator-a",
  scenario: "identity_bead",
  productDescription: "身份珠",
  latestCustomerMessage: "这个身份珠有什么用？",
  conversation: [],
  evidence: [approvedEvidence],
};

export const scanServiceRequest: DecisionCardRequest = {
  ...identityBeadRequest,
  requestId: "req-scan-1",
  scenario: "scan_service",
  productDescription: "身份珠扫码服务",
  latestCustomerMessage: "扫码后可以看什么服务？",
  evidence: [
    {
      id: "evidence-scan-1",
      kind: "service_policy",
      title: "已批准的扫码服务说明",
      verifiedFields: ["scan_service"],
      status: "approved",
    },
  ],
};

export const swapRequest: DecisionCardRequest = {
  ...identityBeadRequest,
  requestId: "req-redesign-1",
  scenario: "swap_or_redesign",
  productDescription: "旧手串改款",
  latestCustomerMessage: "我想把旧手串改成新款。",
  evidence: [
    {
      id: "evidence-redesign-1",
      kind: "service_policy",
      title: "已批准的改款流程说明",
      verifiedFields: ["reuse_or_return_process"],
      status: "approved",
    },
  ],
};

export const controlledReuseOrReturnRequest: DecisionCardRequest = {
  ...identityBeadRequest,
  requestId: "req-reuse-1",
  scenario: "controlled_reuse_or_return",
  productDescription: "旧手串送回服务",
  latestCustomerMessage: "旧料送回后怎么处理？",
  evidence: [
    {
      id: "evidence-reuse-1",
      kind: "service_policy",
      title: "已批准的送回与受控再利用流程",
      verifiedFields: ["reuse_or_return_process"],
      status: "approved",
    },
  ],
};

export const safeCard: DecisionCard = {
  contractVersion: "decision-card.v1",
  cardId: "card-safe-1",
  requestId: "req-identity-1",
  deliveryMode: "copy_only",
  scenario: "identity_bead",
  customerIntent: "learn_identity",
  salesStage: "explain",
  summary: "根据已批准资料说明身份珠服务。",
  replyOptions: [
    {
      id: "reply-1",
      strategy: "explain",
      text: "我先按已核验资料为您说明身份珠的服务路径。",
      evidenceIds: ["evidence-identity-1"],
    },
  ],
  nextAction: {
    kind: "ask_clarifying_question",
    text: "请问您更关心辨识方式、佩戴效果，还是来源资料？",
  },
  verificationNeeded: [],
  safety: {
    status: "safe",
    rules: ["COPY_ONLY_DELIVERY"],
    reasons: ["仅供人工复制发送。"],
  },
  audit: {
    tenantId: "tenant-a",
    operatorId: "operator-a",
    evidenceIds: ["evidence-identity-1"],
  },
};

export function cardWithReply(text: string): DecisionCard {
  return {
    ...safeCard,
    replyOptions: [{ ...safeCard.replyOptions[0], text }],
  };
}

export function requestFor(scenario: SalesScenario): DecisionCardRequest {
  const requests: Partial<Record<SalesScenario, DecisionCardRequest>> = {
    identity_bead: identityBeadRequest,
    scan_service: scanServiceRequest,
    swap_or_redesign: swapRequest,
    controlled_reuse_or_return: controlledReuseOrReturnRequest,
  };

  return requests[scenario] ?? {
    contractVersion: "decision-card.v1",
    requestId: `req-${scenario}`,
    tenantId: "tenant-a",
    operatorId: "operator-a",
    scenario,
    productDescription: `${scenarioLabelFor(scenario)}咨询`,
    latestCustomerMessage: `请协助了解${scenarioLabelFor(scenario)}的下一步。`,
    conversation: [],
    evidence: [],
  };
}
