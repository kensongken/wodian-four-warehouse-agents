#!/usr/bin/env node

import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants, type BigIntStats } from "node:fs";
import {
  chmod,
  link,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rm,
  unlink,
  type FileHandle,
} from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  ModelAnalysisJobV1Schema,
  ModelAnalysisReceiptV1Schema,
  StudioWorkerRuntimeProbeReceiptV1Schema,
  assertReceiptMatchesJob,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type StudioWorkerRuntimeProbeBindingV1,
  type StudioWorkerRuntimeProbeReceiptV1,
} from "@wearable/contracts";
import {
  LocalProcessStudioModelWorker,
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
  type StudioWorkerStageTelemetrySummaryV1,
  type StudioTrustedWorkerContext,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

import {
  createDefaultStudioModelWorkerConfig,
  MODEL_WORKER_OUTER_TIMEOUT_MS,
  type DefaultStudioModelWorkerDependencies,
} from "../../review-app/src/studio-server";
import {
  buildPinnedWorkerRuntimeDiagnostic,
  canonicalPinnedWorkerRuntimeDiagnostic,
  type PinnedWorkerRuntimeDiagnosticV1,
  type StructuredOutputFailureDetail,
} from "./pinned-worker-runtime-diagnostic";

// @ts-expect-error The hardened artifact registry is an ESM script consumed directly by the CLI.
import { artifactManifest, findArtifact, loadModelLock } from "../../../scripts/studio-model-registry.mjs";
// @ts-expect-error The hardened runtime registry is an ESM script consumed directly by the CLI.
import { canonicalWorkerRuntimeProbeJson, findWorkerRuntime, FORMAL_QWEN_PROBE_INPUT_SHA256, FORMAL_QWEN_PROBE_MODEL_ID, FORMAL_QWEN_PROBE_OBSERVATION_KINDS, FORMAL_QWEN_PROBE_PROMPT_VERSION, FORMAL_WORKER_RUNTIME_ID, parseFormalWorkerRuntimeProbeReceipt } from "../../../scripts/studio-worker-runtime-registry.mjs";

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const DEFAULT_RUNTIME_ID = FORMAL_WORKER_RUNTIME_ID as string;
const QWEN_ID = FORMAL_QWEN_PROBE_MODEL_ID as string;
const PROMPT_VERSION = FORMAL_QWEN_PROBE_PROMPT_VERSION as string;
const MAX_RECEIPT_BYTES = 64 * 1024;
const DIGEST = /^[a-f0-9]{64}$/u;
const SAFE_IDENTITY = /^[A-Za-z0-9._-]{1,200}$/u;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const ALLOWED_OBSERVATION_KINDS = FORMAL_QWEN_PROBE_OBSERVATION_KINDS as readonly [
  "visible-person", "visible-product", "visible-action", "visible-scene",
];
const ALLOWED_OBSERVATION_KIND_SET = new Set<string>(ALLOWED_OBSERVATION_KINDS);
const STRUCTURED_OUTPUT_FAILURE_DETAILS = Object.freeze({
  "studio-vision-output-schema-top-level-object-keys": "top-level-object-keys",
  "studio-vision-output-schema-suggested-tag-shape-content-duplication": "suggested-tag-shape-content-duplication",
  "studio-vision-output-schema-visual-observation-shape-kind": "visual-observation-shape-kind",
  "studio-vision-output-schema-visual-observation-interval": "visual-observation-interval",
  "studio-vision-output-schema-demand-evidence-without-trusted-demand": "demand-evidence-without-trusted-demand",
  "studio-vision-output-schema-demand-evidence-shape-trusted-text": "demand-evidence-shape-trusted-text",
  "studio-vision-output-schema-warning-uncertainty-text-arrays": "warning-uncertainty-text-arrays",
  "studio-vision-output-schema-scalar-confidence-optional-claim-shape": "scalar-confidence-optional-claim-shape",
} satisfies Readonly<Record<string, StructuredOutputFailureDetail>>);
const MODEL_ANALYSIS_FAILURE_CODES = Object.freeze({
  "runtime-integrity": new Set([
    "studio-model-artifact-digest-mismatch",
    "studio-model-artifact-missing",
    "studio-model-artifact-path-outside-manifest",
    "studio-model-artifact-permission-mismatch",
    "studio-model-artifact-registry-invalid",
    "studio-model-artifact-unapproved",
    "studio-model-runtime-manifest-incomplete",
    "studio-runtime-artifact-unapproved",
    "studio-runtime-derivation-receipt-invalid",
    "studio-runtime-manifest-invalid",
    "studio-runtime-model-binding-mismatch",
    "studio-runtime-module-origin-invalid",
    "studio-runtime-offline-environment-required",
    "studio-runtime-package-inventory-invalid",
    "studio-runtime-package-origin-invalid",
    "studio-runtime-package-version-mismatch",
    "studio-runtime-python-mismatch",
    "studio-runtime-sidecar-mismatch",
    "studio-runtime-tree-mismatch",
    "studio-vision-runtime-origin-invalid",
    "studio-vision-runtime-version-mismatch",
  ]),
  "media-extraction": new Set([
    "studio-technical-dependency-unavailable",
    "studio-technical-frame-decode-failed",
    "studio-technical-scene-detection-failed",
    "studio-model-worker-invalid-job",
    "studio-vision-context-binding-mismatch",
    "studio-vision-frame-byte-limit",
    "studio-vision-frame-digest-mismatch",
    "studio-vision-frame-extraction-failed",
    "studio-vision-frame-invalid",
    "studio-vision-frame-limit",
    "studio-vision-frame-size-limit",
    "studio-vision-input-invalid",
    "studio-vision-job-count-invalid",
    "studio-vision-media-invalid",
    "studio-vision-media-unavailable",
    "studio-vision-source-digest-mismatch",
    "studio-vision-transcript-limit",
  ]),
  "model-execution-dependency": new Set(["studio-vision-dependency-unavailable"]),
  "model-execution-load": new Set(["studio-vision-model-unavailable"]),
  "model-execution-contract": new Set(["studio-vision-model-invalid"]),
  "model-execution-generation": new Set(["studio-vision-model-failed"]),
  "model-timeout": new Set(["studio-vision-model-timeout"]),
  "structured-output-json": new Set([
    "studio-vision-output-invalid-json",
  ]),
  "structured-output-number": new Set([
    "studio-vision-output-invalid-number",
  ]),
  "structured-output-constraint": new Set([
    "studio-vision-structured-output-unavailable",
  ]),
  "structured-output-schema": new Set([
    "studio-vision-output-schema-invalid",
    ...Object.keys(STRUCTURED_OUTPUT_FAILURE_DETAILS),
  ]),
  "structured-output-size": new Set([
    "studio-vision-output-too-large",
  ]),
} as const);
type ModelAnalysisFailureCategory = keyof typeof MODEL_ANALYSIS_FAILURE_CODES | "unknown";
type SafeModelAnalysisFailureCategory = Exclude<ModelAnalysisFailureCategory, "unknown">;

export class PinnedWorkerRuntimeProbeAnalysisFailureError extends Error {
  readonly category: SafeModelAnalysisFailureCategory;
  readonly stageTelemetry: StudioWorkerStageTelemetrySummaryV1;
  readonly structuredOutputFailureDetail: StructuredOutputFailureDetail | undefined;

  constructor(
    category: SafeModelAnalysisFailureCategory,
    stageTelemetry: StudioWorkerStageTelemetrySummaryV1,
    structuredOutputFailureDetail?: StructuredOutputFailureDetail,
  ) {
    super(`pinned-worker-runtime-probe-model-analysis-${category}`);
    this.name = "PinnedWorkerRuntimeProbeAnalysisFailureError";
    this.category = category;
    this.stageTelemetry = stageTelemetry;
    this.structuredOutputFailureDetail = structuredOutputFailureDetail;
  }
}

type ProbeIdentity = Readonly<{ id: string; version: string; sha256: string }>;
type ProbeRuntimeIdentity = Readonly<{ id: string; version: string; treeSha256: string }>;
export type PinnedWorkerRuntimeProbeBindings = Readonly<{
  runtime: ProbeRuntimeIdentity;
  model: ProbeIdentity;
}>;
type PrivatePublishDependencies = Readonly<{
  beforeLink?: () => void | Promise<void>;
  afterLink?: () => void | Promise<void>;
  unlinkFile?: typeof unlink;
}>;

export type BuildPinnedWorkerRuntimeProbeReportInput = Readonly<{
  runtime: ProbeRuntimeIdentity;
  model: ProbeIdentity;
  job: ModelAnalysisJobV1;
  receipt: ModelAnalysisReceiptV1;
  inputSha256: string;
  maximumResidentSetBytes: number;
  syntheticInput: boolean;
  customerContentUsed: boolean;
  realModel: boolean;
  formalWorkerPath: boolean;
  networkPolicyEnforced: boolean;
  networkConnectionObserved: boolean;
  publishedClaimed: boolean;
}>;

type SafeObservation = Readonly<{
  kind: typeof ALLOWED_OBSERVATION_KINDS[number];
  startMs: number;
  endMs: number;
  confidence: number;
  status: "pending-human-confirmation";
}>;

export function buildPinnedWorkerRuntimeProbeReport(
  input: BuildPinnedWorkerRuntimeProbeReportInput,
): StudioWorkerRuntimeProbeReceiptV1 {
  const runtime = parseRuntimeIdentity(input?.runtime);
  const model = parseModelIdentity(input?.model);
  if (!Number.isSafeInteger(input?.maximumResidentSetBytes) || input.maximumResidentSetBytes < 0) {
    throw new Error("pinned-worker-runtime-probe-metric-invalid");
  }
  if (input?.syntheticInput !== true) {
    throw new Error("pinned-worker-runtime-probe-synthetic-input-required");
  }
  if (input?.customerContentUsed !== false) {
    throw new Error("pinned-worker-runtime-probe-customer-content-forbidden");
  }
  if (input?.realModel !== true) {
    throw new Error("pinned-worker-runtime-probe-real-model-required");
  }
  if (input?.formalWorkerPath !== true) {
    throw new Error("pinned-worker-runtime-probe-formal-worker-required");
  }
  if (input?.networkPolicyEnforced !== true || input?.networkConnectionObserved !== false) {
    throw new Error("pinned-worker-runtime-probe-network-policy-required");
  }
  if (input?.publishedClaimed !== false) {
    throw new Error("pinned-worker-runtime-probe-publish-claim-forbidden");
  }
  if (!DIGEST.test(input?.inputSha256) || input.inputSha256 !== FORMAL_QWEN_PROBE_INPUT_SHA256) {
    throw new Error("pinned-worker-runtime-probe-binding-mismatch");
  }

  let job: ModelAnalysisJobV1;
  let receipt: ModelAnalysisReceiptV1;
  try {
    job = ModelAnalysisJobV1Schema.parse(input.job);
  } catch {
    throw new Error("pinned-worker-runtime-probe-job-schema-invalid");
  }
  try {
    receipt = ModelAnalysisReceiptV1Schema.parse(input.receipt);
  } catch {
    throw new Error("pinned-worker-runtime-probe-receipt-schema-invalid");
  }
  try {
    assertReceiptMatchesJob(job, receipt);
  } catch {
    throw new Error("pinned-worker-runtime-probe-receipt-binding-invalid");
  }
  if (!Number.isSafeInteger(receipt.runtimeMs) || receipt.runtimeMs < 0) {
    throw new Error("pinned-worker-runtime-probe-receipt-invalid");
  }
  if (receipt.status !== "succeeded" || receipt.failure !== null) {
    const category = classifyModelAnalysisFailure(receipt.failure?.code);
    throw new Error(`pinned-worker-runtime-probe-model-analysis-${category}`);
  }
  if (
    receipt.layer !== "vision"
    || receipt.synthetic !== false
    || receipt.providerMode !== "local"
  ) throw new Error("pinned-worker-runtime-probe-receipt-semantic-invalid");
  if (receipt.promptVersion !== PROMPT_VERSION) {
    throw new Error("pinned-worker-runtime-probe-prompt-binding-mismatch");
  }
  if (
    receipt.sourceDigest !== input.inputSha256
    || receipt.inputDigest !== sha256(canonicalJson(job))
    || receipt.inputBinding?.sourceDigest !== input.inputSha256
    || receipt.inputBinding.sourceStartMs !== job.sourceStartMs
    || receipt.inputBinding.sourceEndMs !== job.sourceEndMs
  ) throw new Error("pinned-worker-runtime-probe-input-binding-mismatch");
  const adapter = receipt.artifactBinding?.adapter;
  const boundModel = receipt.artifactBinding?.model;
  if (
    adapter?.id !== runtime.id
    || adapter.version !== runtime.version
    || adapter.sha256 !== runtime.treeSha256
    || boundModel?.id !== model.id
    || boundModel.version !== model.version
    || boundModel.sha256 !== model.sha256
    || receipt.modelName !== model.id
    || receipt.modelVersion !== model.version
    || receipt.checkpointDigest !== model.sha256
    || receipt.runtimeMetrics?.runtimeMs !== receipt.runtimeMs
  ) throw new Error("pinned-worker-runtime-probe-artifact-binding-mismatch");

  const observations = parseSafeObservations(receipt, job);
  const observationKinds = Object.freeze(ALLOWED_OBSERVATION_KINDS.filter((kind) =>
    observations.some((observation) => observation.kind === kind)));
  const safeProjection = Object.freeze({
    adapter: Object.freeze({ id: runtime.id, version: runtime.version, sha256: runtime.treeSha256 }),
    confidence: receipt.confidence,
    input: Object.freeze({
      digest: receipt.inputDigest,
      sourceDigest: receipt.sourceDigest,
      sourceStartMs: receipt.sourceStartMs,
      sourceEndMs: receipt.sourceEndMs,
    }),
    layer: receipt.layer,
    model: Object.freeze({ id: model.id, version: model.version, sha256: model.sha256 }),
    observationCount: observations.length,
    observations,
    promptVersion: receipt.promptVersion,
    providerMode: receipt.providerMode,
    status: receipt.status,
    suggestedTagCount: receipt.suggestedTags.length,
    warningCount: receipt.warnings.length,
  });
  const parsedReport = StudioWorkerRuntimeProbeReceiptV1Schema.parse({
    schemaVersion: 1,
    runtime,
    model,
    promptVersion: receipt.promptVersion,
    inputSha256: input.inputSha256,
    outputSha256: sha256(canonicalJson(safeProjection)),
    observationCount: observations.length,
    observationKinds,
    runtimeMs: receipt.runtimeMs,
    maximumResidentSetBytes: input.maximumResidentSetBytes,
    syntheticInput: true,
    customerContentUsed: false,
    realModel: true,
    formalWorkerPath: true,
    // The OS deny-network policy was verified active and this successful harness established
    // no connection. This does not claim that a child network attempt was observed.
    networkDenied: true,
    providerMode: "local",
    publishedClaimed: false,
  });
  const report = parseFormalWorkerRuntimeProbeReceipt(parsedReport);
  if (report === null) throw new Error("pinned-worker-runtime-probe-summary-invalid");
  Object.freeze(report.runtime);
  Object.freeze(report.model);
  Object.freeze(report.observationKinds);
  return Object.freeze(report);
}

function classifyModelAnalysisFailure(code: unknown): ModelAnalysisFailureCategory {
  if (typeof code !== "string") return "unknown";
  for (const [category, codes] of Object.entries(MODEL_ANALYSIS_FAILURE_CODES)) {
    if (codes.has(code)) return category as ModelAnalysisFailureCategory;
  }
  return "unknown";
}

function classifyStructuredOutputFailureDetail(
  code: unknown,
): StructuredOutputFailureDetail | undefined {
  if (
    typeof code !== "string"
    || !Object.prototype.hasOwnProperty.call(STRUCTURED_OUTPUT_FAILURE_DETAILS, code)
  ) return undefined;
  return STRUCTURED_OUTPUT_FAILURE_DETAILS[
    code as keyof typeof STRUCTURED_OUTPUT_FAILURE_DETAILS
  ];
}

export function canonicalPinnedWorkerRuntimeProbeReceipt(
  input: StudioWorkerRuntimeProbeReceiptV1,
): string {
  const report = parseSanitizedReport(input);
  return `${canonicalJson(report)}\n`;
}

export async function installPinnedWorkerRuntimeProbeReceipt(input: Readonly<{
  sourcePath: string;
  modelRoot: string;
}>, dependencies: PrivatePublishDependencies = {}): Promise<StudioWorkerRuntimeProbeBindingV1> {
  if (
    typeof input?.sourcePath !== "string"
    || !input.sourcePath.trim()
    || typeof input?.modelRoot !== "string"
    || !isAbsolute(input.modelRoot)
  ) throw new Error("pinned-worker-runtime-probe-install-arguments-invalid");
  const uid = currentUserId();
  const source = await readStablePrivateSource(resolve(input.sourcePath), uid);
  try {
    let report: StudioWorkerRuntimeProbeReceiptV1;
    try {
      report = parseSanitizedReport(JSON.parse(source.bytes.toString("utf8")));
    } catch {
      throw new Error("pinned-worker-runtime-probe-install-receipt-invalid");
    }
    const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(report);
    if (!source.bytes.equals(Buffer.from(canonical))) {
      throw new Error("pinned-worker-runtime-probe-install-receipt-noncanonical");
    }
    const sourceSha256 = sha256(source.bytes);
    if (!safeIdentity(report.runtime.id) || !DIGEST.test(sourceSha256)) {
      throw new Error("pinned-worker-runtime-probe-install-filename-unsafe");
    }
    const modelRoot = await inspectPrivateDirectory(input.modelRoot, uid, false);
    const runtimeRoot = await inspectPrivateDirectory(join(modelRoot.path, "runtime"), uid, true);
    const probesRoot = await inspectPrivateDirectory(join(runtimeRoot.path, "probes"), uid, true);
    const destination = join(probesRoot.path, `${report.runtime.id}.json`);
    await source.revalidate();
    await publishPrivateNoClobberFile({
      parent: probesRoot,
      destination,
      bytes: source.bytes,
      uid,
      allowExactExisting: true,
      errorPrefix: "pinned-worker-runtime-probe-install",
      dependencies,
      validateBeforeLink: source.revalidate,
    });
    await source.revalidate();
    await assertDirectoryStable(modelRoot, uid);
    await assertDirectoryStable(runtimeRoot, uid);
    await assertDirectoryStable(probesRoot, uid);
    return probeBinding(report, sourceSha256);
  } finally {
    await source.close();
  }
}

type RunProbeOptions = Readonly<{
  lockPath: string;
  modelRoot: string;
  runtimeId: string;
}>;

export async function runPinnedWorkerRuntimeProbe(
  options: RunProbeOptions,
): Promise<StudioWorkerRuntimeProbeReceiptV1> {
  const bindings = await resolvePinnedWorkerRuntimeProbeBindings(options);
  const runtime = bindings.runtime;
  const qwen = bindings.model;

  const syntheticRoot = await mkdtemp(join(tmpdir(), "pinned-worker-runtime-probe-"));
  await chmod(syntheticRoot, 0o700);
  try {
    const media = await generatePinnedWorkerRuntimeProbeMedia(syntheticRoot);
    const worker = createPinnedWorkerRuntimeProbeWorker(syntheticRoot, {
      lockPath: resolve(options.lockPath),
      modelRoot: resolve(options.modelRoot),
      runtimeId: options.runtimeId,
    });
    const job = ModelAnalysisJobV1Schema.parse({
      schemaVersion: 1,
      jobId: "formal-synthetic-qwen-probe",
      customerId: "synthetic-probe-customer",
      sessionId: "synthetic-probe-session",
      sourceAssetId: "synthetic-programmatic-workbench",
      sourceDigest: media.sha256,
      sourcePath: media.path,
      sourceStartMs: 0,
      sourceEndMs: media.durationMs,
      layer: "vision",
      operationKey: "formal-synthetic-qwen-probe-v1",
      requestedAt: "2026-09-23T00:00:00.000Z",
    });
    const trustedContext: StudioTrustedWorkerContext = Object.freeze({
      customerId: job.customerId,
      sessionId: job.sessionId,
      sourceAssetId: job.sourceAssetId,
      sourceDigest: job.sourceDigest,
      sourceStartMs: job.sourceStartMs,
      sourceEndMs: job.sourceEndMs,
      transcriptSpans: Object.freeze([]),
      demand: null,
    });
    let receipt: ModelAnalysisReceiptV1;
    try {
      receipt = await worker.run(
        job,
        AbortSignal.timeout(MODEL_WORKER_OUTER_TIMEOUT_MS),
        trustedContext,
      );
    } catch (error) {
      if (error instanceof StudioModelWorkerFailureError) {
        const failureCode = error.receipt.failure?.code;
        const category = classifyModelAnalysisFailure(failureCode);
        if (category !== "unknown") {
          throw new PinnedWorkerRuntimeProbeAnalysisFailureError(
            category,
            error.stageTelemetry,
            classifyStructuredOutputFailureDetail(failureCode),
          );
        }
        throw new Error("pinned-worker-runtime-probe-model-analysis-unknown");
      }
      throw error;
    }
    const maximumResidentSetBytes = process.resourceUsage().maxRSS * 1024;
    return buildPinnedWorkerRuntimeProbeReport({
      runtime,
      model: qwen,
      job,
      receipt,
      inputSha256: media.sha256,
      maximumResidentSetBytes,
      syntheticInput: true,
      customerContentUsed: false,
      realModel: true,
      formalWorkerPath: true,
      networkPolicyEnforced: true,
      networkConnectionObserved: false,
      publishedClaimed: false,
    });
  } finally {
    await rm(syntheticRoot, { recursive: true, force: true });
  }
}

export async function resolvePinnedWorkerRuntimeProbeBindings(
  options: Pick<RunProbeOptions, "lockPath" | "runtimeId">,
): Promise<PinnedWorkerRuntimeProbeBindings> {
  const lock = await loadModelLock(options.lockPath);
  const runtime = findWorkerRuntime(lock, options.runtimeId);
  const qwen = findArtifact(lock, QWEN_ID);
  const qwenFiles = artifactManifest(qwen).filter((file: Record<string, unknown>) =>
    file.relativePath === `${QWEN_ID}/model.safetensors`);
  if (
    !safeIdentity(runtime?.id)
    || !safeIdentity(runtime?.version)
    || !DIGEST.test(runtime?.treeSha256)
    || qwen?.id !== QWEN_ID
    || !safeIdentity(qwen?.version)
    || qwenFiles.length !== 1
    || !DIGEST.test(qwenFiles[0]?.sha256)
  ) throw new Error("pinned-worker-runtime-probe-binding-invalid");
  return Object.freeze({
    runtime: Object.freeze({
      id: runtime.id,
      version: runtime.version,
      treeSha256: runtime.treeSha256,
    }),
    model: Object.freeze({
      id: qwen.id,
      version: qwen.version,
      sha256: qwenFiles[0].sha256,
    }),
  });
}

export function createPinnedWorkerRuntimeProbeWorker(
  syntheticRoot: string,
  dependencies: DefaultStudioModelWorkerDependencies = {},
): LocalProcessStudioModelWorker {
  return new LocalProcessStudioModelWorker(createDefaultStudioModelWorkerConfig(syntheticRoot, {
    ...dependencies,
    allowProbeRequired: true,
    enableProbeStageTelemetry: true,
  }));
}

function parseSafeObservations(receipt: ModelAnalysisReceiptV1, job: ModelAnalysisJobV1): readonly SafeObservation[] {
  const value = receipt.qualitySignals.visualObservations;
  if (!Array.isArray(value) || value.length === 0 || value.length > 64) {
    throw new Error("pinned-worker-runtime-probe-observation-invalid");
  }
  const observations = value.map((item): SafeObservation => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("pinned-worker-runtime-probe-observation-invalid");
    }
    const row = item as Record<string, unknown>;
    const keys = Object.keys(row).sort().join("\0");
    if (
      keys !== ["confidence", "endMs", "kind", "startMs", "status", "text"].sort().join("\0")
      || !ALLOWED_OBSERVATION_KIND_SET.has(row.kind as string)
      || typeof row.text !== "string"
      || !row.text.trim()
      || !Number.isSafeInteger(row.startMs)
      || !Number.isSafeInteger(row.endMs)
      || (row.startMs as number) < job.sourceStartMs
      || (row.endMs as number) > job.sourceEndMs
      || (row.endMs as number) <= (row.startMs as number)
      || typeof row.confidence !== "number"
      || !Number.isFinite(row.confidence)
      || row.confidence < 0
      || row.confidence > 1
      || row.status !== "pending-human-confirmation"
    ) throw new Error("pinned-worker-runtime-probe-observation-invalid");
    return Object.freeze({
      kind: row.kind as SafeObservation["kind"],
      startMs: row.startMs as number,
      endMs: row.endMs as number,
      confidence: row.confidence,
      status: "pending-human-confirmation",
    });
  });
  return Object.freeze(observations);
}

