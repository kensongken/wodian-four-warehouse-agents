import { randomUUID } from "node:crypto";
import { sanitizeFeedbackFinalReply } from "@/domain/reply-feedback/redaction";
import {
  ReplyFeedbackRequestSchema,
  StoredReplyFeedbackSchema,
  type ReplyFeedbackRequest,
  type StoredReplyFeedback,
} from "@/domain/reply-feedback/schema";

export interface ReplyFeedbackRepository {
  save(input: ReplyFeedbackRequest): Promise<StoredReplyFeedback>;
  saveWithOutcome(input: ReplyFeedbackRequest): Promise<SaveReplyFeedbackResult>;
  replaceQuarantinedWithSafe(
    input: ReplyFeedbackRequest,
  ): Promise<SaveReplyFeedbackResult | undefined>;
  listForTenant(tenantId: string): Promise<StoredReplyFeedback[]>;
}

export type SaveReplyFeedbackResult = {
  record: StoredReplyFeedback;
  outcome: "created" | "updated" | "unchanged";
};

function feedbackKey(input: Pick<
  ReplyFeedbackRequest,
  "tenantId" | "operatorId" | "cardId" | "agentVersion"
>): string {
  return [input.tenantId, input.operatorId, input.cardId, input.agentVersion].join("\u0000");
}

function evaluationContent(record: StoredReplyFeedback): string {
  return JSON.stringify({
    rating: record.rating,
    reasons: record.reasons,
    privacyStatus: record.privacyStatus,
    privacyReasonCodes: record.privacyReasonCodes,
    finalReply: record.privacyStatus === "safe" ? record.finalReply : undefined,
    scenario: record.scenario,
    salesStage: record.salesStage,
    evidenceIds: record.evidenceIds,
  });
}

function snapshotMatches(
  record: StoredReplyFeedback,
  request: ReplyFeedbackRequest,
): boolean {
  return record.scenario === request.scenario
    && record.salesStage === request.salesStage
    && record.evidenceIds.length === request.evidenceIds.length
    && record.evidenceIds.every((id, index) => id === request.evidenceIds[index]);
}

export class InMemoryReplyFeedbackRepository implements ReplyFeedbackRepository {
  readonly #records: StoredReplyFeedback[] = [];

  async save(input: ReplyFeedbackRequest): Promise<StoredReplyFeedback> {
    return (await this.saveWithOutcome(input)).record;
  }

  async saveWithOutcome(input: ReplyFeedbackRequest): Promise<SaveReplyFeedbackResult> {
    const request = ReplyFeedbackRequestSchema.parse(input);
    const existingIndex = this.#records.findIndex(
      (record) => feedbackKey(record) === feedbackKey(request),
    );
    const existing = this.#records[existingIndex];
    const { finalReply, ...metadata } = request;
    const privacyResult = finalReply === undefined
      ? { privacyStatus: "safe" as const, privacyReasonCodes: [] as [] }
      : sanitizeFeedbackFinalReply(finalReply);
    const storedReply = privacyResult.privacyStatus === "safe"
      ? {
        ...metadata,
        privacyStatus: privacyResult.privacyStatus,
        privacyReasonCodes: privacyResult.privacyReasonCodes,
        ...("sanitizedFinalReply" in privacyResult && privacyResult.sanitizedFinalReply !== ""
          ? { finalReply: privacyResult.sanitizedFinalReply }
          : {}),
      }
      : {
        ...metadata,
        privacyStatus: privacyResult.privacyStatus,
        privacyReasonCodes: privacyResult.privacyReasonCodes,
      };
    const record = StoredReplyFeedbackSchema.parse({
      ...storedReply,
      feedbackId: existing?.feedbackId ?? randomUUID(),
      createdAt: existing?.createdAt ?? new Date().toISOString(),
    });

    if (existing === undefined) {
      this.#records.push(record);
      return { record: StoredReplyFeedbackSchema.parse(record), outcome: "created" };
    }
    if (evaluationContent(existing) === evaluationContent(record)) {
      return { record: StoredReplyFeedbackSchema.parse(existing), outcome: "unchanged" };
    }

    this.#records[existingIndex] = record;
    return { record: StoredReplyFeedbackSchema.parse(record), outcome: "updated" };
  }

  async replaceQuarantinedWithSafe(
    input: ReplyFeedbackRequest,
  ): Promise<SaveReplyFeedbackResult | undefined> {
    const request = ReplyFeedbackRequestSchema.parse(input);
    const existing = this.#records.find(
      (record) => feedbackKey(record) === feedbackKey(request),
    );
    if (
      existing?.privacyStatus !== "quarantined"
      || !snapshotMatches(existing, request)
    ) {
      return undefined;
    }

    if (
      request.finalReply !== undefined
      && sanitizeFeedbackFinalReply(request.finalReply).privacyStatus !== "safe"
    ) {
      return undefined;
    }

    return this.saveWithOutcome(request);
  }

  async listForTenant(tenantId: string): Promise<StoredReplyFeedback[]> {
    return this.#records
      .filter((record) => record.tenantId === tenantId)
      .map((record) => StoredReplyFeedbackSchema.parse(record));
  }
}

const repositoryScope = globalThis as typeof globalThis & {
  __salesReplyFeedbackRepository?: InMemoryReplyFeedbackRepository;
};

export const replyFeedbackRepository =
  repositoryScope.__salesReplyFeedbackRepository ??= new InMemoryReplyFeedbackRepository();
