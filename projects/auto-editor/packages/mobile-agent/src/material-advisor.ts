import { createHash, randomUUID } from "node:crypto";

import type {
  ContentSegment,
  CreativeIntent,
  CreativeSession,
  MaterialSuggestionSet,
  MaterialUseDecision,
} from "@wearable/contracts";

import type { ContentLibrary, CreativeMaterialState } from "./content-library";
import { resolveHistoricalMaterialCatalog } from "./historical-source";
import { isCreativeSessionTransitionAllowed } from "./creative-session-policy";

export interface MaterialMatcher {
  rank(input: {
    intent: CreativeIntent;
    segments: ContentSegment[];
  }): Promise<Array<{ segmentId: string; reason: string; score: number }>>;
}

export type MaterialSuggestionSummary = MaterialSuggestionSet & {
  count: number;
  hasSameProductMatches: boolean;
};

/** Session decision order is authoritative, including equal timestamps and later declines. */
export function currentMaterialViewingDecision(
  session: CreativeSession,
  set: MaterialSuggestionSet | null | undefined,
  decisions: readonly MaterialUseDecision[],
): MaterialUseDecision | null {
  if (!set || set.sessionId !== session.id) return null;
  return [...session.materialViewDecisionIds].reverse().map((id) => decisions.find((decision) =>
    decision.id === id && decision.sessionId === session.id && decision.suggestionSetId === set.id &&
    decision.suggestionVersion === set.version && decision.purpose === set.purpose &&
    (decision.action === "accept-view" || decision.action === "decline-view")))
    .find((decision) => decision !== undefined) ?? null;
}

export async function suggestPostShootHistoricalMaterials(
  input: { sessionId: string; actorId: string; now: string },
  matcher: MaterialMatcher,
  library: ContentLibrary,
): Promise<MaterialSuggestionSummary> {
  requireActor(input.actorId);
  const session = await requireSession(input.sessionId, library);
  if (session.stage !== "ingesting" && session.stage !== "editorial_review") {
    throw new Error("post-shoot-material-suggestion-not-allowed");
  }
  const intent = await requireCurrentIntent(session, library);
  const assets = await library.listAssets();
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const catalog = resolveHistoricalMaterialCatalog(
    session, await library.listSegments(), assets, await library.listCreativeSessions(),
  );
  const eligible = catalog.segments;
  const ranked = await rankMaterialsRetryably(matcher, { intent, segments: eligible });
  const segmentsById = new Map(eligible.map((segment) => [segment.id, segment]));
  if (ranked.some((match) => !segmentsById.has(match.segmentId))) {
    throw new Error("material-no-longer-eligible");
  }
  const suggestions = ranked.flatMap((match) => {
    const segment = segmentsById.get(match.segmentId);
    const asset = segment ? assetsById.get(segment.assetId) : undefined;
    if (!segment || !asset) return [];
    return [{
      segmentId: segment.id, thumbnailPath: asset.localPath, capturedAt: asset.importedAt,
      productId: segment.tags.products[0]?.id ?? null,
      processStage: segment.tags.processStages[0] ?? null, reason: match.reason,
      source: asset.origin, permissionState: segment.permissionState,
      lastUsedAt: segment.tags.usage.lastUsedAt, technicallyAvailable: true,
    }];
  });
  const suggestionSet: MaterialSuggestionSet = {
    id: `material_suggestion_set_${randomUUID()}`, sessionId: session.id,
    version: nextSuggestionVersion(await library.listMaterialSuggestionSets(), session.id),
    purpose: "post-shoot-edit", suggestions, createdAt: input.now,
  };
  await library.putMaterialSuggestionSet(suggestionSet, {
    suggestionSetId: suggestionSet.id,
    purpose: "post-shoot-edit",
    sources: catalog.sources,
  });
  return { ...suggestionSet, suggestions: [], count: suggestions.length,
    hasSameProductMatches: suggestions.some((suggestion) =>
      intent.products.some((product) => product.id === suggestion.productId)) };
}