function parseRuntimeIdentity(value: ProbeRuntimeIdentity): ProbeRuntimeIdentity {
  if (
    !exactKeys(value, ["id", "version", "treeSha256"])
    || !safeIdentity(value.id)
    || !safeIdentity(value.version)
    || !DIGEST.test(value.treeSha256)
  ) throw new Error("pinned-worker-runtime-probe-binding-mismatch");
  return Object.freeze({ ...value });
}

function parseModelIdentity(value: ProbeIdentity): ProbeIdentity {
  if (
    !exactKeys(value, ["id", "version", "sha256"])
    || !safeIdentity(value.id)
    || !safeIdentity(value.version)
    || !DIGEST.test(value.sha256)
  ) throw new Error("pinned-worker-runtime-probe-binding-mismatch");
  return Object.freeze({ ...value });
}

function parseSanitizedReport(value: unknown): StudioWorkerRuntimeProbeReceiptV1 {
  let report: StudioWorkerRuntimeProbeReceiptV1;
  try {
    const parsed = parseFormalWorkerRuntimeProbeReceipt(value);
    if (parsed === null) throw new Error();
    report = StudioWorkerRuntimeProbeReceiptV1Schema.parse(parsed);
  } catch {
    throw new Error("pinned-worker-runtime-probe-install-receipt-invalid");
  }
  return report;
}

