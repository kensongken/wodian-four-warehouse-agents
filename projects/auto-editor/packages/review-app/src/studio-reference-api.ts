import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { appendFile, mkdir, mkdtemp, readFile, realpath, rm, stat } from "node:fs/promises";
import type { IncomingHttpHeaders, IncomingMessage } from "node:http";
import { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pipeline } from "node:stream/promises";

import {
  ReferenceMobileReviewV1Schema,
  ReferenceDraftApprovalSchema,
  ReferenceReusableElementSchema,
  ReferenceSourceKindSchema,
  type ReferenceExecutionReport,
  type ReferenceMobileReviewV1,
  type ReferenceVariantKind,
} from "@wearable/contracts";
import type {
  ReferenceGuidedEditor,
  ReferenceSampleService,
  ReferenceSampleStore,
  ReferenceWorkflowAssetBinding,
  StudioScope,
  ReferenceWorkflowSnapshot,
} from "@wearable/mobile-agent";
import Busboy from "busboy";

export const REFERENCE_UPLOAD_MAX_BYTES = 268_435_456;
export const REFERENCE_UPLOAD_MAX_DURATION_MS = 600_000;

const MAX_FIELD_BYTES = 256;
const MAX_PROCESS_OUTPUT_BYTES = 64 * 1024;
const MEDIA_PROCESS_TIMEOUT_MS = 120_000;
const SAFE_OPERATION_KEY = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;
const ALLOWED_EXTENSIONS = new Set([".mp4", ".mov"]);
const importAuditQueues = new Map<string, Promise<void>>();

export type StudioReferenceSampleService = Pick<
  ReferenceSampleService,
  "importDedicated" | "registerHistorical" | "listSamples"
> & Partial<Pick<ReferenceSampleService, "listHistoricalAssets" | "readSampleMedia">>;

export type StudioReferenceEditor = Pick<
  ReferenceGuidedEditor,
  | "declareRights"
  | "analyze"
  | "confirmStyle"
  | "createMappingPlan"
  | "confirmMappingPlan"
  | "render"
  | "reviseWithPrompt"
  | "approveDraft"
  | "invalidateForAsset"
>;

export type StudioReferenceStore = Pick<ReferenceSampleStore, "snapshot"> & Readonly<{
  withCurrentSnapshot?<T>(work: (snapshot: ReferenceWorkflowSnapshot) => Promise<T> | T): Promise<T>;
}>;

export type StudioReferenceRuntime = Readonly<{
  sampleService: StudioReferenceSampleService;
  editor: StudioReferenceEditor;
  store: StudioReferenceStore;
  enforceMaterialAuthorization?: boolean;
  prepareAnalysis?(scope: StudioScope, referenceSampleId: string): Promise<void>;
}>;

export type StudioReferenceAssetAuthorization = <T>(
  scope: StudioScope,
  bindings: readonly ReferenceWorkflowAssetBinding[],
  work: () => Promise<T> | T,
) => Promise<T>;

export type StudioReferenceApiResult = Readonly<{
  status: number;
  body: unknown;
}>;

export type StudioReferenceUploadLimits = Readonly<{
  maxUploadBytes?: number;
  maxDurationMs?: number;
}>;

export type StudioReferenceMediaProcessLimits = Readonly<{
  timeoutMs?: number;
  maxOutputBytes?: number;
}>;

export type StudioReferenceStagingHooks = Readonly<{
  afterDirectoryCreate?: () => Promise<void>;
  removeDirectory?: (directory: string) => Promise<void>;
}>;

export type StudioReferenceApi = Readonly<{
  importMultipart(request: IncomingMessage, scope: StudioScope): Promise<StudioReferenceApiResult>;
  readDraftMedia(draftId: string, scope: StudioScope): Promise<Buffer>;
  readSampleMedia(sampleId: string, scope: StudioScope): Promise<Readonly<{
    bytes: Buffer;
    contentType: "video/mp4" | "video/quicktime" | "application/octet-stream";
  }>>;
  dispatch(input: Readonly<{
    method: string;
    pathname: string;
    body: Record<string, unknown>;
    scope: StudioScope;
    editor: StudioReferenceEditor;
  }>): Promise<StudioReferenceApiResult | null>;
}>;