export async function decidePostShootMaterialViewing(input: {
  sessionId: string; suggestionSetId: string; decision: "accept" | "decline";
  actorId: string; now: string; operationKey?: string;
}, library: ContentLibrary): Promise<MaterialUseDecision> {
  const { session, set, identity } = await library.projectCreativeMaterialState((state) => ({
    ...postShootMaterialContext(input, state),
    identity: postShootDecisionIdentity(input, input.decision === "accept" ? "accept-view" : "decline-view", [], state.decisions),
  }));
  if (identity.existing) return identity.existing;
  const shown = set.suggestions.map((item) => item.segmentId);
  const decision: MaterialUseDecision = {
    id: identity.id, sessionId: session.id,
    suggestionSetId: set.id, suggestionVersion: set.version, purpose: "post-shoot-edit",
    action: input.decision === "accept" ? "accept-view" : "decline-view",
    shownSegmentIds: shown, selectedSegmentIds: [],
    rejectedSegmentIds: input.decision === "decline" ? shown : [],
    actorId: input.actorId, createdAt: input.now,
  };
  const next = { ...session, materialViewDecisionIds: [...session.materialViewDecisionIds, decision.id],
    updatedAt: input.now };
  await library.commitCreativeBundle({ session: next, expectedSessionSnapshot: session,
    decisions: [decision], auditEvents: [sameStageAudit(session, input.actorId,
      "post-shoot historical material viewing decided", input.now)] });
  return decision;
}

export async function confirmPostShootMaterialSelection(input: {
  sessionId: string; suggestionSetId: string; selectedSegmentIds: string[];
  actorId: string; now: string; operationKey?: string;
}, library: ContentLibrary): Promise<MaterialUseDecision> {
  const { session, set, identity } = await library.projectCreativeMaterialState((state) => {
    const { session, set } = postShootMaterialContext(input, state);
    const identity = postShootDecisionIdentity(input, "confirm-selection", input.selectedSegmentIds, state.decisions);
    if (!identity.existing) {
      if (currentMaterialViewingDecision(session, set, state.decisions)?.action !== "accept-view") {
        throw new Error("post-shoot-material-view-acceptance-required");
      }
      const shown = set.suggestions.map((item) => item.segmentId);
      if (new Set(input.selectedSegmentIds).size !== input.selectedSegmentIds.length ||
        input.selectedSegmentIds.some((id) => !shown.includes(id))) {
        throw new Error("post-shoot-material-selection-invalid");
      }
      const segments = new Map(state.segments.map((item) => [item.id, item]));
      if (input.selectedSegmentIds.some((id) => !isEligible(segments.get(id)))) {
        throw new Error("post-shoot-material-selection-no-longer-eligible");
      }
    }
    return { session, set, identity };
  });
  if (identity.existing) return identity.existing;
  const shown = set.suggestions.map((item) => item.segmentId);
  const decision: MaterialUseDecision = {
    id: identity.id, sessionId: session.id,
    suggestionSetId: set.id, suggestionVersion: set.version, purpose: "post-shoot-edit",
    action: "confirm-selection", shownSegmentIds: shown,
    selectedSegmentIds: input.selectedSegmentIds,
    rejectedSegmentIds: shown.filter((id) => !input.selectedSegmentIds.includes(id)),
    actorId: input.actorId, createdAt: input.now,
  };
  const next = { ...session, materialUseDecisionIds: [...session.materialUseDecisionIds, decision.id],
    updatedAt: input.now };
  await library.commitCreativeBundle({ session: next, expectedSessionSnapshot: session,
    requiredEligibleHistoricalSegmentIds: input.selectedSegmentIds, decisions: [decision],
    auditEvents: [sameStageAudit(session, input.actorId,
      "post-shoot exact historical material selection confirmed", input.now)] });
  return decision;
}

