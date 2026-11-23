"use client";

import { useEffect, useState } from "react";

import type { DecisionCard, SalesStage } from "@/domain/decision-card/types";
import {
  JourneyWriteBackSchema,
  type JourneyWriteBack,
  type SalesJourneyContext,
} from "@/domain/sales-journey/schema";

const STAGE_LABELS: Record<SalesStage, string> = {
  discover: "初步了解",
  explain: "说明方案",
  evaluate: "对比评估",
  advance: "推进成交",
  follow_up: "后续跟进",
};

type SalesJourneyPanelProps = {
  journey: SalesJourneyContext;
  card: DecisionCard;
  agentVersion: string;
  onConfirmJourneyWriteBack: (writeBack: JourneyWriteBack) => void | Promise<void>;
};

type JourneyListProps = {
  title: string;
  items: readonly string[];
  emptyMessage: string;
};

function JourneyList({ title, items, emptyMessage }: JourneyListProps) {
  return (
    <section>
      <h3>{title}</h3>
      {items.length > 0 ? (
        <ul>
          {items.map((item) => <li key={item}>{item}</li>)}
        </ul>
      ) : <p>{emptyMessage}</p>}
    </section>
  );
}

export function SalesJourneyPanel({
  journey,
  card,
  agentVersion,
  onConfirmJourneyWriteBack,
}: SalesJourneyPanelProps) {
  const [isConfirming, setIsConfirming] = useState(false);
  const [confirmationError, setConfirmationError] = useState<string>();
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    setIsConfirming(false);
    setConfirmationError(undefined);
    setConfirmed(false);
  }, [card.cardId]);

  async function confirmWriteBack(): Promise<void> {
    const parsed = JourneyWriteBackSchema.safeParse({
      journeyId: journey.journeyId,
      tenantId: journey.tenantId,
      operatorId: journey.operatorId,
      proposedStage: card.salesStage,
      nextAction: card.nextAction.text,
      confirmedByOperator: true,
    });

    if (!parsed.success) {
      setConfirmationError("旅程建议无效，请重新生成后再确认。");
      return;
    }

    setIsConfirming(true);
    setConfirmationError(undefined);
    try {
      await onConfirmJourneyWriteBack(parsed.data);
      setConfirmed(true);
    } catch {
      setConfirmationError("本地确认失败，建议仍保留，可重试本地确认。");
    } finally {
      setIsConfirming(false);
    }
  }

  return (
    <aside className="sales-journey-panel" aria-label="本次销售旅程">
      <h2>本次销售旅程</h2>
      <p>当前阶段：{STAGE_LABELS[journey.currentStage]}</p>
      <JourneyList title="已确认需求" items={journey.confirmedNeeds} emptyMessage="暂未确认需求。" />
      <JourneyList title="待确认问题" items={journey.pendingQuestions} emptyMessage="暂时没有待确认问题。" />
      <JourneyList title="已承诺事项" items={journey.commitments} emptyMessage="暂时没有已承诺事项。" />
      <JourneyList title="待办事项" items={journey.openTasks} emptyMessage="暂时没有待办事项。" />
      <p>建议版本：{agentVersion}</p>
      <p>建议阶段：{STAGE_LABELS[card.salesStage]}</p>
      <p>建议下一步：{card.nextAction.text}</p>
      <p>本地演示只记录本页确认，不会写入真实 CRM 或客户系统。</p>
      <button type="button" onClick={() => void confirmWriteBack()} disabled={isConfirming}>
        {confirmationError === undefined ? "确认本次旅程建议（本地演示）" : "重试本地确认"}
      </button>
      {isConfirming ? <p role="status">正在确认本地建议…</p> : null}
      {confirmed ? <p role="status">本页确认完成，未写入真实系统。</p> : null}
      {confirmationError !== undefined ? <p role="alert">{confirmationError}</p> : null}
    </aside>
  );
}
