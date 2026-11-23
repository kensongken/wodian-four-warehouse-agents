export const SYNTHETIC_LOCAL_ACCEPTANCE_VALUE = "desensitized-only";

/**
 * One explicit gate for demo-only identity, process memory, and evidence.
 * A production build can never opt in, even if the acceptance flag is set.
 */
export function isExplicitSyntheticLocalMode(): boolean {
  return process.env.NODE_ENV === "development"
    && process.env.SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE
      === SYNTHETIC_LOCAL_ACCEPTANCE_VALUE;
}
