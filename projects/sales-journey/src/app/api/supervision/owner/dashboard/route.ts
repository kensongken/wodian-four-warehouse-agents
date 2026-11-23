import { OwnerFilterSchema, type OwnerFilter } from "@/domain/supervision/schemas";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { getOwnerDirectory, resolveOwnerEmployeeRef, toOwnerDashboardBrowserView } from "@/server/supervision/owner-view";

function parseFilters(request: Request): OwnerFilter {
  return OwnerFilterSchema.parse(Object.fromEntries(new URL(request.url).searchParams.entries()));
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
    const directory = await getOwnerDirectory(runtime.d1, actor.tenantId, runtime.usageTokenSecret);
    const { employeeRef, ...repositoryFilters } = filters;
    const employee = employeeRef ? resolveOwnerEmployeeRef(directory, employeeRef) : undefined;
    if (employeeRef && !employee) return noStore(Response.json({ error: "INVALID_EMPLOYEE_REF" }, { status: 400 }));
    const dashboard = await runtime.repository.getOwnerDashboard({
      tenantId: actor.tenantId,
      now,
      ...repositoryFilters,
      employeeUserId: employee?.userId ?? repositoryFilters.employeeUserId,
    });
    return noStore(Response.json({ dashboard: toOwnerDashboardBrowserView(dashboard, directory) }));
  } catch (error) {
    return noStore(supervisionApiErrorResponse(error));
  }
}
