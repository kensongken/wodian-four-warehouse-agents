import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import {
  CreativeProcessingTaskSchema,
  type ContentSegment,
  type CreativeProcessingTask,
  type EditRevision,
  type StoryCandidate,
  type VariantArtifact,
} from "@wearable/contracts";
import type { ContentLibrary } from "./content-library";
import { resolveCreativeSteps } from "./creative-handoff";
import { planInitialRevision, type EditPlanAnalyzer } from "./edit-planner";
import { renderRevision, verifyRenderedRevision } from "./ffmpeg-renderer";
import { resolveHistoricalMaterialAuthorization } from "./historical-authorization";
import { segmentNewBatch, type SegmentationAnalyzer } from "./semantic-segmenter";
import { tagSegments, type SegmentTagAnalyzer } from "./segment-tagger";
import { prepareStoryCandidates, type StoryAnalyzer } from "./story-decomposer";
import {
  creativeEditorialInputDigest,
  frozenHistoricalCandidateDigest,
  frozenHistoricalEvidenceDigest,
} from "./creative-editorial-evidence";

export type CreativeEditorialDependencies = Readonly<{
  library: ContentLibrary;
  outputDir: string;
  segmentAnalyzer: SegmentationAnalyzer;
  tagAnalyzer: SegmentTagAnalyzer;
  storyAnalyzer: StoryAnalyzer;
  editAnalyzer: EditPlanAnalyzer;
  renderer: typeof renderRevision;
}>;

type EditorialInput = Readonly<{
  creativeSessionId: string;
  operationKey: string;
  now: string;
  manualRestartAuthorizationId?: string;
}>;
type EditorialResult = Readonly<{
  task: CreativeProcessingTask; stories: StoryCandidate[]; revisions: EditRevision[]; artifacts: VariantArtifact[];
}>;
const operations = new WeakMap<ContentLibrary, Map<string, Promise<EditorialResult>>>();

export async function processCreativeEditorial(input: EditorialInput, dependencies: CreativeEditorialDependencies): Promise<EditorialResult> {
  if (!input.operationKey.trim()) throw new Error("creative-editorial-operation-required");
  // Serializes local same-session work. JSON freshness checks remain authoritative across handles/processes.
  let pending = operations.get(dependencies.library);
  if (!pending) { pending = new Map(); operations.set(dependencies.library, pending); }
  const previous = pending.get(input.creativeSessionId);
  const operation = (previous?.catch(() => undefined) ?? Promise.resolve()).then(() => processEditorial(input, dependencies));
  pending.set(input.creativeSessionId, operation);
  try { return await operation; }
  finally { if (pending.get(input.creativeSessionId) === operation) pending.delete(input.creativeSessionId); }
}

