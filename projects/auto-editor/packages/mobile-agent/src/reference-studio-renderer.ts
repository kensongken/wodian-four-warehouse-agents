import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { createRequire } from "node:module";
import {
  lstat,
  mkdtemp,
  open,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  ReferenceDraftPackageSchema,
  ReferencePromptRevisionSchema,
  REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD,
  ReferenceMaterialMappingPlanSchema,
  ReferenceRightsDeclarationSchema,
  ReferenceStyleAnalysisSchema,
  ReferenceVariantPlanSchema,
  StudioMediaAssetSchema,
  type ReferenceDraftPackage,
  type ReferenceMaterialMappingPlan,
  type ReferencePromptRevision,
  type ReferenceRightsDeclaration,
  type ReferenceStyleAnalysis,
  type ReferenceTransitionKind,
  type ReferenceVariantKind,
  type ReferenceVariantPlan,
  type StudioMediaAsset,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";

import { OFFLINE_STUDIO_CAPABILITY } from "./studio-capabilities";

const bundledFfprobePath = (
  createRequire(import.meta.url)("ffprobe-static") as { path?: unknown }
).path;
const TRANSITION_DURATION_MS = 350;
const FRAME_DURATION_MS = 40;
const MAX_SAFE_TIMELINE_MS = 60_000 - FRAME_DURATION_MS;
const VARIANT_KINDS = ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const;
const SAFE_OPERATION_KEY = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;

type MappingRow = ReferenceMaterialMappingPlan["mappings"][number];
type MappingPermission = NonNullable<MappingRow["permission"]>;
type ExecutableTransitionKind = Exclude<ReferenceTransitionKind, "unsupported">;

export type ReferenceRenderAsset = {
  asset: StudioMediaAsset;
  permission: MappingPermission;
  withdrawn: boolean;
  verifiedDigest: string;
  bytes: Uint8Array;
};

export type RenderReferenceVariantsInput = {
  mappingPlan: ReferenceMaterialMappingPlan;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  assets: ReferenceRenderAsset[];
  outputRoot: string;
  operationKey: string;
  audioOverlaySourceAssetId?: string | null;
  now: string;
  ffmpegPath?: string | null;
  ffprobePath?: string | null;
  processLimits?: Partial<ProcessLimits>;
  revisionOverride?: ReferencePromptRevision;
};

type ProcessLimits = Readonly<{ timeoutMs: number; maxOutputBytes: number; killGraceMs: number }>;
const DEFAULT_PROCESS_LIMITS: ProcessLimits = { timeoutMs: 120_000, maxOutputBytes: 1_048_576, killGraceMs: 250 };

export type ReferenceRenderEvidence = Readonly<{
  bindings: ReferenceVariantPlan["bindings"];
  artifacts: readonly Readonly<{
    kind: ReferenceVariantKind;
    variantPlanId: string;
    variantPlanVersion: number;
    draftId: string;
    draftVersion: number;
    outputPath: string;
    outputDigest: string;
    durationSeconds: number;
    videoCodec: "h264";
    audioCodec: "aac";
    width: 720;
    height: 1280;
  }>[];
  sourceLineage: readonly Readonly<{
    kind: ReferenceVariantKind;
    timelineEntryId: string;
    mappingId: string;
    sourceBindingId: string;
    sourceAssetId: string;
    sourceDigest: string;
    sourceStartMs: number;
    sourceEndMs: number;
    permissionId: string;
    permissionVersion: number;
    permissionEpoch: number;
  }>[];
  executedTransitions: readonly Readonly<{
    kind: "fade" | "crossfade";
    variantKind: ReferenceVariantKind;
    timelineEntryId: string;
    offsetMs: number;
    durationMs: 350;
  }>[];
  mappingCounts: Readonly<{ matched: number; substituted: number; unachievable: number }>;
  platformChanges: readonly Readonly<{
    kind: "xiaohongshu" | "douyin" | "wechat-channels";
    changeCodes: readonly string[];
  }>[];
  publishClaimed: false;
}>;

export type RenderReferenceVariantsResult = Readonly<{
  plans: ReferenceVariantPlan[];
  drafts: ReferenceDraftPackage[];
  reportInput: ReferenceRenderEvidence;
}>;

type ValidatedInput = Readonly<{
  mappingPlan: ReferenceMaterialMappingPlan;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  assetsById: ReadonlyMap<string, ReferenceRenderAsset>;
  outputRoot: string;
  outputRootIdentity: OutputRootIdentity;
  operationKey: string;
  audioOverlaySourceAssetId: string | null;
  now: string;
  ffmpeg: string;
  ffprobe: string;
  processLimits: ProcessLimits;
  revisionOverride: ReferencePromptRevision | null;
}>;

type OutputRootIdentity = Readonly<{ path: string; realPath: string; dev: number; ino: number }>;

type PlannedEntry = Readonly<{
  mapping: MappingRow;
  sourceStartMs: number;
  sourceEndMs: number;
  transition: ExecutableTransitionKind;
}>;

type RenderedArtifact = Readonly<{
  kind: ReferenceVariantKind;
  stagePath: string;
  stageHandle: FileHandle;
  stageDev: number;
  stageIno: number;
  stageSize: number;
  finalPath: string;
  outputDigest: string;
  metadata: ProbeMetadata;
}>;

type FinalOutput = Readonly<{
  path: string;
  name: string;
  dev: number;
  ino: number;
}>;

type RunLayout = Readonly<{
  directory: string;
  name: string;
  ownerToken: string;
  dev: number;
  ino: number;
  outputs: readonly FinalOutput[];
}>;

type RunClaim = Readonly<{
  directory: string;
  name: string;
  ownerToken: string;
  outputNames: readonly string[];
}>;

class PreexistingRunDirectoryError extends Error {
  constructor(cause: unknown) {
    super("reference-render-output-exists", { cause });
    this.name = "PreexistingRunDirectoryError";
  }
}

const OUTPUT_ANCHOR_SCRIPT = String.raw`
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const input = JSON.parse(Buffer.from(process.argv.at(-1), "base64url").toString("utf8"));
const matches = (value, expected, kind) => {
  const stat = fs.lstatSync(value);
  return !stat.isSymbolicLink() && stat.dev === expected.dev && stat.ino === expected.ino &&
    (kind === "directory" ? stat.isDirectory() : stat.isFile());
};
const assertRoot = () => {
  if (!matches(".", input.root, "directory")) throw new Error("reference-render-output-root-changed");
};
const digestFd = (fd, size) => {
  const hash = crypto.createHash("sha256");
  const buffer = Buffer.allocUnsafe(262144);
  let offset = 0;
  while (offset < size) {
    const count = fs.readSync(fd, buffer, 0, Math.min(buffer.length, size - offset), offset);
    if (count <= 0) throw new Error("reference-render-stage-read-short");
    hash.update(buffer.subarray(0, count));
    offset += count;
  }
  return hash.digest("hex");
};
try {
  assertRoot();
  if (input.action === "create") {
    let made = false;
    try {
      fs.mkdirSync(input.run.name, { mode: 0o700 });
      made = true;
      const directory = fs.lstatSync(input.run.name);
      fs.writeFileSync(path.join(input.run.name, ".reference-owner"), input.run.ownerToken,
        { encoding: "utf8", flag: "wx", mode: 0o600 });
      const outputs = input.outputs.map((name) => {
        const relativePath = path.join(input.run.name, name);
        const fd = fs.openSync(relativePath,
          fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
          0o600);
        try {
          const stat = fs.fstatSync(fd);
          return { name, dev: stat.dev, ino: stat.ino };
        } finally {
          fs.closeSync(fd);
        }
      });
      process.stdout.write(JSON.stringify({ dev: directory.dev, ino: directory.ino, outputs }));
    } catch (error) {
      const directoryPreexisted = !made && error && error.code === "EEXIST";
      if (made) fs.rmSync(input.run.name, { recursive: true, force: true });
      if (directoryPreexisted) throw new Error("reference-render-output-exists");
      throw error;
    }
  } else if (input.action === "promote") {
    if (!matches(input.run.name, input.run, "directory")) {
      throw new Error("reference-render-output-root-changed");
    }
    const opened = [];
    try {
      for (const [index, output] of input.outputs.entries()) {
        if (!matches(output.stagePath, output.stage, "file")) {
          throw new Error("reference-render-stage-identity-changed");
        }
        const stageFd = 3 + index;
        const stageStat = fs.fstatSync(stageFd);
        if (!stageStat.isFile() || stageStat.dev !== output.stage.dev ||
            stageStat.ino !== output.stage.ino || stageStat.size !== output.stage.size) {
          throw new Error("reference-render-stage-identity-changed");
        }
        const fd = fs.openSync(path.join(input.run.name, output.name),
          fs.constants.O_RDWR | fs.constants.O_NOFOLLOW);
        const stat = fs.fstatSync(fd);
        if (!stat.isFile() || stat.dev !== output.dev || stat.ino !== output.ino) {
          fs.closeSync(fd);
          throw new Error("reference-render-output-root-changed");
        }
        opened.push({ fd, stageFd, stage: output.stage });
      }
      for (const output of opened) {
        fs.ftruncateSync(output.fd, 0);
        const copiedHash = crypto.createHash("sha256");
        const buffer = Buffer.allocUnsafe(262144);
        let offset = 0;
        while (offset < output.stage.size) {
          const count = fs.readSync(output.stageFd, buffer, 0,
            Math.min(buffer.length, output.stage.size - offset), offset);
          if (count <= 0) throw new Error("reference-render-stage-read-short");
          copiedHash.update(buffer.subarray(0, count));
          let written = 0;
          while (written < count) {
            written += fs.writeSync(output.fd, buffer, written, count - written, offset + written);
          }
          offset += count;
        }
        if (copiedHash.digest("hex") !== output.stage.digest) {
          throw new Error("reference-render-stage-digest-changed");
        }
        fs.fsyncSync(output.fd);
        const finalStat = fs.fstatSync(output.fd);
        if (finalStat.size !== output.stage.size || digestFd(output.fd, finalStat.size) !== output.stage.digest) {
          throw new Error("reference-render-final-digest-mismatch");
        }
      }
      process.stdout.write(JSON.stringify({ digests: opened.map((output) => output.stage.digest) }));
    } finally {
      for (const output of opened) {
        try { fs.closeSync(output.fd); } catch {}
      }
    }
  } else if (input.action === "cleanup") {
    if (!fs.existsSync(input.run.name)) {
      process.stdout.write(JSON.stringify({ removed: false }));
    } else {
      const runStat = fs.lstatSync(input.run.name);
      if (runStat.isSymbolicLink() || !runStat.isDirectory() ||
          input.run.dev !== undefined && (runStat.dev !== input.run.dev || runStat.ino !== input.run.ino)) {
        throw new Error("reference-render-output-cleanup-identity-mismatch");
      }
      const ownerPath = path.join(input.run.name, ".reference-owner");
      const ownerFd = fs.openSync(ownerPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
      let owner;
      try { owner = fs.readFileSync(ownerFd, "utf8"); } finally { fs.closeSync(ownerFd); }
      if (owner !== input.run.ownerToken) {
        throw new Error("reference-render-output-cleanup-owner-mismatch");
      }
      fs.rmSync(input.run.name, { recursive: true, force: true });
      process.stdout.write(JSON.stringify({ removed: true }));
    }
  } else {
    throw new Error("reference-render-output-anchor-action-invalid");
  }
} catch (error) {
  process.stderr.write(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
`;

/**
 * Renders only exact, currently-authorized bytes already bound into a human-confirmed mapping plan.
 * Persistence/freshness immediately before commit remains the Task 6 orchestrator's responsibility.
 */
export async function renderReferenceVariants(
  inputValue: RenderReferenceVariantsInput,
): Promise<RenderReferenceVariantsResult> {
  const input = await validateInput(structuredClone(inputValue));
  const plans = buildVariantPlans(input);
  const runClaim = buildRunClaim(input, plans);
  let runLayout: RunLayout | null = null;
  const artifacts: RenderedArtifact[] = [];
  const stageDirectory = await mkdtemp(join(dirname(input.outputRoot), ".reference-render-tmp-"));
  try {
    runLayout = await createRunLayout(input, runClaim);
    for (const [index, plan] of plans.entries()) {
      const stagePath = join(stageDirectory, `${String(index).padStart(2, "0")}-${plan.kind}.mp4`);
      const presentationDurationMs = await renderVariant(input, plan, stageDirectory, stagePath);
      const staged = await pinStagedArtifact(stagePath);
      let metadata: ProbeMetadata;
      try {
        metadata = await verifyPinnedOutput(input.ffprobe, staged.stageHandle, input.processLimits);
        if (Math.abs(metadata.durationSeconds - presentationDurationMs / 1_000) > 0.12) {
          throw new Error("reference-render-duration-mismatch");
        }
      } catch (error) {
        await staged.stageHandle.close().catch(() => undefined);
        throw error;
      }
      const artifact = {
        kind: plan.kind,
        stagePath,
        ...staged,
        finalPath: runLayout.outputs[index]!.path,
        metadata,
      };
      artifacts.push(artifact);
      await assertStagedArtifactCurrent(artifact);
    }
    if (new Set(artifacts.map((artifact) => artifact.outputDigest)).size !== plans.length) {
      throw new Error("reference-render-duplicate-output-digest");
    }

    await promoteRunLayout(input, runLayout, artifacts);
    await assertRunLayoutCurrent(input.outputRootIdentity, runLayout);
    await verifyFinalArtifacts(runLayout, artifacts);
    const drafts = artifacts.map((artifact, index) =>
      buildDraft(plans[index]!, artifact));
    return {
      plans,
      drafts,
      reportInput: buildReportInput(input.mappingPlan, plans, drafts, artifacts),
    };
  } catch (error) {
    if (!(error instanceof PreexistingRunDirectoryError)) {
      const cleanupError = await cleanupRunClaim(input, runLayout ?? runClaim).catch((failure: unknown) => failure);
      if (cleanupError) throw combinedCleanupError(error, cleanupError);
    }
    throw error;
  } finally {
    await Promise.all(artifacts.map(async (artifact) => artifact.stageHandle.close().catch(() => undefined)));
    await rm(stageDirectory, { recursive: true, force: true });
  }
}

async function validateInput(inputValue: RenderReferenceVariantsInput): Promise<ValidatedInput> {
  if (!inputValue || typeof inputValue !== "object") throw new Error("reference-render-input-invalid");
  rejectExternalReferenceUse(inputValue);
  if (!SAFE_OPERATION_KEY.test(inputValue.operationKey ?? "")) {
    throw new Error("reference-render-operation-key-invalid");
  }
  if (!Number.isFinite(Date.parse(inputValue.now))) throw new Error("reference-render-created-at-invalid");
  const revisionOverride = inputValue.revisionOverride === undefined
    ? null
    : ReferencePromptRevisionSchema.safeParse(inputValue.revisionOverride);
  if (revisionOverride !== null && !revisionOverride.success) {
    throw new Error("reference-revision-prompt-unsupported");
  }

  let mappingPlan: ReferenceMaterialMappingPlan;
  try {
    mappingPlan = ReferenceMaterialMappingPlanSchema.parse(inputValue.mappingPlan);
  } catch (error) {
    if (inputValue.mappingPlan?.status !== "confirmed") {
      throw new Error("confirmed-reference-mapping-plan-required");
    }
    throw error;
  }
  if (mappingPlan.status !== "confirmed" || !mappingPlan.confirmedByActorId ||
      !mappingPlan.confirmationReceiptId || !mappingPlan.confirmedAt) {
    throw new Error("confirmed-reference-mapping-plan-required");
  }
  assertMappingIntegrity(mappingPlan);

  const rights = ReferenceRightsDeclarationSchema.parse(inputValue.rights);
  const analysis = ReferenceStyleAnalysisSchema.parse(inputValue.analysis);
  if (canonicalJson(rights.sample) !== canonicalJson(mappingPlan.bindings.sample) ||
      canonicalJson(rightsBinding(rights)) !== canonicalJson(mappingPlan.bindings.rights)) {
    throw new Error("reference-render-rights-binding-mismatch");
  }
  if (canonicalJson(scopedBinding(analysis)) !== canonicalJson(mappingPlan.bindings.analysis) ||
      analysis.customerId !== mappingPlan.customerId || analysis.sessionId !== mappingPlan.sessionId ||
      canonicalJson(analysis.bindings.sample) !== canonicalJson(mappingPlan.bindings.sample) ||
      canonicalJson(analysis.bindings.rights) !== canonicalJson(mappingPlan.bindings.rights)) {
    throw new Error("reference-render-analysis-binding-mismatch");
  }

  const outputRootIdentity = await validateOutputRoot(inputValue.outputRoot);
  const outputRoot = outputRootIdentity.realPath;
  const ffmpeg = inputValue.ffmpegPath === undefined ? ffmpegPath : inputValue.ffmpegPath;
  const ffprobe = inputValue.ffprobePath === undefined
    ? (typeof bundledFfprobePath === "string" ? bundledFfprobePath : null)
    : inputValue.ffprobePath;
  await assertExecutable(ffmpeg, "ffmpeg-binary-unavailable");
  await assertExecutable(ffprobe, "ffprobe-binary-unavailable");

  if (!Array.isArray(inputValue.assets)) throw new Error("reference-render-assets-required");
  const assetsById = new Map<string, ReferenceRenderAsset>();
  const conflictedIds = new Set<string>();
  for (const value of inputValue.assets) {
    const parsed = StudioMediaAssetSchema.parse(value?.asset);
    const normalized = { ...value, asset: parsed, bytes: Buffer.from(value.bytes) };
    const existing = assetsById.get(parsed.id);
    if (existing && canonicalJson(assetIdentity(existing)) !== canonicalJson(assetIdentity(normalized))) {
      assetsById.delete(parsed.id);
      conflictedIds.add(parsed.id);
      continue;
    }
    if (!conflictedIds.has(parsed.id)) assetsById.set(parsed.id, normalized);
  }
  if (conflictedIds.size > 0) throw new Error("reference-render-source-conflict");

  for (const mapping of executableMappings(mappingPlan)) {
    const source = assetsById.get(mapping.sourceAssetId!);
    validateSourceSnapshot(mappingPlan, mapping, source);
  }
  const audioOverlaySourceAssetId = inputValue.audioOverlaySourceAssetId ?? null;
  if (audioOverlaySourceAssetId !== null) {
    const audioRuleIds = new Set(analysis.rules.filter((rule) => rule.kind === "audio").map((rule) => rule.id));
    const audioMappings = executableMappings(mappingPlan)
      .filter((mapping) => mapping.sourceAssetId === audioOverlaySourceAssetId && audioRuleIds.has(mapping.ruleId));
    if (audioMappings.length !== 1) throw new Error("reference-audio-rule-binding-required");
    const audioMapping = audioMappings[0]!;
    const source = assetsById.get(audioOverlaySourceAssetId)!;
    if (source.asset.mediaKind === "image") throw new Error("reference-audio-source-stream-required");
    if (audioMapping.sourceProvenance === "self-owned-reference") {
      const hasBoundAudioRight = rights.sourceKind === "self-owned" &&
        rights.id === mappingPlan.bindings.rights.id &&
        rights.version === mappingPlan.bindings.rights.version &&
        rights.reusableElements.includes("audio-track");
      if (!hasBoundAudioRight) throw new Error("reference-audio-reuse-rights-required");
      if (audioMapping.reuseRights?.id !== rights.id || audioMapping.reuseRights.version !== rights.version ||
          audioMapping.reuseRights.element !== "audio-track") {
        throw new Error("reference-audio-reuse-rights-required");
      }
    }
  }

  return {
    mappingPlan,
    rights,
    analysis,
    assetsById,
    outputRoot,
    outputRootIdentity,
    operationKey: inputValue.operationKey,
    audioOverlaySourceAssetId,
    now: inputValue.now,
    ffmpeg: ffmpeg!,
    ffprobe: ffprobe!,
    processLimits: validateProcessLimits(inputValue.processLimits),
    revisionOverride: revisionOverride === null ? null : revisionOverride.data,
  };
}

function rejectExternalReferenceUse(input: RenderReferenceVariantsInput): void {
  const sample = input?.mappingPlan?.bindings?.sample;
  if (sample?.sourceKind !== "external-style-only") return;
  if (input.audioOverlaySourceAssetId === sample.sourceAssetId ||
      input.mappingPlan.mappings?.some((mapping) =>
        mapping.sourceAssetId === sample.sourceAssetId ||
        mapping.sourceDigest === sample.sourceDigest)) {
    throw new Error("external-reference-content-forbidden");
  }
}

function assertMappingIntegrity(plan: ReferenceMaterialMappingPlan): void {
  const permissionSetDigest = digestPermissionSet(plan.mappings);
  if (permissionSetDigest !== plan.permissionSetDigest) {
    throw new Error("reference-mapping-permission-stale");
  }
  const contentDigest = sha256(canonicalJson({
    bindings: plan.bindings,
    mappings: plan.mappings,
    permissionSetDigest,
  }));
  if (contentDigest !== plan.contentDigest) throw new Error("reference-mapping-input-stale");
  const expectedId = `reference_mapping_plan_${sha256(
    `${plan.bindings.styleSelection.id}\0${plan.bindings.styleSelection.version}\0${contentDigest}`,
  )}`;
  if (plan.id !== expectedId) throw new Error("reference-mapping-input-stale");
}

function validateSourceSnapshot(
  plan: ReferenceMaterialMappingPlan,
  mapping: MappingRow,
  source: ReferenceRenderAsset | undefined,
): asserts source is ReferenceRenderAsset {
  if (!source) throw new Error(`reference-render-source-missing:${mapping.sourceAssetId}`);
  const { asset, permission } = source;
  if (source.withdrawn || asset.viewPermission !== "allowed" || asset.usePermission !== "allowed" ||
      permission.view !== "allowed" || permission.use !== "allowed") {
    throw new Error("reference-render-source-not-authorized");
  }
  const sourceSessionInvalid = mapping.sourceProvenance === "customer-current"
    ? asset.sessionId !== plan.sessionId
    : mapping.sourceProvenance === "customer-authorized-history"
      ? asset.sessionId === plan.sessionId
      : false;
  if (asset.customerId !== plan.customerId || sourceSessionInvalid ||
      asset.id !== mapping.sourceAssetId || permission.customerId !== plan.customerId ||
      permission.sessionId !== plan.sessionId || permission.sourceAssetId !== asset.id) {
    throw new Error("reference-render-source-scope-mismatch");
  }
  if (!(source.bytes instanceof Uint8Array) || source.bytes.byteLength === 0 ||
      asset.byteLength !== source.bytes.byteLength || sha256(source.bytes) !== asset.digest ||
      source.verifiedDigest !== asset.digest || mapping.sourceDigest !== asset.digest ||
      permission.sourceDigest !== asset.digest) {
    throw new Error("reference-render-source-digest-mismatch");
  }
  if (!mapping.permission || canonicalJson(permission) !== canonicalJson(mapping.permission)) {
    throw new Error("reference-render-permission-mismatch");
  }
}

function buildVariantPlans(input: ValidatedInput): ReferenceVariantPlan[] {
  const rulesById = new Map(input.analysis.rules.map((rule) => [rule.id, rule]));
  const videoMappings = executableMappings(input.mappingPlan).filter((mapping) => {
    const rule = rulesById.get(mapping.ruleId);
    if (!rule) throw new Error("reference-render-rule-binding-mismatch");
    return rule.kind !== "audio";
  });
  if (videoMappings.length === 0) throw new Error("reference-render-video-source-required");
  if (videoMappings.some((mapping) =>
    input.assetsById.get(mapping.sourceAssetId!)!.asset.mediaKind !== "video")) {
    throw new Error("reference-render-source-video-required");
  }
  if (videoMappings.some((mapping) => mapping.sourceProvenance === "self-owned-reference" &&
      mapping.reuseRights?.element !== "video-clips")) {
    throw new Error("reference-video-reuse-rights-required");
  }

  const masterEntries = buildProportionalMaster(videoMappings, input.analysis);
  const boundedMaster = fitDurationCeiling(masterEntries);
  const revisedMaster = input.revisionOverride?.target === "editorial-master"
    ? shortenOpening(boundedMaster.entries, input.revisionOverride.trimMs, input.analysis)
    : boundedMaster.entries;
  const xiaohongshu = deriveXiaohongshu(revisedMaster, input.analysis);
  const douyin = deriveDouyin(revisedMaster, input.analysis);
  const wechat = deriveWechat(revisedMaster, xiaohongshu.entries, input.analysis);
  const boundedXiaohongshu = fitDurationCeiling(xiaohongshu.entries);
  const boundedDouyin = fitDurationCeiling(douyin.entries);
  const boundedWechat = fitDurationCeiling(wechat.entries);
  let planned = [
    { kind: "editorial-master" as const, entries: revisedMaster, changes: [] as string[] },
    {
      kind: "xiaohongshu" as const,
      entries: boundedXiaohongshu.entries,
      changes: [xiaohongshu.changeCode, ...(boundedXiaohongshu.trimmed
        ? ["platform.xiaohongshu.trim-duration-ceiling"] : [])],
    },
    {
      kind: "douyin" as const,
      entries: boundedDouyin.entries,
      changes: [douyin.changeCode, ...(boundedDouyin.trimmed
        ? ["platform.douyin.trim-duration-ceiling"] : [])],
    },
    {
      kind: "wechat-channels" as const,
      entries: boundedWechat.entries,
      changes: [wechat.changeCode, ...(boundedWechat.trimmed
        ? ["platform.wechat-channels.trim-duration-ceiling"] : [])],
    },
  ];
  const identities = new Set(planned.map((item) => canonicalJson(item.entries.map((entry) => ({
    sourceAssetId: entry.mapping.sourceAssetId,
    sourceDigest: entry.mapping.sourceDigest,
    sourceStartMs: entry.sourceStartMs,
    sourceEndMs: entry.sourceEndMs,
  })))));
  if (identities.size !== VARIANT_KINDS.length) throw new Error("reference-platform-variation-unavailable");

  if (input.revisionOverride && input.revisionOverride.target !== "editorial-master") {
    planned = planned.filter((item) => item.kind === input.revisionOverride!.target).map((item) => ({
      ...item,
      entries: shortenOpening(item.entries, input.revisionOverride!.trimMs, input.analysis),
      changes: [...item.changes, "revision.shorten-opening"],
    }));
  }

  const bindings = renderBindings(input.mappingPlan);
  const audioRuleIds = new Set(input.analysis.rules.filter((rule) => rule.kind === "audio").map((rule) => rule.id));
  const audioMapping = input.audioOverlaySourceAssetId === null ? null : executableMappings(input.mappingPlan)
    .find((mapping) => mapping.sourceAssetId === input.audioOverlaySourceAssetId && audioRuleIds.has(mapping.ruleId))!;
  return planned.map(({ kind, entries, changes }) => {
    const timeline = buildTimeline(entries);
    const sourceMappings = uniqueMappings([
      ...entries.map((entry) => entry.mapping),
      ...(audioMapping ? [audioMapping] : []),
    ]);
    const authorizedSources = sourceMappings.map((mapping) => ({
      id: mapping.sourceBindingId!,
      mappingId: mapping.id,
      customerId: input.mappingPlan.customerId,
      sessionId: input.mappingPlan.sessionId,
      sourceAssetId: mapping.sourceAssetId!,
      sourceDigest: mapping.sourceDigest!,
      provenance: mapping.sourceProvenance!,
      permission: mapping.permission!,
      reuseRights: mapping.reuseRights,
    }));
    const audioOverlay = audioMapping ? {
      sourceBindingId: audioMapping.sourceBindingId!,
      sourceStartMs: audioMapping.sourceStartMs!,
      sourceEndMs: audioMapping.sourceEndMs!,
      volume: 0.35,
    } : null;
    const content = { bindings, kind, authorizedSources, timeline, audioOverlay, platformChangeCodes: changes };
    return ReferenceVariantPlanSchema.parse({
      id: `reference_variant_${sha256(canonicalJson(content))}`,
      customerId: input.mappingPlan.customerId,
      sessionId: input.mappingPlan.sessionId,
      version: 1,
      ...content,
      status: "rendered",
      publishClaimed: false,
      createdAt: input.now,
    });
  });
}

function shortenOpening(
  entries: readonly PlannedEntry[],
  trimMs: number,
  analysis: ReferenceStyleAnalysis,
): PlannedEntry[] {
  const opening = entries[0];
  if (!opening || opening.sourceStartMs + trimMs >= opening.sourceEndMs) {
    throw new Error("reference-revision-prompt-unachievable");
  }
  const revised = entries.map((entry, index) => index === 0
    ? { ...entry, sourceStartMs: entry.sourceStartMs + trimMs }
    : { ...entry });
  try {
    const refreshed = refreshTransitions(revised, analysis);
    assertTransitionDurations(refreshed);
    if (timelineLength(refreshed) <= 0) throw new Error("reference-transition-duration-invalid");
    return refreshed;
  } catch (error) {
    if (error instanceof Error && error.message === "reference-transition-duration-invalid") {
      throw new Error("reference-revision-prompt-unachievable");
    }
    throw error;
  }
}

function buildProportionalMaster(
  mappings: readonly MappingRow[],
  analysis: ReferenceStyleAnalysis,
): PlannedEntry[] {
  const transitions = mappings.map((mapping, index) =>
    index === 0 ? "cut" as const : transitionForMapping(mapping, analysis));
  const referenceDurations = mappings.map((mapping) =>
    mapping.referenceEndMs - mapping.referenceStartMs);
  const availableDurations = mappings.map((mapping) =>
    mapping.sourceEndMs! - mapping.sourceStartMs!);
  const maximumScale = Math.min(...referenceDurations.map((duration, index) =>
    availableDurations[index]! / duration));
  const overlapMs = transitions.filter((transition) => transition !== "cut").length *
    TRANSITION_DURATION_MS;
  const referenceTotalMs = referenceDurations.reduce((sum, duration) => sum + duration, 0);
  const sixtySecondScale = (60_000 + overlapMs) / referenceTotalMs;
  const scale = Math.min(maximumScale, sixtySecondScale);
  const entries = mappings.map((mapping, index): PlannedEntry => ({
    mapping,
    sourceStartMs: mapping.sourceStartMs!,
    sourceEndMs: mapping.sourceStartMs! + Math.max(1, Math.floor(referenceDurations[index]! * scale)),
    transition: transitions[index]!,
  }));
  assertTransitionDurations(entries);
  if (timelineLength(entries) > 60_000) throw new Error("reference-render-duration-out-of-range");
  return entries;
}

function fitDurationCeiling(
  entries: readonly PlannedEntry[],
): Readonly<{ entries: PlannedEntry[]; trimmed: boolean }> {
  let fitted = entries.map((entry) => ({ ...entry }));
  let trimmed = false;
  while (timelineLength(fitted) > MAX_SAFE_TIMELINE_MS) {
    const requiredMs = Math.ceil((timelineLength(fitted) - MAX_SAFE_TIMELINE_MS) / FRAME_DURATION_MS) *
      FRAME_DURATION_MS;
    let changed = false;
    for (let index = fitted.length - 1; index >= 0; index -= 1) {
      const entry = fitted[index]!;
      const durationMs = entry.sourceEndMs - entry.sourceStartMs;
      const minimumMs = entry.transition === "cut" ? FRAME_DURATION_MS : TRANSITION_DURATION_MS;
      const availableTrimMs = Math.floor((durationMs - minimumMs) / FRAME_DURATION_MS) * FRAME_DURATION_MS;
      if (availableTrimMs <= 0) continue;
      const trimMs = Math.min(requiredMs, availableTrimMs);
      fitted = fitted.map((row, rowIndex) => rowIndex === index
        ? { ...row, sourceEndMs: row.sourceEndMs - trimMs }
        : { ...row });
      assertTransitionDurations(fitted);
      trimmed = true;
      changed = true;
      break;
    }
    if (!changed) throw new Error("reference-render-duration-out-of-range");
  }
  return { entries: fitted, trimmed };
}

function assertTransitionDurations(entries: readonly PlannedEntry[]): void {
  let previousDurationMs = entries[0]
    ? entries[0].sourceEndMs - entries[0].sourceStartMs
    : 0;
  let composedDurationMs = previousDurationMs;
  for (let index = 1; index < entries.length; index += 1) {
    const entry = entries[index]!;
    const durationMs = entry.sourceEndMs - entry.sourceStartMs;
    if (entry.transition !== "cut" &&
        (durationMs < TRANSITION_DURATION_MS
          || previousDurationMs < TRANSITION_DURATION_MS
          || composedDurationMs < TRANSITION_DURATION_MS)) {
      throw new Error("reference-transition-duration-invalid");
    }
    composedDurationMs += durationMs - (entry.transition === "cut" ? 0 : TRANSITION_DURATION_MS);
    previousDurationMs = durationMs;
  }
}

function deriveXiaohongshu(
  master: readonly PlannedEntry[],
  analysis: ReferenceStyleAnalysis,
): Readonly<{ entries: PlannedEntry[]; changeCode: string }> {
  const reordered = master.map((entry) => ({ ...entry }));
  const firstIdentity = sourceIntervalIdentity(reordered[0]!);
  const swapIndex = reordered.findIndex((entry, index) => index > 0 && sourceIntervalIdentity(entry) !== firstIdentity);
  if (swapIndex >= 0) {
    [reordered[0], reordered[swapIndex]] = [reordered[swapIndex]!, reordered[0]!];
    const entries = refreshTransitions(reordered, analysis);
    assertTransitionDurations(entries);
    return { entries, changeCode: "platform.xiaohongshu.reorder-opening" };
  }
  return {
    entries: trimOneFrame(master, "start", analysis),
    changeCode: "platform.xiaohongshu.trim-start-frame",
  };
}

function deriveWechat(
  master: readonly PlannedEntry[],
  _xiaohongshu: readonly PlannedEntry[],
  analysis: ReferenceStyleAnalysis,
): Readonly<{ entries: PlannedEntry[]; changeCode: string }> {
  return {
    entries: trimOneFrame(master, "end", analysis),
    changeCode: "platform.wechat-channels.trim-end-frame",
  };
}

function deriveDouyin(
  master: readonly PlannedEntry[],
  analysis: ReferenceStyleAnalysis,
): Readonly<{ entries: PlannedEntry[]; changeCode: string }> {
  // Conservative separate cut; this is not a claim of Douyin-specific policy compliance.
  const openingTrimmed = trimOneFrame(master, "start", analysis);
  return {
    entries: trimOneFrame(openingTrimmed, "end", analysis),
    changeCode: "platform.douyin.tighten-cut-boundaries",
  };
}

function trimOneFrame(
  entries: readonly PlannedEntry[],
  side: "start" | "end",
  analysis: ReferenceStyleAnalysis,
): PlannedEntry[] {
  const indexes = [...entries.keys()];
  if (side === "end") indexes.reverse();
  for (const index of indexes) {
    const entry = entries[index]!;
    if (entry.sourceEndMs - entry.sourceStartMs <= FRAME_DURATION_MS) continue;
    const candidate = entries.map((row, rowIndex) => rowIndex !== index
      ? { ...row }
      : side === "start"
        ? { ...row, sourceStartMs: row.sourceStartMs + FRAME_DURATION_MS }
        : { ...row, sourceEndMs: row.sourceEndMs - FRAME_DURATION_MS });
    const refreshed = refreshTransitions(candidate, analysis);
    try {
      assertTransitionDurations(refreshed);
      return refreshed;
    } catch (error) {
      if (!(error instanceof Error) || error.message !== "reference-transition-duration-invalid") throw error;
    }
  }
  throw new Error("reference-platform-variation-unavailable");
}

function sourceIntervalIdentity(entry: PlannedEntry): string {
  return [entry.mapping.sourceAssetId, entry.mapping.sourceDigest, entry.sourceStartMs, entry.sourceEndMs].join(":");
}

function refreshTransitions(
  entries: readonly PlannedEntry[],
  analysis: ReferenceStyleAnalysis,
): PlannedEntry[] {
  return entries.map((entry, index) => ({
    ...entry,
    transition: index === 0 ? "cut" : transitionForMapping(entry.mapping, analysis),
  }));
}

function transitionForMapping(
  mapping: MappingRow,
  analysis: ReferenceStyleAnalysis,
): ExecutableTransitionKind {
  const rule = analysis.rules.find((candidate) => candidate.id === mapping.ruleId);
  if (!rule) throw new Error("reference-render-rule-binding-mismatch");
  if (rule.kind !== "transition") return "cut";
  const executable = analysis.transitions.filter((transition): transition is typeof transition & {
    kind: ExecutableTransitionKind;
  } =>
    rule.evidenceIds.includes(transition.id) &&
    transition.kind !== "unsupported" &&
    transition.confidence >= REFERENCE_TRANSITION_CONFIDENCE_THRESHOLD);
  const kinds = [...new Set(executable.map((transition) => transition.kind))];
  if (kinds.length > 1) throw new Error("reference-transition-evidence-ambiguous");
  return kinds[0] ?? "cut";
}

function buildTimeline(entries: readonly PlannedEntry[]): ReferenceVariantPlan["timeline"] {
  const totalReferenceDuration = entries.reduce((sum, entry) =>
    sum + entry.mapping.referenceEndMs - entry.mapping.referenceStartMs, 0);
  let cursorMs = 0;
  return entries.map((entry, index) => {
    const clipDuration = entry.sourceEndMs - entry.sourceStartMs;
    if (index > 0 && entry.transition !== "cut") {
      const previous = entries[index - 1]!;
      const previousDuration = previous.sourceEndMs - previous.sourceStartMs;
      if (clipDuration < TRANSITION_DURATION_MS || previousDuration < TRANSITION_DURATION_MS ||
          cursorMs < TRANSITION_DURATION_MS) {
        throw new Error("reference-transition-duration-invalid");
      }
    }
    const transitionOverlap = index > 0 && entry.transition !== "cut" ? TRANSITION_DURATION_MS : 0;
    cursorMs -= transitionOverlap;
    const timelineStartMs = cursorMs;
    cursorMs += entry.sourceEndMs - entry.sourceStartMs;
    return {
      id: `reference_timeline_${sha256(`${entry.mapping.id}\0${index}\0${entry.sourceStartMs}\0${entry.sourceEndMs}`)}`,
      mappingId: entry.mapping.id,
      ruleId: entry.mapping.ruleId,
      sourceBindingId: entry.mapping.sourceBindingId!,
      sourceStartMs: entry.sourceStartMs,
      sourceEndMs: entry.sourceEndMs,
      timelineStartMs,
      transition: entry.transition,
      targetDurationRatio: (entry.mapping.referenceEndMs - entry.mapping.referenceStartMs) /
        totalReferenceDuration,
      reasonCode: index === 0 ? "reference.timeline.open" : `reference.timeline.${entry.transition}`,
    };
  });
}

async function renderVariant(
  input: ValidatedInput,
  plan: ReferenceVariantPlan,
  stageDirectory: string,
  outputPath: string,
): Promise<number> {
  const variantDirectory = await mkdtemp(join(stageDirectory, `.${plan.kind}-`));
  try {
    const sourceByBinding = new Map(plan.authorizedSources.map((source) => [source.id, source]));
    const clipPaths: string[] = [];
    for (const [index, entry] of plan.timeline.entries()) {
      const binding = sourceByBinding.get(entry.sourceBindingId)!;
      const source = input.assetsById.get(binding.sourceAssetId)!;
      const sourcePath = join(variantDirectory, `source-${String(index).padStart(3, "0")}.bin`);
      const clipPath = join(variantDirectory, `clip-${String(index).padStart(3, "0")}.mp4`);
      await writeFile(sourcePath, source.bytes, { flag: "wx" });
      const sourceProbe = await probe(input.ffprobe, sourcePath, input.processLimits);
      if (!sourceProbe.video) throw new Error(`reference-render-source-video-missing:${source.asset.id}`);
      if (entry.sourceEndMs > sourceProbe.durationSeconds * 1_000 + 50) {
        throw new Error(`reference-render-source-range-invalid:${source.asset.id}`);
      }
      await normalizeClip(
        input.ffmpeg,
        sourcePath,
        clipPath,
        entry.sourceStartMs,
        entry.sourceEndMs - entry.sourceStartMs,
        sourceProbe.audio !== null,
        input.processLimits,
      );
      clipPaths.push(clipPath);
    }

    const composedBasePath = join(variantDirectory, "base.mp4");
    if (plan.timeline.every((entry) => entry.transition === "cut")) {
      await concatClips(input.ffmpeg, clipPaths, composedBasePath, input.processLimits);
    } else {
      await composeTransitionGraph(input.ffmpeg, clipPaths, plan.timeline, composedBasePath, input.processLimits);
    }
    await assertNonEmptyFile(composedBasePath);
    const sourceTimelineMs = timelineDurationMs(plan.timeline);
    if (sourceTimelineMs > 60_000) throw new Error("reference-render-duration-out-of-range");
    const presentationDurationMs = Math.max(30_000, sourceTimelineMs);
    const actualTimelineDurationMs = (await probe(
      input.ffprobe,
      composedBasePath,
      input.processLimits,
    )).durationSeconds * 1_000;
    let basePath = composedBasePath;
    if (presentationDurationMs > actualTimelineDurationMs) {
      basePath = join(variantDirectory, "base-padded.mp4");
      await padBaseToDuration(
        input.ffmpeg,
        composedBasePath,
        basePath,
        presentationDurationMs,
        actualTimelineDurationMs,
        input.processLimits,
      );
    }
    if (plan.audioOverlay) {
      const binding = sourceByBinding.get(plan.audioOverlay.sourceBindingId)!;
      const source = input.assetsById.get(binding.sourceAssetId)!;
      const audioPath = join(variantDirectory, "overlay-source.bin");
      await writeFile(audioPath, source.bytes, { flag: "wx" });
      const audioProbe = await probe(input.ffprobe, audioPath, input.processLimits);
      if (!audioProbe.audio) throw new Error("reference-audio-source-stream-required");
      if (plan.audioOverlay.sourceEndMs > audioProbe.durationSeconds * 1_000 + 50) {
        throw new Error("reference-audio-source-range-invalid");
      }
      await mixAudioOverlay(input.ffmpeg, basePath, audioPath, plan.audioOverlay, outputPath, input.processLimits);
    } else {
      await rename(basePath, outputPath);
    }
    return presentationDurationMs;
  } finally {
    await rm(variantDirectory, { recursive: true, force: true });
  }
}

async function padBaseToDuration(
  ffmpeg: string,
  sourcePath: string,
  outputPath: string,
  presentationDurationMs: number,
  actualTimelineDurationMs: number,
  limits: ProcessLimits,
): Promise<void> {
  const missingDurationMs = Math.max(0, presentationDurationMs - actualTimelineDurationMs);
  const paddingDurationMs = Math.ceil(
    (missingDurationMs + FRAME_DURATION_MS) / FRAME_DURATION_MS,
  ) * FRAME_DURATION_MS;
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error", "-i", sourcePath,
    "-vf", `tpad=stop_mode=clone:stop_duration=${seconds(paddingDurationMs)}`,
    "-af", `apad=pad_dur=${seconds(paddingDurationMs)}`,
    "-t", seconds(presentationDurationMs),
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "25",
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
    "-y", outputPath,
  ], "ffmpeg-reference-duration-pad-failed", limits);
  await assertNonEmptyFile(outputPath);
}

