import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  MaterialCard,
  ReferenceDraftPackage,
  ReferenceExecutionReport,
  ReferenceMaterialMappingPlan,
  ReferenceMobileReviewV1,
  ReferenceRightsDeclaration,
  ReferenceSample,
  ReferenceStyleAnalysis,
  ReferenceVariantKind,
  ReferenceVariantPlan,
  StudioMediaAsset,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ReferenceGuidedEditor,
  type ReferenceEditorAnalysisRequest,
  type ReferenceEditorMappingRequest,
  type ReferenceEditorRenderRequest,
  type ReferenceEditorRenderResult,
  type ReferenceGuidedEditorOptions,
} from "../src/reference-guided-editor";
import {
  createReferenceMaterialMappingPlan,
  type ReferenceMappingAssetSnapshot,
} from "../src/reference-material-mapper";
import {
  JsonReferenceSampleStore,
  REFERENCE_WORKFLOW_INTEGRITY_VERSION,
  referenceEditorOperationMetadata,
  referenceWorkflowDigest,
  type ReferenceEditorOperation,
} from "../src/reference-sample-store";
import type { ReferenceRenderEvidence } from "../src/reference-studio-renderer";
import type { StudioScope } from "../src/studio-capabilities";

const scope: StudioScope = { actorId: "actor-a", customerId: "customer-a", sessionId: "session-a" };
const createdAt = "2026-09-20T00:00:00.000Z";

