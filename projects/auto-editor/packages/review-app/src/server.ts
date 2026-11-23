import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, stat } from "node:fs/promises";
import { extname, join } from "node:path";
import ffmpegPath from "ffmpeg-static";

import {
  type CreativeSession,
  type CreativeProcessingTask,
  type CreativeVersionKey,
  type DeviceRole,
  type MaterialSuggestionSet,
  type MaterialUseDecision,
} from "@wearable/contracts";
import {
  approveVariant,
  answerCreativeIntentRecoverably,
  confirmMaterialSelection,
  confirmPostShootMaterialSelection,
  currentMaterialViewingDecision,
  confirmScriptPackage,
  createShootingPackage,
  createExportPackage,
  deliverExportPackage,
  commitPromptRevision,
  decideMaterialViewing,
  decidePostShootMaterialViewing,
  dispatchShootingPackage,
  generateScriptPackage,
  acceptCreativeCapture,
  LocalCreativeOriginalStore,
  preflightShootingPackageDispatch,
  recordCreativeSessionAction,
  renderRevision,
  verifyRenderedRevision,
  processCreativeEditorial,
  resolveCreativeSteps,
  resolveHistoricalMaterialAuthorizationFromRecords,
  reviseScriptPackage,
  reviseWithPrompt,
  resumeCreativePrivacy,
  startCreativeSession,
  suggestHistoricalMaterials,
  suggestPostShootHistoricalMaterials,
  settleCreativeCapture,
  validateHistoricalMaterialAuthorization,
  type ContentLibrary,
  type CreativeMaterialState,
  type CreativeScriptAgent,
  type CreativeEditorialDependencies,
  type CreativePrivacyDevice,
  type DeviceCapability,
  type IntentInterpreter,
  type LocalDispatchRoster,
  type MaterialMatcher,
  type PhonePublisherAdapter,
  type PromptEditInterpreter,
  type ShotPlanningAgent,
} from "@wearable/mobile-agent";

export type CreativeDeviceAdapters = Readonly<{
  deviceCapabilities: readonly DeviceCapability[];
  privacyDevices: readonly CreativePrivacyDevice[];
  createRoster(version: CreativeVersionKey): LocalDispatchRoster;
}>;

export type CreativeReviewDependencies = Readonly<{
  intentInterpreter: IntentInterpreter;
  materialMatcher: MaterialMatcher;
  scriptAgent: CreativeScriptAgent;
  shotPlanningAgent: ShotPlanningAgent;
  editorial: Pick<CreativeEditorialDependencies,
    "segmentAnalyzer" | "tagAnalyzer" | "storyAnalyzer" | "editAnalyzer">;
  creativeDeviceAdapters: CreativeDeviceAdapters;
}>;

export type ReviewAppDependencies = Readonly<{
  library: ContentLibrary;
  interpreter: PromptEditInterpreter;
  outputDir?: string;
  renderer?: typeof renderRevision;
  designerId?: string;
  reviewToken?: string;
  customerCredentials?: Readonly<Record<string, string>>;
  phonePublisher?: PhonePublisherAdapter;
  creative?: CreativeReviewDependencies;
}>;

export type ReviewServerOptions = Readonly<{
  creativeOperationTtlMs?: number;
  creativeOperationMaxEntries?: number;
  now?: () => number;
}>;

type SecuredDependencies = ReviewAppDependencies & {
  designerId: string;
  reviewToken: string;
  customerCredentials: Readonly<Record<string, string>>;
  clock: () => string;
  creativeHandoff: (CreativeEditorialDependencies & {
    originals: LocalCreativeOriginalStore;
  }) | null;
};

const MAX_BODY_BYTES = 1024 * 1024;
const a0VideoFixtures = new Map<string, Promise<Buffer>>();
const DEFAULT_CREATIVE_OPERATION_TTL_MS = 10 * 60 * 1000;
const DEFAULT_CREATIVE_OPERATION_MAX_ENTRIES = 256;
const RETRYABLE_DOMAIN_ERROR_CODES = new Set([
  "creative-operation-registry-capacity",
  "creative-material-matcher-retryable",
  "creative-material-catalog-race-retryable",
  "creative-intent-interpreter-retryable",
  "creative-privacy-compensation-failed",
  "stale-content-store",
  "content-store-lock-timeout",
]);

type OperationResponse = Readonly<{
  status: number;
  body: unknown;
}>;

type CreativeOperationLease = Readonly<{
  response: OperationResponse;
  release(): void;
}>;

type CreativeOperationEntry = {
  authScope: string;
  clientInstanceId: string;
  operationKey: string;
  request: CreativeOperationRequest;
  fingerprint: string;
  promise: Promise<OperationResponse>;
  completedAt: number | null;
  acknowledgedAt: number | null;
  retryable: boolean;
  waiters: number;
};

type CreativeOperationRequest = Readonly<{
  method: string;
  path: string;
  body: Record<string, unknown>;
}>;

class CreativeClientRegistry {
  readonly #clients = new Map<string, { authScope: string; claim: string }>();

  open(
    authScope: string,
    requested: string | string[] | undefined,
    requestedClaim: string | string[] | undefined,
  ): { clientInstanceId: string; clientClaim: string } {
    if (requested !== undefined) {
      const clientInstanceId = requireOpaqueIdentifier(
        requested,
        "creative-client-instance-required",
        "creative-client-instance-invalid",
      );
      const clientClaim = requireOpaqueIdentifier(
        requestedClaim,
        "creative-client-claim-required",
        "creative-client-claim-invalid",
      );
      const current = this.#clients.get(clientInstanceId);
      if (!current) throw new Error("creative-client-recovery-required");
      if (current.authScope !== authScope) throw new Error("creative-client-instance-invalid");
      if (current.claim !== clientClaim) throw new Error("creative-client-claim-invalid");
      const rotatedClaim = `creative_claim_${randomUUID()}`;
      this.#clients.set(clientInstanceId, { authScope, claim: rotatedClaim });
      return { clientInstanceId, clientClaim: rotatedClaim };
    }
    if (requestedClaim !== undefined) throw new Error("creative-client-instance-required");
    const clientInstanceId = `creative_client_${randomUUID()}`;
    const clientClaim = `creative_claim_${randomUUID()}`;
    this.#clients.set(clientInstanceId, { authScope, claim: clientClaim });
    return { clientInstanceId, clientClaim };
  }

  require(
    authScope: string,
    value: string | string[] | undefined,
    claimValue: string | string[] | undefined,
  ): string {
    const clientInstanceId = requireOpaqueIdentifier(
      value,
      "creative-client-instance-required",
      "creative-client-instance-invalid",
    );
    const clientClaim = requireOpaqueIdentifier(
      claimValue,
      "creative-client-claim-required",
      "creative-client-claim-invalid",
    );
    const current = this.#clients.get(clientInstanceId);
    if (!current || current.authScope !== authScope) {
      throw new Error("creative-client-instance-invalid");
    }
    if (current.claim !== clientClaim) throw new Error("creative-client-claim-invalid");
    return clientInstanceId;
  }
}

export class CreativeOperationRegistry {
  readonly #entries = new Map<string, CreativeOperationEntry>();
  readonly #ttlMs: number;
  readonly #maxEntries: number;
  readonly #now: () => number;

  constructor(options: ReviewServerOptions) {
    this.#ttlMs = Math.max(1, options.creativeOperationTtlMs ?? DEFAULT_CREATIVE_OPERATION_TTL_MS);
    this.#maxEntries = Math.max(
      1,
      options.creativeOperationMaxEntries ?? DEFAULT_CREATIVE_OPERATION_MAX_ENTRIES,
    );
    this.#now = options.now ?? Date.now;
  }

  run(
    authScope: string,
    clientInstanceId: string,
    operationKey: string,
    request: CreativeOperationRequest,
    execute: () => Promise<OperationResponse>,
  ): Promise<CreativeOperationLease> {
    this.#pruneExpired();
    const registryKey = this.#registryKey(authScope, clientInstanceId, operationKey);
    const fingerprint = canonicalJson({ authScope, clientInstanceId, request });
    const existing = this.#entries.get(registryKey);
    if (existing) {
      if (existing.fingerprint !== fingerprint) {
        return Promise.resolve({
          response: {
            status: 409,
            body: { error: "creative-operation-key-conflict" },
          },
          release() {},
        });
      }
      if (existing.completedAt !== null && existing.retryable && existing.waiters === 0) {
        this.#entries.delete(registryKey);
      } else {
        return this.#waitFor(registryKey, existing);
      }
    }
    if (!this.#makeRoom()) {
      return Promise.resolve({
        response: {
          status: 503,
          body: { error: "creative-operation-registry-capacity" },
        },
        release() {},
      });
    }

    const entry: CreativeOperationEntry = {
      authScope,
      clientInstanceId,
      operationKey,
      request: structuredClone(request),
      fingerprint,
      completedAt: null,
      acknowledgedAt: null,
      retryable: false,
      waiters: 0,
      promise: Promise.resolve({ status: 500, body: { error: "internal-error" } }),
    };
    entry.promise = Promise.resolve()
      .then(execute)
      .then((result) => {
        entry.completedAt = this.#now();
        entry.retryable = result.status >= 500;
        return result;
      });
    this.#entries.set(registryKey, entry);
    return this.#waitFor(registryKey, entry);
  }

  inspect(
    authScope: string,
    clientInstanceId: string,
    operationKey: string,
  ): OperationResponse {
    this.#pruneExpired();
    const entry = this.#entries.get(this.#registryKey(authScope, clientInstanceId, operationKey));
    if (!entry) return receiptError(404, "creative-operation-receipt-not-found");
    const status = entry.completedAt === null
      ? "inflight"
      : entry.retryable
        ? "retryable"
        : entry.acknowledgedAt === null
          ? "completed"
          : "acknowledged";
    const receipt = {
      operationKey: entry.operationKey,
      clientInstanceId: entry.clientInstanceId,
      request: structuredClone(entry.request),
      status,
      ...(entry.completedAt === null ? {} : { completedAt: entry.completedAt }),
    };
    return { status: 200, body: { receipt } };
  }

  async inspectResolved(
    authScope: string,
    clientInstanceId: string,
    operationKey: string,
  ): Promise<OperationResponse> {
    const inspected = this.inspect(authScope, clientInstanceId, operationKey);
    if (inspected.status !== 200) return inspected;
    const entry = this.#entries.get(this.#registryKey(authScope, clientInstanceId, operationKey));
    if (!entry || entry.completedAt === null) return inspected;
    const response = await entry.promise;
    return {
      status: 200,
      body: {
        receipt: {
          ...(inspected.body as { receipt: Record<string, unknown> }).receipt,
          response: structuredClone(response),
        },
      },
    };
  }

  acknowledge(
    authScope: string,
    clientInstanceId: string,
    operationKey: string,
  ): OperationResponse {
    this.#pruneExpired();
    const entry = this.#entries.get(this.#registryKey(authScope, clientInstanceId, operationKey));
    if (!entry) return receiptError(404, "creative-operation-receipt-not-found");
    if (entry.completedAt === null) {
      return receiptError(409, "creative-operation-receipt-inflight");
    }
    if (entry.retryable) {
      return receiptError(409, "creative-operation-receipt-retryable");
    }
    if (entry.waiters !== 0) {
      return receiptError(409, "creative-operation-receipt-waiters-active");
    }
    entry.acknowledgedAt ??= this.#now();
    return { status: 200, body: { acknowledged: true } };
  }

  #waitFor(
    registryKey: string,
    entry: CreativeOperationEntry,
  ): Promise<CreativeOperationLease> {
    entry.waiters += 1;
    return entry.promise.then((response) => {
      let released = false;
      return {
        response,
        release: () => {
          if (released) return;
          released = true;
          this.#releaseWaiter(registryKey, entry);
        },
      };
    }, (error) => {
      this.#releaseWaiter(registryKey, entry);
      throw error;
    });
  }

  #releaseWaiter(registryKey: string, entry: CreativeOperationEntry): void {
    entry.waiters -= 1;
    if (entry.waiters < 0 && this.#entries.get(registryKey) === entry) {
      throw new Error("creative-operation-waiter-underflow");
    }
  }

  #pruneExpired(): void {
    const now = this.#now();
    for (const [key, entry] of this.#entries) {
      if (entry.completedAt !== null && !entry.retryable && entry.waiters === 0 &&
        entry.acknowledgedAt !== null && now - entry.acknowledgedAt >= this.#ttlMs) {
        this.#entries.delete(key);
      }
    }
  }

  #makeRoom(): boolean {
    while (this.#entries.size >= this.#maxEntries) {
      const completed = [...this.#entries.entries()]
        .filter(([, entry]) => entry.completedAt !== null && !entry.retryable &&
          entry.waiters === 0 && entry.acknowledgedAt !== null)
        .sort((left, right) => left[1].acknowledgedAt! - right[1].acknowledgedAt!)[0];
      if (!completed) return false;
      this.#entries.delete(completed[0]);
    }
    return true;
  }

  #registryKey(authScope: string, clientInstanceId: string, operationKey: string): string {
    return `${authScope}\u0000${clientInstanceId}\u0000${operationKey}`;
  }
}

function receiptError(status: number, error: string): OperationResponse {
  return { status, body: { error } };
}