async function normalizeClip(
  ffmpeg: string,
  sourcePath: string,
  outputPath: string,
  startMs: number,
  durationMs: number,
  hasAudio: boolean,
  limits: ProcessLimits,
): Promise<void> {
  const args = ["-hide_banner", "-loglevel", "error", "-ss", seconds(startMs), "-i", sourcePath];
  if (hasAudio) {
    args.push("-map", "0:v:0", "-map", "0:a:0");
  } else {
    args.push(
      "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
      "-map", "0:v:0", "-map", "1:a:0",
    );
  }
  args.push(
    "-t", seconds(durationMs),
    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", "-y", outputPath,
  );
  await runChecked(ffmpeg, args, "ffmpeg-reference-normalize-failed", limits);
  await assertNonEmptyFile(outputPath);
}

async function concatClips(
  ffmpeg: string,
  clipPaths: readonly string[],
  outputPath: string,
  limits: ProcessLimits,
): Promise<void> {
  const args = ["-hide_banner", "-loglevel", "error"];
  for (const path of clipPaths) args.push("-i", path);
  const inputs = clipPaths.map((_, index) => `[${index}:v][${index}:a]`).join("");
  args.push(
    "-filter_complex", `${inputs}concat=n=${clipPaths.length}:v=1:a=1[v][a]`,
    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
    "-pix_fmt", "yuv420p", "-r", "25", "-c:a", "aac", "-ar", "44100", "-ac", "2",
    "-movflags", "+faststart", "-y", outputPath,
  );
  await runChecked(ffmpeg, args, "ffmpeg-reference-concat-failed", limits);
}

