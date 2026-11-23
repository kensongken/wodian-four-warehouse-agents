import { createHash } from "node:crypto";

import {
  ContentDemandSchema,
  FeedbackEventSchema,
  StudioContentTypeSchema,
  StudioPermissionStateSchema,
  type ContentDemand,
  type FeedbackEvent,
  type StudioContentType,
  type StudioPermissionState,
} from "@wearable/contracts";

import { CustomerMaterialLibrary } from "./customer-material-library";
import type { StudioScope } from "./studio-capabilities";

export const STUDIO_PREFERENCE_POLICY_VERSION = "studio-preference-policy-v1" as const;
export const STUDIO_PREFERENCE_MINIMUM_APPROVED_EVENTS = 3;
export const STUDIO_PREFERENCE_STEP = 0.05;
export const STUDIO_PREFERENCE_MAX_ABSOLUTE_ADJUSTMENT = 0.15;

export type StudioPreferenceSourceBinding = Readonly<{
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
}>;

export type StudioPreferenceCandidate = StudioPreferenceSourceBinding & Readonly<{
  materialCardId: string;
  customerId: string;
  sessionId: string;
  contentType: StudioContentType;
  eligible: boolean;
  permission: StudioPermissionState;
  technicalUsable: boolean;
  technicalBlockers: readonly string[];
  confirmedFacts: readonly string[];
  baseScore: number;
}>;

export type RankedStudioPreferenceCandidate = StudioPreferenceCandidate & Readonly<{
  preferenceAdjustment: number;
  score: number;
  reasons: readonly string[];
  preferencePolicyVersion: typeof STUDIO_PREFERENCE_POLICY_VERSION;
}>;

export type StudioSuggestionReviewPreferenceInput = Readonly<{
  operationKey: string;
  eventId: string;
  action: "accept-suggestion" | "correct-suggestion" | "reject-suggestion";
  target: string;
  correction: string | null;
  receiptJobId: string;
  source: StudioPreferenceSourceBinding;
  createdAt: string;
}>;

export type StudioMaterialSuitabilityPreferenceInput = Readonly<{
  operationKey: string;
  eventId: string;
  decision: "suitable" | "not-suitable";
  demandId: string;
  matchAssessmentId: string;
  receiptJobId: string;
  source: StudioPreferenceSourceBinding;
  reason: string;
  createdAt: string;
}>;

export type StudioDraftFeedbackPreferenceInput = Readonly<{
  operationKey: string;
  eventId: string;
  action: "adopt" | "reject";
  draftId: string;
  demandId: string;
  sources: readonly StudioPreferenceSourceBinding[];
  createdAt: string;
}>;

type StoredPreferenceEvent = Readonly<{
  id: string;
  sourceEventId: string;
  sampleKey: string;
  operationKey: string;
  bodyDigest: string;
  kind: "suggestion-review" | "material-suitability" | "draft-feedback";
  polarity: 1 | -1;
  signal: string;
  demandId: string | null;
  sources: readonly StudioPreferenceSourceBinding[];
  createdAt: string;
}>;

type NormalizedPreferenceEvent = Readonly<Record<string, unknown>> & Pick<StoredPreferenceEvent,
  "sourceEventId" | "sampleKey" | "operationKey" | "kind" | "polarity" | "signal" |
  "demandId" | "sources" | "createdAt">;

type PreferenceState = Readonly<{
  schemaVersion: 1;
  policyVersion: typeof STUDIO_PREFERENCE_POLICY_VERSION;
  actorId: string;
  customerId: string;
  sessionId: string;
  events: readonly StoredPreferenceEvent[];
}>;

type CanonicalDraftFeedbackState = Readonly<{
  schemaVersion: 1;
  policyVersion: typeof STUDIO_PREFERENCE_POLICY_VERSION;
  actorId: string;
  customerId: string;
  sessionId: string;
  events: readonly CanonicalDraftFeedbackRecord[];
}>;

type CanonicalDraftFeedbackRecord = Readonly<{
  feedback: FeedbackEvent;
  preference: StudioDraftFeedbackPreferenceInput;
}>;