export function createStudioReferenceApi(input: Readonly<{
  sampleService: StudioReferenceSampleService;
  store: StudioReferenceStore;
  prepareAnalysis(scope: StudioScope, referenceSampleId: string): Promise<void>;
  withdrawAsset(scope: StudioScope, assetId: string, operationKey: string): Promise<unknown>;
  withCurrentAssetAuthorization?: StudioReferenceAssetAuthorization;
  stagingRoot: string;
  draftOutputRoot: string;
  auditPath: string;
  now?: () => string;
  ffprobePath: string;
  ffmpegPath: string;
  uploadLimits?: StudioReferenceUploadLimits;
  mediaProcessLimits?: StudioReferenceMediaProcessLimits;
  stagingHooks?: StudioReferenceStagingHooks;
}>): StudioReferenceApi {
  if (!input.stagingRoot?.trim()) throw new Error("reference-import-staging-root-required");
  if (!input.draftOutputRoot?.trim()) throw new Error("reference-draft-output-root-required");
  if (!input.auditPath?.trim()) throw new Error("reference-import-audit-path-required");
  if (!input.ffprobePath?.trim() || !input.ffmpegPath?.trim()) {
    throw new Error("reference-import-media-tools-unavailable");
  }
  const maxUploadBytes = input.uploadLimits?.maxUploadBytes ?? REFERENCE_UPLOAD_MAX_BYTES;
  const maxDurationMs = input.uploadLimits?.maxDurationMs ?? REFERENCE_UPLOAD_MAX_DURATION_MS;
  const processTimeoutMs = input.mediaProcessLimits?.timeoutMs ?? MEDIA_PROCESS_TIMEOUT_MS;
  const maxProcessOutputBytes = input.mediaProcessLimits?.maxOutputBytes ?? MAX_PROCESS_OUTPUT_BYTES;
  if (!Number.isSafeInteger(maxUploadBytes) || maxUploadBytes < 1 || maxUploadBytes > REFERENCE_UPLOAD_MAX_BYTES) {
    throw new Error("reference-import-byte-limit-invalid");
  }
  if (!Number.isSafeInteger(maxDurationMs) || maxDurationMs < 1 ||
      maxDurationMs > REFERENCE_UPLOAD_MAX_DURATION_MS) {
    throw new Error("reference-import-duration-limit-invalid");
  }
  if (!Number.isSafeInteger(processTimeoutMs) || processTimeoutMs < 1 ||
      processTimeoutMs > MEDIA_PROCESS_TIMEOUT_MS ||
      !Number.isSafeInteger(maxProcessOutputBytes) || maxProcessOutputBytes < 1 ||
      maxProcessOutputBytes > MAX_PROCESS_OUTPUT_BYTES) {
    throw new Error("reference-import-media-process-limit-invalid");
  }

  const readCurrentMobileReview = async (scope: StudioScope): Promise<ReferenceMobileReviewV1> => {
    const initial = currentAuthorizationContext(await input.store.snapshot(), scope);
    if (!input.withCurrentAssetAuthorization) return initial.review;
    return input.withCurrentAssetAuthorization(scope, initial.assetBindings, () =>
      withCurrentStoreSnapshot(input.store, (current) => {
        const refreshed = currentAuthorizationContext(current, scope);
        if (refreshed.identity !== initial.identity) {
          throw new Error("reference-workflow-input-stale");
        }
        return refreshed.review;
      }));
  };

  const readCurrentExecutionReport = async (
    scope: StudioScope,
    reportId: string,
  ): Promise<ReferenceExecutionReport> => {
    const initialSnapshot = await input.store.snapshot();
    const latest = latestReportById(initialSnapshot.executionReports, reportId);
    if (!latest || (latest.customerId && latest.customerId !== scope.customerId) ||
        (latest.sessionId && latest.sessionId !== scope.sessionId)) {
      throw new Error("reference-execution-report-not-found");
    }
    if (latest.status !== "reviewable") {
      throw new Error("reference-execution-report-not-current");
    }
    const initial = currentExecutionReportAuthorizationContext(initialSnapshot, scope, reportId);
    const readUnderCurrentStoreSnapshot = () =>
      withCurrentStoreSnapshot(input.store, (current) => {
        const refreshed = currentExecutionReportAuthorizationContext(current, scope, reportId);
        if (refreshed.identity !== initial.identity) {
          throw new Error("reference-execution-report-not-current");
        }
        return refreshed.report;
      });
    return input.withCurrentAssetAuthorization
      ? input.withCurrentAssetAuthorization(scope, initial.assetBindings, readUnderCurrentStoreSnapshot)
      : readUnderCurrentStoreSnapshot();
  };

  const withDraftActionAuthorization = async <T>(
    scope: StudioScope,
    identity: Readonly<{
      reportId: string;
      reportVersion: number;
      draftId: string;
      draftVersion: number;
      draftDigest: string;
    }>,
    work: () => Promise<T>,
  ): Promise<T> => {
    if (!input.withCurrentAssetAuthorization) return work();
    const bindings = draftActionAssetBindings(await input.store.snapshot(), scope, identity);
    // Preflight the permission ledger without nesting the library lock. The
    // production editor repeats this check while committing under the canonical
    // library -> reference-store lock order, closing the preflight race.
    await input.withCurrentAssetAuthorization(scope, bindings, () => undefined);
    return work();
  };

  return {
    async importMultipart(request, scope) {
      let upload: StagedReferenceUpload | undefined;
      let stagingCleaned = false;
      try {
        upload = await stageMultipartUpload(
          request, input.stagingRoot, maxUploadBytes, input.stagingHooks,
        );
        await verifyReferenceMedia(
          upload.path,
          input.ffprobePath,
          input.ffmpegPath,
          maxDurationMs,
          processTimeoutMs,
          maxProcessOutputBytes,
        );
        const bytes = await readFile(upload.path).catch(() => {
          throw new Error("reference-import-staging-io-failed");
        });
        await removeStagingDirectory(upload.directory, input.stagingHooks);
        stagingCleaned = true;
        const registration = await input.sampleService.importDedicated(scope, {
          requestId: upload.operationKey,
          fileName: upload.fileName,
          bytes,
          sourceKind: upload.sourceKind,
          operationKey: upload.operationKey,
        });
        return { status: 201, body: { sample: registration.sample } };
      } catch (error) {
        let failure = error;
        if (upload && !stagingCleaned) {
          try {
            await removeStagingDirectory(upload.directory, input.stagingHooks);
            stagingCleaned = true;
          } catch (cleanupError) {
            failure = cleanupError;
          }
        }
        const code = stableErrorCode(failure, "reference-import-failed");
        await appendImportFailureAudit(input.auditPath, {
          id: `reference_import_failure_${randomUUID()}`,
          createdAt: (input.now ?? (() => new Date().toISOString()))(),
          actorId: scope.actorId,
          customerId: scope.customerId,
          sessionId: scope.sessionId,
          operationKey: upload?.operationKey ?? operationKeyFromError(error),
          errorCode: code,
        });
        throw failure;
      }
    },

    async readDraftMedia(draftIdValue, scope) {
      const draftId = requireString(draftIdValue, "reference-draft-id-required");
      if (input.withCurrentAssetAuthorization) {
        const initial = currentAuthorizationContext(await input.store.snapshot(), scope);
        const initialArtifact = initial.review.artifacts.find((artifact) => artifact.draftId === draftId);
        if (!initialArtifact) {
          const historical = findScopedDraft(initial.snapshot, draftId, scope);
          if (historical) throw new Error("reference-draft-invalidated");
          throw new Error("reference-draft-not-found");
        }
        return input.withCurrentAssetAuthorization(scope, initial.assetBindings, () =>
          withCurrentStoreSnapshot(input.store, async (current) => {
            const refreshed = currentAuthorizationContext(current, scope);
            if (refreshed.identity !== initial.identity ||
                !refreshed.review.artifacts.some((artifact) =>
                  artifact.draftId === initialArtifact.draftId &&
                  artifact.draftVersion === initialArtifact.draftVersion &&
                  artifact.outputDigest === initialArtifact.outputDigest)) {
              throw new Error("reference-workflow-input-stale");
            }
            const currentDraft = exactVersion(
              current.drafts,
              initialArtifact.draftId,
              initialArtifact.draftVersion,
            );
            if (!currentDraft || currentDraft.status !== "reviewable" ||
                currentDraft.outputDigest !== initialArtifact.outputDigest) {
              throw new Error("reference-draft-invalidated");
            }
            return readVerifiedDraftBytes(currentDraft, input.draftOutputRoot);
          }));
      }
      const snapshot = await input.store.snapshot();
      const draft = snapshot.drafts
        .filter((candidate) =>
          candidate.id === draftId
          && candidate.customerId === scope.customerId
          && candidate.sessionId === scope.sessionId)
        .sort((left, right) => right.version - left.version)[0];
      if (!draft) throw new Error("reference-draft-not-found");
      if (draft.status === "invalidated" || snapshot.drafts.some((candidate) =>
        isDraftInvalidationOf(candidate, draft, scope))) {
        throw new Error("reference-draft-invalidated");
      }
      let canonicalRoot: string;
      let canonicalOutput: string;
      try {
        canonicalRoot = await realpath(resolve(input.draftOutputRoot));
        canonicalOutput = await realpath(resolve(draft.outputPath));
      } catch {
        throw new Error("reference-draft-media-unavailable");
      }
      const withinRoot = relative(canonicalRoot, canonicalOutput);
      if (!withinRoot || withinRoot === ".." || withinRoot.startsWith(`..${sep}`) || isAbsolute(withinRoot)) {
        throw new Error("reference-draft-output-binding-invalid");
      }
      const outputStat = await stat(canonicalOutput).catch(() => null);
      if (!outputStat?.isFile()) throw new Error("reference-draft-media-unavailable");
      const bytes = await readFile(canonicalOutput).catch(() => {
        throw new Error("reference-draft-media-unavailable");
      });
      if (createHash("sha256").update(bytes).digest("hex") !== draft.outputDigest) {
        throw new Error("reference-draft-output-digest-mismatch");
      }
      return withCurrentStoreSnapshot(input.store, (current) => {
        const currentDraft = findScopedDraft(current, draftId, scope);
        if (!currentDraft) throw new Error("reference-draft-not-found");
        if (
          currentDraft.status === "invalidated"
          || currentDraft.outputPath !== draft.outputPath
          || currentDraft.outputDigest !== draft.outputDigest
          || current.drafts.some((candidate) => isDraftInvalidationOf(candidate, draft, scope))
        ) throw new Error("reference-draft-invalidated");
        return bytes;
      });
    },

    async readSampleMedia(sampleIdValue, scope) {
      const sampleId = requireString(sampleIdValue, "reference-sample-id-required");
      if (!input.sampleService.readSampleMedia) throw new Error("reference-sample-media-unavailable");
      const media = await input.sampleService.readSampleMedia(scope, sampleId);
      const extension = extname(media.fileName).toLowerCase();
      return {
        bytes: Buffer.from(media.bytes),
        contentType: extension === ".mov"
          ? "video/quicktime"
          : extension === ".mp4"
            ? "video/mp4"
            : "application/octet-stream",
      };
    },

    async dispatch({ method, pathname, body, scope, editor }) {
      if (method === "POST" && pathname === "/api/studio/reference-samples/from-history") {
        assertExactBodyFields(body, ["assetId", "sourceKind", "operationKey"]);
        const registration = await input.sampleService.registerHistorical(scope, {
          assetId: requireString(body.assetId, "reference-history-asset-id-required"),
          sourceKind: requireSourceKind(body.sourceKind),
          operationKey: requireOperationKey(body.operationKey),
        });
        return { status: 201, body: { sample: registration.sample } };
      }
      if (method === "GET" && pathname === "/api/studio/reference-samples") {
        assertExactBodyFields(body, []);
        return { status: 200, body: { samples: await input.sampleService.listSamples(scope) } };
      }
      if (method === "GET" && pathname === "/api/studio/reference-history") {
        assertExactBodyFields(body, []);
        if (!input.sampleService.listHistoricalAssets) {
          throw new Error("reference-history-unavailable");
        }
        return { status: 200, body: { assets: await input.sampleService.listHistoricalAssets(scope) } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-rights") {
        assertExactBodyFields(body, [
          "referenceSampleId", "sourceKind", "reusableElements", "operationKey",
        ]);
        const rights = await editor.declareRights(scope, {
          referenceSampleId: requireString(body.referenceSampleId, "reference-sample-id-required"),
          sourceKind: requireSourceKind(body.sourceKind),
          reusableElements: requireReusableElements(body.reusableElements),
        }, requireOperationKey(body.operationKey));
        return { status: 201, body: { rights } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-analyses") {
        assertExactBodyFields(body, ["referenceSampleId", "operationKey"]);
        const referenceSampleId = requireString(body.referenceSampleId, "reference-sample-id-required");
        const operationKey = requireOperationKey(body.operationKey);
        await input.prepareAnalysis(scope, referenceSampleId);
        const analysis = await editor.analyze(
          scope,
          referenceSampleId,
          operationKey,
        );
        return { status: 201, body: { analysis } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-style-selections") {
        assertExactBodyFields(body, ["analysisId", "selectedRuleIds", "operationKey"]);
        const selection = await editor.confirmStyle(
          scope,
          requireString(body.analysisId, "reference-analysis-id-required"),
          requireStringArray(body.selectedRuleIds, "reference-selected-rule-required"),
          requireOperationKey(body.operationKey),
        );
        return { status: 201, body: { selection } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-mapping-plans") {
        assertExactBodyFields(body, ["referenceSampleId", "operationKey"]);
        const mappingPlan = await editor.createMappingPlan(
          scope,
          requireString(body.referenceSampleId, "reference-sample-id-required"),
          requireOperationKey(body.operationKey),
        );
        return { status: 201, body: { mappingPlan } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-mapping-plans/confirm") {
        assertExactBodyFields(body, ["mappingPlanId", "operationKey"]);
        const mappingPlan = await editor.confirmMappingPlan(
          scope,
          requireString(body.mappingPlanId, "reference-mapping-plan-id-required"),
          requireOperationKey(body.operationKey),
        );
        return { status: 201, body: { mappingPlan } };
      }
      if (method === "POST" && pathname === "/api/studio/reference-drafts") {
        assertExactBodyFields(body, ["mappingPlanId", "operationKey"]);
        const report = await editor.render(
          scope,
          requireString(body.mappingPlanId, "reference-mapping-plan-id-required"),
          requireOperationKey(body.operationKey),
        );
        return { status: 201, body: { report } };
      }
      const draftActionMatch = pathname.match(
        /^\/api\/studio\/reference-drafts\/([^/]+)\/(revise|approve)$/,
      );
      if (method === "POST" && draftActionMatch) {
        const draftId = requireString(
          decodePathSegment(draftActionMatch[1]!, "reference-draft-id-required"),
          "reference-draft-id-required",
        );
        const action = draftActionMatch[2]!;
        if (action === "revise") {
          assertExactBodyFields(body, [
            "reportId", "reportVersion", "draftVersion", "draftDigest", "prompt", "operationKey",
          ]);
          const revisionInput = {
            draftId,
            reportId: requireString(body.reportId, "reference-execution-report-id-required"),
            reportVersion: requirePositiveVersion(body.reportVersion, "reference-revision-version-invalid"),
            draftVersion: requirePositiveVersion(body.draftVersion, "reference-revision-version-invalid"),
            draftDigest: requireDigest(body.draftDigest, "reference-draft-digest-invalid"),
            prompt: requireString(body.prompt, "reference-revision-prompt-required"),
          };
          const operationKey = requireOperationKey(body.operationKey);
          const report = await withDraftActionAuthorization(scope, revisionInput, () =>
            editor.reviseWithPrompt(scope, revisionInput, operationKey));
          return { status: 201, body: { report } };
        }
        assertExactBodyFields(body, [
          "reportId", "reportVersion", "draftVersion", "draftDigest", "operationKey",
        ]);
        const approvalInput = {
          draftId,
          reportId: requireString(body.reportId, "reference-execution-report-id-required"),
          reportVersion: requirePositiveVersion(body.reportVersion, "reference-approval-version-invalid"),
          draftVersion: requirePositiveVersion(body.draftVersion, "reference-approval-version-invalid"),
          draftDigest: requireDigest(body.draftDigest, "reference-draft-digest-invalid"),
        };
        const operationKey = requireOperationKey(body.operationKey);
        const approval = await withDraftActionAuthorization(scope, approvalInput, () =>
          editor.approveDraft(scope, approvalInput, operationKey));
        return { status: 201, body: { approval, review: await readCurrentMobileReview(scope) } };
      }
      const withdrawalMatch = pathname.match(/^\/api\/studio\/reference-assets\/([^/]+)\/withdraw$/);
      if (method === "POST" && withdrawalMatch) {
        assertExactBodyFields(body, ["operationKey"]);
        const withdrawal = await input.withdrawAsset(
          scope,
          requireString(
            decodePathSegment(withdrawalMatch[1]!, "reference-asset-id-required"),
            "reference-asset-id-required",
          ),
          requireOperationKey(body.operationKey),
        );
        return { status: 201, body: { withdrawal } };
      }
      if (method === "GET" && pathname === "/api/studio/reference-mobile-review") {
        assertExactBodyFields(body, []);
        return { status: 200, body: { review: await readCurrentMobileReview(scope) } };
      }
      const reportMatch = pathname.match(/^\/api\/studio\/reference-execution-reports\/([^/]+)$/);
      if (method === "GET" && reportMatch) {
        assertExactBodyFields(body, []);
        const reportId = requireString(
          decodePathSegment(reportMatch[1]!),
          "reference-execution-report-id-required",
        );
        return { status: 200, body: { report: await readCurrentExecutionReport(scope, reportId) } };
      }
      return null;
    },
  };
}

const MOBILE_VARIANT_ORDER = [
  "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
] as const satisfies readonly ReferenceVariantKind[];

function currentAuthorizationContext(
  snapshot: ReferenceWorkflowSnapshot,
  scope: StudioScope,
) {
  const review = buildReferenceMobileReview(snapshot, scope);
  const report = exactVersion(
    snapshot.executionReports,
    review.bindings.executionReport.id,
    review.bindings.executionReport.version,
  );
  if (!report) throw new Error("reference-mobile-review-incomplete");
  const sample = exactVersion(
    snapshot.samples,
    report.bindings.sample.id,
    report.bindings.sample.version,
  );
  const mapping = exactVersion(
    snapshot.mappingPlans,
    report.bindings.mappingPlan.id,
    report.bindings.mappingPlan.version,
  );
  if (!sample || !mapping || sample.status !== "ready" || mapping.status !== "confirmed") {
    throw new Error("reference-mobile-review-invalidated");
  }
  const assetBindings = referenceWorkflowAssetBindings(sample, mapping);
  return {
    snapshot,
    review,
    assetBindings,
    identity: canonicalJson({
      report: review.bindings.executionReport,
      artifacts: review.artifacts.map((artifact) => ({
        kind: artifact.kind,
        draftId: artifact.draftId,
        draftVersion: artifact.draftVersion,
        outputDigest: artifact.outputDigest,
      })),
      assetBindings,
    }),
  };
}

function currentExecutionReportAuthorizationContext(
  snapshot: ReferenceWorkflowSnapshot,
  scope: StudioScope,
  reportId: string,
) {
  try {
    const current = currentAuthorizationContext(snapshot, scope);
    const binding = current.review.bindings.executionReport;
    if (binding.id !== reportId) {
      throw new Error("reference-execution-report-not-current");
    }
    const report = exactVersion(snapshot.executionReports, binding.id, binding.version);
    if (!report || report.status !== "reviewable" ||
        report.customerId !== scope.customerId || report.sessionId !== scope.sessionId) {
      throw new Error("reference-execution-report-not-current");
    }
    return { ...current, report };
  } catch {
    // This public read has one stable fail-closed result for every case where
    // the requested report is no longer the workflow's exact current report.
    throw new Error("reference-execution-report-not-current");
  }
}

function draftActionAssetBindings(
  snapshot: ReferenceWorkflowSnapshot,
  scope: StudioScope,
  identity: Readonly<{
    reportId: string;
    reportVersion: number;
    draftId: string;
    draftVersion: number;
    draftDigest: string;
  }>,
): ReferenceWorkflowAssetBinding[] {
  const report = exactVersion(snapshot.executionReports, identity.reportId, identity.reportVersion);
  const draft = exactVersion(snapshot.drafts, identity.draftId, identity.draftVersion);
  const artifact = report?.artifacts.find((candidate) =>
    candidate.draftId === identity.draftId && candidate.draftVersion === identity.draftVersion &&
    candidate.outputDigest === identity.draftDigest);
  if (!report || !draft || !artifact || draft.outputDigest !== identity.draftDigest ||
      report.customerId !== scope.customerId || report.sessionId !== scope.sessionId ||
      draft.customerId !== scope.customerId || draft.sessionId !== scope.sessionId) {
    throw new Error("reference-workflow-input-stale");
  }
  const sample = exactVersion(
    snapshot.samples,
    report.bindings.sample.id,
    report.bindings.sample.version,
  );
  const mapping = exactVersion(
    snapshot.mappingPlans,
    report.bindings.mappingPlan.id,
    report.bindings.mappingPlan.version,
  );
  if (!sample || !mapping || sample.customerId !== scope.customerId ||
      sample.sessionId !== scope.sessionId || mapping.customerId !== scope.customerId ||
      mapping.sessionId !== scope.sessionId || sample.status !== "ready" ||
      mapping.status !== "confirmed" || report.status !== "reviewable" ||
      draft.status !== "reviewable" ||
      canonicalJson(draft.bindings.mappingPlan) !== canonicalJson(report.bindings.mappingPlan)) {
    throw new Error("reference-workflow-input-stale");
  }
  return referenceWorkflowAssetBindings(sample, mapping);
}

function referenceWorkflowAssetBindings(
  sample: ReferenceWorkflowSnapshot["samples"][number],
  mapping: ReferenceWorkflowSnapshot["mappingPlans"][number],
): ReferenceWorkflowAssetBinding[] {
  const renderSources = new Map<string, ReferenceWorkflowAssetBinding>();
  for (const row of mapping.mappings) {
    if (!row.sourceAssetId) continue;
    if (!row.sourceDigest || !row.permission ||
        row.permission.view !== "allowed" || row.permission.use !== "allowed") {
      throw new Error("reference-workflow-asset-not-current");
    }
    const binding = {
      role: "render-source" as const,
      assetId: row.sourceAssetId,
      assetVersion: row.permission.version,
      sourceDigest: row.sourceDigest,
      permission: {
        version: row.permission.version,
        epoch: row.permission.epoch,
        view: row.permission.view,
        use: row.permission.use,
      },
    };
    const prior = renderSources.get(binding.assetId);
    if (prior && canonicalJson(prior) !== canonicalJson(binding)) {
      throw new Error("reference-workflow-asset-not-current");
    }
    renderSources.set(binding.assetId, binding);
  }
  return [{
    role: "sample-source",
    assetId: sample.sourceAssetId,
    // Active reference samples predate an explicit source-asset-version field;
    // imported studio assets are immutable and begin at version 1.
    assetVersion: 1,
    sourceDigest: sample.sourceDigest,
  }, ...renderSources.values()];
}

async function readVerifiedDraftBytes(
  draft: ReferenceWorkflowSnapshot["drafts"][number],
  draftOutputRoot: string,
): Promise<Buffer> {
  let canonicalRoot: string;
  let canonicalOutput: string;
  try {
    canonicalRoot = await realpath(resolve(draftOutputRoot));
    canonicalOutput = await realpath(resolve(draft.outputPath));
  } catch {
    throw new Error("reference-draft-media-unavailable");
  }
  const withinRoot = relative(canonicalRoot, canonicalOutput);
  if (!withinRoot || withinRoot === ".." || withinRoot.startsWith(`..${sep}`) || isAbsolute(withinRoot)) {
    throw new Error("reference-draft-output-binding-invalid");
  }
  const outputStat = await stat(canonicalOutput).catch(() => null);
  if (!outputStat?.isFile()) throw new Error("reference-draft-media-unavailable");
  const bytes = await readFile(canonicalOutput).catch(() => {
    throw new Error("reference-draft-media-unavailable");
  });
  if (createHash("sha256").update(bytes).digest("hex") !== draft.outputDigest) {
    throw new Error("reference-draft-output-digest-mismatch");
  }
  return bytes;
}

function buildReferenceMobileReview(
  snapshot: ReferenceWorkflowSnapshot,
  scope: StudioScope,
): ReferenceMobileReviewV1 {
  const scopedReports = latestPerIdentity(snapshot.executionReports.filter((report) =>
    report.customerId === scope.customerId && report.sessionId === scope.sessionId));
  const hasScopedWorkflow = snapshot.samples.some((sample) =>
    sample.customerId === scope.customerId && sample.sessionId === scope.sessionId);
  if (!hasScopedWorkflow && scopedReports.length === 0) {
    throw new Error("reference-mobile-review-not-found");
  }
  const reviewableReports = scopedReports.filter((report) => report.status === "reviewable");
  if (reviewableReports.length === 0) {
    if (scopedReports.some((report) => report.status === "invalidated") ||
        latestPerIdentity(snapshot.samples.filter((sample) =>
          sample.customerId === scope.customerId && sample.sessionId === scope.sessionId))
          .some((sample) => sample.status === "invalidated")) {
      throw new Error("reference-mobile-review-invalidated");
    }
    throw new Error("reference-mobile-review-incomplete");
  }
  const highestReportVersion = Math.max(...reviewableReports.map((report) => report.version));
  const currentReports = reviewableReports.filter((report) => report.version === highestReportVersion);
  if (currentReports.length > 1) throw new Error("reference-mobile-review-ambiguous");

  const report = currentReports[0]!;
  const binding = report.bindings;
  const sample = exactVersion(snapshot.samples, binding.sample.id, binding.sample.version);
  const rights = exactVersion(snapshot.rightsDeclarations, binding.rights.id, binding.rights.version);
  const analysis = exactVersion(snapshot.analyses, binding.analysis.id, binding.analysis.version);
  const selection = exactVersion(
    snapshot.styleSelections, binding.styleSelection.id, binding.styleSelection.version,
  );
  const currentMappings = latestPerIdentity(snapshot.mappingPlans).filter((candidate) =>
    candidate.customerId === scope.customerId && candidate.sessionId === scope.sessionId &&
    candidate.status === "confirmed" &&
    canonicalJson(candidate.bindings.sample) === canonicalJson(binding.sample) &&
    canonicalJson(candidate.bindings.rights) === canonicalJson(binding.rights) &&
    canonicalJson(candidate.bindings.analysis) === canonicalJson(binding.analysis) &&
    canonicalJson(candidate.bindings.styleSelection) === canonicalJson(binding.styleSelection));
  if (currentMappings.length > 1) throw new Error("reference-mobile-review-ambiguous");
  if (currentMappings.length === 0) throw new Error("reference-mobile-review-incomplete");
  const mapping = exactVersion(snapshot.mappingPlans, binding.mappingPlan.id, binding.mappingPlan.version);
  if (!sample || !rights || !analysis || !selection || !mapping) {
    throw new Error("reference-mobile-review-incomplete");
  }
  if ([sample, rights, analysis, selection, mapping].some((value) =>
    value.customerId !== scope.customerId || value.sessionId !== scope.sessionId)) {
    throw new Error("reference-mobile-review-not-found");
  }
  if (sample.status === "invalidated" || selection.status === "invalidated" ||
      mapping.status === "invalidated") {
    throw new Error("reference-mobile-review-invalidated");
  }
  if (sample.status !== "ready" || selection.status !== "confirmed" || mapping.status !== "confirmed" ||
      canonicalJson(sampleBindingForMobile(sample)) !== canonicalJson(binding.sample) ||
      canonicalJson(rightsBindingForMobile(rights)) !== canonicalJson(binding.rights) ||
      canonicalJson(scopedContentBinding(analysis)) !== canonicalJson(binding.analysis) ||
      canonicalJson(styleSelectionBindingForMobile(selection)) !== canonicalJson(binding.styleSelection) ||
      canonicalJson(mappingBindingForMobile(mapping)) !== canonicalJson(binding.mappingPlan)) {
    throw new Error("reference-mobile-review-incomplete");
  }

  const reportArtifacts = new Map(report.artifacts.map((artifact) => [artifact.kind, artifact]));
  if (report.artifacts.length !== MOBILE_VARIANT_ORDER.length ||
      reportArtifacts.size !== MOBILE_VARIANT_ORDER.length ||
      MOBILE_VARIANT_ORDER.some((kind) => !reportArtifacts.has(kind))) {
    throw new Error("reference-mobile-review-incomplete");
  }
  const invalidatedApprovalIds = new Set(snapshot.approvals
    .filter((approval) => approval.status === "invalidated")
    .map((approval) => approval.id));
  const allowLegacyApproval = !snapshot.executionReports.some((candidate) =>
    candidate.customerId === scope.customerId && candidate.sessionId === scope.sessionId &&
    candidate.bindings.mappingPlan.id === report.bindings.mappingPlan.id &&
    candidate.bindings.mappingPlan.version === report.bindings.mappingPlan.version &&
    (candidate.id !== report.id || candidate.version !== report.version));
  const artifacts = MOBILE_VARIANT_ORDER.map((kind) => {
    const artifact = reportArtifacts.get(kind)!;
    const plan = exactVersion(snapshot.variantPlans, artifact.variantPlanId, artifact.variantPlanVersion);
    const draft = exactVersion(snapshot.drafts, artifact.draftId, artifact.draftVersion);
    if (!plan || !draft) throw new Error("reference-mobile-review-incomplete");
    if (plan.customerId !== scope.customerId || plan.sessionId !== scope.sessionId ||
        draft.customerId !== scope.customerId || draft.sessionId !== scope.sessionId) {
      throw new Error("reference-mobile-review-not-found");
    }
    if (plan.status === "invalidated" || draft.status === "invalidated") {
      throw new Error("reference-mobile-review-invalidated");
    }
    if (plan.kind !== kind || draft.status !== "reviewable" ||
        draft.outputDigest !== artifact.outputDigest ||
        draft.bindings.variantPlan.id !== plan.id ||
        draft.bindings.variantPlan.version !== plan.version ||
        canonicalJson(plan.bindings) !== canonicalJson(binding) ||
        canonicalJson(draft.bindings.mappingPlan) !== canonicalJson(binding.mappingPlan)) {
      throw new Error("reference-mobile-review-incomplete");
    }
    const approved = snapshot.approvals.some((approval) => {
      if (approval.status !== "active" || invalidatedApprovalIds.has(approval.id) ||
          approval.variant !== kind) return false;
      const parsedReferenceApproval = ReferenceDraftApprovalSchema.safeParse(approval);
      if (parsedReferenceApproval.success) {
        const referenceApproval = parsedReferenceApproval.data;
        return referenceApproval.customerId === scope.customerId &&
          referenceApproval.sessionId === scope.sessionId &&
          referenceApproval.publishClaimed === false &&
          referenceApproval.fileDigest === draft.outputDigest &&
          referenceApproval.bindings.executionReport.id === report.id &&
          referenceApproval.bindings.executionReport.version === report.version &&
          referenceApproval.bindings.draft.id === draft.id &&
          referenceApproval.bindings.draft.version === draft.version &&
          referenceApproval.bindings.draft.outputDigest === draft.outputDigest &&
          referenceApproval.bindings.mappingPlan.id === mapping.id &&
          referenceApproval.bindings.mappingPlan.version === mapping.version &&
          referenceApproval.bindings.mappingPlan.contentDigest === mapping.contentDigest &&
          referenceApproval.bindings.mappingPlan.permissionSetDigest === mapping.permissionSetDigest;
      }
      return allowLegacyApproval && approval.storyId === sample.id && approval.revisionId === mapping.id &&
        approval.revision === mapping.version && approval.artifactId === draft.id &&
        approval.artifactDigest === draft.outputDigest &&
        approval.renderInputDigest === mapping.contentDigest;
    });
    return {
      ...artifact,
      mediaRoute: { kind: "reference-draft-media" as const, version: 1 as const, draftId: draft.id },
      status: approved ? "approved" as const : "reviewable" as const,
    };
  });
  const approvedArtifactKinds = artifacts
    .filter((artifact) => artifact.status === "approved")
    .map((artifact) => artifact.kind);
  const approved = approvedArtifactKinds.length === MOBILE_VARIANT_ORDER.length;
  const stableIdentity = {
    report: { id: report.id, version: report.version },
    artifacts: artifacts.map((artifact) => ({
      kind: artifact.kind, draftId: artifact.draftId, draftVersion: artifact.draftVersion,
      outputDigest: artifact.outputDigest, status: artifact.status,
    })),
  };
  const parsed = ReferenceMobileReviewV1Schema.safeParse({
    id: `reference_mobile_review_${createHash("sha256").update(canonicalJson(stableIdentity)).digest("hex").slice(0, 32)}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    bindings: {
      ...binding,
      executionReport: {
        id: report.id, version: report.version, customerId: report.customerId, sessionId: report.sessionId,
      },
    },
    styleSummary: {
      status: analysis.status,
      ruleKinds: ["rhythm", "transition", "narrative", "audio"].filter((kind) =>
        analysis.rules.some((rule) => rule.kind === kind)),
      capability: analysis.capability.mode === "offline-fallback" ? {
        mode: "offline-fallback",
        label: "离线/模拟分析",
        version: 1,
        provider: null,
      } : {
        mode: "provider",
        label: "移动审片：已配置模型服务 A/B，节奏/转场",
        version: 1,
        provider: "configured-provider",
      },
      warningCodes: [...analysis.warnings].sort(),
    },
    artifacts,
    executionReport: {
      id: report.id,
      version: report.version,
      artifacts: MOBILE_VARIANT_ORDER.map((kind) => reportArtifacts.get(kind)!),
      rhythm: stableExecutionSection(report.rhythm),
      transitions: stableExecutionSection(report.transitions),
      narrative: stableExecutionSection(report.narrative),
      audio: stableExecutionSection(report.audio),
      mappingCounts: report.mappingCounts,
      platformChanges: ["xiaohongshu", "douyin", "wechat-channels"].map((kind) => {
        const change = report.platformChanges.find((candidate) => candidate.kind === kind);
        if (!change) throw new Error("reference-mobile-review-incomplete");
        return { ...change, changeCodes: [...change.changeCodes].sort() };
      }),
    },
    approval: { status: approved ? "approved" : "pending", approvedArtifactKinds },
    status: approved ? "approved" : "reviewable",
    publishClaimed: false,
    createdAt: report.createdAt,
  });
  if (!parsed.success) throw new Error("reference-mobile-review-incomplete");
  return parsed.data;
}

function stableExecutionSection<T extends { evidenceRefs: readonly string[] }>(section: T): T {
  return { ...section, evidenceRefs: [...section.evidenceRefs].sort() };
}

function exactVersion<T extends { id: string; version: number }>(
  values: readonly T[],
  id: string,
  version: number,
): T | undefined {
  const matches = values.filter((value) => value.id === id && value.version === version);
  if (matches.length > 1) throw new Error("reference-mobile-review-ambiguous");
  return matches[0];
}

function latestPerIdentity<T extends { id: string; version: number }>(values: readonly T[]): T[] {
  const latest = new Map<string, T>();
  for (const value of values) {
    const current = latest.get(value.id);
    if (!current || value.version > current.version) latest.set(value.id, value);
  }
  return [...latest.values()];
}

function sampleBindingForMobile(sample: ReferenceWorkflowSnapshot["samples"][number]) {
  return {
    id: sample.id, version: sample.version, customerId: sample.customerId, sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId, sourceDigest: sample.sourceDigest, sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode, candidateScope: sample.candidateScope,
  };
}

function rightsBindingForMobile(rights: ReferenceWorkflowSnapshot["rightsDeclarations"][number]) {
  return {
    id: rights.id, version: rights.version, customerId: rights.customerId, sessionId: rights.sessionId,
    referenceSampleId: rights.sample.id, referenceSampleVersion: rights.sample.version,
    referenceSampleDigest: rights.sample.sourceDigest, sourceKind: rights.sourceKind,
    reusableElements: rights.reusableElements, contentDigest: rights.contentDigest,
  };
}

function scopedContentBinding(value: { id: string; version: number; customerId: string; sessionId: string; contentDigest: string }) {
  return {
    id: value.id, version: value.version, customerId: value.customerId,
    sessionId: value.sessionId, contentDigest: value.contentDigest,
  };
}

function styleSelectionBindingForMobile(selection: ReferenceWorkflowSnapshot["styleSelections"][number]) {
  return {
    ...scopedContentBinding(selection),
    status: selection.status,
    confirmation: {
      receiptId: selection.confirmationReceiptId,
      actorId: selection.actorId,
      confirmedAt: selection.confirmedAt,
    },
  };
}

function mappingBindingForMobile(mapping: ReferenceWorkflowSnapshot["mappingPlans"][number]) {
  const permissions = new Map(mapping.mappings
    .flatMap((item) => item.permission ? [[item.permission.id, {
      id: item.permission.id, version: item.permission.version, epoch: item.permission.epoch,
      customerId: item.permission.customerId, sessionId: item.permission.sessionId,
      sourceAssetId: item.permission.sourceAssetId, sourceDigest: item.permission.sourceDigest,
    }] as const] : []));
  return {
    ...scopedContentBinding(mapping),
    permissionSetDigest: mapping.permissionSetDigest,
    permissions: [...permissions.values()].sort((left, right) => left.id.localeCompare(right.id)),
    status: mapping.status,
    confirmation: {
      receiptId: mapping.confirmationReceiptId,
      actorId: mapping.confirmedByActorId,
      confirmedAt: mapping.confirmedAt,
    },
  };
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, child]) => [key, canonicalize(child)]));
}

function findScopedDraft(
  snapshot: ReferenceWorkflowSnapshot,
  draftId: string,
  scope: StudioScope,
): ReferenceWorkflowSnapshot["drafts"][number] | undefined {
  return snapshot.drafts
    .filter((candidate) =>
      candidate.id === draftId
      && candidate.customerId === scope.customerId
      && candidate.sessionId === scope.sessionId)
    .sort((left, right) => right.version - left.version)[0];
}

function withCurrentStoreSnapshot<T>(
  store: StudioReferenceStore,
  work: (snapshot: ReferenceWorkflowSnapshot) => Promise<T> | T,
): Promise<T> {
  return store.withCurrentSnapshot
    ? store.withCurrentSnapshot(work)
    : store.snapshot().then(work);
}

function isDraftInvalidationOf(
  candidate: ReferenceWorkflowSnapshot["drafts"][number],
  draft: ReferenceWorkflowSnapshot["drafts"][number],
  scope: StudioScope,
): boolean {
  const prefix = `${draft.id}-invalidated-`;
  return candidate.customerId === scope.customerId
    && candidate.sessionId === scope.sessionId
    && candidate.status === "invalidated"
    && candidate.outputPath === draft.outputPath
    && candidate.outputDigest === draft.outputDigest
    && candidate.id.startsWith(prefix)
    && /^[a-f0-9]{12}$/.test(candidate.id.slice(prefix.length));
}

type StagedReferenceUpload = Readonly<{
  directory: string;
  path: string;
  fileName: string;
  sourceKind: "self-owned" | "external-style-only";
  operationKey: string;
}>;

class ReferenceImportFailure extends Error {
  constructor(code: string, readonly operationKey?: string) {
    super(code);
  }
}

function withOperationKey(error: Error, candidate: string | undefined): Error {
  const operationKey = typeof candidate === "string" && SAFE_OPERATION_KEY.test(candidate.trim())
    ? candidate.trim()
    : undefined;
  return new ReferenceImportFailure(stableErrorCode(error, "reference-import-failed"), operationKey);
}

function operationKeyFromError(error: unknown): string | null {
  return error instanceof ReferenceImportFailure ? (error.operationKey ?? null) : null;
}

function stableErrorCode(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  return /^[a-z0-9-]+$/.test(message) ? message : fallback;
}

async function appendImportFailureAudit(
  auditPath: string,
  event: Readonly<{
    id: string;
    createdAt: string;
    actorId: string;
    customerId: string;
    sessionId: string;
    operationKey: string | null;
    errorCode: string;
  }>,
): Promise<void> {
  const previous = importAuditQueues.get(auditPath) ?? Promise.resolve();
  const write = previous.catch(() => undefined).then(async () => {
    await mkdir(dirname(auditPath), { recursive: true, mode: 0o700 });
    await appendFile(auditPath, `${JSON.stringify(event)}\n`, { encoding: "utf8", mode: 0o600 });
  });
  importAuditQueues.set(auditPath, write);
  try {
    await write;
  } catch {
    throw new Error("reference-import-audit-write-failed");
  } finally {
    if (importAuditQueues.get(auditPath) === write) importAuditQueues.delete(auditPath);
  }
}

async function stageMultipartUpload(
  request: IncomingMessage,
  stagingRoot: string,
  maxUploadBytes: number,
  stagingHooks?: StudioReferenceStagingHooks,
): Promise<StagedReferenceUpload> {
  requireMultipartContentType(request.headers);
  assertRequestActive(request);
  let directory: string | undefined;
  try {
    await mkdir(stagingRoot, { recursive: true, mode: 0o700 });
    assertRequestActive(request);
    directory = await mkdtemp(join(stagingRoot, "upload-"));
    await stagingHooks?.afterDirectoryCreate?.();
    assertRequestActive(request);
  } catch (error) {
    if (directory) {
      try {
        await removeStagingDirectory(directory, stagingHooks);
      } catch (cleanupError) {
        throw cleanupError;
      }
    }
    if (stableErrorCode(error, "") === "reference-import-request-aborted") throw error;
    throw new Error("reference-import-staging-io-failed");
  }
  if (!directory) throw new Error("reference-import-staging-io-failed");
  const path = join(directory, "reference-upload.part");
  try {
    const parsed = await parseMultipart(request, path, maxUploadBytes);
    return { directory, path, ...parsed };
  } catch (error) {
    try {
      await removeStagingDirectory(directory, stagingHooks);
    } catch (cleanupError) {
      throw new ReferenceImportFailure(
        "reference-import-staging-cleanup-failed",
        operationKeyFromError(error) ?? undefined,
      );
    }
    throw error;
  }
}

function assertRequestActive(request: IncomingMessage): void {
  if (request.aborted || request.destroyed) {
    throw new Error("reference-import-request-aborted");
  }
}

async function removeStagingDirectory(
  directory: string,
  stagingHooks?: StudioReferenceStagingHooks,
): Promise<void> {
  try {
    if (stagingHooks?.removeDirectory) await stagingHooks.removeDirectory(directory);
    else await rm(directory, { recursive: true, force: true });
  } catch {
    throw new Error("reference-import-staging-cleanup-failed");
  }
}

async function parseMultipart(
  request: IncomingMessage,
  path: string,
  maxUploadBytes: number,
): Promise<Omit<StagedReferenceUpload, "directory" | "path">> {
  let parser: ReturnType<typeof Busboy>;
  try {
    parser = Busboy({
      headers: request.headers,
      limits: {
        fieldSize: MAX_FIELD_BYTES,
        fields: 3,
        fileSize: maxUploadBytes + 1,
        files: 1,
        parts: 4,
      },
    });
  } catch {
    throw new Error("reference-import-multipart-invalid");
  }

  const fields = new Map<string, string>();
  let fileCount = 0;
  let fileName = "";
  let write: Promise<Error | null> | null = null;
  const activeStreams: {
    file?: { destroy(error?: Error): void };
    output?: ReturnType<typeof createWriteStream>;
  } = {};
  let failure: Error | null = null;
  const fail = (code: string) => { failure ??= new Error(code); };

  parser.on("field", (name, value, info) => {
    if (name !== "sourceKind" && name !== "operationKey") {
      fail("reference-import-field-not-allowed");
      return;
    }
    if (info.nameTruncated || info.valueTruncated) {
      fail("reference-import-field-invalid");
      return;
    }
    if (fields.has(name)) {
      fail("reference-import-field-duplicate");
      return;
    }
    fields.set(name, value);
  });
  parser.on("file", (name, stream, info) => {
    fileCount += 1;
    if (name !== "file") {
      fail("reference-import-field-not-allowed");
      stream.resume();
      return;
    }
    if (fileCount !== 1) {
      fail("reference-import-file-count-invalid");
      stream.resume();
      return;
    }
    try {
      fileName = requireUploadFileName(info.filename);
    } catch (error) {
      failure ??= error instanceof Error ? error : new Error("reference-import-file-name-invalid");
    }
    activeStreams.file = stream;
    activeStreams.output = createWriteStream(path, { flags: "wx", mode: 0o600 });
    write = pipeline(stream, activeStreams.output)
      .then(() => null, (error: unknown) =>
        error instanceof Error ? error : new Error("reference-import-staging-io-failed"));
  });
  parser.once("filesLimit", () => fail("reference-import-file-count-invalid"));
  parser.once("fieldsLimit", () => fail("reference-import-field-count-invalid"));
  parser.once("partsLimit", () => fail("reference-import-part-count-invalid"));

  let parseError: Error | null = null;
  try {
    await new Promise<void>((resolve, reject) => {
      parser.once("error", () => reject(new Error("reference-import-multipart-invalid")));
      parser.once("close", resolve);
      request.once("aborted", () => reject(new Error("reference-import-request-aborted")));
      if (request.aborted || request.destroyed) {
        reject(new Error("reference-import-request-aborted"));
        return;
      }
      request.pipe(parser);
    });
  } catch (error) {
    parseError = withOperationKey(
      error instanceof Error ? error : new Error("reference-import-multipart-invalid"),
      fields.get("operationKey"),
    );
    request.unpipe(parser);
    const destroyError = new Error(parseError.message);
    activeStreams.file?.destroy(destroyError);
    activeStreams.output?.destroy(destroyError);
    parser.destroy(destroyError);
  }
  if (write) {
    const writeError = await write;
    if (writeError && !parseError) {
      throw withOperationKey(new Error("reference-import-staging-io-failed"), fields.get("operationKey"));
    }
  }
  if (parseError) throw parseError;
  if (failure) throw withOperationKey(failure, fields.get("operationKey"));
  if (fileCount !== 1 || !write) {
    throw withOperationKey(new Error("reference-import-file-required"), fields.get("operationKey"));
  }
  const fileStat = await stat(path).catch(() => {
    throw new Error("reference-import-staging-io-failed");
  });
  if (!fileStat.isFile() || fileStat.size < 1) {
    throw withOperationKey(new Error("reference-import-file-empty"), fields.get("operationKey"));
  }
  if (fileStat.size > maxUploadBytes) {
    throw withOperationKey(new Error("reference-import-too-large"), fields.get("operationKey"));
  }
  const operationKey = requireOperationKey(fields.get("operationKey"));
  const sourceKind = requireSourceKind(fields.get("sourceKind"));
  return { fileName, operationKey, sourceKind };
}

async function verifyReferenceMedia(
  path: string,
  ffprobePath: string,
  ffmpegPath: string,
  maxDurationMs: number,
  processTimeoutMs: number,
  maxProcessOutputBytes: number,
): Promise<void> {
  let probe: Record<string, unknown>;
  try {
    const result = await runBoundedProcess(ffprobePath, [
      "-v", "error",
      "-show_entries", "format=format_name,duration:stream=codec_type",
      "-of", "json",
      path,
    ], processTimeoutMs, maxProcessOutputBytes);
    probe = JSON.parse(result.stdout) as Record<string, unknown>;
  } catch (error) {
    const infrastructure = mediaInfrastructureError(error);
    if (infrastructure) throw infrastructure;
    throw new Error("reference-import-media-invalid");
  }
  const format = isRecord(probe.format) ? probe.format : null;
  const streams = Array.isArray(probe.streams) ? probe.streams : [];
  const formatNames = typeof format?.format_name === "string"
    ? new Set(format.format_name.split(","))
    : new Set<string>();
  const durationSeconds = Number(format?.duration);
  if ((!formatNames.has("mov") && !formatNames.has("mp4")) ||
      !Number.isFinite(durationSeconds) || durationSeconds <= 0 ||
      durationSeconds * 1_000 > maxDurationMs ||
      !streams.some((stream) => isRecord(stream) && stream.codec_type === "video")) {
    throw new Error(durationSeconds * 1_000 > maxDurationMs
      ? "reference-import-duration-too-long"
      : "reference-import-media-invalid");
  }
  try {
    await runBoundedProcess(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-xerror",
      "-i", path,
      "-map", "0:v", "-map", "0:a?",
      "-f", "null", "-",
    ], processTimeoutMs, maxProcessOutputBytes);
  } catch (error) {
    const infrastructure = mediaInfrastructureError(error);
    if (infrastructure) throw infrastructure;
    throw new Error("reference-import-media-invalid");
  }
}

async function runBoundedProcess(
  command: string,
  args: readonly string[],
  timeoutMs: number,
  maxOutputBytes: number,
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    let settled = false;
    let pendingError: Error | null = null;
    let timer: ReturnType<typeof setTimeout>;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error ? reject(error) : resolve({
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    };
    const terminate = (error: Error) => {
      if (pendingError || settled) return;
      pendingError = error;
      child.kill("SIGKILL");
    };
    const capture = (target: Buffer[]) => (chunk: Buffer | string) => {
      if (pendingError || settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      outputBytes += bytes.byteLength;
      if (outputBytes > maxOutputBytes) {
        terminate(new Error("reference-import-media-output-too-large"));
        return;
      }
      target.push(bytes);
    };
    child.stdout.on("data", capture(stdout));
    child.stderr.on("data", capture(stderr));
    child.once("error", (error: NodeJS.ErrnoException) => finish(new Error(
      error.code === "ENOENT"
        ? "reference-import-media-tool-unavailable"
        : "reference-import-media-process-failed",
    )));
    child.once("close", (status, signal) => pendingError
      ? finish(pendingError)
      : signal !== null
        ? finish(new Error("reference-import-media-tool-crashed"))
      : status === 0
        ? finish()
        : finish(new Error("reference-import-media-rejected")));
    timer = setTimeout(() => {
      terminate(new Error("reference-import-media-tool-timeout"));
    }, timeoutMs);
    timer.unref();
  });
}

function mediaInfrastructureError(error: unknown): Error | null {
  const code = stableErrorCode(error, "reference-import-media-invalid");
  return new Set([
    "reference-import-media-tool-unavailable",
    "reference-import-media-tool-timeout",
    "reference-import-media-tool-crashed",
    "reference-import-media-output-too-large",
    "reference-import-media-process-failed",
  ]).has(code) ? new Error(code) : null;
}

function requireMultipartContentType(headers: IncomingHttpHeaders): void {
  const contentType = headers["content-type"] ?? "";
  if (!/^multipart\/form-data\s*;/i.test(contentType)) {
    throw new Error("reference-import-multipart-content-type-required");
  }
}

function requireUploadFileName(value: string): string {
  const normalized = basename(value.normalize("NFC")).trim();
  if (!normalized || normalized.length > 255 || normalized.includes("\0") ||
      !ALLOWED_EXTENSIONS.has(extname(normalized).toLowerCase())) {
    throw new Error("reference-import-file-name-invalid");
  }
  return normalized;
}

function requireOperationKey(value: unknown): string {
  const normalized = requireString(value, "reference-operation-key-required");
  if (!SAFE_OPERATION_KEY.test(normalized)) throw new Error("reference-operation-key-invalid");
  return normalized;
}

function requirePositiveVersion(value: unknown, code: string): number {
  if (!Number.isInteger(value) || (value as number) <= 0) throw new Error(code);
  return value as number;
}

function requireDigest(value: unknown, code: string): string {
  const digest = requireString(value, code);
  if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error(code);
  return digest;
}

function requireString(value: unknown, code: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(code);
  return value.trim();
}

function requireStringArray(value: unknown, code: string): string[] {
  if (!Array.isArray(value) || value.length === 0 ||
      value.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error(code);
  }
  const normalized = value.map((item) => (item as string).trim());
  if (new Set(normalized).size !== normalized.length) {
    throw new Error("duplicate-selected-reference-rule-id");
  }
  return normalized;
}

function requireReusableElements(value: unknown) {
  if (!Array.isArray(value)) throw new Error("reference-reusable-elements-invalid");
  const parsed = value.map((item) => {
    const result = ReferenceReusableElementSchema.safeParse(item);
    if (!result.success) throw new Error("reference-reusable-element-invalid");
    return result.data;
  });
  if (new Set(parsed).size !== parsed.length) throw new Error("duplicate-reference-reusable-element");
  return parsed;
}

export function assertExactBodyFields(body: Record<string, unknown>, allowed: readonly string[]): void {
  const allowedFields = new Set(allowed);
  if (Object.keys(body).some((key) => !allowedFields.has(key))) {
    throw new Error("studio-customer-scope-forbidden");
  }
}

function requireSourceKind(value: unknown): "self-owned" | "external-style-only" {
  const result = ReferenceSourceKindSchema.safeParse(value);
  if (!result.success) throw new Error("reference-source-kind-invalid");
  return result.data;
}

function decodePathSegment(value: string, code = "reference-execution-report-id-required"): string {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new Error(code);
  }
}

function latestReportById(
  reports: readonly ReferenceExecutionReport[],
  reportId: string,
): ReferenceExecutionReport | undefined {
  return reports
    .filter((report) => report.id === reportId)
    .sort((left, right) => right.version - left.version)[0];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
