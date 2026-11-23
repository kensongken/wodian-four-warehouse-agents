import { createHash, randomUUID } from "node:crypto";

import type {
  ContentSegment,
  CreativeIntent,
  CreativeIntentAnswerOperation,
  CreativeSession,
  CreativeSessionStage,
  MaterialSuggestionSet,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";
import { isEligibleHistoricalSource, resolveHistoricalMaterialCatalog } from "./historical-source";
import { hasMaterialCatalogRaceCause } from "./material-catalog-race-error";
import type { MaterialMatcher } from "./material-advisor";
import { isCreativeSessionTransitionAllowed } from "./creative-session-policy";

export interface CreativeIntentDraft {
  platforms?: string[];
  audience?: string;
  theme?: string;
  products?: Array<string | { id: string; status?: "suggested" | "confirmed" }>;
  shootConditions?: string[];
  prohibitedAreas?: string[];
  tone?: string;
  durationSeconds?: number;
}

export interface IntentInterpreter {
  interpret(input: { text: string; answers: Record<string, string> }): Promise<CreativeIntentDraft>;
}

const unspecifiedIntentValue = "Unspecified";

export async function startCreativeSession(
  input: {
    customerId: string;
    title: string;
    text: string;
    actorId: string;
    now: string;
    idempotencyKey?: string;
  },
  interpreter: IntentInterpreter,
  library: ContentLibrary,
): Promise<{
  session: CreativeSession;
  intent: CreativeIntent;
  questions: string[];
}> {
  const stableSuffix = input.idempotencyKey
    ? createHash("sha256").update(JSON.stringify({
      operationKey: input.idempotencyKey,
      customerId: input.customerId,
      title: input.title,
      text: input.text,
    })).digest("hex").slice(0, 32)
    : null;
  const sessionId = stableSuffix
    ? `creative_session_${stableSuffix}`
    : `creative_session_${randomUUID()}`;
  if (stableSuffix) {
    const existing = (await library.listCreativeSessions()).find((session) => session.id === sessionId);
    if (existing) {
      if (existing.customerId !== input.customerId || existing.title !== input.title) {
        throw new Error("creative-session-operation-conflict");
      }
      const intent = latestIntentForSession(await library.listCreativeIntents(), existing.id);
      if (!intent) throw new Error("creative-intent-not-found");
      return { session: existing, intent, questions: missingIntentFacts(intent) };
    }
  }
  const draft = await interpretIntentRetryably(interpreter, { text: input.text, answers: {} });
  const questions = missingIntentFacts(draft);
  const intent = createIntent(sessionId, 1, draft, input.now,
    stableSuffix ? `creative_intent_${stableSuffix}` : undefined);
  const stage = questions.length === 0 ? "awaiting_material_consent" : "draft_intent";
  const session: CreativeSession = {
    id: sessionId,
    customerId: input.customerId,
    title: input.title,
    stage,
    currentIntentVersion: intent.version,
    currentBriefVersion: 0,
    currentScriptVersion: 0,
    currentShotPlanVersion: 0,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: normalizedStrings(draft.prohibitedAreas),
    status: "active",
    createdAt: input.now,
    updatedAt: input.now,
  };

  await library.commitCreativeBundle({
    session,
    intents: [intent],
    auditEvents: [{
      id: stableSuffix ? `audit_creative_session_${stableSuffix}` : `audit_${randomUUID()}`,
      entityType: "creative-session",
      entityId: session.id,
      actorId: input.actorId,
      action: "creative-session-created",
      reason: null,
      before: null,
      after: { stage: session.stage, intentVersion: intent.version },
      createdAt: input.now,
    }],
  });

  return { session, intent, questions };
}

export async function answerCreativeIntent(
  input: {
    sessionId: string;
    text: string;
    answers: Record<string, string>;
    actorId: string;
    now: string;
  },
  interpreter: IntentInterpreter,
  library: ContentLibrary,
): Promise<{
  session: CreativeSession;
  intent: CreativeIntent;
  questions: string[];
}> {
  const sessions = await library.listCreativeSessions();
  const current = sessions.find((session) => session.id === input.sessionId);
  if (!current) throw new Error(`creative-session-not-found:${input.sessionId}`);
  if (current.stage !== "draft_intent") throw new Error("creative-intent-answer-not-allowed");
  const priorIntent = latestIntentForSession(await library.listCreativeIntents(), current.id);
  if (!priorIntent) throw new Error("creative-intent-not-found");

  const returnedDraft = await interpretIntentRetryably(interpreter, {
    text: input.text,
    answers: input.answers,
  });
  const draft = mergeIntentDraft(priorIntent, returnedDraft);
  const intent = createIntent(current.id, priorIntent.version + 1, draft, input.now);
  const questions = missingIntentFacts(intent);
  const stage = questions.length === 0 ? "awaiting_material_consent" : "draft_intent";
  const session: CreativeSession = {
    ...current,
    stage,
    currentIntentVersion: intent.version,
    prohibitedAreas: intent.prohibitedAreas,
    updatedAt: input.now,
  };

  await library.commitCreativeBundle({
    session,
    intents: [intent],
    auditEvents: [{
      id: `audit_${randomUUID()}`,
      entityType: "creative-session",
      entityId: session.id,
      actorId: input.actorId,
      action: stage === current.stage ? "creative-intent-answered" : "creative-session-transitioned",
      reason: normalizedString(input.text),
      before: { stage: current.stage },
      after: { stage: session.stage },
      createdAt: input.now,
    }],
  });
  return { session, intent, questions };
}

export async function answerCreativeIntentRecoverably(
  input: Readonly<{
    sessionId: string;
    text: string;
    answers: Record<string, string>;
    actorId: string;
    customerId: string;
    clientInstanceId: string;
    route: string;
    bodyDigest: string;
    now: string;
    operationKey: string;
  }>,
  dependencies: Readonly<{
    library: ContentLibrary;
    materialMatcher: MaterialMatcher;
    intentInterpreter?: IntentInterpreter;
  }>,
): Promise<Readonly<{
  session: CreativeSession;
  intent: CreativeIntent;
  questions: string[];
  suggestionSet: MaterialSuggestionSet | null;
}>> {
  const operationId = `creative_intent_answer_operation_${createHash("sha256")
    .update(JSON.stringify([input.customerId, input.clientInstanceId, input.operationKey]))
    .digest("hex").slice(0, 32)}`;
  const existing = (await dependencies.library.listCreativeIntentAnswerOperations()).find(
    (operation) => operation.clientInstanceId === input.clientInstanceId &&
      operation.operationKey === input.operationKey,
  );
  let operation: CreativeIntentAnswerOperation;
  if (existing) {
    assertSameIntentAnswerOperation(existing, input, operationId);
    operation = existing;
  } else {
    const session = await requireIntentAnswerSession(input.sessionId, input.customerId, dependencies.library);
    if (session.stage !== "draft_intent") throw new Error("creative-intent-answer-not-allowed");
    operation = {
      id: operationId,
      operationKey: input.operationKey,
      route: input.route,
      bodyDigest: input.bodyDigest,
      customerId: input.customerId,
      clientInstanceId: input.clientInstanceId,
      creativeSessionId: input.sessionId,
      intentVersion: null,
      suggestionSetId: null,
      phase: "answer-pending",
      updatedAt: input.now,
    };
    await dependencies.library.putCreativeIntentAnswerOperation(operation);
  }

  if (operation.phase === "answer-pending") {
    if (!dependencies.intentInterpreter) throw new Error("creative-intent-interpreter-required");
    const prepared = await prepareIntentAnswer(input, dependencies.intentInterpreter, dependencies.library, operation.id);
    operation = { ...operation, intentVersion: prepared.intent.version, phase: "answer-stored", updatedAt: input.now };
    await dependencies.library.commitCreativeBundle({
      session: prepared.session,
      expectedSessionSnapshot: prepared.priorSession,
      intents: [prepared.intent],
      intentAnswerOperation: operation,
      auditEvents: [prepared.auditEvent],
    });
  }

  let state = await requireStoredIntentAnswer(operation, dependencies.library);
  if (operation.phase === "answer-stored") {
    if (state.questions.length > 0) {
      operation = { ...operation, phase: "complete", updatedAt: input.now };
      await dependencies.library.putCreativeIntentAnswerOperation(operation);
      return { ...state, suggestionSet: null };
    }
    if (state.session.stage !== "draft_intent") {
      throw new Error("creative-intent-answer-operation-state-invalid");
    }
    const preparedSuggestion = await prepareIntentMaterialSuggestion(
      state.session, state.intent, operation, input.now, dependencies.materialMatcher, dependencies.library,
    );
    const suggestionSet = preparedSuggestion.suggestionSet;
    operation = {
      ...operation,
      suggestionSetId: suggestionSet.id,
      phase: "suggestion-stored",
      updatedAt: input.now,
    };
    const awaitingConsent: CreativeSession = {
      ...state.session,
      stage: "awaiting_material_consent",
      updatedAt: input.now,
    };
    try {
      await dependencies.library.commitCreativeBundle({
        session: awaitingConsent,
        expectedSessionSnapshot: state.session,
        materialSuggestionSets: [suggestionSet],
        materialSuggestionPreconditions: [preparedSuggestion.precondition],
        requiredEligibleHistoricalSegmentIds: suggestionSet.suggestions.map((item) => item.segmentId),
        intentAnswerOperation: operation,
        auditEvents: [{
          id: `audit_${operation.id}_material_suggestion`,
          entityType: "creative-session",
          entityId: state.session.id,
          actorId: input.actorId,
          action: "creative-session-transitioned",
          reason: "intent complete and historical material suggestion prepared",
          before: { stage: "draft_intent" },
          after: { stage: "awaiting_material_consent" },
          createdAt: input.now,
        }],
      });
    } catch (error) {
      if (hasMaterialCatalogRaceCause(error)) {
        throw new Error("creative-material-catalog-race-retryable", { cause: error });
      }
      throw error;
    }
    state = { ...state, session: awaitingConsent };
  }

  const suggestionSet = operation.suggestionSetId === null
    ? null
    : (await dependencies.library.listMaterialSuggestionSets()).find(
        (set) => set.id === operation.suggestionSetId && set.sessionId === operation.creativeSessionId,
      ) ?? null;
  if (suggestionSet) {
    await assertRecoverableIntentSuggestion(state.session, suggestionSet, dependencies.library);
  }
  if (operation.phase === "suggestion-stored") {
    if (!suggestionSet) throw new Error("material-suggestion-set-not-found");
    operation = { ...operation, phase: "complete", updatedAt: input.now };
    await dependencies.library.putCreativeIntentAnswerOperation(operation);
  }
  if (operation.phase !== "complete") throw new Error("creative-intent-answer-operation-state-invalid");
  return { ...state, suggestionSet };
}

async function assertRecoverableIntentSuggestion(
  session: CreativeSession,
  suggestionSet: MaterialSuggestionSet,
  library: ContentLibrary,
): Promise<void> {
  const allowedStage = session.stage === "awaiting_material_consent" ||
    session.stage === "reviewing_materials" || session.stage === "materials_declined" ||
    session.stage === "awaiting_script_confirmation" || session.stage === "ready_to_dispatch" ||
    session.stage === "shooting" || session.stage === "ingesting" ||
    session.stage === "editorial_review" || session.stage === "completed";
  const suggestionSets = await library.listMaterialSuggestionSets();
  const latestPreShootVersion = Math.max(...suggestionSets.filter((item) =>
    item.sessionId === session.id && item.purpose !== "post-shoot-edit").map((item) => item.version));
  const [allSegments, allAssets] = await Promise.all([library.listSegments(), library.listAssets()]);
  const segments = new Map(allSegments.map((segment) => [segment.id, segment]));
  const assets = new Map(allAssets.map((asset) => [asset.id, asset]));
  const permissionInvalid = suggestionSet.suggestions.some((suggestion) => {
    const segment = segments.get(suggestion.segmentId);
    return !isEligibleHistoricalSource(segment, assets.get(segment?.assetId ?? ""));
  });
  if (!allowedStage || suggestionSet.version !== latestPreShootVersion || permissionInvalid) {
    throw new Error("creative-intent-answer-operation-state-invalid");
  }
}

async function prepareIntentAnswer(
  input: Readonly<{ sessionId: string; text: string; answers: Record<string, string>; actorId: string; customerId: string; now: string }>,
  interpreter: IntentInterpreter,
  library: ContentLibrary,
  operationId: string,
) {
  const current = await requireIntentAnswerSession(input.sessionId, input.customerId, library);
  if (current.stage !== "draft_intent") throw new Error("creative-intent-answer-not-allowed");
  const priorIntent = latestIntentForSession(await library.listCreativeIntents(), current.id);
  if (!priorIntent) throw new Error("creative-intent-not-found");
  const returnedDraft = await interpretIntentRetryably(interpreter, {
    text: input.text,
    answers: input.answers,
  });
  const intent = createIntent(current.id, priorIntent.version + 1,
    mergeIntentDraft(priorIntent, returnedDraft), input.now, `creative_intent_${operationId}`);
  const questions = missingIntentFacts(intent);
  const session: CreativeSession = {
    ...current,
    stage: "draft_intent",
    currentIntentVersion: intent.version,
    prohibitedAreas: intent.prohibitedAreas,
    updatedAt: input.now,
  };
  return {
    priorSession: current,
    session,
    intent,
    auditEvent: {
      id: `audit_${operationId}`,
      entityType: "creative-session" as const,
      entityId: session.id,
      actorId: input.actorId,
      action: "creative-intent-answered",
      reason: normalizedString(input.text) ?? "customer answered creative intent",
      before: { stage: current.stage },
      after: { stage: session.stage },
      createdAt: input.now,
    },
  };
}

async function requireStoredIntentAnswer(
  operation: CreativeIntentAnswerOperation,
  library: ContentLibrary,
) {
  if (operation.intentVersion === null) throw new Error("creative-intent-answer-operation-state-invalid");
  const session = await requireIntentAnswerSession(operation.creativeSessionId, operation.customerId, library);
  const intent = (await library.listCreativeIntents()).find((item) =>
    item.sessionId === session.id && item.version === operation.intentVersion,
  );
  if (!intent || session.currentIntentVersion !== intent.version) {
    throw new Error("creative-intent-answer-operation-state-invalid");
  }
  return { session, intent, questions: missingIntentFacts(intent) };
}

async function prepareIntentMaterialSuggestion(
  session: CreativeSession,
  intent: CreativeIntent,
  operation: CreativeIntentAnswerOperation,
  now: string,
  matcher: MaterialMatcher,
  library: ContentLibrary,
): Promise<Readonly<{
  suggestionSet: MaterialSuggestionSet;
  precondition: import("./content-library").MaterialSuggestionCatalogPrecondition;
}>> {
  const assets = await library.listAssets();
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  const catalog = resolveHistoricalMaterialCatalog(
    session, await library.listSegments(), assets, await library.listCreativeSessions(),
  );
  const eligible = catalog.segments;
  let ranked: Awaited<ReturnType<MaterialMatcher["rank"]>>;
  try {
    ranked = await matcher.rank({ intent, segments: eligible });
  } catch (error) {
    throw new Error("creative-material-matcher-retryable", { cause: error });
  }
  const eligibleById = new Map(eligible.map((segment) => [segment.id, segment]));
  if (ranked.some((match) => !eligibleById.has(match.segmentId))) {
    throw new Error("material-no-longer-eligible");
  }
  const seen = new Set<string>();
  const suggestions = ranked.flatMap((match) => {
    if (seen.has(match.segmentId)) return [];
    seen.add(match.segmentId);
    const segment = eligibleById.get(match.segmentId);
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
    id: `material_suggestion_set_${operation.id}`,
    sessionId: session.id,
    version: Math.max(0, ...existing.filter((set) => set.sessionId === session.id)
      .map((set) => set.version)) + 1,
    suggestions,
    createdAt: now,
  };
  return {
    suggestionSet,
    precondition: {
      suggestionSetId: suggestionSet.id,
      purpose: "pre-shoot-script",
      sources: catalog.sources,
    },
  };
}

async function interpretIntentRetryably(
  interpreter: IntentInterpreter,
  input: Parameters<IntentInterpreter["interpret"]>[0],
): Promise<Awaited<ReturnType<IntentInterpreter["interpret"]>>> {
  try {
    return await interpreter.interpret(input);
  } catch (error) {
    throw new Error("creative-intent-interpreter-retryable", { cause: error });
  }
}

async function requireIntentAnswerSession(
  sessionId: string,
  customerId: string,
  library: ContentLibrary,
): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error(`creative-session-not-found:${sessionId}`);
  if (session.customerId !== customerId) throw new Error("creative-intent-answer-customer-mismatch");
  if (session.status !== "active") throw new Error("creative-session-not-active");
  return session;
}

function assertSameIntentAnswerOperation(
  operation: CreativeIntentAnswerOperation,
  input: Readonly<{ sessionId: string; customerId: string; clientInstanceId: string; operationKey: string; route: string; bodyDigest: string }>,
  operationId: string,
): void {
  if (operation.id !== operationId || operation.creativeSessionId !== input.sessionId ||
    operation.customerId !== input.customerId || operation.clientInstanceId !== input.clientInstanceId ||
    operation.operationKey !== input.operationKey || operation.route !== input.route ||
    operation.bodyDigest !== input.bodyDigest) {
    throw new Error("creative-intent-answer-operation-conflict");
  }
}

export async function transitionCreativeSession(
  input: {
    sessionId: string;
    to: CreativeSessionStage;
    actorId: string;
    reason: string;
    now: string;
  },
  library: ContentLibrary,
): Promise<CreativeSession> {
  if (input.actorId.trim().length === 0) throw new Error("creative-session-transition-actor-required");
  if (input.reason.trim().length === 0) throw new Error("creative-session-transition-reason-required");

  const sessions = await library.listCreativeSessions();
  const current = sessions.find((session) => session.id === input.sessionId);
  if (!current) throw new Error(`creative-session-not-found:${input.sessionId}`);
  if (!isCreativeSessionTransitionAllowed(current.stage, input.to)) {
    throw new Error("creative-session-transition-not-allowed");
  }
  if (current.stage === "draft_intent" && input.to === "awaiting_material_consent") {
    const intent = latestIntentForSession(await library.listCreativeIntents(), current.id);
    if (!intent || missingIntentFacts(intent).length > 0) {
      throw new Error("creative-intent-incomplete");
    }
  }
  if (input.to === "shooting" && sessions.some(
    (session) => session.id !== current.id && session.stage === "shooting",
  )) {
    throw new Error("active-shooting-session-exists");
  }

  const next: CreativeSession = {
    ...current,
    stage: input.to,
    status: input.to === "invalidated" ? "invalidated" : current.status,
    updatedAt: input.now,
  };
  await library.commitCreativeBundle({
    session: next,
    auditEvents: [{
      id: `audit_${randomUUID()}`,
      entityType: "creative-session",
      entityId: next.id,
      actorId: input.actorId,
      action: "creative-session-transitioned",
      reason: input.reason,
      before: { stage: current.stage },
      after: { stage: next.stage },
      createdAt: input.now,
    }],
  });
  return next;
}

function createIntent(
  sessionId: string,
  version: number,
  draft: CreativeIntentDraft,
  now: string,
  id = `creative_intent_${randomUUID()}`,
): CreativeIntent {
  return {
    id,
    sessionId,
    version,
    platforms: normalizedStrings(draft.platforms),
    audience: normalizedString(draft.audience) ?? unspecifiedIntentValue,
    theme: normalizedString(draft.theme) ?? unspecifiedIntentValue,
    products: normalizeProductSuggestions(draft.products),
    shootConditions: normalizedStrings(draft.shootConditions),
    prohibitedAreas: normalizedStrings(draft.prohibitedAreas),
    tone: normalizedString(draft.tone) ?? unspecifiedIntentValue,
    durationSeconds: validDuration(draft.durationSeconds) ?? 30,
    createdAt: now,
  };
}

function mergeIntentDraft(prior: CreativeIntent, returned: CreativeIntentDraft): CreativeIntentDraft {
  return {
    platforms: returned.platforms ?? prior.platforms,
    audience: returned.audience ?? prior.audience,
    theme: returned.theme ?? prior.theme,
    products: returned.products ?? prior.products,
    shootConditions: returned.shootConditions ?? prior.shootConditions,
    prohibitedAreas: returned.prohibitedAreas ?? prior.prohibitedAreas,
    tone: returned.tone ?? prior.tone,
    durationSeconds: returned.durationSeconds ?? prior.durationSeconds,
  };
}

function latestIntentForSession(
  intents: CreativeIntent[],
  sessionId: string,
): CreativeIntent | undefined {
  return intents
    .filter((intent) => intent.sessionId === sessionId)
    .sort((left, right) => right.version - left.version)[0];
}

function missingIntentFacts(draft: CreativeIntentDraft): string[] {
  const missing: string[] = [];
  if (!specifiedString(draft.theme)) missing.push("theme");
  if (normalizedStrings(draft.platforms).length === 0) missing.push("platforms");
  if (!specifiedString(draft.audience)) missing.push("audience");
  if (normalizedStrings(draft.shootConditions).length === 0) missing.push("shootConditions");
  if (normalizedStrings(draft.prohibitedAreas).length === 0) missing.push("prohibitedAreas");
  return missing;
}

function normalizeProductSuggestions(
  products: CreativeIntentDraft["products"],
): CreativeIntent["products"] {
  return (products ?? []).flatMap((product) => {
    const id = typeof product === "string" ? normalizedString(product) : normalizedString(product.id);
    return id ? [{ id, status: "suggested" as const }] : [];
  });
}

function normalizedStrings(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map(normalizedString).filter((value): value is string => value !== null))];
}

function normalizedString(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function specifiedString(value: string | undefined): string | null {
  const normalized = normalizedString(value);
  return normalized === unspecifiedIntentValue ? null : normalized;
}

function validDuration(value: number | undefined): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}
