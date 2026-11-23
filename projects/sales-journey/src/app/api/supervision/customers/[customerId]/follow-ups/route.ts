import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";
import {
  toEmployeeCustomerView,
  toEmployeeFollowUpReceiptView,
  toEmployeeTimelineView,
} from "@/server/supervision/employee-view";
import {
  createProductStrategyFreshnessGuard,
  ProductStrategyFreshnessUnavailableError,
  StaleProductStrategyError,
  type ProductStrategyFreshnessGuard,
} from "@/server/product-strategy/freshness";
import { getProductStrategyRuntime } from "@/server/product-strategy/runtime";

type RouteContext = Readonly<{
  params: Promise<Readonly<{ customerId: string }>>;
}>;

async function customerIdFrom(context: RouteContext): Promise<string> {
  return (await context.params).customerId;
}

const productStrategyFreshnessGuard: ProductStrategyFreshnessGuard = {
  async assertCurrent(tenantId, seal) {
    try {
      await createProductStrategyFreshnessGuard(
        getProductStrategyRuntime().repository,
      ).assertCurrent(tenantId, seal);
    } catch (error) {
      if (
        error instanceof StaleProductStrategyError
        || error instanceof ProductStrategyFreshnessUnavailableError
      ) {
        throw error;
      }
      throw new ProductStrategyFreshnessUnavailableError();
    }
  },
};

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  try {
    const runtime = getSupervisionApiRuntime();
    const actor = await requireEmployeeFromD1(request.headers, runtime.d1);
    const { customer, timeline } = await createCustomerService(runtime).getCustomerTimeline(
      actor,
      await customerIdFrom(context),
    );
    return Response.json({
      customer: toEmployeeCustomerView(customer),
      timeline: toEmployeeTimelineView(
        timeline,
        (actorUserId) => actorUserId === actor.accountUserId ? actor.email : "店员",
      ),
    });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}

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
    const result = await createCustomerService({
      ...runtime,
      productStrategyFreshnessGuard,
    }).appendFollowUp(
      actor,
      await customerIdFrom(context),
      body,
    );
    return Response.json({
      followUp: toEmployeeFollowUpReceiptView(result.followUp),
    }, { status: 201 });
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }
}
