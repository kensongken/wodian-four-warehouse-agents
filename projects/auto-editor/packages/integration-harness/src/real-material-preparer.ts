import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  link,
  lstat,
  mkdir,
  readFile,
  realpath,
  unlink,
  writeFile,
} from "node:fs/promises";
import { extname, join } from "node:path";

import {
  ContentSegmentSchema,
  RealMaterialCapabilityReportSchema,
  RealMaterialStoryReadinessSchema,
  StudioMediaAssetSchema,
  type ContentSegment,
  type RealMaterialCapabilityReport,
  type RealMaterialConfirmedFact,
  type RealMaterialStoryReadiness,
  type StudioMediaAsset,
} from "@wearable/contracts";
import {
  CustomerMaterialLibrary,
  StudioAnalysisJobService,
  buildRealMaterialCapabilityReport,
  createStillMotionArtifact,
  evaluateRealMaterialStories,
  type StudioAnalysisHistoryEntry,
  type StudioScope,
} from "@wearable/mobile-agent";

import {
  assertLockedMaterialUnchanged,
  readLockedMaterialBytes,
  type LockedRealMaterialInput,
  type loadLockedRealMaterialManifest,
} from "./real-material-manifest";

export type PreparedRealMaterialRun = Readonly<{
  scope: StudioScope;
  assets: readonly StudioMediaAsset[];
  segments: readonly ContentSegment[];
  referenceInputId: string;
  capability: RealMaterialCapabilityReport;
  stories: readonly RealMaterialStoryReadiness[];
  privateBindings: readonly Readonly<{
    inputId: string;
    assetId: string;
    sourceDigest: string;
  }>[];
}>;

type PreparationState = Readonly<{
  schemaVersion: 1;
  manifestIdentity: string;
  savedAt: string;
  prepared: PreparedRealMaterialRun;
}>;