describe("ReferenceGuidedEditor", () => {
  let root: string;
  let outputRoot: string;
  let store: JsonReferenceSampleStore;
  let sample: ReferenceSample;
  let fixture: ReturnType<typeof materialFixture>;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "reference-guided-editor-"));
    outputRoot = join(root, "outputs");
    await mkdir(outputRoot);
    store = await JsonReferenceSampleStore.open(root, scope);
    sample = sampleFixture();
    fixture = materialFixture();
    await store.putSample(sample);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("blocks mapping until the current style version is confirmed", async () => {
    const editor = makeEditor();
    await editor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "rights_1");
    await editor.analyze(scope, sample.id, "analysis_1");

    await expect(editor.createMappingPlan(scope, sample.id, "map_1"))
      .rejects.toThrow("reference-style-selection-required");
  });

  it("rejects external content reuse without changing the last trusted declaration", async () => {
    const editor = makeEditor();
    const first = await editor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "rights_1");
    const before = await store.snapshot();

    await expect(editor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: ["video-clips"],
    }, "rights_forbidden")).rejects.toThrow("external-reference-reusable-elements-forbidden");

    const after = await store.snapshot();
    expect(after.digest).toBe(before.digest);
    expect(after.rightsDeclarations).toEqual([first]);
  });

  it("confirms only the exact persisted mapping draft identity", async () => {
    const editor = makeEditor();
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "style_1");
    const draft = await editor.createMappingPlan(scope, sample.id, "map_1");
    const confirmed = await editor.confirmMappingPlan(scope, draft.id, "confirm_1");

    expect(confirmed.status).toBe("confirmed");
    expect(confirmed.version).toBe(draft.version + 1);
    expect(confirmed.contentDigest).toBe(draft.contentDigest);
  });

  it("does not commit a render completed after rights changed", async () => {
    let resolveRender!: (value: ReferenceEditorRenderResult) => void;
    const pendingRender = new Promise<ReferenceEditorRenderResult>((resolve) => { resolveRender = resolve; });
    const cleanup = vi.fn(async (result: ReferenceEditorRenderResult) => {
      await rm(result.outputDirectory, { recursive: true, force: true });
    });
    const editor = makeEditor({ render: async () => pendingRender, cleanup });
    const confirmed = await reachConfirmedMapping(editor);

    const pending = editor.render(scope, confirmed.id, "render_1");
    await waitUntil(() => Boolean(resolveRender));
    const rightsChanged = editor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "rights_2");
    await rightsChanged;
    const latestBeforeResolve = await store.snapshot();
    const result = await fakeRenderedResult(confirmed, latestAnalysis(latestBeforeResolve), "render_1", outputRoot);
    resolveRender(result);

    await expect(pending).rejects.toThrow("reference-render-input-stale");
    const snapshot = await store.snapshot();
    expect(snapshot.drafts).toHaveLength(0);
    expect(snapshot.variantPlans).toHaveLength(0);
    expect(snapshot.executionReports).toHaveLength(0);
    expect(snapshot.mobileReviews).toHaveLength(0);
    expect(cleanup).toHaveBeenCalledOnce();
    await expect(directoryExists(result.outputDirectory)).resolves.toBe(false);
  });

  it("commits four artifacts and exposes only a safe four-part mobile report", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const rendered = await editor.render(scope, confirmed.id, "render_1");
    const review = await editor.mobileReview(scope, sample.id);

    expect(rendered.artifacts.map((artifact) => artifact.kind)).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
    expect(review.artifacts).toHaveLength(4);
    expect(review.publishClaimed).toBe(false);
    expect(Object.keys(review.executionReport)).toEqual(expect.arrayContaining([
      "rhythm", "transitions", "narrative", "audio",
    ]));
    expect(review.styleSummary.capability).toEqual({
      mode: "provider",
      label: "移动审片：已配置模型服务 A/B，节奏/转场",
      version: 1,
      provider: "configured-provider",
    });
    expect(review.styleSummary.capability.version).not.toBe(730_001);
    expect(JSON.stringify(review)).not.toContain("730001");
    expect(JSON.stringify(review)).not.toMatch(
      /sourcePath|originalBytes|externalTranscript|outputPath|similarity|local evidence|local-v1/i,
    );
  });

  it("does not claim a matched transition when render evidence shows it was not executed", async () => {
    fixture.card.confirmedFacts = ["reference-transition:fade:rule-transition-1"];
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights }) =>
      transitionAnalysisFixture(currentSample, rights) });
    const confirmed = await reachConfirmedMapping(editor);

    const report = await editor.render(scope, confirmed.id, "render_without_transition");

    expect(report.mappingCounts.matched).toBe(1);
    expect(report.transitions.status).toBe("substituted");
    expect(report.transitions.summaryCode).toBe("reference.transition.substituted");
  });

  it("invalidates only outputs that used a withdrawn asset while preserving audit versions", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "render_1");
    const before = await store.snapshot();

    await editor.invalidateForAsset(scope, fixture.asset.id, "withdraw_1");

    await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow("reference-render-required");
    const after = await store.snapshot();
    expect(after.drafts.some((draft) => draft.status === "reviewable")).toBe(true);
    expect(after.drafts.some((draft) => draft.status === "invalidated")).toBe(true);
    expect(after.drafts.length).toBeGreaterThan(before.drafts.length);
    expect(after.mappingPlans.at(-1)?.status).toBe("invalidated");
  });

  it("invalidates the sample and its entire rendered chain when the withdrawn asset is the sample source", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "sample_source_render");
    const currentSample = (await store.snapshot()).samples.filter((candidate) => candidate.id === sample.id)
      .sort((left, right) => right.version - left.version)[0]!;

    await editor.invalidateForAsset(scope, {
      assetId: sample.sourceAssetId,
      reason: "asset-permission-withdrawn",
    }, "sample_source_withdraw");

    const after = await store.snapshot();
    const latestSample = after.samples.filter((candidate) => candidate.id === sample.id)
      .sort((left, right) => right.version - left.version)[0]!;
    expect(latestSample).toMatchObject({
      id: sample.id,
      version: currentSample.version + 1,
      status: "invalidated",
      sourceAssetId: sample.sourceAssetId,
      sourceDigest: sample.sourceDigest,
    });
    expect(after.mappingPlans.some((plan) =>
      plan.id === confirmed.id && plan.version === confirmed.version + 1 && plan.status === "invalidated"))
      .toBe(true);
    expect(report.artifacts.every((artifact) => after.drafts.some((draft) =>
      draft.status === "invalidated" && draft.outputDigest === artifact.outputDigest))).toBe(true);
    const operation = after.editorOperations.find((candidate) =>
      candidate.operationKey === "sample_source_withdraw")!;
    expect(operation.intent).toMatchObject({
      method: "invalidateForAsset",
      affectedSamples: [{ id: sample.id, version: currentSample.version, sourceDigest: sample.sourceDigest }],
      affectedMappings: [{
        id: confirmed.id,
        version: confirmed.version,
        contentDigest: confirmed.contentDigest,
      }],
    });
    expect(operation.result).toMatchObject({
      kind: "invalidation",
      invalidatedSamples: [{
        id: sample.id,
        version: currentSample.version + 1,
        sourceDigest: sample.sourceDigest,
      }],
    });
    await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow("reference-rights-declaration-required");
  });

  it("approves only an exact current draft through the server verifier and replays without a second write", async () => {
    let authorizeCalls = 0;
    const authorize: NonNullable<ReferenceGuidedEditorOptions["withCurrentAssetAuthorization"]> =
      async (_request, work) => {
        authorizeCalls += 1;
        return work();
      };
    const readDraftOutputDigest = vi.fn(async ({ draft }: { draft: ReferenceDraftPackage }) => draft.outputDigest);
    const editor = makeEditor({ authorize, readDraftOutputDigest });
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "approval_render");
    const authorizationCallsBeforeApproval = authorizeCalls;
    const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;
    const request = {
      draftId: master.draftId,
      reportId: report.id,
      reportVersion: report.version,
      draftVersion: master.draftVersion,
      draftDigest: master.outputDigest,
    };

    const first = await editor.approveDraft(scope, request, "approve_master_exact");
    const replay = await editor.approveDraft(scope, request, "approve_master_exact");

    expect(replay).toEqual(first);
    expect(first).toMatchObject({
      kind: "reference-draft",
      reviewerId: scope.actorId,
      status: "active",
      publishClaimed: false,
      bindings: {
        executionReport: { id: report.id, version: report.version },
        draft: { id: master.draftId, version: master.draftVersion, outputDigest: master.outputDigest },
        mappingPlan: {
          id: confirmed.id,
          version: confirmed.version,
          contentDigest: confirmed.contentDigest,
          permissionSetDigest: confirmed.permissionSetDigest,
        },
      },
    });
    expect(authorizeCalls).toBe(authorizationCallsBeforeApproval + 1);
    expect(readDraftOutputDigest).toHaveBeenCalledOnce();
    const snapshot = await store.snapshot();
    expect(snapshot.approvals.filter((approval) => approval.id === first.id)).toEqual([first]);
    expect(snapshot.editorOperations.filter((operation) => operation.method === "approveDraft"))
      .toHaveLength(1);
    expect(artifact(await editor.mobileReview(scope, sample.id), "editorial-master").status)
      .toBe("approved");
    await expect(editor.approveDraft(scope, {
      ...request,
      draftDigest: digest("different-draft"),
    }, "approve_master_exact")).rejects.toThrow("reference-operation-key-conflict");
  });

  it("keeps an exact approval receipt auditable after a later asset withdrawal invalidates it", async () => {
    const editor = makeEditor({
      authorize: async (_request, work) => work(),
      readDraftOutputDigest: async ({ draft }) => draft.outputDigest,
    });
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "approval_withdrawal_render");
    const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;
    const approval = await editor.approveDraft(scope, {
      draftId: master.draftId,
      reportId: report.id,
      reportVersion: report.version,
      draftVersion: master.draftVersion,
      draftDigest: master.outputDigest,
    }, "approval_before_withdrawal");

    await expect(editor.invalidateForAsset(
      scope,
      fixture.asset.id,
      "withdraw_after_approval",
    )).resolves.toBeUndefined();

    const snapshot = await store.snapshot();
    expect(snapshot.approvals.filter((candidate) => candidate.id === approval.id)
      .map((candidate) => candidate.status).sort()).toEqual(["active", "invalidated"]);
    expect(snapshot.editorOperations.some((operation) =>
      operation.operationKey === "withdraw_after_approval" &&
      operation.intent.method === "invalidateForAsset")).toBe(true);
  });

  it("does not approve when the current file digest differs from the bound draft", async () => {
    const editor = makeEditor({
      authorize: async (_request, work) => work(),
      readDraftOutputDigest: async () => digest("tampered-output"),
    });
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "approval_digest_render");
    const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;

    await expect(editor.approveDraft(scope, {
      draftId: master.draftId,
      reportId: report.id,
      reportVersion: report.version,
      draftVersion: master.draftVersion,
      draftDigest: master.outputDigest,
    }, "approval_digest_mismatch")).rejects.toThrow("reference-approval-file-digest-mismatch");
    expect((await store.snapshot()).approvals).toHaveLength(0);
  });

  it("does not replay a historical reviewable render after its asset was withdrawn", async () => {
    const render = vi.fn(async (request: ReferenceEditorRenderRequest) =>
      fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot));
    const editor = makeEditor({ render });
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "withdrawn_render_replay");
    await editor.invalidateForAsset(scope, fixture.asset.id, "withdrawn_render_replay_invalidation");
    const invalidated = await store.snapshot();
    const coldRender = vi.fn(render.getMockImplementation()!);

    await expect(makeEditor({ render: coldRender }).render(scope, confirmed.id, "withdrawn_render_replay"))
      .rejects.toThrow("reference-render-required");
    expect(render).toHaveBeenCalledOnce();
    expect(coldRender).not.toHaveBeenCalled();
    await expect(store.snapshot()).resolves.toEqual(invalidated);
  });

  it("does not invalidate another sample workflow when one sample is re-analyzed", async () => {
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights, operationKey }) => ({
      ...analysisFixture(currentSample, rights),
      id: `analysis-${currentSample.id}-${operationKey}`,
      contentDigest: digest(`analysis-${currentSample.id}-${operationKey}`),
    }) });
    const firstAnalysis = await reachAnalysis(editor);
    await editor.confirmStyle(scope, firstAnalysis.analysis.id, [firstAnalysis.analysis.rules[0]!.id], "style_a");
    const sampleB = sampleFixture("2");
    await store.putSample(sampleB);
    await editor.declareRights(scope, {
      referenceSampleId: sampleB.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "rights_b");
    const analysisB = await editor.analyze(scope, sampleB.id, "analysis_b");
    const selectionB = await editor.confirmStyle(scope, analysisB.id, [analysisB.rules[0]!.id], "style_b");

    await editor.analyze(scope, sample.id, "analysis_a_2");

    const snapshot = await store.snapshot();
    const bVersions = snapshot.styleSelections.filter((selection) => selection.id === selectionB.id);
    expect(bVersions).toHaveLength(1);
    expect(bVersions[0]?.status).toBe("confirmed");
  });

  it("keeps the master identity when revising one platform and replaces all three after master revision", async () => {
    const editor = makeEditor({ revise: async (request) => fakeRevisionResult(request, outputRoot) });
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "render_1");
    const first = await editor.mobileReview(scope, sample.id);
    for (const kind of ["editorial-master", "xiaohongshu"] as const) {
      const approved = artifact(first, kind);
      await store.putApproval({
        id: `approval-${kind}`, storyId: sample.id, revisionId: confirmed.id,
        revision: confirmed.version, artifactId: approved.draftId, artifactDigest: approved.outputDigest,
        coverDigest: digest(`cover-${kind}`), stillDigests: [], renderInputDigest: confirmed.contentDigest,
        variant: kind, reviewerId: scope.actorId, approvedAt: createdAt, status: "active",
      });
    }

    await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("xiaohongshu", "开头更快，但不要改变母版"),
      "revise_xhs_1",
    );
    const platformRevision = await editor.mobileReview(scope, sample.id);
    expect(artifact(platformRevision, "editorial-master").draftId)
      .toBe(artifact(first, "editorial-master").draftId);
    expect(artifact(platformRevision, "editorial-master").status).toBe("reviewable");
    expect(artifact(platformRevision, "xiaohongshu").draftId)
      .not.toBe(artifact(first, "xiaohongshu").draftId);
    const afterPlatform = await store.snapshot();
    expect(afterPlatform.drafts.some((draft) => draft.status === "invalidated" &&
      draft.outputDigest === artifact(first, "xiaohongshu").outputDigest)).toBe(true);
    expect(afterPlatform.drafts.some((draft) => draft.status === "invalidated" &&
      draft.outputDigest === artifact(first, "editorial-master").outputDigest)).toBe(false);
    expect(afterPlatform.approvals.find((approval) => approval.id === "approval-xiaohongshu")?.status)
      .toBe("invalidated");
    expect(afterPlatform.approvals.find((approval) => approval.id === "approval-editorial-master")?.status)
      .toBe("active");

    await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("editorial-master", "母版收紧叙事"),
      "revise_master_1",
    );
    const masterRevision = await editor.mobileReview(scope, sample.id);
    for (const kind of ["editorial-master", "xiaohongshu", "wechat-channels"] as const) {
      expect(artifact(masterRevision, kind).draftId).not.toBe(artifact(platformRevision, kind).draftId);
    }
    const afterMaster = await store.snapshot();
    for (const previous of platformRevision.artifacts) {
      expect(afterMaster.drafts.some((draft) => draft.status === "invalidated" &&
        draft.outputDigest === previous.outputDigest)).toBe(true);
    }
    expect(afterMaster.approvals.find((approval) => approval.id === "approval-editorial-master")?.status)
      .toBe("invalidated");
  });

  it.each(["xiaohongshu", "editorial-master"] as const)(
    "rejects a different-key stale %s revision after another exact revision commits first",
    async (kind) => {
      const staleKey = `concurrent_revision_stale_${kind}`;
      const winnerKey = `concurrent_revision_winner_${kind}`;
      let releaseStale!: () => void;
      const staleGate = new Promise<void>((resolve) => { releaseStale = resolve; });
      let staleResult: ReferenceEditorRenderResult | undefined;
      const cleanup = vi.fn(async (result: ReferenceEditorRenderResult) => {
        await rm(result.outputDirectory, { recursive: true, force: true });
      });
      const revise = vi.fn(async (request: ReferenceEditorRenderRequest) => {
        const result = await fakeRevisionResult(request, outputRoot);
        if (request.operationKey === staleKey) {
          staleResult = result;
          await staleGate;
        }
        return result;
      });
      const editor = makeEditor({ revise, cleanup });
      const confirmed = await reachConfirmedMapping(editor);
      await editor.render(scope, confirmed.id, `concurrent_revision_render_${kind}`);
      const original = await editor.mobileReview(scope, sample.id);
      const input = await currentRevisionInput(kind, "开头缩短 1 秒");

      const stalePending = editor.reviseWithPrompt(scope, input, staleKey);
      const staleOutcome = stalePending.then(
        (value) => ({ value, error: null as Error | null }),
        (error: Error) => ({ value: null, error }),
      );
      await waitUntil(() => staleResult !== undefined);
      const winner = await editor.reviseWithPrompt(scope, input, winnerKey);
      const afterWinner = await store.snapshot();

      releaseStale();
      const outcome = await staleOutcome;

      expect(outcome.value).toBeNull();
      expect(outcome.error?.message).toContain("reference-render-input-stale");
      expect(await store.snapshot()).toEqual(afterWinner);
      expect(afterWinner.editorOperations.some((operation) => operation.operationKey === staleKey)).toBe(false);
      expect(afterWinner.editorOperations.some((operation) => operation.operationKey === winnerKey)).toBe(true);
      expect(cleanup).toHaveBeenCalledOnce();
      expect(cleanup).toHaveBeenCalledWith(staleResult);
      await expect(directoryExists(staleResult!.outputDirectory)).resolves.toBe(false);

      const current = await editor.mobileReview(scope, sample.id);
      expect(current.bindings.executionReport).toMatchObject({ id: winner.id, version: winner.version });
      for (const candidate of ["editorial-master", "xiaohongshu", "wechat-channels"] as const) {
        const changed = artifact(current, candidate).draftId !== artifact(original, candidate).draftId;
        expect(changed).toBe(kind === "editorial-master" || candidate === kind);
      }
    },
  );

  it.each(["mapping-source", "sample-source"] as const)(
    "cleans an in-flight platform revision without a receipt when %s withdrawal wins",
    async (withdrawnSource) => {
      const staleKey = `withdrawal_revision_stale_${withdrawnSource}`;
      let releaseStale!: () => void;
      const staleGate = new Promise<void>((resolve) => { releaseStale = resolve; });
      let staleResult: ReferenceEditorRenderResult | undefined;
      const cleanup = vi.fn(async (result: ReferenceEditorRenderResult) => {
        await rm(result.outputDirectory, { recursive: true, force: true });
      });
      const revise = vi.fn(async (request: ReferenceEditorRenderRequest) => {
        const result = await fakeRevisionResult(request, outputRoot);
        staleResult = result;
        await staleGate;
        return result;
      });
      const editor = makeEditor({ revise, cleanup });
      const confirmed = await reachConfirmedMapping(editor);
      await editor.render(scope, confirmed.id, `withdrawal_revision_render_${withdrawnSource}`);
      const input = await currentRevisionInput("xiaohongshu", "开头缩短 1 秒");

      const stalePending = editor.reviseWithPrompt(scope, input, staleKey);
      const staleOutcome = stalePending.then(
        (value) => ({ value, error: null as Error | null }),
        (error: Error) => ({ value: null, error }),
      );
      await waitUntil(() => staleResult !== undefined);
      await editor.invalidateForAsset(
        scope,
        withdrawnSource === "mapping-source" ? fixture.asset.id : sample.sourceAssetId,
        `withdrawal_winner_${withdrawnSource}`,
      );
      const afterWithdrawal = await store.snapshot();

      releaseStale();
      const outcome = await staleOutcome;

      expect(outcome.value).toBeNull();
      expect(outcome.error?.message).toContain("reference-render-input-stale");
      expect(await store.snapshot()).toEqual(afterWithdrawal);
      expect(afterWithdrawal.editorOperations.some((operation) => operation.operationKey === staleKey)).toBe(false);
      expect(cleanup).toHaveBeenCalledOnce();
      expect(cleanup).toHaveBeenCalledWith(staleResult);
      await expect(directoryExists(staleResult!.outputDirectory)).resolves.toBe(false);
      await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow(
        withdrawnSource === "mapping-source"
          ? "reference-render-required"
          : "reference-rights-declaration-required",
      );
    },
  );

  it("rejects a legacy approval projected onto a later execution report", async () => {
    const editor = makeEditor({ revise: async (request) => fakeRevisionResult(request, outputRoot) });
    const confirmed = await reachConfirmedMapping(editor);
    const original = await editor.render(scope, confirmed.id, "legacy_approval_original_render");
    const originalMaster = original.artifacts.find((candidate) => candidate.kind === "editorial-master")!;
    await store.putApproval({
      id: "legacy-approval-master-before-revision", storyId: sample.id,
      revisionId: confirmed.id, revision: confirmed.version,
      artifactId: originalMaster.draftId, artifactDigest: originalMaster.outputDigest,
      coverDigest: digest("legacy-cover-before-revision"), stillDigests: [],
      renderInputDigest: confirmed.contentDigest, variant: "editorial-master",
      reviewerId: scope.actorId, approvedAt: createdAt, status: "active",
    });

    await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("xiaohongshu", "开头缩短 1 秒"),
      "legacy_approval_platform_revision",
    );
    const current = await editor.mobileReview(scope, sample.id);
    const forged = {
      ...current,
      id: "forged-current-review-with-legacy-approval",
      artifacts: current.artifacts.map((candidate) => candidate.kind === "editorial-master"
        ? { ...candidate, status: "approved" as const }
        : candidate),
      approval: { status: "pending" as const, approvedArtifactKinds: ["editorial-master" as const] },
    };
    const snapshot = await store.snapshot();
    const { digest: _snapshotDigest, ...snapshotPayload } = snapshot;
    const nextMobileReviews = [...snapshot.mobileReviews, forged];

    await expect(store.commit({
      ...snapshot,
      mobileReviews: nextMobileReviews,
      digest: referenceWorkflowDigest({ ...snapshotPayload, mobileReviews: nextMobileReviews }),
    }, snapshot.digest)).rejects.toThrow("reference-mobile-approval-mismatch");
  });

  it("keeps a legacy-approved review readable after a higher-version report uses a new id", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const original = await editor.render(scope, confirmed.id, "legacy_review_original_report");
    for (const artifact of original.artifacts) {
      await store.putApproval({
        id: `legacy-review-approval-${artifact.kind}`, storyId: sample.id,
        revisionId: confirmed.id, revision: confirmed.version,
        artifactId: artifact.draftId, artifactDigest: artifact.outputDigest,
        coverDigest: digest(`legacy-review-cover-${artifact.kind}`), stillDigests: [],
        renderInputDigest: confirmed.contentDigest, variant: artifact.kind,
        reviewerId: scope.actorId, approvedAt: createdAt, status: "active",
      });
    }
    const historicalReview = await editor.mobileReview(scope, sample.id);
    expect(historicalReview.status).toBe("approved");
    const newer = {
      ...original,
      id: `new-${original.id}`,
      version: original.version + 1,
      createdAt: "2026-09-20T00:05:00.000Z",
    };

    await expect(store.putExecutionReport(newer)).resolves.toEqual(newer);

    const after = await store.snapshot();
    expect(after.mobileReviews.find((review) => review.id === historicalReview.id)?.status)
      .toBe("approved");
    const current = await editor.mobileReview(scope, sample.id);
    expect(current.bindings.executionReport).toMatchObject({ id: newer.id, version: newer.version });
    expect(current.artifacts.every((artifact) => artifact.status === "reviewable")).toBe(true);
  });

  it("fails closed and quarantines a pre-fix incompatible snapshot instead of guessing a migration", async () => {
    const quarantine = vi.fn(async () => undefined);
    const brokenStore = {
      ...store,
      snapshot: async () => { throw new Error("reference-variant-mapping-mismatch"); },
    };
    const editor = makeEditor({ store: brokenStore as unknown as JsonReferenceSampleStore, quarantine });

    await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-variant-mapping-mismatch");
  });

  it("replays completed render and confirmation operations, including after rebuilding the editor", async () => {
    const render = vi.fn(async (request: ReferenceEditorRenderRequest) =>
      fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot));
    const editor = makeEditor({ render });
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "style_replay");
    const draft = await editor.createMappingPlan(scope, sample.id, "map_replay");
    const confirmed = await editor.confirmMappingPlan(scope, draft.id, "confirm_replay");
    await expect(makeEditor({ render }).confirmMappingPlan(scope, draft.id, "confirm_replay"))
      .resolves.toEqual(confirmed);

    const [first, concurrent] = await Promise.all([
      editor.render(scope, confirmed.id, "render_replay"),
      editor.render(scope, confirmed.id, "render_replay"),
    ]);
    expect(concurrent).toEqual(first);
    await expect(makeEditor({ render }).render(scope, confirmed.id, "render_replay")).resolves.toEqual(first);
    expect(render).toHaveBeenCalledTimes(1);
    await expect(editor.render(scope, "another-plan", "render_replay"))
      .rejects.toThrow("reference-operation-key-conflict");
  });

  it("never replays or renders from a legacy revision receipt missing exact report and draft identity", async () => {
    const operationKey = "legacy_revision_exact_replay";
    const editor = makeEditor({ revise: async (request) => fakeRevisionResult(request, outputRoot) });
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "legacy_revision_render");
    const originalInput = await currentRevisionInput("xiaohongshu", "legacy exact replay probe");
    await editor.reviseWithPrompt(scope, originalInput, operationKey);

    const snapshotPath = join(
      root,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
      "snapshot.json",
    );
    const persisted = JSON.parse(await readFile(snapshotPath, "utf8")) as Record<string, unknown> & {
      executionReports: ReferenceExecutionReport[];
      editorOperations: ReferenceEditorOperation[];
    };
    const exactOperation = persisted.editorOperations.find((operation) =>
      operation.method === "reviseWithPrompt" && operation.operationKey === operationKey)!;
    const exactReport = persisted.executionReports.find((report) =>
      exactOperation.result.kind === "report" && report.id === exactOperation.result.id &&
      report.version === exactOperation.result.version)!;
    if (exactOperation.intent.method !== "reviseWithPrompt" || !exactReport.operation) {
      throw new Error("test-exact-revision-receipt-missing");
    }
    const legacyInputBindingDigest = digest(canonicalJson(exactReport.bindings));
    const legacyIntent = {
      method: "reviseWithPrompt" as const,
      sampleId: exactOperation.intent.sampleId,
      target: exactOperation.intent.target,
      promptDigest: exactOperation.intent.promptDigest,
      inputBindingDigest: legacyInputBindingDigest,
      expectedResult: exactOperation.intent.expectedResult,
    };
    const legacyReport = {
      ...exactReport,
      operation: { ...exactReport.operation, inputBindingDigest: legacyInputBindingDigest },
    };
    const legacyOperation: ReferenceEditorOperation = {
      ...referenceEditorOperationMetadata(
        scope,
        "reviseWithPrompt",
        operationKey,
        legacyIntent,
      ),
      result: exactOperation.result,
      resultDigest: digest(canonicalJson(legacyReport)),
    };
    persisted.executionReports = persisted.executionReports.map((report) =>
      report.id === legacyReport.id && report.version === legacyReport.version ? legacyReport : report);
    persisted.editorOperations = persisted.editorOperations.map((operation) =>
      operation.id === exactOperation.id ? legacyOperation : operation);
    const { digest: _persistedDigest, ...legacyPayload } = persisted;
    persisted.digest = referenceWorkflowDigest(legacyPayload as never);
    await writeFile(snapshotPath, `${JSON.stringify(persisted)}\n`);

    const acceptedLegacySnapshot = await store.snapshot();
    const currentInput = await currentRevisionInput("xiaohongshu", "legacy exact replay probe");
    const revise = vi.fn(async (request: ReferenceEditorRenderRequest) =>
      fakeRevisionResult(request, outputRoot));
    const cold = makeEditor({ revise });
    for (const exactInput of [originalInput, currentInput]) {
      await expect(cold.reviseWithPrompt(scope, exactInput, operationKey))
        .rejects.toThrow("reference-operation-key-conflict");
    }
    expect(revise).not.toHaveBeenCalled();
    await expect(store.snapshot()).resolves.toEqual(acceptedLegacySnapshot);
  });

  it("isolates in-flight operations across roots while deduplicating editors on one root", async () => {
    const secondRoot = join(root, "second-store-root");
    const secondOutputRoot = join(secondRoot, "outputs");
    await mkdir(secondOutputRoot, { recursive: true });
    const secondStore = await JsonReferenceSampleStore.open(secondRoot, scope);
    await secondStore.putSample(sample);
    const firstMapping = await reachConfirmedMapping(makeEditor({ store }));
    const secondMapping = await reachConfirmedMapping(makeEditor({ store: secondStore }));

    let releaseCrossRoot!: () => void;
    const crossRootGate = new Promise<void>((resolve) => { releaseCrossRoot = resolve; });
    const firstRender = vi.fn(async (request: ReferenceEditorRenderRequest) => {
      await crossRootGate;
      return fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot);
    });
    const secondRender = vi.fn(async (request: ReferenceEditorRenderRequest) => {
      await crossRootGate;
      return fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, secondOutputRoot);
    });
    const firstPending = makeEditor({ store, render: firstRender })
      .render(scope, firstMapping.id, "cross_root_same_key");
    await waitUntil(() => firstRender.mock.calls.length === 1);
    const secondPending = makeEditor({ store: secondStore, render: secondRender })
      .render(scope, secondMapping.id, "cross_root_same_key");
    releaseCrossRoot();
    await expect(Promise.all([firstPending, secondPending])).resolves.toHaveLength(2);
    expect(firstRender).toHaveBeenCalledOnce();
    expect(secondRender).toHaveBeenCalledOnce();
    expect((await store.snapshot()).editorOperations.some((operation) =>
      operation.operationKey === "cross_root_same_key")).toBe(true);
    expect((await secondStore.snapshot()).editorOperations.some((operation) =>
      operation.operationKey === "cross_root_same_key")).toBe(true);

    const reopenedSameRoot = await JsonReferenceSampleStore.open(root, scope);
    const sharedOutputRoot = join(root, "shared-root-output");
    await mkdir(sharedOutputRoot);
    let releaseSameRoot!: () => void;
    const sameRootGate = new Promise<void>((resolve) => { releaseSameRoot = resolve; });
    const sameRootFirst = vi.fn(async (request: ReferenceEditorRenderRequest) => {
      await sameRootGate;
      return fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, sharedOutputRoot);
    });
    const sameRootSecond = vi.fn(async (request: ReferenceEditorRenderRequest) =>
      fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, sharedOutputRoot));
    const sameRootPendingA = makeEditor({ store, render: sameRootFirst })
      .render(scope, firstMapping.id, "same_root_shared_key");
    await waitUntil(() => sameRootFirst.mock.calls.length === 1);
    const sameRootPendingB = makeEditor({ store: reopenedSameRoot, render: sameRootSecond })
      .render(scope, firstMapping.id, "same_root_shared_key");
    releaseSameRoot();
    const [sameRootA, sameRootB] = await Promise.all([sameRootPendingA, sameRootPendingB]);
    expect(sameRootB).toEqual(sameRootA);
    expect(sameRootFirst).toHaveBeenCalledOnce();
    expect(sameRootSecond).not.toHaveBeenCalled();
  });

  it("persists method-and-intent-bound receipts for every mutation entry point", async () => {
    const analyze = vi.fn(async ({ sample: currentSample, rights }: ReferenceEditorAnalysisRequest) =>
      analysisFixture(currentSample, rights));
    const createMapping = vi.fn(async (request: ReferenceEditorMappingRequest) =>
      createReferenceMaterialMappingPlan({
        scope: request.scope, sample: request.sample, rights: request.rights,
        analysis: request.analysis, selection: request.selection,
        currentMaterials: [fixture.card], authorizedHistoricalMaterials: [], assets: [fixture.mappingAsset],
      }));
    const render = vi.fn(async (request: ReferenceEditorRenderRequest) =>
      fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot));
    const revise = vi.fn(async (request: ReferenceEditorRenderRequest) => fakeRevisionResult(request, outputRoot));
    const editor = makeEditor({ analyze, createMapping, render, revise });
    const rights = await editor.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "all_rights");
    const analysis = await editor.analyze(scope, sample.id, "all_analysis");
    const selection = await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "all_style");
    const draft = await editor.createMappingPlan(scope, sample.id, "all_mapping");
    const confirmed = await editor.confirmMappingPlan(scope, draft.id, "all_confirm");
    await editor.render(scope, confirmed.id, "all_render");
    const allRevisionInput = await currentRevisionInput("xiaohongshu", "keep the master");
    await editor.reviseWithPrompt(scope, allRevisionInput, "all_revise");
    await editor.invalidateForAsset(scope, fixture.asset.id, "all_invalidate");

    const coldAnalyze = vi.fn(analyze.getMockImplementation()!);
    const coldMapping = vi.fn(createMapping.getMockImplementation()!);
    const coldRender = vi.fn(render.getMockImplementation()!);
    const coldRevise = vi.fn(revise.getMockImplementation()!);
    const cold = makeEditor({ analyze: coldAnalyze, createMapping: coldMapping, render: coldRender, revise: coldRevise });
    await expect(cold.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "all_rights")).resolves.toEqual(rights);
    await expect(cold.analyze(scope, sample.id, "all_analysis")).resolves.toEqual(analysis);
    await expect(cold.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "all_style"))
      .resolves.toEqual(selection);
    await expect(cold.createMappingPlan(scope, sample.id, "all_mapping")).resolves.toEqual(draft);
    await expect(cold.confirmMappingPlan(scope, draft.id, "all_confirm")).resolves.toEqual(confirmed);
    await expect(cold.render(scope, confirmed.id, "all_render"))
      .rejects.toThrow("reference-render-required");
    await expect(cold.reviseWithPrompt(scope, allRevisionInput, "all_revise"))
      .rejects.toThrow("reference-render-required");
    await expect(cold.invalidateForAsset(scope, fixture.asset.id, "all_invalidate")).resolves.toBeUndefined();
    expect(coldAnalyze).not.toHaveBeenCalled();
    expect(coldMapping).not.toHaveBeenCalled();
    expect(coldRender).not.toHaveBeenCalled();
    expect(coldRevise).not.toHaveBeenCalled();
    expect(new Set((await store.snapshot()).editorOperations.map((operation) => operation.method))).toEqual(new Set([
      "declareRights", "analyze", "confirmStyle", "createMappingPlan", "confirmMappingPlan",
      "render", "reviseWithPrompt", "invalidateForAsset",
    ]));
    const storedOperations = (await store.snapshot()).editorOperations;
    expect(storedOperations.every((operation) => operation.actorId === scope.actorId)).toBe(true);
    const otherActor = { ...scope, actorId: "actor-imposter" };
    await expect(cold.analyze(otherActor, sample.id, "all_analysis"))
      .rejects.toThrow("reference-operation-key-conflict");
    const conflicts = [
      () => cold.declareRights(scope, {
        referenceSampleId: sample.id, sourceKind: "self-owned", reusableElements: [],
      }, "all_rights"),
      () => cold.analyze(scope, "another-sample", "all_analysis"),
      () => cold.confirmStyle(scope, analysis.id, ["another-rule"], "all_style"),
      () => cold.createMappingPlan(scope, "another-sample", "all_mapping"),
      () => cold.confirmMappingPlan(scope, "another-plan", "all_confirm"),
      () => cold.render(scope, "another-plan", "all_render"),
      () => cold.reviseWithPrompt(scope, {
        ...allRevisionInput,
        prompt: "different prompt",
      }, "all_revise"),
      () => cold.invalidateForAsset(scope, "another-asset", "all_invalidate"),
    ];
    for (const conflict of conflicts) {
      await expect(conflict()).rejects.toThrow("reference-operation-key-conflict");
    }
  });

  it("rejects missing, wrong-kind, and wrong-subject operation results before replay", async () => {
    const editor = makeEditor({ revise: async (request) => fakeRevisionResult(request, outputRoot) });
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "integrity_render");
    await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("xiaohongshu", "integrity revision"),
      "integrity_revise",
    );
    await editor.invalidateForAsset(scope, fixture.asset.id, "integrity_invalidate");
    const snapshot = await store.snapshot();
    const { digest: _snapshotDigest, ...snapshotPayload } = snapshot;
    const recordResults = snapshot.editorOperations.filter((operation) =>
      operation.result.kind !== "void" && operation.result.kind !== "invalidation");
    for (const [index, operation] of recordResults.entries()) {
      const forged = {
        ...structuredClone(operation),
        ...referenceEditorOperationMetadata(scope, operation.method, `missing-${index}`, operation.intent),
      };
      if (forged.result.kind === "void" || forged.result.kind === "invalidation") {
        throw new Error("unexpected-non-record-operation-result");
      }
      forged.result.id = `missing-${forged.result.id}`;
      await expect(store.commit({
        ...snapshot, editorOperations: [...snapshot.editorOperations, forged],
        digest: referenceWorkflowDigest({ ...snapshotPayload, editorOperations: [...snapshot.editorOperations, forged] }),
      }, snapshot.digest)).rejects.toThrow("reference-operation-result-missing");
    }

    const invalidate = snapshot.editorOperations.find((operation) => operation.method === "invalidateForAsset")!;
    const report = snapshot.executionReports.find((candidate) => candidate.status === "reviewable")!;
    const wrongKind = {
      ...invalidate,
      ...referenceEditorOperationMetadata(scope, invalidate.method, "wrong-kind", invalidate.intent),
      result: { kind: "report" as const, id: report.id, version: report.version },
    };
    await expect(store.commit({
      ...snapshot, editorOperations: [...snapshot.editorOperations, wrongKind],
      digest: referenceWorkflowDigest({ ...snapshotPayload, editorOperations: [...snapshot.editorOperations, wrongKind] }),
    }, snapshot.digest)).rejects.toThrow("reference-operation-result-kind-mismatch");

    const analysis = snapshot.editorOperations.find((operation) => operation.method === "analyze")!;
    const wrongSubjectMetadata = referenceEditorOperationMetadata(
      scope, analysis.method, "wrong-subject", { ...analysis.intent, sampleId: "another-sample" },
    );
    const wrongSubject = {
      ...analysis,
      ...wrongSubjectMetadata,
    };
    await expect(store.commit({
      ...snapshot, editorOperations: [...snapshot.editorOperations, wrongSubject],
      digest: referenceWorkflowDigest({ ...snapshotPayload, editorOperations: [...snapshot.editorOperations, wrongSubject] }),
    }, snapshot.digest)).rejects.toThrow("reference-operation-result-binding-mismatch");

    const renderOperation = snapshot.editorOperations.find((operation) => operation.method === "render")!;
    const reviseOperation = snapshot.editorOperations.find((operation) => operation.method === "reviseWithPrompt")!;
    const swappedResult = {
      ...renderOperation,
      ...referenceEditorOperationMetadata(scope, renderOperation.method, "swapped-result", renderOperation.intent),
      result: reviseOperation.result,
    };
    await expect(store.commit({
      ...snapshot, editorOperations: [...snapshot.editorOperations, swappedResult],
      digest: referenceWorkflowDigest({
        ...snapshotPayload, editorOperations: [...snapshot.editorOperations, swappedResult],
      }),
    }, snapshot.digest)).rejects.toThrow("reference-operation-result-digest-mismatch");

    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    const persisted = JSON.parse(await readFile(snapshotPath, "utf8")) as Record<string, unknown> & {
      editorOperations: typeof snapshot.editorOperations;
    };
    const ghost = structuredClone(recordResults[0]!);
    Object.assign(ghost, referenceEditorOperationMetadata(
      scope, ghost.method, "historical-ghost", ghost.intent,
    ));
    if (ghost.result.kind === "void" || ghost.result.kind === "invalidation") {
      throw new Error("unexpected-non-record-operation-result");
    }
    ghost.result.id = `missing-${ghost.result.id}`;
    persisted.editorOperations = [...persisted.editorOperations, ghost];
    const { digest: _storedDigest, ...payload } = persisted;
    persisted.digest = referenceWorkflowDigest(payload as never);
    await writeFile(snapshotPath, `${JSON.stringify(persisted)}\n`);
    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ quarantine }).mobileReview(scope, sample.id))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-operation-result-missing");
  });

  it("derives mobile approved status only from exact current approvals and preserves invalidated audit views", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "mobile_approval_render");
    const reviewable = await editor.mobileReview(scope, sample.id);
    expect(reviewable.status).toBe("reviewable");
    expect(reviewable.artifacts.every((artifact) => artifact.status === "reviewable")).toBe(true);

    const forgedApproved = {
      ...reviewable,
      id: "forged-approved-mobile-review",
      artifacts: reviewable.artifacts.map((artifact) => ({ ...artifact, status: "approved" as const })),
      approval: {
        status: "approved" as const,
        approvedArtifactKinds: reviewable.artifacts.map((artifact) => artifact.kind),
      },
      status: "approved" as const,
    };
    const beforeApproval = await store.snapshot();
    const { digest: _beforeApprovalDigest, ...beforeApprovalPayload } = beforeApproval;
    await expect(store.commit({
      ...beforeApproval,
      mobileReviews: [...beforeApproval.mobileReviews, forgedApproved],
      digest: referenceWorkflowDigest({
        ...beforeApprovalPayload, mobileReviews: [...beforeApproval.mobileReviews, forgedApproved],
      }),
    }, beforeApproval.digest)).rejects.toThrow("reference-mobile-approval-mismatch");

    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-mobile-${artifact.kind}`, storyId: sample.id,
        revisionId: confirmed.id, revision: confirmed.version,
        artifactId: artifact.draftId, artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-mobile-${artifact.kind}`), stillDigests: [],
        renderInputDigest: confirmed.contentDigest, variant: artifact.kind,
        reviewerId: scope.actorId, approvedAt: createdAt, status: "active",
      });
    }
    const approved = await editor.mobileReview(scope, sample.id);
    expect(approved.status).toBe("approved");
    expect(approved.artifacts.every((artifact) => artifact.status === "approved")).toBe(true);

    await editor.invalidateForAsset(scope, fixture.asset.id, "mobile_approval_invalidate");
    await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow("reference-render-required");
    const invalidated = (await store.snapshot()).mobileReviews.filter((review) => review.status === "invalidated");
    expect(invalidated.some((review) => review.artifacts.every((artifact) => artifact.status === "invalidated")))
      .toBe(true);
    expect(invalidated.some((review) => review.executionReport.id === approved.executionReport.id)).toBe(true);
  });

  it("converges concurrent mobile reviews after approvals to one canonical persisted result", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "concurrent_mobile_render");
    await editor.mobileReview(scope, sample.id);
    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-concurrent-mobile-${artifact.kind}`,
        storyId: sample.id,
        revisionId: confirmed.id,
        revision: confirmed.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-concurrent-mobile-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: confirmed.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    const before = await store.snapshot();
    const reopened = await JsonReferenceSampleStore.open(root, scope);
    let initialReads = 0;
    let releaseReads!: () => void;
    const bothRead = new Promise<void>((resolve) => { releaseReads = resolve; });
    const racingStore = (delegate: JsonReferenceSampleStore) => ({
      boundScope: delegate.boundScope,
      operationCoordinatorIdentity: delegate.operationCoordinatorIdentity,
      snapshot: async () => {
        const snapshot = await delegate.snapshot();
        initialReads += 1;
        if (initialReads === 2) releaseReads();
        if (initialReads <= 2) await bothRead;
        return snapshot;
      },
      commit: delegate.commit.bind(delegate),
    }) as unknown as JsonReferenceSampleStore;
    const firstEditor = makeEditor({ store: racingStore(store), now: () => createdAt });
    const secondEditor = makeEditor({
      store: racingStore(reopened),
      now: () => "2026-09-20T00:05:00.000Z",
    });

    const [first, second] = await Promise.all([
      firstEditor.mobileReview(scope, sample.id),
      secondEditor.mobileReview(scope, sample.id),
    ]);

    expect(first).toEqual(second);
    expect(first.status).toBe("approved");
    const after = await store.snapshot();
    expect(after.mobileReviews).toHaveLength(before.mobileReviews.length + 1);
    expect(after.mobileReviews.filter((review) => review.id === first.id)).toHaveLength(1);
  });

  it("rejects a recomputed receipt that binds sample A intent to sample B result", async () => {
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights }) =>
      analysisFixture(currentSample, rights) });
    await editor.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "swap_rights_a");
    await editor.analyze(scope, sample.id, "swap_analysis_a_seed");

    const sampleB = sampleFixture("2");
    await store.putSample(sampleB);
    await editor.declareRights(scope, {
      referenceSampleId: sampleB.id, sourceKind: "external-style-only", reusableElements: [],
    }, "swap_rights_b");
    const analysisB = await editor.analyze(scope, sampleB.id, "swap_analysis_b");

    const snapshot = await store.snapshot();
    const operationKey = "swap_analysis_forged";
    const analysisAOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "swap_analysis_a_seed")!;
    const expected = referenceEditorOperationMetadata(scope, "analyze", operationKey, analysisAOperation.intent);
    const forged = {
      ...expected,
      resultDigest: digest(canonicalJson(analysisB)),
      result: { kind: "analysis" as const, id: analysisB.id, version: analysisB.version },
    };
    const { digest: _digest, ...payload } = snapshot;
    const nextPayload = { ...payload, editorOperations: [...snapshot.editorOperations, forged] };
    const forgedSnapshot = { ...nextPayload, digest: referenceWorkflowDigest(nextPayload) };
    await expect(store.commit(forgedSnapshot, snapshot.digest))
      .rejects.toThrow("reference-operation-result-binding-mismatch");

    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    await writeFile(snapshotPath, `${JSON.stringify({
      ...forgedSnapshot,
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);

    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ quarantine }).analyze(scope, sample.id, operationKey))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalled();
  });

  it("rejects a recomputed rights receipt whose reusable-elements intent is rebound", async () => {
    const selfOwned = { ...sampleFixture("self"), sourceKind: "self-owned" as const };
    await store.putSample(selfOwned);
    const editor = makeEditor();
    await editor.declareRights(scope, {
      referenceSampleId: selfOwned.id,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
    }, "semantic_rights_video");
    const audioRights = await editor.declareRights(scope, {
      referenceSampleId: selfOwned.id,
      sourceKind: "self-owned",
      reusableElements: ["audio-track"],
    }, "semantic_rights_audio");
    const snapshot = await store.snapshot();
    const videoOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_rights_video")!;
    const forged = {
      ...referenceEditorOperationMetadata(
        scope, "declareRights", "semantic_rights_forged", videoOperation.intent,
      ),
      result: { kind: "rights" as const, id: audioRights.id, version: audioRights.version },
      resultDigest: digest(canonicalJson(audioRights)),
    } as ReferenceEditorOperation;

    await expect(commitWithOperation(store, snapshot, forged))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("rejects a recomputed style receipt whose selected rules are rebound", async () => {
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights }) => {
      const analysis = analysisFixture(currentSample, rights);
      return {
        ...analysis,
        rules: [...analysis.rules, {
          id: "rule-rhythm-2",
          kind: "rhythm" as const,
          description: "closing rhythm",
          referenceStartMs: 0,
          referenceEndMs: 30_000,
          evidenceIds: ["section-1"],
        }],
      };
    } });
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, ["rule-rhythm-1"], "semantic_style_first");
    const second = await editor.confirmStyle(
      scope, analysis.id, ["rule-rhythm-2"], "semantic_style_second",
    );
    const snapshot = await store.snapshot();
    const firstOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_style_first")!;
    const forged = {
      ...referenceEditorOperationMetadata(
        scope, "confirmStyle", "semantic_style_forged", firstOperation.intent,
      ),
      result: { kind: "selection" as const, id: second.id, version: second.version },
      resultDigest: digest(canonicalJson(second)),
    } as ReferenceEditorOperation;

    await expect(commitWithOperation(store, snapshot, forged))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("rejects an analysis receipt rebound across the exact current rights binding", async () => {
    const editor = makeEditor();
    await editor.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "semantic_input_rights_first");
    await editor.analyze(scope, sample.id, "semantic_input_analysis_first");
    await editor.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "semantic_input_rights_second");
    const secondAnalysis = await editor.analyze(scope, sample.id, "semantic_input_analysis_second");
    const snapshot = await store.snapshot();
    const firstAnalysisOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_input_analysis_first")!;
    const forgedAnalysis = {
      ...referenceEditorOperationMetadata(
        scope, "analyze", "semantic_input_analysis_forged", firstAnalysisOperation.intent,
      ),
      result: { kind: "analysis" as const, id: secondAnalysis.id, version: secondAnalysis.version },
      resultDigest: digest(canonicalJson(secondAnalysis)),
    } as ReferenceEditorOperation;
    await expect(commitWithOperation(store, snapshot, forgedAnalysis))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("rejects a mapping receipt rebound across the exact current selection binding", async () => {
    const editor = makeEditor();
    const { analysis: secondAnalysis } = await reachAnalysis(editor);
    await editor.confirmStyle(
      scope, secondAnalysis.id, [secondAnalysis.rules[0]!.id], "semantic_mapping_style_first",
    );
    await editor.createMappingPlan(scope, sample.id, "semantic_mapping_first");
    await editor.confirmStyle(
      scope, secondAnalysis.id, [secondAnalysis.rules[0]!.id], "semantic_mapping_style_second",
    );
    const secondMapping = await editor.createMappingPlan(scope, sample.id, "semantic_mapping_second");
    const snapshot = await store.snapshot();
    const firstMappingOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_mapping_first")!;
    const forgedMapping = {
      ...referenceEditorOperationMetadata(
        scope, "createMappingPlan", "semantic_mapping_forged", firstMappingOperation.intent,
      ),
      result: { kind: "mapping" as const, id: secondMapping.id, version: secondMapping.version },
      resultDigest: digest(canonicalJson(secondMapping)),
    } as ReferenceEditorOperation;
    await expect(commitWithOperation(store, snapshot, forgedMapping))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("rejects a mapping receipt whose selection binding is not an exact persisted selection", async () => {
    const editor = makeEditor();
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "semantic_mapping_style_exact");
    const mapping = await editor.createMappingPlan(scope, sample.id, "semantic_mapping_exact");
    const snapshot = await store.snapshot();
    const forgedMapping = {
      ...structuredClone(mapping),
      id: "mapping-forged-selection-binding",
      contentDigest: digest("mapping-forged-selection-binding"),
      bindings: {
        ...mapping.bindings,
        styleSelection: {
          ...mapping.bindings.styleSelection,
          contentDigest: digest("missing-selection-content"),
        },
      },
    };
    const operation = {
      ...referenceEditorOperationMetadata(scope, "createMappingPlan", "semantic_mapping_exact_forged", {
        method: "createMappingPlan",
        sampleId: sample.id,
        inputBindingDigest: digest(canonicalJson(forgedMapping.bindings)),
        expectedResult: { id: forgedMapping.id, version: forgedMapping.version },
      }),
      result: { kind: "mapping" as const, id: forgedMapping.id, version: forgedMapping.version },
      resultDigest: digest(canonicalJson(forgedMapping)),
    } as ReferenceEditorOperation;
    const { digest: _snapshotDigest, ...payload } = snapshot;
    const next = {
      ...payload,
      mappingPlans: [...snapshot.mappingPlans, forgedMapping],
      editorOperations: [...snapshot.editorOperations, operation],
    };

    await expect(store.commit({ ...next, digest: referenceWorkflowDigest(next) }, snapshot.digest))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("rejects render and revision receipts rebound to another real report", async () => {
    const editor = makeEditor({ revise: async (request) => fakeRevisionResult(request, outputRoot) });
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "semantic_render_first");
    await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("xiaohongshu", "first prompt"),
      "semantic_revise_first",
    );
    const secondRevision = await editor.reviseWithPrompt(
      scope,
      await currentRevisionInput("wechat-channels", "second prompt"),
      "semantic_revise_second",
    );
    const snapshot = await store.snapshot();
    const renderOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_render_first")!;
    const reviseOperation = snapshot.editorOperations.find((operation) =>
      operation.operationKey === "semantic_revise_first")!;
    const forgedRender = {
      ...referenceEditorOperationMetadata(
        scope, "render", "semantic_render_forged", renderOperation.intent,
      ),
      result: { kind: "report" as const, id: secondRevision.id, version: secondRevision.version },
      resultDigest: digest(canonicalJson(secondRevision)),
    } as ReferenceEditorOperation;
    await expect(commitWithOperation(store, snapshot, forgedRender))
      .rejects.toThrow("reference-operation-result-binding-mismatch");

    const forgedRevision = {
      ...referenceEditorOperationMetadata(
        scope, "reviseWithPrompt", "semantic_revise_forged", reviseOperation.intent,
      ),
      result: { kind: "report" as const, id: secondRevision.id, version: secondRevision.version },
      resultDigest: digest(canonicalJson(secondRevision)),
    } as ReferenceEditorOperation;
    await expect(commitWithOperation(store, snapshot, forgedRevision))
      .rejects.toThrow("reference-operation-result-binding-mismatch");
  });

  it("persists a receipt when analysis discovers an existing business result", async () => {
    const editor = makeEditor();
    const { analysis } = await reachAnalysis(editor);
    await editor.analyze(scope, sample.id, "existing_analysis_recovery");
    const sampleB = sampleFixture("existing-analysis-b");
    await store.putSample(sampleB);
    await editor.declareRights(scope, {
      referenceSampleId: sampleB.id, sourceKind: "external-style-only", reusableElements: [],
    }, "existing_analysis_rights_b");
    await expect(editor.analyze(scope, sampleB.id, "existing_analysis_recovery"))
      .rejects.toThrow("reference-operation-key-conflict");
  });

  it("persists a receipt when mapping discovers an existing business result", async () => {
    const editor = makeEditor();
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "existing_mapping_style");
    await editor.createMappingPlan(scope, sample.id, "existing_mapping_seed");
    await editor.createMappingPlan(scope, sample.id, "existing_mapping_recovery");
    const sampleB = sampleFixture("existing-mapping-b");
    await store.putSample(sampleB);
    await editor.declareRights(scope, {
      referenceSampleId: sampleB.id, sourceKind: "external-style-only", reusableElements: [],
    }, "existing_mapping_rights_b");
    const analysisB = await editor.analyze(scope, sampleB.id, "existing_analysis_b");
    await editor.confirmStyle(scope, analysisB.id, [analysisB.rules[0]!.id], "existing_mapping_style_b");
    await expect(editor.createMappingPlan(scope, sampleB.id, "existing_mapping_recovery"))
      .rejects.toThrow("reference-operation-key-conflict");
  });

  it("persists an asset-and-reason-bound invalidation audit", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    await editor.render(scope, confirmed.id, "invalidation_audit_render");

    await expect(Reflect.apply(editor.invalidateForAsset, editor, [
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "invalidation_audit",
    ])).resolves.toBeUndefined();

    const snapshot = await store.snapshot();
    const operation = snapshot.editorOperations.find((candidate) =>
      candidate.operationKey === "invalidation_audit")!;
    expect(operation.intent).toMatchObject({
      method: "invalidateForAsset",
      assetId: fixture.asset.id,
      reason: "asset-permission-withdrawn",
    });
    expect(operation.result).toMatchObject({
      kind: "invalidation",
      assetId: fixture.asset.id,
      reason: "asset-permission-withdrawn",
    });
    if (operation.result.kind !== "invalidation") throw new Error("invalidation-audit-result-required");
    const reboundResults = [
      { ...operation.result, assetId: "another-asset" },
      { ...operation.result, reason: "another-reason" },
    ];
    for (const [index, result] of reboundResults.entries()) {
      const forged = {
        ...referenceEditorOperationMetadata(
          scope, "invalidateForAsset", `invalidation_audit_forged_${index}`, operation.intent,
        ),
        result,
        resultDigest: digest(canonicalJson(result)),
      } as ReferenceEditorOperation;
      await expect(commitWithOperation(store, snapshot, forged))
        .rejects.toThrow("reference-operation-result-binding-mismatch");
    }
  });

  it("rejects a recomputed invalidation receipt that omits another current mapping using the asset", async () => {
    const editor = makeEditor();
    const first = await reachConfirmedMapping(editor);
    const secondSample = sampleFixture("invalidation-complete-b");
    await store.putSample(secondSample);
    const second = await reachConfirmedMappingForSample(
      editor,
      secondSample.id,
      "invalidation_complete_b",
    );
    const before = await store.snapshot();

    await expect(commitForgedMappingOnlyInvalidation(
      store,
      before,
      fixture.asset.id,
      "asset-permission-withdrawn",
      "invalidation_incomplete_attack",
      [first],
    )).rejects.toThrow("reference-invalidation-affected-set-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);

    await expect(editor.invalidateForAsset(
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "invalidation_complete_positive",
    )).resolves.toBeUndefined();
    const after = await store.snapshot();
    const operation = after.editorOperations.find((candidate) =>
      candidate.operationKey === "invalidation_complete_positive")!;
    expect(operation.intent.method === "invalidateForAsset" && operation.intent.affectedMappings)
      .toEqual([mappingIdentity(first), mappingIdentity(second)].sort(compareMappingIdentity));
    expect(after.mappingPlans.filter((plan) =>
      [first.id, second.id].includes(plan.id) && plan.status === "invalidated")).toHaveLength(2);

    await expect(editor.invalidateForAsset(
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "invalidation_true_noop",
    )).resolves.toBeUndefined();
    const noOp = (await store.snapshot()).editorOperations.find((candidate) =>
      candidate.operationKey === "invalidation_true_noop")!;
    expect(noOp.intent.method === "invalidateForAsset" && noOp.intent.affectedMappings).toEqual([]);
    expect(noOp.result.kind === "invalidation" && noOp.result.invalidatedMappings).toEqual([]);
  });

  it("includes an asset used only by an audio mapping row in the complete invalidation set", async () => {
    fixture.asset.mediaKind = "audio";
    fixture.card.technicalFlags = ["ffprobe:audio"];
    fixture.card.confirmedFacts = ["reference-audio:rule-audio-1"];
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights }) => {
      const analysis = analysisFixture(currentSample, rights);
      return {
        ...analysis,
        contentDigest: digest(`audio-analysis-${rights.id}`),
        rules: [{
          ...analysis.rules[0]!,
          id: "rule-audio-1",
          kind: "audio" as const,
          description: "audio rhythm bed",
        }],
      };
    } });
    const confirmed = await reachConfirmedMapping(editor);
    expect(confirmed.mappings).toEqual([
      expect.objectContaining({
        ruleId: "rule-audio-1",
        sourceAssetId: fixture.asset.id,
      }),
    ]);

    await editor.invalidateForAsset(
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "audio_only_invalidation",
    );

    const snapshot = await store.snapshot();
    const operation = snapshot.editorOperations.find((candidate) =>
      candidate.operationKey === "audio_only_invalidation")!;
    expect(operation.intent.method === "invalidateForAsset" && operation.intent.affectedMappings)
      .toEqual([mappingIdentity(confirmed)]);
    expect(snapshot.mappingPlans.find((plan) =>
      plan.id === confirmed.id && plan.version === confirmed.version + 1)?.status).toBe("invalidated");
  });

  it("rejects mapping-only invalidation when rendered state and approvals remain current", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "downstream_attack_render");
    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-downstream-attack-${artifact.kind}`,
        storyId: sample.id,
        revisionId: confirmed.id,
        revision: confirmed.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-downstream-attack-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: confirmed.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    const before = await store.snapshot();

    await expect(commitForgedMappingOnlyInvalidation(
      store,
      before,
      fixture.asset.id,
      "asset-permission-withdrawn",
      "invalidation_downstream_attack",
      [confirmed],
    )).rejects.toThrow("reference-invalidation-downstream-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("invalidates approval audit for an affected draft that is not listed in a report", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "orphan_draft_render");
    const rendered = await store.snapshot();
    const artifact = report.artifacts[0]!;
    const baseVariant = rendered.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion)!;
    const baseDraft = rendered.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion)!;
    const orphanVariant = {
      ...baseVariant,
      id: `${baseVariant.id}-orphan`,
    };
    const orphanDraft = {
      ...baseDraft,
      id: `${baseDraft.id}-orphan`,
      bindings: {
        ...baseDraft.bindings,
        variantPlan: { ...baseDraft.bindings.variantPlan, id: orphanVariant.id },
      },
      outputDigest: digest("orphan-draft-output"),
      outputPath: `${baseDraft.outputPath}.orphan`,
    };
    await store.putVariantPlan(orphanVariant);
    await store.putDraft(orphanDraft);
    await store.putApproval({
      id: "approval-orphan-draft",
      storyId: sample.id,
      revisionId: confirmed.id,
      revision: confirmed.version,
      artifactId: orphanDraft.id,
      artifactDigest: orphanDraft.outputDigest,
      coverDigest: digest("orphan-draft-cover"),
      stillDigests: [],
      renderInputDigest: confirmed.contentDigest,
      variant: orphanVariant.kind,
      reviewerId: scope.actorId,
      approvedAt: createdAt,
      status: "active",
    });

    await editor.invalidateForAsset(
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "orphan_draft_invalidation",
    );

    const after = await store.snapshot();
    expect(after.approvals.filter((approval) => approval.id === "approval-orphan-draft")
      .map((approval) => approval.status).sort()).toEqual(["active", "invalidated"]);
  });

  it("rejects a no-op invalidation receipt that smuggles an unrelated invalidated mapping", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const before = await store.snapshot();

    await expect(commitForgedMappingOnlyInvalidation(
      store,
      before,
      "asset-not-used-by-any-mapping",
      "asset-permission-withdrawn",
      "invalidation_noop_smuggled_attack",
      [],
      [confirmed],
    )).rejects.toThrow("reference-invalidation-unattributed-transition");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("rejects a receiptless mapping invalidation through the raw commit boundary", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const before = await store.snapshot();
    const { digest: _digest, ...payload } = before;
    const invalidated = {
      ...confirmed,
      version: confirmed.version + 1,
      status: "invalidated" as const,
    };
    const next = { ...payload, mappingPlans: [...before.mappingPlans, invalidated] };

    await expect(store.commit({ ...next, digest: referenceWorkflowDigest(next) }, before.digest))
      .rejects.toThrow("reference-invalidation-unattributed-transition");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("rejects mapping invalidation smuggled behind a non-invalidation receipt", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const before = await store.snapshot();
    const draft = before.mappingPlans.find((plan) =>
      plan.id === confirmed.id && plan.version === confirmed.version - 1)!;
    const intent = {
      method: "confirmMappingPlan" as const,
      planId: confirmed.id,
      inputBindingDigest: digest(canonicalJson(draft)),
      expectedResult: { id: confirmed.id, version: confirmed.version },
    };
    const metadata = referenceEditorOperationMetadata(
      scope,
      "confirmMappingPlan",
      "smuggled_non_invalidation_receipt",
      intent,
    );
    const operation: ReferenceEditorOperation = {
      ...metadata,
      result: { kind: "mapping", id: confirmed.id, version: confirmed.version },
      resultDigest: digest(canonicalJson(confirmed)),
    };
    const invalidated = {
      ...confirmed,
      version: confirmed.version + 1,
      status: "invalidated" as const,
    };
    const { digest: _digest, ...payload } = before;
    const next = {
      ...payload,
      mappingPlans: [...before.mappingPlans, invalidated],
      editorOperations: [...before.editorOperations, operation],
    };

    await expect(store.commit({ ...next, digest: referenceWorkflowDigest(next) }, before.digest))
      .rejects.toThrow("reference-invalidation-unattributed-transition");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("rejects receiptless invalidated records for every downstream collection through raw commit", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "receiptless_downstream_render");
    const artifact = report.artifacts[0]!;
    const approval = {
      id: "approval-receiptless-downstream",
      storyId: sample.id,
      revisionId: confirmed.id,
      revision: confirmed.version,
      artifactId: artifact.draftId,
      artifactDigest: artifact.outputDigest,
      coverDigest: digest("receiptless-downstream-cover"),
      stillDigests: [],
      renderInputDigest: confirmed.contentDigest,
      variant: artifact.kind,
      reviewerId: scope.actorId,
      approvedAt: createdAt,
      status: "active" as const,
    };
    await store.putApproval(approval);
    const before = await store.snapshot();
    const variant = before.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion)!;
    const draft = before.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion)!;
    const review = before.mobileReviews.find((candidate) =>
      candidate.bindings.executionReport.id === report.id &&
      candidate.bindings.executionReport.version === report.version)!;
    const invalidVariant = { ...variant, version: variant.version + 1, status: "invalidated" as const };
    const invalidDraft = {
      ...draft,
      id: `${draft.id}-receiptless-invalidated`,
      status: "invalidated" as const,
    };
    const invalidReport = { ...report, version: report.version + 1, status: "invalidated" as const };
    const invalidApproval = { ...approval, status: "invalidated" as const };
    const invalidReview = {
      ...review,
      id: `${review.id}-receiptless-invalidated`,
      bindings: {
        ...review.bindings,
        executionReport: { ...review.bindings.executionReport, version: invalidReport.version },
      },
      artifacts: review.artifacts.map((item) => ({ ...item, status: "invalidated" as const })),
      executionReport: { ...review.executionReport, version: invalidReport.version },
      status: "invalidated" as const,
    };
    const { digest: _digest, ...payload } = before;
    const cases = [
      { name: "variant", patch: { variantPlans: [...before.variantPlans, invalidVariant] } },
      { name: "draft", patch: { drafts: [...before.drafts, invalidDraft] } },
      { name: "report", patch: { executionReports: [...before.executionReports, invalidReport] } },
      { name: "approval", patch: { approvals: [...before.approvals, invalidApproval] } },
      {
        name: "mobile",
        patch: {
          executionReports: [...before.executionReports, invalidReport],
          mobileReviews: [...before.mobileReviews, invalidReview],
        },
      },
    ];

    for (const item of cases) {
      const next = { ...payload, ...item.patch };
      await expect(store.commit({ ...next, digest: before.digest }, before.digest), item.name)
        .rejects.toThrow("reference-invalidation-downstream-mismatch");
      await expect(store.snapshot()).resolves.toEqual(before);
    }
  });

  it("rejects standalone invalidated writes for every coordinated downstream collection", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "standalone_downstream_render");
    const artifact = report.artifacts[0]!;
    const approval = {
      id: "approval-standalone-downstream",
      storyId: sample.id,
      revisionId: confirmed.id,
      revision: confirmed.version,
      artifactId: artifact.draftId,
      artifactDigest: artifact.outputDigest,
      coverDigest: digest("standalone-downstream-cover"),
      stillDigests: [],
      renderInputDigest: confirmed.contentDigest,
      variant: artifact.kind,
      reviewerId: scope.actorId,
      approvedAt: createdAt,
      status: "active" as const,
    };
    await store.putApproval(approval);
    const before = await store.snapshot();
    const variant = before.variantPlans.find((candidate) => candidate.id === artifact.variantPlanId)!;
    const draft = before.drafts.find((candidate) => candidate.id === artifact.draftId)!;
    const review = before.mobileReviews.find((candidate) =>
      candidate.bindings.executionReport.id === report.id)!;
    const calls: Array<readonly [string, () => Promise<unknown>]> = [
      ["variant", () => store.putVariantPlan({
        ...variant, version: variant.version + 1, status: "invalidated",
      })],
      ["draft", () => store.putDraft({
        ...draft, id: `${draft.id}-standalone-invalidated`, status: "invalidated",
      })],
      ["report", () => store.putExecutionReport({
        ...report, version: report.version + 1, status: "invalidated",
      })],
      ["approval", () => store.putApproval({ ...approval, status: "invalidated" })],
      ["mobile", () => store.putMobileReview({
        ...review,
        id: `${review.id}-standalone-invalidated`,
        artifacts: review.artifacts.map((item) => ({ ...item, status: "invalidated" as const })),
        status: "invalidated",
      })],
    ];

    for (const [name, call] of calls) {
      await expect(call(), name).rejects.toThrow("reference-invalidation-commit-required");
      await expect(store.snapshot()).resolves.toEqual(before);
    }
  });

  it("rejects a downstream invalidation smuggled behind a non-invalidation receipt", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "smuggled_downstream_render");
    const before = await store.snapshot();
    const variant = before.variantPlans.find((candidate) =>
      candidate.bindings.mappingPlan.id === confirmed.id && candidate.status === "rendered")!;
    const operation = confirmedMappingReplayOperation(
      before,
      confirmed,
      "smuggled_downstream_non_invalidation_receipt",
    );
    const { digest: _digest, ...payload } = before;
    const next = {
      ...payload,
      variantPlans: [...before.variantPlans, {
        ...variant,
        version: variant.version + 1,
        status: "invalidated" as const,
      }],
      editorOperations: [...before.editorOperations, operation],
    };

    await expect(store.commit({ ...next, digest: before.digest }, before.digest))
      .rejects.toThrow("reference-invalidation-downstream-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);
    await expect(editor.mobileReview(scope, sample.id)).resolves.toMatchObject({
      executionReport: { id: report.id, version: report.version },
    });
  });

  it("accepts one atomic commit with asset and disjoint rights invalidations", async () => {
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights, operationKey }) => ({
      ...analysisFixture(currentSample, rights),
      id: `analysis-${currentSample.id}-${operationKey}`,
      contentDigest: digest(`analysis-${currentSample.id}-${operationKey}`),
    }) });
    const assetA = fixture.asset;
    const mappingA = await reachConfirmedMapping(editor);
    const sampleB = sampleFixture("atomic-b");
    await store.putSample(sampleB);
    fixture = materialFixture("atomic-b");
    const mappingB = await reachConfirmedMappingForSample(editor, sampleB.id, "atomic_b");
    const before = await store.snapshot();

    const openBranch = async (name: string) => {
      const branchRoot = join(root, name);
      const workflowDirectory = join(
        branchRoot,
        "reference-workflows",
        digest(scope.customerId),
        digest(scope.sessionId),
      );
      await mkdir(workflowDirectory, { recursive: true });
      await writeFile(join(workflowDirectory, "snapshot.json"), `${JSON.stringify({
        ...before,
        integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
      })}\n`);
      return JsonReferenceSampleStore.open(branchRoot, scope);
    };
    const assetBranch = await openBranch("atomic-asset-branch");
    const rightsBranch = await openBranch("atomic-rights-branch");
    await makeEditor({ store: assetBranch }).invalidateForAsset(
      scope,
      assetA.id,
      "atomic_asset_invalidation",
    );
    await makeEditor({ store: rightsBranch }).declareRights(scope, {
      referenceSampleId: sampleB.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "atomic_rights_invalidation");
    const afterAsset = await assetBranch.snapshot();
    const afterRights = await rightsBranch.snapshot();
    const mergeNew = <T>(current: readonly T[], ...branches: ReadonlyArray<readonly T[]>): T[] => {
      const merged = [...current];
      const seen = new Set(current.map((record) => canonicalJson(record)));
      for (const branch of branches) {
        for (const record of branch) {
          const identity = canonicalJson(record);
          if (!seen.has(identity)) {
            seen.add(identity);
            merged.push(record);
          }
        }
      }
      return merged;
    };
    const { digest: _digest, ...payload } = before;
    const combined = {
      ...payload,
      rightsDeclarations: mergeNew(
        before.rightsDeclarations,
        afterAsset.rightsDeclarations,
        afterRights.rightsDeclarations,
      ),
      styleSelections: mergeNew(
        before.styleSelections,
        afterAsset.styleSelections,
        afterRights.styleSelections,
      ),
      mappingPlans: mergeNew(before.mappingPlans, afterAsset.mappingPlans, afterRights.mappingPlans),
      editorOperations: mergeNew(
        before.editorOperations,
        afterAsset.editorOperations,
        afterRights.editorOperations,
      ),
    };

    await expect(store.commit({
      ...combined,
      digest: referenceWorkflowDigest(combined),
    }, before.digest)).resolves.toBeUndefined();
    const committed = await store.snapshot();
    expect(committed.mappingPlans.find((plan) =>
      plan.id === mappingA.id && plan.version === mappingA.version + 1)?.status).toBe("invalidated");
    expect(committed.mappingPlans.find((plan) =>
      plan.id === mappingB.id && plan.version === mappingB.version + 1)?.status).toBe("invalidated");
  });

  it("accepts sequential overlapping asset invalidations in one atomic commit", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const second = materialFixture("atomic-overlap-b");
    const firstRow = confirmed.mappings[0]!;
    const secondRow = {
      ...firstRow,
      id: `${firstRow.id}-atomic-overlap-b`,
      sourceBindingId: `${firstRow.sourceBindingId}-atomic-overlap-b`,
      sourceAssetId: second.asset.id,
      sourceDigest: second.asset.digest,
      permission: second.mappingAsset.permission,
    };
    const dualAssetMapping = {
      ...confirmed,
      version: confirmed.version + 1,
      contentDigest: digest("atomic-overlap-content"),
      permissionSetDigest: digest("atomic-overlap-permissions"),
      mappings: [...confirmed.mappings, secondRow],
    };
    const beforeSeed = await store.snapshot();
    const { digest: _seedDigest, ...seedPayload } = beforeSeed;
    const seededPayload = {
      ...seedPayload,
      mappingPlans: [...beforeSeed.mappingPlans, dualAssetMapping],
    };
    const snapshotPath = join(
      root,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
      "snapshot.json",
    );
    await writeFile(snapshotPath, `${JSON.stringify({
      ...seededPayload,
      digest: referenceWorkflowDigest(seededPayload),
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);
    const before = await store.snapshot();
    const branchRoot = join(root, "atomic-overlap-branch");
    const workflowDirectory = join(
      branchRoot,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
    );
    await mkdir(workflowDirectory, { recursive: true });
    await writeFile(join(workflowDirectory, "snapshot.json"), `${JSON.stringify({
      ...before,
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);
    const branch = await JsonReferenceSampleStore.open(branchRoot, scope);
    const branchEditor = makeEditor({ store: branch });
    await branchEditor.invalidateForAsset(scope, fixture.asset.id, "atomic_overlap_asset_a");
    await branchEditor.invalidateForAsset(scope, second.asset.id, "atomic_overlap_asset_b");
    const sequential = await branch.snapshot();

    await expect(store.commit(sequential, before.digest)).resolves.toBeUndefined();
    const committed = await store.snapshot();
    const operations = committed.editorOperations.filter((operation) =>
      operation.operationKey.startsWith("atomic_overlap_asset_"));
    expect(operations).toHaveLength(2);
    expect(operations[0]?.result).toMatchObject({ kind: "invalidation" });
    expect(operations[1]?.result).toEqual({
      kind: "invalidation",
      assetId: second.asset.id,
      reason: "asset-permission-invalidated",
      affectedSamples: [],
      invalidatedSamples: [],
      affectedMappings: [],
      invalidatedMappings: [],
    });
  });

  it("rejects an operation ledger that analyzes before its rights result exists", async () => {
    const before = await store.snapshot();
    const branchRoot = join(root, "atomic-operation-order-branch");
    const workflowDirectory = join(
      branchRoot,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
    );
    await mkdir(workflowDirectory, { recursive: true });
    await writeFile(join(workflowDirectory, "snapshot.json"), `${JSON.stringify({
      ...before,
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);
    const branch = await JsonReferenceSampleStore.open(branchRoot, scope);
    const branchEditor = makeEditor({ store: branch });
    await branchEditor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "atomic_order_rights");
    await branchEditor.analyze(scope, sample.id, "atomic_order_analysis");
    const sequential = await branch.snapshot();
    const added = sequential.editorOperations.slice(before.editorOperations.length);
    expect(added.map((operation) => operation.method)).toEqual(["declareRights", "analyze"]);
    const { digest: _digest, ...payload } = sequential;
    const swappedPayload = {
      ...payload,
      editorOperations: [...before.editorOperations, added[1]!, added[0]!],
    };

    await expect(store.commit({
      ...swappedPayload,
      digest: referenceWorkflowDigest(swappedPayload),
    }, before.digest)).rejects.toThrow("reference-operation-result-binding-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);
    await expect(store.commit(sequential, before.digest)).resolves.toBeUndefined();
  });

  it("isolates a marker-less legacy ledger with future dependencies", async () => {
    const futureRoot = join(root, "legacy-future-ledger");
    const futureStore = await JsonReferenceSampleStore.open(futureRoot, scope);
    await futureStore.putSample(sample);
    const futureEditor = makeEditor({ store: futureStore });
    await futureEditor.declareRights(scope, {
      referenceSampleId: sample.id,
      sourceKind: "external-style-only",
      reusableElements: [],
    }, "legacy_future_rights");
    await futureEditor.analyze(scope, sample.id, "legacy_future_analysis");
    const futurePath = join(
      futureRoot,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
      "snapshot.json",
    );
    const futureStored = JSON.parse(await readFile(futurePath, "utf8")) as Record<string, unknown> & {
      editorOperations: ReferenceEditorOperation[];
    };
    const futureRights = futureStored.editorOperations.find((operation) => operation.method === "declareRights")!;
    const futureAnalysis = futureStored.editorOperations.find((operation) => operation.method === "analyze")!;
    futureStored.editorOperations = [futureAnalysis, futureRights];
    const { digest: _futureDigest, integrityVersion: _futureVersion, ...futurePayload } = futureStored;
    delete futureStored.integrityVersion;
    futureStored.digest = referenceWorkflowDigest(futurePayload as never);
    const futureBytes = `${JSON.stringify(futureStored)}\n`;
    await writeFile(futurePath, futureBytes);

    await expect(futureStore.snapshot()).rejects.toThrow("reference-snapshot-integrity-version-required");
    await expect(readFile(futurePath, "utf8")).resolves.toBe(futureBytes);
    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ store: futureStore, quarantine }).mobileReview(scope, sample.id))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-snapshot-integrity-version-required");

  });

  it("isolates a marker-less legacy ledger with incomplete downstream invalidation", async () => {
    const incompleteRoot = join(root, "legacy-incomplete-invalidation");
    const incompleteOutput = join(incompleteRoot, "outputs");
    await mkdir(incompleteOutput, { recursive: true });
    const incompleteStore = await JsonReferenceSampleStore.open(incompleteRoot, scope);
    await incompleteStore.putSample(sample);
    const incompleteEditor = makeEditor({
      store: incompleteStore,
      render: async (request) => fakeRenderedResult(
        request.mappingPlan, request.analysis, request.operationKey, incompleteOutput,
      ),
    });
    const incompleteMapping = await reachConfirmedMapping(incompleteEditor);
    const incompleteReport = await incompleteEditor.render(
      scope, incompleteMapping.id, "legacy_incomplete_render",
    );
    for (const artifact of incompleteReport.artifacts) {
      await incompleteStore.putApproval({
        id: `approval-legacy-incomplete-${artifact.kind}`,
        storyId: sample.id,
        revisionId: incompleteMapping.id,
        revision: incompleteMapping.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-legacy-incomplete-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: incompleteMapping.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    await incompleteEditor.invalidateForAsset(
      scope, fixture.asset.id, "legacy_incomplete_invalidation",
    );
    const incompletePath = join(
      incompleteRoot,
      "reference-workflows",
      digest(scope.customerId),
      digest(scope.sessionId),
      "snapshot.json",
    );
    const incompleteStored = JSON.parse(await readFile(incompletePath, "utf8")) as Record<string, unknown> & {
      approvals: Array<{ status: string }>;
    };
    incompleteStored.approvals = incompleteStored.approvals.filter((approval) => approval.status !== "invalidated");
    const { digest: _incompleteDigest, integrityVersion: _incompleteVersion, ...incompletePayload } = incompleteStored;
    delete incompleteStored.integrityVersion;
    incompleteStored.digest = referenceWorkflowDigest(incompletePayload as never);
    const incompleteBytes = `${JSON.stringify(incompleteStored)}\n`;
    await writeFile(incompletePath, incompleteBytes);

    await expect(incompleteStore.snapshot()).rejects.toThrow("reference-snapshot-integrity-version-required");
    await expect(readFile(incompletePath, "utf8")).resolves.toBe(incompleteBytes);
    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ store: incompleteStore, quarantine }).mobileReview(scope, sample.id))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-snapshot-integrity-version-required");
  });

  it("rejects duplicate, noncanonical, and oversized invalidation identity lists", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    await editor.invalidateForAsset(scope, fixture.asset.id, "identity_list_seed");
    const snapshot = await store.snapshot();
    const seed = snapshot.editorOperations.find((candidate) =>
      candidate.operationKey === "identity_list_seed")!;
    if (seed.intent.method !== "invalidateForAsset" || seed.result.kind !== "invalidation") {
      throw new Error("invalidation-seed-required");
    }
    const affected = mappingIdentity(confirmed);
    const invalidated = seed.result.invalidatedMappings[0]!;
    const oversized = Array.from({ length: 257 }, (_, index) => ({
      id: `mapping-${String(index).padStart(3, "0")}`,
      version: 1,
      contentDigest: digest(`mapping-${index}`),
    }));
    const violations = [
      {
        key: "identity_duplicate",
        affected: [affected, affected],
        invalidated: [invalidated, invalidated],
        error: "reference-invalidation-identity-duplicate",
      },
      {
        key: "identity_noncanonical",
        affected: [
          { id: "mapping-z", version: 1, contentDigest: digest("mapping-z") },
          { id: "mapping-a", version: 1, contentDigest: digest("mapping-a") },
        ],
        invalidated: [
          { id: "mapping-z", version: 2, contentDigest: digest("mapping-z") },
          { id: "mapping-a", version: 2, contentDigest: digest("mapping-a") },
        ],
        error: "reference-invalidation-identities-not-canonical",
      },
      {
        key: "identity_oversized",
        affected: oversized,
        invalidated: oversized.map((identity) => ({ ...identity, version: 2 })),
        error: "reference-invalidation-identities-too-many",
      },
    ];

    for (const violation of violations) {
      for (const field of ["intent", "resultAffected", "resultInvalidated"] as const) {
        const operationKey = `${violation.key}_${field}`;
        const base = referenceEditorOperationMetadata(scope, "invalidateForAsset", operationKey, {
          method: "invalidateForAsset",
          assetId: fixture.asset.id,
          reason: "asset-permission-withdrawn",
          affectedMappings: [affected],
        });
        const intent = {
          ...base.intent,
          affectedMappings: field === "intent" ? violation.affected : [affected],
        };
        const result = {
          kind: "invalidation" as const,
          assetId: fixture.asset.id,
          reason: "asset-permission-withdrawn",
          affectedMappings: field === "resultAffected" ? violation.affected : [affected],
          invalidatedMappings: field === "resultInvalidated" ? violation.invalidated : [invalidated],
        };
        const forged = {
          ...base,
          intent,
          intentDigest: digest(canonicalJson({
            customerId: scope.customerId,
            sessionId: scope.sessionId,
            method: "invalidateForAsset",
            actorId: scope.actorId,
            operationKey,
            subjectId: fixture.asset.id,
            intent,
          })),
          result,
          resultDigest: digest(canonicalJson(result)),
        } as ReferenceEditorOperation;
        const { digest: _digest, ...payload } = snapshot;
        const next = { ...payload, editorOperations: [...snapshot.editorOperations, forged] };
        await expect(store.commit({ ...next, digest: snapshot.digest }, snapshot.digest))
          .rejects.toThrow(violation.error);
        await expect(store.snapshot()).resolves.toEqual(snapshot);
      }
    }
  });

  it("rejects a second current confirmed mapping ID instead of selecting old approved output by ID order", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "ambiguous_old_render");
    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-ambiguous-old-${artifact.kind}`,
        storyId: sample.id,
        revisionId: confirmed.id,
        revision: confirmed.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-ambiguous-old-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: confirmed.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    const approved = await editor.mobileReview(scope, sample.id);
    expect(approved.status).toBe("approved");
    const before = await store.snapshot();
    const ambiguous = {
      ...confirmed,
      id: `000-${confirmed.id}`,
      version: 1,
      confirmationReceiptId: `confirmation-ambiguous-${confirmed.id}`,
      confirmedAt: "2026-09-20T00:01:00.000Z",
      createdAt: "2026-09-20T00:01:00.000Z",
    };

    await expect(store.putMappingPlan(ambiguous))
      .rejects.toThrow("reference-mapping-put-requires-initial-draft");
    await expect(store.snapshot()).resolves.toEqual(before);
    await expect(editor.mobileReview(scope, sample.id)).resolves.toEqual(approved);

    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    const { digest: _digest, ...payload } = before;
    const forgedPayload = { ...payload, mappingPlans: [...before.mappingPlans, ambiguous] };
    await writeFile(snapshotPath, `${JSON.stringify({
      ...forgedPayload,
      digest: referenceWorkflowDigest(forgedPayload),
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);
    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ quarantine }).mobileReview(scope, sample.id))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-current-mapping-ambiguous");
  });

  it("invalidates rendered history when a higher confirmed version shadows the same mapping ID", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "same_id_history_render");
    const artifact = report.artifacts[0]!;
    await store.putApproval({
      id: "approval-same-id-history",
      storyId: sample.id,
      revisionId: confirmed.id,
      revision: confirmed.version,
      artifactId: artifact.draftId,
      artifactDigest: artifact.outputDigest,
      coverDigest: digest("same-id-history-cover"),
      stillDigests: [],
      renderInputDigest: confirmed.contentDigest,
      variant: artifact.kind,
      reviewerId: scope.actorId,
      approvedAt: createdAt,
      status: "active",
    });
    const shadowing = {
      ...confirmed,
      version: confirmed.version + 1,
      createdAt: "2026-09-20T00:02:00.000Z",
    };
    const beforeShadow = await store.snapshot();
    const { digest: _shadowDigest, ...shadowPayload } = beforeShadow;
    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    const seededPayload = {
      ...shadowPayload,
      mappingPlans: [...beforeShadow.mappingPlans, shadowing],
    };
    await writeFile(snapshotPath, `${JSON.stringify({
      ...seededPayload,
      digest: referenceWorkflowDigest(seededPayload),
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    })}\n`);
    await expect(store.snapshot()).resolves.toMatchObject({
      mappingPlans: expect.arrayContaining([shadowing]),
    });

    await expect(editor.invalidateForAsset(
      scope,
      { assetId: fixture.asset.id, reason: "asset-permission-withdrawn" },
      "same_id_history_invalidation",
    )).resolves.toBeUndefined();

    const after = await store.snapshot();
    expect(after.mappingPlans.find((plan) =>
      plan.id === confirmed.id && plan.version === shadowing.version + 1)?.status).toBe("invalidated");
    expect(after.executionReports.find((candidate) =>
      candidate.id === report.id && candidate.version === report.version + 1)?.status).toBe("invalidated");
    expect(after.approvals.filter((approval) => approval.id === "approval-same-id-history")
      .map((approval) => approval.status).sort()).toEqual(["active", "invalidated"]);
  });

  it("keeps invalidated mapping IDs terminal and blocks reuse of a withdrawn asset", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    await editor.invalidateForAsset(scope, fixture.asset.id, "terminal_mapping_invalidation");
    const invalidated = (await store.snapshot()).mappingPlans.find((plan) =>
      plan.id === confirmed.id && plan.status === "invalidated")!;
    const before = await store.snapshot();
    const terminalDraft = {
      ...confirmed,
      version: invalidated.version + 1,
      status: "draft" as const,
      confirmedByActorId: null,
      confirmationReceiptId: null,
      confirmedAt: null,
    };
    const { digest: _digest, ...payload } = before;

    await expect(store.commit({
      ...payload,
      mappingPlans: [...before.mappingPlans, terminalDraft],
      digest: before.digest,
    }, before.digest)).rejects.toThrow("reference-invalidated-mapping-terminal");
    await expect(store.putMappingPlan({
      ...confirmed,
      id: `${confirmed.id}-revived-withdrawn-asset`,
      version: 1,
      status: "draft",
      confirmedByActorId: null,
      confirmationReceiptId: null,
      confirmedAt: null,
    })).rejects.toThrow("reference-invalidated-asset-reused");
    await expect(store.putMappingPlan({
      ...confirmed,
      version: invalidated.version + 1,
    })).rejects.toThrow("reference-mapping-put-requires-initial-draft");
    await expect(store.snapshot()).resolves.toEqual(before);
    await expect(editor.render(scope, confirmed.id, "terminal_mapping_render"))
      .rejects.toThrow("confirmed-reference-mapping-plan-required");
  });

  it("keeps exact confirmed and invalidated mapping puts idempotent while rejecting new privileged states", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const beforeConfirmedReplay = await store.snapshot();

    await expect(store.putMappingPlan(confirmed)).resolves.toEqual(confirmed);
    await expect(store.snapshot()).resolves.toEqual(beforeConfirmedReplay);

    await editor.invalidateForAsset(scope, fixture.asset.id, "mapping_put_replay_invalidation");
    const invalidated = (await store.snapshot()).mappingPlans.find((plan) =>
      plan.id === confirmed.id && plan.status === "invalidated")!;
    const beforeInvalidatedReplay = await store.snapshot();

    await expect(store.putMappingPlan(invalidated)).resolves.toEqual(invalidated);
    await expect(store.putMappingPlan({
      ...confirmed,
      id: `${confirmed.id}-forged-confirmed`,
      version: 1,
    })).rejects.toThrow("reference-mapping-put-requires-initial-draft");
    await expect(store.putMappingPlan({
      ...invalidated,
      id: `${invalidated.id}-forged-invalidated`,
      version: 1,
    })).rejects.toThrow("reference-invalidation-commit-required");
    await expect(store.snapshot()).resolves.toEqual(beforeInvalidatedReplay);
  });

  it("rejects a self-consistent forged mapping confirmation receipt", async () => {
    const editor = makeEditor();
    const { analysis } = await reachAnalysis(editor);
    await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "forged_confirm_style");
    const draft = await editor.createMappingPlan(scope, sample.id, "forged_confirm_mapping");
    const before = await store.snapshot();
    const forged = {
      ...draft,
      version: draft.version + 1,
      status: "confirmed" as const,
      confirmedByActorId: scope.actorId,
      confirmationReceiptId: "forged-confirmation-receipt",
      confirmedAt: createdAt,
    };
    const operationKey = "forged_mapping_confirmation_receipt";
    const metadata = referenceEditorOperationMetadata(scope, "confirmMappingPlan", operationKey, {
      method: "confirmMappingPlan",
      planId: draft.id,
      inputBindingDigest: digest(canonicalJson(draft)),
      expectedResult: { id: forged.id, version: forged.version },
    });
    const operation = {
      ...metadata,
      result: { kind: "mapping" as const, id: forged.id, version: forged.version },
      resultDigest: digest(canonicalJson(forged)),
    } as ReferenceEditorOperation;
    const { digest: _digest, ...payload } = before;
    const next = {
      ...payload,
      mappingPlans: [...before.mappingPlans, forged],
      editorOperations: [...before.editorOperations, operation],
    };

    await expect(store.commit({
      ...next,
      digest: referenceWorkflowDigest(next),
    }, before.digest)).rejects.toThrow("reference-operation-result-binding-mismatch");
    await expect(store.snapshot()).resolves.toEqual(before);
  });

  it("invalidates the highest report version for a mapping instead of choosing by report ID", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const first = await editor.render(scope, confirmed.id, "report_order_first");
    const higher = {
      ...first,
      id: `zz-${first.id}`,
      version: first.version + 1,
      createdAt: "2026-09-20T00:03:00.000Z",
    };
    await store.putExecutionReport(higher);

    await expect(editor.render(scope, confirmed.id, "report_order_second"))
      .resolves.toMatchObject({ status: "reviewable" });

    const after = await store.snapshot();
    expect(after.executionReports.find((report) =>
      report.id === higher.id && report.version === higher.version + 1)?.status).toBe("invalidated");
    expect(after.executionReports.some((report) =>
      report.id === first.id && report.version === first.version + 1 && report.status === "invalidated"))
      .toBe(false);
  });

  it("rejects a second current report ID for one mapping without changing drafts or approvals", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "ambiguous_report_render");
    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-ambiguous-report-${artifact.kind}`,
        storyId: sample.id,
        revisionId: confirmed.id,
        revision: confirmed.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-ambiguous-report-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: confirmed.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    const before = await store.snapshot();
    const ambiguous = { ...report, id: `000-${report.id}` };

    await expect(store.putExecutionReport(ambiguous))
      .rejects.toThrow("reference-current-report-ambiguous");
    await expect(store.snapshot()).resolves.toEqual(before);
    expect(before.drafts.filter((draft) => draft.status === "reviewable")).toHaveLength(4);
    expect(before.approvals.filter((approval) => approval.status === "active")).toHaveLength(4);
  });

  it("recovers through a new mapping after the old mapping has a higher invalidated version", async () => {
    const editor = makeEditor();
    const oldMapping = await reachConfirmedMapping(editor);
    const oldReport = await editor.render(scope, oldMapping.id, "recovery_old_render");
    for (const artifact of oldReport.artifacts) {
      await store.putApproval({
        id: `approval-recovery-old-${artifact.kind}`,
        storyId: sample.id,
        revisionId: oldMapping.id,
        revision: oldMapping.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-recovery-old-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: oldMapping.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    expect((await editor.mobileReview(scope, sample.id)).status).toBe("approved");

    await editor.invalidateForAsset(scope, fixture.asset.id, "recovery_invalidate_old");
    fixture = materialFixture("2");
    const analysis = latestAnalysis(await store.snapshot());
    const newSelection = await editor.confirmStyle(
      scope, analysis.id, [analysis.rules[0]!.id], "recovery_new_selection",
    );
    const newDraft = await editor.createMappingPlan(scope, sample.id, "recovery_new_mapping");
    expect(newDraft.bindings.styleSelection.id).toBe(newSelection.id);
    const newMapping = await editor.confirmMappingPlan(scope, newDraft.id, "recovery_new_confirm");
    const newReport = await editor.render(scope, newMapping.id, "recovery_new_render");
    const reviewable = await editor.mobileReview(scope, sample.id);
    expect(reviewable.status).toBe("reviewable");
    expect(reviewable.bindings.mappingPlan.id).toBe(newMapping.id);
    expect(reviewable.executionReport.id).toBe(newReport.id);

    for (const artifact of newReport.artifacts) {
      await store.putApproval({
        id: `approval-recovery-new-${artifact.kind}`,
        storyId: sample.id,
        revisionId: newMapping.id,
        revision: newMapping.version,
        artifactId: artifact.draftId,
        artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-recovery-new-${artifact.kind}`),
        stillDigests: [],
        renderInputDigest: newMapping.contentDigest,
        variant: artifact.kind,
        reviewerId: scope.actorId,
        approvedAt: createdAt,
        status: "active",
      });
    }
    const approved = await editor.mobileReview(scope, sample.id);
    expect(approved.status).toBe("approved");
    expect(approved.bindings.mappingPlan.id).toBe(newMapping.id);

    const snapshot = await store.snapshot();
    expect(snapshot.mappingPlans.some((plan) =>
      plan.id === oldMapping.id && plan.version > newMapping.version && plan.status === "invalidated"))
      .toBe(true);
    expect(snapshot.mobileReviews.some((review) =>
      review.bindings.mappingPlan.id === oldMapping.id && review.status === "invalidated"))
      .toBe(true);
    expect(approved.executionReport.id).not.toBe(oldReport.id);
  });

  it("quarantines a historical receipt missing its canonical intent fields", async () => {
    const editor = makeEditor();
    await editor.declareRights(scope, {
      referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [],
    }, "legacy_rights");
    await editor.analyze(scope, sample.id, "legacy_analysis");

    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    const stored = JSON.parse(await readFile(snapshotPath, "utf8")) as Record<string, unknown> & {
      editorOperations: Array<Record<string, unknown>>;
    };
    const legacyOperation = { ...stored.editorOperations[1]! };
    delete legacyOperation.operationKey;
    delete legacyOperation.intent;
    stored.editorOperations = [stored.editorOperations[0]!, legacyOperation];
    const { digest: _storedDigest, ...legacyPayload } = stored;
    stored.digest = digest(canonicalJson(legacyPayload));
    await writeFile(snapshotPath, `${JSON.stringify(stored)}\n`);

    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ quarantine }).analyze(scope, sample.id, "legacy_analysis"))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-operation-metadata-missing");
  });

  it("returns the exact persisted mobile review after time advances and versions approval changes", async () => {
    let clock = createdAt;
    const editor = makeEditor({ now: () => clock });
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "clock_render");
    const persisted = (await store.snapshot()).mobileReviews.at(-1)!;

    clock = "2026-09-20T00:05:00.000Z";
    await expect(editor.mobileReview(scope, sample.id)).resolves.toEqual(persisted);
    await expect(editor.mobileReview(scope, sample.id)).resolves.toEqual(persisted);
    expect((await store.snapshot()).mobileReviews).toHaveLength(1);

    for (const artifact of report.artifacts) {
      await store.putApproval({
        id: `approval-clock-${artifact.kind}`, storyId: sample.id,
        revisionId: confirmed.id, revision: confirmed.version,
        artifactId: artifact.draftId, artifactDigest: artifact.outputDigest,
        coverDigest: digest(`cover-clock-${artifact.kind}`), stillDigests: [],
        renderInputDigest: confirmed.contentDigest, variant: artifact.kind,
        reviewerId: scope.actorId, approvedAt: clock, status: "active",
      });
    }
    clock = "2026-09-20T00:10:00.000Z";
    const approved = await editor.mobileReview(scope, sample.id);
    expect(approved.id).not.toBe(persisted.id);
    expect(approved.status).toBe("approved");
    expect(approved.createdAt).toBe(clock);
    await expect(editor.mobileReview(scope, sample.id)).resolves.toEqual(approved);
    expect((await store.snapshot()).mobileReviews).toHaveLength(2);

    await editor.invalidateForAsset(scope, fixture.asset.id, "clock_invalidate");
    await expect(editor.mobileReview(scope, sample.id)).rejects.toThrow("reference-render-required");
    const afterInvalidation = await store.snapshot();
    expect(afterInvalidation.mobileReviews.filter((review) => review.status === "invalidated")).toHaveLength(2);
    expect(afterInvalidation.mobileReviews).toHaveLength(4);
  });

  it("rejects every public entry point for a scope not bound to the store before reading state", async () => {
    const editor = makeEditor();
    const other = { actorId: "actor-b", customerId: "customer-b", sessionId: "session-b" };
    const calls = [
      () => editor.declareRights(other, { referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [] }, "x1"),
      () => editor.analyze(other, sample.id, "x2"),
      () => editor.confirmStyle(other, "analysis-x", ["rule-x"], "x3"),
      () => editor.createMappingPlan(other, sample.id, "x4"),
      () => editor.confirmMappingPlan(other, "mapping-x", "x5"),
      () => editor.render(other, "mapping-x", "x6"),
      () => editor.reviseWithPrompt(other, {
        draftId: "draft-x",
        reportId: "report-x",
        reportVersion: 1,
        draftVersion: 1,
        draftDigest: digest("draft-x"),
        prompt: "x",
      }, "x7"),
      () => editor.invalidateForAsset(other, fixture.asset.id, "x8"),
      () => editor.mobileReview(other, sample.id),
    ];
    for (const call of calls) await expect(call()).rejects.toThrow("reference-editor-scope-mismatch");
  });

  it("keeps another sample's confirmed mapping current when sample A is re-analyzed", async () => {
    const editor = makeEditor({ analyze: async ({ sample: currentSample, rights, operationKey }) => ({
      ...analysisFixture(currentSample, rights),
      id: `analysis-${currentSample.id}-${operationKey}`,
      contentDigest: digest(`analysis-${currentSample.id}-${operationKey}`),
    }) });
    await reachConfirmedMapping(editor);
    const sampleB = sampleFixture("2");
    await store.putSample(sampleB);
    await editor.declareRights(scope, { referenceSampleId: sampleB.id, sourceKind: "external-style-only", reusableElements: [] }, "rights_b_mapping");
    const analysisB = await editor.analyze(scope, sampleB.id, "analysis_b_mapping");
    await editor.confirmStyle(scope, analysisB.id, [analysisB.rules[0]!.id], "style_b_mapping");
    const draftB = await editor.createMappingPlan(scope, sampleB.id, "map_b_mapping");
    const confirmedB = await editor.confirmMappingPlan(scope, draftB.id, "confirm_b_mapping");

    await editor.analyze(scope, sample.id, "analysis_a_second");
    const currentB = (await store.snapshot()).mappingPlans.filter((plan) => plan.id === confirmedB.id).at(-1);
    expect(currentB?.status).toBe("confirmed");
  });

  it("cross-validates renderer lineage and transition claims against persisted timelines", async () => {
    const noLineage = makeEditor({ render: async (request) => {
      const result = await fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot);
      return { ...result, reportInput: { ...result.reportInput, sourceLineage: [] } };
    } });
    const firstMapping = await reachConfirmedMapping(noLineage);
    expect((await noLineage.render(scope, firstMapping.id, "render_no_lineage")).rhythm.status).not.toBe("matched");

    fixture.card.confirmedFacts = ["reference-transition:fade:rule-transition-1"];
    const fade = makeEditor({
      analyze: async ({ sample: currentSample, rights }) => transitionAnalysisFixture(currentSample, rights),
      render: async (request) => {
        const result = await fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot);
        return { ...result, reportInput: { ...result.reportInput, executedTransitions: result.plans.map((plan) => ({
          kind: "fade" as const, variantKind: plan.kind, timelineEntryId: plan.timeline[0]!.id,
          offsetMs: 1_000, durationMs: 350 as const,
        })) } };
      },
    });
    await fade.declareRights(scope, { referenceSampleId: sample.id, sourceKind: "external-style-only", reusableElements: [] }, "rights_transition_reset");
    const analysis = await fade.analyze(scope, sample.id, "analysis_fade");
    await fade.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "style_fade");
    const draft = await fade.createMappingPlan(scope, sample.id, "map_fade");
    const confirmed = await fade.confirmMappingPlan(scope, draft.id, "confirm_fade");
    expect((await fade.render(scope, confirmed.id, "render_forged_fade")).transitions.status).not.toBe("matched");
  });

  it("invalidates affected approvals and rejects an orphan report before it becomes readable", async () => {
    const editor = makeEditor();
    const confirmed = await reachConfirmedMapping(editor);
    const report = await editor.render(scope, confirmed.id, "render_approval_probe");
    const master = report.artifacts.find((artifact) => artifact.kind === "editorial-master")!;
    await store.putApproval({
      id: "approval-reference-probe", storyId: sample.id, revisionId: confirmed.id,
      revision: confirmed.version, artifactId: master.draftId, artifactDigest: master.outputDigest,
      coverDigest: digest("cover"), stillDigests: [], renderInputDigest: confirmed.contentDigest,
      variant: "editorial-master", reviewerId: scope.actorId, approvedAt: createdAt, status: "active",
    });
    await editor.invalidateForAsset(scope, fixture.asset.id, "withdraw_approval_probe");
    expect((await store.snapshot()).approvals.find((approval) => approval.id === "approval-reference-probe")?.status)
      .toBe("invalidated");

    const snapshot = await store.snapshot();
    const prior = snapshot.executionReports.find((candidate) => candidate.status === "reviewable")!;
    const orphanArtifacts = prior.artifacts.map((artifact) => ({
      ...artifact, draftId: `missing-${artifact.draftId}`, variantPlanId: `missing-${artifact.variantPlanId}`,
    }));
    const orphan = {
      ...prior,
      version: prior.version + 10,
      artifacts: orphanArtifacts,
      operation: prior.operation ? { ...prior.operation, producedArtifacts: orphanArtifacts } : undefined,
    };
    const { digest: _digest, ...stored } = snapshot;
    const payload = { ...stored, executionReports: [...snapshot.executionReports, orphan] };
    await expect(store.commit({ ...payload, digest: referenceWorkflowDigest(payload) }, snapshot.digest))
      .rejects.toThrow("reference-report-artifact-missing");

    const snapshotPath = join(root, "reference-workflows", digest(scope.customerId), digest(scope.sessionId), "snapshot.json");
    const persisted = JSON.parse(await readFile(snapshotPath, "utf8")) as Record<string, unknown> & {
      executionReports: ReferenceExecutionReport[];
    };
    const persistedReport = persisted.executionReports.at(-1)!;
    const persistedArtifact = persistedReport.artifacts[0]!;
    persistedArtifact.draftId = "missing-historical-draft";
    const operationArtifact = persistedReport.operation?.producedArtifacts.find((candidate) =>
      candidate.kind === persistedArtifact.kind);
    if (operationArtifact) operationArtifact.draftId = persistedArtifact.draftId;
    const { digest: _storedDigest, ...persistedPayload } = persisted;
    persisted.digest = referenceWorkflowDigest(persistedPayload as never);
    await writeFile(snapshotPath, `${JSON.stringify(persisted)}\n`);
    const quarantine = vi.fn(async () => undefined);
    await expect(makeEditor({ quarantine }).mobileReview(scope, sample.id))
      .rejects.toThrow("reference-workflow-repair-required");
    expect(quarantine).toHaveBeenCalledWith(scope, "reference-report-artifact-missing");
  });

  function makeEditor(overrides: Partial<{
    store: JsonReferenceSampleStore;
    render: (request: ReferenceEditorRenderRequest) => Promise<ReferenceEditorRenderResult>;
    revise: (request: ReferenceEditorRenderRequest) => Promise<ReferenceEditorRenderResult>;
    cleanup: (result: ReferenceEditorRenderResult) => Promise<void>;
    quarantine: (scope: StudioScope, reason: string) => Promise<void>;
    analyze: (request: ReferenceEditorAnalysisRequest) => Promise<ReferenceStyleAnalysis>;
    createMapping: ReferenceGuidedEditorOptions["createMapping"];
    now: () => string;
    authorize: NonNullable<ReferenceGuidedEditorOptions["withCurrentAssetAuthorization"]>;
    readDraftOutputDigest: NonNullable<ReferenceGuidedEditorOptions["readDraftOutputDigest"]>;
  }> = {}) {
    return new ReferenceGuidedEditor({
      store: overrides.store ?? store,
      now: overrides.now ?? (() => createdAt),
      analyze: overrides.analyze ?? (async ({ sample: currentSample, rights }) => analysisFixture(currentSample, rights)),
      createMapping: overrides.createMapping ?? (async ({ scope: currentScope, sample: currentSample, rights, analysis, selection }) => {
        const plan = createReferenceMaterialMappingPlan({
          scope: currentScope,
          sample: currentSample,
          rights,
          analysis,
          selection,
          currentMaterials: [fixture.card],
          authorizedHistoricalMaterials: [],
          assets: [fixture.mappingAsset],
        });
        if (!plan.mappings.some((mapping) => mapping.sourceAssetId !== null)) {
          throw new Error(`test-fixture-mapping-unachievable:${JSON.stringify(plan.mappings)}`);
        }
        return plan;
      }),
      render: overrides.render ?? (async (request) =>
        fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot)),
      revise: overrides.revise,
      withCurrentAssetAuthorization: overrides.authorize,
      readDraftOutputDigest: overrides.readDraftOutputDigest,
      cleanupRenderedOutput: overrides.cleanup ?? (async (result) => {
        await rm(result.outputDirectory, { recursive: true, force: true });
      }),
      quarantineIncompatibleState: overrides.quarantine,
    });
  }

  async function currentRevisionInput(kind: ReferenceVariantKind, prompt: string) {
    const snapshot = await store.snapshot();
    const report = snapshot.executionReports.filter((candidate) =>
      candidate.status === "reviewable" && candidate.bindings.sample.id === sample.id).at(-1);
    if (!report) throw new Error("test-current-report-missing");
    const currentArtifact = report.artifacts.find((candidate) => candidate.kind === kind);
    if (!currentArtifact) throw new Error(`test-current-artifact-missing:${kind}`);
    return {
      draftId: currentArtifact.draftId,
      reportId: report.id,
      reportVersion: report.version,
      draftVersion: currentArtifact.draftVersion,
      draftDigest: currentArtifact.outputDigest,
      prompt,
    };
  }
});

