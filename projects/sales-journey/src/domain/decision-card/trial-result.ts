import type { SalesScenario } from "./types";
import { scenarioLabelFor } from "./scenarios/catalog";

export type ReplyUsage = "adopted" | "edited" | "not_used";
export type CustomerOutcome = "follow_up" | "won" | "lost";

export type TrialResultRecord = {
  cardId: string;
  scenario: SalesScenario;
  replyUsage: ReplyUsage;
  customerOutcome: CustomerOutcome;
  recordedAt: string;
};

const REPLY_USAGE_LABEL: Record<ReplyUsage, string> = {
  adopted: "采纳原文",
  edited: "已改写",
  not_used: "未采纳",
};

const CUSTOMER_OUTCOME_LABEL: Record<CustomerOutcome, string> = {
  follow_up: "待跟进",
  won: "已成交",
  lost: "未成交",
};

/**
 * Formats a browser-only trial record. It intentionally has no customer text,
 * product details, price, persistence, transport, or business aggregation.
 */
export function formatTrialResult(record: TrialResultRecord): string {
  return [
    "OPC 本地试用记录",
    `决策卡：${record.cardId}`,
    `场景：${scenarioLabelFor(record.scenario)}`,
    `回复使用：${REPLY_USAGE_LABEL[record.replyUsage]}`,
    `客户结果：${CUSTOMER_OUTCOME_LABEL[record.customerOutcome]}`,
    `记录时间：${record.recordedAt}`,
    "待人工汇总，不计入经营成果。",
  ].join("\n");
}
