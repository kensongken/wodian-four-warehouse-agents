import { createHash } from "node:crypto";

import {
  ReferenceDraftApprovalSchema,
  ReferenceExecutionReportSchema,
  ReferenceMobileReviewV1Schema,
  ReferenceRightsDeclarationSchema,
  ReferenceSampleSchema,
  ReferenceStyleSelectionSchema,
  type ReferenceDraftPackage,
  type ReferenceDraftApproval,
  type ReferenceExecutionReport,
  type ReferenceMaterialMappingPlan,
  type ReferenceMobileReviewV1,
  type ReferenceReusableElement,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceSourceKind,
  type ReferenceStyleAnalysis,
  type ReferenceStyleSelection,
  type ReferenceVariantKind,
  type ReferenceVariantPlan,
} from "@wearable/contracts";

import {
  confirmReferenceMaterialMappingPlan,
  type ReferenceMaterialMappingPlanIdentity,
} from "./reference-material-mapper";
import {
  referenceWorkflowDigest,
  referenceEditorOperationId,
  referenceEditorOperationMetadata,
  referenceEditorOperationRequestIntent,
  type ReferenceEditorOperation,
  type ReferenceEditorOperationIntent,
  type ReferenceEditorOperationMetadata,
  type ReferenceEditorOperationMethod,
  type ReferenceSampleStore,
  type ReferenceWorkflowSnapshot,
} from "./reference-sample-store";
import type { ReferenceRenderEvidence, RenderReferenceVariantsResult } from "./reference-studio-renderer";
import type { StudioScope } from "./studio-capabilities";

export type DeclareReferenceRightsInput = Readonly<{
  referenceSampleId: string;
  sourceKind: ReferenceSourceKind;
  reusableElements: readonly ReferenceReusableElement[];
}>;

export type InvalidateReferenceAssetInput = Readonly<{
  assetId: string;
  reason: string;
}>;

export type ApproveReferenceDraftInput = Readonly<{
  draftId: string;
  reportId: string;
  reportVersion: number;
  draftVersion: number;
  draftDigest: string;
}>;

export type ReviseReferenceDraftInput = ApproveReferenceDraftInput & Readonly<{
  prompt: string;
}>;

type OperationFactory = (intent: ReferenceEditorOperationIntent) => ReferenceEditorOperationMetadata;

export type ReferenceEditorAnalysisRequest = Readonly<{
  scope: StudioScope;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  operationKey: string;
}>;

export type ReferenceEditorMappingRequest = Readonly<{
  scope: StudioScope;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  selection: ReferenceStyleSelection;
  operationKey: string;
}>;

export type ReferenceEditorRenderRequest = Readonly<{
  scope: StudioScope;
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  selection: ReferenceStyleSelection;
  mappingPlan: ReferenceMaterialMappingPlan;
  operationKey: string;
  target: ReferenceVariantKind | null;
  prompt: string | null;
}>;

export type ReferenceEditorRenderResult = RenderReferenceVariantsResult & Readonly<{
  outputDirectory: string;
}>;

export type ReferenceGuidedEditorOptions = Readonly<{
  store: ReferenceSampleStore;
  analyze: (request: ReferenceEditorAnalysisRequest) => Promise<ReferenceStyleAnalysis>;
  createMapping: (request: ReferenceEditorMappingRequest) => Promise<ReferenceMaterialMappingPlan>;
  render: (request: ReferenceEditorRenderRequest) => Promise<ReferenceEditorRenderResult>;
  revise?: (request: ReferenceEditorRenderRequest) => Promise<ReferenceEditorRenderResult>;
  withCurrentAssetAuthorization?: <T>(
    request: Readonly<{
      scope: StudioScope;
      sample: ReferenceSample;
      mappingPlan: ReferenceMaterialMappingPlan;
    }>,
    work: () => Promise<T>,
  ) => Promise<T>;
  readDraftOutputDigest?: (request: Readonly<{
    scope: StudioScope;
    draft: ReferenceDraftPackage;
  }>) => Promise<string>;
  cleanupRenderedOutput: (result: ReferenceEditorRenderResult) => Promise<void>;
  quarantineIncompatibleState?: (scope: StudioScope, reason: string) => Promise<void>;
  now?: () => string;
}>;

type CurrentChain = Readonly<{
  sample: ReferenceSample;
  rights: ReferenceRightsDeclaration;
  analysis: ReferenceStyleAnalysis;
  selection: ReferenceStyleSelection;
  mappingPlan: ReferenceMaterialMappingPlan;
}>;

type ReferenceRevisionExpectation = Readonly<{
  report: Readonly<{ id: string; version: number }>;
  draft: Readonly<{ id: string; version: number; outputDigest: string }>;
}>;

const INTEGRITY_FAILURES = [
  "reference-state-digest-mismatch",
  "reference-draft-mapping-plan-missing",
  "reference-draft-mapping-plan-mismatch",
  "reference-draft-variant-plan-missing",
  "reference-draft-variant-plan-mismatch",
  "reference-draft-lineage-mismatch",
  "reference-variant-mapping-plan-missing",
  "reference-variant-mapping-plan-mismatch",
  "reference-variant-mapping-mismatch",
  "reference-report-artifact-set-mismatch",
  "reference-report-artifact-missing",
  "reference-report-artifact-mismatch",
  "reference-mobile-report-missing",
  "reference-mobile-report-mismatch",
  "reference-mobile-artifact-mismatch",
  "reference-mobile-approval-mismatch",
  "reference-mobile-draft-status-mismatch",
  "reference-operation-result-kind-mismatch",
  "reference-operation-result-missing",
  "reference-operation-result-scope-mismatch",
  "reference-operation-result-binding-mismatch",
  "reference-operation-result-digest-mismatch",
  "reference-operation-metadata-missing",
  "reference-operation-intent-binding-mismatch",
  "reference-operation-intent-method-mismatch",
  "reference-current-mapping-ambiguous",
  "reference-current-report-ambiguous",
  "reference-invalidation-affected-set-mismatch",
  "reference-invalidation-invalidated-set-mismatch",
  "reference-invalidation-downstream-mismatch",
  "reference-invalidation-identities-too-many",
  "reference-invalidation-identity-duplicate",
  "reference-invalidation-identities-not-canonical",
  "reference-invalidation-identity-capacity-exceeded",
  "reference-invalidation-unattributed-transition",
  "reference-invalidated-mapping-terminal",
  "reference-invalidated-sample-terminal",
  "reference-invalidated-asset-reused",
  "reference-approval-operation-required",
  "reference-mapping-confirmation-operation-required",
  "reference-mapping-draft-must-be-initial",
  "reference-snapshot-integrity-version-required",
] as const;

/**
 * Server-owned workflow state machine. Public methods accept intent and stable
 * operation keys; immutable workflow identities always come back from the store.
 */
export class ReferenceGuidedEditor {
  private static readonly inFlight = new Map<string, { intentDigest: string; promise: Promise<unknown> }>();
  private static readonly storeIdentities = new WeakMap<object, string>();
  private static nextStoreIdentity = 1;
  private readonly now: () => string;

  constructor(private readonly options: ReferenceGuidedEditorOptions) {
    this.now = options.now ?? (() => new Date().toISOString());
  }