async function reachAnalysis(editor: ReferenceGuidedEditor) {
  const rights = await editor.declareRights(scope, {
    referenceSampleId: "reference-sample-1",
    sourceKind: "external-style-only",
    reusableElements: [],
  }, "rights_1");
  const analysis = await editor.analyze(scope, "reference-sample-1", "analysis_1");
  return { rights, analysis };
}

async function reachConfirmedMapping(editor: ReferenceGuidedEditor) {
  const { analysis } = await reachAnalysis(editor);
  await editor.confirmStyle(scope, analysis.id, [analysis.rules[0]!.id], "style_1");
  const draft = await editor.createMappingPlan(scope, "reference-sample-1", "map_1");
  return editor.confirmMappingPlan(scope, draft.id, "confirm_1");
}

async function reachConfirmedMappingForSample(
  editor: ReferenceGuidedEditor,
  sampleId: string,
  operationPrefix: string,
) {
  await editor.declareRights(scope, {
    referenceSampleId: sampleId,
    sourceKind: "external-style-only",
    reusableElements: [],
  }, `${operationPrefix}_rights`);
  const analysis = await editor.analyze(scope, sampleId, `${operationPrefix}_analysis`);
  await editor.confirmStyle(
    scope,
    analysis.id,
    [analysis.rules[0]!.id],
    `${operationPrefix}_style`,
  );
  const draft = await editor.createMappingPlan(scope, sampleId, `${operationPrefix}_mapping`);
  return editor.confirmMappingPlan(scope, draft.id, `${operationPrefix}_confirm`);
}

