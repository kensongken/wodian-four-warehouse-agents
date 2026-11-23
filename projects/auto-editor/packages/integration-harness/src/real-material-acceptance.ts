import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import {
  chmod,
  lstat,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import {
  RealMaterialRunDecisionSchema,
  RealMaterialRunReportSchema,
  type RealMaterialRunDecision,
  type RealMaterialRunReport,
  type ReferenceMaterialMappingPlan,
  type ReferenceSample,
  type StudioMediaAsset,
  type StoryType,
} from "@wearable/contracts";
import {
  CustomerMaterialLibrary,
  createFfprobeStudioSourceIntervalResolver,
  createReferenceMaterialMappingPlan,
  JsonReferenceSampleStore,
  LocalProcessStudioModelWorker,
  LocalReferenceGrammarAdapter,
  mergeStudioEvidence,
  ReferenceGuidedEditor,
  ReferenceSampleService,
  renderReferenceVariants,
  selectFinalReferenceAnalysisAttempt,
  StudioAnalysisJobService,
  type ReferenceEditorRenderRequest,
  type ReferenceMappingAssetSnapshot,
  type ReferenceRenderAsset,
  type StudioScope,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

import {
  assertLockedMaterialUnchanged,
  loadLockedRealMaterialManifest,
  readLockedMaterialBytes,
} from "./real-material-manifest";
import { prepareRealMaterialRun, type PreparedRealMaterialRun } from "./real-material-preparer";

const require = createRequire(import.meta.url);
const ffprobePath = (require("ffprobe-static") as { path?: unknown }).path;
const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageDirectory, "../..");
const workerRoot = join(repositoryRoot, "tools", "media-model-worker");
const workerPython = join(workerRoot, ".venv", "bin", "python");
type ReviewPacket = Readonly<{
  schemaVersion: 1;
  runId: string;
  customerId: string;
  generatedAt: string;
  storyResults: PreparedRealMaterialRun["stories"];
  capability: RealMaterialRunReport["capability"];
  referenceRules: readonly Readonly<{ id: string; kind: string; instruction: string }> [];
  proposedReferenceRuleIds: readonly string[];
  mappingPlan: Readonly<{ id: string; version: number; digest: string }>;
  proposedMappings: readonly Readonly<{
    ruleId: string;
    status: "matched" | "substituted" | "unachievable";
    sourceInputId: string | null;
    message: string;
  }>[];
  publishedClaimed: false;
}>;

type PrivateState = Readonly<{
  schemaVersion: 1;
  manifestIdentity: string;
  workflowClockBaseMs: number;
  reviewPacketPath: string;
  reviewPacketDigest: string;
  prepared: PreparedRealMaterialRun;
  proposal: Readonly<{
    sampleId: string;
    analysisId: string;
    ruleIds: readonly string[];
    mappingPlan: Readonly<{ id: string; version: number; digest: string }>;
  }>;
  awaitingReport: RealMaterialRunReport;
  decisionDigest: string | null;
  finalReport: RealMaterialRunReport | null;
  finalBindings: unknown | null;
}>;

type Workflow = Readonly<{
  store: JsonReferenceSampleStore;
  editor: ReferenceGuidedEditor;
  sample: ReferenceSample;
  analysis: Awaited<ReturnType<ReferenceGuidedEditor["analyze"]>>;
  draftMapping: ReferenceMaterialMappingPlan;
}>;

export async function prepareRealMaterialAcceptance(input: Readonly<{
  manifestPath: string;
  approvedRoots: readonly string[];
  runDirectory: string;
}>): Promise<Readonly<{
  report: RealMaterialRunReport;
  reviewPacketPath: string;
  reviewPacketDigest: string;
}>> {
  assertMediaTools();
  const roots = await createRunLayout(input.runDirectory);
  const locked = await loadLockedRealMaterialManifest({
    manifestPath: input.manifestPath,
    approvedRoots: input.approvedRoots,
  }).catch((error: unknown) => {
    if (error instanceof Error && [
      "real-material-source-digest-mismatch",
      "real-material-source-changed-during-lock",
    ].includes(error.message)) {
      throw new Error("real-material-source-changed", { cause: error });
    }
    throw error;
  });
  const manifestIdentity = sha256(canonicalJson(locked.manifest));
  const existing = await readPrivateState(roots.privateStatePath);
  if (existing) {
    if (existing.manifestIdentity !== manifestIdentity) throw new Error("real-material-run-conflict");
    await assertLockedMaterialUnchanged(locked.inputs);
    return {
      report: existing.finalReport ?? existing.awaitingReport,
      reviewPacketPath: existing.reviewPacketPath,
      reviewPacketDigest: existing.reviewPacketDigest,
    };
  }

  const workflowClockBaseMs = Date.now() + 60_000;
  const services = createServices(roots, locked.manifest.customerId, locked.manifest.actorId, workflowClockBaseMs);
  const prepared = await prepareRealMaterialRun({
    locked,
    runtimeRoot: roots.runtime,
    library: services.library,
    analysisJobs: services.analysisJobs,
    now: services.now,
  });
  const workflow = await buildWorkflow({ roots, locked, prepared, services });
  const referenceRuleResults = workflow.draftMapping.mappings.map((mapping) => ({
    ruleId: mapping.ruleId,
    status: mapping.status,
    message: mapping.status === "unachievable"
      ? "No currently authorized material can execute this rule."
      : mapping.status === "matched"
        ? "Authorized material exactly matches the confirmed rule."
        : "Authorized material can approximate this rule without copying reference bytes.",
  }));
  const capability = { ...prepared.capability, runId: locked.manifest.runId };
  const report = RealMaterialRunReportSchema.parse({
    schemaVersion: 1,
    runId: locked.manifest.runId,
    customerId: locked.manifest.customerId,
    status: "awaiting-decision",
    evidenceScope: evidenceScope(locked.manifest.customerId),
    generatedAt: services.now(),
    reviewPacketDigest: null,
    decisionId: null,
    inputReceipts: locked.inputs.map(({ declared, lockedDigest }) => ({
      inputId: declared.inputId,
      role: declared.role,
      mediaKind: declared.mediaKind,
      digest: lockedDigest,
    })),
    capability,
    storyResults: prepared.stories,
    referenceRuleResults,
    outputs: [],
    publishedClaimed: false,
  });
  const packet = reviewPacket({ locked, prepared, workflow, capability, generatedAt: services.now() });
  const reviewPacketBytes = `${canonicalJson(packet)}\n`;
  const reviewPacketDigest = sha256(reviewPacketBytes);
  await writePublicFile(roots.reviewPacketPath, reviewPacketBytes);
  const boundReport = RealMaterialRunReportSchema.parse({ ...report, reviewPacketDigest });
  await assertLockedMaterialUnchanged(locked.inputs);
  const state: PrivateState = {
    schemaVersion: 1,
    manifestIdentity,
    workflowClockBaseMs,
    reviewPacketPath: roots.reviewPacketPath,
    reviewPacketDigest,
    prepared,
    proposal: {
      sampleId: workflow.sample.id,
      analysisId: workflow.analysis.id,
      ruleIds: packet.proposedReferenceRuleIds,
      mappingPlan: packet.mappingPlan,
    },
    awaitingReport: boundReport,
    decisionDigest: null,
    finalReport: null,
    finalBindings: null,
  };
  await writePrivateState(roots.privateStatePath, state);
  await writePublicRecords(roots, boundReport);
  return { report: boundReport, reviewPacketPath: roots.reviewPacketPath, reviewPacketDigest };
}

export async function renderRealMaterialAcceptance(input: Readonly<{
  manifestPath: string;
  decisionPath: string;
  approvedRoots: readonly string[];
  runDirectory: string;
}>): Promise<RealMaterialRunReport> {
  assertMediaTools();
  const roots = await openRunLayout(input.runDirectory);
  const state = await readPrivateState(roots.privateStatePath);
  if (!state) throw new Error("real-material-run-not-prepared");
  const locked = await loadLockedRealMaterialManifest({
    manifestPath: input.manifestPath,
    approvedRoots: input.approvedRoots,
  }).catch((error: unknown) => {
    if (error instanceof Error && [
      "real-material-source-digest-mismatch",
      "real-material-source-changed-during-lock",
    ].includes(error.message)) {
      throw new Error("real-material-source-changed", { cause: error });
    }
    throw error;
  });
  if (sha256(canonicalJson(locked.manifest)) !== state.manifestIdentity) {
    throw new Error("real-material-run-conflict");
  }
  await assertReviewPacketCurrent(state);
  const decisionBytes = await readMode0600File(input.decisionPath, "real-material-decision-mode-must-be-0600");
  const decision = RealMaterialRunDecisionSchema.parse(JSON.parse(decisionBytes.toString("utf8")));
  const decisionDigest = sha256(canonicalJson(decision));
  if (state.finalReport) {
    if (state.decisionDigest !== decisionDigest) throw new Error("real-material-decision-conflict");
    await assertLockedMaterialUnchanged(locked.inputs);
    return state.finalReport;
  }
  validateDecision(decision, state);
  await assertLockedMaterialUnchanged(locked.inputs);

  const services = createServices(
    roots,
    locked.manifest.customerId,
    locked.manifest.actorId,
    state.workflowClockBaseMs,
  );
  const prepared = await prepareRealMaterialRun({
    locked,
    runtimeRoot: roots.runtime,
    library: services.library,
    analysisJobs: services.analysisJobs,
    now: services.now,
  });
  const workflow = await buildWorkflow({ roots, locked, prepared, services });
  assertProposalCurrent(state, workflow);
  await assertLockedMaterialUnchanged(locked.inputs);
  const confirmedMapping = await workflow.editor.confirmMappingPlan(
    prepared.scope,
    workflow.draftMapping.id,
    `${decision.operationKey}:mapping`,
  );

  const outputs: RealMaterialRunReport["outputs"] = [];
  const reportBindings: unknown[] = [];
  for (const storyType of decision.selectedStoryTypes) {
    await assertLockedMaterialUnchanged(locked.inputs);
    const execution = await workflow.editor.render(
      prepared.scope,
      confirmedMapping.id,
      `${decision.operationKey}:render:${storyType}`,
    );
    const snapshot = await workflow.store.snapshot();
    const storyOutputs = await Promise.all(execution.artifacts.map(async (artifact) => {
      const draft = snapshot.drafts.find((candidate) =>
        candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
      const plan = snapshot.variantPlans.find((candidate) =>
        candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
      if (!draft || !plan) throw new Error("real-material-render-evidence-missing");
      const probe = await probeVideo(draft.outputPath);
      const sourceInputIds = unique(draft.lineage.flatMap((lineage) => {
        const binding = prepared.privateBindings.find((candidate) => candidate.assetId === lineage.sourceAssetId);
        return binding ? [binding.inputId] : [];
      }));
      if (sourceInputIds.length === 0) throw new Error("real-material-output-lineage-missing");
      const referenceBinding = prepared.privateBindings.find((binding) =>
        binding.inputId === prepared.referenceInputId);
      if (referenceBinding && draft.lineage.some((row) =>
        row.sourceAssetId === referenceBinding.assetId || row.sourceDigest === referenceBinding.sourceDigest)) {
        throw new Error("external-reference-content-forbidden");
      }
      return {
        storyType,
        variant: artifact.kind,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        timelineDigest: sha256(canonicalJson(plan.timeline)),
        durationMs: Math.round(probe.durationSeconds * 1_000),
        width: probe.width,
        height: probe.height,
        sourceInputIds,
        referenceBytesUsed: false as const,
      };
    }));
    assertDistinctVariants(storyType, storyOutputs);
    outputs.push(...storyOutputs);
    const mobileReview = await workflow.editor.mobileReview(prepared.scope, workflow.sample.id);
    reportBindings.push({
      storyType,
      execution: execution.bindings,
      mobileReview,
      promptRevisionBindings: execution.artifacts.map((artifact) => ({
        reportId: execution.id,
        reportVersion: execution.version,
        draftId: artifact.draftId,
        draftVersion: artifact.draftVersion,
        draftDigest: artifact.outputDigest,
        target: artifact.kind,
      })),
    });
  }
  await assertLockedMaterialUnchanged(locked.inputs);
  const finalReport = RealMaterialRunReportSchema.parse({
    ...state.awaitingReport,
    status: "rendered",
    generatedAt: decision.confirmedAt,
    decisionId: decision.decisionId,
    outputs,
    publishedClaimed: false,
  });
  const finalState: PrivateState = {
    ...state,
    decisionDigest,
    finalReport,
    finalBindings: {
      decision,
      confirmedMapping: {
        id: confirmedMapping.id,
        version: confirmedMapping.version,
        digest: confirmedMapping.contentDigest,
      },
      reports: reportBindings,
    },
  };
  await writePrivateState(roots.privateStatePath, finalState);
  await writePublicRecords(roots, finalReport);
  return finalReport;
}

function createServices(
  roots: RunLayout,
  customerId: string,
  actorId: string,
  clockBaseMs: number,
) {
  let tick = 0;
  const now = () => new Date(clockBaseMs + tick++ * 1_000).toISOString();
  const library = new CustomerMaterialLibrary(roots.library, ({ actorId: candidate, customerId: customer }) =>
    candidate === actorId && customer === customerId);
  const worker = new LocalProcessStudioModelWorker({
    command: "/usr/bin/env",
    args: [
      "PYTHONDONTWRITEBYTECODE=1",
      workerPython,
      "-B",
      "worker.py",
      "--ffprobe-path", ffprobePath as string,
      "--ffmpeg-path", ffmpegPath as string,
    ],
    cwd: workerRoot,
    timeoutMs: 120_000,
    maxStdoutBytes: 256 * 1024,
    maxStderrBytes: 8 * 1024,
    allowedMediaRoot: roots.runtime,
  });
  const analysisJobs = new StudioAnalysisJobService(library, worker, {
    now,
    resolveSourceInterval: createFfprobeStudioSourceIntervalResolver(ffprobePath as string),
  });
  return { now, library, analysisJobs };
}

async function buildWorkflow(input: Readonly<{
  roots: RunLayout;
  locked: Awaited<ReturnType<typeof loadLockedRealMaterialManifest>>;
  prepared: PreparedRealMaterialRun;
  services: ReturnType<typeof createServices>;
}>): Promise<Workflow> {
  const { roots, locked, prepared, services } = input;
  const reference = locked.inputs.find(({ declared }) => declared.inputId === prepared.referenceInputId);
  if (!reference) throw new Error("real-material-reference-required");
  const sampleService = new ReferenceSampleService(services.library, roots.workflow, { now: services.now });
  const referenceBytes = await readLockedMaterialBytes(reference);
  const registration = await sampleService.importDedicated(prepared.scope, {
    requestId: `real-reference-${sha256(`${locked.manifest.runId}\0${reference.declared.inputId}`)}`,
    fileName: "reference.mp4",
    bytes: referenceBytes,
    sourceKind: referenceSourceKind(locked.manifest.referencePolicy),
    operationKey: `real-reference-${sha256(locked.manifest.runId).slice(0, 40)}`,
  });
  const store = await JsonReferenceSampleStore.open(roots.workflow, prepared.scope);
  const grammar = new LocalReferenceGrammarAdapter({ now: services.now });
  const renderAssets = await Promise.all(prepared.assets.map(async (asset) =>
    renderAsset(prepared.scope, asset, await services.library.readOriginal(
      prepared.scope.actorId, prepared.scope.customerId, asset.id,
    ))));

  const withAuthorization = <T>(request: Readonly<{
    scope: StudioScope;
    sample: ReferenceSample;
    mappingPlan: ReferenceMaterialMappingPlan;
  }>, work: () => Promise<T>) => services.library.withReferenceWorkflowAssets(
    request.scope.actorId,
    request.scope.customerId,
    workflowBindings(request.sample, request.mappingPlan),
    work,
  );
  const render = (request: ReferenceEditorRenderRequest) => withAuthorization(request, async () => {
    const rendered = await renderReferenceVariants({
      mappingPlan: request.mappingPlan,
      rights: request.rights,
      analysis: request.analysis,
      assets: [...renderAssets],
      outputRoot: roots.outputs,
      operationKey: request.operationKey,
      now: services.now(),
      ffmpegPath,
      ffprobePath: ffprobePath as string,
    });
    return { ...rendered, outputDirectory: dirname(rendered.drafts[0]!.outputPath) };
  });
  const editor = new ReferenceGuidedEditor({
    store,
    now: services.now,
    analyze: async ({ sample, rights, operationKey }) => {
      const source = await services.library.inspectReferenceAnalysisAsset(
        prepared.scope.actorId,
        prepared.scope.customerId,
        prepared.scope.sessionId,
        sample.sourceAssetId,
      );
      const entry = await services.analysisJobs.enqueueReference(
        prepared.scope,
        source,
        ["scene", "transcript", "vision"],
        operationKey,
      );
      const attempt = selectFinalReferenceAnalysisAttempt(entry, {
        customerId: prepared.scope.customerId,
        sessionId: prepared.scope.sessionId,
        sourceAssetId: sample.sourceAssetId,
        sourceDigest: sample.sourceDigest,
        sourcePath: source.sourcePath,
        sourceStartMs: entry.job.sourceStartMs,
        sourceEndMs: entry.job.sourceEndMs,
      });
      return grammar.extractGrammar({
        scope: prepared.scope,
        sample,
        rights,
        source,
        receipts: attempt.evidenceReceipts,
      });
    },
    createMapping: async ({ scope, sample, rights, analysis, selection }) =>
      createReferenceMaterialMappingPlan({
        scope,
        sample,
        rights,
        analysis,
        selection,
        currentMaterials: await services.library.inventoryAt(
          scope.actorId,
          scope.customerId,
          selection.confirmedAt,
        ),
        authorizedHistoricalMaterials: [],
        assets: renderAssets,
      }),
    render,
    withCurrentAssetAuthorization: withAuthorization,
    readDraftOutputDigest: async ({ draft }) => sha256(await readFile(draft.outputPath)),
    cleanupRenderedOutput: async (result) => safeRemoveOutput(roots.outputs, result.outputDirectory),
  });
  await editor.declareRights(prepared.scope, {
    referenceSampleId: registration.sample.id,
    sourceKind: referenceSourceKind(locked.manifest.referencePolicy),
    reusableElements: [],
  }, `real-rights-${sha256(locked.manifest.runId).slice(0, 40)}`);
  const sample = latestSample(await store.snapshot(), registration.sample.id);
  const analysis = await editor.analyze(
    prepared.scope,
    sample.id,
    `real-reference-analysis-${sha256(locked.manifest.runId).slice(0, 32)}`,
  );
  await ensureMaterialCards(locked, prepared, services, analysis);
  const selectedRuleIds = analysis.rules.filter((rule) => rule.kind !== "audio").map((rule) => rule.id);
  if (selectedRuleIds.length === 0) throw new Error("real-material-reference-rules-missing");
  await editor.confirmStyle(
    prepared.scope,
    analysis.id,
    selectedRuleIds,
    `real-style-proposal-${sha256(locked.manifest.runId).slice(0, 32)}`,
  );
  const draftMapping = await editor.createMappingPlan(
    prepared.scope,
    sample.id,
    `real-mapping-proposal-${sha256(locked.manifest.runId).slice(0, 32)}`,
  );
  return { store, editor, sample, analysis, draftMapping };
}

async function ensureMaterialCards(
  locked: Awaited<ReturnType<typeof loadLockedRealMaterialManifest>>,
  prepared: PreparedRealMaterialRun,
  services: ReturnType<typeof createServices>,
  analysis: Awaited<ReturnType<ReferenceGuidedEditor["analyze"]>>,
): Promise<void> {
  const history = await services.analysisJobs.history(prepared.scope);
  const styleFacts = analysis.rules.filter((rule) => rule.kind !== "audio").map((rule) => {
    if (rule.kind === "rhythm") return {
      rule,
      fact: `reference-rhythm:${normalizeEvidenceCode(rule.id)}`,
    };
    if (rule.kind === "narrative") return {
      rule,
      fact: `reference-narrative:${normalizeEvidenceCode(rule.id)}`,
    };
    const transition = analysis.transitions.find((candidate) => rule.evidenceIds.includes(candidate.id));
    return transition && transition.kind !== "unsupported"
      ? { rule, fact: `reference-transition:${transition.kind}:${normalizeEvidenceCode(rule.id)}` }
      : null;
  }).filter((value): value is NonNullable<typeof value> => value !== null);
  for (const [index, asset] of prepared.assets.entries()) {
    const entry = history.find((candidate) => candidate.job.sourceAssetId === asset.id);
    if (!entry || entry.job.status !== "succeeded") throw new Error("real-material-analysis-history-missing");
    const binding = prepared.privateBindings.find((candidate) => candidate.assetId === asset.id);
    const declared = locked.inputs.find((candidate) => candidate.declared.inputId === binding?.inputId)?.declared;
    if (!declared) throw new Error("real-material-input-binding-missing");
    const assignedStyle = styleFacts[index];
    const verifiedStyleFacts = assignedStyle && styleFactVerified(assignedStyle.rule, analysis, entry)
      ? [assignedStyle.fact]
      : [];
    const merged = mergeStudioEvidence({
      scope: prepared.scope,
      asset,
      sourcePath: entry.job.sourcePath,
      sourceStartMs: entry.job.sourceStartMs,
      sourceEndMs: entry.job.sourceEndMs,
      contentType: "single-product",
      receipts: [...entry.receipts],
      humanFacts: [
        ...declared.confirmedFacts.map((fact) => `${fact.kind}:${fact.value}`),
        ...verifiedStyleFacts,
      ],
      createdAt: locked.manifest.createdAt,
      version: 1,
    });
    if (!merged.card.technicalUsable) throw new Error("real-material-technical-evidence-insufficient");
    await services.library.appendMaterialCard(prepared.scope.actorId, prepared.scope.customerId, merged.card);
  }
}

function reviewPacket(input: Readonly<{
  locked: Awaited<ReturnType<typeof loadLockedRealMaterialManifest>>;
  prepared: PreparedRealMaterialRun;
  workflow: Workflow;
  capability: RealMaterialRunReport["capability"];
  generatedAt: string;
}>): ReviewPacket {
  const assetToInput = new Map(input.prepared.privateBindings.map((binding) => [binding.assetId, binding.inputId]));
  return {
    schemaVersion: 1,
    runId: input.locked.manifest.runId,
    customerId: input.locked.manifest.customerId,
    generatedAt: input.generatedAt,
    storyResults: input.prepared.stories,
    capability: input.capability,
    referenceRules: input.workflow.analysis.rules.map((rule) => ({
      id: rule.id,
      kind: rule.kind,
      instruction: rule.description,
    })),
    proposedReferenceRuleIds: input.workflow.analysis.rules
      .filter((rule) => rule.kind !== "audio")
      .map((rule) => rule.id),
    mappingPlan: {
      id: input.workflow.draftMapping.id,
      version: input.workflow.draftMapping.version,
      digest: input.workflow.draftMapping.contentDigest,
    },
    proposedMappings: input.workflow.draftMapping.mappings.map((mapping) => ({
      ruleId: mapping.ruleId,
      status: mapping.status,
      sourceInputId: mapping.sourceAssetId ? assetToInput.get(mapping.sourceAssetId) ?? null : null,
      message: mapping.differenceReason ?? `Mapping status: ${mapping.status}`,
    })),
    publishedClaimed: false,
  };
}

function validateDecision(decision: RealMaterialRunDecision, state: PrivateState): void {
  if (decision.runId !== state.awaitingReport.runId) throw new Error("real-material-decision-run-mismatch");
  if (decision.reviewPacketDigest !== state.reviewPacketDigest) {
    throw new Error("real-material-review-packet-stale");
  }
  if (decision.selectedStoryTypes.length !== 1) {
    throw new Error("real-material-multi-story-render-not-supported");
  }
  const ready = new Set(state.awaitingReport.storyResults
    .filter(({ status }) => status === "ready")
    .map(({ storyType }) => storyType));
  if (decision.selectedStoryTypes.some((story) => !ready.has(story))) {
    throw new Error("real-material-selected-story-not-ready");
  }
  if (canonicalJson(decision.selectedReferenceRuleIds) !== canonicalJson(state.proposal.ruleIds)) {
    throw new Error("real-material-reference-rule-selection-mismatch");
  }
  if (canonicalJson(decision.mappingPlan) !== canonicalJson(state.proposal.mappingPlan)) {
    throw new Error("real-material-mapping-plan-mismatch");
  }
}

function assertProposalCurrent(state: PrivateState, workflow: Workflow): void {
  if (workflow.sample.id !== state.proposal.sampleId || workflow.analysis.id !== state.proposal.analysisId) {
    throw new Error("real-material-reference-state-stale");
  }
  const current = {
    id: workflow.draftMapping.id,
    version: workflow.draftMapping.version,
    digest: workflow.draftMapping.contentDigest,
  };
  if (canonicalJson(current) !== canonicalJson(state.proposal.mappingPlan)) {
    throw new Error("real-material-mapping-plan-stale");
  }
}

function renderAsset(scope: StudioScope, asset: StudioMediaAsset, bytes: Uint8Array): ReferenceRenderAsset {
  const permission: ReferenceMappingAssetSnapshot["permission"] = {
    id: `real_permission_${sha256(`${scope.customerId}\0${scope.sessionId}\0${asset.id}\0${asset.version}`)}`,
    version: asset.version,
    epoch: asset.version,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    view: "allowed",
    use: "allowed",
  };
  return { asset, permission, withdrawn: false, verifiedDigest: sha256(bytes), bytes };
}

function workflowBindings(sample: ReferenceSample, plan: ReferenceMaterialMappingPlan) {
  const renderSources = new Map(plan.mappings.flatMap((mapping) => {
    if (!mapping.sourceAssetId) return [];
    if (!mapping.sourceDigest || !mapping.permission) throw new Error("real-material-render-binding-missing");
    return [[mapping.sourceAssetId, {
      role: "render-source" as const,
      assetId: mapping.sourceAssetId,
      assetVersion: mapping.permission.version,
      sourceDigest: mapping.sourceDigest,
      permission: {
        version: mapping.permission.version,
        epoch: mapping.permission.epoch,
        view: "allowed" as const,
        use: "allowed" as const,
      },
    }] as const];
  }));
  return [{
    role: "sample-source" as const,
    assetId: sample.sourceAssetId,
    assetVersion: 1,
    sourceDigest: sample.sourceDigest,
  }, ...renderSources.values()];
}

function latestSample(snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>, id: string): ReferenceSample {
  const sample = snapshot.samples.filter((candidate) => candidate.id === id)
    .sort((left, right) => right.version - left.version)[0];
  if (!sample || sample.status !== "ready") throw new Error("real-material-reference-sample-not-ready");
  return sample;
}

function assertDistinctVariants(
  storyType: StoryType,
  outputs: readonly RealMaterialRunReport["outputs"][number][],
): void {
  if (outputs.length !== 4 || new Set(outputs.map(({ variant }) => variant)).size !== 4) {
    throw new Error(`real-material-four-variants-required:${storyType}`);
  }
  if (new Set(outputs.map(({ timelineDigest }) => timelineDigest)).size !== 4) {
    throw new Error(`real-material-distinct-timelines-required:${storyType}`);
  }
  if (new Set(outputs.map(({ artifactDigest }) => artifactDigest)).size !== 4) {
    throw new Error(`real-material-distinct-output-digests-required:${storyType}`);
  }
}

async function probeVideo(path: string): Promise<{ durationSeconds: number; width: number; height: number }> {
  const output = await run(ffprobePath as string, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,width,height",
    "-of", "json",
    path,
  ]);
  const value = JSON.parse(output) as {
    streams?: Array<{ codec_type?: string; width?: number; height?: number }>;
    format?: { duration?: string };
  };
  const video = value.streams?.find((stream) => stream.codec_type === "video");
  const durationSeconds = Number(value.format?.duration);
  if (!video?.width || !video.height || !Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error("real-material-output-ffprobe-invalid");
  }
  return { durationSeconds, width: video.width, height: video.height };
}

type RunLayout = Readonly<{
  root: string;
  runtime: string;
  workflow: string;
  outputs: string;
  evidence: string;
  library: string;
  privateStatePath: string;
  reviewPacketPath: string;
  publicReportPath: string;
  publicMarkdownPath: string;
}>;

async function createRunLayout(runDirectory: string): Promise<RunLayout> {
  if (!isAbsolute(runDirectory)) throw new Error("real-material-run-directory-absolute-required");
  await mkdir(runDirectory, { recursive: true, mode: 0o700 });
  const root = await realpath(runDirectory);
  const metadata = await lstat(root);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new Error("real-material-run-directory-invalid");
  await chmod(root, 0o700);
  const layout = layoutFor(root);
  for (const directory of [layout.runtime, layout.workflow, layout.outputs, layout.evidence, layout.library]) {
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await chmod(directory, 0o700);
  }
  return layout;
}

async function openRunLayout(runDirectory: string): Promise<RunLayout> {
  const root = await realpath(runDirectory).catch(() => { throw new Error("real-material-run-not-prepared"); });
  const layout = layoutFor(root);
  for (const directory of [layout.runtime, layout.workflow, layout.outputs, layout.evidence, layout.library]) {
    const metadata = await lstat(directory).catch(() => { throw new Error("real-material-run-not-prepared"); });
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new Error("real-material-run-not-prepared");
  }
  return layout;
}

function layoutFor(root: string): RunLayout {
  const runtime = join(root, "runtime");
  const evidence = join(root, "evidence");
  return {
    root,
    runtime,
    workflow: join(root, "workflow"),
    outputs: join(root, "outputs"),
    evidence,
    library: join(runtime, "library"),
    privateStatePath: join(runtime, "real-material-acceptance-private.json"),
    reviewPacketPath: join(evidence, "review-packet.json"),
    publicReportPath: join(evidence, "public-report.json"),
    publicMarkdownPath: join(evidence, "public-report.md"),
  };
}

async function assertReviewPacketCurrent(state: PrivateState): Promise<void> {
  const bytes = await readFile(state.reviewPacketPath);
  if (sha256(bytes) !== state.reviewPacketDigest) throw new Error("real-material-review-packet-stale");
}

async function writePublicRecords(roots: RunLayout, report: RealMaterialRunReport): Promise<void> {
  if (report.publishedClaimed !== false) throw new Error("real-material-publish-claim-forbidden");
  await writePublicFile(roots.publicReportPath, `${canonicalJson(report)}\n`);
  const lines = [
    "# Real material acceptance",
    "",
    `- Run: ${report.runId}`,
    `- Status: ${report.status}`,
    `- Evidence: ${report.evidenceScope}`,
    `- Stories ready: ${report.storyResults.filter(({ status }) => status === "ready").length}`,
    `- Outputs: ${report.outputs.length}`,
    "- Published: false",
    "",
  ];
  await writePublicFile(roots.publicMarkdownPath, lines.join("\n"));
}

async function readPrivateState(path: string): Promise<PrivateState | null> {
  try {
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
      throw new Error("real-material-private-state-invalid");
    }
    return JSON.parse(await readFile(path, "utf8")) as PrivateState;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

async function writePrivateState(path: string, state: PrivateState): Promise<void> {
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, `${canonicalJson(state)}\n`, { mode: 0o600, flag: "wx" });
  await chmod(temporary, 0o600);
  await rename(temporary, path);
}

async function writePublicFile(path: string, contents: string): Promise<void> {
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, contents, { mode: 0o600, flag: "wx" });
  await chmod(temporary, 0o600);
  await rename(temporary, path);
}

async function readMode0600File(path: string, code: string): Promise<Buffer> {
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
    throw new Error(code);
  }
  return readFile(path);
}

