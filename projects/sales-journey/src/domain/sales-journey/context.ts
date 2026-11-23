import { DecisionCardRequestSchema } from "@/domain/decision-card/schema";
import { scenarioDefinitionFor } from "@/domain/decision-card/scenarios/catalog";
import type {
  DecisionCardRequest,
  EvidenceRef,
  SalesScenario,
  SalesStage,
} from "@/domain/decision-card/types";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import type { TimelineEntry } from "@/server/supervision/repository";
import { SalesJourneyContextSchema } from "./schema";
import type { SalesJourneyContext } from "./schema";

export type LocalEvidence = EvidenceRef & Readonly<{ tenantId: string }>;

type JourneyDecisionInput = {
  requestId: string;
  operatorId?: string;
  scenario: SalesScenario;
  productDescription: string;
  latestCustomerMessage: string;
  journey: SalesJourneyContext;
  evidence: readonly LocalEvidence[];
};

const RESULT_STAGE: Partial<Record<CustomerRecord["status"], SalesStage>> = {
  interested: "evaluate",
  won: "advance",
  lost: "follow_up",
};

function latestFollowUpFor(
  customer: CustomerRecord,
  timeline: readonly TimelineEntry[],
): Extract<TimelineEntry, { kind: "follow_up" }> | undefined {
  return timeline
    .filter((entry): entry is Extract<TimelineEntry, { kind: "follow_up" }> => (
      entry.kind === "follow_up"
      && entry.record.tenantId === customer.tenantId
      && entry.record.customerId === customer.id
    ))
    .reduce<Extract<TimelineEntry, { kind: "follow_up" }> | undefined>(
      (latest, entry) => !latest
        || entry.record.createdAt > latest.record.createdAt
        || (
          entry.record.createdAt === latest.record.createdAt
          && entry.record.id > latest.record.id
        )
        ? entry
        : latest,
      undefined,
    );
}

function derivedStage(input: {
  customer: CustomerRecord;
  timeline: readonly TimelineEntry[];
  scenario: SalesScenario;
  approvedEvidence: readonly LocalEvidence[];
}): SalesStage {
  const requiredEvidenceField = input.scenario === "identity_bead"
    ? "identity_bead"
    : input.scenario === "scan_service"
      ? "scan_service"
      : undefined;
  if (
    requiredEvidenceField !== undefined
    && !input.approvedEvidence.some(
      (evidence) => evidence.verifiedFields.includes(requiredEvidenceField),
    )
  ) {
    return "discover";
  }

  const latestFollowUp = latestFollowUpFor(input.customer, input.timeline);
  const trustedProgress = (latestFollowUp && RESULT_STAGE[latestFollowUp.record.result])
    ?? RESULT_STAGE[input.customer.status];
  if (trustedProgress !== undefined) return trustedProgress;

  return scenarioDefinitionFor(input.scenario).salesStage;
}

/**
 * Builds the minimum privacy-safe journey needed by reply generation.
 * Historical records influence stage/task presence only; their text and private
 * identifiers never enter the snapshot.
 */
export function buildSupervisedSalesJourney(input: {
  customer: CustomerRecord;
  timeline: readonly TimelineEntry[];
  scenario: SalesScenario;
  latestCustomerMessage: string;
  recentContext?: string;
  approvedEvidence: readonly LocalEvidence[];
}): SalesJourneyContext {
  for (const evidence of input.approvedEvidence) {
    if (evidence.tenantId !== input.customer.tenantId) {
      throw new Error(`Evidence ${evidence.id} belongs to another tenant`);
    }
    if (evidence.status !== "approved") {
      throw new Error(`Evidence ${evidence.id} is not approved`);
    }
  }
  if (new Set(input.approvedEvidence.map((evidence) => evidence.id)).size
      !== input.approvedEvidence.length) {
    throw new Error("Approved evidence ids must be unique");
  }

  const definition = scenarioDefinitionFor(input.scenario);
  const latestFollowUp = latestFollowUpFor(input.customer, input.timeline);
  const pendingQuestions = [
    ...definition.requiredInputs,
    ...(input.recentContext?.trim() ? ["本次补充背景待人工确认"] : []),
  ];

  return SalesJourneyContextSchema.parse({
    tenantId: input.customer.tenantId,
    journeyId: `journey-${input.customer.id}`,
    customerId: input.customer.id,
    subjectId: `category-${input.customer.category}`,
    scenario: input.scenario,
    approvedEvidenceIds: input.approvedEvidence.map((evidence) => evidence.id),
    currentStage: derivedStage(input),
    customerCategory: input.customer.category,
    confirmedNeeds: [],
    pendingQuestions,
    commitments: [],
    openTasks: latestFollowUp && latestFollowUp.record.result !== "won"
      ? ["历史跟进中有待办事项"]
      : [],
    recentConversation: [{
      speaker: "customer",
      text: input.latestCustomerMessage,
    }],
  });
}

export function createJourneyDecisionRequest(
  input: JourneyDecisionInput,
): DecisionCardRequest {
  for (const evidence of input.evidence) {
    if (evidence.tenantId !== input.journey.tenantId) {
      throw new Error(`Evidence ${evidence.id} belongs to another tenant`);
    }
    if (evidence.status !== "approved") {
      throw new Error(`Evidence ${evidence.id} is not approved`);
    }
  }

  const operatorId = input.operatorId ?? input.journey.operatorId;
  if (operatorId === undefined) {
    throw new Error("A server-authenticated operator is required");
  }
  if (input.journey.scenario !== undefined && input.journey.scenario !== input.scenario) {
    throw new Error("Journey scenario does not match decision scenario");
  }
  if (
    input.journey.approvedEvidenceIds !== undefined
    && (
      input.journey.approvedEvidenceIds.length !== input.evidence.length
      || input.journey.approvedEvidenceIds.some(
        (id, index) => id !== input.evidence[index]?.id,
      )
    )
  ) {
    throw new Error("Journey evidence does not match approved evidence order");
  }

  return DecisionCardRequestSchema.parse({
    contractVersion: "decision-card.v1",
    requestId: input.requestId,
    tenantId: input.journey.tenantId,
    operatorId,
    scenario: input.scenario,
    productDescription: [
      input.productDescription,
      `旅程编号：${input.journey.journeyId}`,
      `客户编号：${input.journey.customerId}`,
      `当前商品/服务：${input.journey.subjectId}`,
      `当前旅程阶段：${input.journey.currentStage}`,
      `客户分类：${input.journey.customerCategory}`,
      `已确认需求：${input.journey.confirmedNeeds.join("、") || "无"}`,
      `待确认：${input.journey.pendingQuestions.join("、") || "无"}`,
      `承诺事项：${input.journey.commitments.join("、") || "无"}`,
      `待办事项：${input.journey.openTasks.join("、") || "无"}`,
    ].join("\n"),
    latestCustomerMessage: input.latestCustomerMessage,
    conversation: input.journey.recentConversation,
    operatorStyle: { tone: "warm", maxReplyCharacters: 180 },
    evidence: input.evidence,
  });
}