function sampleFixture(suffix = "1"): ReferenceSample {
  return {
    id: `reference-sample-${suffix}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: `reference-source-asset-${suffix}`,
    sourceDigest: digest(`reference-source-${suffix}`),
    sourceKind: "external-style-only",
    collectionMode: "dedicated-upload",
    candidateScope: "reference-only",
    status: "pending-rights",
    viewPermission: "allowed",
    usePermission: "review-required",
    createdAt,
  };
}

function analysisFixture(sample: ReferenceSample, rights: ReferenceRightsDeclaration): ReferenceStyleAnalysis {
  const bindings = {
    sample: sampleBinding(sample),
    rights: rightsBinding(rights),
  };
  return {
    id: `analysis-${rights.id}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    contentDigest: digest(`analysis-${rights.id}`),
    bindings,
    cuts: [15_000],
    sections: [{ id: "section-1", startMs: 0, endMs: 30_000, summary: "opening" }],
    transcriptSpans: [],
    beatMarkers: [1_000, 2_000],
    transitions: [{ id: "transition-1", atMs: 15_000, kind: "cut", confidence: 0.9 }],
    rules: [{
      id: "rule-rhythm-1",
      kind: "rhythm",
      description: "opening rhythm",
      referenceStartMs: 0,
      referenceEndMs: 30_000,
      evidenceIds: ["section-1"],
    }],
    warnings: [],
    confidence: 0.9,
    status: "complete",
    capability: { mode: "provider", label: "local evidence", version: 730_001, provider: "local-v1" },
    createdAt,
  };
}

