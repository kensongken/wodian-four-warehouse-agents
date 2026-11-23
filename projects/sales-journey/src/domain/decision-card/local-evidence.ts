import type { EvidenceRef } from "./types";

export const JEWELRY_CATEGORIES = [
  "沉香",
  "绿松石",
  "南红",
  "和田玉",
  "彩宝",
  "翡翠",
] as const;

export type JewelryCategory = (typeof JEWELRY_CATEGORIES)[number];

export type LocalEvidenceField =
  | "identity_bead"
  | "scan_service"
  | "reuse_or_return_process"
  | "certificate"
  | "material"
  | "after_sales_policy"
  | "price_or_payment";

export type LocalEvidenceDraft = {
  category: JewelryCategory;
  title: string;
  field: LocalEvidenceField;
  humanApproved?: boolean;
  selectedForRequest?: boolean;
};

const CATEGORY_ID: Record<JewelryCategory, string> = {
  沉香: "agarwood",
  绿松石: "turquoise",
  南红: "nanred",
  和田玉: "hetianjade",
  彩宝: "coloredgem",
  翡翠: "jadeite",
};

export function localEvidenceId(
  category: JewelryCategory,
  field: LocalEvidenceField,
): string {
  return `local-${CATEGORY_ID[category]}-${field}`;
}

/**
 * Converts a temporary browser-only registration into an approved evidence reference.
 * The title is a citation label only; it never creates a jewelry fact or sales promise.
 */
export function approvedEvidenceFromDraft(
  draft: LocalEvidenceDraft,
): EvidenceRef | undefined {
  const title = draft.title.trim();

  if (!draft.humanApproved || title === "") {
    return undefined;
  }

  return {
    id: localEvidenceId(draft.category, draft.field),
    kind: "approved_copy",
    title,
    verifiedFields: [draft.field],
    status: "approved",
  };
}

/**
 * Creates an evidence selection request. The API replaces every field except
 * the id with its server-approved registry record before generating a card.
 */
export function evidenceSelectionFromDraft(
  draft: LocalEvidenceDraft,
): EvidenceRef | undefined {
  const title = draft.title.trim();

  if (!draft.selectedForRequest || title === "") {
    return undefined;
  }

  return {
    id: localEvidenceId(draft.category, draft.field),
    kind: "approved_copy",
    title,
    verifiedFields: [draft.field],
    status: "approved",
  };
}
