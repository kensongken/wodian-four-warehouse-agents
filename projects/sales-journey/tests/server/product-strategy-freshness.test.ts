import { describe, expect, it } from "vitest";

import type { ProductStrategySeal } from "@/domain/decision-card/schema";
import {
  createProductStrategyFreshnessGuard,
} from "@/server/product-strategy/freshness";
import {
  MemoryProductStrategyReceiptRepository,
  type ProductStrategyReceipt,
  type ProductStrategyReceiptRepository,
} from "@/server/product-strategy/repository";

const seal: ProductStrategySeal = {
  productCode: "SYNTHETIC-001",
  strategyInputVersion: 2,
  contentSha256: "a".repeat(64),
  status: "preview_only",
};

function receipt(overrides: Partial<ProductStrategyReceipt> = {}): ProductStrategyReceipt {
  return {
    tenantId: "tenant-a",
    outboxEventId: "66666666-6666-4666-8666-666666666666",
    payloadSha256: "b".repeat(64),
    sourceEventId: "55555555-5555-4555-8555-555555555555",
    productCode: seal.productCode,
    strategyInputVersion: seal.strategyInputVersion,
    productDataVersion: 1,
    contentSha256: seal.contentSha256,
    status: "preview_only",
    receivedAt: "2026-08-31T00:00:00.000Z",
    strategyInput: {} as ProductStrategyReceipt["strategyInput"],
    ...overrides,
  };
}

describe("product strategy freshness guard", () => {
  it("accepts the same tenant, version, and content fingerprint", async () => {
    const repository = new MemoryProductStrategyReceiptRepository();
    await repository.accept(receipt());

    await expect(createProductStrategyFreshnessGuard(repository)
      .assertCurrent("tenant-a", seal)).resolves.toBeUndefined();
  });

  it.each([
    ["older version", { strategyInputVersion: 1 }],
    ["different content", { contentSha256: "c".repeat(64) }],
  ] as const)("rejects a stale seal with %s", async (_case, sealOverride) => {
    const repository = new MemoryProductStrategyReceiptRepository();
    await repository.accept(receipt());

    await expect(createProductStrategyFreshnessGuard(repository).assertCurrent(
      "tenant-a",
      { ...seal, ...sealOverride },
    )).rejects.toMatchObject({ status: 409, code: "STALE_PRODUCT_STRATEGY" });
  });

  it("does not use another tenant's matching product", async () => {
    const repository = new MemoryProductStrategyReceiptRepository();
    await repository.accept(receipt());

    await expect(createProductStrategyFreshnessGuard(repository)
      .assertCurrent("tenant-b", seal))
      .rejects.toMatchObject({ status: 409, code: "STALE_PRODUCT_STRATEGY" });
  });

  it("maps repository failure to a detail-free unavailable error", async () => {
    const repository = {
      findLatest: async () => {
        throw new Error("internal database address");
      },
    } as unknown as ProductStrategyReceiptRepository;

    await expect(createProductStrategyFreshnessGuard(repository)
      .assertCurrent("tenant-a", seal)).rejects.toMatchObject({
        status: 503,
        code: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE",
        message: "Product strategy freshness unavailable.",
      });
  });
});
