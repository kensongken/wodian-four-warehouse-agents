import {
  ReferencePromptRevisionSchema,
  ReferenceVariantKindSchema,
  type ReferencePromptRevision,
  type ReferenceVariantKind,
} from "@wearable/contracts";

const FRAME_DURATION_MS = 40;
const SHORTEN_OPENING_PATTERN = /^\s*开头缩短\s*(\d+(?:\.\d+)?)\s*秒\s*$/u;

export function interpretReferencePromptRevision(input: Readonly<{
  prompt: string;
  target: ReferenceVariantKind;
}>): ReferencePromptRevision {
  const target = ReferenceVariantKindSchema.safeParse(input.target);
  const match = SHORTEN_OPENING_PATTERN.exec(input.prompt);
  if (!target.success || !match) throw new Error("reference-revision-prompt-unsupported");
  const requestedSeconds = Number(match[1]);
  if (!Number.isFinite(requestedSeconds) || requestedSeconds < 0.5 || requestedSeconds > 5) {
    throw new Error("reference-revision-prompt-unsupported");
  }
  const trimMs = Math.round(requestedSeconds * 1_000 / FRAME_DURATION_MS) * FRAME_DURATION_MS;
  return ReferencePromptRevisionSchema.parse({
    kind: "shorten-opening",
    target: target.data,
    requestedSeconds,
    trimMs,
  });
}
