import { requireOwnerFromD1 } from "@/server/auth/authorization";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import {
  getOwnerDirectory,
  getOwnerReplyQualityView,
  parseOwnerReplyQualityFilters,
  resolveOwnerEmployeeRef,
} from "@/server/supervision/owner-view";

function noStore(response: Response): Response {
  response.headers.set("Cache-Control", "no-store, private");
  return response;
}

export async function GET(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireOwnerFromD1(request.headers, runtime.d1);
    const filters = parseOwnerReplyQualityFilters(new URL(request.url).searchParams.entries());
    const directory = await getOwnerDirectory(
      runtime.d1,
      actor.tenantId,
      runtime.usageTokenSecret,
    );
    const employee = filters.employeeRef
      ? resolveOwnerEmployeeRef(directory, filters.employeeRef)
      : undefined;
    if (filters.employeeRef && !employee) {
      return noStore(Response.json({ error: "INVALID_EMPLOYEE_REF" }, { status: 400 }));
    }
    const quality = await getOwnerReplyQualityView(runtime.repository, directory, {
      tenantId: actor.tenantId,
      employeeUserId: employee?.userId,
      scenario: filters.scenario,
      salesStage: filters.salesStage,
      agentVersion: filters.agentVersion,
    });
    return noStore(Response.json({ quality }));
  } catch (error) {
    return noStore(supervisionApiErrorResponse(error));
  }
}