// The persisted decision ID keeps the original operation a replay after the HTTP registry restarts.
function postShootDecisionIdentity(
  input: { sessionId: string; suggestionSetId: string; actorId: string; operationKey?: string },
  action: MaterialUseDecision["action"],
  selectedSegmentIds: string[],
  decisions: readonly MaterialUseDecision[],
) {
  const id = input.operationKey
    ? `material_operation_${createHash("sha256").update(JSON.stringify([input.sessionId, input.operationKey])).digest("hex")}`
    : `material_use_decision_${randomUUID()}`;
  const existing = decisions.find((decision) => decision.id === id);
  if (existing && (existing.sessionId !== input.sessionId || existing.suggestionSetId !== input.suggestionSetId ||
    existing.purpose !== "post-shoot-edit" || existing.actorId !== input.actorId || existing.action !== action ||
    JSON.stringify(existing.selectedSegmentIds) !== JSON.stringify(selectedSegmentIds))) {
    throw new Error("creative-operation-key-conflict");
  }
  if (input.operationKey && !existing && action !== "decline-view" && decisions.some((decision) =>
    decision.sessionId === input.sessionId && decision.suggestionSetId === input.suggestionSetId &&
    decision.purpose === "post-shoot-edit" && !/^material_operation_[a-f0-9]{64}$/.test(decision.id))) {
    // Legacy decisions cannot prove whether this key is fresh or an old withdrawn acceptance.
    throw new Error("post-shoot-material-operation-reconfirmation-required");
  }
  return { id, existing };
}

export async function suggestHistoricalMaterials(
  input: { sessionId: string; actorId: string; now: string },
  matcher: MaterialMatcher,
  library: ContentLibrary,
): Promise<MaterialSuggestionSummary> {
  requireActor(input.actorId);
  const session = await requireSession(input.sessionId, library);
  if (session.stage !== "awaiting_material_consent") {
    throw new Error("material-suggestion-not-allowed");
  }
  const intent = await requireCurrentIntent(session, library);
  const assets = await library.listAssets();
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const catalog = resolveHistoricalMaterialCatalog(
    session, await library.listSegments(), assets, await library.listCreativeSessions(),
  );
  const eligible = catalog.segments;
  const ranked = await rankMaterialsRetryably(matcher, { intent, segments: eligible });
  const segmentsById = new Map(eligible.map((segment) => [segment.id, segment]));
  if (ranked.some((match) => !segmentsById.has(match.segmentId))) {
    throw new Error("material-no-longer-eligible");
  }
  const seen = new Set<string>();
  const suggestions = ranked.flatMap((match) => {
    if (seen.has(match.segmentId)) return [];
    seen.add(match.segmentId);
    const segment = segmentsById.get(match.segmentId);
    const asset = segment ? assetsById.get(segment.assetId) : undefined;
    if (!segment || !asset) return [];
    return [{
      segmentId: segment.id,
      thumbnailPath: asset.localPath,
      capturedAt: asset.importedAt,
      productId: segment.tags.products[0]?.id ?? null,
      processStage: segment.tags.processStages[0] ?? null,
      reason: match.reason,
      source: asset.origin,
      permissionState: segment.permissionState,
      lastUsedAt: segment.tags.usage.lastUsedAt,
      technicallyAvailable: true,
    }];
  });
  const existing = await library.listMaterialSuggestionSets();
  const suggestionSet: MaterialSuggestionSet = {
    id: `material_suggestion_set_${randomUUID()}`,
    sessionId: session.id,
    version: nextSuggestionVersion(existing, session.id),
    suggestions,
    createdAt: input.now,
  };
  await library.putMaterialSuggestionSet(suggestionSet, {
    suggestionSetId: suggestionSet.id,
    purpose: "pre-shoot-script",
    sources: catalog.sources,
  });

  return {
    ...suggestionSet,
    suggestions: [],
    count: suggestions.length,
    hasSameProductMatches: suggestions.some((suggestion) =>
      intent.products.some((product) => product.id === suggestion.productId),
    ),
  };
}

async function rankMaterialsRetryably(
  matcher: MaterialMatcher,
  input: Parameters<MaterialMatcher["rank"]>[0],
): Promise<Awaited<ReturnType<MaterialMatcher["rank"]>>> {
  try {
    return await matcher.rank(input);
  } catch (error) {
    throw new Error("creative-material-matcher-retryable", { cause: error });
  }
}