export function createReviewServer(
  dependencies: ReviewAppDependencies,
  options: ReviewServerOptions = {},
) {
  const outputDir = dependencies.outputDir ?? join(process.cwd(), "review-renders");
  const secured: SecuredDependencies = {
    ...dependencies,
    outputDir,
    designerId: dependencies.designerId ?? "designer_01",
    reviewToken: dependencies.reviewToken ?? randomUUID(),
    customerCredentials: dependencies.customerCredentials ?? {},
    clock: () => new Date(options.now?.() ?? Date.now()).toISOString(),
    creativeHandoff: dependencies.creative ? {
      library: dependencies.library,
      outputDir: join(outputDir, "creative-editorial"),
      ...dependencies.creative.editorial,
      renderer: dependencies.renderer ?? renderRevision,
      originals: new LocalCreativeOriginalStore(join(outputDir, "a0-phone-returns")),
    } : null,
  };
  const creativeOperations = new CreativeOperationRegistry(options);
  const creativeClients = new CreativeClientRegistry();
  return createServer(async (request, response) => {
    try {
      await route(request, response, secured, creativeOperations, creativeClients);
    } catch (error) {
      sendDomainError(response, error);
    }
  });
}

export function listenReviewServer(
  server: ReturnType<typeof createReviewServer>,
  port = 4173,
  host = "127.0.0.1",
): Promise<void> {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolve());
  });
}

async function route(
  request: IncomingMessage,
  response: ServerResponse,
  dependencies: SecuredDependencies,
  creativeOperations: CreativeOperationRegistry,
  creativeClients: CreativeClientRegistry,
): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const host = request.headers.host ?? "";
  if (!isLoopbackHost(host)) throw new Error("loopback-host-required");

  if (method === "GET" && url.pathname === "/api/session") {
    if (!isTrustedOrigin(request.headers.origin, host)) {
      throw new Error("trusted-origin-required");
    }
    const authScope = creativeAuthScope(dependencies);
    const client = creativeClients.open(
      authScope,
      request.headers["x-client-instance"],
      request.headers["x-client-claim"],
    );
    return sendJson(response, 200, {
      designerId: dependencies.designerId,
      reviewToken: dependencies.reviewToken,
      ...client,
    });
  }
  if (method === "POST" || method === "DELETE") {
    if (!isTrustedOrigin(request.headers.origin, host)) {
      throw new Error("trusted-origin-required");
    }
    if (!request.headers["content-type"]?.startsWith("application/json")) {
      throw new Error("json-content-type-required");
    }
    if (request.headers["x-review-token"] !== dependencies.reviewToken) {
      throw new Error("review-token-required");
    }
  }

  if (method === "GET" && url.pathname === "/") {
    return sendStatic(response, "index.html", "text/html; charset=utf-8");
  }
  if (method === "GET" && url.pathname === "/app.js") {
    return sendStatic(response, "app.js", "text/javascript; charset=utf-8");
  }
  if (method === "GET" && url.pathname === "/styles.css") {
    return sendStatic(response, "styles.css", "text/css; charset=utf-8");
  }

  const creativeReceiptAcknowledgementMatch = url.pathname.match(
    /^\/api\/creative-operation-receipts\/([^/]+)$/,
  );
  if (method === "GET" && creativeReceiptAcknowledgementMatch) {
    if (!isTrustedOrigin(request.headers.origin, host)) {
      throw new Error("trusted-origin-required");
    }
    if (request.headers["x-review-token"] !== dependencies.reviewToken) {
      throw new Error("review-token-required");
    }
    const operationKey = requireCreativeOperationKey(
      decodeURIComponent(creativeReceiptAcknowledgementMatch[1]),
    );
    const authScope = creativeAuthScope(dependencies);
    const clientInstanceId = creativeClients.require(
      authScope,
      request.headers["x-client-instance"],
      request.headers["x-client-claim"],
    );
    const result = await creativeOperations.inspectResolved(
      authScope,
      clientInstanceId,
      operationKey,
    );
    const receipt = (result.body as { receipt?: { request: CreativeOperationRequest } }).receipt;
    if (receipt) {
      const projected = await projectCreativeMaterialResponse(receipt.request, result, dependencies.library);
      return sendJson(response, projected.status, projected.body);
    }
    return sendJson(response, result.status, result.body);
  }
  if (method === "DELETE" && creativeReceiptAcknowledgementMatch) {
    await readJsonBody(request);
    const operationKey = requireCreativeOperationKey(
      decodeURIComponent(creativeReceiptAcknowledgementMatch[1]),
    );
    const authScope = creativeAuthScope(dependencies);
    const clientInstanceId = creativeClients.require(
      authScope,
      request.headers["x-client-instance"],
      request.headers["x-client-claim"],
    );
    const result = creativeOperations.acknowledge(authScope, clientInstanceId, operationKey);
    return sendJson(response, result.status, result.body);
  }

  if (
    url.pathname === "/api/creative-sessions" ||
    url.pathname.startsWith("/api/creative-sessions/")
  ) {
    if (method === "GET" && isCreativeSecuredMediaPath(url.pathname)) {
      const sourceOrigin = request.headers.origin ?? originFromReferer(request.headers.referer);
      if (!sourceOrigin || !isTrustedOrigin(sourceOrigin, host)) {
        throw new Error("trusted-origin-required");
      }
      if (request.headers["x-review-token"] !== dependencies.reviewToken) {
        throw new Error("review-token-required");
      }
    }
    return routeCreative(
      request,
      response,
      method,
      url.pathname,
      dependencies,
      creativeOperations,
      creativeClients,
    );
  }

  const batchMatch = url.pathname.match(/^\/api\/batches\/([^/]+)$/);
  if (method === "GET" && batchMatch) {
    const batchId = decodeURIComponent(batchMatch[1]);
    const [assets, segments, stories, creativeSessions] = await Promise.all([
      dependencies.library.listAssets(),
      dependencies.library.listSegments(),
      dependencies.library.listStories(),
      dependencies.library.listCreativeSessions(),
    ]);
    const creativeBatchIds = creativeSessionBatchIds(creativeSessions);
    const creativeSessionIds = new Set(creativeSessions.map((session) => session.id));
    if (creativeBatchIds.has(batchId) || assets.some((asset) => asset.batchId === batchId &&
      asset.captureContext && creativeSessionIds.has(asset.captureContext.creativeSessionId))) {
      throw new Error("batch-not-found");
    }
    const batchAssets = assets.filter((asset) => asset.batchId === batchId);
    const batchSegments = segments.filter((segment) => segment.batchId === batchId);
    return sendJson(response, 200, {
      batchId,
      assets: batchAssets.map(publicAssetView),
      segments: batchSegments,
      stories: stories.filter((story) => story.batchId === batchId),
      failures: [
        ...batchAssets.flatMap((asset) =>
          asset.technicalFlags.map((flag) => ({ entityId: asset.id, flag })),
        ),
        ...batchSegments.flatMap((segment) =>
          segment.tags.technicalFlags.map((flag) => ({ entityId: segment.id, flag })),
        ),
      ],
    });
  }

  if (method === "GET" && url.pathname === "/api/library") {
    const [assets, segments, creativeSessions] = await Promise.all([
      dependencies.library.listAssets(),
      dependencies.library.listSegments(),
      dependencies.library.listCreativeSessions(),
    ]);
    const creativeBatchIds = creativeSessionBatchIds(creativeSessions);
    const publicAssets = assets.filter((asset) => !creativeBatchIds.has(asset.batchId) &&
      !asset.captureContext?.creativeSessionId).map(publicAssetView);
    const publicAssetIds = new Set(publicAssets.map((asset) => asset.id));
    return sendJson(response, 200, {
      assets: publicAssets,
      segments: segments.filter((segment) => !creativeBatchIds.has(segment.batchId) && publicAssetIds.has(segment.assetId)),
    });
  }

  if (method === "GET" && url.pathname === "/api/review") {
    const creativeBatchIds = new Set((await dependencies.library.listCreativeSessions())
      .map((session) => session.editorialBatchId).filter((value): value is string => value !== null));
    const stories = (await dependencies.library.listStories()).filter((story) =>
      !creativeBatchIds.has(story.batchId));
    const revisions = (
      await Promise.all(
        stories.map((story) => dependencies.library.listRevisions(story.id)),
      )
    ).flat();
    const artifacts = await dependencies.library.listAllArtifacts();
    return sendJson(response, 200, {
      stories: stories.map((story) => ({
        ...story,
        revisions: revisions.filter((revision) => revision.storyId === story.id),
        artifacts: artifacts.filter((artifact) =>
          revisions.some(
            (revision) =>
              revision.storyId === story.id && revision.id === artifact.revisionId,
          ),
        ).map(publicArtifactView),
        lineage: {
          new: story.anchorSegmentIds,
          history: story.historicalSegmentIds,
          historicalReasons: story.historicalReasons,
        },
        diffs: revisions
          .filter((revision) => revision.storyId === story.id)
          .filter((revision) => revision.parentRevisionId !== null)
          .map((revision) => ({
            revision: revision.revision,
            parentRevisionId: revision.parentRevisionId,
            prompt: revision.designerPrompt,
            explanation: revision.changeExplanation,
          })),
      })),
    });
  }

  if (method === "GET" && url.pathname === "/api/approved") {
    const [approvals, packages, sessions, stories] = await Promise.all([
      dependencies.library.listApprovals(),
      dependencies.library.listExportPackages(),
      dependencies.library.listCreativeSessions(),
      dependencies.library.listStories(),
    ]);
    const creativeBatchIds = new Set(sessions.map((session) => session.editorialBatchId)
      .filter((value): value is string => value !== null));
    const creativeStoryIds = new Set(stories.filter((story) => creativeBatchIds.has(story.batchId))
      .map((story) => story.id));
    return sendJson(response, 200, {
      approvals: approvals
        .filter((approval) => approval.status === "active" && !creativeStoryIds.has(approval.storyId))
        .map((approval) => ({
          ...approval,
          packages: packages.filter((value) => value.approvalId === approval.id).map(creativeExportPackageView),
        })),
    });
  }

  const revisionMatch = url.pathname.match(
    /^\/api\/stories\/([^/]+)\/revisions$/,
  );
  if (method === "POST" && revisionMatch) {
    const storyId = decodeURIComponent(revisionMatch[1]);
    const body = await readJsonBody(request);
    const prompt = requireString(body.prompt, "prompt-required");
    const designerId = dependencies.designerId;
    const story = (await dependencies.library.listStories()).find(
      (item) => item.id === storyId,
    );
    if (!story) throw new Error("story-not-found");
    if (await isCreativeStory(story.id, dependencies.library)) throw new Error("story-not-found");
    const revisions = await dependencies.library.listRevisions(storyId);
    const current = [...revisions].sort(
      (left, right) => right.revision - left.revision,
    )[0];
    if (!current) throw new Error("story-revision-not-found");
    const segments = await dependencies.library.listSegments();
    const next = await reviseWithPrompt(
      { revision: current, prompt, allowedSegments: segments, designerId, deferPersistence: true },
      dependencies.interpreter,
      dependencies.library,
    );
    const renderer = dependencies.renderer ?? renderRevision;
    const rendered = await renderer(
      next,
      segments,
      await dependencies.library.listAssets(),
      dependencies.outputDir ?? join(process.cwd(), "review-renders"),
    );
    await commitPromptRevision(
      { previous: current, next, artifacts: rendered.artifacts, designerId },
      dependencies.library,
    );
    return sendJson(response, 201, {
      revision: next,
      artifacts: rendered.artifacts.map(publicArtifactView),
    });
  }

  const approvalMatch = url.pathname.match(
    /^\/api\/artifacts\/([^/]+)\/approve$/,
  );
  const artifactMediaMatch = url.pathname.match(
    /^\/api\/artifacts\/([^/]+)\/media$/,
  );
  if (method === "GET" && artifactMediaMatch) {
    const sourceOrigin = request.headers.origin ?? originFromReferer(request.headers.referer);
    if (!sourceOrigin || !isTrustedOrigin(sourceOrigin, host)) {
      throw new Error("trusted-origin-required");
    }
    if (request.headers["x-review-token"] !== dependencies.reviewToken) {
      throw new Error("review-token-required");
    }
    const artifactId = decodeURIComponent(artifactMediaMatch[1]);
    const artifact = (await dependencies.library.listAllArtifacts()).find(
      (item) => item.id === artifactId,
    );
    if (!artifact) throw new Error("artifact-not-found");
    if (await isCreativeArtifact(artifact.id, dependencies.library)) {
      throw new Error("artifact-not-found");
    }
    return sendMediaFile(response, artifact.videoPath);
  }

  if (method === "POST" && approvalMatch) {
    const artifactId = decodeURIComponent(approvalMatch[1]);
    const body = await readJsonBody(request);
    const reviewerId = dependencies.designerId;
    const artifact = (await dependencies.library.listAllArtifacts()).find(
      (item) => item.id === artifactId,
    );
    if (!artifact) throw new Error("artifact-not-found");
    if (await isCreativeArtifact(artifact.id, dependencies.library)) {
      throw new Error("artifact-not-found");
    }
    const stories = await dependencies.library.listStories();
    const storyRevisions = await Promise.all(
      stories.map((story) => dependencies.library.listRevisions(story.id)),
    );
    const revision = storyRevisions.flat().find(
      (item) => item.id === artifact.revisionId,
    );
    if (!revision) throw new Error("artifact-revision-not-found");
    const approval = await approveVariant(
      {
        storyId: revision.storyId,
        revisionId: revision.id,
        revision: revision.revision,
        artifactId: artifact.id,
        variant: artifact.kind,
        reviewerId,
      },
      dependencies.library,
    );
    return sendJson(response, 201, { approval });
  }

  const exportMatch = url.pathname.match(
    /^\/api\/approvals\/([^/]+)\/export$/,
  );
  if (method === "POST" && exportMatch) {
    const approvalId = decodeURIComponent(exportMatch[1]);
    const body = await readJsonBody(request);
    if ("videoPath" in body || "coverPath" in body || "stillPaths" in body) {
      throw new Error("export-path-input-forbidden");
    }
    if (await isCreativeApproval(approvalId, dependencies.library)) {
      throw new Error("approval-not-found");
    }
    const value = await createExportPackage(
      {
        approvalId,
        artifactId: requireString(body.artifactId, "artifact-id-required"),
        revisionId: requireString(body.revisionId, "revision-id-required"),
      },
      dependencies.library,
    );
    return sendJson(response, 201, { package: creativeExportPackageView(value) });
  }

  const deliveryMatch = url.pathname.match(/^\/api\/export-packages\/([^/]+)\/deliver$/);
  if (method === "POST" && deliveryMatch) {
    await readJsonBody(request);
    if (await isCreativeExportPackage(decodeURIComponent(deliveryMatch[1]), dependencies.library)) {
      throw new Error("export-package-not-found");
    }
    if (!dependencies.phonePublisher) throw new Error("phone-publisher-required");
    const delivery = await deliverExportPackage(
      decodeURIComponent(deliveryMatch[1]),
      dependencies.phonePublisher,
      dependencies.library,
    );
    return sendJson(response, 201, { delivery });
  }

  sendJson(response, 404, { error: "route-not-found" });
}