type StudioPreferenceRankerOptions = Readonly<{ now?: () => string }>;

export class StudioPreferenceRanker {
  private static readonly queues = new Map<string, Promise<void>>();
  private readonly now: () => string;

  constructor(
    private readonly library: CustomerMaterialLibrary,
    options: StudioPreferenceRankerOptions = {},
  ) {
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async recordSuggestionReview(
    scope: StudioScope,
    input: StudioSuggestionReviewPreferenceInput,
  ): Promise<StoredPreferenceEvent> {
    return this.record(scope, normalizeSuggestionReview(input));
  }

  async reconcileSuggestionReviews(
    scope: StudioScope,
    inputs: readonly StudioSuggestionReviewPreferenceInput[],
  ): Promise<void> {
    if (!Array.isArray(inputs)) throw new Error("studio-preference-canonical-events-invalid");
    await this.replaceKind(scope, "suggestion-review", inputs.map(normalizeSuggestionReview));
  }

  async recordMaterialSuitability(
    scope: StudioScope,
    input: StudioMaterialSuitabilityPreferenceInput,
  ): Promise<StoredPreferenceEvent> {
    return this.record(scope, normalizeMaterialSuitability(input));
  }

  async reconcileMaterialSuitability(
    scope: StudioScope,
    inputs: readonly StudioMaterialSuitabilityPreferenceInput[],
  ): Promise<void> {
    if (!Array.isArray(inputs)) throw new Error("studio-preference-canonical-events-invalid");
    await this.replaceKind(scope, "material-suitability", inputs.map(normalizeMaterialSuitability));
  }

  async recordDraftFeedback(
    scope: StudioScope,
    input: StudioDraftFeedbackPreferenceInput,
  ): Promise<StoredPreferenceEvent> {
    return this.record(scope, normalizeDraftFeedback(input));
  }

  async commitCanonicalDraftFeedback(
    scopeValue: StudioScope,
    inputValue: StudioDraftFeedbackPreferenceInput,
    feedbackValue: FeedbackEvent,
  ): Promise<void> {
    const scope = parseScope(scopeValue);
    const input = parseDraftFeedbackInput(inputValue);
    const feedback = FeedbackEventSchema.parse(feedbackValue);
    const record = canonicalDraftFeedbackRecord(scope, input, feedback);
    await this.assertSourcesCurrent(scope, [normalizeDraftFeedback(input)]);
    await this.withScopeQueue(scope, async () => {
      const state = await this.loadCanonicalDraftFeedback(scope);
      const operation = state.events.find((event) =>
        event.preference.operationKey === input.operationKey);
      if (operation && canonicalDraftFeedbackDigest(operation) !== canonicalDraftFeedbackDigest(record)) {
        throw new Error("studio-draft-feedback-operation-key-conflict");
      }
      const sourceEvent = state.events.find((event) =>
        event.preference.eventId === input.eventId);
      if (sourceEvent && canonicalDraftFeedbackDigest(sourceEvent) !== canonicalDraftFeedbackDigest(record)) {
        throw new Error("studio-draft-feedback-source-event-conflict");
      }
      if (operation || sourceEvent) return;
      const next: CanonicalDraftFeedbackState = {
        ...state,
        events: [...state.events, record]
          .sort((left, right) => compareCodePoints(
            left.preference.eventId,
            right.preference.eventId,
          )),
      };
      await this.library.writeStudioDraftFeedbackState(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        next,
      );
    });
  }

  async reconcilePersistedDraftFeedback(scopeValue: StudioScope): Promise<void> {
    const scope = parseScope(scopeValue);
    const state = await this.loadCanonicalDraftFeedback(scope);
    await this.replaceKind(
      scope,
      "draft-feedback",
      state.events.map((event) => normalizeDraftFeedback(event.preference)),
    );
  }

  async rank(
    scopeValue: StudioScope,
    candidatesValue: readonly StudioPreferenceCandidate[],
    demandValue: ContentDemand,
  ): Promise<RankedStudioPreferenceCandidate[]> {
    const scope = parseScope(scopeValue);
    const demand = ContentDemandSchema.parse(demandValue);
    if (demand.customerId !== scope.customerId || demand.sessionId !== scope.sessionId || demand.status !== "current") {
      throw new Error("studio-preference-demand-scope-mismatch");
    }
    if (!Array.isArray(candidatesValue)) throw new Error("studio-preference-candidates-invalid");
    const candidates = candidatesValue.map((candidate) => parseCandidate(candidate, scope));
    const state = await this.load(scope);
    const eventCounts = countSignals(state.events, demand);
    return candidates.map((candidate) => {
      const contributions = state.events.flatMap((event) => {
        if (!eventAppliesToDemand(event, demand) || !event.sources.some((source) => sameBinding(source, candidate))) return [];
        const key = signalKey(event, demand);
        const count = eventCounts.get(key) ?? 0;
        if (count < STUDIO_PREFERENCE_MINIMUM_APPROVED_EVENTS) return [];
        return [{ key, polarity: event.polarity, signal: displaySignal(event, demand) }];
      });
      const unique = new Map(contributions.map((row) => [`${row.polarity}:${row.key}`, row]));
      const rawAdjustment = [...unique.values()].reduce((total, row) => {
        const count = eventCounts.get(row.key) ?? 0;
        const steps = Math.floor(count / STUDIO_PREFERENCE_MINIMUM_APPROVED_EVENTS);
        return total + row.polarity * steps * STUDIO_PREFERENCE_STEP;
      }, 0);
      const preferenceAdjustment = round(Math.max(
        -STUDIO_PREFERENCE_MAX_ABSOLUTE_ADJUSTMENT,
        Math.min(STUDIO_PREFERENCE_MAX_ABSOLUTE_ADJUSTMENT, rawAdjustment),
      ));
      const reasons = [...unique.values()]
        .sort((left, right) => compareCodePoints(left.signal, right.signal) || right.polarity - left.polarity)
        .map((row) => `${row.polarity > 0 ? "approved" : "rejected"}-preference:${row.signal}`);
      return Object.freeze({
        ...clone(candidate),
        preferenceAdjustment,
        score: round(candidate.baseScore + preferenceAdjustment),
        reasons: Object.freeze(reasons),
        preferencePolicyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
      });
    }).sort(compareRankedCandidates);
  }

  private async replaceKind(
    scopeValue: StudioScope,
    kind: StoredPreferenceEvent["kind"],
    normalizedEvents: readonly NormalizedPreferenceEvent[],
  ): Promise<void> {
    const scope = parseScope(scopeValue);
    if (normalizedEvents.some((event) => event.kind !== kind)) {
      throw new Error("studio-preference-canonical-events-invalid");
    }
    await this.assertSourcesCurrent(scope, normalizedEvents);
    const events = normalizedEvents.map((event) => buildStoredEvent(scope, event));
    if (
      new Set(events.map((event) => event.operationKey)).size !== events.length
      || new Set(events.map((event) => event.sourceEventId)).size !== events.length
    ) throw new Error("studio-preference-canonical-events-invalid");
    await this.withScopeQueue(scope, async () => {
      const state = await this.loadForReconciliation(scope);
      const next: PreferenceState = {
        ...state,
        events: [...state.events.filter((event) => event.kind !== kind), ...events]
          .sort(compareStoredPreferenceEvents),
      };
      await this.library.writeStudioPreferenceState(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        next,
      );
    });
  }

  private async assertSourcesCurrent(
    scope: StudioScope,
    normalizedEvents: readonly NormalizedPreferenceEvent[],
  ): Promise<void> {
    for (const normalized of normalizedEvents) {
      for (const source of normalized.sources) {
        const snapshot = await this.library.inspectAnalysisAsset(
          scope.actorId,
          scope.customerId,
          scope.sessionId,
          source.sourceAssetId,
        );
        if (snapshot.asset.digest !== source.sourceDigest) {
          throw new Error("studio-preference-source-binding-mismatch");
        }
      }
    }
  }

  private async record(
    scopeValue: StudioScope,
    normalized: NormalizedPreferenceEvent,
  ): Promise<StoredPreferenceEvent> {
    const scope = parseScope(scopeValue);
    await this.assertSourcesCurrent(scope, [normalized]);
    const event = buildStoredEvent(scope, normalized);
    return this.withScopeQueue(scope, async () => {
      const state = await this.load(scope);
      const operation = state.events.find((candidate) => candidate.operationKey === normalized.operationKey);
      if (operation && operation.bodyDigest !== event.bodyDigest) {
        throw new Error("studio-preference-operation-key-conflict");
      }
      if (operation) return clone(operation);
      const sourceEvent = state.events.find((candidate) => candidate.sourceEventId === normalized.sourceEventId);
      if (sourceEvent && sourceEvent.bodyDigest !== event.bodyDigest) {
        throw new Error("studio-preference-source-event-conflict");
      }
      if (sourceEvent) return clone(sourceEvent);
      const next: PreferenceState = {
        ...state,
        events: [...state.events, event].sort(compareStoredPreferenceEvents),
      };
      await this.library.writeStudioPreferenceState(scope.actorId, scope.customerId, scope.sessionId, next);
      return clone(event);
    });
  }

  private async load(scope: StudioScope): Promise<PreferenceState> {
    const value = await this.library.readStudioPreferenceState(scope.actorId, scope.customerId, scope.sessionId);
    if (value === null) return emptyState(scope);
    try {
      return parseState(value, scope);
    } catch {
      throw new Error("studio-preference-state-invalid");
    }
  }

  private async loadForReconciliation(scope: StudioScope): Promise<PreferenceState> {
    const value = await this.library.readStudioPreferenceState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    );
    if (value === null) return emptyState(scope);
    try {
      return parseState(value, scope);
    } catch {
      return emptyState(scope);
    }
  }

