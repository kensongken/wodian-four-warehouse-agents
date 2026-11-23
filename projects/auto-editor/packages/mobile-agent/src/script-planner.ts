import { randomUUID } from "node:crypto";

import {
  ScriptPackageSchema,
  type AuditEvent,
  type ContentSegment,
  type CreativeBrief,
  type CreativeIntent,
  type CreativeSession,
  type MaterialUseDecision,
  type ScriptPackage,
} from "@wearable/contracts";

import type { ContentLibrary, CreativeBundleCommit } from "./content-library";
import { isCreativeSessionTransitionAllowed } from "./creative-session-policy";

export type ScriptDraft = Pick<
  ScriptPackage,
  "title" | "platformDirection" | "opening" | "narrativeOrder" | "sections"
> & {
  changeExplanation?: string | null;
};

export interface CreativeScriptAgent {
  draft(input: {
    intent: CreativeIntent;
    selectedHistoricalSegments: ContentSegment[];
  }): Promise<ScriptDraft>;
  revise(input: {
    current: ScriptPackage;
    prompt: string;
    selectedHistoricalSegments: ContentSegment[];
  }): Promise<ScriptDraft>;
}

export async function generateScriptPackage(
  input: { sessionId: string; actorId: string; now: string },
  agent: CreativeScriptAgent,
  library: ContentLibrary,
): Promise<ScriptPackage> {
  requireActor(input.actorId);
  const session = await requireSession(input.sessionId, library);
  if (session.currentScriptVersion !== 0) throw new Error("script-already-generated");
  const intent = await requireCurrentIntent(session, library);
  const authorization = await requireHistoricalAuthorization(session, library);
  requireGenerationStage(session, authorization.decision);
  const selectedSegments = await requireSelectedSegments(authorization.selectedIds, library);
  const draft = await agent.draft({
    intent,
    selectedHistoricalSegments: selectedSegments,
  });
  validateDraftProvenance(draft, new Set(authorization.selectedIds));

  const briefVersion = session.currentBriefVersion + 1;
  const scriptVersion = session.currentScriptVersion + 1;
  const brief: CreativeBrief = {
    id: `creative_brief_${randomUUID()}`,
    sessionId: session.id,
    intentVersion: intent.version,
    briefVersion,
    title: session.title,
    platformDirection: intent.platforms.join(", "),
    audience: intent.audience,
    theme: intent.theme,
    tone: intent.tone,
    durationSeconds: intent.durationSeconds,
    confirmedSegmentIds: authorization.selectedIds,
    status: "active",
    createdAt: input.now,
  };
  const script = checkedScriptPackage({
    id: `script_${randomUUID()}`,
    sessionId: session.id,
    briefVersion,
    scriptVersion,
    parentScriptId: null,
    designerPrompt: null,
    changeExplanation: null,
    status: "active",
    title: draft.title,
    platformDirection: draft.platformDirection,
    opening: draft.opening,
    narrativeOrder: draft.narrativeOrder,
    sections: draft.sections,
    confirmedAt: null,
    createdAt: input.now,
  });
  const next: CreativeSession = {
    ...session,
    stage: "awaiting_script_confirmation",
    currentBriefVersion: briefVersion,
    currentScriptVersion: scriptVersion,
    updatedAt: input.now,
  };
  await library.commitCreativeBundle({
    session: next,
    expectedSession: sessionPrecondition(session),
    requiredEligibleHistoricalSegmentIds: authorization.selectedIds,
    creativeBriefs: [brief],
    scripts: [script],
    auditEvents: [sessionMutationAudit({
      sessionId: session.id,
      actorId: input.actorId,
      action: session.stage === next.stage ? "script-generated" : "creative-session-transitioned",
      reason: "generated script from final material authorization",
      before: session.stage === next.stage
        ? { briefVersion: session.currentBriefVersion, scriptVersion: session.currentScriptVersion }
        : { stage: session.stage },
      after: session.stage === next.stage
        ? { briefVersion, scriptVersion }
        : { stage: next.stage },
      now: input.now,
    })],
  });
  return script;
}

export type PreparedScriptPackageRevision = Readonly<{
  script: ScriptPackage;
  bundle: CreativeBundleCommit;
}>;

export async function reviseScriptPackage(
  input: { sessionId: string; prompt: string; actorId: string; now: string },
  agent: CreativeScriptAgent,
  library: ContentLibrary,
): Promise<ScriptPackage> {
  const prepared = await prepareScriptPackageRevision(input, agent, library);
  await library.commitCreativeBundle(prepared.bundle);
  return prepared.script;
}

