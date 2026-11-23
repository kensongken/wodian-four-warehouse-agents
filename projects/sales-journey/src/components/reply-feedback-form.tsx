"use client";

import { useEffect, useRef, useState } from "react";

import type { DecisionCard } from "@/domain/decision-card/types";
import {
  ReplyFeedbackAcceptedResponseSchema,
  type ReplyFeedbackRequest,
} from "@/domain/reply-feedback/schema";

type ReplyFeedbackFormProps = {
  card: DecisionCard;
  agentVersion: string;
};

type FeedbackReason = ReplyFeedbackRequest["reasons"][number];
type FeedbackSubmission = Pick<ReplyFeedbackRequest, "rating" | "reasons" | "finalReply">;
type FeedbackIdentity = Pick<
  ReplyFeedbackRequest,
  "tenantId" | "operatorId" | "cardId" | "agentVersion"
>;

const IMPROVEMENT_REASONS: ReadonlyArray<{ value: FeedbackReason; label: string }> = [
  { value: "too_stiff", label: "太生硬" },
  { value: "too_long", label: "太长" },
  { value: "wrong_stage", label: "阶段不符" },
  { value: "fact_inaccurate", label: "事实不准确" },
  { value: "no_next_step", label: "没有推进下一步" },
  { value: "other", label: "其他" },
];

function feedbackIdentityKey(identity: FeedbackIdentity): string {
  return JSON.stringify([
    identity.tenantId,
    identity.operatorId,
    identity.cardId,
    identity.agentVersion,
  ]);
}