function transitionAnalysisFixture(
  sample: ReferenceSample,
  rights: ReferenceRightsDeclaration,
): ReferenceStyleAnalysis {
  const analysis = analysisFixture(sample, rights);
  return {
    ...analysis,
    transitions: [{ id: "transition-1", atMs: 15_000, kind: "fade", confidence: 0.9 }],
    rules: [{
      id: "rule-transition-1",
      kind: "transition",
      description: "opening fade",
      referenceStartMs: 0,
      referenceEndMs: 30_000,
      evidenceIds: ["transition-1"],
    }],
    contentDigest: digest(`transition-analysis-${rights.id}`),
  };
}

function materialFixture(suffix = "1"): {
  card: MaterialCard;
  asset: StudioMediaAsset;
  mappingAsset: ReferenceMappingAssetSnapshot;
} {
  const sourceDigest = digest(suffix === "1" ? "customer-material" : `customer-material-${suffix}`);
  const card: MaterialCard = {
    id: `material-card-${suffix}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: `customer-asset-${suffix}`,
    sourceDigest,
    sourceStartMs: 0,
    sourceEndMs: 30_000,
    contentType: "single-product",
    technicalUsable: true,
    technicalFlags: ["ffprobe:video"],
    observations: ["opening"],
    confirmedFacts: ["reference-rhythm:rule-rhythm-1"],
    pendingFacts: [],
    capability: { mode: "provider", label: "local evidence", version: 1, provider: "local-v1" },
    createdAt,
  };
  const asset: StudioMediaAsset = {
    id: card.sourceAssetId,
    importBindingId: card.sourceAssetId,
    blobId: `blob_${sourceDigest}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    digest: sourceDigest,
    blobDigest: sourceDigest,
    mediaKind: "video",
    byteLength: 128,
    fileName: "customer.mp4",
    capturedAt: null,
    importedAt: createdAt,
    viewPermission: "allowed",
    usePermission: "allowed",
    captureContext: null,
  };
  const permission = {
    id: `permission-${suffix}`,
    version: 1,
    epoch: 1,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: asset.id,
    sourceDigest,
    view: "allowed" as const,
    use: "allowed" as const,
  };
  return {
    card,
    asset,
    mappingAsset: { asset, permission, withdrawn: false, verifiedDigest: sourceDigest },
  };
}

