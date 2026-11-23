import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

import { SalesStrategyInputUpdatedV1Schema } from "@/domain/product-strategy/schema";

it("pins the sender-owned SalesStrategyInputUpdatedV1 schema fingerprint", () => {
  const schema = readFileSync(
    resolve(
      process.cwd(),
      "contracts/integrations/sales-strategy-input-updated.v1.schema.json",
    ),
  );

  expect(createHash("sha256").update(schema).digest("hex")).toBe(
    "e9cdfe762999c8b7a3688023c45e5463822337d6ede1183d75c9400a3595e493",
  );
});

it("pins and validates the sender-owned synthetic notification", () => {
  const fixture = readFileSync(
    resolve(
      process.cwd(),
      "fixtures/integrations/sales-strategy-input-updated.synthetic.json",
    ),
  );

  expect(createHash("sha256").update(fixture).digest("hex")).toBe(
    "ab0b7b1e9664c7f8ecaf6b6c4970b342cad484f398b483ffd91c01fde3a714ee",
  );
  expect(SalesStrategyInputUpdatedV1Schema.parse(JSON.parse(fixture.toString()))).toMatchObject({
    tenant_id: "22222222-2222-4222-8222-222222222222",
    product_code: "SYNTHETIC-001",
    status: "preview_only",
  });
});
