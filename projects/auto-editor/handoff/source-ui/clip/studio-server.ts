import { createHash, randomUUID } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { accessSync, constants, lstatSync, mkdirSync, realpathSync } from "node:fs";
import { lstat, mkdir, open, readFile, realpath, rename, rm, stat } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import {
  CustomerMaterialLibrary,
  ExternalEditHandoffService,
  ExternalEditInbox,
  ExternalEditPackageBuilder,
  ExternalEditReturnService,
  JianyingDesktopBridge,
  JsonReferenceSampleStore,
  LocalReferenceGrammarAdapter,
  LocalProcessStudioModelWorker,
  OFFLINE_STUDIO_CAPABILITY,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  ReferenceGuidedEditor,
  ReferenceSampleService,
  ReferenceWithdrawalCoordinator,
  StudioEmbeddingIndex,
  StudioPreferenceRanker,
  createFfprobeStudioSourceIntervalResolver,
  detectStudioBeatEvidence,
  createReferenceMaterialMappingPlan,
  interpretReferencePromptRevision,
  renderReferenceVariants,
  type ReferenceEditorRenderRequest,
  type LocalProcessStudioModelWorkerConfig,
  type ReferenceMappingAssetSnapshot,
  type ReferenceRenderAsset,
  type JianyingAppInfo,
  type JianyingDesktopAutomation,
  type StudioModelWorker,
  type EditorialDecisionAdapter,
  type RecordCustomerFeedbackInput,
  type StudioDurableStateV1,
  type StudioScope,
} from "@wearable/mobile-agent";
import {
  CustomerDraftFeedbackSchema,
  STUDIO_INPUT_LIMITS,
  StudioAnalysisLayerSchema,
  StudioCaptionCueSchema,
  StudioContentTypeSchema,
  StudioCaptionStyleSchema,
  ModelAnalysisJobV1Schema,
  ModelAnalysisReceiptV1Schema,
  type ContentDemand,
  type DraftPackage,
  type MaterialCard,
  type ModelAnalysisJobV1,
  type ModelAnalysisReceiptV1,
  type StudioAnalysisLayer,
  type StudioContentType,
  type StudioMediaAsset,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";

import {
  assertExactBodyFields,
  createStudioReferenceApi,
  type StudioReferenceApi,
  type StudioReferenceRuntime,
  type StudioReferenceMediaProcessLimits,
  type StudioReferenceStagingHooks,
  type StudioReferenceUploadLimits,
} from "./studio-reference-api";
import {
  createStudioMaterialImportApi,
  normalizeUploadedFileName,
  readStudioMaterialMultipart,
  type StudioMaterialImportApi,
} from "./studio-material-import";
import {
  StudioMaterialTagStore,
  type StudioMaterialTagState,
} from "./studio-material-tags";
import { StudioHardwarePairingStore } from "./studio-hardware-pairing";
import { StudioHardwareInboxStore } from "./studio-hardware-inbox";
import {
  createModelCheckerStatusReader,
  StudioModelRuntime,
  type StudioAnalysisReviewInput,
  type StudioMaterialSuitabilityInput,
  type StudioModelStatusReader,
} from "./studio-model-runtime";
import { STUDIO_RELEASE } from "./studio-release";
import { automateJianyingOnMac } from "./jianying-macos-automation";
import { StudioMusicLibrary } from "./studio-music-library";
import { recommendStudioMusic } from "./studio-music-recommender";
import {
  isEligibleHighlightMaterial,
  StudioHighlightEvaluationStore,
  summarizeHighlightBatch,
} from "./studio-highlight-evaluation";
import { buildStudioHighlightFeedbackDataset } from "./studio-highlight-feedback-dataset";
import { buildStudioCaptionFeedbackDataset } from "./studio-caption-feedback-dataset";
import { writeStudioActiveSession } from "./studio-active-session";

// @ts-expect-error The hardened registry is an ESM script consumed directly by the app runtime.
import { FORMAL_WORKER_RUNTIME_ID, resolveFormalWorkerRuntimeConfig as resolveInstalledFormalWorkerRuntime } from "../../../scripts/studio-worker-runtime-registry.mjs";

export type StudioCustomer = Readonly<{
  id: string;
  label: string;
}>;

export type StudioServerOptions = Readonly<{
  rootDir: string;
  customers?: readonly StudioCustomer[];
  now?: () => string;
  modelWorker?: StudioModelWorker;
  modelStatusReader?: StudioModelStatusReader;
  referenceRuntimeFactory?: StudioReferenceRuntimeFactory;
  referenceUploadLimits?: StudioReferenceUploadLimits;
  referenceMediaTools?: Readonly<{ ffprobePath: string; ffmpegPath: string }>;
  referenceMediaProcessLimits?: StudioReferenceMediaProcessLimits;
  referenceStagingHooks?: StudioReferenceStagingHooks;
  openExternalEditTarget?: (input: { kind: "jianying" | "folder"; target: string }) => Promise<void>;
  findJianyingApp?: () => Promise<JianyingAppInfo | null>;
  automateJianying?: JianyingDesktopAutomation;
  hardwareBaseUrl?: string;
  hardwarePairingEnabled?: boolean;
  editorial?: EditorialDecisionAdapter;
}>;

export type StudioReferenceRuntimeFactory = (
  input: Readonly<{
    scope: StudioScope;
    sessionRoot: string;
    library: CustomerMaterialLibrary;
    studio: PrivateContentStudio;
    model: StudioModelRuntime;
    now: () => string;
  }>,
) => StudioReferenceRuntime | Promise<StudioReferenceRuntime>;

type StudioRuntime = Readonly<{
  token: string;
  scope: StudioScope;
  customer: StudioCustomer;
  sessionRoot: string;
  library: CustomerMaterialLibrary;
  studio: PrivateContentStudio;
  editorialCapability: EditorialDecisionAdapter["capability"];
  model: StudioModelRuntime;
  referenceApi: StudioReferenceApi;
  materialImportApi: StudioMaterialImportApi;
  musicLibrary: StudioMusicLibrary;
  hardwareInbox: StudioHardwareInboxStore;
  materialTags: StudioMaterialTagStore;
  highlights: StudioHighlightEvaluationStore;
  referenceEditor: StudioReferenceRuntime["editor"];
  externalHandoffs: ExternalEditHandoffService;
  externalRoot: string;
  externalInbox: ExternalEditInbox;
  externalReturns: ExternalEditReturnService;
  externalEditBridges: Map<string, { bridge: JianyingDesktopBridge; prepared: Awaited<ReturnType<JianyingDesktopBridge["prepare"]>> }>;
  persistence: { tail: Promise<void> };
}>;

type StudioSessionDescriptorV1 = Readonly<{
  schemaVersion: 1;
  actorId: string;
  customerId: string;
  sessionId: string;
}>;

const MAX_BODY_BYTES = 1024 * 1024;
export const MODEL_WORKER_OUTER_TIMEOUT_MS = 330_000;
const DEFAULT_FORMAL_WORKER_RUNTIME_ID = FORMAL_WORKER_RUNTIME_ID;
const DEFAULT_SANDBOX_PATH = "/usr/bin/sandbox-exec";
const STABLE_FORMAL_RUNTIME_ERRORS = new Set([
  "studio-formal-worker-runtime-unavailable",
  "studio-formal-worker-runtime-unapproved",
  "studio-formal-worker-runtime-integrity-failed",
  "studio-formal-worker-runtime-probe-required",
  "studio-formal-worker-runtime-profile-mismatch",
  "studio-formal-worker-sandbox-unavailable",
]);
const bundledFfprobePath = (
  createRequire(import.meta.url)("ffprobe-static") as { path?: unknown }
).path;
const DEFAULT_CUSTOMERS: readonly StudioCustomer[] = Object.freeze([
  Object.freeze({ id: "synthetic-customer-a", label: "合成客户 A" }),
  Object.freeze({ id: "synthetic-customer-b", label: "合成客户 B" }),
]);
const REFERENCE_INTERNAL_INTEGRITY_ERROR_CODES = new Set([
  "reference-analysis-final-attempt-layer-invalid",
  "reference-analysis-final-attempt-not-succeeded",
  "reference-analysis-scene-not-succeeded",
  "reference-scene-evidence-invalid",
  "reference-transition-evidence-invalid",
  "reference-audio-evidence-invalid",
  "reference-transcript-evidence-invalid",
  "reference-vision-evidence-invalid",
  "reference-bounded-evidence-flag-invalid",
  "reference-evidence-millisecond-invalid",
]);

export function createStudioServer(options: StudioServerOptions) {
  if (!options.rootDir.trim()) throw new Error("studio-root-required");
  const customers = new Map((options.customers ?? DEFAULT_CUSTOMERS).map((customer) => {
    if (!customer.id.trim() || !customer.label.trim()) throw new Error("studio-customer-invalid");
    return [customer.id, customer] as const;
  }));
  const sessions = new Map<string, StudioRuntime>();
  const browserConnections = new Map<string, Readonly<{
    runtimeToken: string;
    expiresAtMs: number;
  }>>();
  const now = options.now ?? (() => new Date().toISOString());
  const hardwarePairings = new StudioHardwarePairingStore(
    options.hardwareBaseUrl ?? "http://127.0.0.1:4273",
    now,
  );

  return createServer(async (request, response) => {
    try {
      await routeStudioRequest(request, response, {
        rootDir: options.rootDir,
        customers,
        sessions,
        browserConnections,
        hardwarePairings,
        hardwarePairingEnabled: options.hardwarePairingEnabled,
        now,
        modelWorker: options.modelWorker,
        modelStatusReader: options.modelStatusReader,
        referenceRuntimeFactory: options.referenceRuntimeFactory,
        referenceUploadLimits: options.referenceUploadLimits,
        referenceMediaTools: options.referenceMediaTools,
        referenceMediaProcessLimits: options.referenceMediaProcessLimits,
        referenceStagingHooks: options.referenceStagingHooks,
        editorial: options.editorial,
        openExternalEditTarget: options.openExternalEditTarget,
        findJianyingApp: options.findJianyingApp,
        automateJianying: options.automateJianying
          ?? (options.openExternalEditTarget || options.findJianyingApp ? undefined : automateJianyingOnMac),
      });
    } catch (error) {
      sendStudioError(response, error);
    }
  });
}

export function listenStudioServer(
  server: ReturnType<typeof createStudioServer>,
  port = 4273,
  host = "127.0.0.1",
): Promise<void> {
  if (!isStudioListenAddress(host)) return Promise.reject(new Error("studio-listen-host-required"));
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolve());
  });
}

export function parseStudioPort(value: string | undefined): number {
  if (value === undefined) return 4273;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("invalid-studio-port");
  }
  return port;
}

