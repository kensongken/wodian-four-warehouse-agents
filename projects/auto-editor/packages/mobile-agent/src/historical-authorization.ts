import {
  HistoricalMaterialAuthorizationSchema,
  type CreativeSession,
  type HistoricalMaterialConsent,
  type MaterialSuggestionSet,
  type MaterialUseDecision,
} from "@wearable/contracts";
import type { ContentLibrary } from "./content-library";

type ConsentRecords = Readonly<{
  sessions: readonly CreativeSession[];
  sets: readonly MaterialSuggestionSet[];
  decisions: readonly MaterialUseDecision[];
}>;

export function validateHistoricalMaterialAuthorization(
  authorization: HistoricalMaterialConsent | undefined,
  batchId: string,
  records: ConsentRecords,
): HistoricalMaterialConsent {
  // JavaScript callers and old persisted commands fail closed, too.
  if (authorization === undefined) return { selectedSegmentIds: [] };
  if (Object.keys(authorization).length === 1 && Array.isArray(authorization.selectedSegmentIds) &&
    authorization.selectedSegmentIds.length === 0) return { selectedSegmentIds: [] };
  const parsed = HistoricalMaterialAuthorizationSchema.safeParse(authorization);
  if (!parsed.success) throw new Error("historical-authorization-invalid");
  const value = parsed.data;
  const session = records.sessions.find((item) => item.id === value.creativeSessionId);
  const current = latestSet(value.creativeSessionId, records.sets);
  const decision = current ? latestDecision(current, records.decisions) : undefined;
  if (!session || session.status !== "active" || session.privacyMode === "paused" ||
    session.editorialBatchId !== batchId || !current || !decision ||
    current.id !== value.suggestionSetId || current.version !== value.suggestionSetVersion ||
    decision.id !== value.decisionId || decision.action !== "confirm-selection" ||
    decision.suggestionVersion !== current.version ||
    !session.materialUseDecisionIds.includes(decision.id) ||
    !sameIds(value.selectedSegmentIds, decision.selectedSegmentIds) ||
    value.selectedSegmentIds.some((id) => !current.suggestions.some((item) =>
      item.segmentId === id && item.permissionState === "allowed" && item.technicallyAvailable))) {
    throw new Error("historical-authorization-invalid");
  }
  return { ...value, selectedSegmentIds: [...value.selectedSegmentIds].sort() };
}

export async function resolveHistoricalMaterialAuthorization(
  creativeSessionId: string,
  library: ContentLibrary,
): Promise<HistoricalMaterialConsent> {
  return resolveHistoricalMaterialAuthorizationFromRecords(creativeSessionId, await historicalConsentRecords(library));
}

export function resolveHistoricalMaterialAuthorizationFromRecords(
  creativeSessionId: string,
  records: ConsentRecords,
): HistoricalMaterialConsent {
  const session = records.sessions.find((item) => item.id === creativeSessionId);
  if (!session) throw new Error(`creative-session-not-found:${creativeSessionId}`);
  const current = latestSet(creativeSessionId, records.sets);
  const decision = current ? latestDecision(current, records.decisions) : undefined;
  if (!current || !decision || decision.action !== "confirm-selection") return { selectedSegmentIds: [] };
  return validateHistoricalMaterialAuthorization({
    creativeSessionId, suggestionSetId: current.id, suggestionSetVersion: current.version,
    decisionId: decision.id, selectedSegmentIds: decision.selectedSegmentIds,
  }, session.editorialBatchId ?? "", records);
}

export async function historicalConsentRecords(library: ContentLibrary): Promise<ConsentRecords> {
  const [sessions, sets, decisions] = await Promise.all([
    library.listCreativeSessions(), library.listMaterialSuggestionSets(), library.listMaterialUseDecisions(),
  ]);
  return { sessions, sets, decisions };
}

function latestSet(sessionId: string, sets: readonly MaterialSuggestionSet[]) {
  return sets.filter((set) => set.sessionId === sessionId && set.purpose === "post-shoot-edit")
    .sort((left, right) => right.version - left.version || right.createdAt.localeCompare(left.createdAt))[0];
}

function latestDecision(set: MaterialSuggestionSet, decisions: readonly MaterialUseDecision[]) {
  return decisions.filter((item) => item.sessionId === set.sessionId && item.suggestionSetId === set.id &&
    item.purpose === "post-shoot-edit")
    .map((item, index) => ({ item, index }))
    .sort((a, b) => b.item.createdAt.localeCompare(a.item.createdAt) || b.index - a.index)[0]?.item;
}

function sameIds(left: readonly string[], right: readonly string[]) {
  return left.length === right.length && left.every((id) => right.includes(id));
}