  async declareRights(
    scopeValue: StudioScope,
    inputValue: DeclareReferenceRightsInput,
    operationKeyValue: string,
  ): Promise<ReferenceRightsDeclaration> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const input = parseRightsInput(inputValue);
    return this.runMutation(scope, "declareRights", operationKey, input, async (snapshot, _operationId, operationFor) => {
    let sample = latestByVersion(snapshot.samples.filter((value) => value.id === input.referenceSampleId));
    if (!sample || sample.status === "invalidated") throw new Error("reference-sample-not-ready");
    if (sample.sourceKind !== input.sourceKind) throw new Error("reference-rights-source-kind-mismatch");

    const existing = snapshot.rightsDeclarations.find((value) =>
      value.id === operationId("reference_rights", scope, operationKey));
    if (existing) {
      assertRightsIntent(existing, input, sample);
      const operation = operationFor({
        method: "declareRights",
        ...input,
        reusableElements: [...input.reusableElements],
        sampleBindingDigest: digest(canonicalJson(existing.sample)),
        expectedResult: { id: existing.id, version: existing.version },
      });
      await this.commit(snapshot, completeOperation(
        snapshot, operation, { kind: "rights", id: existing.id, version: existing.version },
      ));
      return existing;
    }

    const now = this.now();
    const appendedSamples: ReferenceSample[] = [];
    if (sample.status === "pending-rights") {
      sample = ReferenceSampleSchema.parse({
        ...sample,
        version: sample.version + 1,
        status: "ready",
        createdAt: now,
      });
      appendedSamples.push(sample);
    }
    const version = nextVersion(snapshot.rightsDeclarations.filter((value) =>
      value.sample.id === sample!.id));
    const content = {
      sample: sampleBinding(sample),
      sourceKind: input.sourceKind,
      reusableElements: [...input.reusableElements],
      actorId: scope.actorId,
    };
    const rights = ReferenceRightsDeclarationSchema.parse({
      id: operationId("reference_rights", scope, operationKey),
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version,
      contentDigest: digest(canonicalJson(content)),
      ...content,
      createdAt: now,
    });
    const operation = operationFor({
      method: "declareRights",
      ...input,
      reusableElements: [...input.reusableElements],
      sampleBindingDigest: digest(canonicalJson(rights.sample)),
      expectedResult: { id: rights.id, version: rights.version },
    });
    const next = invalidateForUpstream(snapshot, {
      operationKey,
      selections: (selection) => selection.bindings.sample.id === sample!.id,
      mappings: (mapping) => mapping.bindings.sample.id === sample!.id,
    });
    await this.commit(snapshot, completeOperation({
      ...next,
      samples: [...next.samples, ...appendedSamples],
      rightsDeclarations: [...next.rightsDeclarations, rights],
    }, operation, { kind: "rights", id: rights.id, version: rights.version }));
    return rights;
    });
  }

  async analyze(
    scopeValue: StudioScope,
    sampleId: string,
    operationKeyValue: string,
  ): Promise<ReferenceStyleAnalysis> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const normalizedSampleId = parseId(sampleId, "reference-sample-id-required");
    return this.runMutation(scope, "analyze", operationKey, { sampleId: normalizedSampleId },
      async (snapshot, _operationId, operationFor) => {
    const { sample, rights } = requireRights(snapshot, normalizedSampleId);
    const bindingBefore = upstreamBinding({ sample, rights });
    const analysis = await this.options.analyze({ scope, sample, rights, operationKey });
    assertAnalysisCurrent(analysis, sample, rights);
    const latest = await this.snapshot(scope);
    const latestRights = requireRights(latest, normalizedSampleId);
    if (upstreamBinding(latestRights) !== bindingBefore) throw new Error("reference-analysis-input-stale");
    const existing = latest.analyses.find((value) => value.id === analysis.id && value.version === analysis.version);
    const result = existing
      ? requireCanonicalSame(existing, analysis, "reference-analysis-operation-conflict")
      : analysis;
    const operation = operationFor({
      method: "analyze",
      sampleId: normalizedSampleId,
      inputBindingDigest: digest(canonicalJson(result.bindings)),
      expectedResult: { id: result.id, version: result.version },
    });
    if (existing) {
      await this.commit(latest, completeOperation(
        latest, operation, { kind: "analysis", id: result.id, version: result.version },
      ));
      return result;
    }
    const next = invalidateForUpstream(latest, {
      operationKey,
      selections: (selection) => selection.bindings.sample.id === sample.id &&
        (selection.bindings.analysis.id !== analysis.id ||
         selection.bindings.analysis.version !== analysis.version),
      mappings: (mapping) => mapping.bindings.sample.id === sample.id &&
        (mapping.bindings.analysis.id !== analysis.id ||
         mapping.bindings.analysis.version !== analysis.version),
    });
    await this.commit(latest, completeOperation(
      { ...next, analyses: [...next.analyses, result] }, operation,
      { kind: "analysis", id: result.id, version: result.version },
    ));
    return result;
    });
  }

  async confirmStyle(
    scopeValue: StudioScope,
    analysisId: string,
    selectedRuleIdsValue: readonly string[],
    operationKeyValue: string,
  ): Promise<ReferenceStyleSelection> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const normalizedAnalysisId = parseId(analysisId, "reference-analysis-id-required");
    const selectedRuleIds = parseSelectedRuleIds(selectedRuleIdsValue);
    return this.runMutation(scope, "confirmStyle", operationKey,
      { analysisId: normalizedAnalysisId, selectedRuleIds }, async (snapshot, _operationId, operationFor) => {
    const chain = requireAnalysis(snapshot, normalizedAnalysisId);
    const ruleIds = new Set(chain.analysis.rules.map((rule) => rule.id));
    if (selectedRuleIds.some((ruleId) => !ruleIds.has(ruleId))) {
      throw new Error("reference-selected-rule-missing");
    }
    const id = operationId("reference_style_selection", scope, operationKey);
    const existing = snapshot.styleSelections.find((value) => value.id === id && value.status === "confirmed");
    if (existing) {
      if (canonicalJson(existing.selectedRuleIds) !== canonicalJson(selectedRuleIds) ||
          canonicalJson(existing.bindings) !== canonicalJson(selectionInputBindings(chain))) {
        throw new Error("reference-operation-key-conflict");
      }
      const operation = operationFor({
        method: "confirmStyle",
        analysisId: normalizedAnalysisId,
        selectedRuleIds,
        inputBindingDigest: digest(canonicalJson(existing.bindings)),
        expectedResult: { id: existing.id, version: existing.version },
      });
      await this.commit(snapshot, completeOperation(
        snapshot, operation, { kind: "selection", id: existing.id, version: existing.version },
      ));
      return existing;
    }
    const now = this.now();
    const content = {
      bindings: selectionInputBindings(chain),
      selectedRuleIds,
      actorId: scope.actorId,
    };
    const selection = ReferenceStyleSelectionSchema.parse({
      id,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: nextVersion(snapshot.styleSelections.filter((value) =>
        value.bindings.analysis.id === normalizedAnalysisId)),
      contentDigest: digest(canonicalJson(content)),
      ...content,
      confirmationReceiptId: operationId("reference_style_confirmation", scope, operationKey),
      confirmedAt: now,
      status: "confirmed",
      createdAt: now,
    });
    const operation = operationFor({
      method: "confirmStyle",
      analysisId: normalizedAnalysisId,
      selectedRuleIds,
      inputBindingDigest: digest(canonicalJson(selection.bindings)),
      expectedResult: { id: selection.id, version: selection.version },
    });
    const next = invalidateForUpstream(snapshot, {
      operationKey,
      mappings: (mapping) => mapping.bindings.sample.id === chain.sample.id,
    });
    await this.commit(snapshot, completeOperation(
      { ...next, styleSelections: [...next.styleSelections, selection] }, operation,
      { kind: "selection", id: selection.id, version: selection.version },
    ));
    return selection;
    });
  }

  async createMappingPlan(
    scopeValue: StudioScope,
    sampleId: string,
    operationKeyValue: string,
  ): Promise<ReferenceMaterialMappingPlan> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const normalizedSampleId = parseId(sampleId, "reference-sample-id-required");
    return this.runMutation(scope, "createMappingPlan", operationKey,
      { sampleId: normalizedSampleId }, async (snapshot, _operationId, operationFor) => {
    const chain = requireSelection(snapshot, normalizedSampleId);
    const bindingBefore = currentBinding(chain);
    const plan = await this.options.createMapping({ ...chain, scope, operationKey });
    assertMappingDraftCurrent(plan, chain);
    const latest = await this.snapshot(scope);
    const latestChain = requireSelection(latest, normalizedSampleId);
    if (currentBinding(latestChain) !== bindingBefore) throw new Error("reference-mapping-input-stale");
    const existing = latest.mappingPlans.find((value) => value.id === plan.id && value.version === plan.version);
    const result = existing
      ? requireCanonicalSame(existing, plan, "reference-mapping-operation-conflict")
      : plan;
    const operation = operationFor({
      method: "createMappingPlan",
      sampleId: normalizedSampleId,
      inputBindingDigest: digest(canonicalJson(result.bindings)),
      expectedResult: { id: result.id, version: result.version },
    });
    if (existing) {
      await this.commit(latest, completeOperation(
        latest, operation, { kind: "mapping", id: result.id, version: result.version },
      ));
      return result;
    }
    const next = invalidateForUpstream(latest, {
      operationKey,
      mappings: (mapping) => mapping.bindings.sample.id === chain.sample.id,
    });
    await this.commit(latest, completeOperation(
      { ...next, mappingPlans: [...next.mappingPlans, result] }, operation,
      { kind: "mapping", id: result.id, version: result.version },
    ));
    return result;
    });
  }

  async confirmMappingPlan(
    scopeValue: StudioScope,
    planId: string,
    operationKeyValue: string,
  ): Promise<ReferenceMaterialMappingPlan> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const normalizedPlanId = parseId(planId, "reference-mapping-plan-id-required");
    return this.runMutation(scope, "confirmMappingPlan", operationKey,
      { planId: normalizedPlanId }, async (snapshot, _operationId, operationFor) => {
    const latestPlan = latestByVersion(snapshot.mappingPlans.filter((value) => value.id === normalizedPlanId));
    if (latestPlan?.status === "confirmed" &&
        latestPlan.confirmationReceiptId === operationId("reference_mapping_confirmation", scope, operationKey) &&
        latestPlan.confirmedByActorId === scope.actorId) {
      const priorDraft = snapshot.mappingPlans.find((value) =>
        value.id === latestPlan.id && value.version === latestPlan.version - 1 && value.status === "draft");
      if (!priorDraft) throw new Error("reference-mapping-draft-required");
      const operation = operationFor({
        method: "confirmMappingPlan",
        planId: normalizedPlanId,
        inputBindingDigest: digest(canonicalJson(priorDraft)),
        expectedResult: { id: latestPlan.id, version: latestPlan.version },
      });
      await this.commit(snapshot, completeOperation(
        snapshot, operation, { kind: "mapping", id: latestPlan.id, version: latestPlan.version },
      ));
      return latestPlan;
    }
    const draft = latestPlan;
    if (!draft || draft.status !== "draft") throw new Error("reference-mapping-draft-required");
    const selection = currentSelectionForMapping(snapshot, draft);
    if (!selection) throw new Error("reference-mapping-input-stale");
    const expected: ReferenceMaterialMappingPlanIdentity = {
      id: draft.id,
      version: draft.version,
      contentDigest: draft.contentDigest,
    };
    const confirmedDraft = confirmReferenceMaterialMappingPlan(draft, expected, scope.actorId, this.now());
    const confirmed = { ...confirmedDraft, version: draft.version + 1 };
    const operation = operationFor({
      method: "confirmMappingPlan",
      planId: normalizedPlanId,
      inputBindingDigest: digest(canonicalJson(draft)),
      expectedResult: { id: confirmed.id, version: confirmed.version },
    });
    const next = invalidateRenderedState(snapshot, (mapping) => mapping.id === draft.id, operationKey);
    await this.commit(snapshot, completeOperation(
      { ...next, mappingPlans: [...next.mappingPlans, confirmed] }, operation,
      { kind: "mapping", id: confirmed.id, version: confirmed.version },
    ));
    return confirmed;
    });
  }

  async render(
    scopeValue: StudioScope,
    planId: string,
    operationKeyValue: string,
  ): Promise<ReferenceExecutionReport> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const normalizedPlanId = parseId(planId, "reference-mapping-plan-id-required");
    return this.runMutation(scope, "render", operationKey, { planId: normalizedPlanId },
      async (snapshot, currentOperationId, operationFor) => {
      const chain = requireConfirmedMapping(snapshot, normalizedPlanId);
      return this.executeRender(
        scope, chain, operationKey, null, null, this.options.render,
        "render", currentOperationId, operationFor,
      );
    });
  }

  async reviseWithPrompt(
    scopeValue: StudioScope,
    inputValue: ReviseReferenceDraftInput,
    operationKeyValue: string,
  ): Promise<ReferenceExecutionReport> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const input = parseRevisionInput(inputValue);
    if (!this.options.revise) throw new Error("reference-prompt-revision-unavailable");
    const requestIntent = {
      promptDigest: digest(input.prompt),
      report: { id: input.reportId, version: input.reportVersion },
      draft: { id: input.draftId, version: input.draftVersion, outputDigest: input.draftDigest },
    };
    return this.runMutation(scope, "reviseWithPrompt", operationKey, requestIntent,
      async (snapshot, currentOperationId, operationFor) => {
      const target = requireApprovalTarget(snapshot, input);
      return this.executeRender(
        scope, target.chain, operationKey, target.artifact.kind, input.prompt, this.options.revise!,
        "reviseWithPrompt", currentOperationId, operationFor, {
          report: { id: target.report.id, version: target.report.version },
          draft: {
            id: target.draft.id,
            version: target.draft.version,
            outputDigest: target.draft.outputDigest,
          },
        },
      );
    });
  }

  async approveDraft(
    scopeValue: StudioScope,
    inputValue: ApproveReferenceDraftInput,
    operationKeyValue: string,
  ): Promise<ReferenceDraftApproval> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const input = parseApprovalInput(inputValue);
    if (!this.options.withCurrentAssetAuthorization || !this.options.readDraftOutputDigest) {
      throw new Error("reference-approval-verifier-unavailable");
    }
    const requestIntent = {
      report: { id: input.reportId, version: input.reportVersion },
      draft: { id: input.draftId, version: input.draftVersion, outputDigest: input.draftDigest },
    };
    return this.runMutation(scope, "approveDraft", operationKey, requestIntent,
      async (snapshot, _currentOperationId, operationFor) => {
      const initial = requireApprovalTarget(snapshot, input);
      return this.options.withCurrentAssetAuthorization!({
        scope,
        sample: initial.chain.sample,
        mappingPlan: initial.chain.mappingPlan,
      }, async () => {
        const current = await this.snapshot(scope);
        const target = requireApprovalTarget(current, input);
        const fileDigest = parseDigest(
          await this.options.readDraftOutputDigest!({ scope, draft: target.draft }),
          "reference-approval-file-digest-invalid",
        );
        if (fileDigest !== target.draft.outputDigest) {
          throw new Error("reference-approval-file-digest-mismatch");
        }
        const approval = ReferenceDraftApprovalSchema.parse({
          kind: "reference-draft",
          id: operationId("reference_approval", scope, operationKey),
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          version: 1,
          bindings: {
            executionReport: { id: target.report.id, version: target.report.version },
            draft: {
              id: target.draft.id,
              version: target.draft.version,
              outputDigest: target.draft.outputDigest,
            },
            mappingPlan: {
              id: target.chain.mappingPlan.id,
              version: target.chain.mappingPlan.version,
              contentDigest: target.chain.mappingPlan.contentDigest,
              permissionSetDigest: target.chain.mappingPlan.permissionSetDigest,
            },
          },
          variant: target.artifact.kind,
          reviewerId: scope.actorId,
          fileDigest,
          approvedAt: this.now(),
          status: "active",
          publishClaimed: false,
        });
        const operation = operationFor({
          method: "approveDraft",
          report: approval.bindings.executionReport,
          draft: approval.bindings.draft,
          mappingPlan: approval.bindings.mappingPlan,
          variant: approval.variant,
          fileDigest: approval.fileDigest,
          expectedResult: { id: approval.id, version: approval.version },
        });
        const next = completeOperation({
          ...current,
          approvals: [...current.approvals, approval],
        }, operation, { kind: "approval", id: approval.id, version: approval.version });
        try {
          await this.options.store.commit(withDigest(next), current.digest);
        } catch (error) {
          if (errorMessage(error) === "reference-state-conflict") {
            throw new Error("reference-approval-input-stale", { cause: error });
          }
          throw error;
        }
        return approval;
      });
    });
  }

  async invalidateForAsset(
    scopeValue: StudioScope,
    inputValue: string | InvalidateReferenceAssetInput,
    operationKeyValue: string,
  ): Promise<void> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const operationKey = parseOperationKey(operationKeyValue);
    const input = parseInvalidationInput(inputValue);
    return this.runMutation(scope, "invalidateForAsset", operationKey, input,
      async (snapshot, _operationId, operationFor) => {
    const affectedSampleRecords = latestPerId(snapshot.samples).filter((candidate) =>
      candidate.status !== "invalidated" && candidate.sourceAssetId === input.assetId);
    const affectedSamples = affectedSampleRecords
      .map(sampleOperationIdentity).sort(compareOperationSampleIdentity);
    const affectedSampleIds = new Set(affectedSampleRecords.map((candidate) => candidate.id));
    const affectedPlans = latestPerId(snapshot.mappingPlans).filter((plan) =>
      plan.status !== "invalidated" && (
        affectedSampleIds.has(plan.bindings.sample.id) ||
        plan.mappings.some((mapping) => mapping.sourceAssetId === input.assetId)
      ));
    const affectedMappings = affectedPlans.map(mappingOperationIdentity).sort(compareOperationMappingIdentity);
    const affectedIds = new Set(affectedPlans.map((plan) => plan.id));
    let next = affectedIds.size === 0
      ? snapshot
      : invalidateMappingPlans(snapshot, (plan) => affectedIds.has(plan.id), operationKey);
    const invalidatedSamples = affectedSampleRecords.map((candidate) => ({
      ...candidate,
      version: candidate.version + 1,
      status: "invalidated" as const,
    }));
    const invalidatedSelections = latestPerId(snapshot.styleSelections.filter((selection) =>
      affectedSampleIds.has(selection.bindings.sample.id)))
      .filter((selection) => selection.status === "confirmed")
      .map((selection) => ({
        ...selection,
        version: selection.version + 1,
        status: "invalidated" as const,
      }));
    if (invalidatedSamples.length > 0 || invalidatedSelections.length > 0) {
      next = withDigest({
        ...next,
        samples: [...next.samples, ...invalidatedSamples],
        styleSelections: [...next.styleSelections, ...invalidatedSelections],
      });
    }
    const invalidatedSampleIdentities = invalidatedSamples
      .map(sampleOperationIdentity).sort(compareOperationSampleIdentity);
    const invalidatedMappings = affectedPlans.map((plan) => {
      const invalidated = next.mappingPlans.find((candidate) =>
        candidate.id === plan.id && candidate.version === plan.version + 1 && candidate.status === "invalidated");
      if (!invalidated) throw new Error("reference-invalidation-audit-missing");
      return mappingOperationIdentity(invalidated);
    }).sort(compareOperationMappingIdentity);
    const operation = operationFor({
      method: "invalidateForAsset",
      assetId: input.assetId,
      reason: input.reason,
      affectedSamples,
      affectedMappings,
    });
    await this.commit(snapshot, completeOperation(next, operation, {
      kind: "invalidation",
      assetId: input.assetId,
      reason: input.reason,
      affectedSamples,
      invalidatedSamples: invalidatedSampleIdentities,
      affectedMappings,
      invalidatedMappings,
    }));
    });
  }

  async mobileReview(scopeValue: StudioScope, sampleId: string): Promise<ReferenceMobileReviewV1> {
    const scope = parseScope(scopeValue);
    this.assertBoundScope(scope);
    const snapshot = await this.snapshot(scope);
    const chain = requireSelection(snapshot, sampleId);
    const mapping = latestCurrentMapping(snapshot, chain.selection);
    if (!mapping || mapping.status !== "confirmed") throw new Error("reference-render-required");
    const report = latestReport(snapshot, mapping);
    if (!report || report.status !== "reviewable") throw new Error("reference-render-required");
    const review = buildMobileReview(chain, mapping, report, snapshot, this.now());
    const persisted = snapshot.mobileReviews.find((candidate) =>
      candidate.id === review.id && candidate.status !== "invalidated");
    if (persisted) return persisted;
    try {
      await this.commit(snapshot, { ...snapshot, mobileReviews: [...snapshot.mobileReviews, review] });
      return review;
    } catch (error) {
      if (errorMessage(error) !== "reference-state-conflict") throw error;
      const refreshed = await this.snapshot(scope);
      const refreshedChain = requireSelection(refreshed, sampleId);
      const refreshedMapping = latestCurrentMapping(refreshed, refreshedChain.selection);
      if (!refreshedMapping || refreshedMapping.status !== "confirmed") throw new Error("reference-render-required");
      const refreshedReport = latestReport(refreshed, refreshedMapping);
      if (!refreshedReport || refreshedReport.status !== "reviewable") throw new Error("reference-render-required");
      const refreshedReview = buildMobileReview(
        refreshedChain, refreshedMapping, refreshedReport, refreshed, this.now(),
      );
      const converged = refreshed.mobileReviews.find((candidate) =>
        candidate.id === refreshedReview.id && candidate.status !== "invalidated");
      if (converged) return converged;
      throw error;
    }
  }

  private async executeRender(
    scope: StudioScope,
    chain: CurrentChain,
    operationKey: string,
    target: ReferenceVariantKind | null,
    prompt: string | null,
    renderer: (request: ReferenceEditorRenderRequest) => Promise<ReferenceEditorRenderResult>,
    method: "render" | "reviseWithPrompt",
    currentOperationId: string,
    operationFor: OperationFactory,
    revisionExpectation?: ReferenceRevisionExpectation,
  ): Promise<ReferenceExecutionReport> {
    const initial = await this.snapshot(scope);
    const refreshed = requireConfirmedMapping(initial, chain.mappingPlan.id);
    const bindingBefore = currentBinding(refreshed);
    const operationInputBinding = renderOperationBinding(refreshed);
    const promptDigest = prompt === null ? null : digest(prompt);
    if (method === "reviseWithPrompt" && (!revisionExpectation || !target || !promptDigest)) {
      throw new Error("reference-revision-input-binding-required");
    }
    if (revisionExpectation && target) {
      requireExactCurrentRevision(initial, revisionExpectation, target, refreshed);
    }
    const inputBindingDigest = digest(canonicalJson(method === "reviseWithPrompt"
      ? {
          workflow: operationInputBinding,
          current: { ...revisionExpectation, target },
          promptDigest,
        }
      : operationInputBinding));
    const rendered = await renderer({ ...refreshed, scope, operationKey, target, prompt });
    try {
      assertRenderShape(rendered, target, refreshed.mappingPlan);
      if (canonicalJson(rendered.reportInput.bindings) !== canonicalJson(operationInputBinding)) {
        throw new Error("reference-render-bindings-mismatch");
      }
      const commitRendered = async () => {
        const latest = await this.snapshot(scope);
        let latestChain: CurrentChain;
        try {
          latestChain = requireConfirmedMapping(latest, chain.mappingPlan.id);
        } catch {
          throw new Error("reference-render-input-stale");
        }
        if (currentBinding(latestChain) !== bindingBefore) throw new Error("reference-render-input-stale");
        if (revisionExpectation && target) {
          requireExactCurrentRevision(latest, revisionExpectation, target, latestChain);
        }
        const current = currentArtifacts(latest, chain.mappingPlan);
        const selected = mergeRenderedArtifacts(current, rendered, target);
        const invalidated = invalidateCurrentArtifactKinds(
          latest,
          chain.mappingPlan,
          target === null || target === "editorial-master"
            ? new Set(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"])
            : new Set([target]),
          operationKey,
        );
        const report = buildExecutionReport(
          latestChain, selected, rendered.reportInput, invalidated, this.now(), {
            operationId: currentOperationId,
            method,
            inputBindingDigest,
            target,
            promptDigest: prompt === null ? null : digest(prompt),
            producedKinds: rendered.plans.map((plan) => plan.kind),
          },
        );
        const review = buildMobileReview(latestChain, latestChain.mappingPlan, report, {
          ...invalidated,
          variantPlans: [...invalidated.variantPlans, ...rendered.plans],
          drafts: [...invalidated.drafts, ...rendered.drafts],
        }, this.now());
        const expectedResult = {
          id: review.id,
          version: review.version,
          reportId: report.id,
          reportVersion: report.version,
        };
        const operation = method === "render"
          ? operationFor({
            method,
            planId: latestChain.mappingPlan.id,
            inputBindingDigest,
            expectedResult,
          })
          : operationFor({
            method,
            sampleId: latestChain.sample.id,
            target: target!,
            promptDigest: promptDigest!,
            report: revisionExpectation!.report,
            draft: revisionExpectation!.draft,
            inputBindingDigest,
            expectedResult,
          });
        const next = completeOperation({
          ...invalidated,
          variantPlans: [...invalidated.variantPlans, ...rendered.plans],
          drafts: [...invalidated.drafts, ...rendered.drafts],
          executionReports: [...invalidated.executionReports, report],
          mobileReviews: [...invalidated.mobileReviews, review],
        }, operation, { kind: "report", id: report.id, version: report.version });
        await this.options.store.commit(withDigest(next), latest.digest);
        return report;
      };
      return this.options.withCurrentAssetAuthorization
        ? await this.options.withCurrentAssetAuthorization({
            scope,
            sample: refreshed.sample,
            mappingPlan: refreshed.mappingPlan,
          }, commitRendered)
        : await commitRendered();
    } catch (error) {
      await this.options.cleanupRenderedOutput(rendered).catch((cleanupError: unknown) => {
        throw new Error(`reference-render-cleanup-failed:${errorMessage(cleanupError)}`, { cause: error });
      });
      if (errorMessage(error) === "reference-state-conflict") {
        throw new Error("reference-render-input-stale", { cause: error });
      }
      throw error;
    }
  }

  private async snapshot(scope: StudioScope): Promise<ReferenceWorkflowSnapshot> {
    try {
      return await this.options.store.snapshot();
    } catch (error) {
      const reason = errorMessage(error);
      if (!INTEGRITY_FAILURES.some((code) => reason.includes(code))) throw error;
      await this.options.quarantineIncompatibleState?.(scope, reason);
      throw new Error("reference-workflow-repair-required", { cause: error });
    }
  }

  private assertBoundScope(scope: StudioScope): void {
    const bound = this.options.store.boundScope();
    if (scope.customerId !== bound.customerId || scope.sessionId !== bound.sessionId) {
      throw new Error("reference-editor-scope-mismatch");
    }
  }

  private async runMutation<T>(
    scope: StudioScope,
    method: ReferenceEditorOperationMethod,
    operationKey: string,
    requestValue: unknown,
    work: (
      snapshot: ReferenceWorkflowSnapshot,
      operationId: string,
      operationFor: OperationFactory,
    ) => Promise<T>,
  ): Promise<T> {
    const requestIntent = operationRequestIntent(method, requestValue);
    const id = referenceEditorOperationId(scope, method, operationKey);
    const intentDigest = digest(canonicalJson({ actorId: scope.actorId, requestIntent }));
    const sharedKey = `${this.storeCoordinatorIdentity()}\0${scope.customerId}\0${scope.sessionId}\0${method}\0${operationKey}`;
    const running = ReferenceGuidedEditor.inFlight.get(sharedKey);
    if (running) {
      if (running.intentDigest !== intentDigest) throw new Error("reference-operation-key-conflict");
      return running.promise as Promise<T>;
    }
    const promise = (async () => {
      const snapshot = await this.snapshot(scope);
      const existing = snapshot.editorOperations.find((candidate) => candidate.id === id);
      if (existing) {
        if (existing.actorId !== scope.actorId ||
            canonicalJson(referenceEditorOperationRequestIntent(existing.intent)) !== canonicalJson(requestIntent)) {
          throw new Error("reference-operation-key-conflict");
        }
        assertOperationReplayCurrent(snapshot, existing);
        return replayOperation<T>(snapshot, existing);
      }
      const operationFor: OperationFactory = (resolvedIntent) => {
        const operation = referenceEditorOperationMetadata(scope, method, operationKey, resolvedIntent);
        if (canonicalJson(referenceEditorOperationRequestIntent(operation.intent)) !== canonicalJson(requestIntent)) {
          throw new Error("reference-operation-intent-resolution-mismatch");
        }
        return operation;
      };
      return work(snapshot, id, operationFor);
    })();
    ReferenceGuidedEditor.inFlight.set(sharedKey, { intentDigest, promise });
    try {
      return await promise;
    } finally {
      if (ReferenceGuidedEditor.inFlight.get(sharedKey)?.promise === promise) {
        ReferenceGuidedEditor.inFlight.delete(sharedKey);
      }
    }
  }

  private async commit(
    current: ReferenceWorkflowSnapshot,
    next: ReferenceWorkflowSnapshot | Omit<ReferenceWorkflowSnapshot, "digest">,
  ): Promise<void> {
    await this.options.store.commit(withDigest(next), current.digest);
  }

  private storeCoordinatorIdentity(): string {
    const explicit = this.options.store.operationCoordinatorIdentity?.();
    if (explicit) return explicit;
    const store = this.options.store as object;
    const existing = ReferenceGuidedEditor.storeIdentities.get(store);
    if (existing) return existing;
    const assigned = `reference-store-instance-${ReferenceGuidedEditor.nextStoreIdentity++}`;
    ReferenceGuidedEditor.storeIdentities.set(store, assigned);
    return assigned;
  }
}

