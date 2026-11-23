"use client";

import { useEffect, useRef, useState } from "react";

import type { DecisionCard } from "@/domain/decision-card/types";
import type { JourneyWriteBack, SalesJourneyContext } from "@/domain/sales-journey/schema";
import {
  formatTrialResult,
  type CustomerOutcome,
  type ReplyUsage,
} from "@/domain/decision-card/trial-result";

import { SalesJourneyPanel } from "./sales-journey-panel";

type DecisionCardViewProps = {
  card: DecisionCard;
  evidenceTitles?: Readonly<Record<string, string>>;
  selectedReplyId?: string;
  onSelectReply?: (replyId: string) => void;
  showTrialRecorder?: boolean;
  journey?: SalesJourneyContext;
  agentVersion?: string;
  onConfirmJourneyWriteBack?: (writeBack: JourneyWriteBack) => void | Promise<void>;
};

export function DecisionCardView({
  card,
  evidenceTitles = {},
  selectedReplyId,
  onSelectReply,
  showTrialRecorder = true,
  journey,
  agentVersion,
  onConfirmJourneyWriteBack,
}: DecisionCardViewProps) {
  const [copied, setCopied] = useState(false);
  const [voiceUrl, setVoiceUrl] = useState<string>();
  const [voiceDownloadable, setVoiceDownloadable] = useState(false);
  const [isGeneratingVoice, setIsGeneratingVoice] = useState(false);
  const [voiceError, setVoiceError] = useState<string>();
  const [replyUsage, setReplyUsage] = useState<ReplyUsage>();
  const [customerOutcome, setCustomerOutcome] = useState<CustomerOutcome>();
  const [trialRecord, setTrialRecord] = useState<string>();
  const voiceUrlRef = useRef<string | undefined>(undefined);
  const isMountedRef = useRef(false);
  const selectedReply = card.replyOptions.find((reply) => reply.id === selectedReplyId)
    ?? card.replyOptions[0]!;
  const needsReview = card.safety.status === "reviewRequired";

  useEffect(() => {
    isMountedRef.current = true;

    return () => {
      isMountedRef.current = false;
      if (voiceUrlRef.current !== undefined) {
        URL.revokeObjectURL(voiceUrlRef.current);
        voiceUrlRef.current = undefined;
      }
    };
  }, []);

  async function copyReply(): Promise<void> {
    await navigator.clipboard.writeText(selectedReply.text);
    setCopied(true);
  }

  async function generateVoice(): Promise<void> {
    setIsGeneratingVoice(true);
    setVoiceError(undefined);

    try {
      const response = await fetch("/api/voice-preview", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ card }),
      });

      if (!response.ok) {
        const error = (await response.json().catch(() => undefined)) as
          | { error?: unknown }
          | undefined;
        if (isMountedRef.current) {
          setVoiceError(
            error?.error === "CHINESE_FEMALE_VOICE_UNAVAILABLE"
              ? "本机未找到可用的中文女声。"
              : "语音生成失败，请稍后重试。",
          );
        }
        return;
      }

      const nextVoiceUrl = URL.createObjectURL(await response.blob());
      if (!isMountedRef.current) {
        URL.revokeObjectURL(nextVoiceUrl);
        return;
      }

      if (voiceUrlRef.current !== undefined) {
        URL.revokeObjectURL(voiceUrlRef.current);
      }
      voiceUrlRef.current = nextVoiceUrl;
      setVoiceUrl(nextVoiceUrl);
      setVoiceDownloadable(response.headers.get("x-voice-downloadable") === "true");
    } catch {
      if (isMountedRef.current) {
        setVoiceError("语音生成失败，请稍后重试。");
      }
    } finally {
      if (isMountedRef.current) {
        setIsGeneratingVoice(false);
      }
    }
  }

  function createTrialRecord(): void {
    if (replyUsage === undefined || customerOutcome === undefined) {
      return;
    }

    setTrialRecord(
      formatTrialResult({
        cardId: card.cardId,
        scenario: card.scenario,
        replyUsage,
        customerOutcome,
        recordedAt: new Date().toISOString(),
      }),
    );
  }

  async function copyTrialRecord(): Promise<void> {
    if (trialRecord !== undefined) {
      await navigator.clipboard.writeText(trialRecord);
    }
  }

  return (
    <section className="decision-card" aria-label="生成的销售决策卡">
      <h2>销售建议草稿</h2>
      <p role="status">{needsReview ? "需人工复核" : "可直接使用"}</p>
      <p>发送方式：仅供人工复制</p>
      <p style={{ whiteSpace: "pre-line" }}>{card.summary}</p>

      {journey !== undefined && agentVersion !== undefined && onConfirmJourneyWriteBack !== undefined ? (
        <SalesJourneyPanel
          journey={journey}
          card={card}
          agentVersion={agentVersion}
          onConfirmJourneyWriteBack={onConfirmJourneyWriteBack}
        />
      ) : null}

      <h3>建议回复</h3>
      <div className="reply-options">
        {card.replyOptions.map((reply, index) => (
          <article className="reply-option" key={reply.id}>
            {onSelectReply !== undefined ? (
              <label>
                <input
                  type="radio"
                  name="decision-reply"
                  aria-label={`选择第 ${index + 1} 条回复`}
                  checked={selectedReply.id === reply.id}
                  onChange={() => onSelectReply(reply.id)}
                />
                回复 {index + 1}
              </label>
            ) : null}
            <p>{reply.text}</p>
          </article>
        ))}
      </div>
      <button type="button" onClick={copyReply}>
        复制回复
      </button>
      {copied ? <p role="status">已复制，请由人工确认后自行发送。</p> : null}

      <h3>语音试听</h3>
      <button type="button" onClick={generateVoice} disabled={isGeneratingVoice}>
        {isGeneratingVoice ? "正在生成语音…" : "生成语音"}
      </button>
      {voiceError !== undefined ? <p role="alert">{voiceError}</p> : null}
      {voiceUrl !== undefined ? (
        <audio aria-label="销售回复语音试听" controls src={voiceUrl} />
      ) : null}
      {voiceUrl !== undefined && voiceDownloadable ? (
        <a href={voiceUrl} download="销售回复.m4a">
          下载语音
        </a>
      ) : null}

      <h3>引用资料</h3>
      {selectedReply.evidenceIds.length > 0 ? (
        <ul>
          {selectedReply.evidenceIds.map((evidenceId) => (
            <li key={evidenceId}>{evidenceTitles[evidenceId] ?? evidenceId}</li>
          ))}
        </ul>
      ) : (
        <p>本次没有自动引用资料。</p>
      )}

      {needsReview ? (
        <>
          {card.reviewFocus !== undefined ? (
            <p>优先核对：{card.reviewFocus.operatorPrompt}</p>
          ) : null}
          <details>
            <summary>查看全部核验项</summary>
            <ul>
              {card.verificationNeeded.map((item) => (
                <li key={item.field}>{item.reason}</li>
              ))}
            </ul>
          </details>
          <p>{card.nextAction.text}</p>
        </>
      ) : null}

      {showTrialRecorder ? (
        <>
          <h3>OPC 试用结果记录</h3>
          <p>经营证据支持：需由真实 OPC 人工使用并另行汇总；本页面记录不计入经营成果。</p>
          <label htmlFor="reply-usage">回复使用方式</label>
          <select
            id="reply-usage"
            value={replyUsage ?? ""}
            onChange={(event) => setReplyUsage(event.target.value as ReplyUsage)}
          >
            <option value="">请选择</option>
            <option value="adopted">采纳原文</option>
            <option value="edited">已改写</option>
            <option value="not_used">未采纳</option>
          </select>

          <label htmlFor="customer-outcome">客户当前结果</label>
          <select
            id="customer-outcome"
            value={customerOutcome ?? ""}
            onChange={(event) => setCustomerOutcome(event.target.value as CustomerOutcome)}
          >
            <option value="">请选择</option>
            <option value="follow_up">待跟进</option>
            <option value="won">已成交</option>
            <option value="lost">未成交</option>
          </select>

          <button
            type="button"
            onClick={createTrialRecord}
            disabled={replyUsage === undefined || customerOutcome === undefined}
          >
            生成试用记录
          </button>
          {trialRecord !== undefined ? (
            <>
              <pre>{trialRecord}</pre>
              <button type="button" onClick={copyTrialRecord}>
                复制试用记录
              </button>
            </>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