export async function decideMaterialViewing(
  input: {
    sessionId: string;
    suggestionSetId: string;
    decision: "accept" | "decline";
    actorId: string;
    now: string;
  },
  library: ContentLibrary,
): Promise<MaterialUseDecision> {
  requireActor(input.actorId);
  const current = await requireSession(input.sessionId, library);
  if (current.stage !== "awaiting_material_consent") {
    throw new Error("material-view-decision-not-allowed");
  }
  const suggestionSet = await requireCurrentSuggestionSet(input.sessionId, input.suggestionSetId, library);
  const shownSegmentIds = suggestionSet.suggestions.map((suggestion) => suggestion.segmentId);
  const action = input.decision === "accept" ? "accept-view" : "decline-view";
  const nextStage = input.decision === "accept" ? "reviewing_materials" : "materials_declined";
  if (!isCreativeSessionTransitionAllowed(current.stage, nextStage)) {
    throw new Error("creative-session-transition-not-allowed");
  }
  const decision: MaterialUseDecision = {
    id: `material_use_decision_${randomUUID()}`,
    sessionId: current.id,
    suggestionSetId: suggestionSet.id,
    suggestionVersion: suggestionSet.version,
    action,
    shownSegmentIds,
    selectedSegmentIds: [],
    rejectedSegmentIds: input.decision === "decline" ? shownSegmentIds : [],
    actorId: input.actorId,
    createdAt: input.now,
  };
  const next: CreativeSession = {
    ...current,
    stage: nextStage,
    materialViewDecisionIds: [...current.materialViewDecisionIds, decision.id],
    updatedAt: input.now,
  };
  await library.commitCreativeBundle({
    session: next,
    decisions: [decision],
    auditEvents: [creativeSessionAudit({
      sessionId: current.id,
      actorId: input.actorId,
      reason: input.decision === "accept"
        ? "customer accepted historical material viewing"
        : "customer declined historical material viewing",
      beforeStage: current.stage,
      afterStage: next.stage,
      now: input.now,
    })],
  });
  return decision;
}

export async function confirmMaterialSelection(
  input: {
    sessionId: string;
    suggestionSetId: string;
    selectedSegmentIds: string[];
    actorId: string;
    now: string;
  },
  library: ContentLibrary,
): Promise<MaterialUseDecision> {
  requireActor(input.actorId);
  const current = await requireSession(input.sessionId, library);
  const suggestionSet = await requireCurrentSuggestionSet(input.sessionId, input.suggestionSetId, library);
  if (current.stage !== "reviewing_materials") {
    throw new Error("material-selection-not-allowed");
  }
  const previousDecisions = await library.listMaterialUseDecisions();
  if (!previousDecisions.some((decision) =>
    current.materialViewDecisionIds.includes(decision.id) &&
    decision.sessionId === current.id &&
    decision.suggestionSetId === suggestionSet.id &&
    decision.action === "accept-view",
  )) {
    throw new Error("material-view-acceptance-required");
  }
  if (new Set(input.selectedSegmentIds).size !== input.selectedSegmentIds.length) {
    throw new Error("material-selection-segment-duplicate");
  }
  const shownSegmentIds = suggestionSet.suggestions.map((suggestion) => suggestion.segmentId);
  if (input.selectedSegmentIds.some((id) => !shownSegmentIds.includes(id))) {
    throw new Error("material-selection-segment-not-shown");
  }
  const segmentsById = new Map((await library.listSegments()).map((segment) => [segment.id, segment]));
  if (input.selectedSegmentIds.some((id) => !isEligible(segmentsById.get(id)))) {
    throw new Error("material-selection-segment-no-longer-eligible");
  }
  const nextStage = "awaiting_script_confirmation" as const;
  if (!isCreativeSessionTransitionAllowed(current.stage, nextStage)) {
    throw new Error("creative-session-transition-not-allowed");
  }
  const decision: MaterialUseDecision = {
    id: `material_use_decision_${randomUUID()}`,
    sessionId: current.id,
    suggestionSetId: suggestionSet.id,
    suggestionVersion: suggestionSet.version,
    action: "confirm-selection",
    shownSegmentIds,
    selectedSegmentIds: input.selectedSegmentIds,
    rejectedSegmentIds: shownSegmentIds.filter((id) => !input.selectedSegmentIds.includes(id)),
    actorId: input.actorId,
    createdAt: input.now,
  };
  const next: CreativeSession = {
    ...current,
    stage: nextStage,
    materialUseDecisionIds: [...current.materialUseDecisionIds, decision.id],
    updatedAt: input.now,
  };
  await library.commitCreativeBundle({
    session: next,
    requiredEligibleHistoricalSegmentIds: input.selectedSegmentIds,
    decisions: [decision],
    auditEvents: [creativeSessionAudit({
      sessionId: current.id,
      actorId: input.actorId,
      reason: "customer confirmed historical material selection",
      beforeStage: current.stage,
      afterStage: next.stage,
      now: input.now,
    })],
  });
  return decision;
}

