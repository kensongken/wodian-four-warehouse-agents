import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";

import { StudioAnalysisLayerSchema, type StudioAnalysisLayer } from "@wearable/contracts";
import { z } from "zod";

const execFileAsync = promisify(execFile);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const APPROVED_STUDIO_MODEL_EVAL_MANIFEST_SHA256 = "10b2e4897266c9fd6cd81195c0247e1cd80a2aefe9bb5dfdd4e04913717a47de";
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/u);
const IntervalObjectSchema = z.object({
  startMs: z.number().int().nonnegative(),
  endMs: z.number().int().positive(),
}).strict();
const IntervalSchema = IntervalObjectSchema.refine(
  (value) => value.endMs > value.startMs,
  "studio-model-eval-interval-invalid",
);
const TranscriptSpanSchema = IntervalObjectSchema.extend({
  text: z.string().trim().min(1).max(4_000),
}).strict().refine((value) => value.endMs > value.startMs, "studio-model-eval-interval-invalid");
const ExpectedCaseSchema = z.object({
  id: z.string().regex(/^[a-z0-9][a-z0-9-]{0,99}$/u),
  sourcePath: z.string().trim().min(1).max(1_024).refine(
    (value) => !value.startsWith("/") && !value.split("/").includes(".."),
    "studio-model-eval-relative-source-required",
  ),
  sourceDigest: DigestSchema,
  authorizationClass: z.enum(["repository-generated-synthetic", "desensitized-approved"]),
  expectedScenes: z.array(IntervalSchema).max(1_000),
  transcriptReference: z.array(TranscriptSpanSchema).max(10_000),
  exactDuplicateGroups: z.array(z.string().trim().min(1).max(200)).max(1_000),
  nearDuplicateGroups: z.array(z.string().trim().min(1).max(200)).max(1_000),
  suggestedTags: z.array(z.string().trim().min(1).max(200)).max(1_000),
  demandEvidence: z.array(z.string().trim().min(1).max(4_000)).max(1_000),
}).strict();
const ManifestSchema = z.object({
  schemaVersion: z.literal(1),
  boundary: z.literal("synthetic-desensitized-only"),
  providerEvidence: z.literal("fixture/synthetic"),
  cases: z.array(ExpectedCaseSchema).min(1).max(100),
}).strict().superRefine((value, context) => {
  if (new Set(value.cases.map((entry) => entry.id)).size !== value.cases.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["cases"], message: "studio-model-eval-duplicate-case" });
  }
});

const LayerResultBaseSchema = z.object({
  status: z.enum(["succeeded", "failed-retryable", "failed-manual-review"]),
  runtimeMs: z.number().int().nonnegative(),
  error: z.string().trim().min(1).max(1_000).nullable(),
}).strict();
const FixtureSchema = z.object({
  schemaVersion: z.literal(1),
  providerEvidence: z.literal("fixture/synthetic"),
  synthetic: z.literal(true),
  customerId: z.string().trim().min(1).max(200),
  crossCustomerExposureCount: z.number().int().nonnegative(),
  networkModelRequestCount: z.number().int().nonnegative(),
  peakWorkerMemoryBytes: z.number().int().nonnegative(),
  originalMutationCount: z.number().int().nonnegative(),
  actions: z.object({
    delete: z.number().int().nonnegative(),
    upload: z.number().int().nonnegative(),
    publish: z.number().int().nonnegative(),
  }).strict(),
  confirmedFacts: z.array(z.string().trim().min(1).max(4_000)).max(1_000),
  layers: z.object({
    technical: LayerResultBaseSchema.optional(),
    scene: LayerResultBaseSchema.extend({ scenes: z.array(IntervalSchema).max(1_000) }).strict().optional(),
    transcript: LayerResultBaseSchema.extend({ spans: z.array(TranscriptSpanSchema).max(10_000) }).strict().optional(),
    embedding: LayerResultBaseSchema.extend({
      exactDuplicateGroups: z.array(z.string().trim().min(1).max(200)).max(1_000),
      nearDuplicateGroups: z.array(z.string().trim().min(1).max(200)).max(1_000),
    }).strict().optional(),
    vision: LayerResultBaseSchema.extend({
      suggestedTags: z.array(z.string().trim().min(1).max(200)).max(1_000),
      demandEvidence: z.array(z.string().trim().min(1).max(4_000)).max(1_000),
    }).strict().optional(),
  }).strict(),
}).strict();