async function routeCreative(
  request: IncomingMessage,
  response: ServerResponse,
  method: string,
  pathname: string,
  dependencies: SecuredDependencies,
  creativeOperations: CreativeOperationRegistry,
  creativeClients: CreativeClientRegistry,
): Promise<void> {
  const creative = dependencies.creative;
  if (!creative) throw new Error("creative-dependencies-required");
  if (method === "POST") {
    const body = await readJsonBody(request);
    const authenticatedCustomerId = await authenticatePrivacyResumeCustomer(
      pathname,
      request,
      dependencies,
    );
    const operationKey = requireCreativeOperationKey(request.headers["x-operation-key"]);
    const authScope = creativeAuthScope(dependencies);
    const clientInstanceId = creativeClients.require(
      authScope,
      request.headers["x-client-instance"],
      request.headers["x-client-claim"],
    );
    if (
      /^\/api\/creative-sessions\/[^/]+\/events$/.test(pathname) &&
      body.operationId !== operationKey
    ) {
      throw new Error("creative-event-operation-key-mismatch");
    }
    const operationRequest = {
      method,
      path: pathname,
      body,
    };
    const lease = await creativeOperations.run(
      authScope,
      clientInstanceId,
      operationKey,
      operationRequest,
      async () => {
        try {
          return await executeCreativePost(
            pathname, body, dependencies, creative, operationKey, clientInstanceId,
            authenticatedCustomerId,
          );
        } catch (error) {
          return domainErrorResponse(error);
        }
      },
    );
    if (response.destroyed) {
      lease.release();
    } else {
      response.once("finish", lease.release);
      response.once("close", lease.release);
      try {
        const projected = await projectCreativeMaterialResponse(operationRequest, lease.response, dependencies.library);
        sendJson(response, projected.status, projected.body);
      } catch (error) {
        lease.release();
        throw error;
      }
    }
    return;
  }

  if (method === "GET" && pathname === "/api/creative-sessions") {
    const sessions = await dependencies.library.listCreativeSessions();
    return sendJson(response, 200, { sessions: sessions.map(creativeSessionView) });
  }

  const detailMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)$/);
  if (method === "GET" && detailMatch) {
    const session = await requireCreativeSession(
      decodeURIComponent(detailMatch[1]),
      dependencies.library,
    );
    const sessionView = await creativeSessionDetailView(session, dependencies.library);
    return sendJson(response, 200, {
      session: sessionView,
    });
  }

  const artifactMediaMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/artifacts\/([^/]+)\/media$/,
  );
  if (method === "GET" && artifactMediaMatch) {
    const session = await requireCreativeSession(
      decodeURIComponent(artifactMediaMatch[1]), dependencies.library,
    );
    const artifactId = decodeURIComponent(artifactMediaMatch[2]);
    const task = await currentCreativeProcessingTask(session.id, dependencies.library);
    if (!task || task.status !== "reviewable") throw new Error("artifact-not-found");
    const bundle = await currentCreativeEditorialView(task.id, dependencies.library)
      .catch(() => { throw new Error("artifact-not-found"); });
    const artifact = bundle.artifacts.find((item) => item.id === artifactId);
    const revision = artifact && bundle.revisions.find((item) => item.id === artifact.revisionId);
    const story = revision && bundle.stories.find((item) => item.id === revision.storyId);
    if (!artifact || !revision || !story || story.batchId !== session.editorialBatchId) {
      throw new Error("artifact-not-found");
    }
    return sendMediaFile(response, artifact.videoPath);
  }

  const suggestionsMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/material-suggestions$/,
  );
  if (method === "GET" && suggestionsMatch) {
    const sessionId = decodeURIComponent(suggestionsMatch[1]);
    const view = await dependencies.library.projectCreativeMaterialState((state) => {
      const session = requireMaterialProjectionSession(sessionId, state);
      const suggestionSet = currentMaterialSuggestionSet(session.id, state);
      if (currentMaterialViewingDecision(session, suggestionSet, state.decisions)?.action !== "accept-view") {
        throw new Error("material-view-acceptance-required");
      }
      if (!suggestionSet) throw new Error("material-suggestion-set-not-found");
      return { suggestionSet: materialSuggestionSetView(suggestionSet, state) };
    });
    return sendJson(response, 200, view);
  }

  const previewMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/material-suggestions\/([^/]+)\/preview$/,
  );
  if (method === "GET" && previewMatch) {
    const sessionId = decodeURIComponent(previewMatch[1]);
    const segmentId = decodeURIComponent(previewMatch[2]);
    const asset = await dependencies.library.projectCreativeMaterialState((state) => {
      const session = requireMaterialProjectionSession(sessionId, state);
      const suggestionSet = currentMaterialSuggestionSet(session.id, state);
      if (currentMaterialViewingDecision(session, suggestionSet, state.decisions)?.action !== "accept-view") {
        throw new Error("material-view-acceptance-required");
      }
      const suggestion = suggestionSet?.suggestions.find((value) => value.segmentId === segmentId);
      if (!suggestion) throw new Error("material-preview-not-found");
      const segment = state.segments.find((value) => value.id === suggestion.segmentId);
      const asset = segment ? state.assets.find((value) => value.id === segment.assetId) : undefined;
      if (!asset || !isAuthorizedMaterialPreview(segment, asset, suggestion.technicallyAvailable)) {
        throw new Error("material-preview-not-authorized");
      }
      return asset;
    });
    return sendMediaFile(response, asset.localPath, previewContentType(asset.mediaKind, asset.localPath));
  }

  sendJson(response, 404, { error: "route-not-found" });
}