function requireRights(snapshot: ReferenceWorkflowSnapshot, sampleId: string) {
  const sample = latestByVersion(snapshot.samples.filter((value) => value.id === sampleId));
  if (!sample || sample.status !== "ready") throw new Error("reference-rights-declaration-required");
  const rights = latestByVersion(snapshot.rightsDeclarations.filter((value) =>
    canonicalJson(value.sample) === canonicalJson(sampleBinding(sample))));
  if (!rights) throw new Error("reference-rights-declaration-required");
  return { sample, rights };
}

function requireAnalysis(snapshot: ReferenceWorkflowSnapshot, analysisId: string) {
  const analysis = latestByVersion(snapshot.analyses.filter((value) => value.id === analysisId));
  if (!analysis) throw new Error("reference-analysis-required");
  const sample = latestByVersion(snapshot.samples.filter((value) =>
    canonicalJson(sampleBinding(value)) === canonicalJson(analysis.bindings.sample)));
  const rights = latestByVersion(snapshot.rightsDeclarations.filter((value) =>
    canonicalJson(rightsBinding(value)) === canonicalJson(analysis.bindings.rights)));
  if (!sample || !rights || sample.status !== "ready") throw new Error("reference-analysis-input-stale");
  const current = requireRights(snapshot, sample.id);
  if (current.sample.version !== sample.version || current.rights.id !== rights.id ||
      current.rights.version !== rights.version) throw new Error("reference-analysis-input-stale");
  return { sample, rights, analysis };
}

