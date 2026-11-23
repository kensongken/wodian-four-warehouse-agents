declare module "cloudflare:workers" {
  const env: {
    APP_ENV?: string;
    DB: import("./src/server/supervision/d1-repository").D1DatabaseLike;
    FILES: import("./src/server/supervision/r2-vault").R2BucketLike;
    SUPERVISION_USAGE_TOKEN_SECRET: string;
    SUPERVISION_IDEMPOTENCY_KEYRING: string;
    SUPERVISION_RETENTION_SECRET: string;
    PRODUCT_STRATEGY_SIGNING_SECRET: string;
    PRODUCT_IMAGE_ENGINE_BASE_URL: string;
    PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE?: string;
  };
  export { env };
}
