import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, expect, it } from "vitest";

import { GET } from "@/app/api/product-strategy-inputs/route";
import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";
import { MemoryProductStrategyReceiptRepository } from "@/server/product-strategy/repository";
import { configureProductStrategyRuntime } from "@/server/product-strategy/runtime";
import { configureSupervisionApiRuntime } from "@/server/supervision/customer-service";
import type { D1DatabaseLike, D1PreparedStatementLike } from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { TEST_RUNTIME_SEPARATE_SECRETS } from "../support/supervision-runtime";

const input = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.three-stage.synthetic.json",
), "utf8")));

class MembershipD1 implements D1DatabaseLike {
  prepare(): D1PreparedStatementLike {
    const statement: D1PreparedStatementLike = {
      bind: () => statement,
      first: async () => null,
      all: async <T>() => ({ results: [{
        id: "employee-1",
        tenant_id: input.tenant_id,
        email: "employee-1@shop.test",
        role: "employee",
      } as T] }),
      run: async () => ({ success: true }),
    };
    return statement;
  }
}

let restoreProduct: (() => void) | undefined;
let restoreSupervision: (() => void) | undefined;

beforeEach(async () => {
  const repository = new MemoryProductStrategyReceiptRepository();
  await repository.accept({
    tenantId: input.tenant_id,
    outboxEventId: "88888888-8888-4888-8888-888888888888",
    payloadSha256: "a".repeat(64),
    sourceEventId: "77777777-7777-4777-8777-777777777778",
    productCode: input.product_code,
    strategyInputVersion: input.strategy_input_version,
    productDataVersion: input.product_data_version,
    contentSha256: input.content_sha256,
    status: "preview_only",
    receivedAt: "2026-08-31T08:03:00.000Z",
    strategyInput: input,
  });
  restoreProduct = configureProductStrategyRuntime(() => ({
    repository,
    fetcher: { fetch: async () => input },
    signingSecret: "synthetic-sales-delivery-secret-with-32-bytes",
    nowSeconds: () => 1_788_112_000,
  }));
  restoreSupervision = configureSupervisionApiRuntime(() => ({
    d1: new MembershipD1(),
    repository: new MemorySupervisionRepository(),
    vault: { async putText() {}, async deleteText() {} },
    usageTokenSecret: "test-only-usage-token-secret-with-32-bytes",
    ...TEST_RUNTIME_SEPARATE_SECRETS,
    now: () => "2026-08-31T08:03:00.000Z",
  }));
});

afterEach(() => {
  restoreProduct?.();
  restoreSupervision?.();
});

it("returns only safe latest-product summaries for the authenticated tenant", async () => {
  const response = await GET(new Request("http://localhost/api/product-strategy-inputs", {
    headers: {
      "oai-authenticated-user-id": "employee-1",
      "oai-authenticated-user-email": "employee-1@shop.test",
    },
  }));

  expect(response.status).toBe(200);
  const body = await response.json();
  expect(body.products).toEqual([{
    productCode: "SYNTHETIC-001",
    strategyInputVersion: 1,
    materialCandidate: "南红",
    formCandidate: "手串",
    status: "preview_only",
    inventoryStatus: "pending",
    priceStatus: "pending",
    visualObservations: [],
    sellingPointCandidates: [],
    serviceRuleSummaries: [],
    unverifiedItems: ["材质待人工确认", "真伪未验证", "产地未验证"],
    forbiddenExpressions: ["官方保真", "保证升值", "固定成交价"],
  }]);
  expect(JSON.stringify(body)).not.toContain("object_key");
  expect(JSON.stringify(body)).not.toContain("customer");
  expect(JSON.stringify(body)).not.toContain("cost");
  expect(JSON.stringify(body)).not.toContain("internal");
});
