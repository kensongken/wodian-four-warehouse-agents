import type {
  OwnerDashboardDrillDown,
  OwnerDashboardEmployee,
  OwnerDashboardMetricKey,
  OwnerDashboardMetrics,
  OwnerDashboardRecord,
  OwnerDashboardRow,
} from "./contracts";

export type { OwnerDashboardRecord } from "./contracts";

export type OwnerDashboardAggregation = Readonly<{
  totals: OwnerDashboardMetrics;
  employees: readonly OwnerDashboardEmployee[];
  records: readonly OwnerDashboardRow[];
}>;

const METRIC_KEYS: readonly OwnerDashboardMetricKey[] = [
  "submitted",
  "followedUp",
  "pending",
  "due",
  "overdue",
  "interested",
  "won",
  "lost",
  "adopted",
  "edited",
  "notUsed",
  "missingResult",
  "averageResponseDurationMs",
  "completionRate",
];

type MutableMetrics = {
  submitted: number;
  followedUp: number;
  pending: number;
  due: number;
  overdue: number;
  interested: number;
  won: number;
  lost: number;
  adopted: number;
  edited: number;
  notUsed: number;
  missingResult: number;
  averageResponseDurationMs: number | null;
  completionRate: number;
  drillDownRecordIds: Record<OwnerDashboardMetricKey, string[]>;
  responseDurationTotalMs: number;
};

function timestamp(value: string, field: string): number {
  const milliseconds = new Date(value).getTime();
  if (Number.isNaN(milliseconds)) throw new RangeError(`${field} must be an ISO timestamp.`);
  return milliseconds;
}

function drillDownSeed(): Record<OwnerDashboardMetricKey, string[]> {
  return {
    submitted: [],
    followedUp: [],
    pending: [],
    due: [],
    overdue: [],
    interested: [],
    won: [],
    lost: [],
    adopted: [],
    edited: [],
    notUsed: [],
    missingResult: [],
    averageResponseDurationMs: [],
    completionRate: [],
  };
}

function metricSeed(): MutableMetrics {
  return {
    submitted: 0,
    followedUp: 0,
    pending: 0,
    due: 0,
    overdue: 0,
    interested: 0,
    won: 0,
    lost: 0,
    adopted: 0,
    edited: 0,
    notUsed: 0,
    missingResult: 0,
    averageResponseDurationMs: null,
    completionRate: 0,
    drillDownRecordIds: drillDownSeed(),
    responseDurationTotalMs: 0,
  };
}

function count(metrics: MutableMetrics, key: OwnerDashboardMetricKey, recordId: string): void {
  if (
    key !== "averageResponseDurationMs"
    && key !== "completionRate"
  ) {
    metrics[key] += 1;
  }
  metrics.drillDownRecordIds[key].push(recordId);
}

function countRecord(metrics: MutableMetrics, record: OwnerDashboardRecord, nowMs: number): void {
  const responseStartedAtMs = timestamp(
    record.responseStartedAt ?? record.submittedAt,
    "responseStartedAt",
  );
  const plannedAtMs = timestamp(record.plannedFollowUpAt, "plannedFollowUpAt");
  const storedActualAtMs = record.actualFollowUpAt
    ? timestamp(record.actualFollowUpAt, "actualFollowUpAt")
    : undefined;
  const actualAtMs = storedActualAtMs !== undefined && storedActualAtMs <= nowMs
    ? storedActualAtMs
    : undefined;

  count(metrics, "submitted", record.id);
  if (actualAtMs !== undefined) {
    count(metrics, "followedUp", record.id);
    if (actualAtMs >= responseStartedAtMs) {
      metrics.responseDurationTotalMs += actualAtMs - responseStartedAtMs;
      metrics.drillDownRecordIds.averageResponseDurationMs.push(record.id);
    }
  }
  if (record.result === "pending") count(metrics, "pending", record.id);
  if (plannedAtMs <= nowMs) {
    count(metrics, "due", record.id);
    if (actualAtMs !== undefined) metrics.drillDownRecordIds.completionRate.push(record.id);
  }
  if (plannedAtMs < nowMs && actualAtMs === undefined) count(metrics, "overdue", record.id);
  if (record.result === "interested") count(metrics, "interested", record.id);
  if (record.result === "won") count(metrics, "won", record.id);
  if (record.result === "lost") count(metrics, "lost", record.id);
  if (record.replyUsage === "adopted") count(metrics, "adopted", record.id);
  if (record.replyUsage === "edited") count(metrics, "edited", record.id);
  if (record.replyUsage === "not_used") count(metrics, "notUsed", record.id);
  if (
    record.result === "pending"
    && actualAtMs !== undefined
    && actualAtMs + 24 * 60 * 60 * 1_000 <= nowMs
  ) {
    count(metrics, "missingResult", record.id);
  }
}