function requireSelection(snapshot: ReferenceWorkflowSnapshot, sampleId: string) {
  const { sample, rights } = requireRights(snapshot, sampleId);
  const analyses = snapshot.analyses.filter((analysis) =>
    canonicalJson(analysis.bindings.sample) === canonicalJson(sampleBinding(sample)) &&
    canonicalJson(analysis.bindings.rights) === canonicalJson(rightsBinding(rights)));
  const analysis = latestByVersion(analyses);
  if (!analysis) throw new Error("reference-analysis-required");
  const selection = latestByVersion(snapshot.styleSelections.filter((value) =>
    value.status === "confirmed" &&
    canonicalJson(value.bindings.analysis) === canonicalJson(scopedBinding(analysis)) &&
    canonicalJson(value.bindings.rights) === canonicalJson(rightsBinding(rights))));
  if (!selection) throw new Error("reference-style-selection-required");
  return { sample, rights, analysis, selection };
}

function requireConfirmedMapping(snapshot: ReferenceWorkflowSnapshot, planId: string): CurrentChain {
  const mappingPlan = latestByVersion(snapshot.mappingPlans.filter((value) => value.id === planId));
  if (!mappingPlan || mappingPlan.status !== "confirmed") {
    throw new Error("confirmed-reference-mapping-plan-required");
  }
  const selection = currentSelectionForMapping(snapshot, mappingPlan);
  if (!selection) throw new Error("reference-mapping-input-stale");
  const chain = requireSelection(snapshot, mappingPlan.bindings.sample.id);
  if (chain.selection.id !== selection.id || chain.selection.version !== selection.version) {
    throw new Error("reference-mapping-input-stale");
  }
  const currentMapping = latestCurrentMapping(snapshot, chain.selection);
  if (!currentMapping || currentMapping.id !== mappingPlan.id ||
      currentMapping.version !== mappingPlan.version ||
      currentMapping.contentDigest !== mappingPlan.contentDigest) {
    throw new Error("reference-mapping-input-stale");
  }
  return { ...chain, mappingPlan };
}

function requireCurrentRenderedChain(snapshot: ReferenceWorkflowSnapshot, sampleId: string): CurrentChain {
  const chain = requireSelection(snapshot, sampleId);
  const mappingPlan = latestCurrentMapping(snapshot, chain.selection);
  if (!mappingPlan || mappingPlan.status !== "confirmed" || !latestReport(snapshot, mappingPlan)) {
    throw new Error("reference-render-required");
  }
  return { ...chain, mappingPlan };
}

