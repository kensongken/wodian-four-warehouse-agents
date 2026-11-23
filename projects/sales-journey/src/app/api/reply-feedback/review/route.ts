import { timingSafeEqual } from "node:crypto";
import type { StoredReplyFeedback } from "@/domain/reply-feedback/schema";
import { replyFeedbackRepository } from "@/server/reply-feedback/repository";
import { resolveRequestIdentity } from "@/server/request-identity";
import { isExplicitSyntheticLocalMode } from "@/server/synthetic-local-mode";

type FeedbackGroup = {
  scenario: StoredReplyFeedback["scenario"];
  salesStage: StoredReplyFeedback["salesStage"];
  agentVersion: string;
  rating: StoredReplyFeedback["rating"];
  reason: StoredReplyFeedback["reasons"][number] | null;
  count: number;
  finalReplyExamples: string[];
};

function hasReviewerAccess(request: Request): boolean {
  const configuredToken = process.env.REPLY_FEEDBACK_REVIEW_TOKEN;
  const authorization = request.headers.get("authorization");
  if (configuredToken === undefined || configuredToken.trim() === "" || authorization === null) {
    return false;
  }

  const match = /^Bearer (.+)$/.exec(authorization);
  if (match === null) {
    return false;
  }

  const configured = Buffer.from(configuredToken);
  const presented = Buffer.from(match[1]);
  return configured.length === presented.length && timingSafeEqual(configured, presented);
}

function groupFeedback(records: StoredReplyFeedback[]): FeedbackGroup[] {
  const groups = new Map<string, FeedbackGroup>();

  for (const record of records) {
    const finalReply = record.privacyStatus === "safe" ? record.finalReply : undefined;
    const reasons = record.reasons.length === 0 ? [null] : record.reasons;
    for (const reason of reasons) {
      const key = [record.scenario, record.salesStage, record.agentVersion, record.rating, reason ?? ""].join("\u0000");
      const existing = groups.get(key);
      if (existing !== undefined) {
        existing.count += 1;
        if (
          finalReply !== undefined &&
          !existing.finalReplyExamples.includes(finalReply)
        ) {
          existing.finalReplyExamples.push(finalReply);
        }
        continue;
      }

      groups.set(key, {
        scenario: record.scenario,
        salesStage: record.salesStage,
        agentVersion: record.agentVersion,
        rating: record.rating,
        reason,
        count: 1,
        finalReplyExamples: finalReply === undefined ? [] : [finalReply],
      });
    }
  }

  return [...groups.values()].sort((left, right) =>
    [left.scenario, left.salesStage, left.agentVersion, left.rating, left.reason ?? ""].join("\u0000").localeCompare(
      [right.scenario, right.salesStage, right.agentVersion, right.rating, right.reason ?? ""].join("\u0000"),
    ),
  );
}

export async function GET(request: Request): Promise<Response> {
  if (!isExplicitSyntheticLocalMode()) {
    return Response.json(
      { error: "REPLY_FEEDBACK_RUNTIME_UNAVAILABLE" },
      { status: 503 },
    );
  }

  if (!hasReviewerAccess(request)) {
    return Response.json({ error: "REVIEWER_ACCESS_DENIED" }, { status: 403 });
  }

  const identity = resolveRequestIdentity();
  return Response.json({
    groups: groupFeedback(await replyFeedbackRepository.listForTenant(identity.tenantId)),
  });
}