async function routeStudioRequest(
  request: IncomingMessage,
  response: ServerResponse,
  dependencies: Readonly<{
    rootDir: string;
    customers: ReadonlyMap<string, StudioCustomer>;
    sessions: Map<string, StudioRuntime>;
    browserConnections: Map<string, Readonly<{
      runtimeToken: string;
      expiresAtMs: number;
    }>>;
    hardwarePairings: StudioHardwarePairingStore;
    hardwarePairingEnabled: boolean | undefined;
    now: () => string;
    modelWorker: StudioModelWorker | undefined;
    modelStatusReader: StudioModelStatusReader | undefined;
    referenceRuntimeFactory: StudioReferenceRuntimeFactory | undefined;
    referenceUploadLimits: StudioReferenceUploadLimits | undefined;
    referenceMediaTools: Readonly<{ ffprobePath: string; ffmpegPath: string }> | undefined;
    referenceMediaProcessLimits: StudioReferenceMediaProcessLimits | undefined;
    referenceStagingHooks: StudioReferenceStagingHooks | undefined;
    editorial: EditorialDecisionAdapter | undefined;
    openExternalEditTarget: StudioServerOptions["openExternalEditTarget"];
    findJianyingApp: StudioServerOptions["findJianyingApp"];
    automateJianying: StudioServerOptions["automateJianying"];
  }>,
): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const host = request.headers.host ?? "";

  if (method === "GET" && url.pathname === "/hardware/pair") {
    requirePrivateHardwareHost(host);
    return sendHardwarePairingPage(response, url.searchParams.get("code") ?? "");
  }
  if (method === "POST" && url.pathname === "/api/hardware/pair") {
    requirePrivateHardwareHost(host);
    requireHardwareSameOrigin(request, host);
    requireJsonContentType(request);
    const body = await readJsonBody(request);
    assertExactBodyFields(body, ["code", "deviceName"]);
    const binding = dependencies.hardwarePairings.claim(
      requireString(body.code, "hardware-pairing-code-required"),
      requireString(body.deviceName, "hardware-device-name-required"),
    );
    return sendJson(response, 201, { binding });
  }
  if (method === "POST" && url.pathname === "/api/hardware/inbox/receive") {
    requirePrivateHardwareHost(host);
    requireHardwareSameOrigin(request, host);
    const binding = dependencies.hardwarePairings.authorize(request.headers.authorization);
    const runtime = dependencies.sessions.get(binding.runtimeToken);
    if (!runtime) throw new Error("hardware-session-unavailable");
    assertSameScope(runtime.scope, binding.scope);
    const item = await runtime.hardwareInbox.receiveMultipart(request, runtime.scope, binding);
    return sendJson(response, 201, { item });
  }
  if (method === "GET" && url.pathname === "/api/hardware/inbox") {
    requirePrivateHardwareHost(host);
    const binding = dependencies.hardwarePairings.authorize(request.headers.authorization);
    const runtime = dependencies.sessions.get(binding.runtimeToken);
    if (!runtime) throw new Error("hardware-session-unavailable");
    assertSameScope(runtime.scope, binding.scope);
    return sendJson(response, 200, { items: await runtime.hardwareInbox.list(runtime.scope, binding) });
  }
  if (method === "POST" && url.pathname === "/api/hardware/inbox/confirm") {
    requirePrivateHardwareHost(host);
    requireHardwareSameOrigin(request, host);
    requireJsonContentType(request);
    const binding = dependencies.hardwarePairings.authorize(request.headers.authorization);
    const runtime = dependencies.sessions.get(binding.runtimeToken);
    if (!runtime) throw new Error("hardware-session-unavailable");
    assertSameScope(runtime.scope, binding.scope);
    const body = await readJsonBody(request);
    assertExactBodyFields(body, ["id"]);
    const confirmed = await runtime.hardwareInbox.confirm(
      requireString(body.id, "hardware-inbox-item-id-required"),
      runtime.scope,
      binding,
    );
    await persistRuntime(runtime);
    const [visibleMaterial] = await publicMaterials(runtime, [confirmed.material]);
    return sendJson(response, 200, { item: confirmed.item, material: visibleMaterial });
  }
  if (!isLoopbackHost(host)) throw new Error("studio-loopback-host-required");

  if (method === "GET" && url.pathname === "/studio/connect") {
    const code = url.searchParams.get("code") ?? "";
    if (!/^[0-9a-f-]{36}$/u.test(code)) throw new Error("studio-browser-connection-not-found");
    const connection = dependencies.browserConnections.get(code);
    dependencies.browserConnections.delete(code);
    if (!connection || connection.expiresAtMs < Date.now()) {
      throw new Error("studio-browser-connection-not-found");
    }
    if (!dependencies.sessions.has(connection.runtimeToken)) {
      throw new Error("studio-session-token-invalid");
    }
    response.writeHead(303, {
      location: "/studio",
      "set-cookie": `studio_session=${connection.runtimeToken}; HttpOnly; SameSite=Strict; Path=/`,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    });
    response.end();
    return;
  }

  if (method === "GET" && url.pathname === "/studio") {
    return sendStatic(response, "studio.html", "text/html; charset=utf-8");
  }
  if (method === "GET" && url.pathname === "/studio.js") {
    return sendStatic(response, "studio.js", "text/javascript; charset=utf-8");
  }
  if (method === "GET" && url.pathname === "/studio.css") {
    return sendStatic(response, "studio.css", "text/css; charset=utf-8");
  }

  if (method === "GET" && url.pathname.startsWith("/api/studio/external-edit-handoffs")) {
    requireEmptyReadBody(request);
    requireReadOrigin(request, host);
    return routeExternalEditGet(request, response, url.pathname, dependencies.sessions);
  }

  if (method === "POST") {
    requireSameOrigin(request, host);
    const externalCandidateImportMatch = url.pathname.match(
      /^\/api\/studio\/external-edit-handoffs\/([^/]+)\/candidates\/import$/,
    );
    if (externalCandidateImportMatch) {
      const runtime = requireRuntime(request, dependencies.sessions);
      const handoffId = decodeURIComponent(externalCandidateImportMatch[1]!);
      const handoff = await runtime.externalHandoffs.get(runtime.scope, handoffId);
      if (!["editing", "candidate-found", "degraded"].includes(handoff.status)) {
        throw new Error("external-edit-handoff-not-awaiting-return");
      }
      const upload = await readStudioMaterialMultipart(request);
      const imported = await runtime.externalInbox.importCandidate(handoff, {
        bytes: upload.bytes,
        fileName: upload.fileName,
      });
      const candidate = (await runtime.externalReturns.listCandidates(runtime.scope, handoffId))
        .find((item) => item.id === imported.id);
      if (!candidate) throw new Error("external-edit-upload-candidate-invalid");
      return sendJson(response, 201, { candidate: publicExternalEditCandidate(handoffId, candidate) });
    }
    if (url.pathname === "/api/studio/reference-samples/import") {
      const runtime = requireRuntime(request, dependencies.sessions);
      const result = await runtime.referenceApi.importMultipart(request, runtime.scope);
      return sendJson(response, result.status, result.body);
    }
    if (url.pathname === "/api/studio/materials/import") {
      const runtime = requireRuntime(request, dependencies.sessions);
      const material = await runtime.materialImportApi.importMultipart(request, runtime.scope);
      const inventory = await runtime.studio.inventory(runtime.scope);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const [visibleMaterial] = await publicMaterials(runtime, [material]);
      return sendJson(response, 201, {
        material: visibleMaterial,
        inventory,
        materials: await publicMaterials(runtime, materials),
        materialTags: visibleMaterialTagState(await runtime.materialTags.list(), materials),
      });
    }
    if (url.pathname === "/api/studio/music/import") {
      const runtime = requireRuntime(request, dependencies.sessions);
      const imported = await runtime.musicLibrary.importMultipart(request, runtime.scope.sessionId);
      return sendJson(response, 201, {
        track: publicMusicTrack(imported.track),
        ...publicMusicState(runtime.musicLibrary, musicSelectionKey(runtime.scope, imported.briefId)),
      });
    }
    requireJsonContentType(request);
    const body = await readJsonBody(request);

    if (url.pathname === "/api/studio/browser-connections") {
      assertExactBodyFields(body, []);
      const runtime = requireRuntime(request, dependencies.sessions);
      const currentTime = Date.now();
      for (const [code, connection] of dependencies.browserConnections) {
        if (connection.expiresAtMs < currentTime) dependencies.browserConnections.delete(code);
      }
      const code = randomUUID();
      const expiresAtMs = currentTime + 5 * 60 * 1000;
      dependencies.browserConnections.set(code, {
        runtimeToken: runtime.token,
        expiresAtMs,
      });
      return sendJson(response, 201, {
        connectUrl: `/studio/connect?code=${code}`,
        expiresAt: new Date(expiresAtMs).toISOString(),
        oneTime: true,
      });
    }

    if (url.pathname === "/api/studio/session/resume") {
      assertExactBodyFields(body, []);
      const token = requireRuntimeToken(request);
      let runtime = dependencies.sessions.get(token);
      if (!runtime) {
        runtime = await restoreRuntime(
          dependencies.rootDir,
          token,
          dependencies.customers,
          dependencies.now,
          dependencies.modelWorker,
          dependencies.modelStatusReader,
          dependencies.referenceRuntimeFactory,
          dependencies.referenceUploadLimits,
          dependencies.referenceMediaTools,
          dependencies.referenceMediaProcessLimits,
          dependencies.referenceStagingHooks,
          dependencies.editorial,
        );
        dependencies.sessions.set(token, runtime);
      }
      await markActiveRuntime(dependencies.rootDir, runtime, dependencies.now());
      return sendJson(response, 200, {
        resumed: true,
        session: {
          token: runtime.token,
          ...runtime.scope,
          customerLabel: runtime.customer.label,
        },
        capability: runtime.editorialCapability,
        release: STUDIO_RELEASE,
        customers: [...dependencies.customers.values()],
        automaticPublishing: false,
      });
    }

    if (url.pathname === "/api/studio/session") {
      const customerId = requireString(body.customerId, "studio-customer-required");
      const customer = dependencies.customers.get(customerId);
      if (!customer) throw new Error("studio-customer-not-allowed");
      const runtime = await createRuntime(
        dependencies.rootDir,
        customer,
        dependencies.now,
        dependencies.modelWorker,
        dependencies.modelStatusReader,
        dependencies.referenceRuntimeFactory,
        dependencies.referenceUploadLimits,
        dependencies.referenceMediaTools,
        dependencies.referenceMediaProcessLimits,
        dependencies.referenceStagingHooks,
        dependencies.editorial,
      );
      dependencies.sessions.set(runtime.token, runtime);
      await persistRuntime(runtime);
      await markActiveRuntime(dependencies.rootDir, runtime, dependencies.now());
      response.setHeader(
        "set-cookie",
        `studio_session=${runtime.token}; HttpOnly; SameSite=Strict; Path=/`,
      );
      return sendJson(response, 201, {
        session: {
          token: runtime.token,
          ...runtime.scope,
          customerLabel: customer.label,
        },
        capability: runtime.editorialCapability,
        release: STUDIO_RELEASE,
        customers: [...dependencies.customers.values()],
        automaticPublishing: false,
      });
    }

    const runtime = requireRuntime(request, dependencies.sessions);
    await markActiveRuntime(dependencies.rootDir, runtime, dependencies.now());
    assertBoundScope(body, runtime.scope);
    if (url.pathname === "/api/studio/music/list") {
      assertExactBodyFields(body, ["briefId"]);
      const briefId = body.briefId === null ? null : requireString(body.briefId, "brief-id-required");
      return sendJson(response, 200, publicMusicState(runtime.musicLibrary, musicSelectionKey(runtime.scope, briefId)));
    }
    if (url.pathname === "/api/studio/music/select") {
      assertExactBodyFields(body, ["briefId", "trackId"]);
      const briefId = body.briefId === null ? null : requireString(body.briefId, "brief-id-required");
      const trackId = body.trackId === null ? null : requireString(body.trackId, "studio-music-track-id-required");
      const selectionKey = musicSelectionKey(runtime.scope, briefId);
      const brief = briefId === null ? null : await runtime.studio.getBrief(runtime.scope, briefId);
      await runtime.musicLibrary.select(trackId, selectionKey, brief === null ? null : {
        briefId: brief.id, contentType: brief.contentType,
      });
      return sendJson(response, 200, publicMusicState(runtime.musicLibrary, selectionKey));
    }
    if (url.pathname === "/api/studio/music/mix") {
      assertExactBodyFields(body, ["briefId", "trackId", "trackStartMs", "originalGainDb", "musicGainDb"]);
      const briefId = body.briefId === null ? null : requireString(body.briefId, "brief-id-required");
      const brief = briefId === null ? null : await runtime.studio.getBrief(runtime.scope, briefId);
      await runtime.musicLibrary.updateMixPreference(
        requireString(body.trackId, "studio-music-track-id-required"),
        {
          trackStartMs: body.trackStartMs,
          originalGainDb: body.originalGainDb,
          musicGainDb: body.musicGainDb,
        },
        musicSelectionKey(runtime.scope, briefId),
        brief === null ? null : { briefId: brief.id, contentType: brief.contentType },
      );
      return sendJson(response, 200, publicMusicState(runtime.musicLibrary, musicSelectionKey(runtime.scope, briefId)));
    }
    if (url.pathname === "/api/studio/music/recommend") {
      assertExactBodyFields(body, ["briefId"]);
      const brief = await runtime.studio.getBrief(
        runtime.scope, requireString(body.briefId, "brief-id-required"),
      );
      const cards = (await currentSessionMaterials(runtime, dependencies.now()))
        .filter((card) => brief.selectedAssetIds.includes(card.sourceAssetId));
      let visualEvidence: Awaited<ReturnType<StudioModelRuntime["ensureEditorialVisualEvidence"]>> = [];
      try {
        visualEvidence = await runtime.model.ensureEditorialVisualEvidence(cards);
      } catch {
        // Music ranking remains available as an explicitly labelled technical fallback.
      }
      return sendJson(response, 200, {
        recommendations: recommendStudioMusic({
          brief,
          tracks: runtime.musicLibrary.list(musicSelectionKey(runtime.scope, brief.id)).tracks,
          visualEvidence,
          preferences: runtime.musicLibrary.preferenceSummaries(brief.contentType),
        }),
      });
    }
    if (url.pathname === "/api/studio/hardware/pairings") {
      assertExactBodyFields(body, []);
      if (dependencies.hardwarePairingEnabled === false) throw new Error("hardware-lan-disabled");
      const pairing = await dependencies.hardwarePairings.create(runtime.token, runtime.scope);
      return sendJson(response, 201, { pairing });
    }
    if (url.pathname === "/api/studio/hardware/pairings/status") {
      assertExactBodyFields(body, ["pairingId"]);
      const pairing = dependencies.hardwarePairings.status(
        runtime.token,
        requireString(body.pairingId, "hardware-pairing-id-required"),
      );
      return sendJson(response, 200, { pairing });
    }
    if (url.pathname === "/api/studio/material-tags/list") {
      assertExactBodyFields(body, []);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      return sendJson(response, 200, {
        materialTags: visibleMaterialTagState(await runtime.materialTags.list(), materials),
      });
    }
    if (url.pathname === "/api/studio/highlights/list") {
      assertExactBodyFields(body, []);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      return sendJson(response, 200, await currentHighlightSnapshot(runtime, materials));
    }
    if (url.pathname === "/api/studio/highlights/report") {
      assertExactBodyFields(body, []);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const { attempts, evaluations, profiles, rejections, batch } = await currentHighlightSnapshot(runtime, materials);
      return sendJson(response, 200, {
        report: {
          schemaVersion: 1,
          generatedAt: dependencies.now(),
          gate: batch,
          samples: [...new Map(evaluations
            .filter((evaluation) => evaluation.labels.length > 0 && isEligibleHighlightEvaluation(evaluation, materials))
            .map((evaluation) => [
              `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`,
              evaluation,
            ])).values()]
            .map((evaluation) => ({
              sourceAssetId: evaluation.sourceAssetId,
              sourceDigest: evaluation.sourceDigest,
              sourceStartMs: evaluation.sourceStartMs,
              sourceEndMs: evaluation.sourceEndMs,
              status: evaluation.status,
              labels: evaluation.labels,
              modelCandidates: evaluation.modelCandidates,
              scores: evaluation.scores,
              coverageProfile: profiles.find((item) =>
                item.sourceAssetId === evaluation.sourceAssetId
                && item.sourceDigest === evaluation.sourceDigest) ?? null,
              attempt: attempts.find((item) =>
                item.sourceAssetId === evaluation.sourceAssetId
                && item.sourceDigest === evaluation.sourceDigest) ?? null,
            })),
          candidateRejections: rejections,
          productionAccepted: false,
          automaticPublishing: false,
          boundary: "local-pilot-evidence-only",
        },
      });
    }
    if (url.pathname === "/api/studio/highlights/feedback-dataset") {
      assertExactBodyFields(body, []);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const snapshot = await currentHighlightSnapshot(runtime, materials);
      const evaluations = snapshot.evaluations.filter((evaluation) =>
        isEligibleHighlightEvaluation(evaluation, materials));
      const eligibleKeys = new Set(evaluations.map((evaluation) =>
        `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`));
      const rejections = snapshot.rejections.filter((rejection) =>
        eligibleKeys.has(`${rejection.sourceAssetId}\0${rejection.sourceDigest}`));
      const totalFeedbackRecords = snapshot.evaluations.reduce(
        (total, evaluation) => total + evaluation.labels.length, 0,
      ) + snapshot.rejections.length;
      const eligibleFeedbackRecords = evaluations.reduce(
        (total, evaluation) => total + evaluation.labels.length, 0,
      ) + rejections.length;
      return sendJson(response, 200, {
        dataset: buildStudioHighlightFeedbackDataset({
          evaluations,
          rejections,
          excludedUnverifiedSourceRecords: Math.max(0, totalFeedbackRecords - eligibleFeedbackRecords),
          generatedAt: dependencies.now(),
        }),
      });
    }
    if (url.pathname === "/api/studio/highlights") {
      assertExactBodyFields(body, ["materialCardId", "sourceStartMs", "sourceEndMs", "reason", "humanConfirmed", "operationKey"]);
      if (body.humanConfirmed !== true) throw new Error("studio-highlight-human-confirmation-required");
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const materialCardId = requireString(body.materialCardId, "studio-highlight-card-id-required");
      const card = materials.find((item) => item.id === materialCardId);
      if (!card) throw new Error("studio-highlight-card-not-found");
      const label = await runtime.highlights.record({
        card,
        sourceStartMs: body.sourceStartMs as number,
        sourceEndMs: body.sourceEndMs as number,
        reason: body.reason as string,
        operationKey: body.operationKey as string,
      });
      const snapshot = await currentHighlightSnapshot(runtime, materials);
      return sendJson(response, 201, {
        label,
        ...snapshot,
      });
    }
    if (url.pathname === "/api/studio/highlights/accept-candidate") {
      assertExactBodyFields(body, [
        "materialCardId", "receiptJobId", "sourceStartMs", "sourceEndMs", "humanConfirmed", "operationKey",
      ]);
      if (body.humanConfirmed !== true) throw new Error("studio-highlight-human-confirmation-required");
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const materialCardId = requireString(body.materialCardId, "studio-highlight-card-id-required");
      const card = materials.find((item) => item.id === materialCardId);
      if (!card) throw new Error("studio-highlight-card-not-found");
      const receiptJobId = requireString(body.receiptJobId, "studio-highlight-candidate-receipt-required");
      const snapshot = await currentHighlightSnapshot(runtime, materials);
      const evaluation = snapshot.evaluations.find((item) =>
        item.sourceAssetId === card.sourceAssetId && item.sourceDigest === card.sourceDigest);
      const candidate = evaluation?.modelCandidates.find((item) =>
        item.receiptJobId === receiptJobId
        && item.startMs === body.sourceStartMs
        && item.endMs === body.sourceEndMs);
      if (!candidate) throw new Error("studio-highlight-candidate-binding-mismatch");
      if (snapshot.rejections.some((item) =>
        item.sourceAssetId === card.sourceAssetId
        && item.sourceDigest === card.sourceDigest
        && item.receiptJobId === candidate.receiptJobId
        && item.sourceStartMs === candidate.startMs
        && item.sourceEndMs === candidate.endMs)) {
        throw new Error("studio-highlight-candidate-decision-conflict");
      }
      const label = await runtime.highlights.record({
        card,
        sourceStartMs: candidate.startMs,
        sourceEndMs: candidate.endMs,
        reason: `人工看过并确认模型候选：${candidate.text}`.slice(0, 500),
        confirmationMode: "accepted-model-candidate",
        modelReceiptJobId: candidate.receiptJobId,
        operationKey: requireString(body.operationKey, "studio-highlight-operation-key-required"),
      });
      return sendJson(response, 201, {
        label,
        ...await currentHighlightSnapshot(runtime, materials),
      });
    }
    if (url.pathname === "/api/studio/highlights/reject-candidate") {
      assertExactBodyFields(body, [
        "materialCardId", "receiptJobId", "sourceStartMs", "sourceEndMs", "humanConfirmed", "operationKey",
      ]);
      if (body.humanConfirmed !== true) throw new Error("studio-highlight-human-confirmation-required");
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const materialCardId = requireString(body.materialCardId, "studio-highlight-card-id-required");
      const card = materials.find((item) => item.id === materialCardId);
      if (!card) throw new Error("studio-highlight-card-not-found");
      const receiptJobId = requireString(body.receiptJobId, "studio-highlight-candidate-receipt-required");
      const snapshot = await currentHighlightSnapshot(runtime, materials);
      const evaluation = snapshot.evaluations.find((item) =>
        item.sourceAssetId === card.sourceAssetId && item.sourceDigest === card.sourceDigest);
      const candidate = evaluation?.modelCandidates.find((item) =>
        item.receiptJobId === receiptJobId
        && item.startMs === body.sourceStartMs
        && item.endMs === body.sourceEndMs);
      if (!candidate) throw new Error("studio-highlight-candidate-binding-mismatch");
      if (evaluation?.labels.some((item) =>
        item.confirmationMode === "accepted-model-candidate"
        && item.modelReceiptJobId === candidate.receiptJobId
        && item.sourceStartMs === candidate.startMs
        && item.sourceEndMs === candidate.endMs)) {
        throw new Error("studio-highlight-candidate-decision-conflict");
      }
      const rejection = await runtime.highlights.rejectCandidate({
        card,
        sourceStartMs: candidate.startMs,
        sourceEndMs: candidate.endMs,
        receiptJobId: candidate.receiptJobId,
        operationKey: requireString(body.operationKey, "studio-highlight-operation-key-required"),
      });
      return sendJson(response, 201, {
        rejection,
        ...await currentHighlightSnapshot(runtime, materials),
      });
    }
    if (url.pathname === "/api/studio/highlights/coverage") {
      assertExactBodyFields(body, ["materialCardId", "lighting", "motion", "environment", "humanConfirmed", "operationKey"]);
      if (body.humanConfirmed !== true) throw new Error("studio-highlight-human-confirmation-required");
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const materialCardId = requireString(body.materialCardId, "studio-highlight-card-id-required");
      const card = materials.find((item) => item.id === materialCardId);
      if (!card) throw new Error("studio-highlight-card-not-found");
      if (!isEligibleHighlightMaterial(card)) throw new Error("studio-highlight-eligible-material-required");
      const profile = await runtime.highlights.recordCoverage({
        card,
        lighting: body.lighting,
        motion: body.motion,
        environment: body.environment,
        operationKey: body.operationKey,
      });
      return sendJson(response, 201, {
        profile,
        ...await currentHighlightSnapshot(runtime, materials),
      });
    }
    if (url.pathname === "/api/studio/highlights/retest") {
      assertExactBodyFields(body, ["materialCardId"]);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const materialCardId = requireString(body.materialCardId, "studio-highlight-card-id-required");
      const card = materials.find((item) => item.id === materialCardId);
      if (!card) throw new Error("studio-highlight-card-not-found");
      await runtime.model.retestEditorialVisualEvidence([card]);
      return sendJson(response, 200, await currentHighlightSnapshot(runtime, materials));
    }
    if (url.pathname === "/api/studio/highlights/retest-batch") {
      assertExactBodyFields(body, []);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const before = await currentHighlightSnapshot(runtime, materials);
      const labeledKeys = new Set(before.evaluations
        .filter((evaluation) => evaluation.labels.length > 0)
        .map((evaluation) => `${evaluation.sourceAssetId}\0${evaluation.sourceDigest}`));
      const labeledCards = [...new Map(materials
        .filter((card) => isEligibleHighlightMaterial(card) && labeledKeys.has(`${card.sourceAssetId}\0${card.sourceDigest}`))
        .map((card) => [`${card.sourceAssetId}\0${card.sourceDigest}`, card])).values()];
      if (labeledCards.length === 0) throw new Error("studio-highlight-batch-label-required");
      await runtime.model.retestEditorialVisualEvidence(labeledCards);
      const result = await currentHighlightSnapshot(runtime, materials);
      return sendJson(response, 200, result);
    }
    if (url.pathname === "/api/studio/highlights/automatic-drafts") {
      assertExactBodyFields(body, ["briefId", "operationKey"]);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      const snapshot = await currentHighlightSnapshot(runtime, materials);
      if (!snapshot.batch.automaticEditingEligible) {
        throw new Error("studio-highlight-automatic-edit-gate-not-met");
      }
      const briefId = requireString(body.briefId, "brief-id-required");
      const brief = await runtime.studio.getBrief(runtime.scope, briefId);
      const evaluationBySource = new Map(snapshot.evaluations.map((item) => [item.sourceAssetId, item]));
      if (brief.selectedAssetIds.some((sourceAssetId) => evaluationBySource.get(sourceAssetId)?.status !== "passed")) {
        throw new Error("studio-highlight-selected-source-not-passed");
      }
      const drafts = await runtime.studio.createDrafts(
        runtime.scope,
        briefId,
        requireString(body.operationKey, "draft-operation-key-required"),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, {
        drafts: drafts.map(publicDraft),
        gate: snapshot.batch,
        reviewRequired: true,
        productionAccepted: false,
        automaticPublishing: false,
      });
    }
    if (url.pathname === "/api/studio/material-tags") {
      assertExactBodyFields(body, ["label", "operationKey"]);
      await runtime.materialTags.create(body.label, body.operationKey);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      return sendJson(response, 201, {
        materialTags: visibleMaterialTagState(await runtime.materialTags.list(), materials),
      });
    }
    if (url.pathname === "/api/studio/material-tags/assign") {
      assertExactBodyFields(body, ["materialCardId", "tagIds", "operationKey"]);
      const materialCardId = requireString(body.materialCardId, "material-tag-card-id-required");
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      if (!materials.some((material) => material.id === materialCardId)) {
        throw new Error("material-card-not-found");
      }
      await runtime.materialTags.assign(materialCardId, body.tagIds, body.operationKey);
      return sendJson(response, 200, {
        materialTags: visibleMaterialTagState(await runtime.materialTags.list(), materials),
      });
    }
    if (url.pathname.startsWith("/api/studio/external-edit-handoffs")) {
      return routeExternalEditPost(
        response,
        runtime,
        url.pathname,
        body,
        dependencies.now,
        dependencies.openExternalEditTarget,
        dependencies.findJianyingApp,
        dependencies.automateJianying,
        () => persistRuntime(runtime),
      );
    }
    const referenceResult = await runtime.referenceApi.dispatch({
      method,
      pathname: url.pathname,
      body,
      scope: runtime.scope,
      editor: runtime.referenceEditor,
    });
    if (referenceResult) return sendJson(response, referenceResult.status, referenceResult.body);
    if (url.pathname === "/api/studio/analysis/jobs") {
      assertExactBodyFields(body, ["assetId", "layers", "operationKey"]);
      const analysis = await runtime.model.start({
        assetId: requireString(body.assetId, "studio-analysis-asset-id-required"),
        layers: requireAnalysisLayers(body.layers),
        operationKey: requireString(body.operationKey, "studio-analysis-operation-key-required"),
      });
      return sendJson(response, 201, { analysis });
    }
    if (url.pathname === "/api/studio/analysis/history") {
      assertExactBodyFields(body, []);
      return sendJson(response, 200, { history: await runtime.model.history() });
    }
    if (url.pathname === "/api/studio/analysis/review") {
      assertExactBodyFields(body, [
        "action", "receiptJobId", "sourceAssetId", "sourceDigest",
        "sourceStartMs", "sourceEndMs", "target", "correction", "operationKey",
      ]);
      const review = await runtime.model.review(parseAnalysisReviewBody(body));
      return sendJson(response, 201, { review });
    }
    if (url.pathname === "/api/studio/analysis/suitability") {
      assertExactBodyFields(body, [
        "decision", "demandId", "matchAssessmentId", "receiptJobId", "sourceAssetId",
        "sourceDigest", "sourceStartMs", "sourceEndMs", "reason", "operationKey",
      ]);
      const suitability = await runtime.model.recordMaterialSuitability(
        parseMaterialSuitabilityBody(body),
      );
      return sendJson(response, 201, { suitability });
    }
    if (url.pathname === "/api/studio/models/status") {
      assertExactBodyFields(body, []);
      return sendJson(response, 200, { models: await runtime.model.modelsStatus() });
    }
    if (url.pathname === "/api/studio/rankings") {
      assertExactBodyFields(body, ["demandId"]);
      return sendJson(response, 200, {
        ranking: await runtime.model.rank(
          requireString(body.demandId, "demand-id-required"),
        ),
      });
    }
    if (url.pathname === "/api/studio/demands") {
      const demand = await runtime.studio.createDemand(runtime.scope, {
        contentType: requireStudioContentType(body.contentType),
        goal: requireString(body.goal, "studio-goal-required"),
        targetAudience: requireString(body.targetAudience, "studio-target-audience-required"),
        desiredEffect: requireString(body.desiredEffect, "studio-desired-effect-required"),
        mustInclude: requireStringArray(body.mustInclude, "studio-must-include-invalid", {
          tooManyCode: "demand-must-include-too-many",
          duplicateCode: "duplicate-must-include",
        }),
        mustAvoid: requireStringArray(body.mustAvoid, "studio-must-avoid-invalid", {
          tooManyCode: "demand-must-avoid-too-many",
          duplicateCode: "duplicate-must-avoid",
        }),
        submittedBy: requireDemandSubmitter(body.submittedBy),
        operationKey: requireString(body.operationKey, "demand-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { demand });
    }
    if (url.pathname === "/api/studio/matches") {
      const assessment = await runtime.studio.assessDemand(
        runtime.scope,
        requireString(body.demandId, "demand-id-required"),
        requireString(body.operationKey, "match-operation-key-required"),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, { assessment });
    }
    if (url.pathname === "/api/studio/customer-feedback") {
      const feedback = await runtime.studio.recordCustomerFeedback(
        runtime.scope,
        parseCustomerFeedbackBody(body),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, { feedback });
    }
    if (url.pathname === "/api/studio/revision-proposals") {
      const proposal = await runtime.studio.proposeRevision(
        runtime.scope,
        requireString(body.feedbackId, "feedback-id-required"),
        requireString(body.operationKey, "proposal-operation-key-required"),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, { proposal });
    }
    if (url.pathname === "/api/studio/revision-proposals/confirm") {
      const brief = await runtime.studio.confirmRevisionProposal(
        runtime.scope,
        requireString(body.proposalId, "proposal-id-required"),
        requireString(body.operationKey, "proposal-confirmation-operation-key-required"),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, { brief });
    }
    if (url.pathname === "/api/studio/captions/feedback-dataset") {
      assertExactBodyFields(body, []);
      const history = await runtime.studio.listStudioHistory(runtime.scope);
      const revisedDrafts = history.drafts.filter((draft) => draft.captionRevision !== undefined);
      const feedbackPairs = await Promise.all(revisedDrafts.map(async (draft) => [
        draft.id,
        await runtime.studio.listFeedback(runtime.scope, draft.id),
      ] as const));
      return sendJson(response, 200, {
        dataset: buildStudioCaptionFeedbackDataset({
          drafts: history.drafts,
          feedbackByDraftId: Object.fromEntries(feedbackPairs),
          generatedAt: dependencies.now(),
        }),
      });
    }
    if (url.pathname === "/api/studio/history") {
      assertNoClientScope(body, runtime.scope);
      const history = await runtime.studio.listStudioHistory(runtime.scope);
      return sendJson(response, 200, {
        history: {
          ...history,
          drafts: history.drafts.map(publicDraft),
        },
      });
    }
    if (url.pathname === "/api/studio/inventory") {
      const inventory = await runtime.studio.inventory(runtime.scope);
      const materials = await currentSessionMaterials(runtime, dependencies.now());
      return sendJson(response, 200, {
        inventory,
        materials: await publicMaterials(runtime, materials),
        materialTags: visibleMaterialTagState(await runtime.materialTags.list(), materials),
      });
    }
    if (url.pathname === "/api/studio/opportunities") {
      const result = await runtime.studio.discoverOpportunities(runtime.scope, {
        demandId: requireString(body.demandId, "demand-id-required"),
        matchAssessmentId: requireString(body.matchAssessmentId, "match-assessment-id-required"),
      });
      await persistRuntime(runtime);
      const currentCardIds = new Set(result.inventory.items.map((item) => item.materialCardId));
      const materials = (await currentSessionMaterials(runtime, dependencies.now()))
        .filter((card) => currentCardIds.has(card.id));
      return sendJson(response, 200, { ...result, materials: await publicMaterials(runtime, materials) });
    }
    if (url.pathname === "/api/studio/briefs") {
      const brief = await runtime.studio.confirmBrief(runtime.scope, {
        opportunityId: requireString(body.opportunityId, "opportunity-id-required"),
        matchAssessmentId: requireString(body.matchAssessmentId, "match-assessment-id-required"),
        selectedAssetIds: requireStringArray(body.selectedAssetIds, "selected-asset-ids-required"),
        confirmedFactIds: requireStringArray(body.confirmedFactIds, "confirmed-fact-ids-required"),
        reviewRequirements: requireStringArray(body.reviewRequirements, "review-requirements-required"),
        operationKey: requireString(body.operationKey, "brief-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { brief });
    }
    if (url.pathname === "/api/studio/drafts") {
      const drafts = await runtime.studio.createDrafts(
        runtime.scope,
        requireString(body.briefId, "brief-id-required"),
        requireString(body.operationKey, "draft-operation-key-required"),
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, {
        drafts: drafts.map(publicDraft),
      });
    }
    if (url.pathname === "/api/studio/approved-caption-drafts") {
      assertExactBodyFields(body, [
        "briefId", "sourceAssetId", "sourceDigest", "sourceStartMs", "sourceEndMs",
        "subtitleDigest", "approvedAt", "captionCues", "style", "operationKey",
      ]);
      if (!Number.isInteger(body.sourceStartMs) || !Number.isInteger(body.sourceEndMs)) {
        throw new Error("approved-caption-source-range-invalid");
      }
      const draft = await runtime.studio.importApprovedCaptionDraft(runtime.scope, {
        briefId: requireString(body.briefId, "brief-id-required"),
        sourceAssetId: requireString(body.sourceAssetId, "source-asset-id-required"),
        sourceDigest: requireString(body.sourceDigest, "source-digest-required"),
        sourceStartMs: Number(body.sourceStartMs),
        sourceEndMs: Number(body.sourceEndMs),
        subtitleDigest: requireString(body.subtitleDigest, "subtitle-digest-required"),
        approvedAt: requireString(body.approvedAt, "approved-caption-date-required"),
        captionCues: requireApprovedCaptionCues(body.captionCues),
        style: requireCaptionStyle(body.style),
        operationKey: requireString(body.operationKey, "approved-caption-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { draft: publicDraft(draft), publishClaimed: false });
    }
    const captionRevisionMatch = url.pathname.match(/^\/api\/studio\/drafts\/([^/]+)\/captions$/);
    if (captionRevisionMatch) {
      assertExactBodyFields(body, ["captionTexts", "style", "operationKey"]);
      const draft = await runtime.studio.reviseDraftCaptions(
        runtime.scope,
        requireString(decodeURIComponent(captionRevisionMatch[1]!), "draft-id-required"),
        {
          captionTexts: requireCaptionTexts(body.captionTexts),
          style: requireCaptionStyle(body.style),
          operationKey: requireString(body.operationKey, "caption-revision-operation-key-required"),
        },
      );
      await persistRuntime(runtime);
      return sendJson(response, 201, { draft: publicDraft(draft) });
    }
    const shareVersionMatch = url.pathname.match(/^\/api\/studio\/drafts\/([^/]+)\/share-version$/);
    if (shareVersionMatch) {
      assertExactBodyFields(body, ["operationKey"]);
      const draft = await runtime.studio.createShareOptimizedDraft(runtime.scope, {
        parentDraftId: requireString(decodeURIComponent(shareVersionMatch[1]!), "draft-id-required"),
        operationKey: requireString(body.operationKey, "share-optimization-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { draft: publicDraft(draft) });
    }
    if (url.pathname === "/api/studio/feedback") {
      const action = requireFeedbackAction(body.action);
      const feedback = await runtime.studio.recordFeedback(runtime.scope, {
        draftId: requireString(body.draftId, "draft-id-required"),
        action,
        reason: requireNullableString(body.reason, "feedback-reason-invalid"),
        operationKey: requireString(body.operationKey, "feedback-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { feedback });
    }
    if (url.pathname === "/api/studio/distribution-events") {
      assertExactBodyFields(body, ["draftId", "action", "operationKey"]);
      const event = await runtime.studio.recordDistributionEvent(runtime.scope, {
        draftId: requireString(body.draftId, "draft-id-required"),
        action: requireDistributionAction(body.action),
        operationKey: requireString(body.operationKey, "distribution-operation-key-required"),
      });
      await persistRuntime(runtime);
      return sendJson(response, 201, { event });
    }
    throw new Error("studio-route-not-found");
  }

  const referenceDraftMediaMatch = url.pathname.match(
    /^\/api\/studio\/reference-drafts\/([^/]+)\/media$/,
  );
  if (method === "GET" && referenceDraftMediaMatch) {
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const bytes = await runtime.referenceApi.readDraftMedia(
      decodeURIComponent(referenceDraftMediaMatch[1]!), runtime.scope,
    );
    return sendMedia(response, bytes, "video/mp4", request.headers.range);
  }

  const referenceSampleMediaMatch = url.pathname.match(
    /^\/api\/studio\/reference-samples\/([^/]+)\/media$/,
  );
  if (method === "GET" && referenceSampleMediaMatch) {
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const media = await runtime.referenceApi.readSampleMedia(
      decodeURIComponent(referenceSampleMediaMatch[1]!), runtime.scope,
    );
    return sendMedia(response, media.bytes, media.contentType, request.headers.range);
  }

  if (method === "GET" && url.pathname.startsWith("/api/studio/reference-")) {
    requireEmptyReadBody(request);
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const referenceResult = await runtime.referenceApi.dispatch({
      method,
      pathname: url.pathname,
      body: {},
      scope: runtime.scope,
      editor: runtime.referenceEditor,
    });
    if (referenceResult) return sendJson(response, referenceResult.status, referenceResult.body);
  }

  const mediaMatch = url.pathname.match(/^\/api\/studio\/drafts\/([^/]+)\/media$/);
  if (method === "GET" && mediaMatch) {
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const draftId = decodeURIComponent(mediaMatch[1]!);
    const bytes = await runtime.studio.readDraftOutput(runtime.scope, draftId);
    return sendMedia(response, bytes, "video/mp4", request.headers.range);
  }

  const materialMediaMatch = url.pathname.match(/^\/api\/studio\/materials\/([^/]+)\/media$/);
  if (method === "GET" && materialMediaMatch) {
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const assetId = decodeURIComponent(materialMediaMatch[1]!);
    const asset = (await runtime.library.listAssets(runtime.scope.actorId, runtime.scope.customerId))
      .find((candidate) => candidate.id === assetId
        && candidate.sessionId === runtime.scope.sessionId
        && candidate.mediaKind === "video");
    if (!asset) throw new Error("material-media-not-found");
    const bytes = await runtime.library.readOriginal(
      runtime.scope.actorId,
      runtime.scope.customerId,
      asset.id,
    );
    const contentType = extname(asset.fileName).toLowerCase() === ".mov"
      ? "video/quicktime"
      : "video/mp4";
    return sendMedia(response, bytes, contentType, request.headers.range);
  }

  const musicMediaMatch = url.pathname.match(/^\/api\/studio\/music\/([^/]+)\/media$/);
  if (method === "GET" && musicMediaMatch) {
    requireReadOrigin(request, host);
    const runtime = requireRuntime(request, dependencies.sessions);
    const track = await runtime.musicLibrary.readTrack(decodeURIComponent(musicMediaMatch[1]!));
    const extension = extname(track.fileName).toLowerCase();
    const contentType = extension === ".mp3" ? "audio/mpeg"
      : extension === ".wav" ? "audio/wav"
        : extension === ".aac" ? "audio/aac" : "audio/mp4";
    return sendMedia(response, track.bytes, contentType, request.headers.range);
  }

  throw new Error("studio-route-not-found");
}

async function routeExternalEditGet(
  request: IncomingMessage,
  response: ServerResponse,
  pathname: string,
  sessions: ReadonlyMap<string, StudioRuntime>,
): Promise<void> {
  const runtime = requireRuntime(request, sessions);
  const candidateMediaMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/candidates\/([^/]+)\/media$/);
  if (candidateMediaMatch) {
    const handoffId = decodeURIComponent(candidateMediaMatch[1]!);
    const candidateId = decodeURIComponent(candidateMediaMatch[2]!);
    const handoff = await runtime.externalHandoffs.get(runtime.scope, handoffId);
    const candidate = (await runtime.externalReturns.listCandidates(runtime.scope, handoffId))
      .find((item) => item.id === candidateId);
    if (!candidate || candidate.status !== "pending-review") throw new Error("external-edit-candidate-not-found");
    const handle = await runtime.externalInbox.openCandidateForRead(handoff, candidate);
    response.writeHead(200, {
      "content-type": extname(candidate.relativePath).toLowerCase() === ".mov" ? "video/quicktime" : "video/mp4",
      "content-length": candidate.sizeBytes,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    });
    const stream = handle.createReadStream({ autoClose: true });
    stream.once("error", () => response.destroy());
    stream.pipe(response);
    return;
  }
  if (pathname === "/api/studio/external-edit-handoffs") {
    return sendJson(response, 200, { handoffs: await runtime.externalHandoffs.list(runtime.scope) });
  }
  const candidatesMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/candidates$/);
  if (candidatesMatch) {
    const handoffId = decodeURIComponent(candidatesMatch[1]!);
    const candidates = await runtime.externalReturns.listCandidates(runtime.scope, handoffId);
    return sendJson(response, 200, {
      candidates: candidates.map((candidate) => publicExternalEditCandidate(handoffId, candidate)),
    });
  }
  const detailMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)$/);
  if (detailMatch) {
    const handoff = await runtime.externalHandoffs.get(runtime.scope, decodeURIComponent(detailMatch[1]!));
    return sendJson(response, 200, { handoff });
  }
  throw new Error("studio-route-not-found");
}

function publicExternalEditCandidate(
  handoffId: string,
  candidate: Awaited<ReturnType<ExternalEditReturnService["listCandidates"]>>[number],
) {
  return {
    id: candidate.id,
    handoffId: candidate.handoffId,
    parentDraftId: candidate.parentDraftId,
    digest: candidate.digest,
    sizeBytes: candidate.sizeBytes,
    durationSeconds: candidate.durationSeconds,
    mediaFacts: candidate.mediaFacts,
    stableSince: candidate.stableSince,
    detectedAt: candidate.detectedAt,
    status: candidate.status,
    mediaUrl: `/api/studio/external-edit-handoffs/${encodeURIComponent(handoffId)}/candidates/${encodeURIComponent(candidate.id)}/media`,
  };
}

async function routeExternalEditPost(
  response: ServerResponse,
  runtime: StudioRuntime,
  pathname: string,
  body: Record<string, unknown>,
  now: () => string,
  openExternalEditTarget: StudioServerOptions["openExternalEditTarget"],
  findJianyingApp: StudioServerOptions["findJianyingApp"],
  automateJianying: StudioServerOptions["automateJianying"],
  persistStudio: () => Promise<void>,
): Promise<void> {
  if (pathname === "/api/studio/external-edit-handoffs") {
    assertExactBodyFields(body, [
      "parentDraftId", "parentDraftVersion", "parentDraftDigest", "platformVariant",
      "revisionInstruction", "mustKeep", "includeSources", "sourceAssetIds", "operationKey",
    ]);
    const parentDraftId = requireString(body.parentDraftId, "external-edit-parent-draft-required");
    const sourceAssetIds = requireStringArray(body.sourceAssetIds, "external-edit-source-assets-invalid");
    const includeSources = body.includeSources;
    if (typeof includeSources !== "boolean" || includeSources !== (sourceAssetIds.length > 0)) {
      throw new Error("external-edit-source-consent-binding-mismatch");
    }
    if (!Number.isInteger(body.parentDraftVersion) || Number(body.parentDraftVersion) < 1) {
      throw new Error("external-edit-parent-version-invalid");
    }
    if (!isExternalEditPlatformVariant(body.platformVariant)) throw new Error("external-edit-platform-variant-invalid");
    const instruction = requireString(body.revisionInstruction, "external-edit-instruction-required");
    const mustKeep = requireStringArray(body.mustKeep, "external-edit-must-keep-invalid");
    if (instruction.length > 2_000 || mustKeep.length > 20 || mustKeep.some((item) => item.length > 200)) {
      throw new Error("external-edit-input-too-long");
    }
    const sourceAssetBindings = [];
    for (const sourceAssetId of sourceAssetIds) {
      const original = await runtime.library.readOriginal(runtime.scope.actorId, runtime.scope.customerId, sourceAssetId);
      sourceAssetBindings.push({
        sourceAssetId,
        digest: createHash("sha256").update(original).digest("hex"),
      });
    }
    const handoff = await runtime.externalHandoffs.create(runtime.scope, {
      parentDraftId,
      parentDraftVersion: Number(body.parentDraftVersion),
      parentDraftDigest: requireDigest(body.parentDraftDigest, "external-edit-parent-digest-invalid"),
      platformVariant: body.platformVariant,
      revisionInstruction: instruction,
      mustKeep,
      includeSources,
      sourceAssetBindings,
      operationKey: requireString(body.operationKey, "external-edit-operation-key-required"),
    });
    await runtime.externalInbox.createReturnDirectory(handoff);
    return sendJson(response, 201, { handoff });
  }

  const prepareMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/prepare$/);
  if (prepareMatch) {
    assertExactBodyFields(body, []);
    const handoff = await runtime.externalHandoffs.get(runtime.scope, decodeURIComponent(prepareMatch[1]!));
    const parentDraft = await runtime.studio.getDraft(runtime.scope, handoff.parentDraftId);
    const scratchRoot = join(runtimeRootForExternalEdit(runtime), "prepare-scratch", randomUUID());
    await mkdir(scratchRoot, { recursive: true, mode: 0o700 });
    const packageBuilder = new ExternalEditPackageBuilder({
      root: join(runtimeRootForExternalEdit(runtime), "packages"),
      now,
      resolveSources: async (currentHandoff, currentParent) => {
        const sources: Array<{ role: "parent-video" | "source-video" | "subtitle"; sourcePath: string; relativePath: string; digest: string; sourceAssetId?: string }> = [];
        const parentBytes = await runtime.studio.readDraftOutput(runtime.scope, currentParent.id);
        const parentPath = join(scratchRoot, "parent.mp4");
        await writePrivateFile(parentPath, parentBytes);
        sources.push({
          role: "parent-video",
          sourcePath: parentPath,
          relativePath: "video/parent.mp4",
          digest: currentParent.outputDigest,
        });
        for (const binding of currentHandoff.sourceAssetBindings) {
          const bytes = await runtime.library.readOriginal(runtime.scope.actorId, runtime.scope.customerId, binding.sourceAssetId);
          if (createHash("sha256").update(bytes).digest("hex") !== binding.digest) {
            throw new Error("external-edit-source-digest-mismatch");
          }
          const sourcePath = join(scratchRoot, `${createHash("sha256").update(binding.sourceAssetId).digest("hex")}.mp4`);
          await writePrivateFile(sourcePath, bytes);
          sources.push({
            role: "source-video",
            sourcePath,
            relativePath: `sources/${binding.sourceAssetId}.mp4`,
            digest: binding.digest,
            sourceAssetId: binding.sourceAssetId,
          });
        }
        if ((currentParent.captionCues?.length ?? 0) > 0) {
          const subtitleBytes = Buffer.from(captionCuesToSrt(currentParent.captionCues!), "utf8");
          const subtitlePath = join(scratchRoot, "subtitle.srt");
          await writePrivateFile(subtitlePath, subtitleBytes);
          sources.push({
            role: "subtitle",
            sourcePath: subtitlePath,
            relativePath: "support/subtitle.srt",
            digest: createHash("sha256").update(subtitleBytes).digest("hex"),
          });
        }
        return sources;
      },
    });
    const bridge = new JianyingDesktopBridge({
      root: runtimeRootForExternalEdit(runtime),
      packageBuilder,
      // App discovery only proves that the desktop app exists; it does not claim native project/API support.
      findApp: findJianyingApp ?? findInstalledJianyingApp,
      openTarget: openExternalEditTarget ?? defaultOpenExternalEditTarget,
      automate: automateJianying,
      resolveReturnFolder: (currentHandoff) => runtime.externalInbox.createReturnDirectory(currentHandoff),
    });
    try {
      const prepared = await bridge.prepare(handoff, parentDraft);
      const persisted = await runtime.externalHandoffs.transition(runtime.scope, {
        handoffId: handoff.id,
        status: prepared.status,
        operationKey: `prepare:${handoff.id}:${prepared.package.packageDigest.slice(0, 32)}`,
        packageDigest: prepared.package.packageDigest,
      });
      const result = { ...prepared, handoff: persisted };
      runtime.externalEditBridges.set(handoff.id, { bridge, prepared: result });
      return sendJson(response, 200, publicPreparedExternalEdit(result));
    } finally {
      await rm(scratchRoot, { recursive: true, force: true });
    }
  }

  const openMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/open$/);
  if (openMatch) {
    assertExactBodyFields(body, []);
    const handoffId = decodeURIComponent(openMatch[1]!);
    await runtime.externalHandoffs.get(runtime.scope, handoffId);
    const prepared = runtime.externalEditBridges.get(handoffId);
    if (!prepared) throw new Error("external-edit-prepare-required");
    const result = await prepared.bridge.open(prepared.prepared);
    return sendJson(response, 200, result);
  }

  const acceptMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/candidates\/([^/]+)\/accept$/);
  if (acceptMatch) {
    assertExactBodyFields(body, ["operationKey"]);
    const handoffId = decodeURIComponent(acceptMatch[1]!);
    const candidateId = decodeURIComponent(acceptMatch[2]!);
    const draft = await runtime.externalReturns.accept(
      runtime.scope,
      handoffId,
      candidateId,
      requireString(body.operationKey, "external-edit-operation-key-required"),
    );
    await persistStudio();
    return sendJson(response, 201, { draft: publicDraft(draft), lineageConfidence: "parent-only", publishClaimed: false });
  }

  const ignoreMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/candidates\/([^/]+)\/ignore$/);
  if (ignoreMatch) {
    assertExactBodyFields(body, ["operationKey"]);
    await runtime.externalReturns.ignore(
      runtime.scope,
      decodeURIComponent(ignoreMatch[1]!),
      decodeURIComponent(ignoreMatch[2]!),
      requireString(body.operationKey, "external-edit-operation-key-required"),
    );
    return sendJson(response, 200, { ignored: true, deleted: false });
  }

  const cancelMatch = pathname.match(/^\/api\/studio\/external-edit-handoffs\/([^/]+)\/cancel$/);
  if (cancelMatch) {
    assertExactBodyFields(body, []);
    const handoff = await runtime.externalHandoffs.cancel(
      runtime.scope,
      decodeURIComponent(cancelMatch[1]!),
      runtime.scope.actorId,
    );
    return sendJson(response, 200, { handoff });
  }
  throw new Error("studio-route-not-found");
}

function runtimeRootForExternalEdit(runtime: StudioRuntime): string {
  return runtime.externalRoot;
}

function publicPreparedExternalEdit(
  prepared: Awaited<ReturnType<JianyingDesktopBridge["prepare"]>>,
) {
  const { packageRoot: _packageRoot, ...publicValue } = prepared;
  return publicValue;
}

function isExternalEditPlatformVariant(
  value: unknown,
): value is "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels" {
  return value === "editorial-master" || value === "xiaohongshu" || value === "douyin" || value === "wechat-channels";
}

function requireDigest(value: unknown, code: string): string {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) throw new Error(code);
  return value;
}

async function writePrivateFile(path: string, bytes: Buffer): Promise<void> {
  const handle = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.chmod(0o600);
  } finally {
    await handle.close();
  }
}

export function captionCuesToSrt(cues: NonNullable<DraftPackage["captionCues"]>): string {
  return [...cues]
    .sort((left, right) => left.timelineStartMs - right.timelineStartMs || left.id.localeCompare(right.id))
    .map((cue, index) => [
      String(index + 1),
      `${srtTimestamp(cue.timelineStartMs)} --> ${srtTimestamp(cue.timelineEndMs)}`,
      cue.text.replace(/-->/g, "→").replace(/[\r\n]+/g, " ").trim(),
      "",
    ].join("\n"))
    .join("\n");
}

function srtTimestamp(milliseconds: number): string {
  const value = Math.max(0, Math.floor(milliseconds));
  const hours = Math.floor(value / 3_600_000);
  const minutes = Math.floor((value % 3_600_000) / 60_000);
  const seconds = Math.floor((value % 60_000) / 1_000);
  const millis = value % 1_000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}

export function findInstalledJianyingApp(): Promise<JianyingAppInfo | null> {
  if (process.platform !== "darwin") return Promise.resolve(null);
  const candidates = [
    "/Applications/VideoFusion-macOS.app",
    join(homedir(), "Applications", "VideoFusion-macOS.app"),
    "/Applications/JianyingPro.app",
    join(homedir(), "Applications", "JianyingPro.app"),
  ];
  for (const appPath of candidates) {
    try {
      const appInfo = lstatSync(appPath);
      const contentsPath = join(appPath, "Contents");
      const contentsInfo = lstatSync(contentsPath);
      const plistPath = join(contentsPath, "Info.plist");
      const plistInfo = lstatSync(plistPath);
      if (!appInfo.isDirectory() || appInfo.isSymbolicLink()
        || !contentsInfo.isDirectory() || contentsInfo.isSymbolicLink()
        || !plistInfo.isFile() || plistInfo.isSymbolicLink()) continue;
      if (readBundlePlistString(plistPath, "CFBundleIdentifier") !== "com.lemon.lvpro") continue;
      const appVersion = readBundlePlistString(plistPath, "CFBundleShortVersionString")
        ?? readBundlePlistString(plistPath, "CFBundleVersion");
      if (!appVersion) continue;
      return Promise.resolve({
        appPath,
        appVersion,
        // No adapter currently consumes this location; native project layout remains unverified.
        draftRoot: join(homedir(), "Movies", "JianyingPro", "User Data", "Projects", "com.lveditor.draft"),
      });
    } catch {
      // Missing, malformed, or unreadable app bundles are treated as not installed.
    }
  }
  return Promise.resolve(null);
}

function readBundlePlistString(plistPath: string, key: string): string | null {
  const result = spawnSync("/usr/bin/plutil", ["-extract", key, "raw", "-o", "-", plistPath], {
    encoding: "utf8",
    timeout: 2_000,
    maxBuffer: 4_096,
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.error || result.status !== 0 || typeof result.stdout !== "string") return null;
  const value = result.stdout.trim();
  return value.length > 0 && value.length <= 128 ? value : null;
}

async function defaultOpenExternalEditTarget(input: { kind: "jianying" | "folder"; target: string }): Promise<void> {
  if (process.platform !== "darwin") throw new Error("external-edit-local-mac-required");
  await new Promise<void>((resolvePromise, reject) => {
    const args = input.kind === "jianying" ? ["-b", "com.lemon.lvpro"] : [input.target];
    const child = spawn("open", args, { detached: true, stdio: "ignore" });
    child.once("error", reject);
    child.once("spawn", () => {
      child.unref();
      resolvePromise();
    });
  });
}

function requireEmptyReadBody(request: IncomingMessage): void {
  const contentLength = request.headers["content-length"];
  if (request.headers["transfer-encoding"] !== undefined ||
      (contentLength !== undefined && contentLength !== "0")) {
    throw new Error("studio-read-body-forbidden");
  }
}

async function createRuntime(
  rootDir: string,
  customer: StudioCustomer,
  now: () => string,
  suppliedWorker?: StudioModelWorker,
  suppliedStatusReader?: StudioModelStatusReader,
  suppliedReferenceRuntimeFactory?: StudioReferenceRuntimeFactory,
  referenceUploadLimits?: StudioReferenceUploadLimits,
  referenceMediaTools?: Readonly<{ ffprobePath: string; ffmpegPath: string }>,
  referenceMediaProcessLimits?: StudioReferenceMediaProcessLimits,
  referenceStagingHooks?: StudioReferenceStagingHooks,
  editorial?: EditorialDecisionAdapter,
  restored?: Readonly<{
    token: string;
    scope: StudioScope;
    state: StudioDurableStateV1;
  }>,
): Promise<StudioRuntime> {
  const token = restored?.token ?? `studio_${randomUUID()}`;
  const scope: StudioScope = restored?.scope ?? {
    actorId: "local-studio-operator",
    customerId: customer.id,
    sessionId: `studio-session-${randomUUID()}`,
  };
  const sessionRoot = join(rootDir, "sessions", sha256(token));
  const customerLibraryRoot = join(rootDir, "customer-libraries", sha256(customer.id));
  const library = new CustomerMaterialLibrary(customerLibraryRoot, (request) =>
    request.actorId === scope.actorId && request.customerId === scope.customerId, { now });
  const materialTags = new StudioMaterialTagStore(
    join(customerLibraryRoot, "management-tags"),
    customer.id,
    now,
  );
  const highlights = new StudioHighlightEvaluationStore(
    join(sessionRoot, "studio-highlight-evaluations.json"),
    scope,
    now,
  );
  const preferenceRanker = new StudioPreferenceRanker(library, { now });
  let model: StudioModelRuntime | null = null;
  let musicLibrary: StudioMusicLibrary | null = null;
  const studio = new PrivateContentStudio(
    library,
    new OfflineFixtureUnderstandingAdapter(),
    {
      now,
      outputRoot: join(sessionRoot, "drafts"),
      preferenceRanker,
      editorial,
      resolveEditorialTranscriptSpans: async (_scope, cards) =>
        model?.ensureEditorialTranscriptSpans(cards) ?? [],
      resolveEditorialVisualEvidence: async (_scope, cards) =>
        model?.ensureEditorialVisualEvidence(cards) ?? [],
      resolveEditorialBeatEvidence: async (_scope, _cards, sources) =>
        detectStudioBeatEvidence(sources),
      resolveEditorialMusicTrack: async (musicScope, brief) =>
        musicLibrary?.currentTrack(musicSelectionKey(musicScope, brief.id)) ?? null,
    },
  );
  // Synthetic fixtures are bootstrap material for a brand-new session. Their
  // originals and cards already live in the customer library, so regenerating
  // four videos on every resume added tens of seconds without changing state.
  if (!restored) await seedSyntheticMaterials(sessionRoot, library, studio, scope);
  if (restored) await studio.restoreDurableState(scope, restored.state);
  const worker = suppliedWorker ?? createLazyDefaultStudioModelWorker(customerLibraryRoot);
  const statusReader = suppliedStatusReader ?? createModelCheckerStatusReader({
    checkerPath: fileURLToPath(new URL("../../../scripts/check-studio-models.mjs", import.meta.url)),
    lockPath: fileURLToPath(new URL("../../../models.lock.json", import.meta.url)),
    modelRoot: process.env.STUDIO_MODEL_ROOT ?? join(homedir(), ".local", "share", "wearable-studio-models"),
  });
  const runtimeFfprobePath = referenceMediaTools?.ffprobePath ?? bundledFfprobePath;
  const runtimeFfmpegPath = referenceMediaTools?.ffmpegPath ?? ffmpegPath;
  if (typeof runtimeFfprobePath !== "string" || !runtimeFfmpegPath) {
    throw new Error("reference-import-media-tools-unavailable");
  }
  musicLibrary = await StudioMusicLibrary.open({
    root: join(customerLibraryRoot, "music"),
    legacyRoot: join(sessionRoot, "music"),
    library, scope,
    ffprobePath: runtimeFfprobePath, ffmpegPath: runtimeFfmpegPath, now,
  });
  model = new StudioModelRuntime({
    scope,
    library,
    studio,
    worker,
    now,
    modelStatusReader: statusReader,
    embeddingIndex: new StudioEmbeddingIndex(join(sessionRoot, "model-index"), { now }),
    reviewStatePath: join(sessionRoot, "studio-model-review-state.json"),
    preferenceRanker,
    resolveSourceInterval: createFfprobeStudioSourceIntervalResolver(runtimeFfprobePath),
  });
  const referenceRuntime = await (suppliedReferenceRuntimeFactory ?? createDefaultReferenceRuntime)({
    scope,
    sessionRoot,
    library,
    studio,
    model,
    now,
  });
  const referenceWithdrawalCoordinator = new ReferenceWithdrawalCoordinator({
    library,
    editor: referenceRuntime.editor,
  });
  const referenceApi = createStudioReferenceApi({
    sampleService: referenceRuntime.sampleService,
    store: referenceRuntime.store,
    prepareAnalysis: referenceRuntime.prepareAnalysis ?? (async () => undefined),
    withdrawAsset: (withdrawScope, assetId, operationKey) =>
      referenceWithdrawalCoordinator.withdraw(withdrawScope, assetId, operationKey),
    ...(referenceRuntime.enforceMaterialAuthorization ? {
      withCurrentAssetAuthorization: <T>(
        authorizationScope: StudioScope,
        bindings: Parameters<CustomerMaterialLibrary["withReferenceWorkflowAssets"]>[2],
        work: () => Promise<T> | T,
      ) => library.withReferenceWorkflowAssets(
        authorizationScope.actorId,
        authorizationScope.customerId,
        bindings,
        work,
      ),
    } : {}),
    stagingRoot: join(sessionRoot, "reference-upload-staging"),
    draftOutputRoot: join(sessionRoot, "reference-renders"),
    auditPath: join(sessionRoot, "reference-import-failures.jsonl"),
    now,
    ffprobePath: runtimeFfprobePath,
    ffmpegPath: runtimeFfmpegPath,
    uploadLimits: referenceUploadLimits,
    mediaProcessLimits: referenceMediaProcessLimits,
    stagingHooks: referenceStagingHooks,
  });
  const materialImportApi = createStudioMaterialImportApi({
    stagingRoot: join(sessionRoot, "material-upload-staging"),
    library,
    ffprobePath: runtimeFfprobePath,
    ffmpegPath: runtimeFfmpegPath,
    now,
  });
  const hardwareInbox = new StudioHardwareInboxStore(
    join(sessionRoot, "hardware-inbox"),
    materialImportApi,
    now,
  );
  const externalRoot = join(sessionRoot, "external-edit");
  const externalHandoffs = new ExternalEditHandoffService({
    root: join(externalRoot, "handoffs"),
    now,
    resolveParentDraft: async (handoffScope, draftId) => {
      try {
        return await studio.getDraft(handoffScope, draftId);
      } catch {
        return null;
      }
    },
  });
  const externalInbox = new ExternalEditInbox({
    root: join(externalRoot, "return-inbox"),
    now,
    ffprobePath: runtimeFfprobePath,
  });
  const externalReturns = new ExternalEditReturnService({
    root: join(externalRoot, "returns"),
    inbox: externalInbox,
    handoffs: externalHandoffs,
    studio,
    now,
  });
  return {
    token,
    scope,
    customer,
    sessionRoot,
    library,
    materialTags,
    highlights,
    studio,
    editorialCapability: editorial?.capability ?? OFFLINE_STUDIO_CAPABILITY,
    model,
    referenceApi,
    materialImportApi,
    musicLibrary,
    hardwareInbox,
    referenceEditor: referenceRuntime.editor,
    externalHandoffs,
    externalRoot,
    externalInbox,
    externalReturns,
    externalEditBridges: new Map(),
    persistence: { tail: Promise.resolve() },
  };
}

async function restoreRuntime(
  rootDir: string,
  token: string,
  customers: ReadonlyMap<string, StudioCustomer>,
  now: () => string,
  suppliedWorker?: StudioModelWorker,
  suppliedStatusReader?: StudioModelStatusReader,
  suppliedReferenceRuntimeFactory?: StudioReferenceRuntimeFactory,
  referenceUploadLimits?: StudioReferenceUploadLimits,
  referenceMediaTools?: Readonly<{ ffprobePath: string; ffmpegPath: string }>,
  referenceMediaProcessLimits?: StudioReferenceMediaProcessLimits,
  referenceStagingHooks?: StudioReferenceStagingHooks,
  editorial?: EditorialDecisionAdapter,
): Promise<StudioRuntime> {
  const sessionRoot = join(rootDir, "sessions", sha256(token));
  let descriptorValue: unknown;
  let stateValue: unknown;
  try {
    [descriptorValue, stateValue] = await Promise.all([
      readPrivateJson(join(sessionRoot, "session.json")),
      readPrivateJson(join(sessionRoot, "studio-state.json")),
    ]);
  } catch {
    throw new Error("studio-session-token-invalid");
  }
  if (!isStudioSessionDescriptor(descriptorValue)) {
    throw new Error("studio-session-token-invalid");
  }
  const customer = customers.get(descriptorValue.customerId);
  if (!customer) throw new Error("studio-session-token-invalid");
  const scope: StudioScope = {
    actorId: descriptorValue.actorId,
    customerId: descriptorValue.customerId,
    sessionId: descriptorValue.sessionId,
  };
  try {
    return await createRuntime(
      rootDir,
      customer,
      now,
      suppliedWorker,
      suppliedStatusReader,
      suppliedReferenceRuntimeFactory,
      referenceUploadLimits,
      referenceMediaTools,
      referenceMediaProcessLimits,
      referenceStagingHooks,
      editorial,
      { token, scope, state: stateValue as StudioDurableStateV1 },
    );
  } catch {
    throw new Error("studio-session-token-invalid");
  }
}

async function persistRuntime(runtime: StudioRuntime): Promise<void> {
  const pending = runtime.persistence.tail.then(async () => {
    const descriptor: StudioSessionDescriptorV1 = {
      schemaVersion: 1,
      actorId: runtime.scope.actorId,
      customerId: runtime.scope.customerId,
      sessionId: runtime.scope.sessionId,
    };
    const state = await runtime.studio.exportDurableState(runtime.scope);
    await mkdir(runtime.sessionRoot, { recursive: true, mode: 0o700 });
    await Promise.all([
      writePrivateJsonAtomically(join(runtime.sessionRoot, "session.json"), descriptor),
      writePrivateJsonAtomically(join(runtime.sessionRoot, "studio-state.json"), state),
    ]);
  });
  runtime.persistence.tail = pending.catch(() => undefined);
  await pending;
}

async function markActiveRuntime(
  rootDir: string,
  runtime: StudioRuntime,
  now: string,
): Promise<void> {
  await writeStudioActiveSession(rootDir, {
    token: runtime.token,
    actorId: runtime.scope.actorId,
    customerId: runtime.scope.customerId,
    customerLabel: runtime.customer.label,
    sessionId: runtime.scope.sessionId,
  }, now);
}

async function readPrivateJson(path: string): Promise<unknown> {
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1
      || metadata.size === 0 || metadata.size > 16 * 1024 * 1024) {
    throw new Error("studio-session-state-invalid");
  }
  return JSON.parse(await readFile(path, "utf8")) as unknown;
}

async function writePrivateJsonAtomically(path: string, value: unknown): Promise<void> {
  const temporaryPath = `${path}.${randomUUID()}.tmp`;
  try {
    await writePrivateFile(temporaryPath, Buffer.from(`${JSON.stringify(value)}\n`));
    await rename(temporaryPath, path);
  } finally {
    await rm(temporaryPath, { force: true });
  }
}

function isStudioSessionDescriptor(value: unknown): value is StudioSessionDescriptorV1 {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const descriptor = value as Record<string, unknown>;
  return Object.keys(descriptor).length === 4
    && descriptor.schemaVersion === 1
    && typeof descriptor.actorId === "string" && descriptor.actorId.length > 0
    && typeof descriptor.customerId === "string" && descriptor.customerId.length > 0
    && typeof descriptor.sessionId === "string" && descriptor.sessionId.length > 0;
}

async function createDefaultReferenceRuntime(input: Parameters<StudioReferenceRuntimeFactory>[0]) {
  const store = await JsonReferenceSampleStore.open(input.sessionRoot, input.scope);
  const sampleService = new ReferenceSampleService(input.library, input.sessionRoot, { now: input.now });
  const grammar = new LocalReferenceGrammarAdapter({ now: input.now });
  const outputRoot = join(input.sessionRoot, "reference-renders");
  await mkdir(outputRoot, { recursive: true, mode: 0o700 });
  const renderRequest = async (
    request: ReferenceEditorRenderRequest,
    revisionOverride?: ReturnType<typeof interpretReferencePromptRevision>,
  ) => {
    const { scope, rights, analysis, mappingPlan, operationKey } = request;
    const visibleAssets = new Map((await input.library.listAssets(scope.actorId, scope.customerId))
      .map((asset) => [asset.id, asset]));
    const sources: ReferenceRenderAsset[] = [];
    const mappings = mappingPlan.mappings.filter((mapping) => mapping.sourceAssetId !== null);
    for (const assetId of new Set(mappings.map((mapping) => mapping.sourceAssetId!))) {
      const mapping = mappings.find((candidate) => candidate.sourceAssetId === assetId)!;
      const asset = visibleAssets.get(assetId);
      if (!asset || !mapping.permission) throw new Error("reference-render-source-not-authorized");
      const bytes = await input.library.readOriginal(scope.actorId, scope.customerId, asset.id);
      sources.push({
        asset,
        permission: mapping.permission,
        withdrawn: false,
        verifiedDigest: createHash("sha256").update(bytes).digest("hex"),
        bytes,
      });
    }
    const rendered = await renderReferenceVariants({
      mappingPlan,
      rights,
      analysis,
      assets: sources,
      outputRoot,
      operationKey,
      now: input.now(),
      ...(revisionOverride ? { revisionOverride } : {}),
    });
    return {
      ...rendered,
      outputDirectory: dirname(rendered.drafts[0]!.outputPath),
    };
  };
  const editor = new ReferenceGuidedEditor({
    store,
    now: input.now,
    analyze: async ({ scope, sample, rights }) => {
      const source = await input.library.inspectReferenceAnalysisAsset(
        scope.actorId,
        scope.customerId,
        scope.sessionId,
        sample.sourceAssetId,
      );
      const attempt = await input.model.resolveReferenceAnalysisAttempt(source);
      return grammar.extractGrammar({
        scope,
        sample,
        rights,
        source,
        receipts: attempt.evidenceReceipts,
      });
    },
    createMapping: async ({ scope, sample, rights, analysis, selection }) => {
      const cards = await input.library.inventoryAt(scope.actorId, scope.customerId, input.now());
      const assets = await input.library.listAssets(scope.actorId, scope.customerId);
      const allowedAssets = new Map(assets
        .filter((asset) => asset.sessionId !== null && asset.usePermission === "allowed")
        .map((asset) => [asset.id, asset]));
      const snapshots: ReferenceMappingAssetSnapshot[] = [];
      for (const asset of allowedAssets.values()) {
        const bytes = await input.library.readOriginal(scope.actorId, scope.customerId, asset.id);
        snapshots.push({
          asset,
          permission: referencePermission(scope, asset),
          withdrawn: false,
          verifiedDigest: createHash("sha256").update(bytes).digest("hex"),
        });
      }
      return createReferenceMaterialMappingPlan({
        scope,
        sample,
        rights,
        analysis,
        selection,
        currentMaterials: cards.filter((card) => card.sessionId === scope.sessionId),
        authorizedHistoricalMaterials: cards.filter((card) => card.sessionId !== scope.sessionId),
        assets: snapshots,
      });
    },
    render: renderRequest,
    revise: async (request) => {
      if (request.target === null || request.prompt === null) {
        throw new Error("reference-revision-prompt-required");
      }
      return renderRequest(request, interpretReferencePromptRevision({
        prompt: request.prompt,
        target: request.target,
      }));
    },
    withCurrentAssetAuthorization: ({ scope, sample, mappingPlan }, work) => {
      const renderSources = new Map(mappingPlan.mappings.flatMap((mapping) => {
        if (!mapping.sourceAssetId) return [];
        if (!mapping.sourceDigest || !mapping.permission ||
            mapping.permission.view !== "allowed" || mapping.permission.use !== "allowed") {
          throw new Error("reference-workflow-asset-not-current");
        }
        return [[mapping.sourceAssetId, {
          role: "render-source" as const,
          assetId: mapping.sourceAssetId,
          assetVersion: mapping.permission.version,
          sourceDigest: mapping.sourceDigest,
          permission: {
            version: mapping.permission.version,
            epoch: mapping.permission.epoch,
            view: mapping.permission.view,
            use: mapping.permission.use,
          },
        }] as const];
      }));
      return input.library.withReferenceWorkflowAssets(scope.actorId, scope.customerId, [{
        role: "sample-source",
        assetId: sample.sourceAssetId,
        // Studio assets are immutable records. Existing reference samples did
        // not persist this field, so version 1 is the only valid legacy binding.
        assetVersion: 1,
        sourceDigest: sample.sourceDigest,
      }, ...renderSources.values()], work);
    },
    readDraftOutputDigest: async ({ draft }) => {
      let canonicalRoot: string;
      let canonicalOutput: string;
      try {
        canonicalRoot = await realpath(resolve(outputRoot));
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
      return createHash("sha256").update(await readFile(canonicalOutput)).digest("hex");
    },
    cleanupRenderedOutput: async (result) => {
      const relativePath = relative(resolve(outputRoot), resolve(result.outputDirectory));
      if (!relativePath || relativePath.startsWith("..")) {
        throw new Error("reference-render-output-cleanup-forbidden");
      }
      await rm(result.outputDirectory, { recursive: true, force: true });
    },
  });
  return {
    sampleService,
    editor,
    store,
    enforceMaterialAuthorization: true,
    prepareAnalysis: async (scope: StudioScope, referenceSampleId: string) => {
      const sample = (await store.snapshot()).samples
        .filter((candidate) => candidate.id === referenceSampleId)
        .sort((left, right) => right.version - left.version)[0];
      if (!sample || sample.customerId !== scope.customerId || sample.sessionId !== scope.sessionId) {
        throw new Error("reference-sample-not-found");
      }
      if (sample.status !== "ready") throw new Error("reference-sample-not-ready");
      const source = await input.library.inspectReferenceAnalysisAsset(
        scope.actorId, scope.customerId, scope.sessionId, sample.sourceAssetId,
      );
      if (source.asset.digest !== sample.sourceDigest) {
        throw new Error("reference-analysis-source-binding-mismatch");
      }
      await input.model.produceReferenceAnalysisAttempt(source);
    },
  };
}

function referencePermission(scope: StudioScope, asset: Awaited<ReturnType<CustomerMaterialLibrary["listAssets"]>>[number]) {
  return {
    id: `reference_permission_${sha256(`${scope.customerId}\0${scope.sessionId}\0${asset.id}\0${asset.version}`)}`,
    version: asset.version,
    epoch: asset.version,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    view: asset.viewPermission,
    use: asset.usePermission,
  };
}

export function createDefaultStudioModelWorkerConfig(
  sessionRoot: string,
  dependencies: DefaultStudioModelWorkerDependencies = {},
): LocalProcessStudioModelWorkerConfig {
  const workerRoot = fileURLToPath(new URL("../../../tools/media-model-worker/", import.meta.url));
  const lockPath = dependencies.lockPath
    ?? fileURLToPath(new URL("../../../models.lock.json", import.meta.url));
  const modelRoot = resolve(dependencies.modelRoot
    ?? process.env.STUDIO_MODEL_ROOT
    ?? join(homedir(), ".local", "share", "wearable-studio-models"));
  const runtimeId = dependencies.runtimeId ?? DEFAULT_FORMAL_WORKER_RUNTIME_ID;
  const resolved = (dependencies.resolveRuntime ?? resolveInstalledFormalWorkerRuntime)({
    lockPath,
    modelRoot,
    runtimeId,
    ...(dependencies.runtimeProfileId ? {runtimeProfileId:dependencies.runtimeProfileId} : {}),
  });
  if (
    resolved.status !== "runtime-real-probe-succeeded"
    && !(dependencies.allowProbeRequired === true
      && resolved.status === "runtime-installed-probe-required")
  ) {
    if (resolved.status === "runtime-installed-probe-required") {
      throw new Error("studio-formal-worker-runtime-probe-required");
    }
    throw new Error("studio-formal-worker-runtime-integrity-failed");
  }
  const expectedLauncher = join(resolved.runtimeRoot, "bin", "run-offline-worker");
  if (
    resolved.launcherPath !== expectedLauncher
    || !isAbsolute(resolved.runtimeRoot)
    || !/^\d+$/.test(resolved.launcherIdentity.device)
    || !/^\d+$/.test(resolved.launcherIdentity.inode)
    || !Number.isSafeInteger(resolved.launcherIdentity.byteLength)
    || resolved.launcherIdentity.byteLength <= 0
    || !/^[a-f0-9]{64}$/.test(resolved.launcherIdentity.sha256)
    || !/^[A-Za-z0-9._-]{1,200}$/.test(resolved.bundle.id)
    || !/^[a-f0-9]{64}$/.test(resolved.bundle.treeSha256)
  ) throw new Error("studio-formal-worker-runtime-integrity-failed");
  const environment = formalWorkerEnvironment(sessionRoot);
  const sandboxPath = dependencies.sandboxPath ?? DEFAULT_SANDBOX_PATH;
  const sandboxAvailable = dependencies.probeSandbox
    ? dependencies.probeSandbox(sandboxPath)
    : probeSandboxCapability(sandboxPath, environment);
  if (!sandboxAvailable) throw new Error("studio-formal-worker-sandbox-unavailable");
  if (typeof bundledFfprobePath !== "string" || !ffmpegPath) {
    throw new Error("studio-model-worker-media-tools-unavailable");
  }
  return {
    command: resolved.launcherPath,
    commandIdentity: resolved.launcherIdentity,
    args: [
      join(workerRoot, "worker.py"),
      ...(dependencies.runtimeProfileId ? ["--worker-runtime-profile-id",dependencies.runtimeProfileId] : []),
      "--worker-runtime-root", resolved.runtimeRoot,
      "--worker-runtime-artifact-id", resolved.bundle.id,
      "--worker-runtime-tree-sha256", resolved.bundle.treeSha256,
      "--ffprobe-path", bundledFfprobePath,
      "--ffmpeg-path", ffmpegPath,
      "--whisper-path", join(modelRoot, "whisper", "bin", "darwin-arm64", "whisper-cli"),
      "--whisper-model-path", join(modelRoot, "whisper", "ggml-small-q5_1.bin"),
      "--model-lock-path", lockPath,
      "--model-root", modelRoot,
      "--whisper-binary-artifact-id", "whisper.cpp-1.9.2-cli-darwin-arm64",
      "--whisper-model-artifact-id", "whisper.cpp-1.9.2-small-q5_1",
      "--openclip-checkpoint-path", join(modelRoot, "openclip", "vit-b-32", "open_clip_pytorch_model.bin"),
      "--openclip-artifact-id", "openclip-3.3.0-vit-b-32",
      "--qwen-checkpoint-path", join(modelRoot, "qwen3-vl-2b-instruct-4bit", "model.safetensors"),
      "--qwen-artifact-id", "qwen3-vl-2b-instruct-4bit",
      "--mlx-vlm-package-path", join(modelRoot, "runtime"),
      "--mlx-vlm-artifact-id", "mlx-vlm-0.6.6",
    ],
    cwd: realpathSync(workerRoot),
    env: environment,
    timeoutMs: MODEL_WORKER_OUTER_TIMEOUT_MS,
    maxStdoutBytes: 256 * 1024,
    maxStderrBytes: 8 * 1024,
    allowedMediaRoot: realpathSync(sessionRoot),
    stageTelemetry: {
      protocol: "studio-worker-stage-telemetry-v1",
      maxBytes: 4096,
    },
  };
}

type ResolvedFormalWorkerRuntime = Readonly<{
  runtimeRoot: string;
  launcherPath: string;
  launcherIdentity: Readonly<{
    device: string;
    inode: string;
    byteLength: number;
    sha256: string;
  }>;
  status: "runtime-installed-probe-required" | "runtime-real-probe-succeeded";
  bundle: Readonly<{ id: string; treeSha256: string }>;
}>;

export type DefaultStudioModelWorkerDependencies = Readonly<{
  resolveRuntime?: (input: Readonly<{
    lockPath: string;
    modelRoot: string;
    runtimeId: string;
    runtimeProfileId?: string;
  }>) => ResolvedFormalWorkerRuntime;
  sandboxPath?: string;
  probeSandbox?: (sandboxPath: string) => boolean;
  lockPath?: string;
  modelRoot?: string;
  runtimeId?: string;
  runtimeProfileId?: string;
  allowProbeRequired?: boolean;
  enableProbeStageTelemetry?: boolean;
}>;

export function createDefaultStudioModelWorker(
  sessionRoot: string,
  dependencies: DefaultStudioModelWorkerDependencies = {},
): StudioModelWorker {
  try {
    return new LocalProcessStudioModelWorker(
      createDefaultStudioModelWorkerConfig(sessionRoot, dependencies),
    );
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (!STABLE_FORMAL_RUNTIME_ERRORS.has(code)) throw error;
    return createFailClosedStudioModelWorker(code);
  }
}

function createLazyDefaultStudioModelWorker(sessionRoot: string): StudioModelWorker {
  let resolved: StudioModelWorker | null = null;
  const worker = () => {
    resolved ??= createDefaultStudioModelWorker(sessionRoot);
    return resolved;
  };
  const lazyWorker: StudioModelWorker = {
    run: (job, signal, context) => worker().run(job, signal, context),
    health: () => worker().health(),
  };
  return Object.freeze(lazyWorker);
}

function formalWorkerEnvironment(sessionRoot: string): Readonly<Record<string, string>> {
  if (!isAbsolute(sessionRoot)) throw new Error("studio-model-worker-config-invalid");
  const requestedRoot = lstatSync(sessionRoot, { bigint: true });
  const root = realpathSync(sessionRoot);
  const rootDetails = lstatSync(root, { bigint: true });
  const uid = currentUserId();
  if (
    requestedRoot.isSymbolicLink()
    || !rootDetails.isDirectory()
    || rootDetails.uid !== uid
    || (rootDetails.mode & 0o022n) !== 0n
  ) throw new Error("studio-model-worker-config-invalid");
  const privateRoot = join(root, ".studio-model-worker-private");
  const home = join(privateRoot, "home");
  const temporary = join(privateRoot, "tmp");
  for (const path of [privateRoot, home, temporary]) ensurePrivateDirectory(path, uid);
  return Object.freeze({
    PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
    HOME: home,
    LANG: "en_US.UTF-8",
    LC_ALL: "en_US.UTF-8",
    TMPDIR: temporary,
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
  });
}

function currentUserId(): bigint {
  const uid = typeof process.geteuid === "function" ? process.geteuid() : process.getuid?.();
  if (!Number.isSafeInteger(uid) || uid === undefined || uid < 0) {
    throw new Error("studio-model-worker-config-invalid");
  }
  return BigInt(uid);
}

function ensurePrivateDirectory(path: string, uid: bigint): void {
  try {
    mkdirSync(path, { mode: 0o700 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  const details = lstatSync(path, { bigint: true });
  if (
    details.isSymbolicLink()
    || !details.isDirectory()
    || details.uid !== uid
    || (details.mode & 0o7777n) !== 0o700n
  ) throw new Error("studio-model-worker-config-invalid");
}

function probeSandboxCapability(
  sandboxPath: string,
  environment: Readonly<Record<string, string>>,
): boolean {
  try {
    const details = lstatSync(sandboxPath);
    if (details.isSymbolicLink() || !details.isFile()) return false;
    accessSync(sandboxPath, constants.X_OK);
    const result = spawnSync(sandboxPath, [
      "-p", "(version 1)\n(deny network*)\n(allow default)", "/usr/bin/true",
    ], {
      env: environment,
      timeout: 2_000,
      stdio: "ignore",
    });
    return result.error === undefined && result.status === 0 && result.signal === null;
  } catch {
    return false;
  }
}

function createFailClosedStudioModelWorker(failureCode: string): StudioModelWorker {
  return Object.freeze({
    async health() {
      return {
        mode: "offline-fallback" as const,
        installed: false as const,
        modelName: null,
        checkpointDigest: null,
      };
    },
    async run(jobInput: ModelAnalysisJobV1): Promise<ModelAnalysisReceiptV1> {
      const job = ModelAnalysisJobV1Schema.parse(jobInput);
      const { requestedAt: _requestedAt, ...receiptBinding } = job;
      const timestamp = new Date().toISOString();
      return ModelAnalysisReceiptV1Schema.parse({
        ...receiptBinding,
        adapterName: "formal-runtime-blocked",
        modelName: job.layer === "embedding"
          ? "openclip-3.3.0-vit-b-32"
          : job.layer === "transcript"
            ? "whisper.cpp-1.9.2-small-q5_1"
            : "qwen3-vl-2b-instruct-4bit",
        modelVersion: "unavailable",
        checkpointDigest: null,
        inputDigest: sha256(JSON.stringify({
          sourceDigest: job.sourceDigest,
          sourceStartMs: job.sourceStartMs,
          sourceEndMs: job.sourceEndMs,
          layer: job.layer,
        })),
        promptVersion: "studio-formal-runtime-blocked-v1",
        observations: [],
        suggestedTags: [],
        qualitySignals: {},
        confidence: 0,
        warnings: ["formal-worker-runtime-unavailable"],
        startedAt: timestamp,
        completedAt: timestamp,
        runtimeMs: 0,
        status: "failed-manual-review",
        failure: { code: failureCode },
        synthetic: false,
        providerMode: "offline-fallback",
      });
    },
  });
}

async function seedSyntheticMaterials(
  sessionRoot: string,
  library: CustomerMaterialLibrary,
  studio: PrivateContentStudio,
  scope: StudioScope,
): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const definitions = [
    ["0x6d3b2d", 392],
    ["0x326a63", 440],
    ["0x876942", 494],
    ["0x425c78", 523],
  ] as const;
  const fixtureRoot = join(sessionRoot, "synthetic-fixtures");
  await mkdir(fixtureRoot, { recursive: true });
  const fixturePaths = await Promise.all(definitions.map(async ([color, frequency], index) => {
    const path = join(fixtureRoot, `synthetic-color-tone-fixture-${index + 1}.mp4`);
    await makeSyntheticClip(path, color, frequency);
    return path;
  }));
  for (const [index] of definitions.entries()) {
    const path = fixturePaths[index]!;
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: `synthetic-import-${scope.sessionId}-${index + 1}`,
      bytes: await readFile(path),
      mediaKind: "video",
      fileName: `synthetic-color-tone-fixture-${index + 1}.mp4`,
      capturedAt: `2026-09-10T08:00:0${index}.000Z`,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    await studio.analyze(scope, asset.id, {
      synthetic: true,
      contentType: "single-product",
      segment: { startMs: 0, endMs: 10_500 },
      ffprobe: {
        durationSeconds: 10.5,
        hasVideo: true,
        hasAudio: true,
        width: 180,
        height: 320,
      },
      observations: [`纯色合成商品夹具 ${index + 1}`],
      merchandiseFacts: ["example-attribute: 示例属性字段待人工确认"],
    });
  }
}

async function makeSyntheticClip(path: string, color: string, frequency: number): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  await runChecked(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", `color=c=${color}:s=180x320:r=25:d=10.5`,
    "-f", "lavfi", "-i", `sine=frequency=${frequency}:sample_rate=44100:duration=10.5`,
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", path,
  ], "studio-fixture-generation-failed");
}

async function runChecked(command: string, args: readonly string[], code: string): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (status) => status === 0
      ? resolve()
      : reject(new Error(`${code}:${stderr.trim()}`)));
  });
}

function requireRuntime(
  request: IncomingMessage,
  sessions: ReadonlyMap<string, StudioRuntime>,
): StudioRuntime {
  const token = requireRuntimeToken(request);
  const runtime = sessions.get(token);
  if (!runtime) throw new Error("studio-session-token-invalid");
  return runtime;
}

function requireRuntimeToken(request: IncomingMessage): string {
  const authorization = request.headers.authorization;
  const bearer = authorization?.match(/^Bearer (studio_[A-Za-z0-9-]+)$/)?.[1];
  const cookie = parseCookies(request.headers.cookie).studio_session;
  const token = bearer ?? cookie;
  if (!token) throw new Error("studio-session-token-required");
  return token;
}

function assertBoundScope(body: Record<string, unknown>, scope: StudioScope): void {
  void scope;
  for (const key of ["actorId", "customerId", "sessionId"] as const) {
    if (key in body) {
      throw new Error("studio-customer-scope-forbidden");
    }
  }
}

function assertSameScope(left: StudioScope, right: StudioScope): void {
  if (left.actorId !== right.actorId || left.customerId !== right.customerId ||
      left.sessionId !== right.sessionId) {
    throw new Error("hardware-device-scope-mismatch");
  }
}

function requireSameOrigin(request: IncomingMessage, host: string): void {
  const origin = request.headers.origin;
  if (!origin || !isTrustedOrigin(origin, host)) throw new Error("studio-same-origin-required");
}

function requireReadOrigin(request: IncomingMessage, host: string): void {
  const source = request.headers.origin ?? request.headers.referer;
  if (!source) throw new Error("studio-same-origin-required");
  try {
    if (!isTrustedOrigin(new URL(source).origin, host)) throw new Error();
  } catch {
    throw new Error("studio-same-origin-required");
  }
}

function requireJsonContentType(request: IncomingMessage): void {
  const contentType = request.headers["content-type"] ?? "";
  if (!/^application\/json(?:\s*;|$)/i.test(contentType)) {
    throw new Error("studio-json-content-type-required");
  }
}

function isLoopbackAddress(host: string): boolean {
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

function isStudioListenAddress(host: string): boolean {
  return isLoopbackAddress(host) || host === "0.0.0.0";
}

function isLoopbackHost(host: string): boolean {
  return /^(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/.test(host);
}

function hardwareHostname(host: string): string {
  try {
    return new URL(`http://${host}`).hostname;
  } catch {
    return "";
  }
}

function isPrivateHardwareHost(host: string): boolean {
  if (isLoopbackHost(host)) return true;
  const hostname = hardwareHostname(host);
  const octets = hostname.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }
  return octets[0] === 10 ||
    (octets[0] === 172 && octets[1]! >= 16 && octets[1]! <= 31) ||
    (octets[0] === 192 && octets[1] === 168);
}

function requirePrivateHardwareHost(host: string): void {
  if (!isPrivateHardwareHost(host)) throw new Error("hardware-private-host-required");
}

function requireHardwareSameOrigin(request: IncomingMessage, host: string): void {
  const origin = request.headers.origin;
  if (!origin) return;
  try {
    const parsed = new URL(origin);
    if (parsed.protocol !== "http:" || parsed.host !== host || !isPrivateHardwareHost(parsed.host)) throw new Error();
  } catch {
    throw new Error("hardware-same-origin-required");
  }
}

function isTrustedOrigin(origin: string, host: string): boolean {
  try {
    const parsed = new URL(origin);
    return parsed.protocol === "http:" && parsed.host === host && isLoopbackHost(parsed.host);
  } catch {
    return false;
  }
}

function parseCookies(value: string | undefined): Record<string, string> {
  return Object.fromEntries((value ?? "").split(";").flatMap((item) => {
    const separator = item.indexOf("=");
    if (separator < 1) return [];
    return [[item.slice(0, separator).trim(), item.slice(separator + 1).trim()]];
  }));
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.byteLength;
    if (size > MAX_BODY_BYTES) throw new Error("studio-request-body-too-large");
    chunks.push(bytes);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value as Record<string, unknown>;
  } catch {
    throw new Error("studio-invalid-json-body");
  }
}

function requireString(value: unknown, code: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(code);
  return value.trim();
}

function requireNullableString(value: unknown, code: string): string | null {
  if (value === null) return null;
  return requireString(value, code);
}

function requireStringArray(
  value: unknown,
  code: string,
  structuredItemErrors?: Readonly<{
    tooManyCode: string;
    duplicateCode: string;
  }>,
): string[] {
  if (!Array.isArray(value)) {
    throw new Error(code);
  }
  if (structuredItemErrors && value.length > STUDIO_INPUT_LIMITS.structuredItems) {
    throw new Error(structuredItemErrors.tooManyCode);
  }
  if (value.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error(code);
  }
  const normalized = value.map((item) => (item as string).trim());
  if (structuredItemErrors && new Set(normalized).size !== normalized.length) {
    throw new Error(structuredItemErrors.duplicateCode);
  }
  return normalized;
}

function requireCaptionTexts(value: unknown): Array<{ id: string; text: string }> {
  if (!Array.isArray(value) || value.length === 0 || value.length > 500) {
    throw new Error("caption-revision-text-invalid");
  }
  return value.map((entry) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)
        || Object.keys(entry).length !== 2 || !("id" in entry) || !("text" in entry)) {
      throw new Error("caption-revision-text-invalid");
    }
    return {
      id: requireString((entry as Record<string, unknown>).id, "caption-revision-text-invalid"),
      text: requireString((entry as Record<string, unknown>).text, "caption-revision-text-invalid"),
    };
  });
}