async function composeTransitionGraph(
  ffmpeg: string,
  clipPaths: readonly string[],
  timeline: ReferenceVariantPlan["timeline"],
  outputPath: string,
  limits: ProcessLimits,
): Promise<void> {
  const args = ["-hide_banner", "-loglevel", "error"];
  for (const path of clipPaths) args.push("-i", path);
  const filters: string[] = [];
  for (const index of clipPaths.keys()) {
    filters.push(`[${index}:v]settb=AVTB,setpts=PTS-STARTPTS[v${index}]`);
    filters.push(`[${index}:a]asetpts=PTS-STARTPTS[a${index}]`);
  }
  let videoLabel = "v0";
  let audioLabel = "a0";
  let currentDurationMs = timeline[0]!.sourceEndMs - timeline[0]!.sourceStartMs;
  for (let index = 1; index < timeline.length; index += 1) {
    const entry = timeline[index]!;
    const nextDurationMs = entry.sourceEndMs - entry.sourceStartMs;
    const nextVideoLabel = `vx${index}`;
    const nextAudioLabel = `ax${index}`;
    if (entry.transition === "cut") {
      filters.push(`[${videoLabel}][v${index}]concat=n=2:v=1:a=0[${nextVideoLabel}]`);
      filters.push(`[${audioLabel}][a${index}]concat=n=2:v=0:a=1[${nextAudioLabel}]`);
      currentDurationMs += nextDurationMs;
    } else {
      const transition = entry.transition === "fade" ? "fadeblack" : "fade";
      const offsetSeconds = seconds(currentDurationMs - TRANSITION_DURATION_MS);
      filters.push(
        `[${videoLabel}][v${index}]xfade=transition=${transition}:duration=0.35:offset=${offsetSeconds}[${nextVideoLabel}]`,
      );
      filters.push(
        `[${audioLabel}][a${index}]acrossfade=d=0.35:c1=tri:c2=tri[${nextAudioLabel}]`,
      );
      currentDurationMs += nextDurationMs - TRANSITION_DURATION_MS;
    }
    videoLabel = nextVideoLabel;
    audioLabel = nextAudioLabel;
  }
  args.push(
    "-filter_complex", filters.join(";"),
    "-map", `[${videoLabel}]`, "-map", `[${audioLabel}]`,
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "25",
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
    "-y", outputPath,
  );
  await runChecked(ffmpeg, args, "ffmpeg-reference-transition-graph-failed", limits);
}