async function processEditorial(input: EditorialInput, dependencies: CreativeEditorialDependencies): Promise<EditorialResult> {
  const { library } = dependencies;
  await library.assertFresh();
  const taskId = `editorial_task_${digest(`${input.creativeSessionId}:${input.operationKey}`).slice(0, 24)}`;
  if (input.manualRestartAuthorizationId) {
    const authorization = (await library.listCreativeEditorialRestartAuthorizations()).find((item) =>
      item.id === input.manualRestartAuthorizationId && item.creativeSessionId === input.creativeSessionId &&
      item.processOperationKey === input.operationKey && item.status === "consumed" &&
      item.restartedTaskId === taskId);
    const persisted = authorization
      ? (await library.listCreativeProcessingTasks()).find((task) => task.id === taskId)
      : undefined;
    if (authorization && persisted) {
      const replay = await library.beginCreativeEditorialRestart({
        authorizationId: authorization.id,
        creativeSessionId: input.creativeSessionId,
        processOperationKey: input.operationKey,
        replacementTask: persisted,
        consumedAt: input.now,
      });
      if (replay.processingTask.status === "reviewable") {
        return library.readCreativeEditorialBundle(replay.processingTask.id);
      }
      if (replay.processingTask.status === "failed-manual-review") {
        return emptyResult(replay.processingTask);
      }
    }
  }
  const session = (await library.listCreativeSessions()).find((item) => item.id === input.creativeSessionId);
  if (!session) throw new Error(`creative-session-not-found:${input.creativeSessionId}`);
  if (session.status !== "active" || session.privacyMode === "paused" || session.stage !== "editorial_review") {
    throw new Error("creative-editorial-session-not-ready");
  }
  const plan = (await library.listShotPlans()).find((item) => item.sessionId === session.id && item.status === "active" &&
    item.briefVersion === session.currentBriefVersion && item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion);
  if (!plan || !session.editorialBatchId) throw new Error("creative-editorial-session-not-ready");
  const allAssets = await library.listAssets();
  const steps = resolveCreativeSteps(plan, await library.listCreativeSessionEvents(), allAssets);
  if (steps.some((step) => step.state === "pending" || step.state === "retake-pending")) throw new Error("creative-editorial-steps-not-settled");
  const assets = allAssets.filter((asset) => {
    const context = asset.captureContext;
    return asset.batchId === session.editorialBatchId && context?.creativeSessionId === session.id &&
      context.scriptVersion === plan.scriptVersion && context.shotPlanVersion === plan.shotPlanVersion &&
      context.stepOutcome === "completed" && steps.some((step) => step.stepId === context.stepId &&
        step.state === "completed-returned" && step.acceptedTake === context.take);
  }).sort((a, b) => a.id.localeCompare(b.id));
  let tasks = await library.listCreativeProcessingTasks();
  let existing = tasks.find((task) => task.id === taskId);
  if (existing?.checkpoint && (!existing.checkpoint.historicalCandidates ||
    !existing.checkpoint.historicalCandidateDigest || !existing.checkpoint.historicalEvidenceDigest)) {
    existing = await library.migrateLegacyCreativeEditorialTask(existing.id, input.now);
    if (existing.status === "failed-manual-review" &&
      existing.errorCode === "creative-editorial-checkpoint-migration-required") return emptyResult(existing);
    tasks = await library.listCreativeProcessingTasks();
  }
  const currentTask = [...tasks].filter((task) => task.creativeSessionId === session.id && !task.supersededByTaskId)
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt) || right.id.localeCompare(left.id))[0];
  const restartAuthorizations = await library.listCreativeEditorialRestartAuthorizations();
  const restartForTask = restartAuthorizations.find((item) =>
    item.creativeSessionId === session.id && item.processOperationKey === input.operationKey &&
    item.restartedTaskId === taskId);
  const needsManualRestart = !existing && currentTask?.status === "failed-manual-review";
  if ((needsManualRestart || restartForTask) && !input.manualRestartAuthorizationId) {
    throw new Error("creative-editorial-manual-confirmation-required");
  }
  const authorization = await resolveHistoricalMaterialAuthorization(session.id, library);
  const initialSegments = await library.listSegments();
  const currentHistoricalCandidates = authorization.selectedSegmentIds.map((id) =>
    initialSegments.find((segment) => segment.id === id)).filter((value): value is ContentSegment => value !== undefined)
    .sort((left, right) => left.id.localeCompare(right.id));
  const historicalCandidates = existing?.checkpoint?.historicalCandidates ?? currentHistoricalCandidates;
  const historicalCandidateDigest = existing?.checkpoint?.historicalCandidateDigest ??
    frozenHistoricalCandidateDigest(historicalCandidates);
  const historicalEvidenceDigest = existing?.checkpoint?.historicalEvidenceDigest ??
    frozenHistoricalEvidenceDigest(historicalCandidates, allAssets);
  const inputDigest = creativeEditorialInputDigest({ batchId: session.editorialBatchId, authorization,
    historicalCandidateDigest, assets });
  if (existing && existing.inputDigest !== inputDigest) throw new Error("creative-editorial-operation-conflict");
  const currentSegments = initialSegments;
  const reusable = tasks.find((task) => task.creativeSessionId === session.id && task.inputDigest === inputDigest &&
    task.status === "reviewable" && !task.supersededByTaskId && task.checkpoint &&
    task.checkpoint.segmentDigest === segmentDigest(task.checkpoint.segmentIds, currentSegments));
  let task = existing ?? CreativeProcessingTaskSchema.parse({
    id: taskId, creativeSessionId: session.id, editorialBatchId: session.editorialBatchId,
    status: assets.length ? "processing" : "no-assets", phase: assets.length ? "segment" : "complete",
    operationKey: input.operationKey, inputDigest, publishedClaimed: false, errorCode: null, updatedAt: input.now,
    checkpoint: reusable?.checkpoint ?? { authorization, assetIds: assets.map((asset) => asset.id), segmentIds: [],
      historicalCandidates, historicalCandidateDigest, historicalEvidenceDigest,
      stories: [], revisions: [], artifacts: [] },
  });
  if (input.manualRestartAuthorizationId) {
    const begun = await library.beginCreativeEditorialRestart({
      authorizationId: input.manualRestartAuthorizationId,
      creativeSessionId: session.id,
      processOperationKey: input.operationKey,
      replacementTask: task,
      consumedAt: input.now,
    });
    task = begun.processingTask;
    existing ??= task;
  }
  const checkpoint = task.checkpoint!;
  const progress = async (phase: CreativeProcessingTask["phase"]) => {
    task = { ...task, status: "processing", phase, errorCode: null, updatedAt: input.now, checkpoint };
    await library.putCreativeProcessingTask(task);
  };
  try {
    if (task.supersededByTaskId) throw new Error("creative-editorial-task-superseded");
    if (task.status === "failed-manual-review") return emptyResult(task);
    checkpoint.historicalCandidates ??= historicalCandidates;
    checkpoint.historicalCandidateDigest ??= frozenHistoricalCandidateDigest(checkpoint.historicalCandidates);
    checkpoint.historicalEvidenceDigest ??= frozenHistoricalEvidenceDigest(checkpoint.historicalCandidates, allAssets);
    const frozenHistoricalIds = checkpoint.historicalCandidates.map((segment) => segment.id).sort();
    if (JSON.stringify(frozenHistoricalIds) !== JSON.stringify([...authorization.selectedSegmentIds].sort())) {
      throw new Error("creative-editorial-historical-candidates-missing");
    }
    if (checkpoint.historicalCandidateDigest !== frozenHistoricalCandidateDigest(checkpoint.historicalCandidates)) {
      throw new Error("creative-editorial-historical-checkpoint-invalid");
    }
    if (checkpoint.segmentDigest && checkpoint.segmentDigest !== segmentDigest(checkpoint.segmentIds, currentSegments)) {
      throw new Error("creative-editorial-evidence-changed");
    }
    if (task.status === "no-assets") {
      if (!existing) await library.putCreativeProcessingTask(task);
      return emptyResult(task);
    }
    if (task.status === "reviewable") {
      await persistedResult(task, library);
      return await library.readCreativeEditorialBundle(task.id);
    }
    if (checkpoint.segmentIds.length === 0) {
      await progress("segment");
      const existingUntyped = await library.listUntypedSegments();
      const missing = assets.filter((asset) => !existingUntyped.some((segment) => segment.assetId === asset.id));
      await segmentNewBatch(session.editorialBatchId, missing, dependencies.segmentAnalyzer, library);
      const untyped = (await library.listUntypedSegments()).filter((segment) => assets.some((asset) => asset.id === segment.assetId));
      if (assets.some((asset) => !untyped.some((segment) => segment.assetId === asset.id))) {
        throw new Error("creative-segmentation-analyzer-retryable");
      }
      await progress("tag");
      const tagged = await library.listSegments();
      await tagSegments(untyped.filter((segment) => !tagged.some((item) => item.id === segment.id)), dependencies.tagAnalyzer, library);
      checkpoint.segmentIds = untyped.map((segment) => segment.id);
      await progress("story");
    }
    const allSegments = await library.listSegments();
    const current = allSegments.filter((segment) => checkpoint.segmentIds.includes(segment.id));
    if (current.length !== checkpoint.segmentIds.length) throw new Error("creative-editorial-segments-missing");
    checkpoint.segmentDigest = segmentDigest(checkpoint.segmentIds, allSegments);
    if (checkpoint.stories.length === 0) {
      await progress("story");
      const stories = await prepareStoryCandidates({ batchId: session.editorialBatchId, authorization,
        newSegments: current, historicalSegments: checkpoint.historicalCandidates,
        historicalCandidateDigest: checkpoint.historicalCandidateDigest,
      }, dependencies.storyAnalyzer, library);
      const byType = new Map<StoryCandidate["type"], StoryCandidate>();
      for (const story of stories) {
        const previous = byType.get(story.type);
        byType.set(story.type, { ...story, id: `story_creative_${digest(session.id).slice(0, 16)}_${story.type.replaceAll("-", "_")}`,
          anchorSegmentIds: [...new Set([...(previous?.anchorSegmentIds ?? []), ...story.anchorSegmentIds])],
          historicalSegmentIds: [...new Set([...(previous?.historicalSegmentIds ?? []), ...story.historicalSegmentIds])],
          historicalReasons: { ...previous?.historicalReasons, ...story.historicalReasons } });
      }
      checkpoint.stories = [...byType.values()].sort((a, b) => a.type.localeCompare(b.type));
      if (checkpoint.stories.length === 0) throw new Error("creative-editorial-no-supported-story");
      await progress("revision");
    }
    for (const story of checkpoint.stories) {
      if (checkpoint.revisions.some((revision) => revision.storyId === story.id)) continue;
      await progress("revision");
      const lineage = allSegments.filter((segment) => [...story.anchorSegmentIds, ...story.historicalSegmentIds].includes(segment.id));
      const initial = await planInitialRevision(story, lineage, dependencies.editAnalyzer);
      const prior = (await library.listRevisions(story.id)).sort((a, b) => b.revision - a.revision)[0];
      const head = (await library.listCreativeRevisionHeads()).find((item) => item.storyId === story.id);
      const version = Math.max(prior?.revision ?? 0, head?.revision ?? 0) + 1;
      checkpoint.revisions.push({ ...initial, id: `revision_${story.id}_${version}`, revision: version,
        parentRevisionId: head?.revisionId ?? prior?.id ?? null, createdAt: input.now,
        changeExplanation: prior ? "Rebuilt from cumulative captures and the current exact historical decision" : null });
      await progress("revision");
    }
    for (const revision of checkpoint.revisions) {
      await progress("render");
      let artifacts = checkpoint.artifacts.filter((artifact) => artifact.revisionId === revision.id);
      if (artifacts.length === 0) {
        artifacts = (await dependencies.renderer(revision, allSegments, allAssets, dependencies.outputDir)).artifacts;
        await verifyRenderedRevision(revision, artifacts, allSegments, allAssets, await realpath(dependencies.outputDir));
        checkpoint.artifacts.push(...artifacts);
        await progress("render");
      } else await verifyRenderedRevision(revision, artifacts, allSegments, allAssets, await realpath(dependencies.outputDir));
    }
    await progress("render");
    const completed: CreativeProcessingTask = { ...task, status: "reviewable", phase: "complete", errorCode: null };
    await library.commitCreativeEditorialBundle({ processingTask: completed, stories: checkpoint.stories,
      renderOutputRoot: await realpath(dependencies.outputDir),
      revisions: checkpoint.revisions, artifacts: checkpoint.artifacts, auditEvents: [{
        id: `audit_${task.id}_reviewable`, entityType: "creative-session", entityId: session.id, actorId: session.customerId,
        action: "creative-editorial-reviewable", reason: "A-0 local software artifacts verified; human review required",
        before: null, after: { taskId: task.id, inputDigest, storyIds: checkpoint.stories.map((story) => story.id), publishedClaimed: false },
        createdAt: task.updatedAt,
      }] });
    return { task: completed, stories: checkpoint.stories, revisions: checkpoint.revisions, artifacts: checkpoint.artifacts };
  } catch (error) {
    const failure = classifyCreativeEditorialFailure(error);
    task = { ...task, status: failure.status, errorCode: failure.code, updatedAt: input.now };
    await library.putCreativeProcessingTask(task);
    return emptyResult(task);
  }
}

