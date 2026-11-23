import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";
import {
  productStrategyDecisionSummary,
  productStrategyEvidence,
} from "@/server/product-strategy/evidence";

const strategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.synthetic.json",
), "utf8")));

it("exposes the snapshot as preview context without treating candidates as verified facts", () => {
  const evidence = productStrategyEvidence({
    tenantId: strategyInput.tenant_id,
    outboxEventId: "66666666-6666-4666-8666-666666666666",
    payloadSha256: "a".repeat(64),
    sourceEventId: "55555555-5555-4555-8555-555555555555",
    productCode: strategyInput.product_code,
    strategyInputVersion: strategyInput.strategy_input_version,
    productDataVersion: strategyInput.product_data_version,
    contentSha256: strategyInput.content_sha256,
    status: "preview_only",
    receivedAt: "2026-08-31T08:00:00.000Z",
    strategyInput,
  });

  expect(evidence.title).toContain("材质候选");
  expect(evidence.title).toContain("库存待确认");
  expect(evidence.title).toContain("仅供人工预览");
  expect(evidence.verifiedFields).toEqual([]);
});

it("builds an internal strategy summary with explicit candidate and prohibition labels", () => {
  const summary = productStrategyDecisionSummary({
    tenantId: strategyInput.tenant_id,
    outboxEventId: "66666666-6666-4666-8666-666666666666",
    payloadSha256: "a".repeat(64),
    sourceEventId: "55555555-5555-4555-8555-555555555555",
    productCode: strategyInput.product_code,
    strategyInputVersion: strategyInput.strategy_input_version,
    productDataVersion: strategyInput.product_data_version,
    contentSha256: strategyInput.content_sha256,
    status: "preview_only",
    receivedAt: "2026-08-31T08:00:00.000Z",
    strategyInput: {
      ...strategyInput,
      visual_observations: ["红色珠串，可见自然纹理"],
      selling_point_candidates: ["适合日常佩戴"],
      service_rule_summaries: ["尺寸调整需门店确认"],
      unverified_items: ["材质待人工确认"],
      forbidden_expressions: ["官方保真"],
    },
  });

  expect(summary).toContain("可用于观察：红色珠串，可见自然纹理");
  expect(summary).toContain("仅作候选：材质 南红、形态 手串");
  expect(summary).toContain("适合日常佩戴");
  expect(summary).toContain("必须人工核验：材质待人工确认");
  expect(summary).toContain("禁止对客表达：官方保真");
});
