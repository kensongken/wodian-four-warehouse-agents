export const AssetType = [
  "product-image",
  "handmade",
  "scene",
  "persona-copy",
  "customer-interaction",
] as const;

export type AssetType = (typeof AssetType)[number];
export type ProductAssociationStatus =
  | "unassigned"
  | "suggested"
  | "confirmed"
  | "rejected";

export type AssetRecord = Readonly<{
  id: string;
  sessionId: string;
  type: AssetType;
  sceneTags: string[];
  privacyState: "creator" | "visitor";
  publicUse: "allowed" | "review-required" | "blocked";
  source: Readonly<{
    kind: "glass3-local-original" | "capture-session-segments";
    digests: string[];
  }>;
  productAssociation: Readonly<{
    status: ProductAssociationStatus;
    suggestedProductId: string | null;
    confirmedProductId: string | null;
  }>;
}>;

export function createAssetRecord(input: {
  sessionId: string;
  type: AssetType;
  sceneTags?: string[];
  privacyState: "creator" | "visitor";
  source: AssetRecord["source"];
  suggestedProductId: string | null;
}): AssetRecord {
  const suggestedProductId = input.suggestedProductId;
  return {
    id: `asset_${input.sessionId}_${input.source.digests.join("_")}`,
    sessionId: input.sessionId,
    type: input.type,
    sceneTags: [...(input.sceneTags ?? [])],
    privacyState: input.privacyState,
    publicUse: input.privacyState === "visitor" ? "review-required" : "allowed",
    source: { kind: input.source.kind, digests: [...input.source.digests] },
    productAssociation: {
      status: suggestedProductId ? "suggested" : "unassigned",
      suggestedProductId,
      confirmedProductId: null,
    },
  };
}

/** Only a designer-driven confirmation can set a product ID as confirmed. */
export function confirmProductAssociation(
  asset: AssetRecord,
  productId: string,
): AssetRecord {
  if (!productId.trim()) {
    throw new Error("product-id-required");
  }

  return {
    ...asset,
    productAssociation: {
      status: "confirmed",
      suggestedProductId: asset.productAssociation.suggestedProductId,
      confirmedProductId: productId,
    },
  };
}

export function rejectProductAssociation(asset: AssetRecord): AssetRecord {
  return {
    ...asset,
    productAssociation: {
      status: "rejected",
      suggestedProductId: asset.productAssociation.suggestedProductId,
      confirmedProductId: null,
    },
  };
}

export function canExportToPublic(asset: AssetRecord): boolean {
  return asset.publicUse === "allowed";
}

export function canRouteToProductSystems(asset: AssetRecord): boolean {
  return asset.productAssociation.status === "confirmed";
}

export function isAvailableToStoryLibrary(asset: AssetRecord): boolean {
  return asset.publicUse !== "blocked";
}
