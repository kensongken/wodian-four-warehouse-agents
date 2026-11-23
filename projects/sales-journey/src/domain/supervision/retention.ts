import { calculateVaultDeleteAfter, type IsoTimestamp } from "./contracts";

/** Single public retention boundary; the persisted-record schema enforces the same helper. */
export function calculateDeleteAfter(uploadedAt: IsoTimestamp): IsoTimestamp {
  return calculateVaultDeleteAfter(uploadedAt);
}
