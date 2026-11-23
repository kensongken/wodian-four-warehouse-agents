export const TEST_RUNTIME_SEPARATE_SECRETS = Object.freeze({
  idempotencyKeyring: [{
    version: "v1",
    secret: "stable-idempotency-secret-v1-with-32-bytes",
  }] as const,
  retentionSchedulerSecret: "dedicated-retention-secret-with-32-bytes",
});
