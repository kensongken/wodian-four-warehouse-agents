import { createHash } from "node:crypto";

import {
  MaterialCardSchema,
  ReferenceMaterialMappingPlanSchema,
  ReferenceRightsDeclarationSchema,
  ReferenceSampleSchema,
  ReferenceStyleAnalysisSchema,
  ReferenceStyleSelectionSchema,
  StudioMediaAssetSchema,
  type MaterialCard,
  type ReferenceMaterialMappingPlan,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceStyleAnalysis,
  type ReferenceStyleSelection,
  type StudioMediaAsset,
} from "@wearable/contracts";

import type { StudioScope } from "./studio-capabilities";

type MappingRow = ReferenceMaterialMappingPlan["mappings"][number];
type MappingPermission = NonNullable<MappingRow["permission"]>;
type StyleRule = ReferenceStyleAnalysis["rules"][number];

export type ReferenceMappingAssetSnapshot = Readonly<{
  asset: StudioMediaAsset;
  permission: MappingPermission;
  withdrawn: boolean;
  verifiedDigest: string;
}>;

export type CreateReferenceMaterialMappingPlanInput = Readonly<{
  scope: StudioScope;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  selection: ReferenceStyleSelection;
  currentMaterials: readonly MaterialCard[];
  authorizedHistoricalMaterials: readonly MaterialCard[];
  assets: readonly ReferenceMappingAssetSnapshot[];
}>;

export type ReferenceMaterialMappingPlanIdentity = Readonly<Pick<
  ReferenceMaterialMappingPlan,
  "id" | "version" | "contentDigest"
>>;

type Candidate = Readonly<{
  card: MaterialCard;
  snapshot: ReferenceMappingAssetSnapshot;
  tier: 0 | 1;
  evidenceScore: number;
  provenance: MappingRow["sourceProvenance"];
}>;

/** Maps confirmed style rules only to exact, currently authorized material snapshots. */
export function createReferenceMaterialMappingPlan(
  inputValue: CreateReferenceMaterialMappingPlanInput,
): ReferenceMaterialMappingPlan {
  const input = validateMappingInput(inputValue);
  const currentCardIds = new Set(input.currentMaterials.map((card) => card.id));
  const currentSourceIdentities = new Set(input.currentMaterials.map(sourceIdentity));
  const snapshots = authorizedSnapshots(input);
  const cards = canonicalCards(input.currentMaterials, input.authorizedHistoricalMaterials);
  const rulesById = new Map(input.analysis.rules.map((rule) => [rule.id, rule]));

  const mappings = input.selection.selectedRuleIds.map((ruleId) => {
    const rule = rulesById.get(ruleId);
    if (!rule) throw new Error("reference-selected-rule-missing");
    const ranked = cards.flatMap((card): Candidate[] => {
      const snapshot = snapshots.get(card.sourceAssetId);
      if (!snapshot || !eligibleCard(input, rule, card, snapshot, currentCardIds, currentSourceIdentities)) {
        return [];
      }
      const tier = currentCardIds.has(card.id) || currentSourceIdentities.has(sourceIdentity(card)) ? 0 : 1;
      const provenance = isReferenceSource(input.sample, card)
        ? "self-owned-reference"
        : tier === 0 ? "customer-current" : "customer-authorized-history";
      return [{
        card,
        snapshot,
        tier,
        provenance,
        evidenceScore: ruleEvidenceScore(rule, input.analysis, card),
      }];
    }).filter((candidate) => isTechnicallySafeForRule(rule, input.analysis, candidate))
      .sort(compareMappingCandidate);
    const selected = ranked[0];
    return selected ? mappingFromCandidate(input, rule, selected) : unachievableMapping(rule);
  });
  const permissionSetDigest = digestPermissionSet(mappings);
  const bindings = mappingBindings(input);
  const contentDigest = mappingContentDigest({ bindings, mappings, permissionSetDigest });

  return ReferenceMaterialMappingPlanSchema.parse({
    id: `reference_mapping_plan_${sha256(`${input.selection.id}\0${input.selection.version}\0${contentDigest}`)}`,
    customerId: input.scope.customerId,
    sessionId: input.scope.sessionId,
    version: 1,
    contentDigest,
    permissionSetDigest,
    bindings,
    mappings,
    unresolvedCount: mappings.filter((mapping) => mapping.status === "unachievable").length,
    status: "draft",
    confirmedByActorId: null,
    confirmationReceiptId: null,
    confirmedAt: null,
    createdAt: input.selection.confirmedAt,
  });
}

