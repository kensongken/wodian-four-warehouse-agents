export const MAX_EXPIRED_VAULT_ASSET_LIMIT = 500;

export function normalizeExpiredVaultAssetLimit(limit: number): number {
  if (!Number.isFinite(limit) || limit <= 0) return 0;
  return Math.min(Math.trunc(limit), MAX_EXPIRED_VAULT_ASSET_LIMIT);
}