async function executeCreativePost(
  pathname: string,
  body: Record<string, unknown>,
  dependencies: SecuredDependencies,
  creative: CreativeReviewDependencies,
  operationKey: string,
  clientInstanceId: string,
  authenticatedCustomerId: string | null,
): Promise<OperationResponse> {
  const now = dependencies.clock();
  if (pathname === "/api/creative-sessions") {
    const started = await startCreativeSession({
      customerId: requireString(body.customerId, "customer-id-required"),
      title: requireString(body.title, "creative-title-required"),
      text: requireString(body.text, "creative-text-required"),
      actorId: dependencies.designerId,
      now,
      idempotencyKey: operationKey,
    }, creative.intentInterpreter, dependencies.library);
    const materialSuggestion = started.session.stage === "awaiting_material_consent"
      ? await suggestHistoricalMaterials({
          sessionId: started.session.id,
          actorId: dependencies.designerId,
          now,
        }, creative.materialMatcher, dependencies.library)
      : null;
    return {
      status: 201,
      body: {
        session: creativeSessionView(started.session),
        intent: started.intent,
        questions: started.questions,
        materialSuggestion,
      },
    };
  }

  const intentAnswersMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/intent-answers$/,
  );
  if (intentAnswersMatch) {
    const sessionId = decodeURIComponent(intentAnswersMatch[1]);
    const current = await requireCreativeSession(sessionId, dependencies.library);
    const answered = await answerCreativeIntentRecoverably({
      sessionId,
      text: requireString(body.text, "creative-intent-answer-text-required"),
      answers: requireStringRecord(body.answers, "creative-intent-answers-invalid"),
      actorId: dependencies.designerId,
      customerId: current.customerId,
      clientInstanceId,
      route: pathname,
      bodyDigest: createHash("sha256").update(canonicalJson(body)).digest("hex"),
      now,
      operationKey,
    }, {
      library: dependencies.library,
      materialMatcher: creative.materialMatcher,
      intentInterpreter: creative.intentInterpreter,
    });
    const materialSuggestion = answered.suggestionSet === null ? null : {
      ...answered.suggestionSet,
      suggestions: [],
      count: answered.suggestionSet.suggestions.length,
      hasSameProductMatches: answered.suggestionSet.suggestions.some((suggestion) =>
        answered.intent.products.some((product) => product.id === suggestion.productId)),
    };
    return { status: 201, body: {
      session: creativeSessionView(answered.session), intent: answered.intent,
      questions: answered.questions, materialSuggestion,
    } };
  }

  const privacyResumeMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/privacy-resume$/,
  );
  if (privacyResumeMatch) {
    if (!authenticatedCustomerId) throw new Error("customer-credential-required");
    const session = await resumeCreativePrivacy({
      creativeSessionId: decodeURIComponent(privacyResumeMatch[1]),
      actorId: authenticatedCustomerId,
      actorRole: "customer",
      confirmSafe: body.confirmSafe === true
        ? true
        : (() => { throw new Error("privacy-resume-safety-confirmation-required"); })(),
      operationKey,
      now,
    }, {
      library: dependencies.library,
      devices: creative.creativeDeviceAdapters.privacyDevices,
    });
    return { status: 201, body: { session: creativeSessionView(session) } };
  }

  const postSuggestMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/post-shoot-material-suggestions$/);
  if (postSuggestMatch) {
    const materialSuggestion = await suggestPostShootHistoricalMaterials({
      sessionId: decodeURIComponent(postSuggestMatch[1]), actorId: dependencies.designerId, now,
    }, creative.materialMatcher, dependencies.library);
    return { status: 201, body: { materialSuggestion } };
  }
  const postViewingMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/post-shoot-material-viewing$/);
  if (postViewingMatch) {
    const decision = await decidePostShootMaterialViewing({
      sessionId: decodeURIComponent(postViewingMatch[1]),
      suggestionSetId: requireString(body.suggestionSetId, "suggestion-set-id-required"),
      decision: requireOneOf(body.decision, ["accept", "decline"] as const,
        "material-viewing-decision-invalid"), actorId: dependencies.designerId, now, operationKey,
    }, dependencies.library);
    return { status: 201, body: { decision: decision.action === "decline-view"
      ? materialViewingDecisionView(decision)
      : decision } };
  }
  const postSelectionMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/post-shoot-material-selection$/);
  if (postSelectionMatch) {
    const sessionId = decodeURIComponent(postSelectionMatch[1]);
    const decision = await confirmPostShootMaterialSelection({
      sessionId,
      suggestionSetId: requireString(body.suggestionSetId, "suggestion-set-id-required"),
      selectedSegmentIds: requireStringArrayWithCode(body.selectedSegmentIds,
        "selected-segment-ids-invalid"), actorId: dependencies.designerId, now, operationKey,
    }, dependencies.library);
    return { status: 201, body: { decision } };
  }

  const editorialProcessMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/editorial-process$/,
  );
  const editorialManualResetMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/editorial-manual-reset$/,
  );
  if (editorialManualResetMatch) {
    const sessionId = decodeURIComponent(editorialManualResetMatch[1]);
    await requireCreativeSession(sessionId, dependencies.library);
    const confirmedBy = requireString(body.confirmedBy, "manual-restart-designer-required");
    if (confirmedBy !== dependencies.designerId) throw new Error("manual-restart-designer-mismatch");
    const confirmationCode = requireString(body.confirmationCode, "manual-restart-confirmation-code-required");
    if (confirmationCode !== "CONFIRM_MANUAL_EDITORIAL_RESTART") {
      throw new Error("manual-restart-confirmation-code-invalid");
    }
    const confirmationText = requireString(body.confirmationText, "manual-restart-confirmation-text-required");
    if (confirmationText !== "人工核对完成，确认重新开始剪辑") {
      throw new Error("manual-restart-confirmation-text-invalid");
    }
    const processOperationKey = requireCreativeOperationKey(
      requireString(body.processOperationKey, "manual-restart-process-operation-required"),
    );
    const failedTaskId = requireString(body.failedTaskId, "manual-restart-failed-task-required");
    const authorization = await dependencies.library.reserveCreativeEditorialRestart({
      id: `editorial_restart_${createHash("sha256").update(`${sessionId}:${operationKey}`).digest("hex").slice(0, 24)}`,
      creativeSessionId: sessionId,
      failedTaskId,
      resetOperationKey: operationKey,
      processOperationKey,
      confirmedBy,
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText,
      confirmedAt: now,
    });
    return { status: 201, body: { authorization } };
  }
  if (editorialProcessMatch) {
    const sessionId = decodeURIComponent(editorialProcessMatch[1]);
    await requireResolvedPostShootAuthorization(sessionId, dependencies.library);
    const manualRestartAuthorizationId = body.manualRestartAuthorizationId === undefined
      ? undefined
      : requireString(body.manualRestartAuthorizationId, "manual-restart-authorization-id-required");
    const editorial = await processCreativeEditorial({
      creativeSessionId: sessionId,
      operationKey,
      now,
      manualRestartAuthorizationId,
    }, requireCreativeHandoff(dependencies));
    const processingTask = creativeProcessingTaskView(editorial.task);
    return {
      status: editorial.task.status === "failed-retryable" ? 503
        : editorial.task.status === "failed-manual-review" ? 409 : 201,
      body: {
        processingTask,
        stories: editorial.stories,
        revisions: editorial.revisions,
        artifacts: editorial.artifacts.map((artifact) => creativeArtifactView(sessionId, artifact)),
      },
    };
  }

  const scopedRevisionMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/stories\/([^/]+)\/revisions$/,
  );
  if (scopedRevisionMatch) {
    const sessionId = decodeURIComponent(scopedRevisionMatch[1]);
    const storyId = decodeURIComponent(scopedRevisionMatch[2]);
    const { session, editorial } = await requireCreativeReviewContext(sessionId, dependencies.library,
      { kind: "story", id: storyId });
    const story = editorial.stories.find((item) => item.id === storyId);
    const current = editorial.revisions.find((item) => item.storyId === storyId);
    if (!story || !current || story.batchId !== session.editorialBatchId) {
      throw new Error("story-not-found");
    }
    const { segments } = await dependencies.library.readPromptRevisionSources(current, []);
    const next = await reviseWithPrompt({
      revision: current,
      prompt: requireString(body.prompt, "prompt-required"),
      allowedSegments: segments,
      designerId: dependencies.designerId,
      deferPersistence: true,
    }, dependencies.interpreter, dependencies.library);
    const rendered = await (dependencies.renderer ?? renderRevision)(
      next,
      segments,
      await dependencies.library.listAssets(),
      dependencies.outputDir ?? join(process.cwd(), "review-renders"),
    );
    await commitPromptRevision({
      previous: current,
      next,
      artifacts: rendered.artifacts,
      renderOutputRoot: dependencies.outputDir,
      designerId: dependencies.designerId,
    }, dependencies.library);
    return { status: 201, body: {
      revision: next,
      artifacts: rendered.artifacts.map((artifact) => creativeArtifactView(sessionId, artifact)),
    } };
  }

  const scopedApprovalMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/artifacts\/([^/]+)\/approve$/,
  );
  if (scopedApprovalMatch) {
    const sessionId = decodeURIComponent(scopedApprovalMatch[1]);
    const artifactId = decodeURIComponent(scopedApprovalMatch[2]);
    const { editorial } = await requireCreativeReviewContext(sessionId, dependencies.library,
      { kind: "artifact", id: artifactId });
    const artifact = editorial.artifacts.find((item) => item.id === artifactId);
    const revision = artifact && editorial.revisions.find((item) => item.id === artifact.revisionId);
    const story = revision && editorial.stories.find((item) => item.id === revision.storyId);
    if (!artifact || !revision || !story) throw new Error("artifact-not-found");
    const approval = await approveVariant({
      storyId: story.id,
      revisionId: revision.id,
      revision: revision.revision,
      artifactId: artifact.id,
      variant: artifact.kind,
      reviewerId: dependencies.designerId,
    }, dependencies.library);
    return { status: 201, body: { approval } };
  }

  const scopedExportMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/approvals\/([^/]+)\/export$/,
  );
  if (scopedExportMatch) {
    const sessionId = decodeURIComponent(scopedExportMatch[1]);
    const approvalId = decodeURIComponent(scopedExportMatch[2]);
    const { editorial } = await requireCreativeReviewContext(sessionId, dependencies.library,
      { kind: "approval", id: approvalId });
    const approval = (await dependencies.library.listApprovals()).find((item) =>
      item.id === approvalId && item.status === "active");
    const artifact = approval && editorial.artifacts.find((item) =>
      item.id === approval.artifactId && item.revisionId === approval.revisionId);
    if (!approval || !artifact) throw new Error("approval-not-found");
    const value = await createExportPackage({
      approvalId: approval.id,
      artifactId: artifact.id,
      revisionId: artifact.revisionId,
      creativeSessionId: sessionId,
    }, dependencies.library);
    return { status: 201, body: { package: creativeExportPackageView(value) } };
  }

  const scopedDeliveryMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/export-packages\/([^/]+)\/deliver$/,
  );
  if (scopedDeliveryMatch) {
    const sessionId = decodeURIComponent(scopedDeliveryMatch[1]);
    const packageId = decodeURIComponent(scopedDeliveryMatch[2]);
    const { editorial } = await requireCreativeReviewContext(sessionId, dependencies.library,
      { kind: "package", id: packageId });
    const value = (await dependencies.library.listExportPackages()).find((item) => item.id === packageId);
    const approval = value && (await dependencies.library.listApprovals()).find((item) =>
      item.id === value.approvalId && item.status === "active");
    const artifact = approval && editorial.artifacts.find((item) =>
      item.id === approval.artifactId && item.revisionId === approval.revisionId);
    if (!value || !approval || !artifact || value.artifactId !== artifact.id) {
      throw new Error("export-package-not-found");
    }
    if (!dependencies.phonePublisher) throw new Error("phone-publisher-required");
    const delivery = await deliverExportPackage(packageId, dependencies.phonePublisher, dependencies.library, sessionId);
    return { status: 201, body: { delivery } };
  }

  const captureHandoffMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/capture-handoff$/);
  if (captureHandoffMatch) {
    if (body.mode !== "a0-simulated-phone-return") throw new Error("a0-capture-mode-required");
    const sessionId = decodeURIComponent(captureHandoffMatch[1]);
    const session = await requireCreativeSession(sessionId, dependencies.library);
    const stepId = requireString(body.stepId, "creative-capture-step-id-required");
    const deviceId = requireString(body.deviceId, "creative-capture-device-id-required");
    const deviceRole = requireOneOf(body.deviceRole,
      ["watch-director", "main-stone-a", "main-stone-b"] as const,
      "creative-capture-device-role-invalid");
    const carrier = requireString(body.carrier, "creative-capture-carrier-required");
    const take = requirePositiveInteger(body.take, "creative-capture-take-required");
    const stepOutcome = requireOneOf(body.stepOutcome, ["completed", "retake", "skipped"] as const,
      "creative-capture-outcome-invalid");
    const handoff = requireCreativeHandoff(dependencies);
    const batchId = `creative_batch_${sessionId}`;
    const captureVideo = stepOutcome === "skipped"
      ? null
      : await a0SimulatedVideoBytes(join(dependencies.outputDir!, "a0-fixtures"));
    const result = await acceptCreativeCapture({
      creativeSessionId: sessionId, stepId, deviceId, deviceRole, carrier, take, stepOutcome,
      captureSession: { id: `capture_${sessionId}`, startedAt: session.createdAt,
        mode: "creator-necklace", privacyState: "creator", visitorConsent: "not-required",
        status: "stopped", productRef: null }, batchId,
      uploads: captureVideo === null ? [] : [{ bytes: captureVideo, kind: "video",
        extension: ".mp4", deviceModel: "A-0 simulated phone return" }],
      actorId: dependencies.designerId, operationKey, now,
    }, { library: dependencies.library, originals: handoff.originals });
    const processingTask = (await dependencies.library.listCreativeProcessingTasks())
      .find((item) => item.creativeSessionId === sessionId) ?? null;
    return { status: 201, body: { boundary: "A-0 local simulation",
      assets: result.assets.map(creativeCaptureAssetView),
      steps: result.steps, editorialBatchId: result.editorialBatchId,
      processingTask: processingTask ? creativeProcessingTaskView(processingTask) : null } };
  }

  const endHandoffMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/end-handoff$/);
  if (endHandoffMatch) {
    if (body.mode !== "a0-simulated-phone-return") throw new Error("a0-capture-mode-required");
    const sessionId = decodeURIComponent(endHandoffMatch[1]);
    const result = await settleCreativeCapture({ creativeSessionId: sessionId, actorId: dependencies.designerId,
      operationKey, now }, dependencies.library);
    const session = await requireCreativeSession(sessionId, dependencies.library);
    if (result.status === "collecting") throw new Error("creative-capture-returns-pending");
    const processingTask = await currentCreativeProcessingTask(sessionId, dependencies.library);
    return { status: 201, body: { boundary: "A-0 local simulation",
      session: creativeSessionView(session),
      processingTask: processingTask ? creativeProcessingTaskView(processingTask) : null,
      reason: result.status === "no-assets"
        ? "all planned steps skipped; editorial handoff has no captured assets"
        : "all planned capture returns recorded", forcedReshootCount: 0 } };
  }

  const viewingMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/material-viewing$/,
  );
  if (viewingMatch) {
    const decision = requireOneOf(
      body.decision,
      ["accept", "decline"] as const,
      "material-viewing-decision-invalid",
    );
    const value = await decideMaterialViewing({
      sessionId: decodeURIComponent(viewingMatch[1]),
      suggestionSetId: requireString(body.suggestionSetId, "suggestion-set-id-required"),
      decision,
      actorId: dependencies.designerId,
      now,
    }, dependencies.library);
    return { status: 201, body: { decision: materialViewingDecisionView(value) } };
  }

  const selectionMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/material-selection$/,
  );
  if (selectionMatch) {
    const decision = await confirmMaterialSelection({
      sessionId: decodeURIComponent(selectionMatch[1]),
      suggestionSetId: requireString(body.suggestionSetId, "suggestion-set-id-required"),
      selectedSegmentIds: requireStringArrayWithCode(
        body.selectedSegmentIds,
        "selected-segment-ids-invalid",
      ),
      actorId: dependencies.designerId,
      now,
    }, dependencies.library);
    return { status: 201, body: { decision } };
  }

  const scriptsMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/scripts$/);
  if (scriptsMatch) {
    const script = await generateScriptPackage({
      sessionId: decodeURIComponent(scriptsMatch[1]),
      actorId: dependencies.designerId,
      now,
    }, creative.scriptAgent, dependencies.library);
    return { status: 201, body: { script } };
  }

  const revisionsMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/script-revisions$/,
  );
  if (revisionsMatch) {
    const script = await reviseScriptPackage({
      sessionId: decodeURIComponent(revisionsMatch[1]),
      prompt: requireString(body.prompt, "script-revision-prompt-required"),
      actorId: dependencies.designerId,
      now,
    }, creative.scriptAgent, dependencies.library);
    return { status: 201, body: { script } };
  }

  const confirmationMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/script-confirmation$/,
  );
  if (confirmationMatch) {
    const script = await confirmScriptPackage({
      sessionId: decodeURIComponent(confirmationMatch[1]),
      scriptVersion: requirePositiveInteger(body.scriptVersion, "script-version-required"),
      actorId: dependencies.designerId,
      now,
    }, dependencies.library);
    return { status: 201, body: { script } };
  }

  const shootingMatch = pathname.match(
    /^\/api\/creative-sessions\/([^/]+)\/shooting-package$/,
  );
  if (shootingMatch) {
    const sessionId = decodeURIComponent(shootingMatch[1]);
    await requireShootingPackagePreconditions(sessionId, dependencies.library);
    await ensureSimulatedBindings(
      sessionId,
      creative.creativeDeviceAdapters.deviceCapabilities,
      dependencies.designerId,
      now,
      dependencies.library,
    );
    const shootingPackage = await createShootingPackage({
      sessionId,
      deviceCapabilities: creative.creativeDeviceAdapters.deviceCapabilities.map((value) => ({
        ...value,
      })),
      actorId: dependencies.designerId,
      now,
    }, creative.shotPlanningAgent, dependencies.library);
    return { status: 201, body: shootingPackage };
  }

  const dispatchMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/dispatch$/);
  if (dispatchMatch) {
    if (body.confirmStart !== true) throw new Error("dispatch-confirmation-required");
    const sessionId = decodeURIComponent(dispatchMatch[1]);
    const version = await preflightShootingPackageDispatch(sessionId, dependencies.library);
    const roster = creative.creativeDeviceAdapters.createRoster(version);
    const receipts = await dispatchShootingPackage({
      sessionId,
      confirmStart: true,
      roster,
      actorId: dependencies.designerId,
      now,
    }, dependencies.library);
    return { status: 201, body: { receipts } };
  }

  const eventsMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/events$/);
  if (eventsMatch) {
    const sessionId = decodeURIComponent(eventsMatch[1]);
    const result = await recordCreativeEvent(
      sessionId,
      body,
      dependencies.designerId,
      now,
      creative,
      dependencies.library,
    );
    return { status: 201, body: result };
  }

  return { status: 404, body: { error: "route-not-found" } };
}

async function authenticatePrivacyResumeCustomer(
  pathname: string,
  request: IncomingMessage,
  dependencies: SecuredDependencies,
): Promise<string | null> {
  const match = pathname.match(/^\/api\/creative-sessions\/([^/]+)\/privacy-resume$/);
  if (!match) return null;
  const customerId = singleHeader(request.headers["x-customer-id"]);
  const customerToken = singleHeader(request.headers["x-customer-token"]);
  if (!customerId || !customerToken) throw new Error("customer-credential-required");
  if (dependencies.customerCredentials[customerId] !== customerToken) {
    throw new Error("customer-credential-invalid");
  }
  const session = await requireCreativeSession(decodeURIComponent(match[1]), dependencies.library);
  if (session.customerId !== customerId) throw new Error("customer-credential-mismatch");
  return customerId;
}

