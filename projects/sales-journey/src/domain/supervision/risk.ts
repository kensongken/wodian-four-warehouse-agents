import type {
  OwnerCustomerRiskRecord,
  OwnerDashboardRecord,
  OwnerRiskSignal,
  RiskAlert,
  RiskAlertKind,
} from "./contracts";

const MISSING_RESULT_MILLISECONDS = 24 * 60 * 60 * 1_000;

function timestamp(value: string): number {
  const milliseconds = new Date(value).getTime();
  if (Number.isNaN(milliseconds)) throw new RangeError("risk timestamps must be ISO timestamps.");
  return milliseconds;
}

function signalId(kind: RiskAlertKind, parts: readonly string[]): string {
  return `signal:${kind}:${parts.join(":")}`;
}

function recordSignal(
  kind: RiskAlertKind,
  record: OwnerDashboardRecord,
  now: string,
  labels: readonly string[],
): OwnerRiskSignal {
  return {
    id: signalId(kind, [record.tenantId, record.customerId, record.id]),
    tenantId: record.tenantId,
    kind,
    customerId: record.customerId,
    employeeUserId: record.employeeUserId,
    recordIds: [record.id],
    labels: [...labels],
    detectedAt: now,
  };
}

function existingSignal(
  alert: RiskAlert,
  records: readonly OwnerDashboardRecord[],
  now: string,
): OwnerRiskSignal {
  const matchingRecords = records.filter((record) => (
    record.tenantId === alert.tenantId
    && (!alert.customerId || record.customerId === alert.customerId)
    && (!alert.employeeUserId || record.employeeUserId === alert.employeeUserId)
  ));
  return {
    id: `signal:stored:${alert.id}`,
    tenantId: alert.tenantId,
    kind: alert.kind,
    customerId: alert.customerId,
    employeeUserId: alert.employeeUserId,
    recordIds: alert.kind === "duplicate_customer"
      ? alert.customerId ? [alert.customerId] : []
      : matchingRecords.map((record) => record.id).sort(),
    labels: [alert.kind],
    detectedAt: now,
    sourceAlertId: alert.id,
  };
}

/** Maps customer-scoped duplicate signals and follow-up-scoped signals to safe dashboard rows. */
export function riskLabelsForDashboardRecord(
  record: OwnerDashboardRecord,
  signals: readonly OwnerRiskSignal[],
): readonly string[] {
  return [...new Set(signals
    .filter((signal) => (
      signal.tenantId === record.tenantId
      && signal.recordIds.includes(
        signal.kind === "duplicate_customer" ? record.customerId : record.id,
      )
    ))
    .flatMap((signal) => [signal.kind, ...signal.labels]))].sort();
}

/** Evaluates review signals only; callers remain responsible for any human conclusion. */
export function evaluateRiskAlerts(
  inputRecords: readonly OwnerDashboardRecord[],
  now: string,
  existingAlerts: readonly RiskAlert[] = [],
  inputCustomers?: readonly OwnerCustomerRiskRecord[],
): readonly OwnerRiskSignal[] {
  const nowMs = timestamp(now);
  const records = [...new Map(
    inputRecords.map((record) => [record.id, record] as const),
  ).values()];
  const signals: OwnerRiskSignal[] = [];

  for (const record of records) {
    const plannedAtMs = timestamp(record.plannedFollowUpAt);
    const storedActualAtMs = record.actualFollowUpAt
      ? timestamp(record.actualFollowUpAt)
      : undefined;
    const actualAtMs = storedActualAtMs !== undefined && storedActualAtMs <= nowMs
      ? storedActualAtMs
      : undefined;
    if (plannedAtMs < nowMs && actualAtMs === undefined) {
      signals.push(recordSignal("overdue_follow_up", record, now, ["overdue_follow_up"]));
    }
    if (
      record.result === "pending"
      && actualAtMs !== undefined
      && actualAtMs + MISSING_RESULT_MILLISECONDS <= nowMs
    ) {
      signals.push(recordSignal("missing_result", record, now, ["missing_result"]));
    }
    if (record.safetyReviewRequired || (record.safetyFlags?.length ?? 0) > 0) {
      const labels = [...new Set(record.safetyFlags ?? [])].sort();
      signals.push(recordSignal(
        "sales_safety",
        record,
        now,
        labels.length > 0 ? labels : ["review_required"],
      ));
    }
    if (record.retentionFailed) {
      signals.push(recordSignal("retention_failed", record, now, ["retention_failed"]));
    }
  }

  const customerSources: readonly OwnerCustomerRiskRecord[] = inputCustomers ?? records.map(
    (record) => ({
      id: record.customerId,
      tenantId: record.tenantId,
      customerId: record.customerId,
      employeeUserId: record.employeeUserId,
      customerFingerprint: record.customerFingerprint,
    }),
  );
  const customers = [...new Map(
    customerSources.map((customer) => [
      JSON.stringify([customer.tenantId, customer.customerId]),
      customer,
    ] as const),
  ).values()];
  const fingerprints = new Map<string, OwnerCustomerRiskRecord[]>();
  for (const record of customers) {
    if (!record.customerFingerprint) continue;
    const key = `${record.tenantId}:${record.customerFingerprint}`;
    const matches = fingerprints.get(key) ?? [];
    matches.push(record);
    fingerprints.set(key, matches);
  }
  for (const matches of fingerprints.values()) {
    const customerIds = new Set(matches.map((record) => record.customerId));
    if (customerIds.size < 2) continue;
    const sorted = [...matches].sort((left, right) => left.id.localeCompare(right.id));
    signals.push({
      id: signalId("duplicate_customer", [
        sorted[0]!.tenantId,
        ...[...customerIds].sort(),
      ]),
      tenantId: sorted[0]!.tenantId,
      kind: "duplicate_customer",
      recordIds: sorted.map((record) => record.id),
      labels: ["duplicate_customer"],
      detectedAt: now,
    });
  }

  signals.push(...existingAlerts
    .filter((alert) => alert.status === "open")
    .map((alert) => existingSignal(alert, records, now)));

  return signals.sort((left, right) => left.id.localeCompare(right.id));
}