async function mixAudioOverlay(
  ffmpeg: string,
  basePath: string,
  sourcePath: string,
  overlay: NonNullable<ReferenceVariantPlan["audioOverlay"]>,
  outputPath: string,
  limits: ProcessLimits,
): Promise<void> {
  const durationSeconds = seconds(overlay.sourceEndMs - overlay.sourceStartMs);
  const filter = `[1:a]atrim=start=${seconds(overlay.sourceStartMs)}:duration=${durationSeconds},` +
    `asetpts=PTS-STARTPTS,volume=${overlay.volume},apad[overlay];` +
    "[0:a][overlay]amix=inputs=2:duration=first:dropout_transition=0[aout]";
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-i", basePath, "-i", sourcePath,
    "-filter_complex", filter,
    "-map", "0:v:0", "-map", "[aout]",
    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
    "-movflags", "+faststart", "-shortest", "-y", outputPath,
  ], "ffmpeg-reference-audio-overlay-failed", limits);
}

function buildDraft(plan: ReferenceVariantPlan, artifact: RenderedArtifact): ReferenceDraftPackage {
  const sources = new Map(plan.authorizedSources.map((source) => [source.id, source]));
  const lineage: ReferenceDraftPackage["lineage"][number][] = plan.timeline.map((entry) => {
    const source = sources.get(entry.sourceBindingId)!;
    return {
      id: `reference_lineage_${sha256(`${plan.id}\0video\0${entry.id}`)}`,
      use: "video" as const,
      timelineEntryId: entry.id,
      mappingId: entry.mappingId,
      sourceBindingId: entry.sourceBindingId,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: entry.sourceStartMs,
      sourceEndMs: entry.sourceEndMs,
    };
  });
  if (plan.audioOverlay) {
    const source = sources.get(plan.audioOverlay.sourceBindingId)!;
    lineage.push({
      id: `reference_lineage_${sha256(`${plan.id}\0audio\0${plan.audioOverlay.sourceBindingId}`)}`,
      use: "audio-overlay",
      timelineEntryId: null,
      mappingId: source.mappingId,
      sourceBindingId: source.id,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: plan.audioOverlay.sourceStartMs,
      sourceEndMs: plan.audioOverlay.sourceEndMs,
    });
  }
  return ReferenceDraftPackageSchema.parse({
    id: `reference_draft_${sha256(`${plan.id}\0${artifact.outputDigest}`)}`,
    customerId: plan.customerId,
    sessionId: plan.sessionId,
    version: 1,
    bindings: {
      mappingPlan: plan.bindings.mappingPlan,
      variantPlan: {
        id: plan.id,
        version: plan.version,
        customerId: plan.customerId,
        sessionId: plan.sessionId,
        kind: plan.kind,
      },
    },
    outputDigest: artifact.outputDigest,
    outputPath: artifact.finalPath,
    durationSeconds: artifact.metadata.durationSeconds,
    lineage,
    status: "reviewable",
    capability: OFFLINE_STUDIO_CAPABILITY,
    publishClaimed: false,
    createdAt: plan.createdAt,
  });
}