function finalize(metrics: MutableMetrics): OwnerDashboardMetrics {
  const responseCount = metrics.drillDownRecordIds.averageResponseDurationMs.length;
  const averageResponseDurationMs = responseCount === 0
    ? null
    : Math.round(metrics.responseDurationTotalMs / responseCount);
  const completionRate = metrics.due === 0
    ? 0
    : Math.round((metrics.drillDownRecordIds.completionRate.length / metrics.due) * 10_000) / 100;
  const sortedDrillDown = METRIC_KEYS.reduce<Record<OwnerDashboardMetricKey, string[]>>(
    (result, key) => {
      result[key] = [...metrics.drillDownRecordIds[key]].sort();
      return result;
    },
    drillDownSeed(),
  ) satisfies OwnerDashboardDrillDown;

  return {
    submitted: metrics.submitted,
    followedUp: metrics.followedUp,
    pending: metrics.pending,
    due: metrics.due,
    overdue: metrics.overdue,
    interested: metrics.interested,
    won: metrics.won,
    lost: metrics.lost,
    adopted: metrics.adopted,
    edited: metrics.edited,
    notUsed: metrics.notUsed,
    missingResult: metrics.missingResult,
    averageResponseDurationMs,
    completionRate,
    drillDownRecordIds: sortedDrillDown,
  };
}

function safeRow(record: OwnerDashboardRecord, nowMs: number): OwnerDashboardRow {
  const actualFollowUpAt = record.actualFollowUpAt
    && timestamp(record.actualFollowUpAt, "actualFollowUpAt") <= nowMs
    ? record.actualFollowUpAt
    : undefined;
  return {
    id: record.id,
    customerNumber: record.customerNumber,
    customerAlias: record.customerAlias,
    employeeUserId: record.employeeUserId,
    employeeLabel: record.employeeLabel,
    category: record.category,
    submittedAt: record.submittedAt,
    plannedFollowUpAt: record.plannedFollowUpAt,
    actualFollowUpAt,
    decisionUsage: record.replyUsage,
    nextAction: record.nextAction,
    result: record.result,
    riskLabels: [...(record.riskLabels ?? [])].sort(),
  };
}

/** Aggregates one unique follow-up row per record ID into proof-carrying metrics. */
export function aggregateOwnerDashboard(
  inputRecords: readonly OwnerDashboardRecord[],
  now: string,
): OwnerDashboardAggregation {
  const nowMs = timestamp(now, "now");
  const records = [...new Map(
    inputRecords.map((record) => [record.id, record] as const),
  ).values()].sort((left, right) => left.id.localeCompare(right.id));
  const totals = metricSeed();
  const byEmployee = new Map<string, { label: string; metrics: MutableMetrics }>();

  for (const record of records) {
    const employee = byEmployee.get(record.employeeUserId) ?? {
      label: record.employeeLabel,
      metrics: metricSeed(),
    };
    countRecord(totals, record, nowMs);
    countRecord(employee.metrics, record, nowMs);
    byEmployee.set(record.employeeUserId, employee);
  }

  return {
    totals: finalize(totals),
    employees: [...byEmployee.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([employeeUserId, employee]) => ({
        employeeUserId,
        employeeLabel: employee.label,
        ...finalize(employee.metrics),
      })),
    records: records.map((record) => safeRow(record, nowMs)),
  };
}