export async function prepareRealMaterialRun(input: Readonly<{
  locked: Awaited<ReturnType<typeof loadLockedRealMaterialManifest>>;
  runtimeRoot: string;
  library: CustomerMaterialLibrary;
  analysisJobs: StudioAnalysisJobService;
  now: () => string;
}>): Promise<PreparedRealMaterialRun> {
  await assertLockedMaterialUnchanged(input.locked.inputs);
  const manifest = input.locked.manifest;
  const referenceInputs = input.locked.inputs.filter(({ declared }) => declared.role === "reference");
  if (referenceInputs.length !== 1) throw new Error("real-material-reference-required");
  if (typeof input.now !== "function") throw new Error("real-material-now-required");

  await mkdir(input.runtimeRoot, { recursive: true, mode: 0o700 });
  const runtimeRoot = await realpath(input.runtimeRoot);
  const runtimeMetadata = await lstat(runtimeRoot);
  if (!runtimeMetadata.isDirectory() || runtimeMetadata.isSymbolicLink()) {
    throw new Error("real-material-runtime-root-directory-required");
  }
  const stateDirectory = join(runtimeRoot, "prepared-runs");
  await mkdir(stateDirectory, { recursive: true, mode: 0o700 });
  await chmod(stateDirectory, 0o700);
  const statePath = join(stateDirectory, `${sha256(manifest.runId)}.json`);
  const manifestIdentity = sha256(JSON.stringify(manifest));
  const replay = await readPreparationState(statePath);
  if (replay) {
    if (replay.manifestIdentity !== manifestIdentity) throw new Error("real-material-run-conflict");
    await assertLockedMaterialUnchanged(input.locked.inputs);
    return replay.prepared;
  }

  const scope: StudioScope = Object.freeze({
    actorId: manifest.actorId,
    customerId: manifest.customerId,
    sessionId: `real_session_${sha256(manifest.runId).slice(0, 24)}`,
  });
  const assets: StudioMediaAsset[] = [];
  const segments: ContentSegment[] = [];
  const privateBindings: Array<{ inputId: string; assetId: string; sourceDigest: string }> = [];
  const analysis: StudioAnalysisHistoryEntry[] = [];

  for (const lockedInput of input.locked.inputs) {
    const preparedSource = await prepareSourceBytes(lockedInput, manifest.runId, runtimeRoot);
    const usePermission = usePermissionFor(lockedInput);
    const asset = await input.library.importOriginal(scope.actorId, scope.customerId, {
      requestId: `real-import-${sha256(`${manifest.runId}\0${lockedInput.declared.inputId}`)}`,
      bytes: preparedSource.bytes,
      mediaKind: preparedSource.mediaKind,
      fileName: `material-${sha256(lockedInput.declared.inputId).slice(0, 16)}${preparedSource.extension}`,
      capturedAt: null,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission,
    });
    privateBindings.push({
      inputId: lockedInput.declared.inputId,
      assetId: asset.id,
      sourceDigest: lockedInput.lockedDigest,
    });
    const operationKey = `real-analysis-${sha256(`${manifest.runId}\0${lockedInput.declared.inputId}`).slice(0, 40)}`;
    const entry = lockedInput.declared.role === "reference"
      ? await input.analysisJobs.enqueueReference(
          scope,
          await input.library.inspectReferenceAnalysisAsset(
            scope.actorId, scope.customerId, scope.sessionId, asset.id,
          ),
          ["technical", "scene"],
          operationKey,
        )
      : await input.analysisJobs.enqueue(scope, asset.id, ["technical", "scene"], operationKey);
    analysis.push(entry);

    if (lockedInput.declared.role !== "reference") {
      assets.push(asset);
      segments.push(...segmentsForInput(lockedInput, asset, entry));
    }
  }

  const allTechnicalReady = analysis.length === input.locked.inputs.length && analysis.every((entry) =>
    entry.receipts.some((receipt) => acceptableTechnicalReceipt(receipt, "technical")),
  );
  const allSceneReady = analysis.length === input.locked.inputs.length && analysis.every((entry) =>
    entry.receipts.some((receipt) => acceptableTechnicalReceipt(receipt, "scene")),
  );
  const allReceipts = analysis.flatMap((entry) => entry.receipts);
  const capabilityReceipts = allSceneReady
    ? allReceipts.filter((receipt) => receipt.layer !== "technical")
    : allReceipts.filter((receipt) => receipt.layer !== "technical" && (
        receipt.layer !== "scene" || !acceptableTechnicalReceipt(receipt, "scene")
      ));
  const capability = buildRealMaterialCapabilityReport({
    mediaToolsReady: allTechnicalReady,
    receipts: capabilityReceipts,
    artifacts: [],
  });
  const stories = evaluateRealMaterialStories({
    requestedStoryTypes: manifest.requestedStoryTypes,
    segments,
    capability,
  });
  const prepared = parsePreparedRun({
    scope,
    assets,
    segments,
    referenceInputId: referenceInputs[0]!.declared.inputId,
    capability,
    stories,
    privateBindings,
  });

  await assertLockedMaterialUnchanged(input.locked.inputs);
  const state = parsePreparationState({
    schemaVersion: 1,
    manifestIdentity,
    savedAt: input.now(),
    prepared,
  });
  const winner = await writePreparationState(statePath, state);
  if (winner.manifestIdentity !== manifestIdentity) throw new Error("real-material-run-conflict");
  await assertLockedMaterialUnchanged(input.locked.inputs);
  return winner.prepared;
}

async function prepareSourceBytes(
  input: LockedRealMaterialInput,
  runId: string,
  runtimeRoot: string,
): Promise<Readonly<{ bytes: Buffer; mediaKind: "video" | "audio"; extension: string }>> {
  if (input.declared.mediaKind === "image") {
    if (!input.declared.allowedUses.includes("still-motion")) {
      throw new Error("real-material-still-motion-permission-required");
    }
    const artifact = await createStillMotionArtifact({
      sourcePath: input.canonicalPath,
      sourceDigest: input.lockedDigest,
      motion: motionForDigest(input.lockedDigest),
      outputRoot: join(runtimeRoot, "still-motion"),
      operationKey: `still-${sha256(`${runId}\0${input.declared.inputId}`).slice(0, 40)}`,
    });
    return { bytes: await readFile(artifact.outputPath), mediaKind: "video", extension: ".mp4" };
  }
  const bytes = await readLockedMaterialBytes(input);
  return {
    bytes,
    mediaKind: input.declared.mediaKind,
    extension: safeExtension(input.canonicalPath, input.declared.mediaKind),
  };
}