  private async loadCanonicalDraftFeedback(scope: StudioScope): Promise<CanonicalDraftFeedbackState> {
    const value = await this.library.readStudioDraftFeedbackState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    );
    if (value === null) return emptyCanonicalDraftFeedbackState(scope);
    try {
      return parseCanonicalDraftFeedbackState(value, scope);
    } catch {
      throw new Error("studio-draft-feedback-state-invalid");
    }
  }

  private async withScopeQueue<T>(scope: StudioScope, work: () => Promise<T>): Promise<T> {
    const key = this.library.studioPreferenceCoordinationKey(scope.actorId, scope.customerId, scope.sessionId);
    const previous = StudioPreferenceRanker.queues.get(key) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate, () => gate);
    StudioPreferenceRanker.queues.set(key, tail);
    await previous.catch(() => undefined);
    try {
      return await work();
    } finally {
      release();
      if (StudioPreferenceRanker.queues.get(key) === tail) StudioPreferenceRanker.queues.delete(key);
    }
  }
}

function normalizeSuggestionReview(
  input: StudioSuggestionReviewPreferenceInput,
): NormalizedPreferenceEvent {
  assertExactKeys(input, [
    "operationKey", "eventId", "action", "target", "correction", "receiptJobId", "source", "createdAt",
  ]);
  if (!["accept-suggestion", "correct-suggestion", "reject-suggestion"].includes(input.action)) {
    throw new Error("studio-preference-event-invalid");
  }
  const target = requireText(input.target);
  const correction = input.correction === null ? null : requireText(input.correction);
  if ((input.action === "correct-suggestion") !== (correction !== null)) {
    throw new Error("studio-preference-event-invalid");
  }
  const signal = input.action === "correct-suggestion" ? correction! : target;
  const source = parseBinding(input.source);
  const receiptJobId = requireText(input.receiptJobId);
  return {
    sourceEventId: requireText(input.eventId),
    sampleKey: digest(JSON.stringify({
      kind: "suggestion-review",
      source,
      action: input.action,
      humanSemantic: input.action === "correct-suggestion" ? correction : target,
    })),
    operationKey: requireText(input.operationKey),
    kind: "suggestion-review",
    polarity: input.action === "reject-suggestion" ? -1 : 1,
    signal,
    demandId: null,
    sources: [source],
    createdAt: requireDateTime(input.createdAt),
    receiptJobId,
    target,
    correction,
    action: input.action,
  };
}

