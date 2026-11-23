import {
  configureSupervisionApiRuntime,
  type IdempotencyKeyring,
} from "@/server/supervision/customer-service";
import {
  D1ProductStrategyReceiptRepository,
  MemoryProductStrategyReceiptRepository,
} from "@/server/product-strategy/repository";
import {
  configureProductStrategyRuntime,
  isExplicitLocalSyntheticProductStrategyMode,
} from "@/server/product-strategy/runtime";
import { HttpProductStrategyInputFetcher } from "@/server/product-strategy/fetcher";
import { R2VaultStorage } from "@/server/supervision/r2-vault";

export async function register(): Promise<void> {
  let env: typeof import("cloudflare:workers").env;
  try {
    ({ env } = await import("cloudflare:workers"));
  } catch (error) {
    if (process.env.NEXT_RUNTIME === "nodejs") return;
    throw error;
  }
  if (
    isExplicitLocalSyntheticProductStrategyMode()
    || (
      env.APP_ENV === "development"
      && env.PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE === "desensitized-only"
    )
  ) {
    const repository = new MemoryProductStrategyReceiptRepository();
    configureProductStrategyRuntime(() => ({
      repository,
      fetcher: new HttpProductStrategyInputFetcher(
        env.PRODUCT_IMAGE_ENGINE_BASE_URL,
        env.PRODUCT_STRATEGY_SIGNING_SECRET,
        () => Math.floor(Date.now() / 1_000),
      ),
      signingSecret: env.PRODUCT_STRATEGY_SIGNING_SECRET,
      nowSeconds: () => Math.floor(Date.now() / 1_000),
    }));
    return;
  }
  const idempotencyKeyring = JSON.parse(
    env.SUPERVISION_IDEMPOTENCY_KEYRING,
  ) as IdempotencyKeyring;
  configureSupervisionApiRuntime(() => ({
    d1: env.DB,
    vault: new R2VaultStorage(env.FILES),
    usageTokenSecret: env.SUPERVISION_USAGE_TOKEN_SECRET,
    idempotencyKeyring,
    retentionSchedulerSecret: env.SUPERVISION_RETENTION_SECRET,
  }));
  configureProductStrategyRuntime(() => ({
    repository: new D1ProductStrategyReceiptRepository(env.DB),
    fetcher: new HttpProductStrategyInputFetcher(
      env.PRODUCT_IMAGE_ENGINE_BASE_URL,
      env.PRODUCT_STRATEGY_SIGNING_SECRET,
      () => Math.floor(Date.now() / 1_000),
    ),
    signingSecret: env.PRODUCT_STRATEGY_SIGNING_SECRET,
    disallowedSecrets: [
      env.SUPERVISION_USAGE_TOKEN_SECRET,
      env.SUPERVISION_RETENTION_SECRET,
      ...idempotencyKeyring.map(({ secret }) => secret),
    ],
    nowSeconds: () => Math.floor(Date.now() / 1_000),
  }));
}