async function commitWithOperation(
  store: JsonReferenceSampleStore,
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
  operation: ReferenceEditorOperation,
): Promise<void> {
  const { digest: _digest, ...payload } = snapshot;
  const next = { ...payload, editorOperations: [...snapshot.editorOperations, operation] };
  await store.commit({ ...next, digest: referenceWorkflowDigest(next) }, snapshot.digest);
}

async function commitForgedMappingOnlyInvalidation(
  store: JsonReferenceSampleStore,
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
  assetId: string,
  reason: string,
  operationKey: string,
  affectedPlans: readonly ReferenceMaterialMappingPlan[],
  appendedPlans: readonly ReferenceMaterialMappingPlan[] = affectedPlans,
): Promise<void> {
  const affectedMappings = affectedPlans.map(mappingIdentity).sort(compareMappingIdentity);
  const invalidatedPlans = appendedPlans.map((plan) => ({
    ...plan,
    version: plan.version + 1,
    status: "invalidated" as const,
  }));
  const invalidatedMappings = affectedPlans.map((plan) => ({
    ...plan,
    version: plan.version + 1,
    status: "invalidated" as const,
  })).map(mappingIdentity).sort(compareMappingIdentity);
  const intent = {
    method: "invalidateForAsset" as const,
    assetId,
    reason,
    affectedMappings,
  };
  const result = {
    kind: "invalidation" as const,
    assetId,
    reason,
    affectedMappings,
    invalidatedMappings,
  };
  const operation = {
    ...referenceEditorOperationMetadata(scope, "invalidateForAsset", operationKey, intent),
    result,
    resultDigest: digest(canonicalJson(result)),
  } as ReferenceEditorOperation;
  const { digest: _digest, ...payload } = snapshot;
  const next = {
    ...payload,
    mappingPlans: [...snapshot.mappingPlans, ...invalidatedPlans],
    editorOperations: [...snapshot.editorOperations, operation],
  };
  await store.commit({ ...next, digest: referenceWorkflowDigest(next) }, snapshot.digest);
}