const LAYERS = Object.freeze([
  "technical", "scene", "transcript", "embedding", "vision",
] as const satisfies readonly StudioAnalysisLayer[]);

type ExpectedCase = z.infer<typeof ExpectedCaseSchema>;
type Fixture = z.infer<typeof FixtureSchema>;
type Accuracy = Readonly<{
  expectedSceneCount: number;
  observedSceneCount: number;
  matchedSceneCount: number;
  unexpectedSceneCount: number;
  expectedTranscriptSpanCount: number;
  observedTranscriptSpanCount: number;
  matchedTranscriptSpanCount: number;
  unexpectedTranscriptSpanCount: number;
  expectedExactDuplicateGroupCount: number;
  observedExactDuplicateGroupCount: number;
  matchedExactDuplicateGroupCount: number;
  unexpectedExactDuplicateGroupCount: number;
  expectedNearDuplicateGroupCount: number;
  observedNearDuplicateGroupCount: number;
  matchedNearDuplicateGroupCount: number;
  unexpectedNearDuplicateGroupCount: number;
  expectedSuggestedTagCount: number;
  observedSuggestedTagCount: number;
  matchedSuggestedTagCount: number;
  unexpectedSuggestedTagCount: number;
  expectedDemandEvidenceCount: number;
  observedDemandEvidenceCount: number;
  matchedDemandEvidenceCount: number;
  unexpectedDemandEvidenceCount: number;
}>;

type ProviderEvidence = "fixture/synthetic" | "approved-local-model";
type RealProviderAcceptance = Readonly<{
  status: "passed" | "blocked";
  reason: "model-checker-require-ready-failed" | "approved-local-browser-evidence-not-provided" | null;
}>;

export type StudioModelEvaluationReport = Readonly<{
  schemaVersion: 1;
  boundary: "synthetic-desensitized-only";
  providerEvidence: "fixture/synthetic";
  fixtureMechanicalPass: boolean;
  realProviderAcceptance: RealProviderAcceptance;
  manifestPath: string;
  manifestDigest: string;
  modelLockDigest: string;
  gitSha: string;
  runtimeVersions: Readonly<{
    node: string;
    python: "not-run: fixture/synthetic";
    platform: string;
    architecture: string;
  }>;
  caseCount: number;
  layers: ReadonlyArray<Readonly<{
    name: StudioAnalysisLayer;
    totalCases: number;
    passedCases: number;
    failedCases: number;
    totalRuntimeMs: number;
    meanRuntimeMs: number;
    errors: string[];
  }>>;
  accuracy: Accuracy;
  peakWorkerMemoryBytes: number;
  falseConfirmedFactCount: number;
  crossCustomerExposureCount: number;
  networkModelRequestCount: number;
  originalMutationCount: number;
  deleteActionCount: number;
  uploadActionCount: number;
  publishActionCount: number;
  reportPath: string | null;
}>;

export function evaluateRealProviderAcceptance(input: Readonly<{
  checkerRequireReady: boolean;
  browserProviderEvidence: ProviderEvidence;
}>): RealProviderAcceptance {
  if (input.browserProviderEvidence !== "approved-local-model") {
    return { status: "blocked", reason: "approved-local-browser-evidence-not-provided" };
  }
  if (!input.checkerRequireReady) {
    return { status: "blocked", reason: "model-checker-require-ready-failed" };
  }
  return { status: "passed", reason: null };
}