function usePermissionFor(input: LockedRealMaterialInput): "allowed" | "review-required" {
  if (input.declared.role === "reference") return "review-required";
  if (!input.declared.allowedUses.includes("render")) {
    throw new Error("real-material-render-permission-required");
  }
  return "allowed";
}

function segmentsForInput(
  input: LockedRealMaterialInput,
  asset: StudioMediaAsset,
  analysis: StudioAnalysisHistoryEntry,
): ContentSegment[] {
  const durationMs = analysis.job.sourceEndMs - analysis.job.sourceStartMs;
  if (!Number.isSafeInteger(durationMs) || durationMs <= 0) {
    throw new Error("real-material-source-duration-invalid");
  }
  const groups = groupFacts(input.declared.confirmedFacts, durationMs);
  const technical = analysis.receipts.find((receipt) => receipt.layer === "technical");
  const hasAudio = technical?.qualitySignals.hasAudio === true;
  return groups.map(({ startMs, endMs, facts }, index) => {
    if (startMs < 0 || endMs > durationMs || endMs <= startMs) {
      throw new Error("real-material-confirmed-fact-outside-source");
    }
    const productIds = unique(facts.filter(({ kind }) => kind === "product-id").map(({ value }) => value));
    const processStages = unique(facts.filter(({ kind }) => kind === "process-stage").map(({ value }) => value));
    const emotions = unique(facts.filter(({ kind }) => kind === "emotion").map(({ value }) => value));
    const spokenFacts = facts.filter(({ kind }) => kind === "first-person-choice" || kind === "opinion");
    const supportingFacts = facts.filter(({ kind }) => kind === "supporting-evidence");
    const baseTypes = new Set<ContentSegment["tags"]["baseTypes"][number]>([
      input.declared.mediaKind === "image" ? "product-image" : "scene",
    ]);
    if (spokenFacts.length > 0) baseTypes.add("persona-copy");
    return ContentSegmentSchema.parse({
      id: `segment_${sha256(`${asset.id}\0${startMs}\0${endMs}\0${index}`).slice(0, 40)}`,
      assetId: asset.id,
      batchId: `batch_${sha256(input.declared.inputId).slice(0, 24)}`,
      startMs,
      endMs,
      permissionState: "allowed",
      tags: {
        baseTypes: [...baseTypes],
        people: [],
        products: productIds.map((id) => ({ id, status: "confirmed" as const })),
        processStages,
        topics: supportingFacts.map(({ value }) => `confirmed-supporting-evidence:${value}`),
        emotions,
        transcript: spokenFacts.map(({ value }) => value).join(" "),
        technicalFlags: hasAudio ? ["ffprobe:has-audio"] : [],
        usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
      },
    });
  });
}

function groupFacts(
  facts: readonly RealMaterialConfirmedFact[],
  durationMs: number,
): Array<{ startMs: number; endMs: number; facts: RealMaterialConfirmedFact[] }> {
  if (facts.length === 0) return [{ startMs: 0, endMs: durationMs, facts: [] }];
  const groups = new Map<string, { startMs: number; endMs: number; facts: RealMaterialConfirmedFact[] }>();
  for (const fact of facts) {
    const startMs = fact.sourceStartMs ?? 0;
    const endMs = fact.sourceEndMs ?? durationMs;
    const key = `${startMs}\0${endMs}`;
    const group = groups.get(key) ?? { startMs, endMs, facts: [] };
    group.facts.push(fact);
    groups.set(key, group);
  }
  return [...groups.values()].sort((left, right) =>
    left.startMs - right.startMs || left.endMs - right.endMs,
  );
}

function acceptableTechnicalReceipt(
  receipt: StudioAnalysisHistoryEntry["receipts"][number],
  layer: "technical" | "scene",
): boolean {
  return receipt.layer === layer
    && receipt.status === "succeeded"
    && receipt.providerMode === "local"
    && receipt.synthetic === false;
}

function motionForDigest(digest: string): "still" | "push-in" | "pull-out" {
  return (["still", "push-in", "pull-out"] as const)[Number.parseInt(digest.slice(0, 2), 16) % 3]!;
}