async function persistedResult(task: CreativeProcessingTask, library: ContentLibrary): Promise<EditorialResult> {
  const checkpoint = task.checkpoint!;
  const stories = (await library.listStories()).filter((story) => checkpoint.stories.some((item) => item.id === story.id));
  const revisions = (await Promise.all(stories.map((story) => library.listRevisions(story.id)))).flat()
    .filter((revision) => checkpoint.revisions.some((item) => item.id === revision.id));
  const artifacts = (await library.listAllArtifacts()).filter((artifact) => revisions.some((revision) => revision.id === artifact.revisionId));
  if (!stories.length || revisions.length !== stories.length || artifacts.length !== revisions.length * 4) {
    throw new Error("creative-editorial-persisted-bundle-incomplete");
  }
  return { task, stories, revisions, artifacts };
}

function emptyResult(task: CreativeProcessingTask): EditorialResult { return { task, stories: [], revisions: [], artifacts: [] }; }
function digest(value: string): string { return createHash("sha256").update(value).digest("hex"); }

type EditorialFailure = Readonly<{
  status: "failed-retryable" | "failed-manual-review";
  code: string;
}>;

const RETRYABLE_EDITORIAL_CODES = new Set([
  "transient-render-failure",
  "temporarily-unavailable",
  "storage-full",
  "stale-content-store",
  "content-store-lock-timeout",
  "creative-segmentation-analyzer-retryable",
]);
const RETRYABLE_SYSTEM_CODES = new Set(["EAGAIN", "EBUSY", "ECONNRESET", "EIO", "EMFILE", "ENFILE", "ENOSPC", "ETIMEDOUT"]);

function classifyCreativeEditorialFailure(error: unknown): EditorialFailure {
  const message = error instanceof Error ? error.message : "creative-editorial-unknown-failure";
  const code = /^[a-z0-9-]+/.exec(message)?.[0] ?? "creative-editorial-unknown-failure";
  const systemCode = error && typeof error === "object" && "code" in error
    ? String((error as { code?: unknown }).code ?? "") : "";
  return RETRYABLE_EDITORIAL_CODES.has(code) || RETRYABLE_SYSTEM_CODES.has(systemCode)
    ? { status: "failed-retryable", code }
    : { status: "failed-manual-review", code };
}
function segmentDigest(ids: string[], segments: ContentSegment[]): string {
  return digest(JSON.stringify(ids.map((id) => editorialEvidenceSegment(
    segments.find((segment) => segment.id === id),
  ))));
}

function editorialEvidenceSegment(segment: ContentSegment | undefined) {
  if (!segment) return undefined;
  const { usage: _mutableUsage, ...evidenceTags } = segment.tags;
  return { ...segment, tags: evidenceTags };
}