function requireApprovedCaptionCues(value: unknown) {
  if (!Array.isArray(value) || value.length === 0 || value.length > 500) {
    throw new Error("approved-caption-cues-invalid");
  }
  return value.map((entry) => {
    const parsed = StudioCaptionCueSchema.safeParse(entry);
    if (!parsed.success) throw new Error("approved-caption-cues-invalid");
    return parsed.data;
  });
}

function requireCaptionStyle(value: unknown) {
  const parsed = StudioCaptionStyleSchema.safeParse(value);
  if (!parsed.success) throw new Error("caption-revision-style-invalid");
  return parsed.data;
}

function requireStudioContentType(value: unknown): StudioContentType {
  const parsed = StudioContentTypeSchema.safeParse(value);
  if (!parsed.success) {
    throw new Error("studio-content-type-invalid");
  }
  return parsed.data;
}

function requireDemandSubmitter(value: unknown): ContentDemand["submittedBy"] {
  if (value !== "customer-self-declared" && value !== "operator-on-behalf") {
    throw new Error("studio-demand-submitter-invalid");
  }
  return value;
}

function assertNoClientScope(body: Record<string, unknown>, scope: StudioScope): void {
  assertBoundScope(body, scope);
}

function requireAnalysisLayers(value: unknown): StudioAnalysisLayer[] {
  if (!Array.isArray(value) || value.length === 0) throw new Error("studio-analysis-layers-required");
  return value.map((layer) => {
    const result = StudioAnalysisLayerSchema.safeParse(layer);
    if (!result.success) throw new Error("studio-analysis-layer-invalid");
    return result.data;
  });
}