function requireApprovalTarget(
  snapshot: ReferenceWorkflowSnapshot,
  input: ApproveReferenceDraftInput,
) {
  const report = snapshot.executionReports.find((candidate) =>
    candidate.id === input.reportId && candidate.version === input.reportVersion);
  if (!report || report.status !== "reviewable") throw new Error("reference-approval-input-stale");
  const chain = requireCurrentRenderedChain(snapshot, report.bindings.sample.id);
  const currentReport = latestReport(snapshot, chain.mappingPlan);
  if (!currentReport || currentReport.id !== report.id || currentReport.version !== report.version) {
    throw new Error("reference-approval-input-stale");
  }
  const artifact = report.artifacts.find((candidate) =>
    candidate.draftId === input.draftId && candidate.draftVersion === input.draftVersion &&
    candidate.outputDigest === input.draftDigest);
  if (!artifact) throw new Error("reference-approval-input-stale");
  const draft = snapshot.drafts.find((candidate) =>
    candidate.id === input.draftId && candidate.version === input.draftVersion);
  if (!draft || draft.status !== "reviewable" || draft.outputDigest !== input.draftDigest ||
      draft.bindings.mappingPlan.id !== chain.mappingPlan.id ||
      draft.bindings.mappingPlan.version !== chain.mappingPlan.version ||
      draft.bindings.variantPlan.kind !== artifact.kind) {
    throw new Error("reference-approval-input-stale");
  }
  const variant = snapshot.variantPlans.find((candidate) =>
    candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
  if (!variant || variant.status === "invalidated" || variant.kind !== artifact.kind ||
      draft.bindings.variantPlan.id !== variant.id || draft.bindings.variantPlan.version !== variant.version) {
    throw new Error("reference-approval-input-stale");
  }
  return { chain, report, artifact, draft, variant };
}

function requireExactCurrentRevision(
  snapshot: ReferenceWorkflowSnapshot,
  expectation: ReferenceRevisionExpectation,
  target: ReferenceVariantKind,
  expectedChain: CurrentChain,
): void {
  try {
    const current = requireApprovalTarget(snapshot, {
      draftId: expectation.draft.id,
      reportId: expectation.report.id,
      reportVersion: expectation.report.version,
      draftVersion: expectation.draft.version,
      draftDigest: expectation.draft.outputDigest,
    });
    if (current.artifact.kind !== target || currentBinding(current.chain) !== currentBinding(expectedChain)) {
      throw new Error("reference-render-input-stale");
    }
  } catch (error) {
    if (errorMessage(error) === "reference-render-input-stale") throw error;
    throw new Error("reference-render-input-stale", { cause: error });
  }
}

function currentSelectionForMapping(snapshot: ReferenceWorkflowSnapshot, mapping: ReferenceMaterialMappingPlan) {
  return snapshot.styleSelections.find((selection) =>
    selection.status === "confirmed" &&
    canonicalJson(styleSelectionBinding(selection)) === canonicalJson(mapping.bindings.styleSelection));
}

function latestCurrentMapping(snapshot: ReferenceWorkflowSnapshot, selection: ReferenceStyleSelection) {
  const latestById = new Map<string, ReferenceMaterialMappingPlan>();
  for (const plan of snapshot.mappingPlans) {
    if (canonicalJson(plan.bindings.sample) !== canonicalJson(selection.bindings.sample) ||
        canonicalJson(plan.bindings.rights) !== canonicalJson(selection.bindings.rights) ||
        canonicalJson(plan.bindings.analysis) !== canonicalJson(selection.bindings.analysis) ||
        canonicalJson(plan.bindings.styleSelection) !== canonicalJson(styleSelectionBinding(selection))) continue;
    const current = latestById.get(plan.id);
    if (!current || plan.version > current.version) latestById.set(plan.id, plan);
  }
  const confirmed = [...latestById.values()].filter((plan) => plan.status === "confirmed");
  if (confirmed.length > 1) throw new Error("reference-current-mapping-ambiguous");
  return confirmed[0];
}

function latestReport(snapshot: ReferenceWorkflowSnapshot, mapping: ReferenceMaterialMappingPlan) {
  const reports = latestPerId(snapshot.executionReports).filter((report) =>
    report.status === "reviewable" &&
    report.bindings.mappingPlan.id === mapping.id && report.bindings.mappingPlan.version === mapping.version);
  if (reports.length === 0) return undefined;
  const highestVersion = Math.max(...reports.map((report) => report.version));
  const current = reports.filter((report) => report.version === highestVersion);
  if (current.length !== 1) throw new Error("reference-current-report-ambiguous");
  return current[0];
}

function currentArtifacts(snapshot: ReferenceWorkflowSnapshot, mapping: ReferenceMaterialMappingPlan) {
  const report = latestReport(snapshot, mapping);
  if (!report || report.status !== "reviewable") return new Map<ReferenceVariantKind, {
    plan: ReferenceVariantPlan; draft: ReferenceDraftPackage;
  }>();
  const result = new Map<ReferenceVariantKind, { plan: ReferenceVariantPlan; draft: ReferenceDraftPackage }>();
  for (const artifact of report.artifacts) {
    const plan = snapshot.variantPlans.find((value) =>
      value.id === artifact.variantPlanId && value.version === artifact.variantPlanVersion);
    const draft = snapshot.drafts.find((value) =>
      value.id === artifact.draftId && value.version === artifact.draftVersion);
    if (!plan || !draft || plan.status === "invalidated" || draft.status === "invalidated") continue;
    result.set(artifact.kind, { plan, draft });
  }
  return result;
}

function mergeRenderedArtifacts(
  current: Map<ReferenceVariantKind, { plan: ReferenceVariantPlan; draft: ReferenceDraftPackage }>,
  rendered: ReferenceEditorRenderResult,
  target: ReferenceVariantKind | null,
) {
  const merged = new Map(current);
  for (const plan of rendered.plans) {
    const draft = rendered.drafts.find((value) => value.bindings.variantPlan.id === plan.id);
    if (!draft) throw new Error("reference-render-artifact-set-invalid");
    merged.set(plan.kind, { plan, draft });
  }
  if (target === null || target === "editorial-master") {
    if (rendered.plans.length !== 4) throw new Error("reference-render-artifact-set-invalid");
  } else if (rendered.plans.length !== 1 || rendered.plans[0]?.kind !== target) {
    throw new Error("reference-render-artifact-set-invalid");
  }
  for (const kind of ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const) {
    if (!merged.has(kind)) throw new Error("reference-render-artifact-set-invalid");
  }
  return merged;
}

function buildExecutionReport(
  chain: CurrentChain,
  artifacts: Map<ReferenceVariantKind, { plan: ReferenceVariantPlan; draft: ReferenceDraftPackage }>,
  evidence: ReferenceRenderEvidence,
  snapshot: ReferenceWorkflowSnapshot,
  now: string,
  operation: Readonly<{
    operationId: string;
    method: "render" | "reviseWithPrompt";
    inputBindingDigest: string;
    target: ReferenceVariantKind | null;
    promptDigest: string | null;
    producedKinds: readonly ReferenceVariantKind[];
  }>,
): ReferenceExecutionReport {
  const versions = snapshot.executionReports.filter((report) =>
    report.bindings.mappingPlan.id === chain.mappingPlan.id &&
    report.bindings.mappingPlan.version === chain.mappingPlan.version);
  const rows = [...artifacts.entries()].map(([kind, value]) => ({
    kind,
    variantPlanId: value.plan.id,
    variantPlanVersion: value.plan.version,
    draftId: value.draft.id,
    draftVersion: value.draft.version,
    outputDigest: value.draft.outputDigest,
  }));
  const section = (kind: ReferenceStyleAnalysis["rules"][number]["kind"]) => {
    const ruleIds = new Set(chain.analysis.rules.filter((rule) => rule.kind === kind).map((rule) => rule.id));
    const mappings = chain.mappingPlan.mappings.filter((mapping) => ruleIds.has(mapping.ruleId));
    const mappedStatus = executionStatus(mappings.map((mapping) => mapping.status));
    const status = mappedStatus === "matched" &&
      !mappings.every((mapping) => mappingWasActuallyExecuted(kind, mapping, chain.analysis, artifacts, evidence))
      ? "substituted" as const
      : mappedStatus;
    const actualTransitionEvidence = kind === "transition"
      ? evidence.executedTransitions.filter((item) => {
        const plan = artifacts.get(item.variantKind)?.plan;
        const entry = plan?.timeline.find((candidate) => candidate.id === item.timelineEntryId);
        return Boolean(entry && entry.transition === item.kind && entry.timelineStartMs === item.offsetMs);
      }).map((item) =>
        `transition:${item.kind}:${digest(`${item.variantKind}:${item.timelineEntryId}`).slice(0, 16)}`)
      : [];
    return {
      status,
      summaryCode: `reference.${kind}.${status}`,
      evidenceRefs: [...new Set([...mappings.map((mapping) => mapping.id), ...actualTransitionEvidence])],
    };
  };
  const platformChanges = (["xiaohongshu", "douyin", "wechat-channels"] as const).map((kind) => ({
    kind,
    changeCodes: artifacts.get(kind)!.plan.platformChangeCodes,
  }));
  return ReferenceExecutionReportSchema.parse({
    id: `reference_execution_report_${digest(chain.mappingPlan.id).slice(0, 32)}`,
    customerId: chain.mappingPlan.customerId,
    sessionId: chain.mappingPlan.sessionId,
    version: nextVersion(versions),
    bindings: artifacts.get("editorial-master")!.plan.bindings,
    artifacts: rows,
    rhythm: section("rhythm"),
    transitions: section("transition"),
    narrative: section("narrative"),
    audio: section("audio"),
    mappingCounts: mappingCounts(chain.mappingPlan),
    platformChanges,
    operation: {
      operationId: operation.operationId,
      method: operation.method,
      inputBindingDigest: operation.inputBindingDigest,
      target: operation.target,
      promptDigest: operation.promptDigest,
      producedArtifacts: operation.producedKinds.map((kind) => {
        const artifact = rows.find((candidate) => candidate.kind === kind);
        if (!artifact) throw new Error("reference-render-artifact-set-invalid");
        return artifact;
      }),
    },
    status: "reviewable",
    publishClaimed: false,
    createdAt: now,
  });
}

function mappingWasActuallyExecuted(
  kind: ReferenceStyleAnalysis["rules"][number]["kind"],
  mapping: ReferenceMaterialMappingPlan["mappings"][number],
  analysis: ReferenceStyleAnalysis,
  artifacts: Map<ReferenceVariantKind, { plan: ReferenceVariantPlan; draft: ReferenceDraftPackage }>,
  evidence: ReferenceRenderEvidence,
): boolean {
  if (mapping.status === "unachievable") return false;
  const plans = [...artifacts.values()].map((value) => value.plan);
  if (kind === "audio") {
    return plans.some((plan) => {
      if (!plan.audioOverlay) return false;
      return plan.authorizedSources.some((source) =>
        source.id === plan.audioOverlay!.sourceBindingId && source.mappingId === mapping.id &&
        evidence.sourceLineage.some((lineage) => lineage.kind === plan.kind &&
          lineage.timelineEntryId === `audio-${source.id}` && lineage.mappingId === mapping.id &&
          lineage.sourceBindingId === source.id && lineage.sourceAssetId === source.sourceAssetId &&
          lineage.sourceDigest === source.sourceDigest &&
          lineage.sourceStartMs === plan.audioOverlay!.sourceStartMs &&
          lineage.sourceEndMs === plan.audioOverlay!.sourceEndMs &&
          lineage.permissionId === source.permission.id &&
          lineage.permissionVersion === source.permission.version &&
          lineage.permissionEpoch === source.permission.epoch));
    });
  }
  const entries = plans.flatMap((plan) => plan.timeline
    .filter((entry) => entry.mappingId === mapping.id)
    .map((entry) => ({ plan, entry })));
  const executedEntries = entries.filter(({ plan, entry }) => {
    const source = plan.authorizedSources.find((candidate) => candidate.id === entry.sourceBindingId);
    return Boolean(source && evidence.sourceLineage.some((lineage) =>
      lineage.kind === plan.kind && lineage.timelineEntryId === entry.id &&
      lineage.mappingId === entry.mappingId && lineage.sourceBindingId === entry.sourceBindingId &&
      lineage.sourceAssetId === source.sourceAssetId && lineage.sourceDigest === source.sourceDigest &&
      lineage.sourceStartMs === entry.sourceStartMs && lineage.sourceEndMs === entry.sourceEndMs &&
      lineage.permissionId === source.permission.id &&
      lineage.permissionVersion === source.permission.version &&
      lineage.permissionEpoch === source.permission.epoch));
  });
  if (executedEntries.length === 0) return false;
  if (kind !== "transition") return true;
  const rule = analysis.rules.find((candidate) => candidate.id === mapping.ruleId);
  const expectedKinds = new Set(analysis.transitions
    .filter((transition) => rule?.evidenceIds.includes(transition.id) && transition.kind !== "unsupported")
    .map((transition) => transition.kind));
  if (expectedKinds.size !== 1) return false;
  const expected = [...expectedKinds][0]!;
  if (expected === "cut") return executedEntries.some(({ entry }) => entry.transition === "cut");
  return evidence.executedTransitions.some((executed) => executed.kind === expected &&
    executedEntries.some(({ plan, entry }) => plan.kind === executed.variantKind &&
      entry.id === executed.timelineEntryId && entry.transition === executed.kind &&
      entry.timelineStartMs === executed.offsetMs));
}

function buildMobileReview(
  chain: Omit<CurrentChain, "mappingPlan">,
  mapping: ReferenceMaterialMappingPlan,
  report: ReferenceExecutionReport,
  snapshot: Pick<ReferenceWorkflowSnapshot,
    "drafts" | "variantPlans" | "executionReports" | "mobileReviews" | "approvals">,
  now: string,
): ReferenceMobileReviewV1 {
  const invalidatedApprovalIds = new Set(snapshot.approvals
    .filter((approval) => approval.status === "invalidated").map((approval) => approval.id));
  const allowLegacyApproval = legacyApprovalCanBindReport(snapshot.executionReports, report);
  const artifacts = report.artifacts.map((artifact) => {
    const draft = snapshot.drafts.find((value) =>
      value.id === artifact.draftId && value.version === artifact.draftVersion);
    if (!draft) throw new Error("reference-mobile-artifact-missing");
    const approval = snapshot.approvals.find((candidate) => candidate.status === "active" &&
      !invalidatedApprovalIds.has(candidate.id) && referenceApprovalMatchesCurrent(
        candidate, chain.sample, mapping, report, draft, artifact.kind, allowLegacyApproval,
      ));
    return {
      ...artifact,
      mediaRoute: { kind: "reference-draft-media" as const, version: 1 as const, draftId: artifact.draftId },
      status: approval ? "approved" as const : draft.status,
    };
  });
  const status = artifacts.every((artifact) => artifact.status === "approved") ? "approved" as const : "reviewable" as const;
  const approvedArtifactKinds = artifacts
    .filter((artifact) => artifact.status === "approved")
    .map((artifact) => artifact.kind);
  return ReferenceMobileReviewV1Schema.parse({
    id: `reference_mobile_review_${digest(`${report.id}:${report.version}:${canonicalJson(
      artifacts.map((artifact) => ({ kind: artifact.kind, status: artifact.status })),
    )}`).slice(0, 32)}`,
    customerId: report.customerId,
    sessionId: report.sessionId,
    version: 1,
    bindings: {
      ...report.bindings,
      executionReport: {
        id: report.id, version: report.version, customerId: report.customerId, sessionId: report.sessionId,
      },
    },
    styleSummary: {
      status: chain.analysis.status,
      ruleKinds: [...new Set(chain.analysis.rules.map((rule) => rule.kind))],
      capability: chain.analysis.capability.mode === "provider"
        ? {
            mode: "provider" as const,
            label: "移动审片：已配置模型服务 A/B，节奏/转场" as const,
            version: 1 as const,
            provider: "configured-provider" as const,
          }
        : {
            mode: "offline-fallback" as const,
            label: "离线/模拟分析" as const,
            version: 1 as const,
            provider: null,
          },
      warningCodes: chain.analysis.warnings,
    },
    artifacts,
    executionReport: {
      id: report.id,
      version: report.version,
      artifacts: report.artifacts,
      rhythm: report.rhythm,
      transitions: report.transitions,
      narrative: report.narrative,
      audio: report.audio,
      mappingCounts: report.mappingCounts,
      platformChanges: report.platformChanges,
    },
    approval: {
      status: approvedArtifactKinds.length === 4 ? "approved" : "pending",
      approvedArtifactKinds,
    },
    status,
    publishClaimed: false,
    createdAt: now,
  });
}

function invalidateForUpstream(
  snapshot: ReferenceWorkflowSnapshot,
  input: Readonly<{
    operationKey: string;
    selections?: (selection: ReferenceStyleSelection) => boolean;
    mappings?: (mapping: ReferenceMaterialMappingPlan) => boolean;
  }>,
): ReferenceWorkflowSnapshot {
  let next = snapshot;
  if (input.selections) {
    const invalidated = latestPerId(snapshot.styleSelections.filter((value) =>
      input.selections!(value))).filter((value) => value.status === "confirmed").map((value) => ({
        ...value, version: value.version + 1, status: "invalidated" as const,
      }));
    next = withDigest({ ...next, styleSelections: [...next.styleSelections, ...invalidated] });
  }
  if (input.mappings) next = invalidateMappingPlans(next, input.mappings, input.operationKey);
  return next;
}

function invalidateMappingPlans(
  snapshot: ReferenceWorkflowSnapshot,
  predicate: (mapping: ReferenceMaterialMappingPlan) => boolean,
  operationKey: string,
): ReferenceWorkflowSnapshot {
  const current = latestPerId(snapshot.mappingPlans).filter((plan) =>
    plan.status !== "invalidated" && predicate(plan));
  if (current.length === 0) return snapshot;
  const ids = new Set(current.map((plan) => plan.id));
  const mappingPlans = current.map((plan) => ({ ...plan, version: plan.version + 1, status: "invalidated" as const }));
  const rendered = invalidateRenderedState(snapshot, (plan) => ids.has(plan.id), operationKey);
  return withDigest({ ...rendered, mappingPlans: [...rendered.mappingPlans, ...mappingPlans] });
}

function invalidateRenderedState(
  snapshot: ReferenceWorkflowSnapshot,
  predicate: (mapping: ReferenceMaterialMappingPlan) => boolean,
  operationKey: string,
): ReferenceWorkflowSnapshot {
  const mappings = snapshot.mappingPlans.filter(predicate);
  const keys = new Set(mappings.map((plan) => `${plan.id}:${plan.version}`));
  const reports = latestPerId(snapshot.executionReports).filter((report) =>
    keys.has(`${report.bindings.mappingPlan.id}:${report.bindings.mappingPlan.version}`) && report.status === "reviewable");
  const variants = latestPerId(snapshot.variantPlans).filter((plan) =>
    keys.has(`${plan.bindings.mappingPlan.id}:${plan.bindings.mappingPlan.version}`) && plan.status !== "invalidated");
  const invalidVariants = variants.map((plan) => ({ ...plan, version: plan.version + 1, status: "invalidated" as const }));
  const invalidById = new Map(invalidVariants.map((plan) => [plan.id, plan]));
  const affectedDrafts = snapshot.drafts.filter((draft) => invalidById.has(draft.bindings.variantPlan.id) &&
    draft.status !== "invalidated");
  const drafts = affectedDrafts.map((draft) => {
    const variant = invalidById.get(draft.bindings.variantPlan.id)!;
    return {
      ...draft,
      id: `${draft.id}-invalidated-${digest(operationKey).slice(0, 12)}`,
      bindings: { ...draft.bindings, variantPlan: { ...draft.bindings.variantPlan, version: variant.version } },
      status: "invalidated" as const,
    };
  });
  const invalidReports = reports.map((report) => ({
    ...report, version: report.version + 1, status: "invalidated" as const,
  }));
  const reportVersions = new Map(invalidReports.map((report) => [report.id, report]));
  const affectedDraftIds = new Set(affectedDrafts.map((draft) => draft.id));
  const approvals = invalidateApprovals(snapshot, affectedDraftIds);
  const mobileReviews = snapshot.mobileReviews.filter((review) =>
    reportVersions.get(review.bindings.executionReport.id)?.version ===
      review.bindings.executionReport.version + 1 && review.status !== "invalidated")
    .map((review) => {
      const report = reportVersions.get(review.bindings.executionReport.id)!;
      return {
        ...review,
        id: `${review.id}-invalidated-${digest(operationKey).slice(0, 12)}`,
        bindings: {
          ...review.bindings,
          executionReport: { ...review.bindings.executionReport, version: report.version },
        },
        artifacts: review.artifacts.map((artifact) => ({ ...artifact, status: "invalidated" as const })),
        executionReport: { ...review.executionReport, version: report.version },
        status: "invalidated" as const,
      };
    });
  return withDigest({
    ...snapshot,
    variantPlans: [...snapshot.variantPlans, ...invalidVariants],
    drafts: [...snapshot.drafts, ...drafts],
    executionReports: [...snapshot.executionReports, ...invalidReports],
    approvals,
    mobileReviews: [...snapshot.mobileReviews, ...mobileReviews],
  });
}

function invalidateCurrentArtifactKinds(
  snapshot: ReferenceWorkflowSnapshot,
  mapping: ReferenceMaterialMappingPlan,
  kinds: ReadonlySet<ReferenceVariantKind>,
  operationKey: string,
): ReferenceWorkflowSnapshot {
  const report = latestReport(snapshot, mapping);
  if (!report || report.status !== "reviewable") return snapshot;
  const targets = report.artifacts.filter((artifact) => kinds.has(artifact.kind));
  if (targets.length === 0) return snapshot;
  const invalidVariants = targets.map((artifact) => {
    const plan = snapshot.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
    if (!plan) throw new Error("reference-render-audit-artifact-missing");
    return { ...plan, version: plan.version + 1, status: "invalidated" as const };
  });
  const variantsById = new Map(invalidVariants.map((plan) => [plan.id, plan]));
  const invalidDrafts = targets.map((artifact) => {
    const draft = snapshot.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
    const variant = variantsById.get(artifact.variantPlanId);
    if (!draft || !variant) throw new Error("reference-render-audit-artifact-missing");
    return {
      ...draft,
      id: `${draft.id}-invalidated-${digest(operationKey).slice(0, 12)}`,
      bindings: { ...draft.bindings, variantPlan: { ...draft.bindings.variantPlan, version: variant.version } },
      status: "invalidated" as const,
    };
  });
  const invalidReport = { ...report, version: report.version + 1, status: "invalidated" as const };
  const invalidReviews = snapshot.mobileReviews.filter((review) =>
    review.bindings.executionReport.id === report.id &&
    review.bindings.executionReport.version === report.version && review.status !== "invalidated")
    .map((review) => ({
      ...review,
      id: `${review.id}-invalidated-${digest(operationKey).slice(0, 12)}`,
      bindings: {
        ...review.bindings,
        executionReport: { ...review.bindings.executionReport, version: invalidReport.version },
      },
      artifacts: review.artifacts.map((artifact) =>
        kinds.has(artifact.kind) ? { ...artifact, status: "invalidated" as const } : artifact),
      executionReport: { ...review.executionReport, version: invalidReport.version },
      status: "invalidated" as const,
    }));
  const approvals = invalidateApprovals(snapshot, new Set(targets.map((artifact) => artifact.draftId)));
  return withDigest({
    ...snapshot,
    variantPlans: [...snapshot.variantPlans, ...invalidVariants],
    drafts: [...snapshot.drafts, ...invalidDrafts],
    executionReports: [...snapshot.executionReports, invalidReport],
    approvals,
    mobileReviews: [...snapshot.mobileReviews, ...invalidReviews],
  });
}

function invalidateApprovals(
  snapshot: ReferenceWorkflowSnapshot,
  affectedDraftIds: ReadonlySet<string>,
) {
  const invalidated = snapshot.approvals.filter((approval) =>
    approval.status === "active" && affectedDraftIds.has(referenceApprovalDraftId(approval)))
    .map((approval) => ({ ...approval, status: "invalidated" as const }));
  if (invalidated.length === 0) return snapshot.approvals;
  return [...invalidated, ...snapshot.approvals];
}

function isReferenceDraftApproval(
  approval: ReferenceWorkflowSnapshot["approvals"][number],
): approval is Extract<ReferenceWorkflowSnapshot["approvals"][number], { kind: "reference-draft" }> {
  return "kind" in approval && approval.kind === "reference-draft";
}

function referenceApprovalDraftId(
  approval: ReferenceWorkflowSnapshot["approvals"][number],
): string {
  return isReferenceDraftApproval(approval) ? approval.bindings.draft.id : approval.artifactId;
}

function referenceApprovalMatchesCurrent(
  approval: ReferenceWorkflowSnapshot["approvals"][number],
  sample: ReferenceSample,
  mapping: ReferenceMaterialMappingPlan,
  report: ReferenceExecutionReport,
  draft: ReferenceDraftPackage,
  variant: ReferenceVariantKind,
  allowLegacyApproval: boolean,
): boolean {
  if (isReferenceDraftApproval(approval)) {
    return approval.bindings.executionReport.id === report.id &&
      approval.bindings.executionReport.version === report.version &&
      approval.bindings.mappingPlan.id === mapping.id &&
      approval.bindings.mappingPlan.version === mapping.version &&
      approval.bindings.mappingPlan.contentDigest === mapping.contentDigest &&
      approval.bindings.mappingPlan.permissionSetDigest === mapping.permissionSetDigest &&
      approval.bindings.draft.id === draft.id && approval.bindings.draft.version === draft.version &&
      approval.bindings.draft.outputDigest === draft.outputDigest &&
      approval.fileDigest === draft.outputDigest && approval.variant === variant &&
      approval.publishClaimed === false;
  }
  return allowLegacyApproval && approval.storyId === sample.id && approval.revisionId === mapping.id &&
    approval.revision === mapping.version && approval.artifactId === draft.id &&
    approval.artifactDigest === draft.outputDigest &&
    approval.renderInputDigest === mapping.contentDigest && approval.variant === variant;
}

function legacyApprovalCanBindReport(
  reports: readonly ReferenceExecutionReport[],
  report: ReferenceExecutionReport,
): boolean {
  return !reports.some((candidate) =>
    candidate.customerId === report.customerId && candidate.sessionId === report.sessionId &&
    candidate.bindings.mappingPlan.id === report.bindings.mappingPlan.id &&
    candidate.bindings.mappingPlan.version === report.bindings.mappingPlan.version &&
    (candidate.id !== report.id || candidate.version !== report.version));
}

function assertAnalysisCurrent(
  analysis: ReferenceStyleAnalysis,
  sample: ReferenceSample,
  rights: ReferenceRightsDeclaration,
): void {
  if (canonicalJson(analysis.bindings.sample) !== canonicalJson(sampleBinding(sample)) ||
      canonicalJson(analysis.bindings.rights) !== canonicalJson(rightsBinding(rights))) {
    throw new Error("reference-analysis-input-stale");
  }
}

function assertMappingDraftCurrent(
  plan: ReferenceMaterialMappingPlan,
  chain: Omit<CurrentChain, "mappingPlan">,
): void {
  if (plan.status !== "draft" ||
      canonicalJson(plan.bindings.sample) !== canonicalJson(sampleBinding(chain.sample)) ||
      canonicalJson(plan.bindings.rights) !== canonicalJson(rightsBinding(chain.rights)) ||
      canonicalJson(plan.bindings.analysis) !== canonicalJson(scopedBinding(chain.analysis)) ||
      canonicalJson(plan.bindings.styleSelection) !== canonicalJson(styleSelectionBinding(chain.selection))) {
    throw new Error("reference-mapping-input-stale");
  }
}

function assertRenderShape(
  result: ReferenceEditorRenderResult,
  target: ReferenceVariantKind | null,
  mappingPlan: ReferenceMaterialMappingPlan,
): void {
  if (!result?.outputDirectory?.trim() || result.plans.length !== result.drafts.length ||
      result.plans.length !== result.reportInput.artifacts.length) {
    throw new Error("reference-render-artifact-set-invalid");
  }
  const kinds = result.plans.map((plan) => plan.kind);
  if (target === null || target === "editorial-master") {
    if (canonicalJson(kinds) !== canonicalJson(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"])) {
      throw new Error("reference-render-artifact-set-invalid");
    }
  }
  for (const plan of result.plans) {
    const draft = result.drafts.find((candidate) => candidate.bindings.variantPlan.id === plan.id &&
      candidate.bindings.variantPlan.version === plan.version);
    const artifact = result.reportInput.artifacts.find((candidate) => candidate.kind === plan.kind);
    if (!draft || !artifact || artifact.variantPlanId !== plan.id || artifact.variantPlanVersion !== plan.version ||
        artifact.draftId !== draft.id || artifact.draftVersion !== draft.version ||
        artifact.outputPath !== draft.outputPath || artifact.outputDigest !== draft.outputDigest ||
        artifact.durationSeconds !== draft.durationSeconds) {
      throw new Error("reference-render-artifact-evidence-mismatch");
    }
  }
  if (canonicalJson(result.reportInput.bindings) !== canonicalJson(result.plans[0]?.bindings)) {
    throw new Error("reference-render-bindings-mismatch");
  }
  if (canonicalJson(mappingCounts(mappingPlan)) !== canonicalJson(result.reportInput.mappingCounts)) {
    throw new Error("reference-render-mapping-counts-mismatch");
  }
  const expectedChanges = result.plans.filter((plan): plan is ReferenceVariantPlan & {
    kind: "xiaohongshu" | "douyin" | "wechat-channels";
  } => plan.kind !== "editorial-master").map((plan) => ({ kind: plan.kind, changeCodes: plan.platformChangeCodes }));
  const actualChanges = result.reportInput.platformChanges.filter((change) =>
    expectedChanges.some((expected) => expected.kind === change.kind));
  if (canonicalJson(expectedChanges) !== canonicalJson(actualChanges)) {
    throw new Error("reference-render-platform-changes-mismatch");
  }
}

function executionStatus(statuses: ReferenceMaterialMappingPlan["mappings"][number]["status"][]) {
  if (statuses.length === 0 || statuses.every((status) => status === "unachievable")) return "unachievable" as const;
  if (statuses.some((status) => status !== "matched")) return "substituted" as const;
  return "matched" as const;
}

function mappingCounts(plan: ReferenceMaterialMappingPlan) {
  return {
    matched: plan.mappings.filter((row) => row.status === "matched").length,
    substituted: plan.mappings.filter((row) => row.status === "substituted").length,
    unachievable: plan.mappings.filter((row) => row.status === "unachievable").length,
  };
}

function currentBinding(chain: CurrentChain | Omit<CurrentChain, "mappingPlan">): string {
  return canonicalJson({
    sample: sampleBinding(chain.sample),
    rights: rightsBinding(chain.rights),
    analysis: scopedBinding(chain.analysis),
    selection: scopedBinding(chain.selection),
    ...("mappingPlan" in chain ? {
      mappingPlan: {
        ...scopedBinding(chain.mappingPlan),
        permissionSetDigest: chain.mappingPlan.permissionSetDigest,
        status: chain.mappingPlan.status,
      },
    } : {}),
  });
}

function upstreamBinding(chain: { sample: ReferenceSample; rights: ReferenceRightsDeclaration }): string {
  return canonicalJson({ sample: sampleBinding(chain.sample), rights: rightsBinding(chain.rights) });
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

function selectionInputBindings(
  chain: Pick<CurrentChain, "sample" | "rights" | "analysis">,
) {
  return {
    sample: sampleBinding(chain.sample),
    rights: rightsBinding(chain.rights),
    analysis: scopedBinding(chain.analysis),
  };
}

function styleSelectionBinding(selection: ReferenceStyleSelection) {
  return {
    ...scopedBinding(selection),
    status: selection.status,
    confirmation: {
      receiptId: selection.confirmationReceiptId,
      actorId: selection.actorId,
      confirmedAt: selection.confirmedAt,
    },
  };
}

function renderOperationBinding(chain: CurrentChain) {
  if (chain.selection.status !== "confirmed" || chain.mappingPlan.status !== "confirmed" ||
      !chain.selection.confirmationReceiptId || !chain.selection.confirmedAt ||
      !chain.mappingPlan.confirmationReceiptId || !chain.mappingPlan.confirmedByActorId ||
      !chain.mappingPlan.confirmedAt) {
    throw new Error("confirmed-reference-mapping-plan-required");
  }
  const permissions = new Map<string, {
    id: string; version: number; epoch: number; customerId: string; sessionId: string;
    sourceAssetId: string; sourceDigest: string;
  }>();
  for (const mapping of chain.mappingPlan.mappings) {
    if (!mapping.permission) continue;
    const { view: _view, use: _use, ...identity } = mapping.permission;
    permissions.set(identity.id, identity);
  }
  return {
    sample: sampleBinding(chain.sample),
    rights: rightsBinding(chain.rights),
    analysis: scopedBinding(chain.analysis),
    styleSelection: styleSelectionBinding(chain.selection),
    mappingPlan: {
      ...scopedBinding(chain.mappingPlan),
      permissionSetDigest: chain.mappingPlan.permissionSetDigest,
      permissions: [...permissions.values()].sort((left, right) =>
        left.id < right.id ? -1 : left.id > right.id ? 1 : 0),
      status: chain.mappingPlan.status,
      confirmation: {
        receiptId: chain.mappingPlan.confirmationReceiptId,
        actorId: chain.mappingPlan.confirmedByActorId,
        confirmedAt: chain.mappingPlan.confirmedAt,
      },
    },
  };
}

function mappingOperationIdentity(plan: ReferenceMaterialMappingPlan) {
  return { id: plan.id, version: plan.version, contentDigest: plan.contentDigest };
}

function sampleOperationIdentity(sample: ReferenceSample) {
  return { id: sample.id, version: sample.version, sourceDigest: sample.sourceDigest };
}

function compareOperationSampleIdentity(
  left: ReturnType<typeof sampleOperationIdentity>,
  right: ReturnType<typeof sampleOperationIdentity>,
): number {
  return left.id < right.id ? -1 : left.id > right.id ? 1 : left.version - right.version;
}

function compareOperationMappingIdentity(
  left: ReturnType<typeof mappingOperationIdentity>,
  right: ReturnType<typeof mappingOperationIdentity>,
): number {
  return left.id < right.id ? -1 : left.id > right.id ? 1 : left.version - right.version;
}

function scopedBinding(value: { id: string; version: number; customerId: string; sessionId: string; contentDigest: string }) {
  return {
    id: value.id,
    version: value.version,
    customerId: value.customerId,
    sessionId: value.sessionId,
    contentDigest: value.contentDigest,
  };
}

function parseRightsInput(value: DeclareReferenceRightsInput): DeclareReferenceRightsInput {
  if (!value || typeof value !== "object") throw new Error("reference-rights-input-required");
  const referenceSampleId = parseId(value.referenceSampleId, "reference-sample-id-required");
  const reusableElements = [...value.reusableElements].sort();
  if (new Set(reusableElements).size !== reusableElements.length) {
    throw new Error("duplicate-reference-reusable-element");
  }
  if (value.sourceKind === "external-style-only" && reusableElements.length > 0) {
    throw new Error("external-reference-reusable-elements-forbidden");
  }
  return { ...value, referenceSampleId, reusableElements };
}

function parseInvalidationInput(
  value: string | InvalidateReferenceAssetInput,
): InvalidateReferenceAssetInput {
  if (typeof value === "string") {
    return {
      assetId: parseId(value, "reference-asset-id-required"),
      reason: "asset-permission-invalidated",
    };
  }
  if (!value || typeof value !== "object") throw new Error("reference-asset-id-required");
  return {
    assetId: parseId(value.assetId, "reference-asset-id-required"),
    reason: parseReason(value.reason),
  };
}

function parseApprovalInput(value: ApproveReferenceDraftInput): ApproveReferenceDraftInput {
  if (!value || typeof value !== "object") throw new Error("reference-approval-input-required");
  if (!Number.isInteger(value.reportVersion) || value.reportVersion <= 0 ||
      !Number.isInteger(value.draftVersion) || value.draftVersion <= 0) {
    throw new Error("reference-approval-version-invalid");
  }
  return {
    draftId: parseId(value.draftId, "reference-draft-id-required"),
    reportId: parseId(value.reportId, "reference-execution-report-id-required"),
    reportVersion: value.reportVersion,
    draftVersion: value.draftVersion,
    draftDigest: parseDigest(value.draftDigest, "reference-draft-digest-invalid"),
  };
}

function parseRevisionInput(value: ReviseReferenceDraftInput): ReviseReferenceDraftInput {
  const approval = parseApprovalInput(value);
  if (typeof value.prompt !== "string" || !value.prompt.trim()) {
    throw new Error("reference-revision-prompt-required");
  }
  const prompt = value.prompt.trim();
  if (prompt.length > 4_096) throw new Error("reference-revision-prompt-invalid");
  return { ...approval, prompt };
}

function operationRequestIntent(method: ReferenceEditorOperationMethod, value: unknown): unknown {
  if (!value || typeof value !== "object") throw new Error("reference-operation-intent-invalid");
  const input = value as Record<string, unknown>;
  switch (method) {
    case "declareRights": return { method, ...parseRightsInput(value as DeclareReferenceRightsInput) };
    case "analyze":
    case "createMappingPlan": return {
      method,
      sampleId: parseId(input.sampleId as string, "reference-sample-id-required"),
    };
    case "confirmStyle": return {
      method,
      analysisId: parseId(input.analysisId as string, "reference-analysis-id-required"),
      selectedRuleIds: parseSelectedRuleIds(input.selectedRuleIds as readonly string[]),
    };
    case "confirmMappingPlan":
    case "render": return {
      method,
      planId: parseId(input.planId as string, "reference-mapping-plan-id-required"),
    };
    case "reviseWithPrompt": {
      const report = input.report as Record<string, unknown> | undefined;
      const draft = input.draft as Record<string, unknown> | undefined;
      if (report && draft) {
        return {
          method,
          promptDigest: parseDigest(input.promptDigest, "reference-revision-prompt-invalid"),
          report: {
            id: parseId(report.id as string, "reference-execution-report-id-required"),
            version: parsePositiveVersion(report.version, "reference-revision-version-invalid"),
          },
          draft: {
            id: parseId(draft.id as string, "reference-draft-id-required"),
            version: parsePositiveVersion(draft.version, "reference-revision-version-invalid"),
            outputDigest: parseDigest(draft.outputDigest, "reference-draft-digest-invalid"),
          },
        };
      }
      const target = input.target;
      if (!(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const).includes(target as never)) {
        throw new Error("reference-revision-target-invalid");
      }
      return {
        method,
        sampleId: parseId(input.sampleId as string, "reference-sample-id-required"),
        target,
        promptDigest: parseDigest(input.promptDigest, "reference-revision-prompt-invalid"),
      };
    }
    case "approveDraft": {
      const report = input.report as Record<string, unknown>;
      const draft = input.draft as Record<string, unknown>;
      return {
        method,
        report: {
          id: parseId(report?.id as string, "reference-execution-report-id-required"),
          version: parsePositiveVersion(report?.version, "reference-approval-version-invalid"),
        },
        draft: {
          id: parseId(draft?.id as string, "reference-draft-id-required"),
          version: parsePositiveVersion(draft?.version, "reference-approval-version-invalid"),
          outputDigest: parseDigest(draft?.outputDigest, "reference-draft-digest-invalid"),
        },
      };
    }
    case "invalidateForAsset": return {
      method,
      assetId: parseId(input.assetId as string, "reference-asset-id-required"),
      reason: parseReason(input.reason),
    };
  }
}

function parsePositiveVersion(value: unknown, error: string): number {
  if (!Number.isInteger(value) || (value as number) <= 0) throw new Error(error);
  return value as number;
}

function parseDigest(value: unknown, error: string): string {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) throw new Error(error);
  return value;
}

function parseReason(value: unknown): string {
  if (typeof value !== "string" || !/^[a-z0-9][a-z0-9._:-]{0,127}$/.test(value)) {
    throw new Error("reference-invalidation-reason-invalid");
  }
  return value;
}

function assertRightsIntent(
  existing: ReferenceRightsDeclaration,
  input: DeclareReferenceRightsInput,
  sample: ReferenceSample,
): void {
  if (existing.sample.id !== input.referenceSampleId || existing.sourceKind !== input.sourceKind ||
      canonicalJson(existing.reusableElements) !== canonicalJson(input.reusableElements) ||
      canonicalJson(existing.sample) !== canonicalJson(sampleBinding(sample))) {
    throw new Error("reference-operation-key-conflict");
  }
}

function parseSelectedRuleIds(value: readonly string[]): string[] {
  if (!Array.isArray(value) || value.length === 0) throw new Error("reference-selected-rules-required");
  const ids = value.map((item) => parseId(item, "reference-selected-rule-required"));
  if (new Set(ids).size !== ids.length) throw new Error("duplicate-selected-reference-rule-id");
  return ids.sort();
}

function parseScope(value: StudioScope): StudioScope {
  if (!value || typeof value !== "object") throw new Error("reference-scope-required");
  return {
    actorId: parseId(value.actorId, "actor-id-required"),
    customerId: parseId(value.customerId, "customer-id-required"),
    sessionId: parseId(value.sessionId, "session-id-required"),
  };
}

function parseOperationKey(value: string): string {
  const key = parseId(value, "reference-operation-key-required");
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/.test(key)) throw new Error("reference-operation-key-invalid");
  return key;
}

function parseId(value: string, error: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
  return value.trim();
}

function operationId(prefix: string, scope: StudioScope, operationKey: string): string {
  return `${prefix}_${digest(`${scope.customerId}\0${scope.sessionId}\0${operationKey}`)}`;
}

function nextVersion(values: readonly { version: number }[]): number {
  return values.length === 0 ? 1 : Math.max(...values.map((value) => value.version)) + 1;
}

function latestByVersion<T extends { version: number }>(values: readonly T[]): T | undefined {
  return [...values].sort((left, right) => right.version - left.version)[0];
}

function latestPerId<T extends { id: string; version: number }>(values: readonly T[]): T[] {
  const result = new Map<string, T>();
  for (const value of values) {
    const current = result.get(value.id);
    if (!current || value.version > current.version) result.set(value.id, value);
  }
  return [...result.values()];
}

function requireCanonicalSame<T>(existing: T, requested: T, error: string): T {
  if (canonicalJson(existing) !== canonicalJson(requested)) throw new Error(error);
  return existing;
}

function withDigest(
  value: ReferenceWorkflowSnapshot | Omit<ReferenceWorkflowSnapshot, "digest">,
): ReferenceWorkflowSnapshot {
  const payload = {
    samples: value.samples,
    rightsDeclarations: value.rightsDeclarations,
    analyses: value.analyses,
    styleSelections: value.styleSelections,
    mappingPlans: value.mappingPlans,
    variantPlans: value.variantPlans,
    drafts: value.drafts,
    executionReports: value.executionReports,
    approvals: value.approvals,
    mobileReviews: value.mobileReviews,
    editorOperations: value.editorOperations,
  };
  return { ...payload, digest: referenceWorkflowDigest(payload) };
}

function completeOperation(
  snapshot: ReferenceWorkflowSnapshot | Omit<ReferenceWorkflowSnapshot, "digest">,
  operation: ReferenceEditorOperationMetadata,
  result: ReferenceEditorOperation["result"],
): ReferenceWorkflowSnapshot {
  const resultDigest = operationResultDigest(snapshot, operation.subjectId, result);
  return withDigest({
    ...snapshot,
    editorOperations: [...snapshot.editorOperations, { ...operation, resultDigest, result }],
  });
}

function operationResultDigest(
  snapshot: ReferenceWorkflowSnapshot | Omit<ReferenceWorkflowSnapshot, "digest">,
  subjectId: string,
  result: ReferenceEditorOperation["result"],
): string {
  if (result.kind === "void") return digest(canonicalJson({ kind: "void", subjectId }));
  if (result.kind === "invalidation") return digest(canonicalJson(result));
  if (result.kind === "approval") {
    const value = snapshot.approvals.find((candidate) => isReferenceDraftApproval(candidate) &&
      candidate.id === result.id && candidate.version === result.version && candidate.status === "active");
    if (!value) throw new Error("reference-operation-result-missing");
    return digest(canonicalJson(value));
  }
  const collection = result.kind === "rights" ? snapshot.rightsDeclarations
    : result.kind === "analysis" ? snapshot.analyses
    : result.kind === "selection" ? snapshot.styleSelections
    : result.kind === "mapping" ? snapshot.mappingPlans
    : snapshot.executionReports;
  const value = collection.find((candidate) => candidate.id === result.id && candidate.version === result.version);
  if (!value) throw new Error("reference-operation-result-missing");
  return digest(canonicalJson(value));
}

function replayOperation<T>(
  snapshot: ReferenceWorkflowSnapshot,
  operation: ReferenceEditorOperation,
): T {
  const result = operation.result;
  if (result.kind === "void" || result.kind === "invalidation") return undefined as T;
  if (result.kind === "approval") {
    const value = snapshot.approvals.find((candidate) => isReferenceDraftApproval(candidate) &&
      candidate.id === result.id && candidate.version === result.version && candidate.status === "active");
    if (!value) throw new Error("reference-operation-result-missing");
    return value as T;
  }
  const collection = result.kind === "rights" ? snapshot.rightsDeclarations
    : result.kind === "analysis" ? snapshot.analyses
    : result.kind === "selection" ? snapshot.styleSelections
    : result.kind === "mapping" ? snapshot.mappingPlans
    : snapshot.executionReports;
  const value = collection.find((candidate) => candidate.id === result.id && candidate.version === result.version);
  if (!value) throw new Error("reference-operation-result-missing");
  return value as T;
}

function assertOperationReplayCurrent(
  snapshot: ReferenceWorkflowSnapshot,
  operation: ReferenceEditorOperation,
): void {
  if (operation.method === "approveDraft") {
    if (operation.result.kind !== "approval" || operation.intent.method !== "approveDraft") {
      throw new Error("reference-approval-input-stale");
    }
    const result = operation.result;
    const intent = operation.intent;
    const approval = snapshot.approvals.find((candidate) => isReferenceDraftApproval(candidate) &&
      candidate.id === result.id && candidate.version === result.version &&
      candidate.status === "active");
    if (!approval || snapshot.approvals.some((candidate) =>
      candidate.id === approval.id && candidate.status === "invalidated")) {
      throw new Error("reference-approval-input-stale");
    }
    requireApprovalTarget(snapshot, {
      draftId: intent.draft.id,
      reportId: intent.report.id,
      reportVersion: intent.report.version,
      draftVersion: intent.draft.version,
      draftDigest: intent.draft.outputDigest,
    });
    return;
  }
  if (operation.method !== "render" && operation.method !== "reviseWithPrompt") return;
  if (operation.result.kind !== "report") throw new Error("reference-render-required");
  const result = operation.result;
  const historical = snapshot.executionReports.find((candidate) =>
    candidate.id === result.id && candidate.version === result.version);
  if (!historical) throw new Error("reference-operation-result-missing");
  try {
    const chain = requireConfirmedMapping(snapshot, historical.bindings.mappingPlan.id);
    const current = latestReport(snapshot, chain.mappingPlan);
    if (!current || current.id !== historical.id || current.version !== historical.version ||
        current.status !== "reviewable") {
      throw new Error("reference-render-required");
    }
  } catch (error) {
    if (errorMessage(error) === "reference-operation-result-missing") throw error;
    throw new Error("reference-render-required", { cause: error });
  }
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

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