function mappingIdentity(plan: ReferenceMaterialMappingPlan) {
  return { id: plan.id, version: plan.version, contentDigest: plan.contentDigest };
}

function confirmedMappingReplayOperation(
  snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>,
  confirmed: ReferenceMaterialMappingPlan,
  operationKey: string,
): ReferenceEditorOperation {
  const draft = snapshot.mappingPlans.find((plan) =>
    plan.id === confirmed.id && plan.version === confirmed.version - 1)!;
  const metadata = referenceEditorOperationMetadata(scope, "confirmMappingPlan", operationKey, {
    method: "confirmMappingPlan",
    planId: confirmed.id,
    inputBindingDigest: digest(canonicalJson(draft)),
    expectedResult: { id: confirmed.id, version: confirmed.version },
  });
  return {
    ...metadata,
    result: { kind: "mapping", id: confirmed.id, version: confirmed.version },
    resultDigest: digest(canonicalJson(confirmed)),
  };
}

function compareMappingIdentity(
  left: ReturnType<typeof mappingIdentity>,
  right: ReturnType<typeof mappingIdentity>,
): number {
  return left.id < right.id ? -1 : left.id > right.id ? 1
    : left.version - right.version || left.contentDigest.localeCompare(right.contentDigest);
}

async function fakeRenderedResult(
  mappingPlan: ReferenceMaterialMappingPlan,
  analysis: ReferenceStyleAnalysis,
  operationKey: string,
  outputRoot: string,
  kinds: readonly ReferenceVariantKind[] = ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
): Promise<ReferenceEditorRenderResult> {
  const directory = join(outputRoot, `reference-run-${operationKey}`);
  await mkdir(directory, { recursive: false });
  const mapping = mappingPlan.mappings.find((row) => row.sourceAssetId !== null)!;
  const plans: ReferenceVariantPlan[] = [];
  const drafts: ReferenceDraftPackage[] = [];
  const artifacts: Array<ReferenceRenderEvidence["artifacts"][number]> = [];
  for (const kind of kinds) {
    const planId = `variant-${operationKey}-${kind}`;
    const draftId = `draft-${operationKey}-${kind}`;
    const outputDigest = digest(`${operationKey}-${kind}`);
    const outputPath = join(directory, `${kind}.mp4`);
    await writeFile(outputPath, `${operationKey}-${kind}`);
    const mappingBinding = confirmedMappingBinding(mappingPlan);
    const plan: ReferenceVariantPlan = {
      id: planId,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      bindings: { ...mappingPlan.bindings, mappingPlan: mappingBinding },
      kind,
      authorizedSources: [{
        id: mapping.sourceBindingId!,
        mappingId: mapping.id,
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        sourceAssetId: mapping.sourceAssetId!,
        sourceDigest: mapping.sourceDigest!,
        provenance: mapping.sourceProvenance!,
        permission: mapping.permission!,
        reuseRights: mapping.reuseRights,
      }],
      timeline: [{
        id: `timeline-${operationKey}-${kind}`,
        mappingId: mapping.id,
        ruleId: mapping.ruleId,
        sourceBindingId: mapping.sourceBindingId!,
        sourceStartMs: mapping.sourceStartMs!,
        sourceEndMs: mapping.sourceEndMs!,
        timelineStartMs: 0,
        transition: "cut",
        targetDurationRatio: 1,
        reasonCode: `reference.${kind.replace("editorial-master", "master")}`,
      }],
      audioOverlay: null,
      platformChangeCodes: kind === "editorial-master" ? [] : [`reference.${kind}.adapted`],
      status: "rendered",
      publishClaimed: false,
      createdAt,
    };
    const draft: ReferenceDraftPackage = {
      id: draftId,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      bindings: {
        mappingPlan: mappingBinding,
        variantPlan: { id: plan.id, version: plan.version, customerId: scope.customerId, sessionId: scope.sessionId, kind },
      },
      outputDigest,
      outputPath,
      durationSeconds: 30,
      lineage: [{
        id: `lineage-${operationKey}-${kind}`,
        use: "video",
        timelineEntryId: plan.timeline[0]!.id,
        mappingId: mapping.id,
        sourceBindingId: mapping.sourceBindingId!,
        sourceAssetId: mapping.sourceAssetId!,
        sourceDigest: mapping.sourceDigest!,
        sourceStartMs: mapping.sourceStartMs!,
        sourceEndMs: mapping.sourceEndMs!,
      }],
      status: "reviewable",
      capability: analysis.capability,
      publishClaimed: false,
      createdAt,
    };
    plans.push(plan);
    drafts.push(draft);
    artifacts.push({
      kind,
      variantPlanId: plan.id,
      variantPlanVersion: plan.version,
      draftId: draft.id,
      draftVersion: draft.version,
      outputPath,
      outputDigest,
      durationSeconds: 30,
      videoCodec: "h264",
      audioCodec: "aac",
      width: 720,
      height: 1280,
    });
  }
  const counts = mappingCounts(mappingPlan);
  return {
    plans,
    drafts,
    outputDirectory: directory,
    reportInput: {
      bindings: plans[0]!.bindings,
      artifacts,
      sourceLineage: plans.flatMap((plan) => plan.timeline.map((row) => ({
        kind: plan.kind,
        timelineEntryId: row.id,
        mappingId: row.mappingId,
        sourceBindingId: row.sourceBindingId,
        sourceAssetId: mapping.sourceAssetId!,
        sourceDigest: mapping.sourceDigest!,
        sourceStartMs: row.sourceStartMs,
        sourceEndMs: row.sourceEndMs,
        permissionId: mapping.permission!.id,
        permissionVersion: mapping.permission!.version,
        permissionEpoch: mapping.permission!.epoch,
      }))),
      executedTransitions: [],
      mappingCounts: counts,
      platformChanges: ["xiaohongshu", "douyin", "wechat-channels"].map((kind) => ({
        kind: kind as "xiaohongshu" | "douyin" | "wechat-channels",
        changeCodes: [`reference.${kind}.adapted`],
      })),
      publishClaimed: false,
    },
  };
}