function parseAnalysisReviewBody(body: Record<string, unknown>): StudioAnalysisReviewInput {
  return {
    action: body.action as StudioAnalysisReviewInput["action"],
    receiptJobId: body.receiptJobId as string,
    sourceAssetId: body.sourceAssetId as string,
    sourceDigest: body.sourceDigest as string,
    sourceStartMs: body.sourceStartMs as number,
    sourceEndMs: body.sourceEndMs as number,
    target: body.target as string,
    correction: body.correction as string | null,
    operationKey: body.operationKey as string,
  };
}

function parseMaterialSuitabilityBody(
  body: Record<string, unknown>,
): StudioMaterialSuitabilityInput {
  return {
    decision: body.decision as StudioMaterialSuitabilityInput["decision"],
    demandId: body.demandId as string,
    matchAssessmentId: body.matchAssessmentId as string,
    receiptJobId: body.receiptJobId as string,
    sourceAssetId: body.sourceAssetId as string,
    sourceDigest: body.sourceDigest as string,
    sourceStartMs: body.sourceStartMs as number,
    sourceEndMs: body.sourceEndMs as number,
    reason: body.reason as string,
    operationKey: body.operationKey as string,
  };
}

function parseCustomerFeedbackBody(
  body: Record<string, unknown>,
): RecordCustomerFeedbackInput {
  const satisfaction = CustomerDraftFeedbackSchema.shape.satisfaction.safeParse(body.satisfaction);
  if (!satisfaction.success) throw new Error("customer-feedback-satisfaction-invalid");
  const issueCategories = CustomerDraftFeedbackSchema.shape.issueCategories.safeParse(body.issueCategories);
  if (!issueCategories.success) throw new Error("customer-feedback-issue-categories-invalid");
  return {
    draftId: requireString(body.draftId, "draft-id-required"),
    satisfaction: satisfaction.data,
    issueCategories: issueCategories.data,
    comment: body.comment === null
      ? null
      : requireString(body.comment, "customer-feedback-comment-invalid"),
    mustKeep: requireStringArray(body.mustKeep, "customer-feedback-must-keep-invalid", {
      tooManyCode: "customer-feedback-must-keep-too-many",
      duplicateCode: "duplicate-must-keep",
    }),
    mustRemove: requireStringArray(body.mustRemove, "customer-feedback-must-remove-invalid", {
      tooManyCode: "customer-feedback-must-remove-too-many",
      duplicateCode: "duplicate-must-remove",
    }),
    needsReshoot: requireStringArray(body.needsReshoot, "customer-feedback-needs-reshoot-invalid", {
      tooManyCode: "customer-feedback-needs-reshoot-too-many",
      duplicateCode: "duplicate-needs-reshoot",
    }),
    submittedBy: requireDemandSubmitter(body.submittedBy),
    operationKey: requireString(body.operationKey, "customer-feedback-operation-key-required"),
  };
}