function singleHeader(value: string | string[] | undefined): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function creativeSessionView(session: CreativeSession) {
  return {
    id: session.id,
    customerId: session.customerId,
    title: session.title,
    stage: session.stage,
    currentIntentVersion: session.currentIntentVersion,
    currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion,
    currentShotPlanVersion: session.currentShotPlanVersion,
    captureBatchIds: session.captureBatchIds,
    editorialBatchId: session.editorialBatchId,
    privacyMode: session.privacyMode,
    status: session.status,
    devices: session.deviceBindings.map((binding) => ({
      deviceId: binding.deviceId,
      deviceRole: binding.deviceRole,
      transport: binding.transport,
      status: session.privacyMode === "paused" ? "privacy-paused" as const : "simulated" as const,
    })),
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    allowedActions: allowedCreativeActions(session),
  };
}

async function creativeSessionDetailView(
  session: CreativeSession,
  library: ContentLibrary,
) {
  const [
    intents,
    briefs,
    scripts,
    shotPlans,
    directorCards,
    deviceBriefs,
    receipts,
    events,
    assets,
    processingTasks,
  ] = await Promise.all([
    library.listCreativeIntents(),
    library.listCreativeBriefs(),
    library.listScriptPackages(),
    library.listShotPlans(),
    library.listDirectorCards(),
    library.listDeviceBriefs(),
    library.listDispatchReceipts(),
    library.listCreativeSessionEvents(),
    library.listAssets(),
    library.listCreativeProcessingTasks(),
  ]);
  const versionMatches = (value: {
    sessionId: string;
    briefVersion: number;
    scriptVersion: number;
  }) => value.sessionId === session.id &&
    value.briefVersion === session.currentBriefVersion &&
    value.scriptVersion === session.currentScriptVersion;
  const taskVersionMatches = (value: {
    sessionId: string;
    briefVersion: number;
    scriptVersion: number;
    shotPlanVersion: number;
  }) => versionMatches(value) && value.shotPlanVersion === session.currentShotPlanVersion;
  const intent = intents.find((value) =>
    value.sessionId === session.id && value.version === session.currentIntentVersion,
  ) ?? null;
  const creativeBrief = briefs.find((value) =>
    value.sessionId === session.id && value.briefVersion === session.currentBriefVersion,
  ) ?? null;
  const script = scripts.find((value) => versionMatches(value) && value.status === "active") ?? null;
  const scriptHistory = scripts
    .filter((value) => value.sessionId === session.id && value.briefVersion === session.currentBriefVersion)
    .sort((left, right) => left.scriptVersion - right.scriptVersion);
  const shotPlan = shotPlans.find((value) => taskVersionMatches(value) && value.status === "active") ?? null;
  const currentDirectorCards = directorCards.filter((value) =>
    taskVersionMatches(value) && value.status === "active",
  );
  const currentDeviceBriefs = deviceBriefs.filter((value) =>
    taskVersionMatches(value) && value.status === "active",
  );
  const currentReceipts = receipts.filter(taskVersionMatches);
  const currentEvents = events.filter(taskVersionMatches);
  const stepResolutions = shotPlan
    ? resolveCreativeSteps(shotPlan, currentEvents, assets)
    : [];
  const processingTask = [...processingTasks]
    .filter((task) => task.creativeSessionId === session.id && !task.supersededByTaskId)
    .sort(compareCreativeProcessingTasks)[0] ?? null;
  if (processingTask?.status === "reviewable") {
    try { await currentCreativeEditorialView(processingTask.id, library); }
    catch (error) {
      if (!(error instanceof Error) || error.message !== "creative-editorial-current-output-invalid") throw error;
    }
  }

  return library.projectCreativeMaterialState(async (state) => {
    const currentSession = requireMaterialProjectionSession(session.id, state);
    const current = await currentCreativePublicState(session.id, state);
    const suggestionSet = currentMaterialSuggestionSet(session.id, state);
    const postShootSuggestionSet = [...state.sets]
      .filter((value) => value.sessionId === session.id && value.purpose === "post-shoot-edit")
      .sort((left, right) => right.version - left.version)[0] ?? null;
    const postShootViewDecision = currentMaterialViewingDecision(currentSession, postShootSuggestionSet, state.decisions);
    const canRevealSuggestions = currentSession.stage === "reviewing_materials" &&
      currentMaterialViewingDecision(currentSession, suggestionSet, state.decisions)?.action === "accept-view";
    const view = {
      ...creativeSessionView(currentSession),
      intent,
      questions: intent && session.stage === "draft_intent" ? missingIntentQuestionKeys(intent) : [],
      materialSuggestion: suggestionSet ? {
        id: suggestionSet.id,
        version: suggestionSet.version,
        count: suggestionSet.suggestions.length,
        hasSameProductMatches: intent !== null && suggestionSet.suggestions.some((suggestion) =>
          intent.products.some((product) => product.id === suggestion.productId),
        ),
        createdAt: suggestionSet.createdAt,
      } : null,
      suggestionSet: canRevealSuggestions && suggestionSet
        ? materialSuggestionSetView(suggestionSet, state)
        : null,
      materialDecision: currentMaterialDecisionView(currentSession, suggestionSet, state.decisions),
      postShootMaterialSuggestion: postShootSuggestionSet ? {
        id: postShootSuggestionSet.id, version: postShootSuggestionSet.version,
        purpose: postShootSuggestionSet.purpose, count: postShootSuggestionSet.suggestions.length,
      } : null,
      postShootMaterialDecision: currentMaterialDecisionView(currentSession, postShootSuggestionSet, state.decisions),
      postShootSuggestionSet: postShootSuggestionSet && postShootViewDecision?.action === "accept-view"
        ? materialSuggestionSetView(postShootSuggestionSet, state)
        : null,
      creativeBrief,
      script,
      scriptHistory,
      shotPlan,
      directorCards: currentDirectorCards,
      deviceBriefs: currentDeviceBriefs,
      receipts: currentReceipts,
      events: currentEvents,
      stepResolutions,
      processingTask: current.task ? creativeProcessingTaskView(current.task) : null,
      stories: current.stories,
      revisions: current.revisions,
      artifacts: current.artifacts.map((artifact) => creativeArtifactView(session.id, artifact)),
      approvals: current.approvals,
      exportPackages: current.exportPackages.map(creativeExportPackageView),
      deliveries: current.deliveries,
      devices: currentSession.deviceBindings.map((binding) => creativeDeviceView({
        binding,
        session: currentSession,
        directorCards: currentDirectorCards,
        deviceBriefs: currentDeviceBriefs,
        receipts: currentReceipts,
      })),
    };
    // Pre-shoot confirmed script evidence is separate. Check only the public editorial
    // content, including prompt text that names history without using it in the timeline.
    requireHistoricalIdentityViewing(session.id, {
      stories: view.stories, revisions: view.revisions, artifacts: view.artifacts,
      approvals: view.approvals, exportPackages: view.exportPackages, deliveries: view.deliveries,
    }, state);
    return view;
  });
}

function compareCreativeProcessingTasks(
  left: Awaited<ReturnType<ContentLibrary["listCreativeProcessingTasks"]>>[number],
  right: Awaited<ReturnType<ContentLibrary["listCreativeProcessingTasks"]>>[number],
): number {
  const operationPriority = Number(Boolean(right.operationKey)) - Number(Boolean(left.operationKey));
  return right.updatedAt.localeCompare(left.updatedAt) || operationPriority || right.id.localeCompare(left.id);
}

async function currentCreativeProcessingTask(
  sessionId: string,
  library: ContentLibrary,
) {
  return [...await library.listCreativeProcessingTasks()]
    .filter((task) => task.creativeSessionId === sessionId && !task.supersededByTaskId)
    .sort(compareCreativeProcessingTasks)[0] ?? null;
}

async function requireResolvedPostShootAuthorization(
  sessionId: string,
  library: ContentLibrary,
) {
  return library.projectCreativeMaterialState((state) => {
    const session = requireMaterialProjectionSession(sessionId, state);
    const suggestionSet = [...state.sets]
      .filter((value) => value.sessionId === sessionId && value.purpose === "post-shoot-edit")
      .sort((left, right) => right.version - left.version)[0];
    const viewing = currentMaterialViewingDecision(session, suggestionSet, state.decisions);
    if (viewing?.action === "decline-view") return viewing;
    const decision = suggestionSet && currentMaterialDecisionView(session, suggestionSet, state.decisions);
    if (viewing?.action === "accept-view" && decision?.action === "confirm-selection") return decision;
    throw new Error("post-shoot-authorization-required");
  });
}

function creativeProcessingTaskView(
  task: Awaited<ReturnType<ContentLibrary["listCreativeProcessingTasks"]>>[number],
) {
  return {
    id: task.id,
    creativeSessionId: task.creativeSessionId,
    editorialBatchId: task.editorialBatchId,
    status: task.status,
    phase: task.phase,
    errorCode: task.errorCode,
    updatedAt: task.updatedAt,
    operationKey: task.operationKey,
    inputDigest: task.inputDigest,
    publishedClaimed: task.publishedClaimed,
    supersededByTaskId: task.supersededByTaskId,
  };
}

function creativeCaptureAssetView(
  asset: Awaited<ReturnType<ContentLibrary["listAssets"]>>[number],
) {
  return {
    id: asset.id,
    batchId: asset.batchId,
    sessionId: asset.sessionId,
    digest: asset.digest,
    mediaKind: asset.mediaKind,
    importedAt: asset.importedAt,
    permissionState: asset.permissionState,
    origin: asset.origin,
    technicalFlags: asset.technicalFlags,
    captureContext: asset.captureContext,
  };
}

function publicAssetView(
  asset: Awaited<ReturnType<ContentLibrary["listAssets"]>>[number],
) {
  return creativeCaptureAssetView(asset);
}

function publicArtifactView(
  artifact: Awaited<ReturnType<ContentLibrary["listAllArtifacts"]>>[number],
) {
  return {
    id: artifact.id,
    revisionId: artifact.revisionId,
    kind: artifact.kind,
    digest: artifact.digest,
    sourceSegmentIds: artifact.sourceSegmentIds,
    coverDigest: artifact.coverDigest,
    stillDigests: artifact.stillDigests,
    visualSourceSegmentIds: artifact.visualSourceSegmentIds,
    renderInputDigest: artifact.renderInputDigest,
    status: artifact.status,
    mediaUrl: `/api/artifacts/${encodeURIComponent(artifact.id)}/media`,
  };
}

function creativeSessionBatchIds(
  sessions: Awaited<ReturnType<ContentLibrary["listCreativeSessions"]>>,
): Set<string> {
  return new Set(sessions.flatMap((session) => [
    ...session.captureBatchIds,
    ...(session.editorialBatchId ? [session.editorialBatchId] : []),
  ]));
}

async function requireCreativeReviewContext(
  sessionId: string,
  library: ContentLibrary,
  scope: Readonly<{ kind: "story" | "artifact" | "approval" | "package"; id: string }>,
) {
  const session = await requireCreativeSession(sessionId, library);
  const task = await currentCreativeProcessingTask(session.id, library);
  if (task?.status === "failed-manual-review" &&
    task.errorCode === "creative-editorial-current-output-invalid" &&
    await failedCurrentOutputOwnsReviewScope(session.id, task.id, scope, library)) {
    throw new Error(task.errorCode);
  }
  if (!task || task.status !== "reviewable" || task.supersededByTaskId) {
    throw new Error("creative-review-not-found");
  }
  const editorial = await currentCreativeEditorialView(task.id, library);
  if (editorial.stories.some((story) => story.batchId !== session.editorialBatchId)) {
    throw new Error("creative-review-not-found");
  }
  return { session, task, editorial };
}

async function failedCurrentOutputOwnsReviewScope(
  sessionId: string,
  taskId: string,
  scope: Readonly<{ kind: "story" | "artifact" | "approval" | "package"; id: string }>,
  library: ContentLibrary,
): Promise<boolean> {
  const heads = (await library.listCreativeRevisionHeads()).filter((head) =>
    head.creativeSessionId === sessionId && head.taskId === taskId);
  if (scope.kind === "story") return heads.some((head) => head.storyId === scope.id);
  if (scope.kind === "artifact") {
    const artifact = (await library.listAllArtifacts()).find((item) => item.id === scope.id && item.status === "active");
    return artifact !== undefined && heads.some((head) => head.revisionId === artifact.revisionId);
  }
  const approvals = await library.listApprovals();
  if (scope.kind === "approval") {
    const approval = approvals.find((item) => item.id === scope.id && item.status === "active");
    return approval !== undefined &&
      heads.some((head) => head.revisionId === approval.revisionId);
  }
  const value = (await library.listExportPackages()).find((item) => item.id === scope.id && item.status !== "invalidated");
  const packageApproval = value && approvals.find((item) =>
    item.id === value.approvalId && item.status === "active" && item.artifactId === value.artifactId);
  return packageApproval !== undefined && heads.some((head) => head.revisionId === packageApproval.revisionId);
}

function creativeExportPackageView(
  value: Awaited<ReturnType<ContentLibrary["listExportPackages"]>>[number],
) {
  return {
    id: value.id,
    approvalId: value.approvalId,
    artifactId: value.artifactId,
    variant: value.variant,
    videoDigest: value.videoDigest,
    title: value.title,
    caption: value.caption,
    coverDigest: value.coverDigest,
    stillDigests: value.stillDigests,
    renderInputDigest: value.renderInputDigest,
    idempotencyKey: value.idempotencyKey,
    status: value.status,
    createdAt: value.createdAt,
  };
}

async function isCreativeStory(storyId: string, library: ContentLibrary): Promise<boolean> {
  const story = (await library.listStories()).find((item) => item.id === storyId);
  if (!story) return false;
  return (await library.listCreativeSessions()).some((session) =>
    session.editorialBatchId !== null && session.editorialBatchId === story.batchId);
}

