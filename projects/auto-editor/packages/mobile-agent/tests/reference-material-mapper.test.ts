import { createHash } from "node:crypto";

import type {
  MaterialCard,
  ReferenceMaterialMappingPlan,
  ReferenceRightsDeclaration,
  ReferenceSample,
  ReferenceStyleAnalysis,
  ReferenceStyleSelection,
  StudioMediaAsset,
} from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import {
  confirmReferenceMaterialMappingPlan,
  createReferenceMaterialMappingPlan,
  type ReferenceMappingAssetSnapshot,
} from "../src/reference-material-mapper";
import type { StudioScope } from "../src/studio-capabilities";

const createdAt = "2026-09-19T08:00:00.000Z";
const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};

describe("reference material mapper", () => {
  it("prefers current material and never emits a capture task", () => {
    const input = mappingInput();
    const currentMatch = card("current", {
      observations: ["usable video"],
      confirmedFacts: ["reference-rhythm:rule-rhythm-1"],
    });
    const betterHistory = card("history", {
      sessionId: "older-session",
      observations: ["rhythm", "tight", "opening", "fast", "precise"],
      confirmedFacts: ["reference-rhythm:rule-rhythm-1", "tight opening"],
    });

    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [currentMatch],
      authorizedHistoricalMaterials: [betterHistory],
      assets: [
        assetSnapshot(currentMatch),
        assetSnapshot(betterHistory, { permissionSessionId: scope.sessionId }),
      ],
    });

    expect(plan.mappings[0]).toMatchObject({
      status: "matched",
      sourceAssetId: currentMatch.sourceAssetId,
      sourceProvenance: "customer-current",
    });
    expect(JSON.stringify(plan)).not.toMatch(/capture|reshoot|shotPlan/i);
  });

  it("marks an unsupported rule unachievable instead of inventing a source", () => {
    const plan = createReferenceMaterialMappingPlan(mappingInput());

    expect(plan.mappings[0]).toMatchObject({
      status: "unachievable",
      sourceBindingId: null,
      sourceAssetId: null,
      sourceDigest: null,
      permission: null,
      differenceReason: "no-authorized-material",
    });
  });

  it("uses a safe partial-content candidate only as an explicit substitution", () => {
    const candidate = card("substitute", {
      observations: [],
      confirmedFacts: [],
    });

    const plan = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [candidate],
      assets: [assetSnapshot(candidate)],
    });

    expect(plan.mappings[0]).toMatchObject({
      status: "substituted",
      sourceAssetId: candidate.sourceAssetId,
      differenceReason: "authorized-material-content-differs",
    });
  });

  it.each(["video", "audio"] as const)(
    "keeps an unconfirmed audio rule unachievable instead of substituting a generic %s asset",
    (mediaKind) => {
      const input = mappingInput({
        ruleKind: "audio",
        ruleDescription: "align edits to audio peaks",
      });
      const unconfirmed = card(`unconfirmed-audio-${mediaKind}`, {
        confirmedFacts: [],
      });

      const plan = createReferenceMaterialMappingPlan({
        ...input,
        currentMaterials: [unconfirmed],
        assets: [assetSnapshot(unconfirmed, { mediaKind })],
      });

      expect(plan.mappings[0]).toMatchObject({
        status: "unachievable",
        sourceBindingId: null,
        sourceAssetId: null,
        sourceDigest: null,
        permission: null,
        differenceReason: "no-authorized-material",
      });
    },
  );

  it.each([
    ["rhythm", "rhythm tight opening"],
    ["narrative", "designer explains the ring"],
    ["transition", "fade between scenes"],
  ] as const)("never treats real technical observations as matched %s evidence", (kind, description) => {
    const input = mappingInput({ ruleKind: kind, ruleDescription: description, transitionKind: "fade" });
    const technicalOnly = card(`technical-${kind}`, {
      observations: [
        "duration-ms:12000",
        "video-stream:present",
        "audio-stream:present",
        "frame-size:1920x1080",
        "video-codec:h264",
        "audio-codec:aac",
        "scene-count:1",
      ],
      confirmedFacts: [],
    });

    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [technicalOnly],
      assets: [assetSnapshot(technicalOnly)],
    });

    expect(plan.mappings[0]).toMatchObject({
      status: "substituted",
      differenceReason: "authorized-material-content-differs",
    });
  });

  it("does not treat a confirmed cut as evidence for the selected fade rule", () => {
    const input = mappingInput({
      ruleKind: "transition",
      ruleDescription: "fade between scenes",
      transitionKind: "fade",
    });
    const cutOnly = card("cut-only", {
      confirmedFacts: ["reference-transition:cut:rule-transition-1"],
    });
    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [cutOnly],
      assets: [assetSnapshot(cutOnly)],
    });

    expect(plan.mappings[0]?.status).toBe("substituted");
  });

  it.each([
    ["rhythm", "rhythm tight opening", "reference-rhythm:rule-rhythm-1", undefined],
    ["transition", "fade between scenes", "reference-transition:fade:rule-transition-1", "fade"],
    ["narrative", "designer explains the ring", "reference-narrative:rule-narrative-1", undefined],
    ["audio", "align edits to audio peaks", "reference-audio:rule-audio-1", undefined],
  ] as const)("matches %s only with exact rule-bound evidence", (kind, description, fact, transitionKind) => {
    const input = mappingInput({ ruleKind: kind, ruleDescription: description, transitionKind });
    const exact = card(`exact-${kind}`, { confirmedFacts: [fact] });
    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [exact],
      assets: [assetSnapshot(exact)],
    });

    expect(plan.mappings[0]).toMatchObject({ status: "matched", sourceAssetId: exact.sourceAssetId });
  });

  it.each([
    ["cross-customer card", () => ({
      currentMaterials: [card("foreign-card", { customerId: "customer-b" })],
      assets: [assetSnapshot(card("foreign-card"))],
    })],
    ["cross-customer asset", () => {
      const candidate = card("foreign-asset");
      return {
        currentMaterials: [candidate],
        assets: [assetSnapshot(candidate, { assetCustomerId: "customer-b", permissionCustomerId: "customer-b" })],
      };
    }],
    ["withdrawn asset", () => {
      const candidate = card("withdrawn");
      return { currentMaterials: [candidate], assets: [assetSnapshot(candidate, { withdrawn: true })] };
    }],
    ["unverified digest", () => {
      const candidate = card("unverified");
      return {
        currentMaterials: [candidate],
        assets: [assetSnapshot(candidate, { verifiedDigest: digest("different-bytes") })],
      };
    }],
    ["stale permission", () => {
      const candidate = card("stale-permission");
      return {
        currentMaterials: [candidate],
        assets: [assetSnapshot(candidate, { permissionSourceDigest: digest("old-permission") })],
      };
    }],
  ])("rejects a %s candidate", (_name, makeOverride) => {
    const override = makeOverride();
    const plan = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      ...override,
      authorizedHistoricalMaterials: [],
    });

    expect(plan.mappings[0]?.status).toBe("unachievable");
  });

  it("never reuses the external reference sample even when it appears in ordinary inputs", () => {
    const input = mappingInput();
    const relabeled = card("relabeled-reference", {
      sourceAssetId: input.sample.sourceAssetId,
      sourceDigest: input.sample.sourceDigest,
    });
    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [relabeled],
      assets: [assetSnapshot(relabeled)],
    });

    expect(plan.mappings[0]?.status).toBe("unachievable");
  });

  it("requires the exact self-owned video-clips grant before reusing the reference", () => {
    const withoutGrant = mappingInput({ sourceKind: "self-owned", reusableElements: [] });
    const referenceCard = card("self-owned-reference", {
      sourceAssetId: withoutGrant.sample.sourceAssetId,
      sourceDigest: withoutGrant.sample.sourceDigest,
    });
    const withoutGrantPlan = createReferenceMaterialMappingPlan({
      ...withoutGrant,
      currentMaterials: [referenceCard],
      assets: [assetSnapshot(referenceCard)],
    });
    expect(withoutGrantPlan.mappings[0]?.status).toBe("unachievable");

    const withGrant = mappingInput({ sourceKind: "self-owned", reusableElements: ["video-clips"] });
    const allowedPlan = createReferenceMaterialMappingPlan({
      ...withGrant,
      currentMaterials: [referenceCard],
      assets: [assetSnapshot(referenceCard)],
    });
    expect(allowedPlan.mappings[0]).toMatchObject({
      sourceAssetId: referenceCard.sourceAssetId,
      sourceProvenance: "self-owned-reference",
      reuseRights: { id: withGrant.rights.id, version: withGrant.rights.version, element: "video-clips" },
    });
  });

  it("binds self-owned audio rules to audio-track rights without relabeling them", () => {
    const input = mappingInput({
      sourceKind: "self-owned",
      reusableElements: ["audio-track"],
      ruleKind: "audio",
      ruleDescription: "align edits to audio peaks",
    });
    const referenceCard = card("self-owned-audio", {
      sourceAssetId: input.sample.sourceAssetId,
      sourceDigest: input.sample.sourceDigest,
      confirmedFacts: ["reference-audio:rule-audio-1"],
    });
    const plan = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [referenceCard],
      assets: [assetSnapshot(referenceCard)],
    });
    expect(plan.mappings[0]).toMatchObject({
      sourceProvenance: "self-owned-reference",
      reuseRights: { id: input.rights.id, version: input.rights.version, element: "audio-track" },
    });
  });

  it("uses value identity and stable tie-breaking so retries produce the same plan", () => {
    const alpha = card("alpha");
    const beta = card("beta");
    const first = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [structuredClone(beta), structuredClone(alpha)],
      assets: [assetSnapshot(beta), assetSnapshot(alpha)],
    });
    const second = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [structuredClone(alpha), structuredClone(beta)],
      authorizedHistoricalMaterials: [structuredClone(alpha)],
      assets: [assetSnapshot(alpha), assetSnapshot(beta)],
    });

    expect(first).toEqual(second);
    expect(first.mappings[0]?.sourceAssetId).toBe(alpha.sourceAssetId);
  });

  it("uses stable source identity before runtime-dependent card ids when evidence ties", () => {
    const alphaSource = card("alpha", { id: "card-z-runtime" });
    const betaSource = card("beta", { id: "card-a-runtime" });

    const plan = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [betaSource, alphaSource],
      assets: [assetSnapshot(betaSource), assetSnapshot(alphaSource)],
    });

    expect(plan.mappings[0]?.sourceAssetId).toBe(alphaSource.sourceAssetId);
  });

  it.each([
    ["current/current same id", (left: MaterialCard, right: MaterialCard) => ({
      currentMaterials: [left, right], authorizedHistoricalMaterials: [],
    })],
    ["history/history same source interval", (left: MaterialCard, right: MaterialCard) => ({
      currentMaterials: [],
      authorizedHistoricalMaterials: [
        { ...left, sessionId: "older-session" },
        { ...right, id: "card-other-id", sessionId: "older-session" },
      ],
    })],
    ["current/history same id", (left: MaterialCard, right: MaterialCard) => ({
      currentMaterials: [left], authorizedHistoricalMaterials: [{ ...right, sessionId: "older-session" }],
    })],
  ] as const)("rejects conflicting duplicate cards independent of order: %s", (_name, arrange) => {
    const original = card("duplicate");
    const conflicting = { ...original, confirmedFacts: ["reference-rhythm:different-rule"] };
    const forward = arrange(original, conflicting);
    const reverse = arrange(conflicting, original);
    const snapshot = assetSnapshot(original);

    expect(() => createReferenceMaterialMappingPlan({
      ...mappingInput(), ...forward, assets: [snapshot],
    })).toThrow("reference-material-card-conflict");
    expect(() => createReferenceMaterialMappingPlan({
      ...mappingInput(), ...reverse, assets: [snapshot],
    })).toThrow("reference-material-card-conflict");
  });

  it("canonicalizes identical aliases without changing plan identity", () => {
    const candidate = card("alias");
    const input = mappingInput();
    const once = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [candidate],
      assets: [assetSnapshot(candidate)],
    });
    const aliased = createReferenceMaterialMappingPlan({
      ...input,
      currentMaterials: [structuredClone(candidate), structuredClone(candidate)],
      authorizedHistoricalMaterials: [structuredClone(candidate)],
      assets: [assetSnapshot(candidate)],
    });

    expect(aliased).toEqual(once);
    expect(aliased.id).toBe(once.id);
  });

  it("confirms an honest plan with unresolved rows", () => {
    const plan = createReferenceMaterialMappingPlan(mappingInput());
    const confirmed = confirmReferenceMaterialMappingPlan(
      plan,
      draftIdentity(plan),
      "reviewer-a",
      "2026-09-19T09:00:00.000Z",
    );

    expect(confirmed).toMatchObject({
      status: "confirmed",
      unresolvedCount: 1,
      confirmedByActorId: "reviewer-a",
      confirmedAt: "2026-09-19T09:00:00.000Z",
    });
    expect(confirmed.confirmationReceiptId).toMatch(/^reference_mapping_confirmation_/);
  });

  it("rejects invalidated upstream selection and changed permissions at confirmation", () => {
    const candidate = card("confirmation-check");
    const plan = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [candidate],
      assets: [assetSnapshot(candidate)],
    });
    const invalidated = structuredClone(plan);
    invalidated.bindings.styleSelection.status = "invalidated";
    expect(() => confirmReferenceMaterialMappingPlan(
      invalidated as ReferenceMaterialMappingPlan,
      draftIdentity(plan),
      "reviewer-a",
      "2026-09-19T09:00:00.000Z",
    )).toThrow("reference-mapping-input-stale");

    const changedPermission = structuredClone(plan);
    changedPermission.mappings[0]!.permission!.epoch += 1;
    expect(() => confirmReferenceMaterialMappingPlan(
      changedPermission,
      draftIdentity(plan),
      "reviewer-a",
      "2026-09-19T09:00:00.000Z",
    )).toThrow("reference-mapping-permission-stale");
  });

  it("rejects a mutated row even when the caller recomputes the public digests and plan id", () => {
    const candidate = card("forged-plan");
    const plan = createReferenceMaterialMappingPlan({
      ...mappingInput(),
      currentMaterials: [candidate],
      assets: [assetSnapshot(candidate)],
    });
    const trustedIdentity = draftIdentity(plan);
    const forged = structuredClone(plan);
    forged.mappings[0]!.status = "substituted";
    forged.mappings[0]!.differenceReason = "authorized-material-content-differs";
    forged.contentDigest = digest(canonicalJson({
      bindings: forged.bindings,
      mappings: forged.mappings,
      permissionSetDigest: forged.permissionSetDigest,
    }));
    forged.id = `reference_mapping_plan_${digest(
      `${forged.bindings.styleSelection.id}\0${forged.bindings.styleSelection.version}\0${forged.contentDigest}`,
    )}`;

    expect(() => confirmReferenceMaterialMappingPlan(
      forged,
      trustedIdentity,
      "reviewer-a",
      "2026-09-19T09:00:00.000Z",
    )).toThrow("reference-mapping-draft-identity-mismatch");
  });
});