function probeBinding(
  report: StudioWorkerRuntimeProbeReceiptV1,
  receiptSha256: string,
): StudioWorkerRuntimeProbeBindingV1 {
  return Object.freeze({
    receiptPath: `runtime/probes/${report.runtime.id}.json`,
    receiptSha256,
    runtimeTreeSha256: report.runtime.treeSha256,
    modelSha256: report.model.sha256,
    promptVersion: report.promptVersion,
    inputSha256: report.inputSha256,
  });
}

export async function generatePinnedWorkerRuntimeProbeMedia(root: string): Promise<Readonly<{
  path: string;
  sha256: string;
  durationMs: number;
}>> {
  if (typeof ffmpegPath !== "string" || !isAbsolute(ffmpegPath)) {
    throw new Error("pinned-worker-runtime-probe-media-tools-unavailable");
  }
  const path = join(root, "synthetic-workbench.mp4");
  const filters = [
    "drawbox=x=0:y=265:w=640:h=95:color=0x6b4f35:t=fill",
    "drawbox=x=78:y=105:w=190:h=88:color=0xf2c9a0:t=fill",
    "drawbox=x=225:y=112:w=34:h=70:color=0xf2c9a0:t=fill",
    "drawbox=x=138:y=124:w=52:h=52:color=0xd4af37:t=8",
    "drawbox=x=382:y=72:w=22:h=175:color=0x59636e:t=fill",
    "drawbox=x=345:y=70:w=96:h=24:color=0x374151:t=fill",
    "drawbox=x=470:y=190:w=92:h=64:color=0x2563eb:t=fill",
  ].join(",");
  try {
    await execFileAsync(ffmpegPath, [
      "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=0xf5f1e8:s=640x360:r=4:d=2",
      "-vf", filters, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryslow",
      "-crf", "18", "-g", "8", "-keyint_min", "8", "-sc_threshold", "0",
      "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-y", path,
    ], { timeout: 30_000, maxBuffer: 64 * 1024 });
  } catch {
    throw new Error("pinned-worker-runtime-probe-media-generation-failed");
  }
  return Object.freeze({
    path: await realpath(path),
    sha256: sha256(await readFile(path)),
    durationMs: 2_000,
  });
}

