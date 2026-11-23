import type { ProductStrategySeal } from "@/domain/decision-card/schema";
import type { ProductStrategyReceiptRepository } from "./repository";

export class StaleProductStrategyError extends Error {
  readonly status = 409;
  readonly code = "STALE_PRODUCT_STRATEGY";

  constructor() {
    super("Product strategy is no longer current.");
    this.name = "StaleProductStrategyError";
  }
}

export class ProductStrategyFreshnessUnavailableError extends Error {
  readonly status = 503;
  readonly code = "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE";

  constructor() {
    super("Product strategy freshness unavailable.");
    this.name = "ProductStrategyFreshnessUnavailableError";
  }
}

export type ProductStrategyFreshnessGuard = Readonly<{
  assertCurrent(tenantId: string, seal: ProductStrategySeal): Promise<void>;
}>;

export function createProductStrategyFreshnessGuard(
  repository: ProductStrategyReceiptRepository,
): ProductStrategyFreshnessGuard {
  return {
    async assertCurrent(tenantId, seal) {
      let latest;
      try {
        latest = await repository.findLatest(tenantId, seal.productCode);
      } catch {
        throw new ProductStrategyFreshnessUnavailableError();
      }
      if (
        !latest
        || latest.strategyInputVersion !== seal.strategyInputVersion
        || latest.contentSha256 !== seal.contentSha256
        || latest.status !== seal.status
      ) {
        throw new StaleProductStrategyError();
      }
    },
  };
}
