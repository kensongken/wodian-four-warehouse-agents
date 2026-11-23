import { z } from "zod";

import { ReferenceVariantKindSchema } from "./reference-guided-editing";

export const ReferencePromptRevisionSchema = z.object({
  kind: z.literal("shorten-opening"),
  target: ReferenceVariantKindSchema,
  requestedSeconds: z.number().finite().min(0.5).max(5),
  trimMs: z.number().int().min(520).max(5_000).multipleOf(40),
}).strict().superRefine((value, ctx) => {
  const expectedTrimMs = Math.round(value.requestedSeconds * 1_000 / 40) * 40;
  if (value.trimMs !== expectedTrimMs) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["trimMs"],
      message: "reference-revision-trim-binding-mismatch",
    });
  }
});

export type ReferencePromptRevision = z.infer<typeof ReferencePromptRevisionSchema>;
