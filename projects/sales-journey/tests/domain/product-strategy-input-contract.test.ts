import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

import {
  canonicalSalesStrategyInputSha256,
  SalesStrategyInputV1Schema,
} from "@/domain/product-strategy/input-schema";

const contractPath = resolve(
  process.cwd(),
  "contracts/integrations/sales-strategy-input.v1.schema.json",
);
const fixturePath = resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.synthetic.json",
);

it("pins the full sender-owned SalesStrategyInputV1 contract and fixture", async () => {
  const contract = readFileSync(contractPath);
  const fixture = readFileSync(fixturePath);
  const parsed = SalesStrategyInputV1Schema.parse(JSON.parse(fixture.toString()));

  expect(createHash("sha256").update(contract).digest("hex")).toBe(
    "c2de9482f1fcb1459e8f247b42341d348bd3b772ea70dd476dce274c15eb7055",
  );
  expect(createHash("sha256").update(fixture).digest("hex")).toBe(
    "98c1665e35c67d46564af206aeeda48220d471b05985a80abcfd1d01b6632a26",
  );
  expect(await canonicalSalesStrategyInputSha256(parsed)).toBe(parsed.content_sha256);
});

it("rejects sales_ready and extra customer data", () => {
  const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));

  expect(SalesStrategyInputV1Schema.safeParse({
    ...fixture,
    status: "sales_ready",
    customer_name: "不应出现",
  }).success).toBe(false);
});
