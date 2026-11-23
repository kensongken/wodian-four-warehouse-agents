import type {
  ContentLibrary,
  PrivacyInvalidationOutcome,
} from "./content-library";

export type InvalidationResult = PrivacyInvalidationOutcome;

export async function withdrawSegmentPermission(
  input: Readonly<{
    segmentId: string;
    reason: string;
    actorId: string;
  }>,
  library: ContentLibrary,
): Promise<InvalidationResult> {
  if (input.reason.trim().length === 0) throw new Error("withdrawal-reason-required");
  return library.commitPrivacyInvalidation(input);
}
