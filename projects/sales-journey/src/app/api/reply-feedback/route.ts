import {
  ReplyFeedbackAcceptedResponseSchema,
  ReplyFeedbackRequestSchema,
} from "@/domain/reply-feedback/schema";
import { replyFeedbackRepository } from "@/server/reply-feedback/repository";
import { generatedCardRegistry } from "@/server/decision-card/registry";
import { resolveRequestIdentity } from "@/server/request-identity";
import { isExplicitSyntheticLocalMode } from "@/server/synthetic-local-mode";

const INVALID_REPLY_FEEDBACK = "INVALID_REPLY_FEEDBACK";
const TENANT_SCOPE_MISMATCH = "TENANT_SCOPE_MISMATCH";
const RUNTIME_UNAVAILABLE = "REPLY_FEEDBACK_RUNTIME_UNAVAILABLE";

function invalidFeedback(details: unknown): Response {
  return Response.json({ error: INVALID_REPLY_FEEDBACK, details }, { status: 400 });
}

export async function POST(request: Request): Promise<Response> {
  if (!isExplicitSyntheticLocalMode()) {
    return Response.json({ error: RUNTIME_UNAVAILABLE }, { status: 503 });
  }

  let body: unknown;

  try {
    body = await request.json();
  } catch {
    return invalidFeedback({ formErrors: ["请求内容必须是有效 JSON。"] });
  }

  const parsed = ReplyFeedbackRequestSchema.safeParse(body);
  if (!parsed.success) {
    return invalidFeedback(parsed.error.flatten());
  }

  const identity = resolveRequestIdentity();
  if (
    parsed.data.tenantId !== identity.tenantId ||
    parsed.data.operatorId !== identity.operatorId
  ) {
    return Response.json({ error: TENANT_SCOPE_MISMATCH }, { status: 403 });
  }

  const cardMatch = generatedCardRegistry.matchFeedback(parsed.data);
  if (cardMatch === "unknown") {
    return Response.json({ error: "EXPIRED_DECISION_CARD" }, { status: 410 });
  }
  if (cardMatch === "mismatch") {
    return Response.json({ error: "DECISION_CARD_FEEDBACK_MISMATCH" }, { status: 409 });
  }
  const result = cardMatch === "expired"
    ? await replyFeedbackRepository.replaceQuarantinedWithSafe(parsed.data)
    : await replyFeedbackRepository.saveWithOutcome(parsed.data);
  if (result === undefined) {
    return Response.json({ error: "EXPIRED_DECISION_CARD" }, { status: 410 });
  }

  return Response.json(
    ReplyFeedbackAcceptedResponseSchema.parse({
      accepted: true,
      outcome: result.outcome,
      privacyStatus: result.record.privacyStatus,
      privacyReasonCodes: result.record.privacyReasonCodes,
    }),
    { status: result.outcome === "created" ? 201 : 200 },
  );
}
