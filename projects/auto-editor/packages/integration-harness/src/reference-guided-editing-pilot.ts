import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { chmod, mkdir, mkdtemp, open, readFile, realpath, rename, rm } from "node:fs/promises";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  type ReferenceMaterialMappingPlan,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceStyleAnalysis,
  type ReferenceStyleSelection,
  type ReferenceVariantKind,
  type StudioMediaAsset,
} from "@wearable/contracts";
import {
  CustomerMaterialLibrary,
  createFfprobeStudioSourceIntervalResolver,
  createReferenceMaterialMappingPlan,
  interpretReferencePromptRevision,
  JsonReferenceSampleStore,
  LocalReferenceGrammarAdapter,
  LocalProcessStudioModelWorker,
  mergeStudioEvidence,
  ReferenceGuidedEditor,
  ReferenceSampleService,
  ReferenceWithdrawalCoordinator,
  renderReferenceVariants,
  selectFinalReferenceAnalysisAttempt,
  StudioAnalysisJobService,
  type FinalReferenceAnalysisAttempt,
  type ReferenceMappingAssetSnapshot,
  type ReferenceEditorRenderRequest,
  type ReferenceRenderAsset,
  type StudioAnalysisHistoryEntry,
  type StudioScope,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

const require = createRequire(import.meta.url);
const bundledFfprobePath = (require("ffprobe-static") as { path?: unknown }).path;
const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageDirectory, "../..");
const workerRoot = join(repositoryRoot, "tools", "media-model-worker");
const workerPython = join(workerRoot, ".venv", "bin", "python");
const CREATED_AT = Date.parse("2026-09-21T00:00:00.000Z");
const VARIANT_KINDS: readonly ReferenceVariantKind[] = [
  "editorial-master",
  "xiaohongshu",
  "douyin",
  "wechat-channels",
];

export type ReferenceGuidedEditingPilotReport = Readonly<{
  mappingCounts: Readonly<{ matched: number; substituted: number; unachievable: number }>;
  variants: ReadonlyArray<Readonly<{ kind: ReferenceVariantKind; ffprobeDecodable: boolean }>>;
  referenceFixture: Readonly<{
    hardCut: true;
    fade: true;
    crossfade: true;
    audioPulseCount: 8;
  }>;
  lineageExact: boolean;
  referenceBytesReused: false;
  oldDraftsInvalidatedAfterStyleChange: boolean;
  platformRevisionExact: boolean;
  allDraftsApproved: boolean;
  approvalReplayExact: boolean;
  withdrawalFailClosed: boolean;
  publishedClaimed: false;
  evidenceScope: "local-synthetic";
  runRecordPath: string;
}>;

type MediaProbe = Readonly<{
  durationSeconds: number;
  videoCodec: string;
  audioCodec: string | null;
  width: number;
  height: number;
  videoStreamCount: number;
  audioStreamCount: number;
}>;

type ReferenceSnapshot = Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>;
type ReferenceExecutionReport = Awaited<ReturnType<ReferenceGuidedEditor["render"]>>;
type ReferenceArtifact = ReferenceExecutionReport["artifacts"][number];