async function isCreativeArtifact(artifactId: string, library: ContentLibrary): Promise<boolean> {
  const artifact = (await library.listAllArtifacts()).find((item) => item.id === artifactId);
  if (!artifact) return false;
  const stories = await library.listStories();
  for (const story of stories) {
    if ((await library.listRevisions(story.id)).some((revision) => revision.id === artifact.revisionId)) {
      return isCreativeStory(story.id, library);
    }
  }
  return false;
}

async function isCreativeApproval(approvalId: string, library: ContentLibrary): Promise<boolean> {
  const approval = (await library.listApprovals()).find((item) => item.id === approvalId);
  return approval ? isCreativeStory(approval.storyId, library) : false;
}

async function isCreativeExportPackage(packageId: string, library: ContentLibrary): Promise<boolean> {
  const value = (await library.listExportPackages()).find((item) => item.id === packageId);
  return value ? isCreativeApproval(value.approvalId, library) : false;
}

function creativeArtifactView(
  sessionId: string,
  artifact: Awaited<ReturnType<ContentLibrary["listAllArtifacts"]>>[number],
) {
  return {
    id: artifact.id,
    revisionId: artifact.revisionId,
    kind: artifact.kind,
    status: artifact.status,
    sourceSegmentIds: artifact.sourceSegmentIds,
    visualSourceSegmentIds: artifact.visualSourceSegmentIds,
    mediaUrl: `/api/creative-sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifact.id)}/media`,
  };
}

async function currentCreativeEditorialView(
  taskId: string,
  library: ContentLibrary,
) {
  return library.readCreativeEditorialBundle(taskId, { currentRevisions: true });
}

function missingIntentQuestionKeys(intent: Awaited<ReturnType<ContentLibrary["listCreativeIntents"]>>[number]): string[] {
  const missing: string[] = [];
  if (!intent.theme.trim() || intent.theme === "Unspecified") missing.push("theme");
  if (intent.platforms.length === 0) missing.push("platforms");
  if (!intent.audience.trim() || intent.audience === "Unspecified") missing.push("audience");
  if (intent.shootConditions.length === 0) missing.push("shootConditions");
  if (intent.prohibitedAreas.length === 0) missing.push("prohibitedAreas");
  return missing;
}

function requireMaterialProjectionSession(sessionId: string, state: CreativeMaterialState): CreativeSession {
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session) throw new Error(`creative-session-not-found:${sessionId}`);
  return session;
}

function currentMaterialSuggestionSet(
  sessionId: string,
  state: CreativeMaterialState,
) {
  return [...state.sets]
    .filter((value) => value.sessionId === sessionId)
    .sort((left, right) => right.version - left.version)[0] ?? null;
}

function materialSuggestionSetView(
  suggestionSet: MaterialSuggestionSet,
  state: CreativeMaterialState,
) {
  const segmentsById = new Map(state.segments.map((value) => [value.id, value]));
  const assetsById = new Map(state.assets.map((value) => [value.id, value]));
  return {
    id: suggestionSet.id,
    sessionId: suggestionSet.sessionId,
    version: suggestionSet.version,
    createdAt: suggestionSet.createdAt,
    suggestions: suggestionSet.suggestions.map(({ thumbnailPath: _thumbnailPath, ...suggestion }) => {
      const segment = segmentsById.get(suggestion.segmentId);
      const asset = segment ? assetsById.get(segment.assetId) : undefined;
      const authorized = asset && isAuthorizedMaterialPreview(
        segment,
        asset,
        suggestion.technicallyAvailable,
      );
      return {
        ...suggestion,
        permissionState: segment?.permissionState ?? suggestion.permissionState,
        previewUrl: authorized
          ? `/api/creative-sessions/${encodeURIComponent(suggestionSet.sessionId)}/material-suggestions/${encodeURIComponent(suggestion.segmentId)}/preview`
          : null,
        previewKind: authorized ? asset.mediaKind : null,
      };
    }),
  };
}

function isAuthorizedMaterialPreview(
  segment: Awaited<ReturnType<ContentLibrary["listSegments"]>>[number] | undefined,
  asset: Awaited<ReturnType<ContentLibrary["listAssets"]>>[number],
  technicallyAvailable: boolean,
): boolean {
  return segment !== undefined &&
    segment.permissionState === "allowed" &&
    asset.permissionState === "allowed" &&
    technicallyAvailable &&
    segment.tags.technicalFlags.length === 0 &&
    asset.technicalFlags.length === 0;
}

function creativeDeviceView(input: {
  binding: CreativeSession["deviceBindings"][number];
  session: CreativeSession;
  directorCards: Awaited<ReturnType<ContentLibrary["listDirectorCards"]>>;
  deviceBriefs: Awaited<ReturnType<ContentLibrary["listDeviceBriefs"]>>;
  receipts: Awaited<ReturnType<ContentLibrary["listDispatchReceipts"]>>;
}) {
  const tasks = input.binding.deviceRole === "watch-director"
    ? input.directorCards
    : input.deviceBriefs.filter((value) =>
        value.deviceRole === input.binding.deviceRole && value.deviceId === input.binding.deviceId,
      );
  const taskReceipt = (taskId: string) => input.receipts.find((receipt) =>
    receipt.id === `dispatch_${taskId}` &&
    receipt.deviceRole === input.binding.deviceRole &&
    receipt.deviceId === input.binding.deviceId,
  );
  let status: "privacy-paused" | "waiting-dispatch" | "pending" | "failed" | "received";
  if (input.session.privacyMode === "paused") status = "privacy-paused";
  else if (tasks.length > 0 && tasks.every((task) => taskReceipt(task.id)?.status === "received")) {
    status = "received";
  } else if (tasks.some((task) => taskReceipt(task.id)?.status === "failed")) status = "failed";
  else if (tasks.some((task) => taskReceipt(task.id)?.status === "pending")) status = "pending";
  else status = "waiting-dispatch";
  const batteryByRole = { "watch-director": 82, "main-stone-a": 76, "main-stone-b": 71 };
  return {
    deviceId: input.binding.deviceId,
    deviceRole: input.binding.deviceRole,
    transport: input.binding.transport,
    status,
    battery: { label: "模拟电量", percent: batteryByRole[input.binding.deviceRole] },
  };
}

