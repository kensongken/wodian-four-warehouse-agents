"use client";

import { type FormEvent, useState } from "react";

import type { FollowUpResult, ReplyUsage } from "@/domain/supervision/contracts";
import type {
  EmployeeCorrectionView,
  EmployeeTimelineEntryView,
} from "@/domain/supervision/employee-view";

type CustomerTimelineProps = Readonly<{
  customerId: string;
  entries: readonly EmployeeTimelineEntryView[];
}>;

const ACTION_LABELS = {
  follow_up: "客户跟进",
  decision_usage: "决策卡使用",
  correction: "补充纠错",
} as const;

const RESULT_LABELS: Record<FollowUpResult, string> = {
  pending: "待跟进",
  interested: "意向中",
  won: "已成交",
  lost: "未成交",
};

const USAGE_LABELS: Record<ReplyUsage, string> = {
  adopted: "已采用",
  edited: "已改写",
  not_used: "未使用",
};

function displayTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function entrySummary(entry: EmployeeTimelineEntryView): string {
  if (entry.kind === "follow_up") return entry.customerMessageSummary;
  if (entry.kind === "correction") return entry.reason;
  return `决策记录：${entry.replySelection ?? "未选择回复"} · ${USAGE_LABELS[entry.replyUsage]}`;
}

function entryDetail(entry: EmployeeTimelineEntryView): string | undefined {
  if (entry.kind !== "follow_up") return undefined;
  const selection = entry.replyUsage === "not_used"
    ? USAGE_LABELS.not_used
    : `${entry.replySelection ?? "未记录"} · ${USAGE_LABELS[entry.replyUsage]}`;
  const actual = entry.actualFollowUpAt
    ? displayTime(entry.actualFollowUpAt)
    : "未记录";
  return `${selection} · 结果：${RESULT_LABELS[entry.result]} · 计划：${displayTime(entry.plannedFollowUpAt)} · 实际：${actual} · 下一步：${entry.nextAction}`;
}

function correctionViewFrom(payload: unknown): EmployeeCorrectionView | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const correction = (payload as Record<string, unknown>).correction;
  if (!correction || typeof correction !== "object") return undefined;
  const source = correction as Record<string, unknown>;
  if (
    source.kind !== "correction"
    || typeof source.id !== "string"
    || typeof source.actorLabel !== "string"
    || typeof source.createdAt !== "string"
    || typeof source.followUpId !== "string"
    || typeof source.reason !== "string"
  ) {
    return undefined;
  }
  return {
    kind: "correction",
    id: source.id,
    actorLabel: source.actorLabel,
    createdAt: source.createdAt,
    followUpId: source.followUpId,
    reason: source.reason,
  };
}

function correctionRequestId(): string {
  return `correction-${crypto.randomUUID()}`;
}

export function CustomerTimeline({
  customerId,
  entries,
}: CustomerTimelineProps) {
  const [visibleEntries, setVisibleEntries] = useState(entries);
  const [correctingFollowUpId, setCorrectingFollowUpId] = useState<string>();
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [requestId, setRequestId] = useState(correctionRequestId);
  const [requestFingerprint, setRequestFingerprint] = useState<string>();

  async function appendCorrection(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!correctingFollowUpId || status === "saving") return;
    const fingerprint = JSON.stringify({ followUpId: correctingFollowUpId, reason });
    const submittedRequestId = requestFingerprint !== undefined
      && requestFingerprint !== fingerprint
      ? correctionRequestId()
      : requestId;
    setRequestId(submittedRequestId);
    setRequestFingerprint(fingerprint);
    setStatus("saving");
    try {
      const response = await fetch(
        `/api/supervision/customers/${encodeURIComponent(customerId)}/corrections`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            requestId: submittedRequestId,
            followUpId: correctingFollowUpId,
            reason,
          }),
        },
      );
      const payload: unknown = await response.json().catch(() => undefined);
      const correction = response.ok ? correctionViewFrom(payload) : undefined;
      if (!correction) {
        throw new Error("correction failed");
      }
      setVisibleEntries((current) => [
        ...current,
        correction,
      ]);
      setReason("");
      setCorrectingFollowUpId(undefined);
      setStatus("saved");
      setRequestId(correctionRequestId());
      setRequestFingerprint(undefined);
    } catch {
      setStatus("failed");
    }
  }

  return (
    <section className="employee-panel timeline-panel" aria-label="客户不可改写时间线">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">留痕记录</p>
          <h2>客户时间线</h2>
        </div>
        <span className="immutable-chip">只追加，不改删</span>
      </div>

      {visibleEntries.length === 0 ? (
        <p className="empty-timeline">还没有跟进记录。第一次保存后会出现在这里。</p>
      ) : (
        <ol className="customer-timeline">
          {visibleEntries.map((entry) => {
            const detail = entryDetail(entry);
            return (
              <li key={`${entry.kind}-${entry.id}`}>
                <div className="timeline-marker" aria-hidden="true" />
                <article>
                  <header>
                    <strong>{ACTION_LABELS[entry.kind]}</strong>
                    <time dateTime={entry.createdAt}>{displayTime(entry.createdAt)}</time>
                  </header>
                  <p className="timeline-actor">{entry.actorLabel}</p>
                  <p>{entrySummary(entry)}</p>
                  {detail !== undefined ? <p className="timeline-detail">{detail}</p> : null}
                  {entry.kind === "follow_up" ? (
                    <button
                      className="text-action"
                      type="button"
                      disabled={status === "saving"}
                      onClick={() => {
                        setRequestId(correctionRequestId());
                        setRequestFingerprint(undefined);
                        setCorrectingFollowUpId(entry.id);
                        setReason("");
                        setStatus("idle");
                      }}
                    >
                      为本条添加纠错
                    </button>
                  ) : null}
                </article>
              </li>
            );
          })}
        </ol>
      )}

      {correctingFollowUpId !== undefined ? (
        <form className="correction-form" onSubmit={appendCorrection}>
          <p>原记录会保留；本次说明会作为一条新记录追加。</p>
          <label>
            纠错说明
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              required
              disabled={status === "saving"}
            />
          </label>
          <button className="primary-action" type="submit" disabled={status === "saving"}>
            {status === "saving" ? "正在提交…" : "提交补充纠错"}
          </button>
          <button
            type="button"
            disabled={status === "saving"}
            onClick={() => setCorrectingFollowUpId(undefined)}
          >取消</button>
        </form>
      ) : null}
      {status === "saved" ? <p className="sync-success" role="status">纠错已追加</p> : null}
      {status === "failed" ? <p className="sync-failed" role="alert">尚未同步，请重试</p> : null}
    </section>
  );
}