/** Confirms the exact draft bytes; stale upstream or permission edits fail closed. */
export function confirmReferenceMaterialMappingPlan(
  planValue: ReferenceMaterialMappingPlan,
  expectedDraft: ReferenceMaterialMappingPlanIdentity,
  actorId: string,
  now: string,
): ReferenceMaterialMappingPlan {
  if (!expectedDraft || typeof expectedDraft.id !== "string" || !expectedDraft.id.trim()
      || !Number.isSafeInteger(expectedDraft.version) || expectedDraft.version < 1
      || typeof expectedDraft.contentDigest !== "string" || !/^[a-f0-9]{64}$/.test(expectedDraft.contentDigest)) {
    throw new Error("reference-mapping-draft-identity-required");
  }
  if (!actorId?.trim()) throw new Error("reference-mapping-confirming-actor-required");
  if (!Number.isFinite(Date.parse(now))) throw new Error("reference-mapping-confirmed-at-invalid");
  if (planValue?.bindings?.styleSelection?.status !== "confirmed") {
    throw new Error("reference-mapping-input-stale");
  }
  let plan: ReferenceMaterialMappingPlan;
  try {
    plan = ReferenceMaterialMappingPlanSchema.parse(planValue);
  } catch {
    throw new Error("reference-mapping-input-stale");
  }
  if (plan.status !== "draft") throw new Error("reference-mapping-draft-required");
  if (plan.id !== expectedDraft.id || plan.version !== expectedDraft.version
      || plan.contentDigest !== expectedDraft.contentDigest) {
    throw new Error("reference-mapping-draft-identity-mismatch");
  }
  if (plan.mappings.length === 0 || plan.mappings.some((mapping) =>
    !["matched", "substituted", "unachievable"].includes(mapping.status))) {
    throw new Error("reference-mapping-row-unresolved");
  }
  const permissionSetDigest = digestPermissionSet(plan.mappings);
  if (permissionSetDigest !== plan.permissionSetDigest) {
    throw new Error("reference-mapping-permission-stale");
  }
  const contentDigest = mappingContentDigest({
    bindings: plan.bindings,
    mappings: plan.mappings,
    permissionSetDigest,
  });
  if (contentDigest !== plan.contentDigest) throw new Error("reference-mapping-input-stale");
  const expectedPlanId = `reference_mapping_plan_${sha256(
    `${plan.bindings.styleSelection.id}\0${plan.bindings.styleSelection.version}\0${plan.contentDigest}`,
  )}`;
  if (plan.id !== expectedPlanId) throw new Error("reference-mapping-input-stale");

  return ReferenceMaterialMappingPlanSchema.parse({
    ...plan,
    status: "confirmed",
    confirmedByActorId: actorId.trim(),
    confirmationReceiptId: `reference_mapping_confirmation_${sha256(
      `${plan.id}\0${plan.version}\0${plan.contentDigest}\0${actorId.trim()}\0${now}`,
    )}`,
    confirmedAt: now,
  });
}