function buildReportInput(
  mappingPlan: ReferenceMaterialMappingPlan,
  plans: readonly ReferenceVariantPlan[],
  drafts: readonly ReferenceDraftPackage[],
  artifacts: readonly RenderedArtifact[],
): ReferenceRenderEvidence {
  const artifactRows = plans.map((plan, index) => ({
    kind: plan.kind,
    variantPlanId: plan.id,
    variantPlanVersion: plan.version,
    draftId: drafts[index]!.id,
    draftVersion: drafts[index]!.version,
    outputPath: artifacts[index]!.finalPath,
    outputDigest: artifacts[index]!.outputDigest,
    durationSeconds: artifacts[index]!.metadata.durationSeconds,
    videoCodec: "h264" as const,
    audioCodec: "aac" as const,
    width: 720 as const,
    height: 1280 as const,
  }));
  const sourceLineage = plans.flatMap((plan) => {
    const sources = new Map(plan.authorizedSources.map((source) => [source.id, source]));
    const timelineRows = plan.timeline.map((entry) => {
      const source = sources.get(entry.sourceBindingId)!;
      return {
        kind: plan.kind,
        timelineEntryId: entry.id,
        mappingId: entry.mappingId,
        sourceBindingId: entry.sourceBindingId,
        sourceAssetId: source.sourceAssetId,
        sourceDigest: source.sourceDigest,
        sourceStartMs: entry.sourceStartMs,
        sourceEndMs: entry.sourceEndMs,
        permissionId: source.permission.id,
        permissionVersion: source.permission.version,
        permissionEpoch: source.permission.epoch,
      };
    });
    if (!plan.audioOverlay) return timelineRows;
    const source = sources.get(plan.audioOverlay.sourceBindingId)!;
    return [...timelineRows, {
      kind: plan.kind,
      timelineEntryId: `audio-${plan.audioOverlay.sourceBindingId}`,
      mappingId: source.mappingId,
      sourceBindingId: source.id,
      sourceAssetId: source.sourceAssetId,
      sourceDigest: source.sourceDigest,
      sourceStartMs: plan.audioOverlay.sourceStartMs,
      sourceEndMs: plan.audioOverlay.sourceEndMs,
      permissionId: source.permission.id,
      permissionVersion: source.permission.version,
      permissionEpoch: source.permission.epoch,
    }];
  });
  const executedTransitions = plans.flatMap((plan) => plan.timeline.flatMap((entry) =>
    entry.transition === "fade" || entry.transition === "crossfade" ? [{
      kind: entry.transition,
      variantKind: plan.kind,
      timelineEntryId: entry.id,
      offsetMs: entry.timelineStartMs,
      durationMs: TRANSITION_DURATION_MS as 350,
    }] : []));
  return {
    bindings: plans[0]!.bindings,
    artifacts: artifactRows,
    sourceLineage,
    executedTransitions,
    mappingCounts: {
      matched: mappingPlan.mappings.filter((mapping) => mapping.status === "matched").length,
      substituted: mappingPlan.mappings.filter((mapping) => mapping.status === "substituted").length,
      unachievable: mappingPlan.mappings.filter((mapping) => mapping.status === "unachievable").length,
    },
    platformChanges: plans.filter((plan): plan is ReferenceVariantPlan & {
      kind: "xiaohongshu" | "douyin" | "wechat-channels";
    } => plan.kind !== "editorial-master").map((plan) => ({
      kind: plan.kind,
      changeCodes: plan.platformChangeCodes,
    })),
    publishClaimed: false,
  };
}