function normalizeMaterialSuitability(
  input: StudioMaterialSuitabilityPreferenceInput,
): NormalizedPreferenceEvent {
  assertExactKeys(input, [
    "operationKey", "eventId", "decision", "demandId", "matchAssessmentId", "receiptJobId", "source", "reason", "createdAt",
  ]);
  if (input.decision !== "suitable" && input.decision !== "not-suitable") {
    throw new Error("studio-preference-event-invalid");
  }
  const demandId = requireText(input.demandId);
  const source = parseBinding(input.source);
  return {
    sourceEventId: requireText(input.eventId),
    sampleKey: digest(JSON.stringify({
      kind: "material-suitability",
      demandId,
      source,
      decision: input.decision,
    })),
    operationKey: requireText(input.operationKey),
    kind: "material-suitability",
    polarity: input.decision === "suitable" ? 1 : -1,
    signal: "material-suitability",
    demandId,
    sources: [source],
    createdAt: requireDateTime(input.createdAt),
    decision: input.decision,
    matchAssessmentId: requireText(input.matchAssessmentId),
    receiptJobId: requireText(input.receiptJobId),
    reason: requireText(input.reason),
  };
}

function normalizeDraftFeedback(
  inputValue: StudioDraftFeedbackPreferenceInput,
): NormalizedPreferenceEvent {
  const input = parseDraftFeedbackInput(inputValue);
  return {
    sourceEventId: input.eventId,
    sampleKey: digest(JSON.stringify({
      kind: "draft-feedback",
      demandId: input.demandId,
      sources: [...input.sources].sort(compareSourceBindings),
      action: input.action,
    })),
    operationKey: input.operationKey,
    kind: "draft-feedback",
    polarity: input.action === "adopt" ? 1 : -1,
    signal: "draft-adoption",
    demandId: input.demandId,
    sources: input.sources,
    createdAt: input.createdAt,
    action: input.action,
    draftId: input.draftId,
  };
}

