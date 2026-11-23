import { z } from "zod";

export const CaptureModeSchema = z.enum([
  "daily-pendant",
  "creator-necklace",
  "maker-brooch",
]);

export const PrivacyStateSchema = z.enum(["creator", "visitor", "paused"]);

export const CaptureSessionSchema = z
  .object({
    id: z.string().min(1),
    startedAt: z.string().datetime(),
    mode: CaptureModeSchema,
    privacyState: PrivacyStateSchema,
    visitorConsent: z.enum(["not-required", "confirmed", "declined"]).optional(),
    status: z.enum(["recording", "stopped", "sync-pending", "synced"]),
    productRef: z.string().min(1).nullable(),
  })
  .superRefine((value, ctx) => {
    if (value.privacyState === "visitor" && value.visitorConsent !== "confirmed") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "visitor mode requires confirmed consent",
      });
    }

    if (value.privacyState === "paused" && value.status === "recording") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "paused mode cannot record",
      });
    }
  });

export type CaptureMode = z.infer<typeof CaptureModeSchema>;
export type PrivacyState = z.infer<typeof PrivacyStateSchema>;
export type CaptureSession = z.infer<typeof CaptureSessionSchema>;