type ProbeMetadata = Readonly<{
  durationSeconds: number;
  video: Readonly<{ codec: string; width: number; height: number }> | null;
  audio: Readonly<{ codec: string }> | null;
}>;

async function verifyOutput(
  ffprobe: string,
  path: string,
  limits: ProcessLimits,
  inputFds: readonly number[] = [],
): Promise<ProbeMetadata> {
  const metadata = await probe(ffprobe, path, limits, inputFds);
  if (!metadata.video) throw new Error("reference-render-output-video-missing");
  if (metadata.video.codec !== "h264" || metadata.video.width !== 720 || metadata.video.height !== 1280) {
    throw new Error("reference-render-output-video-format-invalid");
  }
  if (!metadata.audio || metadata.audio.codec !== "aac") {
    throw new Error("reference-render-output-audio-format-invalid");
  }
  if (metadata.durationSeconds < 30 || metadata.durationSeconds > 60) {
    throw new Error("reference-render-output-duration-invalid");
  }
  return metadata;
}

async function verifyPinnedOutput(
  ffprobe: string,
  handle: FileHandle,
  limits: ProcessLimits,
): Promise<ProbeMetadata> {
  const descriptorPath = process.platform === "linux" ? "/proc/self/fd/3" : "/dev/fd/3";
  return verifyOutput(ffprobe, descriptorPath, limits, [handle.fd]);
}