export function evaluateStudioModelCase(
  expectedValue: unknown,
  fixtureValue: unknown,
): Readonly<{
  passed: boolean;
  errors: string[];
  layerErrors: Readonly<Record<StudioAnalysisLayer, string[]>>;
  layerRuntimeMs: Readonly<Record<StudioAnalysisLayer, number>>;
  accuracy: Accuracy;
}> {
  const expected = ExpectedCaseSchema.parse(expectedValue);
  const fixture = FixtureSchema.parse(fixtureValue);
  const layerErrors = Object.fromEntries(LAYERS.map((layer) => [layer, [] as string[]])) as Record<StudioAnalysisLayer, string[]>;
  const layerRuntimeMs = Object.fromEntries(LAYERS.map((layer) => [
    layer, fixture.layers[layer]?.runtimeMs ?? 0,
  ])) as Record<StudioAnalysisLayer, number>;
  for (const layer of LAYERS) {
    const observed = fixture.layers[layer];
    if (!observed) {
      layerErrors[layer].push(`${expected.id}:missing-layer-evidence:${layer}`);
    } else if (observed.status !== "succeeded" || observed.error !== null) {
      layerErrors[layer].push(`${expected.id}:${observed.error ?? observed.status}`);
    }
  }

  const scene = compareExactMultiset(expected.expectedScenes, fixture.layers.scene?.scenes ?? []);
  const transcript = compareExactMultiset(expected.transcriptReference, fixture.layers.transcript?.spans ?? []);
  const exact = compareExactMultiset(expected.exactDuplicateGroups, fixture.layers.embedding?.exactDuplicateGroups ?? []);
  const near = compareExactMultiset(expected.nearDuplicateGroups, fixture.layers.embedding?.nearDuplicateGroups ?? []);
  const tags = compareExactMultiset(expected.suggestedTags, fixture.layers.vision?.suggestedTags ?? []);
  const demand = compareExactMultiset(expected.demandEvidence, fixture.layers.vision?.demandEvidence ?? []);
  appendComparisonErrors(layerErrors.scene, expected.id, "scene", scene);
  appendComparisonErrors(layerErrors.transcript, expected.id, "transcript", transcript);
  appendComparisonErrors(layerErrors.embedding, expected.id, "exact-duplicate", exact);
  appendComparisonErrors(layerErrors.embedding, expected.id, "near-duplicate", near);
  appendComparisonErrors(layerErrors.vision, expected.id, "suggested-tag", tags);
  appendComparisonErrors(layerErrors.vision, expected.id, "demand-evidence", demand);
  const accuracy: Accuracy = {
    expectedSceneCount: scene.expected,
    observedSceneCount: scene.observed,
    matchedSceneCount: scene.matched,
    unexpectedSceneCount: scene.unexpected,
    expectedTranscriptSpanCount: transcript.expected,
    observedTranscriptSpanCount: transcript.observed,
    matchedTranscriptSpanCount: transcript.matched,
    unexpectedTranscriptSpanCount: transcript.unexpected,
    expectedExactDuplicateGroupCount: exact.expected,
    observedExactDuplicateGroupCount: exact.observed,
    matchedExactDuplicateGroupCount: exact.matched,
    unexpectedExactDuplicateGroupCount: exact.unexpected,
    expectedNearDuplicateGroupCount: near.expected,
    observedNearDuplicateGroupCount: near.observed,
    matchedNearDuplicateGroupCount: near.matched,
    unexpectedNearDuplicateGroupCount: near.unexpected,
    expectedSuggestedTagCount: tags.expected,
    observedSuggestedTagCount: tags.observed,
    matchedSuggestedTagCount: tags.matched,
    unexpectedSuggestedTagCount: tags.unexpected,
    expectedDemandEvidenceCount: demand.expected,
    observedDemandEvidenceCount: demand.observed,
    matchedDemandEvidenceCount: demand.matched,
    unexpectedDemandEvidenceCount: demand.unexpected,
  };
  const errors = LAYERS.flatMap((layer) => layerErrors[layer]);
  return { passed: errors.length === 0, errors, layerErrors, layerRuntimeMs, accuracy };
}

