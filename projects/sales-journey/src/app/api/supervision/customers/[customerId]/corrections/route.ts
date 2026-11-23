import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { toEmployeeCorrectionView } from "@/server/supervision/employee-view";

type RouteContext = Readonly<{
  params: Promise<Readonly<{ customerId: string }>>;
}>;

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "INVALID_SUPERVISION_REQUEST" }, { status: 400 });
    }
    const customerId = (await context.params).customerId;
    const { correction } = await createCustomerService(runtime).appendCorrection(
      actor,
      customerId,
      body,
    );
    return Response.json({
      correction: toEmployeeCorrectionView(correction, actor.email),
    }, { status: 201 });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}