async function probe(
  ffprobe: string,
  path: string,
  limits: ProcessLimits,
  inputFds: readonly number[] = [],
): Promise<ProbeMetadata> {
  const result = await runChecked(ffprobe, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
    "-of", "json",
    path,
  ], "ffprobe-reference-inspection-failed", limits, undefined, inputFds);
  let parsed: {
    streams?: Array<{ codec_type?: unknown; codec_name?: unknown; width?: unknown; height?: unknown }>;
    format?: { duration?: unknown };
  };
  try {
    parsed = JSON.parse(result.stdout) as typeof parsed;
  } catch {
    throw new Error("ffprobe-reference-output-invalid");
  }
  const durationSeconds = Number(parsed.format?.duration);
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error("ffprobe-reference-duration-invalid");
  }
  const video = parsed.streams?.find((stream) => stream.codec_type === "video");
  const audio = parsed.streams?.find((stream) => stream.codec_type === "audio");
  return {
    durationSeconds,
    video: video && typeof video.codec_name === "string"
      ? { codec: video.codec_name, width: Number(video.width), height: Number(video.height) }
      : null,
    audio: audio && typeof audio.codec_name === "string" ? { codec: audio.codec_name } : null,
  };
}

function renderBindings(plan: ReferenceMaterialMappingPlan): ReferenceVariantPlan["bindings"] {
  const permissions = new Map<string, Omit<MappingPermission, "view" | "use">>();
  for (const mapping of executableMappings(plan)) {
    const { view: _view, use: _use, ...identity } = mapping.permission!;
    permissions.set(identity.id, identity);
  }
  return {
    ...plan.bindings,
    mappingPlan: {
      id: plan.id,
      version: plan.version,
      customerId: plan.customerId,
      sessionId: plan.sessionId,
      contentDigest: plan.contentDigest,
      permissionSetDigest: plan.permissionSetDigest,
      permissions: [...permissions.values()].sort((left, right) => compareText(left.id, right.id)),
      status: plan.status,
      confirmation: {
        receiptId: plan.confirmationReceiptId!,
        actorId: plan.confirmedByActorId!,
        confirmedAt: plan.confirmedAt!,
      },
    },
  };
}

function executableMappings(plan: ReferenceMaterialMappingPlan): Array<MappingRow & {
  sourceBindingId: string;
  sourceAssetId: string;
  sourceDigest: string;
  sourceStartMs: number;
  sourceEndMs: number;
  sourceProvenance: NonNullable<MappingRow["sourceProvenance"]>;
  permission: MappingPermission;
}> {
  return plan.mappings.filter((mapping): mapping is ReturnType<typeof executableMappings>[number] =>
    mapping.status !== "unachievable" && mapping.sourceBindingId !== null &&
    mapping.sourceAssetId !== null && mapping.sourceDigest !== null &&
    mapping.sourceStartMs !== null && mapping.sourceEndMs !== null &&
    mapping.sourceProvenance !== null && mapping.permission !== null);
}

function uniqueMappings(mappings: readonly MappingRow[]): MappingRow[] {
  return [...new Map(mappings.map((mapping) => [mapping.id, mapping])).values()];
}

function timelineLength(entries: readonly PlannedEntry[]): number {
  return entries.reduce((sum, entry, index) => sum + entry.sourceEndMs - entry.sourceStartMs -
    (index > 0 && entry.transition !== "cut" ? TRANSITION_DURATION_MS : 0), 0);
}

function timelineDurationMs(timeline: ReferenceVariantPlan["timeline"]): number {
  return timeline.reduce((sum, entry, index) => sum + entry.sourceEndMs - entry.sourceStartMs -
    (index > 0 && entry.transition !== "cut" ? TRANSITION_DURATION_MS : 0), 0);
}

function outputFileName(input: ValidatedInput, kind: ReferenceVariantKind): string {
  const runDigest = sha256(`${input.mappingPlan.id}\0${input.mappingPlan.contentDigest}\0${input.operationKey}`)
    .slice(0, 24);
  return `reference-${runDigest}-${kind}.mp4`;
}

function buildRunClaim(
  input: ValidatedInput,
  plans: readonly ReferenceVariantPlan[],
): RunClaim {
  const runDigest = sha256([
    input.mappingPlan.id,
    input.mappingPlan.contentDigest,
    input.operationKey,
  ].join("\0")).slice(0, 24);
  const name = `reference-run-${runDigest}`;
  const directory = join(input.outputRoot, name);
  assertContainedPath(input.outputRoot, directory);
  const outputNames = plans.map((plan) => outputFileName(input, plan.kind));
  for (const outputName of outputNames) assertContainedPath(directory, join(directory, outputName));
  return { directory, name, ownerToken: randomUUID(), outputNames };
}

async function createRunLayout(
  input: ValidatedInput,
  claim: RunClaim,
): Promise<RunLayout> {
  let result: { stdout: string; stderr: string };
  try {
    result = await runOutputAnchor(input, {
      action: "create",
      root: identityPayload(input.outputRootIdentity),
      run: { name: claim.name, ownerToken: claim.ownerToken },
      outputs: claim.outputNames,
    }, "reference-render-output-create-failed");
  } catch (error) {
    if (errorMessage(error) === "reference-render-output-create-failed:reference-render-output-exists") {
      throw new PreexistingRunDirectoryError(error);
    }
    throw error;
  }
  const parsed = JSON.parse(result.stdout) as {
    dev: number;
    ino: number;
    outputs: Array<{ name: string; dev: number; ino: number }>;
  };
  if (!Number.isSafeInteger(parsed.dev) || !Number.isSafeInteger(parsed.ino) ||
      parsed.outputs.length !== claim.outputNames.length) {
    throw new Error("reference-render-output-anchor-invalid");
  }
  const outputs = parsed.outputs.map((output, index) => {
    if (output.name !== claim.outputNames[index] || !Number.isSafeInteger(output.dev) ||
        !Number.isSafeInteger(output.ino)) {
      throw new Error("reference-render-output-anchor-invalid");
    }
    return { ...output, path: join(claim.directory, output.name) };
  });
  const layout = {
    directory: claim.directory,
    name: claim.name,
    ownerToken: claim.ownerToken,
    dev: parsed.dev,
    ino: parsed.ino,
    outputs,
  };
  await assertRunLayoutCurrent(input.outputRootIdentity, layout);
  return layout;
}

async function promoteRunLayout(
  input: ValidatedInput,
  layout: RunLayout,
  artifacts: readonly RenderedArtifact[],
): Promise<void> {
  for (const artifact of artifacts) await assertStagedArtifactCurrent(artifact);
  const result = await runOutputAnchor(input, {
    action: "promote",
    root: identityPayload(input.outputRootIdentity),
    run: { name: layout.name, ownerToken: layout.ownerToken, dev: layout.dev, ino: layout.ino },
    outputs: layout.outputs.map((output, index) => ({
      name: output.name,
      dev: output.dev,
      ino: output.ino,
      stagePath: artifacts[index]!.stagePath,
      stage: {
        dev: artifacts[index]!.stageDev,
        ino: artifacts[index]!.stageIno,
        size: artifacts[index]!.stageSize,
        digest: artifacts[index]!.outputDigest,
      },
    })),
  }, "reference-render-output-promote-failed", artifacts.map((artifact) => artifact.stageHandle.fd));
  const promoted = JSON.parse(result.stdout) as { digests?: unknown };
  if (!Array.isArray(promoted.digests) ||
      canonicalJson(promoted.digests) !== canonicalJson(artifacts.map((artifact) => artifact.outputDigest))) {
    throw new Error("reference-render-output-promote-evidence-invalid");
  }
  for (const artifact of artifacts) await assertStagedArtifactCurrent(artifact);
}

async function cleanupRunClaim(input: ValidatedInput, run: RunLayout | RunClaim): Promise<void> {
  await runOutputAnchor(input, {
    action: "cleanup",
    root: identityPayload(input.outputRootIdentity),
    run: {
      name: run.name,
      ownerToken: run.ownerToken,
      ...(runHasIdentity(run) ? { dev: run.dev, ino: run.ino } : {}),
    },
  }, "reference-render-output-cleanup-failed");
}

function runHasIdentity(run: RunLayout | RunClaim): run is RunLayout {
  return "dev" in run && "ino" in run;
}

function identityPayload(identity: OutputRootIdentity): { dev: number; ino: number } {
  return { dev: identity.dev, ino: identity.ino };
}

async function runOutputAnchor(
  input: ValidatedInput,
  payload: unknown,
  errorCode: string,
  inputFds: readonly number[] = [],
): Promise<{ stdout: string; stderr: string }> {
  const encoded = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const anchorLimits = {
    ...input.processLimits,
    maxOutputBytes: Math.max(input.processLimits.maxOutputBytes, 16_384),
  };
  return runChecked(process.execPath, ["-e", OUTPUT_ANCHOR_SCRIPT, encoded], errorCode,
    anchorLimits, input.outputRootIdentity.realPath, inputFds);
}

async function assertRunLayoutCurrent(root: OutputRootIdentity, layout: RunLayout): Promise<void> {
  if (!await runLayoutIsCurrent(root, layout)) {
    throw new Error("reference-render-output-root-changed");
  }
}