async function safeRemoveOutput(outputRoot: string, candidate: string): Promise<void> {
  const canonicalRoot = await realpath(outputRoot);
  const target = resolve(candidate);
  const within = relative(canonicalRoot, target);
  if (!within || within === ".." || within.startsWith(`..${sep}`)) {
    throw new Error("real-material-render-cleanup-forbidden");
  }
  await rm(target, { recursive: true, force: true });
}

function evidenceScope(customerId: string): "local-real-media" | "local-synthetic-test" {
  return customerId.startsWith("test-") ? "local-synthetic-test" : "local-real-media";
}

function referenceSourceKind(
  policy: "customer-owned" | "external-style-only",
): "self-owned" | "external-style-only" {
  return policy === "customer-owned" ? "self-owned" : "external-style-only";
}

function assertMediaTools(): void {
  if (!ffmpegPath || typeof ffprobePath !== "string") throw new Error("real-material-media-tools-unavailable");
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function normalizeEvidenceCode(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function styleFactVerified(
  rule: Awaited<ReturnType<ReferenceGuidedEditor["analyze"]>>["rules"][number],
  analysis: Awaited<ReturnType<ReferenceGuidedEditor["analyze"]>>,
  entry: Awaited<ReturnType<StudioAnalysisJobService["history"]>>[number],
): boolean {
  const receipt = entry.receipts.find((candidate) =>
    candidate.layer === "scene" && candidate.status === "succeeded"
    && candidate.providerMode === "local" && !candidate.synthetic);
  if (!receipt) return false;
  const scenes = evidenceRows(receipt.qualitySignals.scenes);
  if (rule.kind === "rhythm") {
    const actual = scenes.map((scene) => ({ startMs: scene.startMs, endMs: scene.endMs }));
    const expected = analysis.sections.map(({ startMs, endMs }) => ({ startMs, endMs }));
    return canonicalJson(actual) === canonicalJson(expected);
  }
  if (rule.kind === "transition") {
    const reference = analysis.transitions.find((candidate) => rule.evidenceIds.includes(candidate.id));
    const transitions = evidenceRows(receipt.qualitySignals.transitionEvidence);
    return Boolean(reference && transitions.some((candidate) =>
      candidate.kind === reference.kind && candidate.atMs === reference.atMs));
  }
  return false;
}

function evidenceRows(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> =>
    typeof item === "object" && item !== null && !Array.isArray(item));
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(sortValue(value));
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, sortValue(item)]));
  }
  return value;
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function run(command: string, args: readonly string[]): Promise<string> {
  return new Promise((resolveRun, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolveRun(Buffer.concat(stdout).toString("utf8"))
      : reject(new Error(Buffer.concat(stderr).toString("utf8"))));
  });
}
