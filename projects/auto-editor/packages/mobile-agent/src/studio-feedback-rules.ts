import {
  RevisionProposalSchema,
  type CustomerDraftFeedback,
  type RevisionProposal,
} from "@wearable/contracts";

const SUPPORTED_FEEDBACK: Readonly<Record<string, string>> = Object.freeze({
  "请让设计师观点更突出": "提高设计师观点开场的相对占比",
  "请让成品结尾更突出": "提高成品结尾镜头的相对占比",
  "节奏太慢": "仅在有安全时长预算的适用方向缩短非核心镜头；达到 30 秒下限时标记不适用",
});

export type RevisionRuleInput = Readonly<{
  identity: Omit<RevisionProposal,
    "understoodFeedback" | "plannedChanges" | "retainedElements" |
    "unsupportedRequests" | "captureGaps" | "status" |
    "confirmedByActorId" | "confirmedAt" | "createdAt">;
  feedback: CustomerDraftFeedback;
  createdAt: string;
}>;

export type CustomerFeedbackConsistencyInput = Readonly<{
  satisfaction: CustomerDraftFeedback["satisfaction"];
  issueCategories: readonly CustomerDraftFeedback["issueCategories"][number][];
  mustRemove: readonly string[];
  needsReshoot: readonly string[];
}>;

export function assertCustomerFeedbackConsistency(
  feedback: CustomerFeedbackConsistencyInput,
): void {
  if (feedback.satisfaction === "satisfied" &&
      (feedback.issueCategories.includes("fact-error") ||
       feedback.mustRemove.length > 0 || feedback.needsReshoot.length > 0)) {
    throw new Error("satisfied-customer-feedback-contradiction");
  }
}

export function normalizeOfflineFeedbackPhrase(reasonValue: string): string {
  if (typeof reasonValue !== "string") return "";
  return reasonValue
    .normalize("NFKC")
    .trim()
    .replace(/\s+/gu, "")
    .replace(/^(?:[-*•]|\(\d+\)|\d+[.)、:：]|[一二三四五六七八九十]+[.)、:：])/u, "")
    .replace(/[。.!！]+$/u, "");
}

function summarizeFeedback(feedback: CustomerDraftFeedback): string {
  const satisfaction = {
    satisfied: "客户表示满意",
    "partially-satisfied": "客户表示部分满意",
    unsatisfied: "客户表示不满意",
  }[feedback.satisfaction];
  return [
    satisfaction,
    feedback.comment,
    ...feedback.mustKeep.map((value) => `保留：${value}`),
    ...feedback.mustRemove.map((value) => `删除：${value}`),
    ...feedback.needsReshoot.map((value) => `现有素材无法完成：${value}`),
  ].filter(Boolean).join("；");
}

export function buildOfflineRevisionProposal(input: RevisionRuleInput): RevisionProposal {
  const normalized = normalizeOfflineFeedbackPhrase(input.feedback.comment ?? "");
  const planned = SUPPORTED_FEEDBACK[normalized] ? [SUPPORTED_FEEDBACK[normalized]!] : [];
  const captureGaps = [...input.feedback.needsReshoot];
  const unsupportedRequests = [
    ...(input.feedback.issueCategories.includes("fact-error")
      ? ["事实错误需人工核实并更正，自由文本不能确认事实"]
      : []),
    ...input.feedback.mustRemove.map((value) => `删除请求需人工确认：${value}`),
    ...(planned.length === 0 && captureGaps.length === 0 &&
        !input.feedback.issueCategories.includes("fact-error") && input.feedback.mustRemove.length === 0
      ? [normalized || "反馈尚未转成受支持的修改规则"]
      : []),
  ];
  return RevisionProposalSchema.parse({
    ...input.identity,
    understoodFeedback: summarizeFeedback(input.feedback),
    plannedChanges: planned,
    retainedElements: input.feedback.mustKeep,
    unsupportedRequests,
    captureGaps,
    status: "proposed",
    confirmedByActorId: null,
    confirmedAt: null,
    createdAt: input.createdAt,
  });
}