/** Runs the Task 9 local-synthetic proof through exported production service boundaries. */
export async function runReferenceGuidedEditingPilot(
  workDirValue: string,
): Promise<ReferenceGuidedEditingPilotReport> {
  if (!ffmpegPath || typeof bundledFfprobePath !== "string") {
    throw new Error("reference-pilot-media-tools-unavailable");
  }
  const workDir = resolve(workDirValue);
  const attemptsRoot = join(workDir, "attempts");
  const evidenceRoot = join(workDir, "evidence");
  await Promise.all([
    mkdir(attemptsRoot, { recursive: true }),
    mkdir(evidenceRoot, { recursive: true }),
  ]);
  const runtimeRoot = await realpath(await mkdtemp(join(attemptsRoot, "attempt-")));
  const attemptId = basename(runtimeRoot);
  const inputRoot = join(runtimeRoot, "inputs");
  const workflowRoot = join(runtimeRoot, "workflow");
  const outputRoot = join(runtimeRoot, "outputs");
  await Promise.all([
    mkdir(inputRoot, { recursive: true }),
    mkdir(workflowRoot, { recursive: true }),
    mkdir(outputRoot, { recursive: true }),
  ]);

  const referencePath = join(inputRoot, "synthetic-reference.mp4");
  await makeSyntheticReference(referencePath, ffmpegPath);
  const customerPaths = await makeCustomerClips(inputRoot, ffmpegPath);
  const referenceBytes = await readFile(referencePath);
  const customerBytes = await Promise.all(customerPaths.map((path) => readFile(path)));
  const referenceDigest = sha256(referenceBytes);
  const inputDigests = customerBytes.map(sha256);
  if (new Set([referenceDigest, ...inputDigests]).size !== 5) {
    throw new Error("reference-pilot-input-digests-not-distinct");
  }

  const scope: StudioScope = {
    actorId: "reference-pilot-operator",
    customerId: "reference-pilot-customer",
    sessionId: "reference-pilot-session",
  };
  let clockTick = 0;
  const now = () => new Date(CREATED_AT + clockTick++ * 1_000).toISOString();
  const library = new CustomerMaterialLibrary(join(runtimeRoot, "library"), ({ actorId, customerId }) =>
    actorId === scope.actorId && customerId === scope.customerId);
  const customerAssets: StudioMediaAsset[] = [];
  for (const [index, bytes] of customerBytes.entries()) {
    customerAssets.push(await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: `reference-pilot-customer-clip-${index + 1}`,
      bytes,
      mediaKind: "video",
      fileName: `customer-clip-${index + 1}.mp4`,
      capturedAt: new Date(CREATED_AT + index * 1_000).toISOString(),
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    }));
  }

  const sampleService = new ReferenceSampleService(library, workflowRoot, { now });
  const registration = await sampleService.importDedicated(scope, {
    requestId: "reference-pilot-import",
    fileName: "synthetic-reference.mp4",
    bytes: referenceBytes,
    sourceKind: "external-style-only",
    operationKey: "reference-pilot-import",
  });
  const store = await JsonReferenceSampleStore.open(workflowRoot, scope);
  const modelWorker = new LocalProcessStudioModelWorker({
    command: "/usr/bin/env",
    args: [
      "PYTHONDONTWRITEBYTECODE=1",
      workerPython,
      "-B",
      "worker.py",
      "--ffprobe-path", bundledFfprobePath,
      "--ffmpeg-path", ffmpegPath,
    ],
    cwd: workerRoot,
    timeoutMs: 120_000,
    maxStdoutBytes: 256 * 1024,
    maxStderrBytes: 8 * 1024,
    allowedMediaRoot: runtimeRoot,
  });
  const analysisJobs = new StudioAnalysisJobService(library, modelWorker, {
    now,
    resolveSourceInterval: createFfprobeStudioSourceIntervalResolver(bundledFfprobePath),
  });
  const grammar = new LocalReferenceGrammarAdapter({ now });
  const analysisAttemptHolder: {
    value: Readonly<{
      entry: StudioAnalysisHistoryEntry;
      attempt: FinalReferenceAnalysisAttempt;
    }> | null;
  } = { value: null };
  const renderAssets = await Promise.all(customerAssets.map(async (asset) => {
    const bytes = await library.readOriginal(scope.actorId, scope.customerId, asset.id);
    return renderAsset(scope, asset, bytes);
  }));
  const materialPipeline: Array<Readonly<{
    entry: StudioAnalysisHistoryEntry;
    merged: ReturnType<typeof mergeStudioEvidence>;
  }>> = [];

  const withCurrentAssetAuthorization = <T>(
    request: Readonly<{
      scope: StudioScope;
      sample: ReferenceSample;
      mappingPlan: ReferenceMaterialMappingPlan;
    }>,
    work: () => Promise<T>,
  ) => library.withReferenceWorkflowAssets(
    request.scope.actorId,
    request.scope.customerId,
    referenceWorkflowAssetBindings(request.sample, request.mappingPlan),
    work,
  );
  const renderRequest = (
    request: ReferenceEditorRenderRequest,
    revisionOverride?: ReturnType<typeof interpretReferencePromptRevision>,
  ) => withCurrentAssetAuthorization(request, async () => {
    const rendered = await renderReferenceVariants({
      mappingPlan: request.mappingPlan,
      rights: request.rights,
      analysis: request.analysis,
      assets: renderAssets,
      outputRoot,
      operationKey: request.operationKey,
      now: now(),
      ffmpegPath,
      ffprobePath: bundledFfprobePath,
      ...(revisionOverride ? { revisionOverride } : {}),
    });
    return { ...rendered, outputDirectory: dirname(rendered.drafts[0]!.outputPath) };
  });
  const editor = new ReferenceGuidedEditor({
    store,
    now,
    analyze: async ({ sample, rights, operationKey }) => {
      const source = await library.inspectReferenceAnalysisAsset(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        sample.sourceAssetId,
      );
      const entry = await analysisJobs.enqueueReference(
        scope,
        source,
        ["scene", "transcript", "vision"],
        operationKey,
      );
      const attempt = selectFinalReferenceAnalysisAttempt(entry, {
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        sourceAssetId: sample.sourceAssetId,
        sourceDigest: sample.sourceDigest,
        sourcePath: source.sourcePath,
        sourceStartMs: entry.job.sourceStartMs,
        sourceEndMs: entry.job.sourceEndMs,
      });
      const receipt = attempt.evidenceReceipts.find((candidate) => candidate.layer === "scene");
      const beatMarkers = receipt?.qualitySignals.beatMarkers;
      if (!receipt || receipt.status !== "succeeded" || receipt.synthetic || receipt.providerMode !== "local") {
        throw new Error("reference-pilot-real-scene-evidence-required");
      }
      if (!Array.isArray(beatMarkers) || beatMarkers.length !== 8) {
        throw new Error("reference-pilot-eight-audio-pulses-not-detected");
      }
      const optionalReceipts = attempt.allReceipts.filter((candidate) => candidate.layer !== "scene");
      if (
        optionalReceipts.length !== 2
        || optionalReceipts.some((candidate) =>
          candidate.status !== "failed-manual-review"
          || candidate.failure?.code !== "studio-model-adapter-not-configured")
        || attempt.evidenceReceipts.length !== 1
      ) throw new Error("reference-pilot-optional-failure-evidence-missing");
      analysisAttemptHolder.value = { entry, attempt };
      return grammar.extractGrammar({
        scope,
        sample,
        rights,
        source,
        receipts: attempt.evidenceReceipts,
      });
    },
    createMapping: async ({ scope: mappingScope, sample, rights, analysis, selection }) =>
      createReferenceMaterialMappingPlan({
        scope: mappingScope,
        sample,
        rights,
        analysis,
        selection,
        currentMaterials: await library.inventoryAt(
          mappingScope.actorId,
          mappingScope.customerId,
          selection.confirmedAt,
        ),
        authorizedHistoricalMaterials: [],
        assets: renderAssets,
      }),
    render: renderRequest,
    revise: async (request) => {
      if (!request.target || !request.prompt) throw new Error("reference-pilot-revision-input-missing");
      return renderRequest(request, interpretReferencePromptRevision({
        prompt: request.prompt,
        target: request.target,
      }));
    },
    withCurrentAssetAuthorization,
    readDraftOutputDigest: async ({ draft }) => sha256(await readFile(draft.outputPath)),
    cleanupRenderedOutput: async (result) => {
      const canonicalRoot = await realpath(outputRoot);
      const candidate = resolve(result.outputDirectory);
      const within = relative(canonicalRoot, candidate);
      if (!within || within === ".." || within.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`)) {
        throw new Error("reference-pilot-render-cleanup-forbidden");
      }
      await rm(candidate, { recursive: true, force: true });
    },
  });

  const rights = await editor.declareRights(scope, {
    referenceSampleId: registration.sample.id,
    sourceKind: "external-style-only",
    reusableElements: [],
  }, "reference-pilot-rights");
  const readySample = latestById((await store.snapshot()).samples, registration.sample.id);
  if (!readySample || readySample.status !== "ready") throw new Error("reference-pilot-sample-not-ready");
  const analysis = await editor.analyze(scope, readySample.id, "reference-pilot-analysis");
  for (const [index, asset] of customerAssets.entries()) {
    const entry = await analysisJobs.enqueue(
      scope,
      asset.id,
      ["technical", "scene"],
      `reference-pilot-material-analysis-${index + 1}`,
    );
    if (entry.job.status !== "succeeded" || entry.receipts.length !== 2) {
      throw new Error("reference-pilot-material-analysis-failed");
    }
    const merged = mergeStudioEvidence({
      scope,
      asset,
      sourcePath: entry.job.sourcePath,
      sourceStartMs: entry.job.sourceStartMs,
      sourceEndMs: entry.job.sourceEndMs,
      contentType: "single-product",
      receipts: [...entry.receipts],
      humanFacts: confirmedMaterialFacts(analysis, index, entry),
      createdAt: now(),
      version: 1,
    });
    if (!merged.card.technicalUsable) {
      throw new Error("reference-pilot-material-technical-evidence-insufficient");
    }
    await library.appendMaterialCard(scope.actorId, scope.customerId, merged.card);
    materialPipeline.push({ entry, merged });
  }
  const selection = await editor.confirmStyle(
    scope,
    analysis.id,
    analysis.rules.map((rule) => rule.id),
    "reference-pilot-style-v1",
  );
  const draftMapping = await editor.createMappingPlan(
    scope,
    readySample.id,
    "reference-pilot-mapping-v1",
  );
  const confirmedMapping = await editor.confirmMappingPlan(
    scope,
    draftMapping.id,
    "reference-pilot-mapping-confirm-v1",
  );
  const report = await editor.render(scope, confirmedMapping.id, "reference-pilot-render-v1");
  const renderedSnapshot = await store.snapshot();
  const renderedDrafts = report.artifacts.map((artifact) => {
    const draft = renderedSnapshot.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
    if (!draft) throw new Error("reference-pilot-draft-missing");
    return draft;
  });
  const outputProbes = await Promise.all(renderedDrafts.map((draft) => probeMedia(
    draft.outputPath,
    bundledFfprobePath,
  )));

  const lineageExact = verifyExactLineage(
    readySample,
    rights,
    analysis,
    selection,
    confirmedMapping,
    report,
    renderedSnapshot,
  );
  const referenceBytesReused = renderedDrafts.some((draft) =>
    draft.lineage.some((row) =>
      row.sourceAssetId === readySample.sourceAssetId || row.sourceDigest === readySample.sourceDigest));
  if (referenceBytesReused) throw new Error("external-reference-content-forbidden");

  if (analysis.rules.length < 2) throw new Error("reference-pilot-style-change-unavailable");
  const replacementSelection = await editor.confirmStyle(
    scope,
    analysis.id,
    analysis.rules.slice(0, -1).map((rule) => rule.id),
    "reference-pilot-style-v2",
  );
  const invalidatedSnapshot = await store.snapshot();
  const invalidationEvidence = buildInvalidationEvidence({
    priorSelection: selection,
    replacementSelection,
    priorMapping: confirmedMapping,
    priorReport: report,
    artifacts: report.artifacts,
    renderedSnapshot,
    finalSnapshot: invalidatedSnapshot,
  });
  const oldDraftsInvalidatedAfterStyleChange = invalidationEvidence.finalSnapshotVerified;

  const revisionMappingDraft = await editor.createMappingPlan(
    scope,
    readySample.id,
    "reference-pilot-mapping-v2",
  );
  const revisionMapping = await editor.confirmMappingPlan(
    scope,
    revisionMappingDraft.id,
    "reference-pilot-mapping-confirm-v2",
  );
  const revisionBaseReport = await editor.render(
    scope,
    revisionMapping.id,
    "reference-pilot-render-v2",
  );
  const revisionTargetKind = "wechat-channels" as const;
  const revisionPrompt = "开头缩短 1 秒" as const;
  const priorRevisionArtifact = requireReportArtifact(revisionBaseReport, revisionTargetKind);
  const revisionInput = {
    draftId: priorRevisionArtifact.draftId,
    reportId: revisionBaseReport.id,
    reportVersion: revisionBaseReport.version,
    draftVersion: priorRevisionArtifact.draftVersion,
    draftDigest: priorRevisionArtifact.outputDigest,
    prompt: revisionPrompt,
  };
  const revisedReport = await editor.reviseWithPrompt(
    scope,
    revisionInput,
    "reference-pilot-revise-xiaohongshu-v1",
  );
  const revisedArtifact = requireReportArtifact(revisedReport, revisionTargetKind);
  const platformRevisionExact = revisedReport.version > revisionBaseReport.version
    && revisedArtifact.draftId !== priorRevisionArtifact.draftId
    && VARIANT_KINDS.filter((kind) => kind !== revisionTargetKind).every((kind) =>
      requireReportArtifact(revisedReport, kind).draftId ===
        requireReportArtifact(revisionBaseReport, kind).draftId);
  if (!platformRevisionExact) throw new Error("reference-pilot-platform-revision-not-exact");

  const approvalEvidence = [];
  let approvalReplayExact = true;
  for (const kind of VARIANT_KINDS) {
    const artifact = requireReportArtifact(revisedReport, kind);
    const input = {
      draftId: artifact.draftId,
      reportId: revisedReport.id,
      reportVersion: revisedReport.version,
      draftVersion: artifact.draftVersion,
      draftDigest: artifact.outputDigest,
    };
    const operationKey = `reference-pilot-approve-${kind}`;
    const approval = await editor.approveDraft(scope, input, operationKey);
    const replay = await editor.approveDraft(scope, input, operationKey);
    const exactReplay = canonicalJson(replay) === canonicalJson(approval);
    approvalReplayExact = approvalReplayExact && exactReplay;
    approvalEvidence.push({
      kind,
      approval,
      draftBinding: approval.bindings.draft,
      exactReplay,
    });
  }
  if (!approvalReplayExact) throw new Error("reference-pilot-approval-replay-mismatch");
  const mobileApprovalReview = await withCurrentAssetAuthorization({
    scope,
    sample: readySample,
    mappingPlan: revisionMapping,
  }, () => editor.mobileReview(scope, readySample.id));
  const allDraftsApproved = mobileApprovalReview.status === "approved"
    && canonicalJson(mobileApprovalReview.approval.approvedArtifactKinds) === canonicalJson(VARIANT_KINDS)
    && mobileApprovalReview.publishClaimed === false;
  if (!allDraftsApproved) throw new Error("reference-pilot-mobile-approval-incomplete");

  const withdrawalCoordinator = new ReferenceWithdrawalCoordinator({ library, editor });
  const withdrawal = await withdrawalCoordinator.withdraw(
    scope,
    readySample.sourceAssetId,
    "reference-pilot-withdraw-sample-source",
  );
  const withdrawnSnapshot = await store.snapshot();
  const approvalIds = new Set(approvalEvidence.map((item) => item.approval.id));
  const approvalAuditInvalidated = [...approvalIds].every((approvalId) =>
    withdrawnSnapshot.approvals.some((approval) =>
      approval.id === approvalId && approval.status === "invalidated"));
  if (!approvalAuditInvalidated) throw new Error("reference-pilot-approval-invalidation-audit-missing");

  const finalMaster = requireReportArtifact(revisedReport, "editorial-master");
  const failClosedErrors = {
    mobileReview: await captureError(() => withCurrentAssetAuthorization({
      scope, sample: readySample, mappingPlan: revisionMapping,
    }, () => editor.mobileReview(scope, readySample.id))),
    revise: await captureError(() => withCurrentAssetAuthorization({
      scope, sample: readySample, mappingPlan: revisionMapping,
    }, () => editor.reviseWithPrompt(scope, revisionInput, "reference-pilot-revise-after-withdrawal"))),
    approve: await captureError(() => withCurrentAssetAuthorization({
      scope, sample: readySample, mappingPlan: revisionMapping,
    }, () => editor.approveDraft(scope, {
      draftId: finalMaster.draftId,
      reportId: revisedReport.id,
      reportVersion: revisedReport.version,
      draftVersion: finalMaster.draftVersion,
      draftDigest: finalMaster.outputDigest,
    }, "reference-pilot-approve-after-withdrawal"))),
    media: await captureError(() => withCurrentAssetAuthorization({
      scope, sample: readySample, mappingPlan: revisionMapping,
    }, async () => {
      const snapshot = await store.snapshot();
      const draft = snapshot.drafts.find((candidate) =>
        candidate.id === finalMaster.draftId && candidate.version === finalMaster.draftVersion);
      if (!draft) throw new Error("reference-pilot-current-draft-missing");
      await readFile(draft.outputPath);
    })),
  };
  const expectedWithdrawalError = "reference-workflow-asset-withdrawn";
  const withdrawalFailClosed = Object.values(failClosedErrors).every((code) =>
    code === expectedWithdrawalError);
  if (!withdrawalFailClosed) throw new Error("reference-pilot-withdrawal-did-not-fail-closed");

  const mappingCounts = report.mappingCounts;
  if (
    mappingCounts.matched !== 2
    || mappingCounts.substituted !== 1
    || mappingCounts.unachievable !== 1
    || confirmedMapping.mappings.length !== 4
  ) {
    throw new Error("reference-pilot-mapping-counts-mismatch");
  }
  if (!lineageExact || !oldDraftsInvalidatedAfterStyleChange) {
    throw new Error("reference-pilot-lineage-or-invalidation-mismatch");
  }
  if (report.publishClaimed || renderedDrafts.some((draft) => draft.publishClaimed)) {
    throw new Error("reference-pilot-publish-claim-forbidden");
  }
  const retainedAttempt = analysisAttemptHolder.value;
  if (!retainedAttempt) throw new Error("reference-pilot-analysis-attempt-missing");
  const retainedSceneReceipt = retainedAttempt.attempt.evidenceReceipts[0];
  if (!retainedSceneReceipt || retainedSceneReceipt.layer !== "scene") {
    throw new Error("reference-pilot-scene-receipt-missing");
  }

  const inputPaths = [referencePath, ...customerPaths];
  const inputBytes = [referenceBytes, ...customerBytes];
  const runRecordPath = join(evidenceRoot, "reference-guided-editing-run.json");
  const outputs = renderedDrafts.map((draft, index) => ({
    kind: report.artifacts[index]!.kind,
    path: draft.outputPath,
    digest: draft.outputDigest,
    ffprobe: outputProbes[index]!,
    lineage: draft.lineage,
    artifact: report.artifacts[index]!,
    variantPlan: exactVariantPlanEvidence(renderedSnapshot, report.artifacts[index]!),
    draft: exactDraftEvidence(draft),
    executionReport: {
      id: report.id,
      version: report.version,
      status: report.status,
      bindings: report.bindings,
    },
  }));
  await writePrivateJson(runRecordPath, {
    schemaVersion: 1,
    evidenceScope: "local-synthetic",
    generatedAt: now(),
    attempt: { id: attemptId, runtimeRoot },
    inputs: inputPaths.map((path, index) => ({
      kind: index === 0 ? "external-style-only-reference" : `customer-clip-${index}`,
      path,
      digest: sha256(inputBytes[index]!),
      byteLength: inputBytes[index]!.byteLength,
    })),
    referenceFixture: {
      generator: "ffmpeg-lavfi",
      hardCut: true,
      fade: true,
      crossfade: true,
      audioPulseCount: 8,
      detectedBeatMarkers: retainedSceneReceipt.qualitySignals.beatMarkers,
      sceneReceiptJobId: retainedSceneReceipt.jobId,
      sceneReceiptInputDigest: retainedSceneReceipt.inputDigest,
    },
    analysisPipeline: {
      job: retainedAttempt.entry.job,
      allReceipts: retainedAttempt.attempt.allReceipts,
      evidenceReceipts: retainedAttempt.attempt.evidenceReceipts,
      evidenceReceiptIds: retainedAttempt.attempt.evidenceReceipts.map((receipt) => receipt.jobId),
      analysis: {
        id: analysis.id,
        version: analysis.version,
        contentDigest: analysis.contentDigest,
        status: analysis.status,
        warnings: analysis.warnings,
        confidence: analysis.confidence,
        capability: analysis.capability,
        cuts: analysis.cuts,
        sections: analysis.sections,
        transcriptSpans: analysis.transcriptSpans,
        beatMarkers: analysis.beatMarkers,
        transitions: analysis.transitions,
        rules: analysis.rules,
      },
    },
    materialPipeline: materialPipeline.map(({ entry, merged }) => ({
      job: entry.job,
      receipts: entry.receipts,
      card: merged.card,
      evidence: merged.evidence,
      degradedReasons: merged.degradedReasons,
    })),
    mappingCounts,
    mappings: confirmedMapping.mappings,
    versionBindings: {
      sample: { id: readySample.id, version: readySample.version, sourceDigest: readySample.sourceDigest },
      rights: { id: rights.id, version: rights.version, contentDigest: rights.contentDigest },
      analysis: { id: analysis.id, version: analysis.version, contentDigest: analysis.contentDigest },
      styleSelection: { id: selection.id, version: selection.version, contentDigest: selection.contentDigest },
      mappingPlan: {
        id: confirmedMapping.id,
        version: confirmedMapping.version,
        contentDigest: confirmedMapping.contentDigest,
        permissionSetDigest: confirmedMapping.permissionSetDigest,
      },
      executionReport: { id: report.id, version: report.version },
      exact: lineageExact,
    },
    outputs,
    referenceBytesReused: false,
    invalidationEvidence,
    oldDraftsInvalidatedAfterStyleChange,
    revisionEvidence: {
      prompt: revisionPrompt,
      targetKind: revisionTargetKind,
      priorReport: { id: revisionBaseReport.id, version: revisionBaseReport.version },
      priorDraft: {
        id: priorRevisionArtifact.draftId,
        version: priorRevisionArtifact.draftVersion,
        outputDigest: priorRevisionArtifact.outputDigest,
      },
      revisedReport: { id: revisedReport.id, version: revisedReport.version },
      revisedDraft: {
        id: revisedArtifact.draftId,
        version: revisedArtifact.draftVersion,
        outputDigest: revisedArtifact.outputDigest,
      },
    },
    approvalEvidence,
    mobileApprovalEvidence: {
      status: mobileApprovalReview.status,
      approvedArtifactKinds: mobileApprovalReview.approval.approvedArtifactKinds,
      publishClaimed: mobileApprovalReview.publishClaimed,
    },
    withdrawalEvidence: {
      sourceAssetId: readySample.sourceAssetId,
      withdrawal,
      approvalAuditInvalidated,
      failClosedErrors,
    },
    publishedClaimed: false,
  });

  return {
    mappingCounts,
    variants: outputs.map((output) => ({
      kind: output.kind,
      ffprobeDecodable: output.ffprobe.videoStreamCount === 1
        && output.ffprobe.audioStreamCount === 1
        && output.ffprobe.videoCodec === "h264"
        && output.ffprobe.audioCodec === "aac"
        && output.ffprobe.width === 720
        && output.ffprobe.height === 1280
        && output.ffprobe.durationSeconds >= 30
        && output.ffprobe.durationSeconds <= 60,
    })),
    referenceFixture: { hardCut: true, fade: true, crossfade: true, audioPulseCount: 8 },
    lineageExact,
    referenceBytesReused: false,
    oldDraftsInvalidatedAfterStyleChange,
    platformRevisionExact,
    allDraftsApproved,
    approvalReplayExact,
    withdrawalFailClosed,
    publishedClaimed: false,
    evidenceScope: "local-synthetic",
    runRecordPath,
  };
}

function referenceWorkflowAssetBindings(
  sample: ReferenceSample,
  mappingPlan: ReferenceMaterialMappingPlan,
) {
  const renderSources = new Map(mappingPlan.mappings.flatMap((mapping) => {
    if (!mapping.sourceAssetId) return [];
    if (!mapping.sourceDigest || !mapping.permission
        || mapping.permission.view !== "allowed" || mapping.permission.use !== "allowed") {
      throw new Error("reference-pilot-render-source-binding-missing");
    }
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

function requireReportArtifact(
  report: ReferenceExecutionReport,
  kind: ReferenceVariantKind,
): ReferenceArtifact {
  const artifact = report.artifacts.find((candidate) => candidate.kind === kind);
  if (!artifact) throw new Error("reference-pilot-report-artifact-missing");
  return artifact;
}

async function captureError(work: () => Promise<unknown>): Promise<string> {
  try {
    await work();
  } catch (error) {
    return error instanceof Error ? error.message.split(":", 1)[0]! : String(error);
  }
  throw new Error("reference-pilot-expected-failure-missing");
}

function confirmedMaterialFacts(
  analysis: ReferenceStyleAnalysis,
  materialIndex: number,
  entry: StudioAnalysisHistoryEntry,
): string[] {
  const receipt = entry.receipts.find((candidate) => candidate.layer === "scene");
  if (!receipt || receipt.status !== "succeeded" || receipt.providerMode !== "local" || receipt.synthetic) {
    throw new Error("reference-pilot-material-real-scene-evidence-required");
  }
  const scenes = evidenceRows(receipt.qualitySignals.scenes, "reference-pilot-material-scenes-invalid");
  const transitions = evidenceRows(
    receipt.qualitySignals.transitionEvidence,
    "reference-pilot-material-transitions-invalid",
  );

  if (materialIndex === 0) {
    const rhythmRule = analysis.rules.find((rule) => rule.kind === "rhythm");
    if (!rhythmRule) throw new Error("reference-pilot-rhythm-rule-missing");
    const actualIntervals = scenes.map((scene) => ({
      startMs: evidenceMillisecond(scene.startMs),
      endMs: evidenceMillisecond(scene.endMs),
    }));
    const referenceIntervals = analysis.sections.map((section) => ({
      startMs: section.startMs,
      endMs: section.endMs,
    }));
    if (canonicalJson(actualIntervals) !== canonicalJson(referenceIntervals)) {
      throw new Error("reference-pilot-rhythm-evidence-mismatch");
    }
    return [`reference-rhythm:${normalizeEvidenceCode(rhythmRule.id)}`];
  }

  if (materialIndex === 1) {
    const match = referenceTransitionRule(analysis, "cut");
    if (!transitions.some((transition) =>
      transition.kind === "cut" && evidenceMillisecond(transition.atMs) === match.transition.atMs)) {
      throw new Error("reference-pilot-cut-evidence-mismatch");
    }
    return [`reference-transition:cut:${normalizeEvidenceCode(match.rule.id)}`];
  }

  if (materialIndex === 2) {
    const match = referenceTransitionRule(analysis, "fade");
    const lateFade = transitions.find((transition) =>
      transition.kind === "fade"
      && evidenceMillisecond(transition.atMs) > match.transition.atMs + 1_000);
    if (!lateFade) throw new Error("reference-pilot-late-fade-evidence-mismatch");
    return [];
  }

  return [];
}

function referenceTransitionRule(
  analysis: ReferenceStyleAnalysis,
  kind: "cut" | "fade",
): Readonly<{
  rule: ReferenceStyleAnalysis["rules"][number];
  transition: ReferenceStyleAnalysis["transitions"][number];
}> {
  const transition = analysis.transitions.find((candidate) => candidate.kind === kind);
  const rule = transition && analysis.rules.find((candidate) =>
    candidate.kind === "transition" && candidate.evidenceIds.includes(transition.id));
  if (!transition || !rule) throw new Error(`reference-pilot-${kind}-rule-missing`);
  return { rule, transition };
}

function evidenceRows(value: unknown, code: string): ReadonlyArray<Readonly<Record<string, unknown>>> {
  if (!Array.isArray(value) || value.some((row) => !isRecord(row))) throw new Error(code);
  return value as ReadonlyArray<Readonly<Record<string, unknown>>>;
}

function evidenceMillisecond(value: unknown): number {
  if (!Number.isSafeInteger(value)) throw new Error("reference-pilot-evidence-millisecond-invalid");
  return value as number;
}

function normalizeEvidenceCode(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function renderAsset(scope: StudioScope, asset: StudioMediaAsset, bytes: Uint8Array): ReferenceRenderAsset {
  const permission: ReferenceMappingAssetSnapshot["permission"] = {
    id: `reference_permission_${sha256(`${scope.customerId}\0${scope.sessionId}\0${asset.id}\0${asset.version}`)}`,
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

function verifyExactLineage(
  sample: ReferenceSample,
  rights: ReferenceRightsDeclaration,
  analysis: ReferenceStyleAnalysis,
  selection: ReferenceStyleSelection,
  mapping: ReferenceMaterialMappingPlan,
  report: Awaited<ReturnType<ReferenceGuidedEditor["render"]>>,
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
): boolean {
  const bindings = report.bindings;
  const exactBindings = canonicalJson(bindings.sample) === canonicalJson(sampleBinding(sample))
    && canonicalJson(bindings.rights) === canonicalJson(rightsBinding(sample, rights))
    && canonicalJson(bindings.analysis) === canonicalJson(scopedBinding(analysis))
    && bindings.styleSelection.id === selection.id
    && bindings.styleSelection.version === selection.version
    && bindings.styleSelection.contentDigest === selection.contentDigest
    && bindings.mappingPlan.id === mapping.id
    && bindings.mappingPlan.version === mapping.version
    && bindings.mappingPlan.contentDigest === mapping.contentDigest
    && bindings.mappingPlan.permissionSetDigest === mapping.permissionSetDigest;
  const mappingById = new Map(mapping.mappings.map((row) => [row.id, row]));
  const exactArtifacts = report.artifacts.length === 4 && report.artifacts.every((artifact) => {
    const plan = snapshot.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
    const draft = snapshot.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
    if (!plan || !draft || draft.outputDigest !== artifact.outputDigest || plan.kind !== artifact.kind) return false;
    return draft.lineage.every((row) => {
      const mapped = mappingById.get(row.mappingId);
      return Boolean(mapped
        && mapped.sourceBindingId === row.sourceBindingId
        && mapped.sourceAssetId === row.sourceAssetId
        && mapped.sourceDigest === row.sourceDigest);
    });
  });
  return exactBindings && exactArtifacts;
}

function exactVariantPlanEvidence(snapshot: ReferenceSnapshot, artifact: ReferenceArtifact) {
  const plan = snapshot.variantPlans.find((candidate) =>
    candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
  if (!plan || plan.status !== "rendered") throw new Error("reference-pilot-variant-plan-evidence-missing");
  return {
    id: plan.id,
    version: plan.version,
    contentDigest: sha256(canonicalJson(plan)),
    kind: plan.kind,
    status: plan.status,
    bindings: plan.bindings,
  };
}

function exactDraftEvidence(draft: ReferenceSnapshot["drafts"][number]) {
  return {
    id: draft.id,
    version: draft.version,
    status: draft.status,
    outputDigest: draft.outputDigest,
    bindings: draft.bindings,
  };
}

function buildInvalidationEvidence(input: Readonly<{
  priorSelection: ReferenceStyleSelection;
  replacementSelection: ReferenceStyleSelection;
  priorMapping: ReferenceMaterialMappingPlan;
  priorReport: ReferenceExecutionReport;
  artifacts: readonly ReferenceArtifact[];
  renderedSnapshot: ReferenceSnapshot;
  finalSnapshot: ReferenceSnapshot;
}>) {
  const persistedPriorSelection = input.finalSnapshot.styleSelections.find((candidate) =>
    candidate.id === input.priorSelection.id && candidate.version === input.priorSelection.version);
  const persistedReplacementSelection = input.finalSnapshot.styleSelections.find((candidate) =>
    candidate.id === input.replacementSelection.id && candidate.version === input.replacementSelection.version);
  const mappingTombstone = input.finalSnapshot.mappingPlans.find((candidate) =>
    candidate.id === input.priorMapping.id
    && candidate.version === input.priorMapping.version + 1
    && candidate.status === "invalidated");
  const reportTombstone = input.finalSnapshot.executionReports.find((candidate) =>
    candidate.id === input.priorReport.id
    && candidate.version === input.priorReport.version + 1
    && candidate.status === "invalidated");
  if (!persistedPriorSelection || persistedPriorSelection.status !== "confirmed"
      || !persistedReplacementSelection || persistedReplacementSelection.status !== "confirmed"
      || !mappingTombstone || !reportTombstone) {
    throw new Error("reference-pilot-upstream-invalidation-evidence-missing");
  }

  const drafts = input.artifacts.map((artifact) => {
    const priorPlan = input.renderedSnapshot.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
    const planTombstone = input.finalSnapshot.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId
      && candidate.version === artifact.variantPlanVersion + 1
      && candidate.status === "invalidated");
    const priorDraft = input.renderedSnapshot.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
    const draftTombstone = input.finalSnapshot.drafts.find((candidate) =>
      candidate.status === "invalidated"
      && candidate.outputDigest === artifact.outputDigest
      && candidate.outputPath === priorDraft?.outputPath
      && candidate.bindings.variantPlan.id === artifact.variantPlanId
      && candidate.bindings.variantPlan.version === planTombstone?.version);
    if (!priorPlan || priorPlan.status !== "rendered" || !planTombstone
        || !priorDraft || priorDraft.status !== "reviewable" || !draftTombstone) {
      throw new Error("reference-pilot-draft-invalidation-evidence-missing");
    }
    return {
      prior: exactDraftEvidence(priorDraft),
      variantPlanTombstone: {
        id: planTombstone.id,
        version: planTombstone.version,
        contentDigest: sha256(canonicalJson(planTombstone)),
        status: planTombstone.status,
      },
      tombstone: exactDraftEvidence(draftTombstone),
    };
  });
  if (new Set(drafts.map((item) => item.tombstone.id)).size !== VARIANT_KINDS.length) {
    throw new Error("reference-pilot-draft-invalidation-tombstones-not-distinct");
  }

  return {
    priorStyleSelection: {
      id: persistedPriorSelection.id,
      version: persistedPriorSelection.version,
      contentDigest: persistedPriorSelection.contentDigest,
      status: persistedPriorSelection.status,
    },
    replacementStyleSelection: {
      id: persistedReplacementSelection.id,
      version: persistedReplacementSelection.version,
      contentDigest: persistedReplacementSelection.contentDigest,
      status: persistedReplacementSelection.status,
    },
    mappingPlan: {
      prior: {
        id: input.priorMapping.id,
        version: input.priorMapping.version,
        contentDigest: input.priorMapping.contentDigest,
        permissionSetDigest: input.priorMapping.permissionSetDigest,
        status: input.priorMapping.status,
      },
      tombstone: {
        id: mappingTombstone.id,
        version: mappingTombstone.version,
        contentDigest: mappingTombstone.contentDigest,
        permissionSetDigest: mappingTombstone.permissionSetDigest,
        status: mappingTombstone.status,
      },
    },
    executionReport: {
      prior: {
        id: input.priorReport.id,
        version: input.priorReport.version,
        status: input.priorReport.status,
      },
      tombstone: {
        id: reportTombstone.id,
        version: reportTombstone.version,
        status: reportTombstone.status,
      },
    },
    drafts,
    finalSnapshotVerified: true as const,
  };
}

async function writePrivateJson(path: string, value: unknown): Promise<void> {
  const temporaryPath = join(dirname(path), `.reference-guided-editing-run-${randomUUID()}.tmp`);
  let handle: Awaited<ReturnType<typeof open>> | null = null;
  try {
    handle = await open(temporaryPath, "wx", 0o600);
    await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8" });
    await handle.chmod(0o600);
    await handle.sync();
    await handle.close();
    handle = null;
    await rename(temporaryPath, path);
    await chmod(path, 0o600);
  } finally {
    await handle?.close().catch(() => undefined);
    await rm(temporaryPath, { force: true }).catch(() => undefined);
  }
}

async function makeSyntheticReference(path: string, ffmpeg: string): Promise<void> {
  const pulses = [0.75, 2, 3.25, 4.5, 5.75, 7, 8.25, 9.5]
    .map((start) => `between(t\\,${start}\\,${(start + 0.08).toFixed(2)})`)
    .join("+");
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", "smptebars=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=3",
    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", `aevalsrc=if(gt(${pulses}\\,0)\\,0.8*sin(2*PI*880*t)\\,0):s=8000:d=11`,
    "-filter_complex",
    "[0:v]settb=AVTB,format=yuv420p[a];[1:v]settb=AVTB,format=yuv420p[b];" +
      "[2:v]settb=AVTB,format=yuv420p[c];[3:v]settb=AVTB,format=yuv420p[d];" +
      "[a][b]concat=n=2:v=1:a=0[ab];[ab]settb=AVTB[ab2];" +
      "[ab2][c]xfade=transition=fadeblack:duration=0.5:offset=5.5[abc];" +
      "[abc]settb=AVTB[abc2];[abc2][d]xfade=transition=fade:duration=0.5:offset=8,format=yuv420p[v]",
    "-map", "[v]", "-map", "4:a", "-t", "11",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", path,
  ], "reference-pilot-reference-generation-failed");
}

async function makeCustomerClips(root: string, ffmpeg: string): Promise<string[]> {
  const paths = [1, 2, 3, 4].map((index) => join(root, `customer-clip-${index}.mp4`));
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x6d3b2d:s=180x320:r=25:d=3",
    "-f", "lavfi", "-i", "color=c=0x326a63:s=180x320:r=25:d=2.68",
    "-f", "lavfi", "-i", "color=c=0x876942:s=180x320:r=25:d=5.32",
    "-f", "lavfi", "-i", "sine=frequency=392:sample_rate=44100:duration=11",
    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
    "-map", "[v]", "-map", "3:a", "-t", "11",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", paths[0]!,
  ], "reference-pilot-customer-clip-generation-failed");
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=25:duration=3",
    "-f", "lavfi", "-i", "smptebars=size=180x320:rate=25:duration=8",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=11",
    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
    "-map", "[v]", "-map", "2:a", "-t", "11",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", paths[1]!,
  ], "reference-pilot-customer-clip-generation-failed");
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc=size=180x320:rate=25:duration=8.5",
    "-f", "lavfi", "-i", "color=c=0x425c78:s=180x320:r=25:d=3",
    "-f", "lavfi", "-i", "sine=frequency=494:sample_rate=44100:duration=11",
    "-filter_complex",
    "[0:v]settb=AVTB,format=yuv420p[a];[1:v]settb=AVTB,format=yuv420p[b];" +
      "[a][b]xfade=transition=fadeblack:duration=0.5:offset=8,format=yuv420p[v]",
    "-map", "[v]", "-map", "2:a", "-t", "11",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", paths[2]!,
  ], "reference-pilot-customer-clip-generation-failed");
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x5a416f:s=180x320:r=25:d=11",
    "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=44100:duration=11",
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", paths[3]!,
  ], "reference-pilot-customer-clip-generation-failed");
  return paths;
}

async function probeMedia(path: string, ffprobe: string): Promise<MediaProbe> {
  const stdout = await runChecked(ffprobe, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
    "-of", "json", path,
  ], "reference-pilot-ffprobe-failed");
  const parsed = JSON.parse(stdout) as {
    streams?: Array<{ codec_type?: string; codec_name?: string; width?: number; height?: number }>;
    format?: { duration?: string | number };
  };
  const video = parsed.streams?.find((stream) => stream.codec_type === "video");
  const audio = parsed.streams?.find((stream) => stream.codec_type === "audio");
  const durationSeconds = Number(parsed.format?.duration);
  if (!video || !Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error("reference-pilot-ffprobe-output-invalid");
  }
  return {
    durationSeconds,
    videoCodec: String(video.codec_name),
    audioCodec: audio?.codec_name ?? null,
    width: Number(video.width),
    height: Number(video.height),
    videoStreamCount: parsed.streams?.filter((stream) => stream.codec_type === "video").length ?? 0,
    audioStreamCount: parsed.streams?.filter((stream) => stream.codec_type === "audio").length ?? 0,
  };
}

function sampleBinding(sample: ReferenceSample) {
  return {
    id: sample.id,
    version: sample.version,
    customerId: sample.customerId,
    sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId,
    sourceDigest: sample.sourceDigest,
    sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode,
    candidateScope: sample.candidateScope,
  };
}

function rightsBinding(sample: ReferenceSample, rights: ReferenceRightsDeclaration) {
  return {
    id: rights.id,
    version: rights.version,
    customerId: rights.customerId,
    sessionId: rights.sessionId,
    referenceSampleId: sample.id,
    referenceSampleVersion: sample.version,
    referenceSampleDigest: sample.sourceDigest,
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

function latestById<T extends { id: string; version: number }>(values: readonly T[], id: string): T | undefined {
  return values.filter((value) => value.id === id).sort((left, right) => right.version - left.version)[0];
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
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

function runChecked(command: string, args: readonly string[], code: string): Promise<string> {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    let stderr = "";
    child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", () => rejectRun(new Error(code)));
    child.once("close", (status) => status === 0
      ? resolveRun(Buffer.concat(stdout).toString("utf8"))
      : rejectRun(new Error(`${code}:${stderr.trim().slice(0, 500)}`)));
  });
}