export async function prepareScriptPackageRevision(
  input: { sessionId: string; prompt: string; actorId: string; now: string },
  agent: CreativeScriptAgent,
  library: ContentLibrary,
): Promise<PreparedScriptPackageRevision> {
  requireActor(input.actorId);
  if (input.prompt.trim().length === 0) throw new Error("script-revision-prompt-required");
  const session = await requireSession(input.sessionId, library);
  if (!["awaiting_script_confirmation", "ready_to_dispatch", "shooting"].includes(session.stage)) {
    throw new Error("script-revision-not-allowed");
  }
  const current = await requireCurrentScript(session, library);
  const authorization = await requireHistoricalAuthorization(session, library);
  const selectedSegments = await requireSelectedSegments(authorization.selectedIds, library);
  const draft = await agent.revise({
    current,
    prompt: input.prompt,
    selectedHistoricalSegments: selectedSegments,
  });
  validateDraftProvenance(draft, new Set(authorization.selectedIds));
  if (!draft.changeExplanation || draft.changeExplanation.trim().length === 0) {
    throw new Error("script-change-explanation-required");
  }

  const scriptVersion = session.currentScriptVersion + 1;
  const script = checkedScriptPackage({
    id: `script_${randomUUID()}`,
    sessionId: session.id,
    briefVersion: current.briefVersion,
    scriptVersion,
    parentScriptId: current.id,
    designerPrompt: input.prompt,
    changeExplanation: draft.changeExplanation,
    status: "active",
    title: draft.title,
    platformDirection: draft.platformDirection,
    opening: draft.opening,
    narrativeOrder: draft.narrativeOrder,
    sections: draft.sections,
    confirmedAt: null,
    createdAt: input.now,
  });
  const next: CreativeSession = {
    ...session,
    stage: "awaiting_script_confirmation",
    currentScriptVersion: scriptVersion,
    currentShotPlanVersion: 0,
    updatedAt: input.now,
  };
  const bundle: CreativeBundleCommit = {
    session: next,
    expectedSession: sessionPrecondition(session),
    expectedScript: scriptPrecondition(current),
    requiredEligibleHistoricalSegmentIds: authorization.selectedIds,
    scripts: [script],
    invalidateTaskLineage: current.confirmedAt === null
      ? []
      : [{
          sessionId: current.sessionId,
          briefVersion: current.briefVersion,
          scriptVersion: current.scriptVersion,
        }],
    auditEvents: [sessionMutationAudit({
      sessionId: session.id,
      actorId: input.actorId,
      action: session.stage === next.stage ? "script-revised" : "creative-session-transitioned",
      reason: draft.changeExplanation,
      before: session.stage === next.stage
        ? { scriptVersion: session.currentScriptVersion }
        : { stage: session.stage },
      after: session.stage === next.stage
        ? { scriptVersion }
        : { stage: next.stage },
      now: input.now,
    })],
  };
  return { script, bundle };
}

export async function confirmScriptPackage(
  input: { sessionId: string; scriptVersion: number; actorId: string; now: string },
  library: ContentLibrary,
): Promise<ScriptPackage> {
  requireActor(input.actorId);
  const session = await requireSession(input.sessionId, library);
  if (input.scriptVersion !== session.currentScriptVersion) {
    throw new Error("script-version-not-current");
  }
  if (session.stage !== "awaiting_script_confirmation") {
    throw new Error("script-confirmation-not-allowed");
  }
  const current = await requireCurrentScript(session, library);
  if (current.confirmedAt !== null) throw new Error("script-already-confirmed");
  const authorization = await requireHistoricalAuthorization(session, library);
  await requireSelectedSegments(authorization.selectedIds, library);
  const confirmed = checkedScriptPackage({ ...current, confirmedAt: input.now });
  const next: CreativeSession = {
    ...session,
    stage: "ready_to_dispatch",
    updatedAt: input.now,
  };
  if (!isCreativeSessionTransitionAllowed(session.stage, next.stage)) {
    throw new Error("creative-session-transition-not-allowed");
  }
  await library.commitCreativeBundle({
    session: next,
    expectedSession: sessionPrecondition(session),
    expectedScript: scriptPrecondition(current),
    requiredEligibleHistoricalSegmentIds: authorization.selectedIds,
    scripts: [confirmed],
    auditEvents: [
      sessionMutationAudit({
        sessionId: session.id,
        actorId: input.actorId,
        action: "creative-session-transitioned",
        reason: "customer confirmed current script version",
        before: { stage: session.stage },
        after: { stage: next.stage },
        now: input.now,
      }),
      sessionMutationAudit({
        sessionId: session.id,
        actorId: input.actorId,
        action: "script-confirmed",
        reason: "customer confirmed current script version",
        before: { scriptVersion: current.scriptVersion, confirmedAt: null },
        after: { scriptVersion: current.scriptVersion },
        now: input.now,
      }),
    ],
  });
  return confirmed;
}

