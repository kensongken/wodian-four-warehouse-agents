import type {
  CorrectionRecord,
  CustomerRecord,
  DecisionUsageRecord,
  FollowUpRecord,
} from "@/domain/supervision/contracts";
import type {
  EmployeeCorrectionView,
  EmployeeCustomerView,
  EmployeeFollowUpReceiptView,
  EmployeeTimelineEntryView,
} from "@/domain/supervision/employee-view";
import type { TimelineEntry } from "@/server/supervision/repository";

export function toEmployeeCustomerView(customer: CustomerRecord): EmployeeCustomerView {
  return {
    id: customer.id,
    customerNumber: customer.customerNumber,
    customerAlias: customer.customerAlias,
    category: customer.category,
    plannedFollowUpAt: customer.plannedFollowUpAt,
    status: customer.status,
  };
}

export function toEmployeeFollowUpReceiptView(
  followUp: FollowUpRecord,
): EmployeeFollowUpReceiptView {
  return { id: followUp.id };
}

export function toEmployeeCorrectionView(
  correction: CorrectionRecord,
  actorLabel: string,
): EmployeeCorrectionView {
  return {
    kind: "correction",
    id: correction.id,
    actorLabel,
    createdAt: correction.createdAt,
    followUpId: correction.followUpId,
    reason: correction.reason,
  };
}

function replySelection(usage: DecisionUsageRecord | undefined): string | undefined {
  if (!usage?.chosenReplyId) return undefined;
  const index = usage.providedReplyIds.indexOf(usage.chosenReplyId);
  return index < 0 ? undefined : `第 ${index + 1} 条回复`;
}

export function toEmployeeTimelineView(
  timeline: readonly TimelineEntry[],
  actorLabelFor: (actorUserId: string) => string,
): EmployeeTimelineEntryView[] {
  const usageByFollowUp = new Map(
    timeline
      .filter((entry): entry is Extract<TimelineEntry, { kind: "decision_usage" }> =>
        entry.kind === "decision_usage")
      .map((entry) => [entry.record.followUpId, entry.record]),
  );

  return timeline.map((entry): EmployeeTimelineEntryView => {
    if (entry.kind === "follow_up") {
      return {
        kind: "follow_up",
        id: entry.record.id,
        actorLabel: actorLabelFor(entry.record.employeeUserId),
        createdAt: entry.record.createdAt,
        customerMessageSummary: entry.record.customerMessageSummary,
        plannedFollowUpAt: entry.record.plannedFollowUpAt,
        actualFollowUpAt: entry.record.actualFollowUpAt,
        result: entry.record.result,
        nextAction: entry.record.nextAction,
        replyUsage: entry.record.replyUsage,
        replySelection: replySelection(usageByFollowUp.get(entry.record.id)),
      };
    }
    if (entry.kind === "decision_usage") {
      return {
        kind: "decision_usage",
        id: entry.record.id,
        actorLabel: actorLabelFor(entry.record.employeeUserId),
        createdAt: entry.record.createdAt,
        replyUsage: entry.record.replyUsage,
        replySelection: replySelection(entry.record),
      };
    }
    return toEmployeeCorrectionView(
      entry.record,
      actorLabelFor(entry.record.actorUserId),
    );
  });
}