function validateMappingInput(
  inputValue: CreateReferenceMaterialMappingPlanInput,
): CreateReferenceMaterialMappingPlanInput {
  if (!inputValue || typeof inputValue !== "object") throw new Error("reference-mapping-input-invalid");
  const { scope } = inputValue;
  if (!scope?.actorId?.trim() || !scope.customerId?.trim() || !scope.sessionId?.trim()) {
    throw new Error("reference-mapping-scope-invalid");
  }
  const sample = ReferenceSampleSchema.parse(inputValue.sample);
  const rights = ReferenceRightsDeclarationSchema.parse(inputValue.rights);
  const analysis = ReferenceStyleAnalysisSchema.parse(inputValue.analysis);
  const selection = ReferenceStyleSelectionSchema.parse(inputValue.selection);
  const currentMaterials = inputValue.currentMaterials.map((card) => MaterialCardSchema.parse(card));
  const authorizedHistoricalMaterials = inputValue.authorizedHistoricalMaterials
    .map((card) => MaterialCardSchema.parse(card));
  if (!Array.isArray(inputValue.assets)) throw new Error("reference-mapping-assets-required");

  if ([sample, rights, analysis, selection].some((value) =>
    value.customerId !== scope.customerId || value.sessionId !== scope.sessionId)) {
    throw new Error("reference-mapping-scope-mismatch");
  }
  if (sample.status !== "ready" || sample.viewPermission !== "allowed" || selection.status !== "confirmed") {
    throw new Error("reference-mapping-input-stale");
  }
  const expectedSample = sampleBinding(sample);
  const expectedRights = rightsBinding(sample, rights);
  const expectedAnalysis = scopedBinding(analysis);
  if (
    canonicalJson(rights.sample) !== canonicalJson(expectedSample)
    || canonicalJson(analysis.bindings.sample) !== canonicalJson(expectedSample)
    || canonicalJson(analysis.bindings.rights) !== canonicalJson(expectedRights)
    || canonicalJson(selection.bindings.sample) !== canonicalJson(expectedSample)
    || canonicalJson(selection.bindings.rights) !== canonicalJson(expectedRights)
    || canonicalJson(selection.bindings.analysis) !== canonicalJson(expectedAnalysis)
  ) throw new Error("reference-mapping-input-stale");
  const ruleIds = new Set(analysis.rules.map((rule) => rule.id));
  if (selection.selectedRuleIds.some((id) => !ruleIds.has(id))) {
    throw new Error("reference-selected-rule-missing");
  }
  return {
    ...inputValue,
    sample,
    rights,
    analysis,
    selection,
    currentMaterials,
    authorizedHistoricalMaterials,
  };
}

function authorizedSnapshots(
  input: CreateReferenceMaterialMappingPlanInput,
): ReadonlyMap<string, ReferenceMappingAssetSnapshot> {
  const snapshots = new Map<string, ReferenceMappingAssetSnapshot>();
  const conflicted = new Set<string>();
  for (const value of input.assets) {
    const parsed = StudioMediaAssetSchema.safeParse(value?.asset);
    if (!parsed.success || !permissionIsCurrent(input.scope, parsed.data, value)) continue;
    const snapshot = { ...value, asset: parsed.data };
    const existing = snapshots.get(parsed.data.id);
    if (existing && canonicalJson(existing) !== canonicalJson(snapshot)) {
      snapshots.delete(parsed.data.id);
      conflicted.add(parsed.data.id);
      continue;
    }
    if (!conflicted.has(parsed.data.id)) snapshots.set(parsed.data.id, snapshot);
  }
  return snapshots;
}

function permissionIsCurrent(
  scope: StudioScope,
  asset: StudioMediaAsset,
  snapshot: ReferenceMappingAssetSnapshot,
): boolean {
  const permission = snapshot?.permission;
  return snapshot.withdrawn === false
    && snapshot.verifiedDigest === asset.digest
    && asset.blobDigest === asset.digest
    && asset.customerId === scope.customerId
    && asset.viewPermission === "allowed"
    && asset.usePermission === "allowed"
    && typeof permission?.id === "string"
    && permission.id.trim().length > 0
    && Number.isSafeInteger(permission.version)
    && permission.version > 0
    && Number.isSafeInteger(permission.epoch)
    && permission.epoch > 0
    && permission.customerId === scope.customerId
    && permission.sessionId === scope.sessionId
    && permission.sourceAssetId === asset.id
    && permission.sourceDigest === asset.digest
    && permission.view === "allowed"
    && permission.use === "allowed";
}

function eligibleCard(
  input: CreateReferenceMaterialMappingPlanInput,
  rule: StyleRule,
  card: MaterialCard,
  snapshot: ReferenceMappingAssetSnapshot,
  currentCardIds: ReadonlySet<string>,
  currentSourceIdentities: ReadonlySet<string>,
): boolean {
  const isCurrent = currentCardIds.has(card.id) || currentSourceIdentities.has(sourceIdentity(card));
  return card.technicalUsable
    && card.customerId === input.scope.customerId
    && (!isCurrent || card.sessionId === input.scope.sessionId)
    && card.sourceAssetId === snapshot.asset.id
    && card.sourceDigest === snapshot.asset.digest
    && (!isReferenceSource(input.sample, card)
      || canReuseSelfOwnedReference(input.rights, rule.kind === "audio" ? "audio-track" : "video-clips"));
}