function safeExtension(path: string, mediaKind: "video" | "audio"): string {
  const extension = extname(path).toLowerCase();
  if (/^\.[a-z0-9]{1,8}$/.test(extension)) return extension;
  return mediaKind === "video" ? ".mp4" : ".audio";
}

async function readPreparationState(path: string): Promise<PreparationState | null> {
  let raw: string;
  try {
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
      throw new Error("real-material-preparation-state-invalid");
    }
    raw = await readFile(path, "utf8");
  } catch (error) {
    if (isNodeError(error, "ENOENT")) return null;
    throw error;
  }
  try {
    return parsePreparationState(JSON.parse(raw));
  } catch (error) {
    if (error instanceof Error && error.message === "real-material-preparation-state-invalid") throw error;
    throw new Error("real-material-preparation-state-invalid", { cause: error });
  }
}

async function writePreparationState(path: string, state: PreparationState): Promise<PreparationState> {
  const temporary = `${path}.next-${randomUUID()}`;
  await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { flag: "wx", mode: 0o600 });
  await chmod(temporary, 0o600);
  try {
    await link(temporary, path);
    return state;
  } catch (error) {
    if (!isNodeError(error, "EEXIST")) throw error;
    const existing = await readPreparationState(path);
    if (!existing) throw new Error("real-material-preparation-state-invalid");
    return existing;
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
}

function parsePreparationState(value: unknown): PreparationState {
  if (!isRecord(value)
    || value.schemaVersion !== 1
    || typeof value.manifestIdentity !== "string"
    || !/^[a-f0-9]{64}$/.test(value.manifestIdentity)
    || typeof value.savedAt !== "string"
    || !Number.isFinite(Date.parse(value.savedAt))) {
    throw new Error("real-material-preparation-state-invalid");
  }
  return Object.freeze({
    schemaVersion: 1,
    manifestIdentity: value.manifestIdentity,
    savedAt: value.savedAt,
    prepared: parsePreparedRun(value.prepared),
  });
}

function parsePreparedRun(value: unknown): PreparedRealMaterialRun {
  if (!isRecord(value)
    || !isRecord(value.scope)
    || typeof value.scope.actorId !== "string" || !value.scope.actorId
    || typeof value.scope.customerId !== "string" || !value.scope.customerId
    || typeof value.scope.sessionId !== "string" || !value.scope.sessionId
    || !Array.isArray(value.assets)
    || !Array.isArray(value.segments)
    || typeof value.referenceInputId !== "string" || !value.referenceInputId
    || !Array.isArray(value.stories)
    || !Array.isArray(value.privateBindings)) {
    throw new Error("real-material-preparation-state-invalid");
  }
  const privateBindings = value.privateBindings.map((binding) => {
    if (!isRecord(binding)
      || typeof binding.inputId !== "string" || !binding.inputId
      || typeof binding.assetId !== "string" || !binding.assetId
      || typeof binding.sourceDigest !== "string" || !/^[a-f0-9]{64}$/.test(binding.sourceDigest)) {
      throw new Error("real-material-preparation-state-invalid");
    }
    return Object.freeze({
      inputId: binding.inputId,
      assetId: binding.assetId,
      sourceDigest: binding.sourceDigest,
    });
  });
  if (new Set(privateBindings.map(({ inputId }) => inputId)).size !== privateBindings.length) {
    throw new Error("real-material-preparation-state-invalid");
  }
  return Object.freeze({
    scope: Object.freeze({
      actorId: value.scope.actorId,
      customerId: value.scope.customerId,
      sessionId: value.scope.sessionId,
    }),
    assets: Object.freeze(value.assets.map((asset) => StudioMediaAssetSchema.parse(asset))),
    segments: Object.freeze(value.segments.map((segment) => ContentSegmentSchema.parse(segment))),
    referenceInputId: value.referenceInputId,
    capability: RealMaterialCapabilityReportSchema.parse(value.capability),
    stories: Object.freeze(value.stories.map((story) => RealMaterialStoryReadinessSchema.parse(story))),
    privateBindings: Object.freeze(privateBindings),
  });
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error && error.code === code;
}
