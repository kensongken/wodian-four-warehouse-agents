import { createDecisionCard } from "@/domain/decision-card/advisor";
import {
  DecisionCardRequestSchema,
  DecisionCardSchema,
} from "@/domain/decision-card/schema";
import {
  buildSupervisedSalesJourney,
  createJourneyDecisionRequest,
} from "@/domain/sales-journey/context";
import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  evidenceTitlesFor,
  generatedCardRegistry,
  REPLY_AGENT_VERSION,
  resolveApprovedEvidence,
} from "@/server/decision-card/registry";
import {
  createCustomerService,
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { z } from "zod";
import { getProductStrategyRuntime } from "@/server/product-strategy/runtime";
import {
  productStrategyDecisionSummary,
  productStrategyEvidence,
} from "@/server/product-strategy/evidence";
import type { ProductStrategyReceipt } from "@/server/product-strategy/repository";

const INVALID_REQUEST_ERROR = "INVALID_DECISION_CARD_REQUEST";
const DecisionCardRouteRequestSchema = DecisionCardRequestSchema.omit({
  tenantId: true,
  operatorId: true,
  productDescription: true,
}).extend({
  customerId: z.string().trim().min(1).max(120),
  recentContext: z.string().trim().min(1).max(8_000).optional(),
  productDescription: z.string().trim().min(1).max(8_000).optional(),
  productCode: z.string().min(1).max(64).regex(/^[A-Za-z0-9_-]+$/u).optional(),
}).superRefine((input, context) => {
  if (!input.productCode && !input.productDescription) {
    context.addIssue({
      code: "custom",
      path: ["productDescription"],
      message: "Product code or product description is required.",
    });
  }
});

function invalidRequest(details: unknown): Response {
  return Response.json(
    {
      error: INVALID_REQUEST_ERROR,
      details,
    },
    { status: 400 },
  );
}

/**
 * Creates a decision card for manual copy only and returns a short-lived,
 * server-signed token that can later attach the employee's usage to a follow-up.
 */
export async function POST(request: Request): Promise<Response> {
  let runtime;
  let actor;
  try {
    runtime = getSupervisionApiRuntime();
    actor = await requireEmployeeFromD1(request.headers, runtime.d1);
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }

  let body: unknown;

  try {
    body = await request.json();
  } catch {
    return invalidRequest({ formErrors: ["请求内容必须是有效 JSON。"] });
  }

  const parsed = DecisionCardRouteRequestSchema.safeParse(body);
  if (!parsed.success) {
    return invalidRequest(parsed.error.flatten());
  }

  const approvedEvidence = resolveApprovedEvidence(
    actor.tenantId,
    parsed.data.evidence.map((item) => item.id),
  );
  if (approvedEvidence === undefined) {
    return Response.json({ error: "UNAPPROVED_EVIDENCE" }, { status: 400 });
  }

  const evidence = [...approvedEvidence];
  let productDescription = parsed.data.productDescription ?? "";
  let productDecisionSummary: string | undefined;
  let productStrategyReceipt: ProductStrategyReceipt | undefined;
  if (parsed.data.productCode) {
    let snapshot;
    try {
      snapshot = await getProductStrategyRuntime().repository.findLatest(
        actor.tenantId,
        parsed.data.productCode,
      );
    } catch {
      return Response.json({ error: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE" }, { status: 503 });
    }
    if (!snapshot) {
      return Response.json({ error: "PRODUCT_STRATEGY_INPUT_NOT_FOUND" }, { status: 404 });
    }
    productStrategyReceipt = snapshot;
    const snapshotEvidence = productStrategyEvidence(snapshot);
    productDecisionSummary = productStrategyDecisionSummary(snapshot);
    evidence.push(snapshotEvidence);
    // When a product code is supplied, the server snapshot replaces browser-authored
    // product facts. Candidate wording keeps the advisor in human-review mode.
    productDescription = snapshotEvidence.title;
  }

  try {
    const service = createCustomerService(runtime);
    const { customer, timeline } = await service.getCustomerTimeline(
      actor,
      parsed.data.customerId,
    );
    const tenantEvidence = evidence.map((item) => ({
      ...item,
      tenantId: actor.tenantId,
    }));
    const journey = buildSupervisedSalesJourney({
      customer,
      timeline,
      scenario: parsed.data.scenario,
      latestCustomerMessage: parsed.data.latestCustomerMessage,
      recentContext: parsed.data.recentContext,
      approvedEvidence: tenantEvidence,
    });
    const serverRequestId = crypto.randomUUID();
    const generatedCard = createDecisionCard(createJourneyDecisionRequest({
      requestId: serverRequestId,
      operatorId: actor.accountUserId,
      scenario: parsed.data.scenario,
      productDescription,
      latestCustomerMessage: parsed.data.latestCustomerMessage,
      journey,
      evidence: tenantEvidence,
    }));
    const productStrategySeal = productStrategyReceipt && {
      productCode: productStrategyReceipt.productCode,
      strategyInputVersion: productStrategyReceipt.strategyInputVersion,
      contentSha256: productStrategyReceipt.contentSha256,
      status: productStrategyReceipt.status,
    };
    const card = DecisionCardSchema.parse({
      ...generatedCard,
      summary: productDecisionSummary
        ? `${generatedCard.summary}\n${productDecisionSummary}`
        : generatedCard.summary,
      cardId: `card-${crypto.randomUUID()}`,
      requestId: `request-${serverRequestId}`,
      salesStage: journey.currentStage,
      replyOptions: generatedCard.replyOptions.map((reply) => ({
        ...reply,
        id: `reply-${crypto.randomUUID()}`,
      })),
      productStrategySeal,
    });
    generatedCardRegistry.register(card, REPLY_AGENT_VERSION);

    const usage = await service.issueDecisionUsageToken(
      actor,
      parsed.data.customerId,
      card,
      {
        scenario: journey.scenario ?? parsed.data.scenario,
        salesStage: journey.currentStage,
        evidenceIds: journey.approvedEvidenceIds ?? [],
      },
      productStrategySeal,
    );
    return Response.json({
      card,
      usageToken: usage.token,
      usageTokenExpiresAt: usage.expiresAt,
      cardInstanceId: usage.cardInstanceId,
      cardDigest: usage.cardDigest,
      evidenceTitles: evidenceTitlesFor(evidence),
    });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}