function canonicalCards(
  currentMaterials: readonly MaterialCard[],
  authorizedHistoricalMaterials: readonly MaterialCard[],
): MaterialCard[] {
  const byId = new Map<string, string>();
  const bySource = new Map<string, string>();
  const seen = new Set<string>();
  const unique: MaterialCard[] = [];
  for (const card of [...currentMaterials, ...authorizedHistoricalMaterials]) {
    const canonical = canonicalJson(card);
    const source = sourceIdentity(card);
    const sameId = byId.get(card.id);
    const sameSource = bySource.get(source);
    if (sameId !== undefined && sameId !== canonical
        || sameSource !== undefined && sameSource !== canonical) {
      throw new Error("reference-material-card-conflict");
    }
    byId.set(card.id, canonical);
    bySource.set(source, canonical);
    if (seen.has(canonical)) continue;
    seen.add(canonical);
    unique.push(card);
  }
  return unique;
}

function mappingFromCandidate(
  input: CreateReferenceMaterialMappingPlanInput,
  rule: StyleRule,
  candidate: Candidate,
): MappingRow {
  const exact = contentMatchesRule(rule, input.analysis, candidate.card);
  const mappingId = `reference_mapping_${sha256(`${rule.id}\0${candidate.card.id}`)}`;
  return {
    id: mappingId,
    ruleId: rule.id,
    referenceStartMs: rule.referenceStartMs,
    referenceEndMs: rule.referenceEndMs,
    targetRule: rule.description,
    sourceBindingId: `reference_source_binding_${sha256(`${mappingId}\0${sourceIdentity(candidate.card)}`)}`,
    sourceAssetId: candidate.card.sourceAssetId,
    sourceDigest: candidate.card.sourceDigest,
    sourceStartMs: candidate.card.sourceStartMs,
    sourceEndMs: candidate.card.sourceEndMs,
    sourceProvenance: candidate.provenance,
    permission: candidate.snapshot.permission,
    reuseRights: candidate.provenance === "self-owned-reference"
      ? {
          id: input.rights.id,
          version: input.rights.version,
          element: rule.kind === "audio" ? "audio-track" as const : "video-clips" as const,
        }
      : null,
    status: exact ? "matched" : "substituted",
    differenceReason: exact ? null : "authorized-material-content-differs",
  };
}

function unachievableMapping(rule: StyleRule): MappingRow {
  return {
    id: `reference_mapping_${sha256(`${rule.id}\0unachievable`)}`,
    ruleId: rule.id,
    referenceStartMs: rule.referenceStartMs,
    referenceEndMs: rule.referenceEndMs,
    targetRule: rule.description,
    sourceBindingId: null,
    sourceAssetId: null,
    sourceDigest: null,
    sourceStartMs: null,
    sourceEndMs: null,
    sourceProvenance: null,
    permission: null,
    reuseRights: null,
    status: "unachievable",
    differenceReason: "no-authorized-material",
  };
}

function isTechnicallySafeForRule(
  rule: StyleRule,
  analysis: ReferenceStyleAnalysis,
  candidate: Candidate,
): boolean {
  const kind = candidate.snapshot.asset.mediaKind;
  if (rule.kind === "rhythm" || rule.kind === "transition") return kind === "video";
  if (rule.kind === "narrative") return kind === "video" || kind === "image";
  return (kind === "video" || kind === "audio") && contentMatchesRule(rule, analysis, candidate.card);
}

function contentMatchesRule(
  rule: StyleRule,
  analysis: ReferenceStyleAnalysis,
  card: MaterialCard,
): boolean {
  const confirmed = new Set(card.confirmedFacts.map(normalizeEvidenceCode));
  if (rule.kind === "rhythm") return confirmed.has(`reference-rhythm:${normalizeEvidenceCode(rule.id)}`);
  if (rule.kind === "narrative") return confirmed.has(`reference-narrative:${normalizeEvidenceCode(rule.id)}`);
  if (rule.kind === "audio") return confirmed.has(`reference-audio:${normalizeEvidenceCode(rule.id)}`);
  const transitionKinds = analysis.transitions
    .filter((transition) => rule.evidenceIds.includes(transition.id) && transition.kind !== "unsupported")
    .map((transition) => transition.kind);
  return transitionKinds.length === 1
    && confirmed.has(`reference-transition:${transitionKinds[0]}:${normalizeEvidenceCode(rule.id)}`);
}