function previewContentType(
  mediaKind: Awaited<ReturnType<ContentLibrary["listAssets"]>>[number]["mediaKind"],
  path: string,
): string {
  if (mediaKind === "video") return "video/mp4";
  if (mediaKind === "audio") return "audio/mpeg";
  return ({ ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp" })[
    extname(path).toLowerCase()
  ] ?? "image/jpeg";
}

type CurrentCreativePublicState = Readonly<{
  available: boolean;
  task: CreativeMaterialState["editorial"]["tasks"][number] | null;
  stories: CreativeMaterialState["editorial"]["stories"][number][];
  revisions: CreativeMaterialState["editorial"]["revisions"][number][];
  artifacts: CreativeMaterialState["editorial"]["artifacts"][number][];
  approvals: CreativeMaterialState["editorial"]["approvals"][number][];
  exportPackages: CreativeMaterialState["editorial"]["exports"][number][];
  deliveries: CreativeMaterialState["editorial"]["deliveries"][number][];
}>;

/** One latest-snapshot boundary for every customer-visible current editorial result. */
async function currentCreativePublicState(
  sessionId: string,
  state: CreativeMaterialState,
): Promise<CurrentCreativePublicState> {
  const empty = {
    stories: [], revisions: [], artifacts: [], approvals: [], exportPackages: [], deliveries: [],
  } satisfies Omit<CurrentCreativePublicState, "available" | "task">;
  const session = requireMaterialProjectionSession(sessionId, state);
  const task = [...state.editorial.tasks]
    .filter((item) => item.creativeSessionId === sessionId && !item.supersededByTaskId)
    .sort(compareCreativeProcessingTasks)[0] ?? null;
  if (!task || task.status !== "reviewable") return { available: false, task, ...empty };
  try {
    if (!task.checkpoint || task.editorialBatchId !== session.editorialBatchId) {
      throw new Error("creative-current-public-state-invalid");
    }
    const stories = task.checkpoint.stories.map((checkpointStory) => {
      const story = singleCreativeProjectionRecord(state.editorial.stories.filter((item) =>
        item.id === checkpointStory.id && item.batchId === task.editorialBatchId));
      if (JSON.stringify(story) !== JSON.stringify(checkpointStory)) {
        throw new Error("creative-current-public-state-invalid");
      }
      return story;
    });
    const revisions = stories.map((story) => {
      const head = singleCreativeProjectionRecord(state.editorial.revisionHeads.filter((item) =>
        item.storyId === story.id && item.creativeSessionId === sessionId && item.taskId === task.id));
      const revision = singleCreativeProjectionRecord(state.editorial.revisions.filter((item) =>
        item.id === head.revisionId && item.storyId === story.id && item.revision === head.revision));
      return revision;
    });
    const artifacts = revisions.flatMap((revision) => state.editorial.artifacts.filter((item) =>
      item.revisionId === revision.id && item.status === "active"));
    for (const revision of revisions) {
      const binding = singleCreativeProjectionRecord(state.editorial.revisionBindings.filter((item) =>
        item.revisionId === revision.id && item.creativeSessionId === sessionId && item.taskId === task.id));
      await verifyRenderedRevision(revision, artifacts.filter((item) => item.revisionId === revision.id),
        state.segments, state.assets, binding.outputRoot);
    }
    requireCreativeEditorialProjectionAuthority(task,
      stories.flatMap((story) => story.historicalSegmentIds), state);
    const artifactById = new Map(artifacts.map((item) => [item.id, item]));
    const revisionById = new Map(revisions.map((item) => [item.id, item]));
    const approvals = state.editorial.approvals.filter((item) =>
      item.status === "active" && artifactById.has(item.artifactId));
    for (const approval of approvals) {
      const artifact = artifactById.get(approval.artifactId);
      const revision = artifact && revisionById.get(artifact.revisionId);
      if (!artifact || !revision || approval.storyId !== revision.storyId ||
        approval.revisionId !== revision.id || approval.revision !== revision.revision ||
        approval.variant !== artifact.kind || approval.artifactDigest !== artifact.digest ||
        approval.coverDigest !== artifact.coverDigest ||
        JSON.stringify(approval.stillDigests) !== JSON.stringify(artifact.stillDigests) ||
        approval.renderInputDigest !== artifact.renderInputDigest) {
        throw new Error("creative-current-public-state-invalid");
      }
    }
    const approvalById = new Map(approvals.map((item) => [item.id, item]));
    const exportPackages = state.editorial.exports.filter((item) =>
      item.status !== "invalidated" && approvalById.has(item.approvalId));
    for (const value of exportPackages) {
      const approval = approvalById.get(value.approvalId);
      const artifact = approval && artifactById.get(approval.artifactId);
      if (!approval || !artifact || value.artifactId !== artifact.id || value.variant !== artifact.kind ||
        value.videoPath !== artifact.videoPath || value.videoDigest !== artifact.digest ||
        value.coverPath !== artifact.coverPath || value.coverDigest !== artifact.coverDigest ||
        JSON.stringify(value.stillPaths) !== JSON.stringify(artifact.stillPaths) ||
        JSON.stringify(value.stillDigests) !== JSON.stringify(artifact.stillDigests) ||
        value.renderInputDigest !== artifact.renderInputDigest) {
        throw new Error("creative-current-public-state-invalid");
      }
    }
    const packageById = new Map(exportPackages.map((item) => [item.id, item]));
    const deliveries = state.editorial.deliveries.filter((item) => packageById.has(item.packageId));
    for (const delivery of deliveries) {
      const value = packageById.get(delivery.packageId);
      if (!value || delivery.publishedClaimed !== false ||
        (delivery.status === "received" && value.status !== "delivered") ||
        (delivery.status === "pending-retry" && value.status !== "pending-retry")) {
        throw new Error("creative-current-public-state-invalid");
      }
    }
    return { available: true, task, stories, revisions, artifacts, approvals, exportPackages, deliveries };
  } catch (error) {
    if (error instanceof Error && error.message === "historical-authorization-invalid") throw error;
    return { available: false, task: { ...task, status: "failed-manual-review",
      errorCode: "creative-current-public-state-invalid", publishedClaimed: false }, ...empty };
  }
}

async function projectCreativeMaterialResponse(
  request: CreativeOperationRequest,
  response: OperationResponse,
  library: ContentLibrary,
): Promise<OperationResponse> {
  const match = request.path.match(/^\/api\/creative-sessions\/([^/]+)\/(?:post-shoot-)?material-(viewing|selection)$/);
  if (!match) return projectCreativeEditorialResponse(request, response, library);
  if (response.status >= 400 || (match[2] === "viewing" && request.body.decision === "decline")) return response;
  const sessionId = decodeURIComponent(match[1]);
  return library.projectCreativeMaterialState((state) => {
    const session = state.sessions.find((item) => item.id === sessionId);
    const set = state.sets.find((item) => item.id === request.body.suggestionSetId && item.sessionId === sessionId);
    if (!session || !set || currentMaterialViewingDecision(session, set, state.decisions)?.action !== "accept-view") {
      return { status: 403, body: { error: "creative-material-view-no-longer-authorized" } };
    }
    return response;
  });
}

function requireCreativeEditorialProjectionAuthority(
  task: CreativeProcessingTask,
  historicalIds: readonly string[],
  state: CreativeMaterialState,
): void {
  if (!task.checkpoint) throw new Error("historical-authorization-invalid");
  const session = requireMaterialProjectionSession(task.creativeSessionId, state);
  const set = [...state.sets].filter((value) => value.sessionId === session.id && value.purpose === "post-shoot-edit")
    .sort((left, right) => right.version - left.version)[0] ?? null;
  const authorization = validateHistoricalMaterialAuthorization(task.checkpoint.authorization, task.editorialBatchId, state);
  const current = resolveHistoricalMaterialAuthorizationFromRecords(session.id, state);
  if (JSON.stringify(authorization) !== JSON.stringify(current) ||
    (authorization.selectedSegmentIds.length > 0 && currentMaterialViewingDecision(session, set, state.decisions)?.action !== "accept-view") ||
    historicalIds.some((id) => !(authorization.selectedSegmentIds as readonly string[]).includes(id))) {
    throw new Error("historical-authorization-invalid");
  }
}

function requireHistoricalIdentityViewing(sessionId: string, value: unknown, state: CreativeMaterialState): void {
  const historicalAssets = new Set(state.assets.filter((asset) => asset.origin === "historical").map((asset) => asset.id));
  const historicalSegments = state.segments.filter((segment) => historicalAssets.has(segment.assetId));
  const identities = new Set([...historicalAssets, ...historicalSegments.map((segment) => segment.id)]);
  const knownIds = [...new Set([...state.assets, ...state.segments].map((record) => record.id))];
  const referenced = [...referencedCatalogIdentities(value, knownIds)].filter((id) => identities.has(id));
  if (referenced.length === 0) return;
  const session = requireMaterialProjectionSession(sessionId, state);
  const set = [...state.sets].filter((item) => item.sessionId === sessionId && item.purpose === "post-shoot-edit")
    .sort((left, right) => right.version - left.version)[0] ?? null;
  const shownSegments = new Set(set?.suggestions.map((item) => item.segmentId));
  const visible = new Set([...shownSegments, ...historicalSegments.filter((segment) => shownSegments.has(segment.id)).map((segment) => segment.assetId)]);
  if (currentMaterialViewingDecision(session, set, state.decisions)?.action !== "accept-view" ||
    referenced.some((id) => !visible.has(id))) throw new Error("historical-authorization-invalid");
}

function referencedCatalogIdentities(value: unknown, knownIds: readonly string[]): Set<string> {
  const referenced = new Set<string>();
  const visit = (part: unknown): void => {
    if (typeof part === "string") {
      const matched: Array<{ start: number; end: number; id: string }> = [];
      for (const id of knownIds) {
        for (let start = part.indexOf(id); start !== -1; start = part.indexOf(id, start + 1)) {
          const end = start + id.length;
          // IDs are case-sensitive literals. Chinese/Japanese prose may touch an ID;
          // ASCII identifier continuations must not turn a word or longer ID into a match.
          if (/[A-Za-z0-9_-]/.test(part[start - 1] ?? "") || /[A-Za-z0-9_-]/.test(part[end] ?? "")) continue;
          matched.push({ start, end, id });
        }
      }
      // A containing range always precedes its contents in this order. Keep only a
      // running maximum end: O(m log m) sorting + O(m) scanning, never O(m²) rescans.
      // Crossing ranges extend the maximum and remain distinct; their union is not an ID.
      matched.sort((left, right) => left.start - right.start || right.end - left.end);
      let maximumEnd = -1;
      for (const range of matched) {
        if (range.end <= maximumEnd) continue;
        maximumEnd = range.end;
        referenced.add(range.id);
      }
    } else if (Array.isArray(part)) {
      part.forEach(visit);
    } else if (part && typeof part === "object") {
      for (const [key, item] of Object.entries(part)) { visit(key); visit(item); }
    }
  };
  // Inspect original strings, not JSON escapes: a newline must not become an ASCII 'n'.
  visit(value);
  return referenced;
}

async function projectCreativeEditorialResponse(
  request: CreativeOperationRequest,
  response: OperationResponse,
  library: ContentLibrary,
): Promise<OperationResponse> {
  const match = request.path.match(/^\/api\/creative-sessions\/([^/]+)\/(editorial-process|stories\/([^/]+)\/revisions|artifacts\/([^/]+)\/approve|approvals\/([^/]+)\/export|export-packages\/([^/]+)\/deliver)$/);
  if (!match || response.status >= 400) return response;
  const receipt = (response.body as { receipt?: { response?: OperationResponse } } | null)?.receipt;
  const completed = receipt ? receipt.response : response;
  const body = completed?.body as { processingTask?: { id: string }; revision?: { id: string } } | undefined;
  const sessionId = decodeURIComponent(match[1]);
  return library.projectCreativeMaterialState(async (state) => {
    // Viewing and using are distinct: quoted identities require viewing even when frozen
    // use is empty. Preserve the original durable request/response; restrict only disclosure.
    requireHistoricalIdentityViewing(sessionId, response.body, state);
    // Errors and inflight receipts contain no successful editorial output to re-authorize.
    if (!completed || completed.status >= 400) return response;
    const records = state.editorial;
    let task: CreativeProcessingTask;
    if (match[2] === "editorial-process") {
      task = singleCreativeProjectionRecord(records.tasks.filter((value) =>
        value.id === body?.processingTask?.id && value.creativeSessionId === sessionId));
    } else {
      let revisionId: string;
      if (match[3]) {
        const storyId = decodeURIComponent(match[3]);
        // Inflight prompt receipts also expose their request text. Use the independent head,
        // never a surviving revision maximum, to resolve their current frozen-use owner.
        revisionId = body?.revision?.id ?? singleCreativeProjectionRecord(records.revisionHeads.filter((head) =>
          head.storyId === storyId && head.creativeSessionId === sessionId)).revisionId;
        singleCreativeProjectionRecord(records.revisions.filter((revision) => revision.id === revisionId && revision.storyId === storyId));
      } else {
        const artifactId = match[4] ? decodeURIComponent(match[4])
          : match[5] ? singleCreativeProjectionRecord(records.approvals.filter((approval) =>
            approval.id === decodeURIComponent(match[5]))).artifactId
            : singleCreativeProjectionRecord(records.exports.filter((value) =>
              value.id === decodeURIComponent(match[6]))).artifactId;
        revisionId = singleCreativeProjectionRecord(records.artifacts.filter((artifact) => artifact.id === artifactId)).revisionId;
      }
      const binding = singleCreativeProjectionRecord(records.revisionBindings.filter((value) =>
        value.revisionId === revisionId && value.creativeSessionId === sessionId));
      task = singleCreativeProjectionRecord(records.tasks.filter((value) => value.id === binding.taskId && value.creativeSessionId === sessionId));
    }
    if (!task.checkpoint) throw new Error("historical-authorization-invalid");
    const current = await currentCreativePublicState(sessionId, state);
    if (!current.available || current.task?.id !== task.id ||
      !await creativeEditorialResponseIsCurrent(match, completed, current, task, state)) {
      return { status: 403, body: { error: "creative-current-output-no-longer-authorized" } };
    }
    // Viewing and use identity checks above remain independent from this exact-current gate.
    return response;
  });
}

async function creativeEditorialResponseIsCurrent(
  match: RegExpMatchArray,
  completed: OperationResponse | undefined,
  current: CurrentCreativePublicState,
  task: CreativeProcessingTask,
  state: CreativeMaterialState,
): Promise<boolean> {
  const records = state.editorial;
  if (!completed || completed.status >= 400) return match[2] === "editorial-process";
  const body = completed.body as {
    processingTask?: { id?: string };
    revision?: { id?: string };
    approval?: { id?: string; artifactId?: string };
    package?: { id?: string; approvalId?: string };
    delivery?: { packageId?: string; receiptId?: string | null };
  } | undefined;
  if (match[2] === "editorial-process") {
    try {
      if (!task.checkpoint) return false;
      const storiesById = uniqueCreativeProjectionMap(records.stories);
      const revisionsById = uniqueCreativeProjectionMap(records.revisions);
      const artifactsById = uniqueCreativeProjectionMap(records.artifacts);
      const bindingsByRevisionId = uniqueCreativeProjectionMap(records.revisionBindings, (item) => item.revisionId);
      const stories = task.checkpoint.stories.map((checkpoint) => {
        const story = storiesById.get(checkpoint.id);
        if (!story || canonicalJson(story) !== canonicalJson(checkpoint)) {
          throw new Error("creative-editorial-response-lineage-invalid");
        }
        return story;
      });
      const revisions = task.checkpoint.revisions.map((checkpoint) => {
        const revision = revisionsById.get(checkpoint.id);
        if (!revision || canonicalJson(revision) !== canonicalJson(checkpoint)) {
          throw new Error("creative-editorial-response-lineage-invalid");
        }
        return revision;
      });
      const artifacts = task.checkpoint.artifacts.map((checkpoint) => {
        const artifact = artifactsById.get(checkpoint.id);
        if (!artifact || canonicalJson(artifact) !== canonicalJson(checkpoint)) {
          throw new Error("creative-editorial-response-lineage-invalid");
        }
        return artifact;
      });
      const artifactsByRevisionId = new Map<string, typeof artifacts>();
      for (const artifact of artifacts) {
        const group = artifactsByRevisionId.get(artifact.revisionId) ?? [];
        group.push(artifact);
        artifactsByRevisionId.set(artifact.revisionId, group);
      }
      for (const revision of revisions) {
        const binding = bindingsByRevisionId.get(revision.id);
        if (!binding || binding.creativeSessionId !== task.creativeSessionId || binding.taskId !== task.id) {
          throw new Error("creative-editorial-response-lineage-invalid");
        }
        await verifyRenderedRevision(revision, artifactsByRevisionId.get(revision.id) ?? [],
          state.segments, state.assets, binding.outputRoot);
      }
      const projected = {
        processingTask: creativeProcessingTaskView(task),
        stories,
        revisions,
        artifacts: artifacts.map((artifact) => creativeArtifactView(task.creativeSessionId, artifact)),
      };
      return canonicalJson(body) === canonicalJson(projected);
    } catch {
      return false;
    }
  }
  if (match[3]) return current.revisions.some((item) => item.id === body?.revision?.id && item.storyId === decodeURIComponent(match[3]!));
  if (match[4]) return current.approvals.some((item) => item.id === body?.approval?.id &&
    item.artifactId === decodeURIComponent(match[4]!));
  if (match[5]) return current.exportPackages.some((item) => item.id === body?.package?.id &&
    item.approvalId === decodeURIComponent(match[5]!));
  if (match[6]) return current.deliveries.some((item) => item.packageId === decodeURIComponent(match[6]!) &&
    item.packageId === body?.delivery?.packageId && item.receiptId === body.delivery.receiptId);
  return false;
}

function uniqueCreativeProjectionMap<T>(
  records: readonly T[],
  key: (record: T) => string = (record) => (record as { id: string }).id,
): Map<string, T> {
  const result = new Map<string, T>();
  for (const record of records) {
    const id = key(record);
    if (result.has(id)) throw new Error("historical-authorization-invalid");
    result.set(id, record);
  }
  return result;
}

function singleCreativeProjectionRecord<T>(records: readonly T[]): T {
  if (records.length !== 1) throw new Error("historical-authorization-invalid");
  return records[0]!;
}

function currentMaterialDecisionView(
  session: CreativeSession,
  set: MaterialSuggestionSet | null,
  decisions: readonly MaterialUseDecision[],
) {
  const viewing = currentMaterialViewingDecision(session, set, decisions);
  if (!viewing || !set) return null;
  if (viewing.action === "decline-view") return materialViewingDecisionView(viewing);
  return [...session.materialUseDecisionIds].reverse().map((id) => decisions.find((decision) =>
    decision.id === id && decision.sessionId === session.id && decision.suggestionSetId === set.id &&
    decision.suggestionVersion === set.version && decision.purpose === set.purpose))
    .find((decision) => decision !== undefined) ?? viewing;
}

function materialViewingDecisionView(value: Awaited<ReturnType<typeof decideMaterialViewing>>) {
  return {
    id: value.id,
    sessionId: value.sessionId,
    suggestionSetId: value.suggestionSetId,
    suggestionVersion: value.suggestionVersion,
    purpose: value.purpose,
    action: value.action,
    actorId: value.actorId,
    createdAt: value.createdAt,
  };
}

function allowedCreativeActions(session: CreativeSession): string[] {
  if (session.status !== "active") return [];
  switch (session.stage) {
    case "draft_intent":
      return ["answer-intent"];
    case "awaiting_material_consent":
      return ["accept-material-viewing", "decline-material-viewing"];
    case "reviewing_materials":
      return ["view-material-suggestions", "confirm-material-selection"];
    case "materials_declined":
      return ["generate-script"];
    case "awaiting_script_confirmation":
      return session.currentScriptVersion === 0
        ? ["generate-script"]
        : ["revise-script", "confirm-script"];
    case "ready_to_dispatch":
      return session.currentShotPlanVersion === 0
        ? ["revise-script", "create-shooting-package"]
        : ["revise-script", "dispatch"];
    case "shooting":
      return ["revise-script", "record-event", "capture-handoff", "end-handoff"];
    case "ingesting":
      return ["record-event", "capture-handoff", "end-handoff"];
    case "editorial_review":
      return ["propose-post-shoot-materials", "decide-post-shoot-materials",
        "confirm-post-shoot-materials"];
    case "paused":
      return session.privacyMode === "paused" ? ["resume-privacy"] : [];
    default:
      return [];
  }
}

async function requireCreativeSession(
  sessionId: string,
  library: ContentLibrary,
): Promise<CreativeSession> {
  const session = (await library.listCreativeSessions()).find((value) => value.id === sessionId);
  if (!session) throw new Error("creative-session-not-found");
  return session;
}

async function ensureSimulatedBindings(
  sessionId: string,
  capabilities: readonly DeviceCapability[],
  actorId: string,
  now: string,
  library: ContentLibrary,
): Promise<void> {
  const roles: DeviceRole[] = ["watch-director", "main-stone-a", "main-stone-b"];
  if (
    capabilities.length !== roles.length ||
    roles.some((role) => capabilities.filter((value) => value.deviceRole === role).length !== 1) ||
    capabilities.some((value) =>
      typeof value.deviceId !== "string" || value.deviceId.trim().length === 0 ||
      typeof value.carrier !== "string" || value.carrier.trim().length === 0
    )
  ) {
    throw new Error("creative-device-adapters-invalid");
  }
  const session = await requireCreativeSession(sessionId, library);
  const bindings = capabilities.map((value) => ({
    deviceId: value.deviceId,
    deviceRole: value.deviceRole,
    transport: "simulated" as const,
  }));
  if (JSON.stringify(session.deviceBindings) === JSON.stringify(bindings)) return;
  if (session.deviceBindings.length > 0) throw new Error("creative-device-bindings-conflict");
  const next = { ...session, deviceBindings: bindings, updatedAt: now };
  await library.commitCreativeBundle({
    session: next,
    expectedSessionSnapshot: session,
    auditEvents: [{
      id: `audit_${randomUUID()}`,
      entityType: "creative-session",
      entityId: session.id,
      actorId,
      action: "simulated-device-bindings-added",
      reason: "bound deterministic local A-0 device roles",
      before: { deviceBindings: [] },
      after: { deviceBindings: bindings },
      createdAt: now,
    }],
  });
}

async function requireShootingPackagePreconditions(
  sessionId: string,
  library: ContentLibrary,
): Promise<void> {
  const session = await requireCreativeSession(sessionId, library);
  if (session.status !== "active") throw new Error("creative-session-not-active");
  const script = (await library.listScriptPackages()).find((value) =>
    value.sessionId === session.id &&
    value.briefVersion === session.currentBriefVersion &&
    value.scriptVersion === session.currentScriptVersion,
  );
  if (!script) throw new Error("script-not-found");
  if (script.status !== "active") throw new Error("script-not-active");
  if (script.confirmedAt === null) throw new Error("confirmed-script-required");
  if (session.stage !== "ready_to_dispatch") throw new Error("shooting-package-not-allowed");
  if (session.currentShotPlanVersion !== 0) {
    throw new Error("shooting-package-already-created");
  }
}

async function recordCreativeEvent(
  sessionId: string,
  body: Record<string, unknown>,
  actorId: string,
  now: string,
  creative: CreativeReviewDependencies,
  library: ContentLibrary,
) {
  const action = requireOneOf(
    body.action,
    ["started", "completed", "retake", "skipped", "voice-rewrite", "note", "privacy-paused"] as const,
    "creative-event-action-invalid",
  );
  const operationId = requireString(body.operationId, "creative-event-operation-id-required");
  const stepId = requireString(body.stepId, "creative-event-step-id-required");
  const briefVersion = requirePositiveInteger(body.briefVersion, "brief-version-required");
  const scriptVersion = requirePositiveInteger(body.scriptVersion, "script-version-required");
  const shotPlanVersion = requirePositiveInteger(body.shotPlanVersion, "shot-plan-version-required");
  const transcript = optionalStringValue(body.transcript, "creative-event-transcript-invalid");
  const reason = optionalStringValue(body.reason, "creative-event-reason-invalid");
  const deviceId = optionalString(body.deviceId, "creative-event-device-id-invalid");
  const deviceRole = body.deviceRole === undefined || body.deviceRole === null
    ? null
    : requireOneOf(
        body.deviceRole,
        ["watch-director", "main-stone-a", "main-stone-b"] as const,
        "creative-event-device-role-invalid",
      );
  const restorePrivacy = action === "privacy-paused"
    ? await pauseBoundCreativeDevices(sessionId, creative.creativeDeviceAdapters.privacyDevices, library)
    : null;
  try {
    return await recordCreativeSessionAction({
      sessionId,
      briefVersion,
      scriptVersion,
      shotPlanVersion,
      stepId,
      action,
      actorId,
      deviceId,
      deviceRole,
      reason,
      transcript,
      operationId,
      now,
    }, creative.scriptAgent, library);
  } catch (error) {
    restorePrivacy?.();
    throw error;
  }
}

async function pauseBoundCreativeDevices(
  sessionId: string,
  devices: readonly CreativePrivacyDevice[],
  library: ContentLibrary,
): Promise<() => void> {
  const session = await requireCreativeSession(sessionId, library);
  const byId = new Map(devices.map((device) => [device.deviceId, device]));
  if (byId.size !== devices.length) throw new Error("creative-privacy-device-roster-invalid");
  const bound = session.deviceBindings.map((binding) => byId.get(binding.deviceId));
  if (bound.some((device) => device === undefined)) {
    throw new Error("creative-privacy-device-roster-invalid");
  }
  const previouslyActive = (bound as CreativePrivacyDevice[]).filter(
    (device) => !device.isPrivacyPaused(),
  );
  try {
    for (const device of bound as CreativePrivacyDevice[]) device.pausePrivacy();
  } catch (error) {
    for (const device of previouslyActive) {
      try { device.resumePrivacy(); } catch { /* session remains active; surface original pause failure */ }
    }
    throw error;
  }
  return () => {
    for (const device of previouslyActive) {
      try { device.resumePrivacy(); } catch { /* preserve the original persistence error */ }
    }
  };
}

function requirePositiveInteger(value: unknown, code: string): number {
  if (!Number.isInteger(value) || (value as number) < 1) throw new Error(code);
  return value as number;
}

function requireStringArrayWithCode(value: unknown, code: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(code);
  }
  return value;
}