function mappingInput(options: Readonly<{
  sourceKind?: ReferenceSample["sourceKind"];
  reusableElements?: ReferenceRightsDeclaration["reusableElements"];
  ruleKind?: ReferenceStyleAnalysis["rules"][number]["kind"];
  ruleDescription?: string;
  transitionKind?: "cut" | "fade" | "crossfade";
}> = {}) {
  const sourceKind = options.sourceKind ?? "external-style-only";
  const reusableElements = options.reusableElements ?? [];
  const sample: ReferenceSample = {
    id: "reference-sample-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 2,
    sourceAssetId: "reference-asset-1",
    sourceDigest: digest("reference-source"),
    sourceKind,
    collectionMode: "historical-pointer",
    candidateScope: "reference-only",
    status: "ready",
    viewPermission: "allowed",
    usePermission: "allowed",
    createdAt,
  };
  const sampleBinding = {
    id: sample.id,
    version: sample.version,
    customerId: sample.customerId,
    sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId,
    sourceDigest: sample.sourceDigest,
    sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode,
    candidateScope: sample.candidateScope,
  } as const;
  const rights: ReferenceRightsDeclaration = {
    id: "reference-rights-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 3,
    contentDigest: digest(`rights-${sourceKind}-${reusableElements.join("-")}`),
    sample: sampleBinding,
    sourceKind,
    reusableElements,
    actorId: scope.actorId,
    createdAt,
  };
  const rightsBinding = {
    id: rights.id,
    version: rights.version,
    customerId: rights.customerId,
    sessionId: rights.sessionId,
    referenceSampleId: sample.id,
    referenceSampleVersion: sample.version,
    referenceSampleDigest: sample.sourceDigest,
    sourceKind: rights.sourceKind,
    reusableElements: rights.reusableElements,
    contentDigest: rights.contentDigest,
  } as const;
  const ruleKind = options.ruleKind ?? "rhythm";
  const ruleId = `rule-${ruleKind}-1`;
  const transitionId = "transition-1";
  const rule = {
    id: ruleId,
    kind: ruleKind,
    description: options.ruleDescription ?? "rhythm tight opening",
    referenceStartMs: 0,
    referenceEndMs: 2_000,
    evidenceIds: ruleKind === "transition" ? [transitionId] : ["section-1"],
  };
  const analysis: ReferenceStyleAnalysis = {
    id: "reference-analysis-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 4,
    contentDigest: digest("analysis"),
    bindings: { sample: sampleBinding, rights: rightsBinding },
    cuts: [],
    sections: [{ id: "section-1", startMs: 0, endMs: 2_000, summary: "opening" }],
    transcriptSpans: [],
    beatMarkers: [],
    transitions: options.transitionKind
      ? [{ id: transitionId, atMs: 1_000, kind: options.transitionKind, confidence: 0.9 }]
      : [],
    rules: [rule],
    warnings: [],
    confidence: 0.9,
    status: "complete",
    capability: {
      mode: "provider",
      label: "local reference evidence",
      version: 1,
      provider: "local-reference-v1",
    },
    createdAt,
  };
  const selection: ReferenceStyleSelection = {
    id: "reference-selection-1",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 5,
    contentDigest: digest("selection"),
    bindings: {
      sample: sampleBinding,
      rights: rightsBinding,
      analysis: {
        id: analysis.id,
        version: analysis.version,
        customerId: analysis.customerId,
        sessionId: analysis.sessionId,
        contentDigest: analysis.contentDigest,
      },
    },
    selectedRuleIds: [rule.id],
    actorId: scope.actorId,
    confirmationReceiptId: "selection-confirmation-1",
    confirmedAt: createdAt,
    status: "confirmed",
    createdAt,
  };
  return {
    scope,
    sample,
    rights,
    analysis,
    selection,
    currentMaterials: [] as MaterialCard[],
    authorizedHistoricalMaterials: [] as MaterialCard[],
    assets: [] as ReferenceMappingAssetSnapshot[],
  };
}

