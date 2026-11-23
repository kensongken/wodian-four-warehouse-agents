import { SalesStrategyInputUpdatedV1Schema } from "@/domain/product-strategy/schema";
import {
  ProductStrategyEventConflictError,
} from "@/server/product-strategy/repository";
import {
  getProductStrategyRuntime,
  ProductStrategyRuntimeUnavailableError,
} from "@/server/product-strategy/runtime";
import {
  sha256Hex,
  verifyProductImageEngineSignature,
} from "@/server/product-strategy/signature";
import { ProductStrategyFetchError } from "@/server/product-strategy/fetcher";

const PATH = "/api/integrations/product-image-engine/events";
const TOPIC = "sales.strategy.input.updated.v1";

function error(code: string, status: number): Response {
  return Response.json({ error: code }, { status });
}

export async function POST(request: Request): Promise<Response> {
  let runtime;
  try {
    runtime = getProductStrategyRuntime();
  } catch (runtimeError) {
    if (runtimeError instanceof ProductStrategyRuntimeUnavailableError) {
      return error("PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE", 503);
    }
    throw runtimeError;
  }

  const sourceSystem = request.headers.get("X-Source-System") ?? "";
  const tenantId = request.headers.get("X-Tenant-Id") ?? "";
  const topic = request.headers.get("X-Event-Topic") ?? "";
  const outboxEventId = request.headers.get("X-Outbox-Event-Id") ?? "";
  const timestampText = request.headers.get("X-Signature-Timestamp") ?? "";
  const suppliedSignature = request.headers.get("X-Signature") ?? "";
  const body = await request.text();
  const timestamp = Number(timestampText);

  const validSignature = sourceSystem === "product-image-engine"
    && await verifyProductImageEngineSignature({
      secret: runtime.signingSecret,
      timestamp,
      nowSeconds: runtime.nowSeconds(),
      sourceSystem,
      tenantId,
      method: "POST",
      path: PATH,
      body,
      suppliedSignature,
    });
  if (!validSignature) return error("INVALID_SOURCE_SIGNATURE", 401);
  if (topic !== TOPIC) return error("UNSUPPORTED_EVENT_TOPIC", 422);
  if (!/^[0-9a-f-]{36}$/u.test(outboxEventId)) {
    return error("INVALID_OUTBOX_EVENT_ID", 422);
  }

  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return error("INVALID_STRATEGY_NOTIFICATION", 422);
  }
  const parsed = SalesStrategyInputUpdatedV1Schema.safeParse(payload);
  if (!parsed.success) return error("INVALID_STRATEGY_NOTIFICATION", 422);
  if (parsed.data.tenant_id !== tenantId) return error("TENANT_SCOPE_MISMATCH", 403);

  const payloadSha256 = await sha256Hex(body);
  try {
    const existing = await runtime.repository.find(tenantId, outboxEventId);
    if (existing) {
      if (existing.payloadSha256 !== payloadSha256) {
        throw new ProductStrategyEventConflictError();
      }
      return Response.json({
        accepted: true,
        outcome: "duplicate",
        outboxEventId: existing.outboxEventId,
        tenantId: existing.tenantId,
        productCode: existing.productCode,
        strategyInputVersion: existing.strategyInputVersion,
        status: existing.status,
      }, { status: 200 });
    }
    const strategyInput = await runtime.fetcher.fetch(
      tenantId,
      parsed.data.product_code,
      parsed.data.strategy_input_version,
    );
    if (
      strategyInput.tenant_id !== tenantId
      || strategyInput.product_code !== parsed.data.product_code
      || strategyInput.strategy_input_version !== parsed.data.strategy_input_version
      || strategyInput.product_data_version !== parsed.data.product_data_version
      || strategyInput.content_sha256 !== parsed.data.content_sha256
      || strategyInput.status !== parsed.data.status
    ) return error("STRATEGY_INPUT_IDENTITY_MISMATCH", 409);
    const result = await runtime.repository.accept({
      tenantId,
      outboxEventId,
      payloadSha256,
      sourceEventId: parsed.data.event_id,
      productCode: parsed.data.product_code,
      strategyInputVersion: parsed.data.strategy_input_version,
      productDataVersion: parsed.data.product_data_version,
      contentSha256: parsed.data.content_sha256,
      status: parsed.data.status,
      receivedAt: new Date(runtime.nowSeconds() * 1000).toISOString(),
      strategyInput,
    });
    return Response.json({
      accepted: true,
      outcome: result.outcome,
      outboxEventId: result.record.outboxEventId,
      tenantId: result.record.tenantId,
      productCode: result.record.productCode,
      strategyInputVersion: result.record.strategyInputVersion,
      status: result.record.status,
    }, { status: result.outcome === "created" ? 202 : 200 });
  } catch (repositoryError) {
    if (repositoryError instanceof ProductStrategyFetchError) {
      return error(
        repositoryError.code === "unavailable"
          ? "STRATEGY_INPUT_SOURCE_UNAVAILABLE"
          : "INVALID_STRATEGY_INPUT",
        repositoryError.code === "unavailable" ? 503 : 422,
      );
    }
    if (repositoryError instanceof ProductStrategyEventConflictError) {
      return error("EVENT_IDENTITY_CONFLICT", 409);
    }
    throw repositoryError;
  }
}
