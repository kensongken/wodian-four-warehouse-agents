import { expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  D1ProductStrategyReceiptRepository,
  MemoryProductStrategyReceiptRepository,
  ProductStrategyEventConflictError,
  type ProductStrategyReceipt,
} from "@/server/product-strategy/repository";
import type {
  D1DatabaseLike,
  D1PreparedStatementLike,
} from "@/server/supervision/d1-repository";
import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";

const strategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.synthetic.json",
), "utf8")));

class ReceiptD1 implements D1DatabaseLike {
  readonly rows = new Map<string, Record<string, unknown>>();

  prepare(sql: string): D1PreparedStatementLike {
    let values: unknown[] = [];
    const statement: D1PreparedStatementLike = {
      bind: (...bound) => {
        values = bound;
        return statement;
      },
      first: async <T>() => {
        if (!sql.includes("FROM product_strategy_receipts")) return null;
        return (this.rows.get(`${values[0]}\0${values[1]}`) ?? null) as T | null;
      },
      all: async <T>() => ({ results: [...this.rows.values()] as T[] }),
      run: async () => {
        if (sql.includes("INSERT INTO product_strategy_receipts")) {
          const [
            tenantId,
            outboxEventId,
            payloadSha256,
            sourceEventId,
            productCode,
            strategyInputVersion,
            productDataVersion,
            contentSha256,
            status,
            receivedAt,
            strategyInputJson,
          ] = values;
          this.rows.set(`${tenantId}\0${outboxEventId}`, {
            tenant_id: tenantId,
            outbox_event_id: outboxEventId,
            payload_sha256: payloadSha256,
            source_event_id: sourceEventId,
            product_code: productCode,
            strategy_input_version: strategyInputVersion,
            product_data_version: productDataVersion,
            content_sha256: contentSha256,
            status,
            received_at: receivedAt,
            strategy_input_json: strategyInputJson,
          });
        }
        return { success: true };
      },
    };
    return statement;
  }
}

function receipt(overrides: Partial<ProductStrategyReceipt> = {}): ProductStrategyReceipt {
  return {
    tenantId: "22222222-2222-4222-8222-222222222222",
    outboxEventId: "66666666-6666-4666-8666-666666666666",
    payloadSha256: "a".repeat(64),
    sourceEventId: "55555555-5555-4555-8555-555555555555",
    productCode: "SYNTHETIC-001",
    strategyInputVersion: 1,
    productDataVersion: 1,
    contentSha256: strategyInput.content_sha256,
    status: "preview_only",
    receivedAt: "2026-08-31T08:00:00.000Z",
    strategyInput,
    ...overrides,
  };
}

it("persists one receipt and returns an exact retry as duplicate", async () => {
  const d1 = new ReceiptD1();
  const repository = new D1ProductStrategyReceiptRepository(d1);

  const created = await repository.accept(receipt());
  const duplicate = await repository.accept(receipt());

  expect(created.outcome).toBe("created");
  expect(duplicate.outcome).toBe("duplicate");
  expect(await repository.find(receipt().tenantId, receipt().outboxEventId)).toEqual(
    receipt(),
  );
  expect(d1.rows.size).toBe(1);
});

it("rejects changed content under the same tenant and outbox event id", async () => {
  const repository = new D1ProductStrategyReceiptRepository(new ReceiptD1());
  await repository.accept(receipt());

  await expect(repository.accept(receipt({ payloadSha256: "c".repeat(64) })))
    .rejects.toBeInstanceOf(ProductStrategyEventConflictError);
});

it("scopes the same outbox event id independently by tenant", async () => {
  const d1 = new ReceiptD1();
  const repository = new D1ProductStrategyReceiptRepository(d1);

  await repository.accept(receipt());
  await repository.accept(receipt({
    tenantId: "33333333-3333-4333-8333-333333333333",
  }));

  expect(d1.rows.size).toBe(2);
});

it("lists only the latest snapshot for each product in one tenant", async () => {
  const repository = new MemoryProductStrategyReceiptRepository();
  await repository.accept(receipt());
  await repository.accept(receipt({
    outboxEventId: "77777777-7777-4777-8777-777777777777",
    strategyInputVersion: 2,
    strategyInput: { ...strategyInput, strategy_input_version: 2 },
  }));
  await repository.accept(receipt({
    outboxEventId: "88888888-8888-4888-8888-888888888888",
    productCode: "SYNTHETIC-002",
    strategyInput: { ...strategyInput, product_code: "SYNTHETIC-002" },
  }));
  await repository.accept(receipt({
    tenantId: "33333333-3333-4333-8333-333333333333",
    outboxEventId: "99999999-9999-4999-8999-999999999999",
  }));

  const results = await repository.listLatest(receipt().tenantId, 20);

  expect(results.map((item) => [item.productCode, item.strategyInputVersion])).toEqual([
    ["SYNTHETIC-001", 2],
    ["SYNTHETIC-002", 1],
  ]);
});
