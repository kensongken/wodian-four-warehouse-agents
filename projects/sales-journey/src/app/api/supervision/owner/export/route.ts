import type { OwnerDashboard, OwnerDashboardRow } from "@/domain/supervision/contracts";
import { OwnerFilterSchema, type OwnerFilter } from "@/domain/supervision/schemas";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { getOwnerDirectory, resolveOwnerEmployeeRef } from "@/server/supervision/owner-view";

const CSV_HEADERS = [
  "customerNumber",
  "customerAlias",
  "employeeLabel",
  "category",
  "submittedAt",
  "plannedFollowUpAt",
  "actualFollowUpAt",
  "decisionUsage",
  "nextAction",
  "result",
  "riskLabels",
] as const;

function parseFilters(request: Request): OwnerFilter {
  return OwnerFilterSchema.parse(Object.fromEntries(new URL(request.url).searchParams.entries()));
}

function csvCell(value: string): string {
  const formulaSafe = /^[=+\-@]/u.test(value) ? `'${value}` : value;
  return /[",\r\n]/u.test(formulaSafe)
    ? `"${formulaSafe.replaceAll('"', '""')}"`
    : formulaSafe;
}

function csvRow(record: OwnerDashboardRow): string {
  return [
    record.customerNumber,
    record.customerAlias,
    record.employeeLabel,
    record.category,
    record.submittedAt,
    record.plannedFollowUpAt,
    record.actualFollowUpAt ?? "",
    record.decisionUsage,
    record.nextAction,
    record.result,
    record.riskLabels.join("|"),
  ].map(csvCell).join(",");
}

/** Serializes the explicit safe-row allowlist; vault fields have no path into this function. */
function exportOwnerCsv(dashboard: OwnerDashboard): string {
  return [CSV_HEADERS.join(","), ...dashboard.records.map(csvRow)].join("\n");
}

function noStore(response: Response): Response {
  response.headers.set("Cache-Control", "no-store, private");
  return response;
}

export async function GET(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireOwnerFromD1(request.headers, runtime.d1);
    const filters = parseFilters(request);
    const now = runtime.now?.() ?? new Date().toISOString();
    const { employeeRef, ...publicFilters } = filters;
    const directory = await getOwnerDirectory(runtime.d1, actor.tenantId, runtime.usageTokenSecret);
    const employeeUserId = employeeRef
      ? resolveOwnerEmployeeRef(directory, employeeRef)?.userId
      : filters.employeeUserId;
    const dashboard = await runtime.repository.getOwnerDashboard({
      tenantId: actor.tenantId,
      now,
      ...publicFilters,
      employeeUserId: employeeRef && !employeeUserId ? "__no_matching_employee__" : employeeUserId,
    });
    const exportId = runtime.createId?.() ?? crypto.randomUUID();
    await runtime.repository.appendAudit({
      id: `audit-${exportId}`,
      tenantId: actor.tenantId,
      actorUserId: actor.accountUserId,
      action: "owner.exported",
      targetKind: "owner_export",
      targetId: exportId,
      occurredAt: now,
      createdAt: now,
      details: { filters: publicFilters, employeeRef, rowCount: dashboard.records.length },
    });
    const employeeLabels = new Map(directory.employees.map((employee) => [employee.userId, employee.displayLabel]));
    const safeDashboard = { ...dashboard, records: dashboard.records.map((record) => ({ ...record, employeeLabel: employeeLabels.get(record.employeeUserId) ?? "已停用员工" })) };
    return noStore(new Response(exportOwnerCsv(safeDashboard), {
      headers: {
        "Content-Disposition": `attachment; filename="owner-supervision-${now.slice(0, 10)}.csv"`,
        "Content-Type": "text/csv; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
    }));
  } catch (error) {
    return noStore(supervisionApiErrorResponse(error));
  }
}