export async function runStudioModelEvaluation(input: Readonly<{
  manifestPath: string;
  outputDirectory?: string;
  checkerRequireReady?: boolean;
  browserProviderEvidence?: ProviderEvidence;
}>): Promise<StudioModelEvaluationReport> {
  const manifestPath = resolve(input.manifestPath);
  const manifestBytes = await readFile(manifestPath);
  const manifestDigest = sha256(manifestBytes);
  if (manifestDigest !== APPROVED_STUDIO_MODEL_EVAL_MANIFEST_SHA256) {
    throw new Error("studio-model-eval-manifest-digest-unapproved");
  }
  const manifest = ManifestSchema.parse(JSON.parse(manifestBytes.toString("utf8")));
  const manifestRoot = dirname(manifestPath);
  const rows = new Map<StudioAnalysisLayer, {
    passedCases: number;
    failedCases: number;
    totalRuntimeMs: number;
    errors: string[];
  }>(LAYERS.map((layer) => [layer, { passedCases: 0, failedCases: 0, totalRuntimeMs: 0, errors: [] }]));
  const accuracy = {
    expectedSceneCount: 0,
    observedSceneCount: 0,
    matchedSceneCount: 0,
    unexpectedSceneCount: 0,
    expectedTranscriptSpanCount: 0,
    observedTranscriptSpanCount: 0,
    matchedTranscriptSpanCount: 0,
    unexpectedTranscriptSpanCount: 0,
    expectedExactDuplicateGroupCount: 0,
    observedExactDuplicateGroupCount: 0,
    matchedExactDuplicateGroupCount: 0,
    unexpectedExactDuplicateGroupCount: 0,
    expectedNearDuplicateGroupCount: 0,
    observedNearDuplicateGroupCount: 0,
    matchedNearDuplicateGroupCount: 0,
    unexpectedNearDuplicateGroupCount: 0,
    expectedSuggestedTagCount: 0,
    observedSuggestedTagCount: 0,
    matchedSuggestedTagCount: 0,
    unexpectedSuggestedTagCount: 0,
    expectedDemandEvidenceCount: 0,
    observedDemandEvidenceCount: 0,
    matchedDemandEvidenceCount: 0,
    unexpectedDemandEvidenceCount: 0,
  };
  let peakWorkerMemoryBytes = 0;
  let falseConfirmedFactCount = 0;
  let crossCustomerExposureCount = 0;
  let networkModelRequestCount = 0;
  let originalMutationCount = 0;
  let deleteActionCount = 0;
  let uploadActionCount = 0;
  let publishActionCount = 0;

  for (const expected of manifest.cases) {
    const sourcePath = resolve(manifestRoot, expected.sourcePath);
    if (relative(manifestRoot, sourcePath).startsWith("..")) {
      throw new Error(`studio-model-eval-source-outside-manifest:${expected.id}`);
    }
    const bytes = await readFile(sourcePath);
    if (sha256(bytes) !== expected.sourceDigest) {
      throw new Error(`studio-model-eval-source-digest-mismatch:${expected.id}`);
    }
    const fixture = FixtureSchema.parse(JSON.parse(bytes.toString("utf8")));
    const evaluated = evaluateStudioModelCase(expected, fixture);
    peakWorkerMemoryBytes = Math.max(peakWorkerMemoryBytes, fixture.peakWorkerMemoryBytes);
    falseConfirmedFactCount += fixture.confirmedFacts.length;
    crossCustomerExposureCount += fixture.crossCustomerExposureCount;
    networkModelRequestCount += fixture.networkModelRequestCount;
    originalMutationCount += fixture.originalMutationCount;
    deleteActionCount += fixture.actions.delete;
    uploadActionCount += fixture.actions.upload;
    publishActionCount += fixture.actions.publish;

    for (const layer of LAYERS) {
      const row = rows.get(layer)!;
      row.totalRuntimeMs += evaluated.layerRuntimeMs[layer];
      if (evaluated.layerErrors[layer].length > 0) {
        row.failedCases += 1;
        row.errors.push(...evaluated.layerErrors[layer]);
      } else {
        row.passedCases += 1;
      }
    }
    for (const key of Object.keys(accuracy) as Array<keyof Accuracy>) {
      accuracy[key] += evaluated.accuracy[key];
    }
  }

  const layers = LAYERS.map((name) => {
    const row = rows.get(name)!;
    return {
      name,
      totalCases: manifest.cases.length,
      ...row,
      meanRuntimeMs: manifest.cases.length === 0 ? 0 : row.totalRuntimeMs / manifest.cases.length,
    };
  });
  const fixtureMechanicalPass = layers.every((row) => row.failedCases === 0)
    && falseConfirmedFactCount === 0
    && crossCustomerExposureCount === 0
    && networkModelRequestCount === 0
    && originalMutationCount === 0
    && deleteActionCount === 0
    && uploadActionCount === 0
    && publishActionCount === 0;
  const outputDirectory = input.outputDirectory === undefined ? null : resolve(input.outputDirectory);
  const reportPath = outputDirectory === null ? null : join(outputDirectory, "studio-model-evaluation-report.json");
  const report: StudioModelEvaluationReport = {
    schemaVersion: 1,
    boundary: manifest.boundary,
    providerEvidence: manifest.providerEvidence,
    fixtureMechanicalPass,
    realProviderAcceptance: evaluateRealProviderAcceptance({
      checkerRequireReady: input.checkerRequireReady ?? false,
      browserProviderEvidence: input.browserProviderEvidence ?? "fixture/synthetic",
    }),
    manifestPath,
    manifestDigest,
    modelLockDigest: sha256(await readFile(resolve(repositoryRoot, "models.lock.json"))),
    gitSha: (await execFileAsync("git", ["rev-parse", "HEAD"], { cwd: repositoryRoot })).stdout.trim(),
    runtimeVersions: {
      node: process.version,
      python: "not-run: fixture/synthetic",
      platform: process.platform,
      architecture: process.arch,
    },
    caseCount: manifest.cases.length,
    layers,
    accuracy,
    peakWorkerMemoryBytes,
    falseConfirmedFactCount,
    crossCustomerExposureCount,
    networkModelRequestCount,
    originalMutationCount,
    deleteActionCount,
    uploadActionCount,
    publishActionCount,
    reportPath,
  };
  if (reportPath !== null) {
    await mkdir(outputDirectory!, { recursive: true });
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  }
  return report;
}