function parseDraftFeedbackInput(
  input: StudioDraftFeedbackPreferenceInput,
): StudioDraftFeedbackPreferenceInput {
  assertExactKeys(input, [
    "operationKey", "eventId", "action", "draftId", "demandId", "sources", "createdAt",
  ]);
  if (input.action !== "adopt" && input.action !== "reject") {
    throw new Error("studio-preference-event-invalid");
  }
  if (!Array.isArray(input.sources) || input.sources.length === 0) {
    throw new Error("studio-preference-event-invalid");
  }
  const sources = input.sources.map(parseBinding).sort(compareSourceBindings);
  assertUniqueBindings(sources);
  return {
    operationKey: requireText(input.operationKey),
    eventId: requireText(input.eventId),
    action: input.action,
    draftId: requireText(input.draftId),
    demandId: requireText(input.demandId),
    sources,
    createdAt: requireDateTime(input.createdAt),
  };
}

function canonicalDraftFeedbackRecord(
  scope: StudioScope,
  preference: StudioDraftFeedbackPreferenceInput,
  feedback: FeedbackEvent,
): CanonicalDraftFeedbackRecord {
  if (
    feedback.customerId !== scope.customerId
    || feedback.sessionId !== scope.sessionId
    || feedback.actorId !== scope.actorId
    || feedback.id !== preference.eventId
    || feedback.draftId !== preference.draftId
    || feedback.action !== preference.action
    || feedback.createdAt !== preference.createdAt
    || feedback.publishClaimed
  ) throw new Error("studio-draft-feedback-canonical-binding-mismatch");
  return { feedback: clone(feedback), preference: clone(preference) };
}

function canonicalDraftFeedbackDigest(record: CanonicalDraftFeedbackRecord): string {
  const { createdAt: _feedbackCreatedAt, ...feedback } = record.feedback;
  const { createdAt: _preferenceCreatedAt, ...preference } = record.preference;
  return digest(JSON.stringify({ feedback, preference }));
}