function requireFeedbackAction(value: unknown) {
  if (!["adopt", "reject", "request-changes"].includes(String(value))) {
    throw new Error("feedback-action-not-allowed");
  }
  return value as "adopt" | "reject" | "request-changes";
}

function requireDistributionAction(value: unknown) {
  const allowed = ["download-started", "system-share-invoked", "share-fallback-download"] as const;
  if (!allowed.includes(String(value) as typeof allowed[number])) {
    throw new Error("distribution-action-not-allowed");
  }
  return value as typeof allowed[number];
}

function publicDraft(draft: Awaited<ReturnType<PrivateContentStudio["getDraft"]>>) {
  const { outputPath: _serverOnlyOutputPath, ...safeDraft } = draft;
  return {
    ...safeDraft,
    mediaUrl: `/api/studio/drafts/${encodeURIComponent(draft.id)}/media`,
  };
}

function publicMusicTrack<T extends { assetId: string }>(track: T): T & { mediaUrl: string } {
  return { ...track, mediaUrl: `/api/studio/music/${encodeURIComponent(track.assetId)}/media` };
}

function publicMusicState(library: StudioMusicLibrary, briefId: string | null = null) {
  const state = library.list(briefId);
  return { ...state, tracks: state.tracks.map(publicMusicTrack) };
}

