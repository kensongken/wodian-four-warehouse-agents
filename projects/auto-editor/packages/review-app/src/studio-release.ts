export const STUDIO_RELEASE = Object.freeze({
  version: "1.0.0",
  label: "本地合成候选",
  boundary: "local-synthetic-candidate",
  production: false,
  deployed: false,
} as const);

export type StudioReleaseMetadata = typeof STUDIO_RELEASE;