export function ReplyFeedbackForm({ card, agentVersion }: ReplyFeedbackFormProps) {
  const feedbackIdentity = feedbackIdentityKey({
    tenantId: card.audit.tenantId,
    operatorId: card.audit.operatorId,
    cardId: card.cardId,
    agentVersion,
  });
  const [isImprovementFormOpen, setIsImprovementFormOpen] = useState(false);
  const [reasons, setReasons] = useState<FeedbackReason[]>([]);
  const [finalReply, setFinalReply] = useState("");
  const [submissionError, setSubmissionError] = useState(false);
  const [submissionPrivacyStatus, setSubmissionPrivacyStatus] = useState<"safe" | "quarantined">();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [lastSubmission, setLastSubmission] = useState<ReplyFeedbackRequest>();
  const feedbackIdentityRef = useRef(feedbackIdentity);
  const requestSequenceRef = useRef(0);
  const isSubmittingRef = useRef(false);
  const abortControllerRef = useRef<AbortController | undefined>(undefined);

  feedbackIdentityRef.current = feedbackIdentity;

  useEffect(() => {
    requestSequenceRef.current += 1;
    isSubmittingRef.current = false;
    abortControllerRef.current?.abort();
    abortControllerRef.current = undefined;
    setIsImprovementFormOpen(false);
    setReasons([]);
    setFinalReply("");
    setSubmissionError(false);
    setSubmissionPrivacyStatus(undefined);
    setIsSubmitting(false);
    setLastSubmission(undefined);

    return () => {
      abortControllerRef.current?.abort();
    };
  }, [feedbackIdentity]);

  function feedbackPayload(submission: FeedbackSubmission): ReplyFeedbackRequest {
    return {
      tenantId: card.audit.tenantId,
      operatorId: card.audit.operatorId,
      cardId: card.cardId,
      agentVersion,
      rating: submission.rating,
      reasons: [...submission.reasons],
      ...(submission.finalReply === undefined || submission.finalReply.trim() === ""
        ? {}
        : { finalReply: submission.finalReply.trim() }),
      scenario: card.scenario,
      salesStage: card.salesStage,
      evidenceIds: [...card.audit.evidenceIds],
    };
  }

  function requestIsCurrent(requestSequence: number, requestIdentity: string): boolean {
    return (
      requestSequenceRef.current === requestSequence
      && feedbackIdentityRef.current === requestIdentity
    );
  }

  async function submitFeedback(request: ReplyFeedbackRequest): Promise<void> {
    const requestIdentity = feedbackIdentityKey(request);
    if (isSubmittingRef.current || requestIdentity !== feedbackIdentityRef.current) {
      return;
    }

    const requestSequence = requestSequenceRef.current + 1;
    const abortController = new AbortController();
    requestSequenceRef.current = requestSequence;
    abortControllerRef.current = abortController;
    isSubmittingRef.current = true;
    setSubmissionError(false);
    setSubmissionPrivacyStatus(undefined);
    setIsSubmitting(true);
    setLastSubmission(request);

    try {
      const response = await fetch("/api/reply-feedback", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-tenant-id": request.tenantId,
        },
        body: JSON.stringify(request),
        signal: abortController.signal,
      });

      if (!requestIsCurrent(requestSequence, requestIdentity)) {
        return;
      }

      if (!response.ok) {
        isSubmittingRef.current = false;
        abortControllerRef.current = undefined;
        setIsSubmitting(false);
        setSubmissionError(true);
        return;
      }

      const parsedResponse = ReplyFeedbackAcceptedResponseSchema.safeParse(await response.json());
      if (!requestIsCurrent(requestSequence, requestIdentity)) {
        return;
      }

      isSubmittingRef.current = false;
      abortControllerRef.current = undefined;
      setIsSubmitting(false);
      if (!parsedResponse.success) {
        setSubmissionError(true);
        return;
      }

      setSubmissionPrivacyStatus(parsedResponse.data.privacyStatus);
    } catch {
      if (!requestIsCurrent(requestSequence, requestIdentity)) {
        return;
      }

      isSubmittingRef.current = false;
      abortControllerRef.current = undefined;
      setIsSubmitting(false);
      setSubmissionError(true);
    }
  }

  function toggleReason(reason: FeedbackReason, checked: boolean): void {
    setReasons((currentReasons) =>
      checked
        ? [...currentReasons, reason]
        : currentReasons.filter((currentReason) => currentReason !== reason),
    );
  }

  return (
    <section aria-label="客服评价">
      <h3>客服评价</h3>
      <button
        type="button"
        onClick={() => void submitFeedback(feedbackPayload({ rating: "helpful", reasons: [] }))}
        disabled={isSubmitting}
      >
        👍 有帮助
      </button>
      <button
        type="button"
        onClick={() => {
          setIsImprovementFormOpen(true);
          setSubmissionPrivacyStatus(undefined);
          setSubmissionError(false);
        }}
        disabled={isSubmitting}
      >
        👎 需要改进
      </button>

      {isImprovementFormOpen ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (reasons.length > 0) {
              void submitFeedback(feedbackPayload({ rating: "needs_improvement", reasons, finalReply }));
            }
          }}
        >
          <fieldset disabled={isSubmitting}>
            <legend>请选择需要改进的地方</legend>
            {IMPROVEMENT_REASONS.map((reason) => (
              <label key={reason.value}>
                <input
                  type="checkbox"
                  checked={reasons.includes(reason.value)}
                  onChange={(event) => toggleReason(reason.value, event.target.checked)}
                />
                {reason.label}
              </label>
            ))}
          </fieldset>
          <label htmlFor="final-reply">客服最后采用的回复</label>
          <textarea
            id="final-reply"
            value={finalReply}
            onChange={(event) => setFinalReply(event.target.value)}
            disabled={isSubmitting}
          />
          <p>提交前请删除姓名、电话、微信号、地址和支付信息；系统仍会自动脱敏。</p>
          <button
            type="button"
            onClick={() => setFinalReply("")}
            disabled={isSubmitting || finalReply === ""}
          >
            清除最终回复
          </button>
          <button type="submit" disabled={reasons.length === 0 || isSubmitting}>
            {submissionPrivacyStatus === "quarantined" ? "重新提交评价" : "提交评价"}
          </button>
        </form>
      ) : null}

      {isSubmitting ? <p role="status">正在提交评价…</p> : null}
      {submissionPrivacyStatus === "safe" ? (
        <p role="status">感谢评价，已进入优化池。</p>
      ) : null}
      {submissionPrivacyStatus === "quarantined" ? (
        <p role="alert">
          评价已记录，但最终回复已隔离，不会进入优化池。请删除敏感内容后重新提交。
        </p>
      ) : null}
      {submissionError ? (
        <>
          <p role="alert">评价暂未提交，不影响您复制和跟进客户。</p>
          {lastSubmission !== undefined ? (
            <button type="button" onClick={() => void submitFeedback(lastSubmission)}>
              重试提交
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

/**
 * The employee follow-up surface deliberately sends only the signed usage
 * proof and the employee's evaluation.  Customer/card identity is derived by
 * the supervision service; `cardInstanceId` is used by the parent as a React
 * key so a replacement card starts with a clean local form.
 */
export type SupervisionReplyFeedbackFormProps = Readonly<{
  customerId: string;
  usageToken: string;
  defaultFinalReply?: string;
  cardInstanceId: string;
}>;

type EmployeeFeedbackSubmission = Readonly<{
  rating: "helpful" | "needs_improvement";
  reasonCodes: FeedbackReason[];
  finalReply?: string;
}>;

export function SupervisionReplyFeedbackForm({
  customerId,
  usageToken,
  defaultFinalReply,
}: SupervisionReplyFeedbackFormProps) {
  const [isImprovementFormOpen, setIsImprovementFormOpen] = useState(false);
  const [reasons, setReasons] = useState<FeedbackReason[]>([]);
  const [finalReply, setFinalReply] = useState(defaultFinalReply ?? "");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState(false);
  const [submissionPrivacyStatus, setSubmissionPrivacyStatus] = useState<"safe" | "quarantined">();
  const [lastSubmission, setLastSubmission] = useState<EmployeeFeedbackSubmission>();
  const isSubmittingRef = useRef(false);

  useEffect(() => {
    setFinalReply(defaultFinalReply ?? "");
  }, [defaultFinalReply]);

  function toggleReason(reason: FeedbackReason, checked: boolean): void {
    setReasons((currentReasons) => checked
      ? [...currentReasons, reason]
      : currentReasons.filter((currentReason) => currentReason !== reason));
  }

  async function submitFeedback(submission: EmployeeFeedbackSubmission): Promise<void> {
    if (isSubmittingRef.current) return;

    isSubmittingRef.current = true;
    setIsSubmitting(true);
    setSubmissionError(false);
    setSubmissionPrivacyStatus(undefined);
    setLastSubmission(submission);

    try {
      const response = await fetch(
        `/api/supervision/customers/${encodeURIComponent(customerId)}/reply-feedback`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            usageToken,
            rating: submission.rating,
            reasonCodes: submission.reasonCodes,
            ...(submission.finalReply === undefined || submission.finalReply.trim() === ""
              ? {}
              : { finalReply: submission.finalReply.trim() }),
          }),
        },
      );
      const parsed = response.ok
        ? ReplyFeedbackAcceptedResponseSchema.safeParse(await response.json())
        : undefined;
      isSubmittingRef.current = false;
      setIsSubmitting(false);
      if (!parsed?.success) {
        setSubmissionError(true);
        return;
      }
      setSubmissionPrivacyStatus(parsed.data.privacyStatus);
    } catch {
      isSubmittingRef.current = false;
      setIsSubmitting(false);
      setSubmissionError(true);
    }
  }

  function openImprovementForm(): void {
    setIsImprovementFormOpen(true);
    setSubmissionError(false);
    setSubmissionPrivacyStatus(undefined);
  }

  return (
    <section className="supervision-reply-feedback" aria-label="本次回复评价">
      <div className="feedback-heading">
        <div>
          <p className="eyebrow">可选复盘</p>
          <h3>这条建议对你有帮助吗？</h3>
        </div>
        <span>不会影响复制或保存跟进</span>
      </div>
      <div className="choice-row">
        <button
          type="button"
          onClick={() => void submitFeedback({ rating: "helpful", reasonCodes: [] })}
          disabled={isSubmitting}
        >
          👍 有帮助
        </button>
        <button type="button" onClick={openImprovementForm} disabled={isSubmitting}>
          👎 需要改进
        </button>
      </div>

      {isImprovementFormOpen ? (
        <div className="feedback-improvement-form">
          <fieldset disabled={isSubmitting}>
            <legend>请选择需要改进的地方</legend>
            {IMPROVEMENT_REASONS.map((reason) => (
              <label key={reason.value}>
                <input
                  type="checkbox"
                  checked={reasons.includes(reason.value)}
                  onChange={(event) => toggleReason(reason.value, event.target.checked)}
                />
                {reason.label}
              </label>
            ))}
          </fieldset>
          <label>
            最终回复（可选）
            <textarea
              value={finalReply}
              onChange={(event) => setFinalReply(event.target.value)}
              disabled={isSubmitting}
            />
          </label>
          <p className="privacy-note">提交前请删除姓名、电话、微信号、地址和支付信息；系统仍会自动脱敏。</p>
          <div className="feedback-actions">
            <button
              type="button"
              onClick={() => setFinalReply("")}
              disabled={isSubmitting || finalReply === ""}
            >
              清除回复文本
            </button>
            <button
              type="button"
              disabled={isSubmitting || reasons.length === 0}
              onClick={() => void submitFeedback({
                rating: "needs_improvement",
                reasonCodes: reasons,
                finalReply,
              })}
            >
              {submissionPrivacyStatus === "quarantined" ? "重新提交评价" : "提交评价"}
            </button>
          </div>
        </div>
      ) : null}

      {isSubmitting ? <p role="status">正在提交评价…</p> : null}
      {submissionPrivacyStatus === "safe" ? <p className="sync-success" role="status">感谢评价，已进入优化池。</p> : null}
      {submissionPrivacyStatus === "quarantined" ? (
        <p className="sync-failed" role="alert">
          评价已记录，但回复文本没有保存，也不会进入优化池。请清除或修改敏感内容后重新提交。
        </p>
      ) : null}
      {submissionError ? (
        <>
          <p className="sync-failed" role="alert">评价暂未提交，不影响您复制和保存跟进。</p>
          {lastSubmission !== undefined ? (
            <button type="button" onClick={() => void submitFeedback(lastSubmission)}>重试提交</button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