function ruleEvidenceScore(rule: StyleRule, analysis: ReferenceStyleAnalysis, card: MaterialCard): number {
  const haystack = `${card.contentType} ${card.technicalFlags.join(" ")} ${card.observations.join(" ")} ${card.confirmedFacts.join(" ")}`
    .toLocaleLowerCase();
  const tokens = [...new Set(rule.description.toLocaleLowerCase().split(/[^\p{L}\p{N}]+/u)
    .filter((token) => token.length > 1))];
  const overlap = tokens.filter((token) => haystack.includes(token)).length;
  return (contentMatchesRule(rule, analysis, card) ? 10_000 : 0)
    + overlap * 100
    + card.confirmedFacts.length * 10
    + card.observations.length;
}

function normalizeEvidenceCode(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function compareMappingCandidate(left: Candidate, right: Candidate): number {
  return left.tier - right.tier
    || right.evidenceScore - left.evidenceScore
    || compareText(sourceIdentity(left.card), sourceIdentity(right.card))
    || compareText(left.card.id, right.card.id)
    || compareText(left.snapshot.permission.id, right.snapshot.permission.id);
}

function canReuseSelfOwnedReference(
  rights: ReferenceRightsDeclaration,
  element: "video-clips" | "audio-track",
): boolean {
  return rights.sourceKind === "self-owned" && rights.reusableElements.includes(element);
}

function isReferenceSource(sample: ReferenceSample, card: MaterialCard): boolean {
  return card.sourceAssetId === sample.sourceAssetId && card.sourceDigest === sample.sourceDigest;
}

function sourceIdentity(card: MaterialCard): string {
  return [card.sourceAssetId, card.sourceDigest, card.sourceStartMs, card.sourceEndMs].join(":");
}

function mappingBindings(input: CreateReferenceMaterialMappingPlanInput) {
  return {
    sample: sampleBinding(input.sample),
    rights: rightsBinding(input.sample, input.rights),
    analysis: scopedBinding(input.analysis),
    styleSelection: {
      ...scopedBinding(input.selection),
      status: input.selection.status,
      confirmation: {
        receiptId: input.selection.confirmationReceiptId,
        actorId: input.selection.actorId,
        confirmedAt: input.selection.confirmedAt,
      },
    },
  };
}

function sampleBinding(sample: ReferenceSample) {
  return {
    id: sample.id,
    version: sample.version,
    customerId: sample.customerId,
    sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId,
    sourceDigest: sample.sourceDigest,
    sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode,
    candidateScope: sample.candidateScope,
  };
}

function rightsBinding(sample: ReferenceSample, rights: ReferenceRightsDeclaration) {
  return {
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
  };
}

function scopedBinding(value: Readonly<{
  id: string;
  version: number;
  customerId: string;
  sessionId: string;
  contentDigest: string;
}>) {
  return {
    id: value.id,
    version: value.version,
    customerId: value.customerId,
    sessionId: value.sessionId,
    contentDigest: value.contentDigest,
  };
}

function digestPermissionSet(mappings: readonly MappingRow[]): string {
  const permissions = new Map<string, Omit<MappingPermission, "view" | "use">>();
  for (const mapping of mappings) {
    if (!mapping.permission) continue;
    const { view: _view, use: _use, ...identity } = mapping.permission;
    const existing = permissions.get(identity.id);
    if (existing && canonicalJson(existing) !== canonicalJson(identity)) {
      throw new Error("reference-mapping-permission-identity-conflict");
    }
    permissions.set(identity.id, identity);
  }
  return sha256(canonicalJson([...permissions.values()].sort((left, right) => compareText(left.id, right.id))));
}

function mappingContentDigest(value: Readonly<{
  bindings: ReferenceMaterialMappingPlan["bindings"];
  mappings: readonly MappingRow[];
  permissionSetDigest: string;
}>): string {
  return sha256(canonicalJson(value));
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
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

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
