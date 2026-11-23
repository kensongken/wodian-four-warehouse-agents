import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import { getProductStrategyRuntime } from "@/server/product-strategy/runtime";
import {
  getSupervisionApiRuntime,
  supervisionApiErrorResponse,
} from "@/server/supervision/customer-service";

export async function GET(request: Request): Promise<Response> {
  let tenantId: string;
  try {
    const supervision = getSupervisionApiRuntime();
    tenantId = (await requireEmployeeFromD1(request.headers, supervision.d1)).tenantId;
  } catch (error) {
    return supervisionApiErrorResponse(error);
  }

  try {
    const records = await getProductStrategyRuntime().repository.listLatest(tenantId, 100);
    return Response.json({
      products: records.map(({ strategyInput }) => ({
        productCode: strategyInput.product_code,
        strategyInputVersion: strategyInput.strategy_input_version,
        materialCandidate: strategyInput.material.value,
        formCandidate: strategyInput.form.value,
        status: strategyInput.status,
        inventoryStatus: strategyInput.inventory_status,
        priceStatus: strategyInput.price_status,
        visualObservations: strategyInput.visual_observations,
        sellingPointCandidates: strategyInput.selling_point_candidates,
        serviceRuleSummaries: strategyInput.service_rule_summaries,
        unverifiedItems: strategyInput.unverified_items,
        forbiddenExpressions: strategyInput.forbidden_expressions,
      })),
    });
  } catch {
    return Response.json({ error: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE" }, { status: 503 });
  }
}
