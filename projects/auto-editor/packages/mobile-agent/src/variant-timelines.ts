import type { VariantPlan } from "@wearable/contracts";

/** Ordered source cuts define distinct edits; labels, captions and role names do not. */
export function assertDifferentTimelines(variants: readonly VariantPlan[]): void {
  const kinds = new Set(variants.map((variant) => variant.kind));
  const signatures = variants.map((variant) => JSON.stringify(
    variant.timeline.map(({ segmentId, inMs, outMs }) => ({ segmentId, inMs, outMs })),
  ));
  if (variants.length !== 4 || kinds.size !== 4 || !kinds.has("editorial-master") ||
    !kinds.has("xiaohongshu") || !kinds.has("douyin") || !kinds.has("wechat-channels") ||
    variants.some((variant) => variant.timeline.length === 0) || new Set(signatures).size !== 4) {
    throw new Error("variant-timelines-must-differ");
  }
}