function compareExactMultiset<T>(expected: readonly T[], observed: readonly T[]) {
  const remaining = observed.map(canonicalValue).sort(codePointCompare);
  let matched = 0;
  for (const value of expected.map(canonicalValue).sort(codePointCompare)) {
    const index = remaining.indexOf(value);
    if (index === -1) continue;
    matched += 1;
    remaining.splice(index, 1);
  }
  return { expected: expected.length, observed: observed.length, matched, unexpected: remaining.length };
}

function appendComparisonErrors(
  errors: string[],
  caseId: string,
  label: string,
  comparison: Readonly<{ expected: number; matched: number; unexpected: number }>,
): void {
  if (comparison.matched < comparison.expected) errors.push(`${caseId}:missing-${label}-output`);
  if (comparison.unexpected > 0) errors.push(`${caseId}:unexpected-${label}-output`);
}

function canonicalValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalValue).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value).sort(([left], [right]) => codePointCompare(left, right))
      .map(([key, entry]) => `${JSON.stringify(key)}:${canonicalValue(entry)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function codePointCompare(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0)!);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index]! - rightPoints[index]!;
  }
  return leftPoints.length - rightPoints.length;
}

function sha256(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function runCli(): Promise<void> {
  const args = process.argv.slice(2).filter((argument) => argument !== "--");
  const manifest = option(args, "--manifest");
  if (!manifest) throw new Error("studio-model-eval-manifest-required");
  const requestedOutput = option(args, "--output");
  const outputDirectory = requestedOutput ?? await mkdtemp(join(tmpdir(), "studio-model-eval-"));
  const report = await runStudioModelEvaluation({ manifestPath: manifest, outputDirectory });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (!report.fixtureMechanicalPass) process.exitCode = 1;
}

function option(args: readonly string[], name: string): string | null {
  const index = args.indexOf(name);
  if (index === -1) return null;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`${name.slice(2)}-value-required`);
  return value;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await runCli();
}