function validateDraftProvenance(draft: ScriptDraft, selectedIds: Set<string>): void {
  if (typeof draft !== "object" || draft === null || !Array.isArray(draft.sections)) {
    throw new Error("script-draft-invalid");
  }
  const validSources = new Set([
    "customer-input",
    "historical-material",
    "agent-suggestion",
    "shoot-suggestion",
  ]);
  for (const section of draft.sections) {
    if (typeof section !== "object" || section === null ||
      !Array.isArray(section.referencedSegmentIds) ||
      section.referencedSegmentIds.some((id) => typeof id !== "string")) {
      throw new Error("script-draft-invalid");
    }
    if (typeof section.source !== "string" || !validSources.has(section.source)) {
      throw new Error("script-section-source-invalid");
    }
    if (section.source === "historical-material") {
      if (section.referencedSegmentIds.length === 0) {
        throw new Error("script-historical-source-required");
      }
      if (section.referencedSegmentIds.some((id) => !selectedIds.has(id))) {
        throw new Error("script-material-not-selected");
      }
      continue;
    }
    if (section.referencedSegmentIds.length > 0) {
      throw new Error("script-material-not-selected");
    }
  }
}

async function requireHistoricalAuthorization(
  session: CreativeSession,
  library: ContentLibrary,
): Promise<{ decision: MaterialUseDecision; selectedIds: string[] }> {
  const allowedIds = new Set([
    ...session.materialViewDecisionIds,
    ...session.materialUseDecisionIds,
  ]);
  const decision = (await library.listMaterialUseDecisions())
    .filter((item) => item.sessionId === session.id && allowedIds.has(item.id) && (
      (item.action === "decline-view" && session.materialViewDecisionIds.includes(item.id)) ||
      (item.action === "confirm-selection" && session.materialUseDecisionIds.includes(item.id))
    ))
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
    .at(-1);
  if (!decision) throw new Error("script-material-decision-required");
  return {
    decision,
    selectedIds: decision.action === "confirm-selection" ? decision.selectedSegmentIds : [],
  };
}

async function requireSelectedSegments(
  selectedIds: string[],
  library: ContentLibrary,
): Promise<ContentSegment[]> {
  const segmentsById = new Map((await library.listSegments()).map((segment) => [segment.id, segment]));
  return selectedIds.map((id) => {
    const segment = segmentsById.get(id);
    if (!isEligible(segment)) throw new Error("material-no-longer-eligible");
    return segment;
  });
}

function isEligible(segment: ContentSegment | undefined): segment is ContentSegment {
  return segment !== undefined && segment.permissionState === "allowed" &&
    segment.tags.technicalFlags.length === 0 &&
    (segment.tags.products.length === 0 ||
      segment.tags.products.every((product) => product.status === "confirmed"));
}

function requireGenerationStage(session: CreativeSession, decision: MaterialUseDecision): void {
  const expectedStage = decision.action === "decline-view"
    ? "materials_declined"
    : "awaiting_script_confirmation";
  if (session.stage !== expectedStage) throw new Error("script-generation-not-allowed");
  if (session.stage !== "awaiting_script_confirmation" &&
    !isCreativeSessionTransitionAllowed(session.stage, "awaiting_script_confirmation")) {
    throw new Error("creative-session-transition-not-allowed");
  }
}

async function requireSession(sessionId: string, library: ContentLibrary): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId);
  if (!session) throw new Error(`creative-session-not-found:${sessionId}`);
  if (session.status !== "active") throw new Error("creative-session-not-active");
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

async function requireCurrentScript(
  session: CreativeSession,
  library: ContentLibrary,
): Promise<ScriptPackage> {
  const script = (await library.listScriptPackages()).find(
    (item) => item.sessionId === session.id &&
      item.briefVersion === session.currentBriefVersion &&
      item.scriptVersion === session.currentScriptVersion,
  );
  if (!script) throw new Error("script-not-found");
  if (script.status !== "active") throw new Error("script-not-active");
  return script;
}

function checkedScriptPackage(value: ScriptPackage): ScriptPackage {
  return ScriptPackageSchema.parse(value);
}

function sessionPrecondition(session: CreativeSession) {
  return {
    id: session.id,
    stage: session.stage,
    currentIntentVersion: session.currentIntentVersion,
    currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion,
    currentShotPlanVersion: session.currentShotPlanVersion,
  };
}

function scriptPrecondition(script: ScriptPackage) {
  return {
    id: script.id,
    sessionId: script.sessionId,
    briefVersion: script.briefVersion,
    scriptVersion: script.scriptVersion,
    status: script.status,
    confirmedAt: script.confirmedAt,
  };
}

function requireActor(actorId: string): void {
  if (actorId.trim().length === 0) throw new Error("script-actor-required");
}

function sessionMutationAudit(input: {
  sessionId: string;
  actorId: string;
  action: string;
  reason: string;
  before: unknown;
  after: unknown;
  now: string;
}): AuditEvent {
  return {
    id: `audit_${randomUUID()}`,
    entityType: "creative-session",
    entityId: input.sessionId,
    actorId: input.actorId,
    action: input.action,
    reason: input.reason,
    before: input.before,
    after: input.after,
    createdAt: input.now,
  };
}
