import type { JewelryCategory } from "@/domain/decision-card/local-evidence";
import type { FollowUpResult, ReplyUsage } from "@/domain/supervision/contracts";

export type EmployeeCustomerView = Readonly<{
  id: string;
  customerNumber: string;
  customerAlias: string;
  category: JewelryCategory;
  plannedFollowUpAt: string;
  status: FollowUpResult;
}>;

type EmployeeTimelineBase = Readonly<{
  id: string;
  actorLabel: string;
  createdAt: string;
}>;

export type EmployeeFollowUpView = EmployeeTimelineBase & Readonly<{
  kind: "follow_up";
  customerMessageSummary: string;
  plannedFollowUpAt: string;
  actualFollowUpAt?: string;
  result: FollowUpResult;
  nextAction: string;
  replyUsage: ReplyUsage;
  replySelection?: string;
}>;

export type EmployeeDecisionUsageView = EmployeeTimelineBase & Readonly<{
  kind: "decision_usage";
  replyUsage: ReplyUsage;
  replySelection?: string;
}>;

export type EmployeeCorrectionView = EmployeeTimelineBase & Readonly<{
  kind: "correction";
  followUpId: string;
  reason: string;
}>;

export type EmployeeFollowUpReceiptView = Readonly<{
  id: string;
}>;

export type EmployeeTimelineEntryView =
  | EmployeeFollowUpView
  | EmployeeDecisionUsageView
  | EmployeeCorrectionView;