function musicSelectionKey(scope: StudioScope, briefId: string | null): string {
  return `${scope.sessionId}:${briefId ?? "session-default"}`;
}

async function currentSessionMaterials(runtime: StudioRuntime, asOf: string) {
  return (await runtime.library.inventoryAt(
    runtime.scope.actorId,
    runtime.scope.customerId,
    asOf,
  )).filter((card) => card.sessionId === runtime.scope.sessionId);
}

async function currentHighlightSnapshot(runtime: StudioRuntime, materials: readonly MaterialCard[]) {
  const evidence = await runtime.model.editorialVisualEvidence(materials);
  const attempts = await runtime.model.editorialVisualAttempts(materials);
  const evaluations = await runtime.highlights.evaluate(materials, evidence);
  const profiles = await runtime.highlights.coverageProfiles();
  const rejections = await runtime.highlights.candidateRejections();
  return {
    attempts,
    evaluations,
    profiles,
    rejections,
    batch: summarizeHighlightBatch(
      evaluations.filter((evaluation) => isEligibleHighlightEvaluation(evaluation, materials)),
      attempts,
      materials.filter(isEligibleHighlightMaterial),
      profiles,
    ),
  };
}

function isEligibleHighlightEvaluation(
  evaluation: Readonly<{ sourceAssetId: string; sourceDigest: string }>,
  materials: readonly MaterialCard[],
): boolean {
  const material = materials.find((item) =>
    item.sourceAssetId === evaluation.sourceAssetId && item.sourceDigest === evaluation.sourceDigest);
  return Boolean(material && isEligibleHighlightMaterial(material));
}