async function verifyFinalArtifacts(
  layout: RunLayout,
  artifacts: readonly RenderedArtifact[],
): Promise<void> {
  for (const [index, artifact] of artifacts.entries()) {
    const output = layout.outputs[index]!;
    const handle = await open(output.path, constants.O_RDONLY | constants.O_NOFOLLOW).catch(() => null);
    if (!handle) throw new Error("reference-render-output-root-changed");
    try {
      const before = await handle.stat();
      if (!before.isFile() || before.dev !== output.dev || before.ino !== output.ino) {
        throw new Error("reference-render-output-root-changed");
      }
      if (before.size !== artifact.stageSize) {
        throw new Error("reference-render-final-digest-mismatch");
      }
      const digest = await digestFileHandle(handle, before.size);
      const after = await handle.stat();
      if (after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size) {
        throw new Error("reference-render-final-digest-mismatch");
      }
      if (digest !== artifact.outputDigest) {
        throw new Error("reference-render-final-digest-mismatch");
      }
      if (!await pathMatchesIdentity(output.path, output.dev, output.ino)) {
        throw new Error("reference-render-output-root-changed");
      }
    } finally {
      await handle.close().catch(() => undefined);
    }
  }
}

async function runLayoutIsCurrent(root: OutputRootIdentity, layout: RunLayout): Promise<boolean> {
  try {
    await assertOutputRootUnchanged(root);
  } catch {
    return false;
  }
  if (!await pathMatchesIdentity(layout.directory, layout.dev, layout.ino)) return false;
  for (const output of layout.outputs) {
    if (!await pathMatchesIdentity(output.path, output.dev, output.ino)) return false;
  }
  return true;
}

async function pathMatchesIdentity(path: string, dev: number, ino: number): Promise<boolean> {
  const metadata = await lstat(path).catch(() => null);
  if (!metadata || metadata.isSymbolicLink()) return false;
  return (metadata.isFile() || metadata.isDirectory()) && metadata.dev === dev && metadata.ino === ino;
}

async function pinStagedArtifact(stagePath: string): Promise<Pick<
  RenderedArtifact,
  "stageHandle" | "stageDev" | "stageIno" | "stageSize" | "outputDigest"
>> {
  const stageHandle = await open(stagePath, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const metadata = await stageHandle.stat();
    if (!metadata.isFile() || metadata.size <= 0) throw new Error("reference-render-stage-file-invalid");
    const outputDigest = await digestFileHandle(stageHandle, metadata.size);
    const after = await stageHandle.stat();
    if (after.dev !== metadata.dev || after.ino !== metadata.ino || after.size !== metadata.size) {
      throw new Error("reference-render-stage-identity-changed");
    }
    return {
      stageHandle,
      stageDev: metadata.dev,
      stageIno: metadata.ino,
      stageSize: metadata.size,
      outputDigest,
    };
  } catch (error) {
    await stageHandle.close().catch(() => undefined);
    throw error;
  }
}

async function digestFileHandle(handle: FileHandle, size: number): Promise<string> {
  const digest = createHash("sha256");
  const buffer = Buffer.allocUnsafe(262_144);
  let offset = 0;
  while (offset < size) {
    const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, size - offset), offset);
    if (bytesRead <= 0) throw new Error("reference-render-stage-read-short");
    digest.update(buffer.subarray(0, bytesRead));
    offset += bytesRead;
  }
  return digest.digest("hex");
}

async function assertStagedArtifactCurrent(artifact: RenderedArtifact): Promise<void> {
  if (!await pathMatchesIdentity(artifact.stagePath, artifact.stageDev, artifact.stageIno)) {
    throw new Error("reference-render-stage-identity-changed");
  }
  const metadata = await artifact.stageHandle.stat();
  if (!metadata.isFile() || metadata.dev !== artifact.stageDev || metadata.ino !== artifact.stageIno ||
      metadata.size !== artifact.stageSize) {
    throw new Error("reference-render-stage-identity-changed");
  }
}

function combinedCleanupError(primary: unknown, cleanup: unknown): Error {
  return new Error(`${errorMessage(primary)};reference-render-cleanup-error:${errorMessage(cleanup)}`, {
    cause: primary,
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function validateOutputRoot(outputRootValue: string): Promise<OutputRootIdentity> {
  if (typeof outputRootValue !== "string" || !isAbsolute(outputRootValue) || resolve(outputRootValue) !== outputRootValue) {
    throw new Error("reference-render-output-root-invalid");
  }
  const metadata = await lstat(outputRootValue).catch(() => null);
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("reference-render-output-root-unavailable");
  }
  const realPath = await realpath(outputRootValue);
  const identity = await stat(realPath);
  return { path: outputRootValue, realPath, dev: identity.dev, ino: identity.ino };
}

async function assertOutputRootUnchanged(expected: OutputRootIdentity): Promise<void> {
  const metadata = await lstat(expected.path).catch(() => null);
  if (!metadata?.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("reference-render-output-root-changed");
  }
  const realPath = await realpath(expected.path).catch(() => null);
  const identity = realPath ? await stat(realPath).catch(() => null) : null;
  if (realPath !== expected.realPath || identity?.dev !== expected.dev || identity.ino !== expected.ino) {
    throw new Error("reference-render-output-root-changed");
  }
}

function assertContainedPath(root: string, candidate: string): void {
  const child = relative(root, candidate);
  if (!child || child === ".." || child.startsWith(`..${sep}`) || isAbsolute(child)) {
    throw new Error("reference-render-output-outside-root");
  }
}

async function runChecked(
  command: string,
  args: string[],
  errorCode: string,
  limits: ProcessLimits,
  cwd?: string,
  inputFds: readonly number[] = [],
): Promise<{ stdout: string; stderr: string }> {
  const result = await new Promise<{ code: number | null; stdout: string; stderr: string; failure: string | null }>((resolveResult, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe", ...inputFds],
      detached: process.platform !== "win32",
      cwd,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let byteCount = 0;
    let failure: string | null = null;
    let killTimer: NodeJS.Timeout | null = null;
    const terminate = (reason: string) => {
      if (failure) return;
      failure = reason;
      const target = process.platform === "win32" ? child.pid : child.pid ? -child.pid : undefined;
      if (target !== undefined) {
        try { process.kill(target, "SIGTERM"); } catch { /* already exited */ }
        killTimer = setTimeout(() => {
          try { process.kill(target, "SIGKILL"); } catch { /* already exited */ }
        }, limits.killGraceMs);
        killTimer.unref();
      }
    };
    const timeout = setTimeout(() => terminate(`${errorCode.replace(/-failed$/, "")}-timeout`), limits.timeoutMs);
    timeout.unref();
    const drain = (target: Buffer[]) => (chunk: Buffer | string) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      byteCount += buffer.byteLength;
      if (byteCount > limits.maxOutputBytes) terminate(`${errorCode.replace(/-failed$/, "")}-output-overflow`);
      else target.push(buffer);
    };
    child.stdout!.on("data", drain(stdout));
    child.stderr!.on("data", drain(stderr));
    child.once("error", reject);
    child.once("close", (code) => {
      clearTimeout(timeout);
      if (killTimer) clearTimeout(killTimer);
      resolveResult({ code, stdout: Buffer.concat(stdout).toString(), stderr: Buffer.concat(stderr).toString(), failure });
    });
  }).catch((error: unknown) => {
    throw new Error(`${errorCode}:${error instanceof Error ? error.message : "spawn-failed"}`);
  });
  if (result.failure) throw new Error(result.failure);
  if (result.code !== 0) throw new Error(`${errorCode}:${result.stderr.trim()}`);
  return result;
}

function validateProcessLimits(value: Partial<ProcessLimits> | undefined): ProcessLimits {
  const result = { ...DEFAULT_PROCESS_LIMITS, ...value };
  if (!Number.isSafeInteger(result.timeoutMs) || result.timeoutMs < 50 || result.timeoutMs > 600_000 ||
      !Number.isSafeInteger(result.maxOutputBytes) || result.maxOutputBytes < 256 || result.maxOutputBytes > 16_777_216 ||
      !Number.isSafeInteger(result.killGraceMs) || result.killGraceMs < 10 || result.killGraceMs > 10_000) {
    throw new Error("reference-render-process-limits-invalid");
  }
  return result;
}

async function assertExecutable(path: string | null, error: string): Promise<void> {
  if (!path) throw new Error(error);
  const metadata = await stat(path).catch(() => null);
  if (!metadata?.isFile() || (metadata.mode & 0o111) === 0) throw new Error(error);
}

async function assertNonEmptyFile(path: string): Promise<void> {
  const metadata = await stat(path).catch(() => null);
  if (!metadata?.isFile() || metadata.size === 0) throw new Error("reference-render-output-empty");
}

function rightsBinding(rights: ReferenceRightsDeclaration) {
  return {
    id: rights.id,
    version: rights.version,
    customerId: rights.customerId,
    sessionId: rights.sessionId,
    referenceSampleId: rights.sample.id,
    referenceSampleVersion: rights.sample.version,
    referenceSampleDigest: rights.sample.sourceDigest,
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

function assetIdentity(source: ReferenceRenderAsset): unknown {
  return {
    asset: source.asset,
    permission: source.permission,
    withdrawn: source.withdrawn,
    verifiedDigest: source.verifiedDigest,
    bytesDigest: source.bytes instanceof Uint8Array ? sha256(source.bytes) : null,
  };
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

function seconds(milliseconds: number): string {
  return (milliseconds / 1_000).toFixed(3);
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