function isEligible(segment: ContentSegment | undefined): segment is ContentSegment {
  return segment !== undefined &&
    segment.permissionState === "allowed" &&
    segment.tags.technicalFlags.length === 0 &&
    (segment.tags.products.length === 0 ||
      segment.tags.products.every((product) => product.status === "confirmed"));
}

async function requireSession(sessionId: string, library: ContentLibrary): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error(`creative-session-not-found:${sessionId}`);
  return session;
}

async function requireCurrentIntent(
  session: CreativeSession,
  library: ContentLibrary,
): Promise<CreativeIntent> {
  const intent = (await library.listCreativeIntents()).find(
    (item) => item.sessionId === session.id && item.version === session.currentIntentVersion,
  );
  if (!intent) throw new Error("creative-intent-not-found");
  return intent;
}

async function requireCurrentSuggestionSet(
  sessionId: string,
  suggestionSetId: string,
  library: ContentLibrary,
): Promise<MaterialSuggestionSet> {
  const suggestionSets = await library.listMaterialSuggestionSets();
  const suggestionSet = suggestionSets.find((item) => item.id === suggestionSetId);
  if (!suggestionSet) throw new Error(`material-suggestion-set-not-found:${suggestionSetId}`);
  if (suggestionSet.sessionId !== sessionId) {
    throw new Error("material-suggestion-set-session-mismatch");
  }
  const currentVersion = Math.max(
    ...suggestionSets.filter((item) => item.sessionId === sessionId).map((item) => item.version),
  );
  if (suggestionSet.version !== currentVersion) {
    throw new Error("material-suggestion-set-not-current");
  }
  return suggestionSet;
}

function nextSuggestionVersion(suggestionSets: MaterialSuggestionSet[], sessionId: string): number {
  return Math.max(0, ...suggestionSets.filter((item) => item.sessionId === sessionId).map((item) => item.version)) + 1;
}

function requireActor(actorId: string): void {
  if (actorId.trim().length === 0) throw new Error("material-decision-actor-required");
}

function postShootMaterialContext(input: { sessionId: string; suggestionSetId: string }, state: CreativeMaterialState) {
  const session = state.sessions.find((item) => item.id === input.sessionId);
  if (!session) throw new Error(`creative-session-not-found:${input.sessionId}`);
  if (session.stage !== "ingesting" && session.stage !== "editorial_review") {
    throw new Error("post-shoot-material-decision-not-allowed");
  }
  const set = state.sets.find((item) => item.id === input.suggestionSetId);
  if (!set) throw new Error(`material-suggestion-set-not-found:${input.suggestionSetId}`);
  if (set.sessionId !== session.id) throw new Error("material-suggestion-set-session-mismatch");
  if (set.version !== Math.max(...state.sets.filter((item) => item.sessionId === session.id).map((item) => item.version))) {
    throw new Error("material-suggestion-set-not-current");
  }
  if (set.purpose !== "post-shoot-edit") throw new Error("post-shoot-material-suggestion-required");
  return { session, set };
}

function sameStageAudit(session: CreativeSession, actorId: string, reason: string, now: string) {
  return { id: `audit_${randomUUID()}`, entityType: "creative-session" as const,
    entityId: session.id, actorId, action: "creative-session-updated", reason,
    before: { stage: session.stage }, after: { stage: session.stage }, createdAt: now };
}

function creativeSessionAudit(input: {
  sessionId: string;
  actorId: string;
  reason: string;
  beforeStage: CreativeSession["stage"];
  afterStage: CreativeSession["stage"];
  now: string;
}) {
  return {
    id: `audit_${randomUUID()}`,
    entityType: "creative-session" as const,
    entityId: input.sessionId,
    actorId: input.actorId,
    action: "creative-session-transitioned",
    reason: input.reason,
    before: { stage: input.beforeStage },
    after: { stage: input.afterStage },
    createdAt: input.now,
  };
}