function visibleMaterialTagState(
  state: StudioMaterialTagState,
  materials: readonly { id: string }[],
): StudioMaterialTagState {
  const visibleCardIds = new Set(materials.map((material) => material.id));
  return {
    ...state,
    assignments: state.assignments.filter((assignment) => visibleCardIds.has(assignment.materialCardId)),
  };
}

async function publicMaterials(runtime: StudioRuntime, materials: readonly MaterialCard[]) {
  const assets = await runtime.library.listAssets(runtime.scope.actorId, runtime.scope.customerId);
  const assetById = new Map(assets.map((asset) => [asset.id, asset]));
  return materials.map((material) => publicMaterial(material, assetById.get(material.sourceAssetId)));
}

function studioLibraryDate(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "日期待确认";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const byType = new Map(parts.map((part) => [part.type, part.value]));
  return `${byType.get("year")}-${byType.get("month")}-${byType.get("day")}`;
}

function publicMaterial(material: MaterialCard, asset?: StudioMediaAsset) {
  const fileName = normalizeUploadedFileName(asset?.fileName || `素材-${material.sourceAssetId}.mp4`);
  const uploadedAt = asset?.importedAt || material.createdAt;
  const uploadedDate = studioLibraryDate(uploadedAt);
  const uploadSource = asset?.captureContext
    ? "硬件自动接入"
    : material.id.startsWith("card_upload_")
      ? "手机／本机上传"
      : "已有素材";
  return {
    ...material,
    mediaUrl: `/api/studio/materials/${encodeURIComponent(material.sourceAssetId)}/media`,
    fileName,
    displayName: /^card_(?:upload|hardware)_/u.test(material.id) ? fileName : null,
    uploadedAt,
    uploadSource,
    libraryPath: `素材库 / ${uploadSource} / ${uploadedDate} / ${fileName}`,
  };
}