async function fakeRevisionResult(request: ReferenceEditorRenderRequest, outputRoot: string) {
  const kinds = request.target === "editorial-master"
    ? ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const
    : [request.target!] as const;
  return fakeRenderedResult(request.mappingPlan, request.analysis, request.operationKey, outputRoot, kinds);
}

function sampleBinding(sample: ReferenceSample) {
  return {
    id: sample.id, version: sample.version, customerId: sample.customerId, sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId, sourceDigest: sample.sourceDigest, sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode, candidateScope: sample.candidateScope,
  } as const;
}

function rightsBinding(rights: ReferenceRightsDeclaration) {
  return {
    id: rights.id, version: rights.version, customerId: rights.customerId, sessionId: rights.sessionId,
    referenceSampleId: rights.sample.id, referenceSampleVersion: rights.sample.version,
    referenceSampleDigest: rights.sample.sourceDigest, sourceKind: rights.sourceKind,
    reusableElements: rights.reusableElements, contentDigest: rights.contentDigest,
  };
}

function confirmedMappingBinding(plan: ReferenceMaterialMappingPlan) {
  const permissions = plan.mappings.flatMap((mapping) => mapping.permission ? [{
    id: mapping.permission.id,
    version: mapping.permission.version,
    epoch: mapping.permission.epoch,
    customerId: mapping.permission.customerId,
    sessionId: mapping.permission.sessionId,
    sourceAssetId: mapping.permission.sourceAssetId,
    sourceDigest: mapping.permission.sourceDigest,
  }] : []);
  return {
    id: plan.id, version: plan.version, customerId: plan.customerId, sessionId: plan.sessionId,
    contentDigest: plan.contentDigest, permissionSetDigest: plan.permissionSetDigest, permissions,
    status: "confirmed" as const,
    confirmation: { receiptId: plan.confirmationReceiptId!, actorId: plan.confirmedByActorId!, confirmedAt: plan.confirmedAt! },
  };
}

function mappingCounts(plan: ReferenceMaterialMappingPlan) {
  return {
    matched: plan.mappings.filter((row) => row.status === "matched").length,
    substituted: plan.mappings.filter((row) => row.status === "substituted").length,
    unachievable: plan.mappings.filter((row) => row.status === "unachievable").length,
  };
}

function latestAnalysis(snapshot: Awaited<ReturnType<JsonReferenceSampleStore["snapshot"]>>) {
  return snapshot.analyses.at(-1)!;
}

function artifact(review: ReferenceMobileReviewV1, kind: ReferenceVariantKind) {
  return review.artifacts.find((item) => item.kind === kind)!;
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    .map(([key, child]) => [key, canonicalize(child)]));
}

async function directoryExists(path: string): Promise<boolean> {
  try {
    await mkdir(path);
    return false;
  } catch (error) {
    return Boolean(error && typeof error === "object" && "code" in error && error.code === "EEXIST");
  }
}

async function waitUntil(predicate: () => boolean): Promise<void> {
  for (let index = 0; index < 100; index += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error("test-wait-timeout");
}