type PrivateDirectory = Readonly<{ path: string; dev: bigint; ino: bigint }>;

async function inspectPrivateDirectory(path: string, uid: bigint, create: boolean): Promise<PrivateDirectory> {
  if (create) {
    try {
      await mkdir(path, { mode: 0o700 });
    } catch (error) {
      if (!hasCode(error, "EEXIST")) throw error;
    }
  }
  const requested = await lstat(path, { bigint: true }).catch(() => null);
  if (
    requested === null
    || requested.isSymbolicLink()
    || !requested.isDirectory()
    || requested.uid !== uid
    || (requested.mode & 0o7777n) !== 0o700n
  ) throw new Error("pinned-worker-runtime-probe-install-directory-unsafe");
  const canonical = await realpath(path);
  const details = await lstat(canonical, { bigint: true });
  if (details.dev !== requested.dev || details.ino !== requested.ino) {
    throw new Error("pinned-worker-runtime-probe-install-directory-unsafe");
  }
  return Object.freeze({ path: canonical, dev: details.dev, ino: details.ino });
}

async function assertDirectoryStable(directory: PrivateDirectory, uid: bigint): Promise<void> {
  const details = await lstat(directory.path, { bigint: true }).catch(() => null);
  if (
    details === null
    || details.isSymbolicLink()
    || !details.isDirectory()
    || details.dev !== directory.dev
    || details.ino !== directory.ino
    || details.uid !== uid
    || (details.mode & 0o7777n) !== 0o700n
  ) throw new Error("pinned-worker-runtime-probe-install-directory-changed");
}