function requireStringRecord(value: unknown, code: string): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    Object.values(value).some((item) => typeof item !== "string")) throw new Error(code);
  return value as Record<string, string>;
}

function requireDeviceCarrier(
  session: CreativeSession,
  deviceId: string,
  capabilities: readonly DeviceCapability[],
): string {
  if (!session.deviceBindings.some((binding) => binding.deviceId === deviceId)) {
    throw new Error("creative-capture-device-not-assigned");
  }
  const carrier = capabilities.find((item) => item.deviceId === deviceId)?.carrier;
  if (!carrier) throw new Error("creative-capture-carrier-required");
  return carrier;
}

function requireOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  code: string,
): T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) throw new Error(code);
  return value as T[number];
}

function optionalString(value: unknown, code: string): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string" || value.trim().length === 0) throw new Error(code);
  return value.trim();
}

function optionalStringValue(value: unknown, code: string): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") throw new Error(code);
  return value;
}

function isLoopbackHost(host: string): boolean {
  return /^(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/.test(host);
}

function isTrustedOrigin(origin: string | undefined, host: string): boolean {
  if (origin === undefined) return true;
  try {
    const parsed = new URL(origin);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host === host && isLoopbackHost(parsed.host);
  } catch {
    return false;
  }
}

function originFromReferer(referer: string | undefined): string | undefined {
  if (!referer) return undefined;
  try {
    return new URL(referer).origin;
  } catch {
    return undefined;
  }
}

function isMaterialPreviewPath(pathname: string): boolean {
  return /^\/api\/creative-sessions\/[^/]+\/material-suggestions\/[^/]+\/preview$/.test(pathname);
}

function isCreativeSecuredMediaPath(pathname: string): boolean {
  return isMaterialPreviewPath(pathname) ||
    /^\/api\/creative-sessions\/[^/]+\/artifacts\/[^/]+\/media$/.test(pathname);
}

function requireCreativeHandoff(
  dependencies: SecuredDependencies,
): NonNullable<SecuredDependencies["creativeHandoff"]> {
  if (!dependencies.creativeHandoff) throw new Error("creative-editorial-dependencies-required");
  return dependencies.creativeHandoff;
}

async function a0SimulatedVideoBytes(directory: string): Promise<Buffer> {
  let pending = a0VideoFixtures.get(directory);
  if (!pending) {
    pending = createA0SimulatedVideo(directory);
    a0VideoFixtures.set(directory, pending);
  }
  try {
    return await pending;
  } catch (error) {
    a0VideoFixtures.delete(directory);
    throw error;
  }
}

async function createA0SimulatedVideo(directory: string): Promise<Buffer> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const binary = ffmpegPath;
  await mkdir(directory, { recursive: true });
  const finalPath = join(directory, "a0-simulated-phone-return.mp4");
  try {
    const existing = await readFile(finalPath);
    if (existing.length > 0) return existing;
  } catch { /* generate the deterministic local fixture below */ }
  const pendingPath = `${finalPath}.pending-${randomUUID()}.mp4`;
  await new Promise<void>((resolve, reject) => {
    const child = spawn(binary, [
      "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
      "color=c=0x16322a:s=160x120:r=25:d=1.5", "-c:v", "libx264", "-pix_fmt",
      "yuv420p", "-movflags", "+faststart", "-y", pendingPath,
    ], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (data) => { stderr += String(data); });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(
      new Error(`a0-video-generation-failed:${stderr}`),
    ));
  });
  try { await rename(pendingPath, finalPath); }
  catch {
    const winner = await readFile(finalPath).catch(() => null);
    if (!winner?.length) throw new Error("a0-video-generation-failed");
  }
  return readFile(finalPath);
}

async function sendMediaFile(
  response: ServerResponse,
  path: string,
  contentType = "video/mp4",
): Promise<void> {
  let metadata;
  try {
    metadata = await stat(path);
    if (!metadata.isFile() || metadata.size === 0) throw new Error("empty");
  } catch {
    throw new Error("artifact-media-unavailable");
  }
  response.writeHead(200, {
    "content-type": contentType,
    "content-length": metadata.size,
    "accept-ranges": "none",
    "x-content-type-options": "nosniff",
  });
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.once("error", reject);
    stream.once("end", resolve);
    stream.pipe(response);
  });
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const declaredLength = Number(request.headers["content-length"] ?? 0);
  let tooLarge = declaredLength > MAX_BODY_BYTES;
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) {
      tooLarge = true;
      chunks.length = 0;
    } else if (!tooLarge) {
      chunks.push(buffer);
    }
  }
  if (tooLarge) throw new Error("request-body-too-large");
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("invalid-json-body");
    }
    return value as Record<string, unknown>;
  } catch (error) {
    if (error instanceof Error && error.message === "invalid-json-body") throw error;
    throw new Error("invalid-json-body");
  }
}

function requireString(value: unknown, code: string): string {
  if (typeof value !== "string" || value.trim().length === 0) throw new Error(code);
  return value;
}

function requireCreativeOperationKey(value: string | string[] | undefined): string {
  return requireOpaqueIdentifier(
    value,
    "creative-operation-key-required",
    "creative-operation-key-invalid",
  );
}

function requireOpaqueIdentifier(
  value: string | string[] | undefined,
  missingCode: string,
  invalidCode: string,
): string {
  if (value === undefined) throw new Error(missingCode);
  if (typeof value !== "string" || value.length < 8 || value.length > 200 ||
    value.trim() !== value || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)) {
    throw new Error(invalidCode);
  }
  return value;
}

function creativeAuthScope(dependencies: SecuredDependencies): string {
  return canonicalJson({
    designerId: dependencies.designerId,
    reviewToken: dependencies.reviewToken,
  });
}

function creativeRequestScope(pathname: string, body: Record<string, unknown>): string {
  if (pathname === "/api/creative-sessions") {
    return `customer:${typeof body.customerId === "string" ? body.customerId : ""}`;
  }
  const sessionMatch = pathname.match(/^\/api\/creative-sessions\/([^/]+)/);
  return sessionMatch ? `session:${sessionMatch[1]}` : "creative-route";
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function requireStringArray(value: unknown): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error("still-paths-invalid");
  }
  return value;
}

async function sendStatic(
  response: ServerResponse,
  filename: string,
  contentType: string,
): Promise<void> {
  const path = new URL(`../public/${filename}`, import.meta.url);
  const body = await readFile(path);
  response.writeHead(200, {
    "content-type": contentType,
    "content-length": body.length,
  });
  response.end(body);
}

function sendJson(response: ServerResponse, status: number, value: unknown): void {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.length,
  });
  response.end(body);
}

function sendDomainError(response: ServerResponse, error: unknown): void {
  const result = domainErrorResponse(error);
  sendJson(response, result.status, result.body);
}

function domainErrorResponse(error: unknown): OperationResponse {
  const message = error instanceof Error ? error.message : "internal-error";
  const code = /^[a-z0-9-]+/.exec(message)?.[0] ?? "internal-error";
  const status = code.endsWith("not-found")
    ? 404
    : code === "creative-operation-key-conflict" ||
        code === "creative-editorial-manual-confirmation-required" ||
        code === "creative-editorial-manual-task-not-current" ||
        code === "creative-editorial-manual-authorization-conflict" ||
        code === "creative-editorial-manual-authorization-invalid" ||
        code === "creative-client-claim-invalid" ||
        code === "creative-client-recovery-required" ||
        code.startsWith("customer-credential-")
      ? 409
      : RETRYABLE_DOMAIN_ERROR_CODES.has(code)
        ? 503
        : code === "internal-error"
          ? 500
          : 400;
  return {
    status,
    body: code === "creative-client-recovery-required"
      ? {
          error: code,
          recovery: {
            reason: "server-owner-state-unavailable",
            automaticRetryAllowed: false,
          },
        }
      : { error: code },
  };
}
