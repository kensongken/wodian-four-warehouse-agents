import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import { toEmployeeCustomerView } from "@/server/supervision/employee-view";

async function readJson(request: Request): Promise<unknown> {
  try {
    return await request.json();
  } catch {
    throw new SyntaxError("invalid JSON");
  }
}

export async function POST(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    const body = await readJson(request);
    const service = createCustomerService(runtime);
    const { customer } = await service.createCustomer(actor, body);
    return Response.json({ customer: toEmployeeCustomerView(customer) }, { status: 201 });
  } catch (error) {
    if (error instanceof SyntaxError) {
      return Response.json({ error: "INVALID_SUPERVISION_REQUEST" }, { status: 400 });
    }
    return supervisionApiErrorResponse(error);
  }
}

export async function GET(request: Request): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    const customers = await createCustomerService(runtime).listCustomers(actor);
    return Response.json({ customers: customers.map(toEmployeeCustomerView) });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}
