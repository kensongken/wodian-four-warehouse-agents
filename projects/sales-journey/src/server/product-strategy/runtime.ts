import {
  MemoryProductStrategyReceiptRepository,
  type ProductStrategyReceiptRepository,
} from "./repository";
import {
  HttpProductStrategyInputFetcher,
  type ProductStrategyInputFetcher,
} from "./fetcher";

type ProductStrategyRuntime = Readonly<{
  repository: ProductStrategyReceiptRepository;
  fetcher: ProductStrategyInputFetcher;
  signingSecret: string;
  disallowedSecrets?: readonly string[];
  nowSeconds: () => number;
}>;

type RuntimeFactory = () => ProductStrategyRuntime | undefined;

let runtimeFactory: RuntimeFactory | undefined;
const localSyntheticRepository = new MemoryProductStrategyReceiptRepository();

export function isExplicitLocalSyntheticProductStrategyMode(): boolean {
  return process.env.NODE_ENV === "development"
    && process.env.PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE === "desensitized-only";
}

function getLocalSyntheticRuntime(): ProductStrategyRuntime | undefined {
  if (!isExplicitLocalSyntheticProductStrategyMode()) return undefined;
  return {
    repository: localSyntheticRepository,
    fetcher: new HttpProductStrategyInputFetcher(
      process.env.PRODUCT_IMAGE_ENGINE_BASE_URL ?? "",
      process.env.PRODUCT_STRATEGY_SIGNING_SECRET ?? "",
      () => Math.floor(Date.now() / 1_000),
    ),
    signingSecret: process.env.PRODUCT_STRATEGY_SIGNING_SECRET ?? "",
    nowSeconds: () => Math.floor(Date.now() / 1_000),
  };
}

export class ProductStrategyRuntimeUnavailableError extends Error {
  constructor() {
    super("Product strategy runtime unavailable.");
    this.name = "ProductStrategyRuntimeUnavailableError";
  }
}

export function configureProductStrategyRuntime(
  factory: RuntimeFactory | undefined,
): () => void {
  const previous = runtimeFactory;
  runtimeFactory = factory;
  return () => {
    if (runtimeFactory === factory) runtimeFactory = previous;
  };
}

export function getProductStrategyRuntime(): ProductStrategyRuntime {
  const runtime = runtimeFactory?.() ?? getLocalSyntheticRuntime();
  if (
    !runtime
    || typeof runtime.repository?.accept !== "function"
    || typeof runtime.fetcher?.fetch !== "function"
    || new TextEncoder().encode(runtime.signingSecret).byteLength < 32
    || runtime.disallowedSecrets?.includes(runtime.signingSecret)
    || typeof runtime.nowSeconds !== "function"
  ) {
    throw new ProductStrategyRuntimeUnavailableError();
  }
  return runtime;
}