function buildStoredEvent(
  scope: StudioScope,
  normalized: NormalizedPreferenceEvent,
): StoredPreferenceEvent {
  const { createdAt: _createdAt, ...idempotentBody } = normalized;
  return Object.freeze({
    id: `preference_${digest(`${scope.customerId}\0${scope.sessionId}\0${scope.actorId}\0${normalized.sourceEventId}`)}`,
    sourceEventId: normalized.sourceEventId,
    sampleKey: normalized.sampleKey,
    operationKey: normalized.operationKey,
    bodyDigest: digest(JSON.stringify(idempotentBody)),
    kind: normalized.kind,
    polarity: normalized.polarity,
    signal: normalized.signal,
    demandId: normalized.demandId,
    sources: Object.freeze(normalized.sources.map((source) => Object.freeze({ ...source }))),
    createdAt: normalized.createdAt,
  });
}

function compareStoredPreferenceEvents(
  left: StoredPreferenceEvent,
  right: StoredPreferenceEvent,
): number {
  return compareCodePoints(left.kind, right.kind)
    || compareCodePoints(left.sourceEventId, right.sourceEventId);
}

function emptyState(scope: StudioScope): PreferenceState {
  return {
    schemaVersion: 1,
    policyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
    actorId: scope.actorId,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    events: [],
  };
}

function emptyCanonicalDraftFeedbackState(scope: StudioScope): CanonicalDraftFeedbackState {
  return {
    schemaVersion: 1,
    policyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
    actorId: scope.actorId,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    events: [],
  };
}

function parseCanonicalDraftFeedbackState(
  value: unknown,
  scope: StudioScope,
): CanonicalDraftFeedbackState {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
  assertExactKeys(value, [
    "schemaVersion", "policyVersion", "actorId", "customerId", "sessionId", "events",
  ]);
  const state = value as Record<string, unknown>;
  if (
    state.schemaVersion !== 1
    || state.policyVersion !== STUDIO_PREFERENCE_POLICY_VERSION
    || state.actorId !== scope.actorId
    || state.customerId !== scope.customerId
    || state.sessionId !== scope.sessionId
    || !Array.isArray(state.events)
  ) throw new Error();
  const events = state.events.map((event) => {
    if (!event || typeof event !== "object" || Array.isArray(event)) throw new Error();
    assertExactKeys(event, ["feedback", "preference"]);
    const record = event as Record<string, unknown>;
    const preference = parseDraftFeedbackInput(
      record.preference as StudioDraftFeedbackPreferenceInput,
    );
    const feedback = FeedbackEventSchema.parse(record.feedback);
    return canonicalDraftFeedbackRecord(scope, preference, feedback);
  });
  if (
    new Set(events.map((event) => event.preference.operationKey)).size !== events.length
    || new Set(events.map((event) => event.preference.eventId)).size !== events.length
  ) throw new Error();
  return { ...emptyCanonicalDraftFeedbackState(scope), events };
}

function parseState(value: unknown, scope: StudioScope): PreferenceState {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
  assertExactKeys(value, ["schemaVersion", "policyVersion", "actorId", "customerId", "sessionId", "events"]);
  const state = value as Record<string, unknown>;
  if (
    state.schemaVersion !== 1 || state.policyVersion !== STUDIO_PREFERENCE_POLICY_VERSION
    || state.actorId !== scope.actorId || state.customerId !== scope.customerId || state.sessionId !== scope.sessionId
    || !Array.isArray(state.events)
  ) throw new Error();
  const events = state.events.map(parseStoredEvent);
  if (new Set(events.map((event) => event.operationKey)).size !== events.length) throw new Error();
  if (new Set(events.map((event) => event.sourceEventId)).size !== events.length) throw new Error();
  return { ...emptyState(scope), events };
}