async function readStablePrivateSource(path: string, uid: bigint): Promise<Readonly<{
  bytes: Buffer;
  revalidate(): Promise<void>;
  close(): Promise<void>;
}>> {
  let handle: FileHandle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch {
    throw new Error("pinned-worker-runtime-probe-install-source-unsafe");
  }
  try {
    const before = await handle.stat({ bigint: true });
    if (
      !before.isFile()
      || before.nlink !== 1n
      || before.uid !== uid
      || (before.mode & 0o7777n) !== 0o600n
      || before.size <= 0n
      || before.size > BigInt(MAX_RECEIPT_BYTES)
    ) throw new Error("pinned-worker-runtime-probe-install-source-unsafe");
    const bytes = Buffer.alloc(Number(before.size));
    let offset = 0;
    while (offset < bytes.length) {
      const result = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (result.bytesRead === 0) throw new Error("pinned-worker-runtime-probe-install-source-changed");
      offset += result.bytesRead;
    }
    const after = await handle.stat({ bigint: true });
    if (!sameStableFile(before, after)) {
      throw new Error("pinned-worker-runtime-probe-install-source-changed");
    }
    const identity = {
      dev: before.dev,
      ino: before.ino,
      size: before.size,
      mtimeNs: before.mtimeNs,
      ctimeNs: before.ctimeNs,
    };
    let closed = false;
    return Object.freeze({
      bytes,
      async revalidate() {
        const opened = await handle.stat({ bigint: true }).catch(() => null);
        const current = await lstat(path, { bigint: true }).catch(() => null);
        if (
          opened === null
          || !sameStableFile(before, opened)
          || current === null
          || current.dev !== identity.dev
          || current.ino !== identity.ino
          || current.size !== identity.size
          || current.mtimeNs !== identity.mtimeNs
          || current.ctimeNs !== identity.ctimeNs
          || current.nlink !== 1n
          || current.uid !== uid
          || (current.mode & 0o7777n) !== 0o600n
        ) throw new Error("pinned-worker-runtime-probe-install-source-changed");
      },
      async close() {
        if (closed) return;
        closed = true;
        await handle.close();
      },
    });
  } catch (error) {
    await handle.close();
    throw error;
  }
}

async function exactPrivateFile(
  path: string,
  expected: Buffer,
  uid: bigint,
  expectedLinks: bigint,
): Promise<BigIntStats | null> {
  let handle: FileHandle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch {
    return null;
  }
  try {
    const before = await handle.stat({ bigint: true });
    if (
      !before.isFile()
      || before.nlink !== expectedLinks
      || before.uid !== uid
      || (before.mode & 0o7777n) !== 0o600n
      || before.size !== BigInt(expected.byteLength)
    ) return null;
    const observed = Buffer.alloc(expected.byteLength);
    let offset = 0;
    while (offset < observed.length) {
      const result = await handle.read(observed, offset, observed.length - offset, offset);
      if (result.bytesRead === 0) return null;
      offset += result.bytesRead;
    }
    const after = await handle.stat({ bigint: true });
    const pathname = await lstat(path, { bigint: true }).catch(() => null);
    return pathname !== null
      && !pathname.isSymbolicLink()
      && sameStableFile(before, after)
      && sameStableFile(after, pathname)
      && observed.equals(expected)
      ? after
      : null;
  } finally {
    await handle.close();
  }
}

async function exactInstalledReceipt(path: string, expected: Buffer, uid: bigint): Promise<boolean> {
  return await exactPrivateFile(path, expected, uid, 1n) !== null;
}