function card(suffix: string, overrides: Partial<MaterialCard> = {}): MaterialCard {
  return {
    id: `card-${suffix}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: `asset-${suffix}`,
    sourceDigest: digest(`asset-${suffix}`),
    sourceStartMs: 0,
    sourceEndMs: 3_000,
    contentType: "single-product",
    technicalUsable: true,
    technicalFlags: ["ffprobe:video"],
    observations: ["rhythm"],
    confirmedFacts: ["reference-rhythm:rule-rhythm-1"],
    pendingFacts: [],
    capability: {
      mode: "provider",
      label: "local material evidence",
      version: 1,
      provider: "local-material-v1",
    },
    createdAt,
    ...overrides,
  };
}

function assetSnapshot(
  source: MaterialCard,
  override: Readonly<{
    assetCustomerId?: string;
    mediaKind?: StudioMediaAsset["mediaKind"];
    permissionCustomerId?: string;
    permissionSessionId?: string;
    permissionSourceDigest?: string;
    verifiedDigest?: string;
    withdrawn?: boolean;
  }> = {},
): ReferenceMappingAssetSnapshot {
  const asset: StudioMediaAsset = {
    id: source.sourceAssetId,
    importBindingId: source.sourceAssetId,
    blobId: `blob_${source.sourceDigest}`,
    customerId: override.assetCustomerId ?? source.customerId,
    sessionId: source.sessionId,
    version: 1,
    digest: source.sourceDigest,
    blobDigest: source.sourceDigest,
    mediaKind: override.mediaKind ?? "video",
    byteLength: 128,
    fileName: `${source.id}.mp4`,
    capturedAt: null,
    importedAt: createdAt,
    viewPermission: "allowed",
    usePermission: "allowed",
    captureContext: null,
  };
  return {
    asset,
    permission: {
      id: `permission-${source.sourceAssetId}`,
      version: 1,
      epoch: 1,
      customerId: override.permissionCustomerId ?? scope.customerId,
      sessionId: override.permissionSessionId ?? source.sessionId,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: override.permissionSourceDigest ?? source.sourceDigest,
      view: "allowed",
      use: "allowed",
    },
    withdrawn: override.withdrawn ?? false,
    verifiedDigest: override.verifiedDigest ?? source.sourceDigest,
  };
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function draftIdentity(plan: ReferenceMaterialMappingPlan) {
  return { id: plan.id, version: plan.version, contentDigest: plan.contentDigest };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