function parseStoredEvent(value: unknown): StoredPreferenceEvent {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
  assertExactKeys(value, [
    "id", "sourceEventId", "sampleKey", "operationKey", "bodyDigest", "kind", "polarity", "signal", "demandId", "sources", "createdAt",
  ]);
  const event = value as Record<string, unknown>;
  if (
    !["suggestion-review", "material-suitability", "draft-feedback"].includes(String(event.kind))
    || (event.polarity !== 1 && event.polarity !== -1)
    || !Array.isArray(event.sources) || event.sources.length === 0
    || (event.demandId !== null && typeof event.demandId !== "string")
    || !/^[a-f0-9]{64}$/.test(String(event.sampleKey))
    || !/^[a-f0-9]{64}$/.test(String(event.bodyDigest))
  ) throw new Error();
  const sources = event.sources.map(parseBinding);
  assertUniqueBindings(sources);
  return {
    id: requireText(event.id),
    sourceEventId: requireText(event.sourceEventId),
    sampleKey: String(event.sampleKey),
    operationKey: requireText(event.operationKey),
    bodyDigest: String(event.bodyDigest),
    kind: event.kind as StoredPreferenceEvent["kind"],
    polarity: event.polarity,
    signal: requireText(event.signal),
    demandId: event.demandId === null ? null : requireText(event.demandId),
    sources,
    createdAt: requireDateTime(event.createdAt),
  };
}

function parseCandidate(value: StudioPreferenceCandidate, scope: StudioScope): StudioPreferenceCandidate {
  try {
    assertExactKeys(value, [
      "materialCardId", "customerId", "sessionId", "sourceAssetId", "sourceDigest",
      "sourceStartMs", "sourceEndMs", "contentType",
      "eligible", "permission", "technicalUsable", "technicalBlockers", "confirmedFacts", "baseScore",
    ]);
    const candidate = value as StudioPreferenceCandidate;
    const source = parseBinding({
      sourceAssetId: candidate.sourceAssetId,
      sourceDigest: candidate.sourceDigest,
      sourceStartMs: candidate.sourceStartMs,
      sourceEndMs: candidate.sourceEndMs,
    });
    const contentType = StudioContentTypeSchema.parse(candidate.contentType);
    const permission = StudioPermissionStateSchema.parse(candidate.permission);
    if (
      candidate.customerId !== scope.customerId || candidate.sessionId !== scope.sessionId
      ||
      typeof candidate.eligible !== "boolean" || typeof candidate.technicalUsable !== "boolean"
      || !Array.isArray(candidate.technicalBlockers) || !Array.isArray(candidate.confirmedFacts)
      || !Number.isFinite(candidate.baseScore) || candidate.baseScore < 0 || candidate.baseScore > 1
    ) throw new Error();
    const technicalBlockers = candidate.technicalBlockers.map(requireText);
    const confirmedFacts = candidate.confirmedFacts.map(requireText);
    if (new Set(technicalBlockers).size !== technicalBlockers.length || new Set(confirmedFacts).size !== confirmedFacts.length) {
      throw new Error();
    }
    return {
      materialCardId: requireText(candidate.materialCardId),
      customerId: requireText(candidate.customerId),
      sessionId: requireText(candidate.sessionId),
      ...source,
      contentType,
      eligible: candidate.eligible,
      permission,
      technicalUsable: candidate.technicalUsable,
      technicalBlockers,
      confirmedFacts,
      baseScore: candidate.baseScore,
    };
  } catch {
    throw new Error("studio-preference-candidate-invalid");
  }
}

function parseScope(value: StudioScope): StudioScope {
  if (!value || typeof value !== "object") throw new Error("studio-preference-scope-invalid");
  return {
    actorId: requireText(value.actorId),
    customerId: requireText(value.customerId),
    sessionId: requireText(value.sessionId),
  };
}

