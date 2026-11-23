import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

it("creates a tenant-scoped immutable product strategy receipt ledger", () => {
  const sql = readFileSync(
    resolve(process.cwd(), "drizzle/0005_product_strategy_receipts.sql"),
    "utf8",
  );

  expect(sql).toContain("CREATE TABLE product_strategy_receipts");
  expect(sql).toContain("PRIMARY KEY (tenant_id, outbox_event_id)");
  expect(sql).toContain("CHECK (status = 'preview_only')");
  expect(sql).toContain("CREATE INDEX idx_product_strategy_receipts_product_version");
  expect(sql).toContain("(tenant_id, product_code, strategy_input_version)");
});

it("adds the full strategy snapshot without rewriting migration 005", () => {
  const sql = readFileSync(
    resolve(process.cwd(), "drizzle/0006_product_strategy_snapshot.sql"),
    "utf8",
  );

  expect(sql).toContain("ALTER TABLE product_strategy_receipts");
  expect(sql).toContain("strategy_input_json TEXT NOT NULL");
});