async function unlinkOwnedFile(
  path: string,
  identity: Readonly<{ dev: bigint; ino: bigint }>,
  unlinkFile: typeof unlink = unlink,
): Promise<"absent" | "foreign" | "removed"> {
  const current = await lstat(path, { bigint: true }).catch(() => null);
  if (current === null) return "absent";
  if (current.dev !== identity.dev || current.ino !== identity.ino) return "foreign";
  try {
    await unlinkFile(path);
    return "removed";
  } catch (error) {
    if (hasCode(error, "ENOENT")) return "absent";
    throw error;
  }
}

async function syncPrivateDirectory(path: string): Promise<void> {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

type PrivatePublicationInput = Readonly<{
  parent: PrivateDirectory;
  destination: string;
  bytes: Buffer;
  uid: bigint;
  allowExactExisting: boolean;
  errorPrefix: string;
  dependencies: PrivatePublishDependencies;
  validateBeforeLink?: () => Promise<void>;
}>;

async function recoverPrivatePublication(
  input: PrivatePublicationInput,
  excludedTemporaryName?: string,
): Promise<"none" | "invalid" | "recovered"> {
  await assertDirectoryStable(input.parent, input.uid);
  const destinationName = basename(input.destination);
  const temporaryPrefix = `.${destinationName}.tmp-`;
  const entries = await readdir(input.parent.path, { withFileTypes: true });
  const possibleCandidates = entries.filter((entry) => (
    entry.name !== excludedTemporaryName
    && entry.name.startsWith(temporaryPrefix)
  ));
  await assertDirectoryStable(input.parent, input.uid);
  if (possibleCandidates.some((entry) => (
    !UUID_V4.test(entry.name.slice(temporaryPrefix.length))
  ))) return "invalid";
  const candidates = possibleCandidates;
  if (candidates.length === 0) return "none";
  if (candidates.length !== 1) return "invalid";
  const temporary = join(input.parent.path, candidates[0]!.name);
  const destinationDetails = await exactPrivateFile(
    input.destination,
    input.bytes,
    input.uid,
    2n,
  );
  const temporaryDetails = await exactPrivateFile(temporary, input.bytes, input.uid, 2n);
  if (
    destinationDetails === null
    || temporaryDetails === null
    || destinationDetails.dev !== temporaryDetails.dev
    || destinationDetails.ino !== temporaryDetails.ino
  ) return "invalid";
  let removed: "absent" | "foreign" | "removed";
  try {
    removed = await unlinkOwnedFile(temporary, destinationDetails, input.dependencies.unlinkFile);
  } catch {
    throw new Error(`${input.errorPrefix}-temporary-cleanup-failed`);
  }
  if (removed === "foreign") return "invalid";
  await syncPrivateDirectory(input.parent.path);
  const remaining = await lstatIfPresent(temporary);
  const finalDestination = await exactPrivateFile(input.destination, input.bytes, input.uid, 1n);
  await assertDirectoryStable(input.parent, input.uid);
  if (remaining !== null || finalDestination === null) return "invalid";
  return "recovered";
}

async function finalizeOwnedPublication(
  input: PrivatePublicationInput,
  temporary: string,
  identity: Readonly<{ dev: bigint; ino: bigint }>,
): Promise<boolean> {
  const temporaryDetails = await exactPrivateFile(temporary, input.bytes, input.uid, 2n);
  const destinationDetails = await exactPrivateFile(input.destination, input.bytes, input.uid, 2n);
  if (
    temporaryDetails !== null
    && destinationDetails !== null
    && temporaryDetails.dev === identity.dev
    && temporaryDetails.ino === identity.ino
    && destinationDetails.dev === identity.dev
    && destinationDetails.ino === identity.ino
  ) {
    let removed: "absent" | "foreign" | "removed";
    try {
      removed = await unlinkOwnedFile(temporary, identity, input.dependencies.unlinkFile);
    } catch {
      throw new Error(`${input.errorPrefix}-temporary-cleanup-failed`);
    }
    if (removed === "foreign") return false;
    await syncPrivateDirectory(input.parent.path);
  }
  const remaining = await lstatIfPresent(temporary);
  const finalDestination = await exactPrivateFile(input.destination, input.bytes, input.uid, 1n);
  await assertDirectoryStable(input.parent, input.uid);
  return remaining === null
    && finalDestination !== null
    && finalDestination.dev === identity.dev
    && finalDestination.ino === identity.ino;
}

async function publishPrivateNoClobberFile(input: PrivatePublicationInput): Promise<void> {
  const conflictCode = input.allowExactExisting
    ? `${input.errorPrefix}-destination-conflict`
    : `${input.errorPrefix}-exists`;
  let temporary = "";
  let temporaryIdentity: Readonly<{ dev: bigint; ino: bigint }> | null = null;
  let handle: FileHandle | null = null;
  let linked = false;
  try {
    const initialRecovery = await recoverPrivatePublication(input);
    if (initialRecovery === "recovered") return;
    if (initialRecovery === "invalid") throw new Error(conflictCode);
    if (await lstatIfPresent(input.destination) !== null) {
      if (input.allowExactExisting
        && await exactInstalledReceipt(input.destination, input.bytes, input.uid)) return;
      throw new Error(conflictCode);
    }

    temporary = join(input.parent.path, `.${basename(input.destination)}.tmp-${randomUUID()}`);
    handle = await open(
      temporary,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    await handle.writeFile(input.bytes);
    await handle.sync();
    const details = await handle.stat({ bigint: true });
    if (
      !details.isFile()
      || details.nlink !== 1n
      || details.uid !== input.uid
      || (details.mode & 0o7777n) !== 0o600n
      || details.size !== BigInt(input.bytes.byteLength)
    ) throw new Error(`${input.errorPrefix}-temporary-unsafe`);
    temporaryIdentity = { dev: details.dev, ino: details.ino };
    await handle.close();
    handle = null;
    await assertDirectoryStable(input.parent, input.uid);
    await input.dependencies.beforeLink?.();
    await input.validateBeforeLink?.();
    try {
      await link(temporary, input.destination);
      linked = true;
    } catch (error) {
      if (!hasCode(error, "EEXIST")) throw error;
      const racedRecovery = await recoverPrivatePublication(input, basename(temporary));
      if (racedRecovery === "recovered") return;
      if (
        racedRecovery === "none"
        && input.allowExactExisting
        && await exactInstalledReceipt(input.destination, input.bytes, input.uid)
      ) return;
      throw new Error(conflictCode);
    }
    await input.dependencies.afterLink?.();
    if (await finalizeOwnedPublication(input, temporary, temporaryIdentity)) {
      temporaryIdentity = null;
      return;
    }
    throw new Error(`${input.errorPrefix}-promotion-failed`);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith(`${input.errorPrefix}-`)) throw error;
    throw new Error(`${input.errorPrefix}-failed`);
  } finally {
    await handle?.close().catch(() => undefined);
    if (temporaryIdentity !== null && !linked) {
      let removed: "absent" | "foreign" | "removed";
      try {
        removed = await unlinkOwnedFile(temporary, temporaryIdentity, input.dependencies.unlinkFile);
      } catch {
        throw new Error(`${input.errorPrefix}-temporary-cleanup-failed`);
      }
      if (removed === "foreign") {
        throw new Error(`${input.errorPrefix}-temporary-cleanup-failed`);
      }
      await syncPrivateDirectory(input.parent.path);
    }
  }
}

function sameStableFile(left: BigIntStats, right: BigIntStats): boolean {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.uid === right.uid
    && left.nlink === right.nlink
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function currentUserId(): bigint {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid?.();
  if (!Number.isSafeInteger(uid) || uid === undefined || uid < 0) {
    throw new Error("pinned-worker-runtime-probe-install-user-invalid");
  }
  return BigInt(uid);
}

function lstatIfPresent(path: string) {
  return lstat(path, { bigint: true }).catch((error: unknown) => {
    if (hasCode(error, "ENOENT")) return null;
    throw error;
  });
}

function exactKeys<T extends object>(value: T, keys: readonly string[]): boolean {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0"));
}

function canonicalJson(value: unknown): string {
  return canonicalWorkerRuntimeProbeJson(value) as string;
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function safeIdentity(value: unknown): value is string {
  return typeof value === "string"
    && value !== "."
    && value !== ".."
    && SAFE_IDENTITY.test(value);
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error
    && (error as NodeJS.ErrnoException).code === code);
}

function validOutputPathSyntax(path: unknown): path is string {
  return typeof path === "string"
    && isAbsolute(path)
    && resolve(path) === path
    && safeIdentity(basename(path));
}

type CliOptions = Readonly<{
  lockPath: string;
  modelRoot: string;
  runtimeId: string;
  outputPath: string | null;
  diagnosticOutputPath: string | null;
  sourcePath: string | null;
  installReceipt: boolean;
}>;

function parseArguments(arguments_: readonly string[]): CliOptions {
  const options = {
    lockPath: join(repositoryRoot, "models.lock.json"),
    modelRoot: join(homedir(), ".local", "share", "wearable-studio-models"),
    runtimeId: DEFAULT_RUNTIME_ID,
    outputPath: null as string | null,
    diagnosticOutputPath: null as string | null,
    sourcePath: null as string | null,
    installReceipt: false,
  };
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--install-receipt") options.installReceipt = true;
    else if (["--lock", "--root", "--runtime-id", "--output", "--diagnostic-output", "--source"].includes(argument)) {
      const value = arguments_[index + 1];
      if (!value) throw new Error("pinned-worker-runtime-probe-arguments-invalid");
      if (argument === "--lock") options.lockPath = value;
      else if (argument === "--root") options.modelRoot = value;
      else if (argument === "--runtime-id") options.runtimeId = value;
      else if (argument === "--output") options.outputPath = value;
      else if (argument === "--diagnostic-output") options.diagnosticOutputPath = value;
      else options.sourcePath = value;
      index += 1;
    } else throw new Error("pinned-worker-runtime-probe-arguments-invalid");
  }
  const ordinary = !options.installReceipt
    && options.outputPath !== null
    && options.diagnosticOutputPath !== null
    && options.sourcePath === null;
  const install = options.installReceipt
    && options.sourcePath !== null
    && options.outputPath === null
    && options.diagnosticOutputPath === null;
  if (!ordinary && !install) throw new Error("pinned-worker-runtime-probe-arguments-invalid");
  if (ordinary && !validOutputPathSyntax(options.outputPath)) {
    throw new Error("pinned-worker-runtime-probe-output-arguments-invalid");
  }
  if (ordinary && !validOutputPathSyntax(options.diagnosticOutputPath)) {
    throw new Error("pinned-worker-runtime-diagnostic-output-arguments-invalid");
  }
  if (ordinary && options.outputPath === options.diagnosticOutputPath) {
    throw new Error("pinned-worker-runtime-probe-output-paths-invalid");
  }
  return Object.freeze(options);
}

async function validatePrivateOutputTarget(path: string, errorPrefix: string): Promise<void> {
  if (!validOutputPathSyntax(path)) throw new Error(`${errorPrefix}-arguments-invalid`);
  const uid = currentUserId();
  try {
    await inspectPrivateDirectory(dirname(path), uid, false);
  } catch {
    throw new Error(`${errorPrefix}-directory-unsafe`);
  }
  if (await lstatIfPresent(path) !== null) throw new Error(`${errorPrefix}-exists`);
}

export async function writePinnedWorkerRuntimeProbeOutput(
  path: string,
  report: StudioWorkerRuntimeProbeReceiptV1,
  dependencies: PrivatePublishDependencies = {},
): Promise<string> {
  if (!validOutputPathSyntax(path)) {
    throw new Error("pinned-worker-runtime-probe-output-arguments-invalid");
  }
  const uid = currentUserId();
  let parent: PrivateDirectory;
  try {
    parent = await inspectPrivateDirectory(dirname(path), uid, false);
  } catch {
    throw new Error("pinned-worker-runtime-probe-output-directory-unsafe");
  }
  const output = join(parent.path, basename(path));
  const bytes = Buffer.from(canonicalPinnedWorkerRuntimeProbeReceipt(report));
  await publishPrivateNoClobberFile({
    parent,
    destination: output,
    bytes,
    uid,
    allowExactExisting: false,
    errorPrefix: "pinned-worker-runtime-probe-output",
    dependencies,
  });
  await assertDirectoryStable(parent, uid);
  return sha256(bytes);
}

export async function writePinnedWorkerRuntimeDiagnosticOutput(
  path: string,
  diagnostic: PinnedWorkerRuntimeDiagnosticV1,
  dependencies: PrivatePublishDependencies = {},
): Promise<string> {
  if (!validOutputPathSyntax(path)) {
    throw new Error("pinned-worker-runtime-diagnostic-output-arguments-invalid");
  }
  const uid = currentUserId();
  let parent: PrivateDirectory;
  try {
    parent = await inspectPrivateDirectory(dirname(path), uid, false);
  } catch {
    throw new Error("pinned-worker-runtime-diagnostic-output-directory-unsafe");
  }
  const output = join(parent.path, basename(path));
  const bytes = Buffer.from(canonicalPinnedWorkerRuntimeDiagnostic(diagnostic));
  await publishPrivateNoClobberFile({
    parent,
    destination: output,
    bytes,
    uid,
    allowExactExisting: false,
    errorPrefix: "pinned-worker-runtime-diagnostic-output",
    dependencies,
  });
  await assertDirectoryStable(parent, uid);
  return sha256(bytes);
}

export type RunPinnedWorkerRuntimeProbeCommandOptions = Readonly<{
  lockPath: string;
  modelRoot: string;
  runtimeId: string;
  outputPath: string;
  diagnosticOutputPath: string;
}>;

type RunPinnedWorkerRuntimeProbeCommandDependencies = Readonly<{
  runProbe?: typeof runPinnedWorkerRuntimeProbe;
  resolveBindings?: typeof resolvePinnedWorkerRuntimeProbeBindings;
  writeSuccess?: typeof writePinnedWorkerRuntimeProbeOutput;
  writeDiagnostic?: typeof writePinnedWorkerRuntimeDiagnosticOutput;
  nowMs?: () => number;
}>;

export async function runPinnedWorkerRuntimeProbeCommand(
  options: RunPinnedWorkerRuntimeProbeCommandOptions,
  dependencies: RunPinnedWorkerRuntimeProbeCommandDependencies = {},
): Promise<Readonly<{ status: "succeeded"; receiptSha256: string }>> {
  if (options.outputPath === options.diagnosticOutputPath) {
    throw new Error("pinned-worker-runtime-probe-output-paths-invalid");
  }
  await validatePrivateOutputTarget(
    options.outputPath,
    "pinned-worker-runtime-probe-output",
  );
  await validatePrivateOutputTarget(
    options.diagnosticOutputPath,
    "pinned-worker-runtime-diagnostic-output",
  );

  const resolveBindings = dependencies.resolveBindings
    ?? resolvePinnedWorkerRuntimeProbeBindings;
  const bindings = await resolveBindings(options);
  const nowMs = dependencies.nowMs ?? (() => performance.now());
  const startedAtMs = nowMs();
  let report: StudioWorkerRuntimeProbeReceiptV1;
  try {
    report = await (dependencies.runProbe ?? runPinnedWorkerRuntimeProbe)(options);
  } catch (error) {
    const failure: Readonly<{
      status: "timeout" | "failed";
      category: SafeModelAnalysisFailureCategory;
      telemetry: StudioWorkerStageTelemetrySummaryV1;
      structuredOutputFailureDetail?: StructuredOutputFailureDetail;
    }> | null = error instanceof StudioModelWorkerTimeoutError
      ? error.stageTelemetry === null
        ? null
        : { status: "timeout" as const, category: "model-timeout" as const, telemetry: error.stageTelemetry }
      : error instanceof PinnedWorkerRuntimeProbeAnalysisFailureError
        ? {
            status: "failed" as const,
            category: error.category,
            telemetry: error.stageTelemetry,
            ...(error.structuredOutputFailureDetail === undefined
              ? {}
              : { structuredOutputFailureDetail: error.structuredOutputFailureDetail }),
          }
        : error instanceof StudioModelWorkerFailureError
          ? classifyModelAnalysisFailure(error.receipt.failure?.code) === "unknown"
            ? null
            : {
                status: "failed" as const,
                category: classifyModelAnalysisFailure(error.receipt.failure?.code) as SafeModelAnalysisFailureCategory,
                telemetry: error.stageTelemetry,
                ...(classifyStructuredOutputFailureDetail(error.receipt.failure?.code) === undefined
                  ? {}
                  : {
                      structuredOutputFailureDetail: classifyStructuredOutputFailureDetail(
                        error.receipt.failure?.code,
                      )!,
                    }),
              }
        : null;
    if (failure?.telemetry.lastStage !== null && failure?.telemetry.lastStage !== undefined) {
      let diagnostic: PinnedWorkerRuntimeDiagnosticV1;
      try {
        const stageRecordedMs = Object.values(failure.telemetry.completedStageMs)
          .reduce<number>((total, value) => total + (value ?? 0), failure.telemetry.activeStageElapsedMs ?? 0);
        const measuredMs = Math.max(0, Math.trunc(nowMs() - startedAtMs));
        diagnostic = buildPinnedWorkerRuntimeDiagnostic({
          schemaVersion: 1,
          status: failure.status,
          safeFailureCategory: failure.category,
          runtimeId: bindings.runtime.id,
          runtimeTreeSha256: bindings.runtime.treeSha256,
          modelId: bindings.model.id,
          modelSha256: bindings.model.sha256,
          promptVersion: PROMPT_VERSION,
          inputSha256: FORMAL_QWEN_PROBE_INPUT_SHA256,
          syntheticInput: true,
          customerContentUsed: false,
          totalElapsedMs: Math.max(measuredMs, stageRecordedMs),
          lastStage: failure.telemetry.lastStage,
          completedStageMs: failure.telemetry.completedStageMs,
          activeStageElapsedMs: failure.telemetry.activeStageElapsedMs,
          ...(failure.structuredOutputFailureDetail === undefined
            ? {}
            : { structuredOutputFailureDetail: failure.structuredOutputFailureDetail }),
        });
      } catch {
        throw error;
      }
      try {
        await (dependencies.writeDiagnostic ?? writePinnedWorkerRuntimeDiagnosticOutput)(
          options.diagnosticOutputPath,
          diagnostic,
        );
      } catch {
        throw new Error("pinned-worker-runtime-probe-diagnostic-publish-failed");
      }
    }
    throw error;
  }
  const receiptSha256 = await (
    dependencies.writeSuccess ?? writePinnedWorkerRuntimeProbeOutput
  )(options.outputPath, report);
  return Object.freeze({ status: "succeeded", receiptSha256 });
}

async function main(): Promise<void> {
  const options = parseArguments(process.argv.slice(2));
  if (options.installReceipt) {
    const binding = await installPinnedWorkerRuntimeProbeReceipt({
      sourcePath: options.sourcePath!,
      modelRoot: resolve(options.modelRoot),
    });
    process.stdout.write(`${canonicalJson(binding)}\n`);
    return;
  }
  const result = await runPinnedWorkerRuntimeProbeCommand({
    lockPath: resolve(options.lockPath),
    modelRoot: resolve(options.modelRoot),
    runtimeId: options.runtimeId,
    outputPath: options.outputPath!,
    diagnosticOutputPath: options.diagnosticOutputPath!,
  });
  process.stdout.write(`${canonicalJson(result)}\n`);
}

export function publicPinnedWorkerRuntimeProbeFailureCode(error: unknown): string {
  if (error instanceof Error && error.message === "studio-model-worker-timeout") {
    return "pinned-worker-runtime-probe-model-analysis-model-timeout";
  }
  if (
    error instanceof Error
    && /^pinned-worker-runtime-diagnostic-output-(?:arguments-invalid|directory-unsafe|exists)$/u
      .test(error.message)
  ) return error.message;
  return error instanceof Error && error.message.startsWith("pinned-worker-runtime-probe-")
    ? error.message
    : "pinned-worker-runtime-probe-failed";
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    const code = publicPinnedWorkerRuntimeProbeFailureCode(error);
    process.stderr.write(`${code}\n`);
    process.exitCode = 1;
  });
}