function parseBinding(value: unknown): StudioPreferenceSourceBinding {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("studio-preference-event-invalid");
  assertExactKeys(value, ["sourceAssetId", "sourceDigest", "sourceStartMs", "sourceEndMs"]);
  const source = value as Record<string, unknown>;
  if (
    typeof source.sourceAssetId !== "string" || !source.sourceAssetId.trim()
    || typeof source.sourceDigest !== "string" || !/^[a-f0-9]{64}$/.test(source.sourceDigest)
    || !Number.isSafeInteger(source.sourceStartMs) || (source.sourceStartMs as number) < 0
    || !Number.isSafeInteger(source.sourceEndMs) || (source.sourceEndMs as number) <= (source.sourceStartMs as number)
  ) throw new Error("studio-preference-event-invalid");
  return {
    sourceAssetId: source.sourceAssetId.trim(),
    sourceDigest: source.sourceDigest,
    sourceStartMs: source.sourceStartMs as number,
    sourceEndMs: source.sourceEndMs as number,
  };
}

function countSignals(events: readonly StoredPreferenceEvent[], demand: ContentDemand): Map<string, number> {
  const sets = new Map<string, Set<string>>();
  for (const event of events) {
    if (!eventAppliesToDemand(event, demand)) continue;
    const key = signalKey(event, demand);
    const ids = sets.get(key) ?? new Set<string>();
    ids.add(event.sampleKey);
    sets.set(key, ids);
  }
  return new Map([...sets].map(([key, ids]) => [key, ids.size]));
}

function eventAppliesToDemand(event: StoredPreferenceEvent, demand: ContentDemand): boolean {
  return event.demandId === null || event.demandId === demand.id;
}

function signalKey(event: StoredPreferenceEvent, demand: ContentDemand): string {
  return `${event.polarity}:${event.signal}:${event.demandId === null ? "global" : demand.contentType}`;
}

function displaySignal(event: StoredPreferenceEvent, demand: ContentDemand): string {
  return event.kind === "suggestion-review" ? event.signal : `${event.signal}:${demand.contentType}`;
}

function sameBinding(left: StudioPreferenceSourceBinding, right: StudioPreferenceSourceBinding): boolean {
  return left.sourceAssetId === right.sourceAssetId && left.sourceDigest === right.sourceDigest
    && left.sourceStartMs === right.sourceStartMs && left.sourceEndMs === right.sourceEndMs;
}

function assertUniqueBindings(sources: readonly StudioPreferenceSourceBinding[]): void {
  if (new Set(sources.map((source) => JSON.stringify(source))).size !== sources.length) {
    throw new Error("studio-preference-event-invalid");
  }
}

function assertExactKeys(value: object, expected: readonly string[]): void {
  if (Object.keys(value).sort().join("\0") !== [...expected].sort().join("\0")) {
    throw new Error("studio-preference-event-invalid");
  }
}

function requireText(value: unknown): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > 1_000) {
    throw new Error("studio-preference-event-invalid");
  }
  return value.trim();
}

function requireDateTime(value: unknown): string {
  const text = requireText(value);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(text)) {
    throw new Error("studio-preference-event-invalid");
  }
  return text;
}

function compareRankedCandidates(
  left: RankedStudioPreferenceCandidate,
  right: RankedStudioPreferenceCandidate,
): number {
  return Number(right.eligible) - Number(left.eligible)
    || right.score - left.score
    || compareCodePoints(left.materialCardId, right.materialCardId);
}

function compareSourceBindings(
  left: StudioPreferenceSourceBinding,
  right: StudioPreferenceSourceBinding,
): number {
  return compareCodePoints(JSON.stringify(left), JSON.stringify(right));
}

function compareCodePoints(left: string, right: string): number {
  const leftScalars = Array.from(left);
  const rightScalars = Array.from(right);
  const length = Math.min(leftScalars.length, rightScalars.length);
  for (let index = 0; index < length; index += 1) {
    const leftValue = leftScalars[index]!.codePointAt(0)!;
    const rightValue = rightScalars[index]!.codePointAt(0)!;
    if (leftValue !== rightValue) return leftValue < rightValue ? -1 : 1;
  }
  return leftScalars.length - rightScalars.length;
}

function round(value: number): number {
  return Math.round((value + Number.EPSILON) * 1_000_000) / 1_000_000;
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function clone<T>(value: T): T {
  return structuredClone(value);
}