function sendHardwarePairingPage(response: ServerResponse, code: string): void {
  const nonce = randomUUID().replaceAll("-", "");
  const serializedCode = JSON.stringify(code)
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e")
    .replaceAll("&", "\\u0026");
  const body = Buffer.from(`<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>连接内容工作台</title><style>
:root{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f6f0e7;color:#1e2823}*{box-sizing:border-box}body{margin:0;padding:28px 18px 48px}main{max-width:520px;margin:auto}.eyebrow{color:#8c603f;font-weight:800}.card{display:grid;gap:16px;margin-top:18px;padding:22px;border-radius:24px;background:#fff;box-shadow:0 16px 40px #4f403016}h1{font-size:2rem;margin:8px 0}h2{margin:0}p{margin:0;line-height:1.6;color:#695f55}label{display:grid;gap:7px;font-weight:800}input,select,button{width:100%;min-height:52px;border-radius:14px;font:inherit}input,select{border:1px solid #cfc1b0;padding:10px 13px;background:#fff}button{border:0;padding:12px 16px;background:#244336;color:#fff;font-weight:850}button.secondary{border:1px solid #cfc1b0;background:#fff;color:#55493e}button:disabled{opacity:.45}.badge{width:max-content;padding:6px 10px;border-radius:999px;background:#f4e0cc;color:#8c4f2d;font-weight:800;font-size:.82rem}.inbox-item{display:grid;gap:12px;padding:14px;border:1px solid #ddd0c0;border-radius:16px;background:#fbf8f3}.inbox-item video{width:100%;max-height:260px;border-radius:12px;background:#111}.inbox-meta{display:grid;gap:3px}.inbox-meta small{color:#766b60}.actions{display:grid;grid-template-columns:1fr auto;gap:8px}.status{padding:13px;border-radius:13px;background:#f3eee6;color:#5f554b}.success{background:#e7f1e9;color:#245438}.error{background:#fbe9e6;color:#932c21}[hidden]{display:none!important}</style></head>
<body><main><div class="eyebrow">内容总编 · iPhone 网关</div><h1>连接这台 Mac</h1><p>连接后，你从手机选择的视频会进入当前客户的素材库，不会自动发布。</p>
<section class="card" id="pair-card"><label>设备名称<input id="device-name" value="我的 iPhone" maxlength="80"></label><button id="pair-button">连接这台 Mac</button><div class="status" id="pair-status">等待你确认连接</div></section>
<section class="card" id="upload-card" hidden><span class="badge">本地模拟，不代表真实硬件已接入</span><h2>硬件收件箱</h2><p>先从手机选一段视频，模拟“智能主石把素材送到 iPhone”。确认后才会进入 Mac 素材库。</p><label>模拟硬件送入视频<input id="video-file" type="file" accept="video/mp4,video/quicktime,.mp4,.mov"></label><article class="inbox-item" id="inbox-item" hidden><video id="inbox-preview" controls playsinline></video><div class="inbox-meta"><strong id="inbox-name"></strong><small id="inbox-details">等待确认</small></div><label>内容类型<select id="content-type"><option value="single-product">产品展示</option><option value="jewelry-making-process">制作过程</option><option value="designer-viewpoint">人物口播／设计师表达</option></select></label><div class="actions"><button id="upload-button">送到 Mac 待确认</button><button class="secondary" id="discard-button" type="button">移除</button></div><div class="status" id="upload-status">待送入收件箱，不会自动进素材库</div></article></section>
</main><script nonce="${nonce}">
const code=${serializedCode};let deviceToken="",pendingCapture=null,pendingInboxId="",previewUrl="";const pairButton=document.querySelector("#pair-button"),pairStatus=document.querySelector("#pair-status"),uploadCard=document.querySelector("#upload-card"),uploadButton=document.querySelector("#upload-button"),uploadStatus=document.querySelector("#upload-status"),videoFile=document.querySelector("#video-file"),inboxItem=document.querySelector("#inbox-item"),inboxPreview=document.querySelector("#inbox-preview"),inboxName=document.querySelector("#inbox-name"),inboxDetails=document.querySelector("#inbox-details"),discardButton=document.querySelector("#discard-button");
function show(node,text,kind=""){node.textContent=text;node.className="status "+kind}
pairButton.addEventListener("click",async()=>{pairButton.disabled=true;show(pairStatus,"正在连接…");try{const response=await fetch("/api/hardware/pair",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({code,deviceName:document.querySelector("#device-name").value})});const result=await response.json();if(!response.ok)throw new Error(result.error||"连接失败");deviceToken=result.binding.deviceToken;show(pairStatus,"已连接，可以接收模拟硬件素材","success");uploadCard.hidden=false}catch(error){show(pairStatus,"连接失败，请回到 Mac 重新生成二维码","error");pairButton.disabled=false}});
function stableFileKey(file){const value=file.name+"\\0"+file.size+"\\0"+file.lastModified;let hash=2166136261;for(let index=0;index<value.length;index+=1)hash=Math.imul(hash^value.charCodeAt(index),16777619);return (hash>>>0).toString(16).padStart(8,"0")+"-"+file.size+"-"+file.lastModified}
function clearInbox(){if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl="";pendingCapture=null;pendingInboxId="";videoFile.value="";inboxPreview.removeAttribute("src");inboxItem.hidden=true;uploadButton.textContent="送到 Mac 待确认";discardButton.textContent="移除"}
videoFile.addEventListener("change",()=>{const file=videoFile.files[0];if(!file){clearInbox();return}pendingInboxId="";uploadButton.textContent="送到 Mac 待确认";inboxItem.hidden=false;inboxName.textContent=file.name;const key=stableFileKey(file);pendingCapture={file,key,capturedAt:new Date(file.lastModified||Date.now()).toISOString()};if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(file);inboxPreview.src=previewUrl;inboxDetails.textContent="模拟智能主石 · "+(file.size/1024/1024).toFixed(1)+" MB · 本地编号 "+key.slice(0,8);show(uploadStatus,"待送入收件箱，不会自动进素材库");uploadButton.disabled=false});
discardButton.addEventListener("click",()=>{clearInbox()});
uploadButton.addEventListener("click",async()=>{if(!pendingCapture){show(uploadStatus,"请先让模拟硬件送入一段视频","error");return}uploadButton.disabled=true;try{if(!pendingInboxId){show(uploadStatus,"正在从 iPhone 送到 Mac 收件箱…");const form=new FormData();form.append("file",pendingCapture.file,pendingCapture.file.name);form.append("contentType",document.querySelector("#content-type").value);form.append("operationKey","hardware-sim-"+pendingCapture.key);form.append("captureMetadata",JSON.stringify({captureId:"sim_capture_"+pendingCapture.key,deviceId:"simulated-main-stone-local-v1",deviceName:"模拟智能主石",capturedAt:pendingCapture.capturedAt,simulated:true}));const response=await fetch("/api/hardware/inbox/receive",{method:"POST",headers:{authorization:"Bearer "+deviceToken},body:form});const result=await response.json();if(!response.ok)throw new Error(result.error||"送入收件箱失败");pendingInboxId=result.item.id;show(uploadStatus,"已到 Mac 收件箱，请预览后确认入库。","success");inboxDetails.textContent="Mac 已收到 · 还未进入素材库";uploadButton.textContent="确认进入素材库";uploadButton.disabled=false;return}show(uploadStatus,"正在检查视频并写入当前客户素材库…");const response=await fetch("/api/hardware/inbox/confirm",{method:"POST",headers:{authorization:"Bearer "+deviceToken,"content-type":"application/json"},body:JSON.stringify({id:pendingInboxId})});const result=await response.json();if(!response.ok)throw new Error(result.error||"入库失败");show(uploadStatus,"已进入素材库："+(result.material.displayName||result.material.fileName||"视频")+"。回到 Mac 刷新即可查看。","success");inboxDetails.textContent="已送达素材库 · 文件摘要 "+String(result.material.sourceDigest||"").slice(0,8);uploadButton.textContent="已进入素材库";discardButton.textContent="清空"}catch(error){show(uploadStatus,"本次操作失败，可直接重试："+error.message,"error");uploadButton.disabled=false}});
</script></body></html>`);
  response.writeHead(200, {
    "content-type": "text/html; charset=utf-8",
    "content-length": body.byteLength,
    "cache-control": "no-store",
    "content-security-policy": `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'; connect-src 'self'; media-src blob:; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

async function sendStatic(
  response: ServerResponse,
  filename: string,
  contentType: string,
): Promise<void> {
  const body = await readFile(new URL(`../public/${filename}`, import.meta.url));
  response.writeHead(200, {
    "content-type": contentType,
    "content-length": body.byteLength,
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function sendMedia(
  response: ServerResponse,
  bytes: Buffer,
  contentType = "video/mp4",
  rangeValue?: string,
): void {
  const sharedHeaders = {
    "content-type": contentType,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "accept-ranges": "bytes",
  };
  if (rangeValue === undefined) {
    response.writeHead(200, { ...sharedHeaders, "content-length": bytes.byteLength });
    response.end(bytes);
    return;
  }
  const match = /^bytes=(\d*)-(\d*)$/.exec(rangeValue.trim());
  if (!match || (!match[1] && !match[2])) {
    response.writeHead(416, { ...sharedHeaders, "content-range": `bytes */${bytes.byteLength}` });
    response.end();
    return;
  }
  const suffixLength = match[1] ? null : Number(match[2]);
  const start = suffixLength === null
    ? Number(match[1])
    : Math.max(0, bytes.byteLength - suffixLength);
  const requestedEnd = match[2] && suffixLength === null
    ? Number(match[2])
    : bytes.byteLength - 1;
  const end = Math.min(requestedEnd, bytes.byteLength - 1);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)
      || start < 0 || start >= bytes.byteLength || end < start) {
    response.writeHead(416, { ...sharedHeaders, "content-range": `bytes */${bytes.byteLength}` });
    response.end();
    return;
  }
  const partial = bytes.subarray(start, end + 1);
  response.writeHead(206, {
    ...sharedHeaders,
    "content-length": partial.byteLength,
    "content-range": `bytes ${start}-${end}/${bytes.byteLength}`,
  });
  response.end(partial);
}

function sendJson(response: ServerResponse, status: number, value: unknown): void {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.byteLength,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function sendStudioError(response: ServerResponse, error: unknown): void {
  const message = error instanceof Error ? error.message : "studio-internal-error";
  const code = /^[a-z0-9-]+/.exec(message)?.[0] ?? "studio-internal-error";
  const unprocessable = code === "reference-import-media-invalid" ||
    code === "reference-import-duration-too-long" ||
    code === "material-upload-media-invalid" ||
    code === "material-upload-duration-too-long" ||
    code === "external-edit-upload-file-name-invalid" ||
    code === "external-edit-upload-media-invalid" ||
    code === "external-edit-upload-duration-invalid" ||
    code === "studio-highlight-batch-label-required" ||
    code === "studio-highlight-automatic-edit-gate-not-met" ||
    code === "studio-highlight-selected-source-not-passed" ||
    code === "reference-analysis-receipts-not-found" ||
    code === "reference-analysis-manual-review-required" ||
    code === "reference-receipt-real-local-evidence-required" ||
    code === "reference-revision-prompt-unsupported" ||
    code === "reference-revision-prompt-unachievable";
  const referenceIntegrityFailure = REFERENCE_INTERNAL_INTEGRITY_ERROR_CODES.has(code);
  const externalEditConflict = new Set([
    "external-edit-parent-changed",
    "external-edit-active-handoff-exists",
    "external-edit-already-returned",
    "external-edit-operation-key-reused",
    "external-edit-transition-not-allowed",
    "external-edit-candidate-changed",
    "external-edit-return-binding-mismatch",
    "external-edit-candidate-ignored",
    "external-edit-handoff-not-awaiting-return",
  ]).has(code);
  const status = unprocessable
    ? 422
    : code === "studio-route-not-found" || code.endsWith("not-found")
    ? 404
    : code === "studio-session-token-required" || code === "studio-session-token-invalid" ||
        code === "hardware-device-token-required" || code === "hardware-device-token-invalid" ||
        code === "hardware-device-token-expired"
      ? 401
      : code === "studio-loopback-host-required" ||
          code === "hardware-private-host-required" ||
          code === "hardware-same-origin-required" ||
          code === "hardware-device-scope-mismatch" ||
          code === "studio-same-origin-required" ||
          code === "studio-customer-scope-forbidden" ||
          code === "studio-customer-not-allowed"
      ? 403
      : code.endsWith("forbidden")
        ? 403
        : code.endsWith("actor-mismatch")
          ? 403
          : code === "studio-json-content-type-required" ||
              code === "reference-import-multipart-content-type-required"
              || code === "material-upload-multipart-content-type-required"
          ? 415
          : code === "reference-import-too-large" || code === "material-upload-too-large" ||
              code === "external-edit-upload-too-large"
            ? 413
          : code === "hardware-session-unavailable" ||
              code === "reference-import-media-tool-unavailable" ||
              code === "reference-import-media-tool-timeout" ||
              code === "reference-import-media-tool-crashed" ||
              code === "material-upload-media-tool-unavailable" ||
              code === "material-upload-media-tool-timeout" ||
              code === "reference-analysis-temporarily-unavailable"
            ? 503
          : referenceIntegrityFailure ||
              code === "reference-import-staging-io-failed" ||
              code === "reference-import-staging-cleanup-failed" ||
              code === "reference-import-audit-write-failed" ||
              code === "reference-import-media-output-too-large" ||
              code === "reference-import-media-process-failed" ||
              code === "reference-draft-media-unavailable" ||
              code === "reference-draft-output-binding-invalid" ||
              code === "external-edit-upload-output-invalid" ||
              code === "external-edit-upload-candidate-invalid"
            ? 500
          : externalEditConflict ||
              code.endsWith("conflict") ||
              code.endsWith("not-current") ||
              code.endsWith("stale") ||
              code === "match-not-ready" ||
              code === "feedback-not-revision-request" ||
              code === "distribution-draft-not-adopted" ||
              code === "share-optimization-parent-not-adopted" ||
              code === "revision-proposal-has-unresolved-gaps" ||
              code === "revision-proposal-has-unsupported-requests" ||
              code === "reference-draft-invalidated" ||
              code === "reference-draft-output-digest-mismatch" ||
              code === "reference-render-required" ||
              code === "reference-sample-invalidated" ||
              code === "reference-sample-source-withdrawn" ||
              code === "reference-sample-source-binding-invalid" ||
              code === "reference-workflow-asset-withdrawn" ||
              code === "reference-mobile-review-invalidated" ||
              code === "reference-mobile-review-incomplete" ||
              code === "reference-mobile-review-ambiguous" ||
              (code.endsWith("mismatch") && !code.endsWith("actor-mismatch"))
            ? 409
            : code === "studio-internal-error"
              ? 500
              : 400;
  sendJson(response, status, { error: code });
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
