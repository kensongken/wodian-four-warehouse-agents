import { createHash } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";

import type { ContentSegment, LibraryAsset, StoryType, VideoVariant } from "@wearable/contracts";
import {
  createLocalDispatchRoster, JsonContentStore, LocalDispatchMailbox,
  type CreativeScriptAgent, type EditPlanAnalyzer, type IntentInterpreter,
  type MaterialMatcher, type PhonePublisherAdapter, type PromptEditInterpreter,
  type SegmentTagAnalyzer, type ShotPlanningAgent, type StoryAnalyzer,
} from "@wearable/mobile-agent";
import { createReviewServer, listenReviewServer, type ReviewAppDependencies } from "../../review-app/src/server";
import { makeSyntheticClip } from "./editorial-pilot";

const CUSTOMER_ID = "customer_pilot_01";
const DESIGNER_ID = "designer_pilot_01";
const REVIEW_TOKEN = "creative-pilot-review-token";
const SELECTED_HISTORY_ID = "history_rework_01";
const UNSELECTED_HISTORY_ID = "history_unselected_02";

export type CreativeDirectorPilotReport = Readonly<{
  creativeSessionId: string;
  historicalConsent: { declinedHistoryCount: 0; viewedButUnselectedCount: 0; exactSelectedIds: string[] };
  script: { versions: [1, 2]; confirmedVersion: 2; unselectedReferenceCount: 0 };
  dispatch: { transport: "simulated"; receiptCount: number; preConfirmationReceiptCount: 0 };
  fieldActions: { completed: number; retake: number; skipped: number; notes: number; forcedReshootCount: 0 };
  staleTaskRejected: true;
  duplicateEventCount: 0;
  returnedAssetCount: number;
  storyTypes: StoryType[];
  variantKinds: VideoVariant[];
  approvedVariant: { variant: VideoVariant; revision: number };
  publishedClaimed: false;
}>;

/** All workflow transitions below cross the production HTTP boundary. */
export async function runCreativeDirectorPilot(workDir: string): Promise<CreativeDirectorPilotReport> {
  await mkdir(workDir, { recursive: true });
  const library = await JsonContentStore.open(join(workDir, "content-library.json"));
  await seedHistoricalMaterial(library, workDir);
  const publisher = localPhonePublisher();
  const server = createReviewServer(pilotDependencies(library, workDir, publisher));
  await listenReviewServer(server, 0, "127.0.0.1");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("creative-pilot-server-address-missing");
  const http = new PilotHttpClient(`http://127.0.0.1:${address.port}`);
  try {
    await http.open();
    const created = await http.post("/api/creative-sessions", {
      customerId: CUSTOMER_ID, title: "绿松石戒指的第三次返工",
      text: "拍一条说明第三次返工原因、过程和感受的内容",
    }, "pilot-create-session-0001");
    const sessionId = requiredString(created.session?.id);
    const suggestionSetId = requiredString(created.materialSuggestion?.id);
    await http.post(`/api/creative-sessions/${sessionId}/material-viewing`, {
      suggestionSetId, decision: "accept",
    }, "pilot-view-history-0001");
    const visible = await http.get(`/api/creative-sessions/${sessionId}/material-suggestions`);
    assertPilotInvariant(visible.suggestionSet.suggestions.some(
      (item: { segmentId: string }) => item.segmentId === SELECTED_HISTORY_ID));
    await http.post(`/api/creative-sessions/${sessionId}/material-selection`, {
      suggestionSetId, selectedSegmentIds: [SELECTED_HISTORY_ID],
    }, "pilot-select-history-0001");

    const scriptV1 = (await http.post(`/api/creative-sessions/${sessionId}/scripts`, {},
      "pilot-script-v1-0001")).script;
    const scriptV2 = (await http.post(`/api/creative-sessions/${sessionId}/script-revisions`, {
      prompt: "开头先说第三次返工，再讲当时为什么坚持。",
    }, "pilot-script-v2-0001")).script;
    const preConfirmationReceiptCount = (await library.listDispatchReceipts())
      .filter((item) => item.sessionId === sessionId).length;
    await http.post(`/api/creative-sessions/${sessionId}/script-confirmation`, { scriptVersion: 2 },
      "pilot-confirm-script-v2-0001");
    const shooting = await http.post(`/api/creative-sessions/${sessionId}/shooting-package`, {},
      "pilot-shooting-package-0001");
    const steps = shooting.shotPlan.steps as Array<{ id: string }>;
    assertPilotInvariant(steps.length === 2);
    const dispatch = await http.post(`/api/creative-sessions/${sessionId}/dispatch`, { confirmStart: true },
      "pilot-dispatch-0001");

    const version = { briefVersion: 1, scriptVersion: 2, shotPlanVersion: 1 };
    const stale = await http.postError(`/api/creative-sessions/${sessionId}/events`, {
      ...version, scriptVersion: 1, stepId: steps[0]!.id, action: "note",
      operationId: "pilot-stale-card-0001", deviceId: "watch_sim_01",
      deviceRole: "watch-director", reason: "stale task must fail",
    }, "pilot-stale-card-0001", 400);
    assertPilotInvariant(stale.error === "creative-event-version-not-current");
    const retake = { ...version, stepId: steps[0]!.id, action: "retake",
      operationId: "pilot-retake-0001", deviceId: "stone_a_sim_01", deviceRole: "main-stone-a" };
    await http.post(`/api/creative-sessions/${sessionId}/events`, retake, "pilot-retake-0001");
    await http.post(`/api/creative-sessions/${sessionId}/events`, retake, "pilot-retake-0001");
    await http.post(`/api/creative-sessions/${sessionId}/events`, {
      ...version, stepId: steps[0]!.id, action: "note", operationId: "pilot-note-0001",
      deviceId: "watch_sim_01", deviceRole: "watch-director", reason: "戒面反光已经降低",
    }, "pilot-note-0001");
    await http.post(`/api/creative-sessions/${sessionId}/events`, {
      ...version, stepId: steps[0]!.id, action: "completed", operationId: "pilot-completed-take2-0001",
      deviceId: "stone_a_sim_01", deviceRole: "main-stone-a",
    }, "pilot-completed-take2-0001");
    await http.post(`/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId: steps[0]!.id, deviceId: "stone_a_sim_01",
      deviceRole: "main-stone-a", carrier: "ring", take: 2, stepOutcome: "completed",
    }, "pilot-return-take2-0001");
    await http.post(`/api/creative-sessions/${sessionId}/events`, {
      ...version, stepId: steps[1]!.id, action: "skipped", operationId: "pilot-skip-step2-field-0001",
      deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", reason: "这个机位没有新增信息",
    }, "pilot-skip-step2-field-0001");
    await http.post(`/api/creative-sessions/${sessionId}/capture-handoff`, {
      mode: "a0-simulated-phone-return", stepId: steps[1]!.id, deviceId: "stone_b_sim_01",
      deviceRole: "main-stone-b", carrier: "pendant", take: 1, stepOutcome: "skipped",
    }, "pilot-skip-step2-0001");

    const ended = await http.post(`/api/creative-sessions/${sessionId}/end-handoff`, {
      mode: "a0-simulated-phone-return",
    }, "pilot-end-handoff-0001");
    assertPilotInvariant(ended.processingTask?.status === "ready-for-editorial");
    assertPilotInvariant(ended.stories === undefined && ended.artifacts === undefined);
    const suggested = await http.post(`/api/creative-sessions/${sessionId}/post-shoot-material-suggestions`, {},
      "pilot-post-suggest-0001");
    const postSetId = requiredString(suggested.materialSuggestion?.id);
    await http.post(`/api/creative-sessions/${sessionId}/post-shoot-material-viewing`, {
      suggestionSetId: postSetId, decision: "accept",
    }, "pilot-post-view-0001");
    const postShootVisible = await http.get(`/api/creative-sessions/${sessionId}/material-suggestions`);
    assertPilotInvariant(postShootVisible.suggestionSet.suggestions.some(
      (item: { segmentId: string }) => item.segmentId === SELECTED_HISTORY_ID));
    const selected = await http.post(`/api/creative-sessions/${sessionId}/post-shoot-material-selection`, {
      suggestionSetId: postSetId, selectedSegmentIds: [SELECTED_HISTORY_ID],
    }, "pilot-post-select-0001");
    assertPilotInvariant(selected.processingTask === undefined);
    const processed = await http.post(`/api/creative-sessions/${sessionId}/editorial-process`, {},
      "pilot-editorial-process-0001");
    assertPilotInvariant(processed.processingTask?.status === "reviewable");

    const beforeRevision = await http.get(`/api/creative-sessions/${sessionId}`);
    const story = beforeRevision.session.stories[0];
    assertPilotInvariant(Boolean(story?.id));
    const revised = await http.post(`/api/creative-sessions/${sessionId}/stories/${story.id}/revisions`, {
      prompt: "视频号开头更直接，先说第三次返工。",
    }, "pilot-editorial-prompt-0001");
    const artifacts = revised.artifacts as Array<{
      id: string; kind: VideoVariant; mediaUrl: string;
    }>;
    assertPilotInvariant(artifacts.length === 4);
    assertPilotInvariant(!JSON.stringify(revised).includes("videoPath") &&
      !JSON.stringify(revised).includes("coverPath") && !JSON.stringify(revised).includes("stillPaths"));
    const wechat = artifacts.find((item) => item.kind === "wechat-channels");
    assertPilotInvariant(Boolean(wechat));
    await http.media(requiredString(wechat!.mediaUrl));
    const approval = (await http.post(
      `/api/creative-sessions/${sessionId}/artifacts/${wechat!.id}/approve`, {},
      "pilot-approve-exact-0001")).approval;
    const exportPackage = (await http.post(
      `/api/creative-sessions/${sessionId}/approvals/${approval.id}/export`, {},
      "pilot-export-0001")).package;
    assertPilotInvariant(!JSON.stringify(exportPackage).includes("Path"));
    const delivery = (await http.post(
      `/api/creative-sessions/${sessionId}/export-packages/${exportPackage.id}/deliver`, {},
      "pilot-deliver-local-0001")).delivery;
    assertPilotInvariant(delivery.status === "received" && delivery.publishedClaimed === false);

    const detail = await http.get(`/api/creative-sessions/${sessionId}`);
    assertPilotInvariant(detail.session.processingTask?.status === "reviewable");
    assertPilotInvariant(detail.session.revisions.some(
      (item: { id: string }) => item.id === revised.revision.id));
    assertPilotInvariant(detail.session.artifacts.length === detail.session.revisions.length * 4);
    for (const artifact of detail.session.artifacts) {
      await http.media(requiredString(artifact.mediaUrl));
    }
    const events = (await library.listCreativeSessionEvents()).filter((item) =>
      item.sessionId === sessionId && ["completed", "retake", "skipped", "note"].includes(item.action));
    const fieldEvents = events.filter((item) => !item.id.startsWith("creative_capture_"));
    const duplicateEventCount = events.length - new Set(events.map((item) => item.idempotencyKey)).size;
    const scripts = (await library.listScriptPackages()).filter((item) => item.sessionId === sessionId);
    const unselectedReferenceCount = scripts.flatMap((item) => item.sections)
      .flatMap((item) => item.referencedSegmentIds).filter((id) => id === UNSELECTED_HISTORY_ID).length;
    const returnedAssets = (await library.listAssets()).filter((item) =>
      item.captureContext?.creativeSessionId === sessionId);
    assertPilotInvariant(scriptV1.scriptVersion === 1 && scriptV2.scriptVersion === 2);
    assertPilotInvariant(preConfirmationReceiptCount === 0 && dispatch.receipts.length === 4);
    assertPilotInvariant(unselectedReferenceCount === 0 && duplicateEventCount === 0);
    assertPilotInvariant(returnedAssets.length === 1 && returnedAssets[0]!.captureContext?.take === 2);
    assertPilotInvariant(publisher.receipts.length === 1);

    return {
      creativeSessionId: sessionId,
      historicalConsent: { declinedHistoryCount: 0, viewedButUnselectedCount: 0,
        exactSelectedIds: [SELECTED_HISTORY_ID] },
      script: { versions: [1, 2], confirmedVersion: 2, unselectedReferenceCount: 0 },
      dispatch: { transport: "simulated", receiptCount: dispatch.receipts.length, preConfirmationReceiptCount: 0 },
      fieldActions: { completed: countAction(fieldEvents, "completed"), retake: countAction(fieldEvents, "retake"),
        skipped: countAction(fieldEvents, "skipped"), notes: countAction(fieldEvents, "note"), forcedReshootCount: 0 },
      staleTaskRejected: true, duplicateEventCount: 0, returnedAssetCount: returnedAssets.length,
      storyTypes: detail.session.stories.map((item: { type: StoryType }) => item.type),
      variantKinds: artifacts.map((item) => item.kind),
      approvedVariant: { variant: approval.variant, revision: approval.revision },
      publishedClaimed: false,
    };
  } finally {
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

class PilotHttpClient {
  #client = "";
  #claim = "";
  constructor(private readonly baseUrl: string) {}
  async open() {
    const value = await parseResponse(await fetch(`${this.baseUrl}/api/session`));
    this.#client = requiredString(value.clientInstanceId);
    this.#claim = requiredString(value.clientClaim);
  }
  async get(path: string) {
    return parseResponse(await fetch(`${this.baseUrl}${path}`, { headers: { "x-review-token": REVIEW_TOKEN } }));
  }
  async media(path: string) {
    const response = await fetch(`${this.baseUrl}${path}`, { headers: {
      "x-review-token": REVIEW_TOKEN, referer: `${this.baseUrl}/`,
    } });
    if (!response.ok || !response.headers.get("content-type")?.startsWith("video/mp4") ||
        (await response.arrayBuffer()).byteLength === 0) {
      throw new Error(`pilot-media-${response.status}`);
    }
  }
  async post(path: string, body: Record<string, unknown>, key: string, creative = true) {
    return parseResponse(await fetch(`${this.baseUrl}${path}`, {
      method: "POST", body: JSON.stringify(body), headers: {
        "content-type": "application/json", "x-review-token": REVIEW_TOKEN, "x-operation-key": key,
        ...(creative ? { "x-client-instance": this.#client, "x-client-claim": this.#claim } : {}),
      },
    }));
  }
  async postError(path: string, body: Record<string, unknown>, key: string, status: number) {
    const response = await fetch(`${this.baseUrl}${path}`, { method: "POST", body: JSON.stringify(body), headers: {
      "content-type": "application/json", "x-review-token": REVIEW_TOKEN, "x-operation-key": key,
      "x-client-instance": this.#client, "x-client-claim": this.#claim,
    } });
    const value = await response.json() as Record<string, any>;
    if (response.status !== status) throw new Error(`pilot-http-expected-${status}-got-${response.status}:${JSON.stringify(value)}`);
    return value;
  }
}

async function parseResponse(response: Response): Promise<Record<string, any>> {
  const value = await response.json() as Record<string, any>;
  if (!response.ok) throw new Error(`pilot-http-${response.status}:${JSON.stringify(value)}`);
  return value;
}

function pilotDependencies(library: JsonContentStore, workDir: string,
  phonePublisher: PhonePublisherAdapter): ReviewAppDependencies {
  const capabilities = [
    { deviceId: "watch_sim_01", deviceRole: "watch-director" as const, carrier: "watch" },
    { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a" as const, carrier: "ring" },
    { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b" as const, carrier: "pendant" },
  ];
  const mailboxes = { "watch-director": new LocalDispatchMailbox("watch_sim_01"),
    "main-stone-a": new LocalDispatchMailbox("stone_a_sim_01"),
    "main-stone-b": new LocalDispatchMailbox("stone_b_sim_01") };
  return { library, outputDir: join(workDir, "review-renders"), designerId: DESIGNER_ID,
    reviewToken: REVIEW_TOKEN, phonePublisher, interpreter: promptInterpreter(), creative: {
      intentInterpreter: intentInterpreter(), materialMatcher: materialMatcher(), scriptAgent: scriptAgent(),
      shotPlanningAgent: shotPlanningAgent(), editorial: editorialAnalyzers(), creativeDeviceAdapters: {
        deviceCapabilities: capabilities,
        privacyDevices: capabilities.map(({ deviceId }) => ({ deviceId, pausePrivacy() {}, resumePrivacy() {},
          isPrivacyPaused() { return false; } })),
        createRoster: (version) => createLocalDispatchRoster({ mailboxes, version }).roster,
      },
    } };
}

function intentInterpreter(): IntentInterpreter {
  return { interpret: async () => ({ platforms: ["wechat-channels", "xiaohongshu"],
    audience: "喜欢手工珠宝的客户", theme: "绿松石戒指第三次返工",
    products: ["turquoise_ring_01"], shootConditions: ["本地合成工作台"],
    prohibitedAreas: ["客户面部", "订单信息"], tone: "平静真诚", durationSeconds: 30 }) };
}
function materialMatcher(): MaterialMatcher {
  return { rank: async ({ segments }) => segments.map((segment, index) => ({ segmentId: segment.id,
    reason: "同一件绿松石戒指的可用历史素材", score: 100 - index })) };
}
function scriptAgent(): CreativeScriptAgent {
  const draft = (opening: string, selected: ContentSegment[], explanation?: string) => ({
    title: "绿松石戒指的第三次返工", platformDirection: "wechat-channels", opening,
    narrativeOrder: ["customer", ...selected.map((_, i) => `history-${i + 1}`), "shoot"], sections: [
      { id: "customer", expression: opening, source: "customer-input" as const, referencedSegmentIds: [] },
      ...selected.map((segment, i) => ({ id: `history-${i + 1}`, expression: "使用客户明确选择的旧素材",
        source: "historical-material" as const, referencedSegmentIds: [segment.id] })),
      { id: "shoot", expression: "拍摄当下过程与感受", source: "shoot-suggestion" as const,
        referencedSegmentIds: [] },
    ], ...(explanation ? { changeExplanation: explanation } : {}) });
  return { draft: async ({ selectedHistoricalSegments }) => draft("为什么这枚戒指做了第三次？",
    [...selectedHistoricalSegments]), revise: async ({ selectedHistoricalSegments, prompt }) =>
      draft("第三次返工，不是重来，是把遗憾做完。", [...selectedHistoricalSegments], `已执行：${prompt}`) };
}
function shotPlanningAgent(): ShotPlanningAgent {
  const step = (purpose: string, role: "main-stone-a" | "main-stone-b", optional: boolean) => ({
    purpose, expressionSuggestion: optional ? "没有新信息时可以跳过" : "说出为什么仍然决定再做一次",
    visualDirection: role === "main-stone-a" ? "主石 A 拍戒面近景" : "主石 B 拍工作台",
    durationSeconds: optional ? 10 : 20, mustCapture: [optional ? "工作台" : "戒面"],
    optionalGapSuggestions: [{ suggestion: optional ? "无合适画面可直接跳过" : "方便时补工具特写", required: false as const }],
    completionCriteria: [optional ? "拍摄或明确跳过" : "原因和决定都清楚"],
    deviceRoles: ["watch-director" as const, role], privacyReminder: "不拍客户面部和订单信息" });
  return { plan: async () => ({ steps: [step("讲清第三次返工的原因和心路", "main-stone-a", false),
    step("补一段工作台全景", "main-stone-b", true)] }) };
}
function editorialAnalyzers() {
  const segmentAnalyzer = { analyze: async () => [{ startMs: 0, endMs: 1_200,
    transcript: "我认为手工应该留下痕迹，我犹豫过，但最后决定再做一次。", signals: [] as const }] };
  const tagAnalyzer: SegmentTagAnalyzer = { suggest: async (segment) => ({ baseTypes: ["persona-copy", "handmade"],
    people: [DESIGNER_ID], products: [], processStages: ["rework"], topics: ["product-making"],
    emotions: ["hesitation", "persistence"], transcript: segment.transcript, technicalFlags: [],
    usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null } }) };
  const storyAnalyzer: StoryAnalyzer = { propose: async (segments) => [
    { type: "persona-monologue", anchorSegmentIds: segments.map((item) => item.id) },
    { type: "inner-journey", anchorSegmentIds: segments.map((item) => item.id) }] };
  const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
    const segmentId = story.anchorSegmentIds[0]!;
    const timeline = (outMs: number) => [{ segmentId, inMs: 0, outMs, role: "anchor" as const }];
    const cover = { segmentId, atMs: 100 };
    return { master: { timeline: timeline(400), title: "原视频", caption: "完整叙事", cover },
      xiaohongshu: { timeline: timeline(600), title: "小红书", caption: "情绪开场", cover },
      douyin: { timeline: timeline(700), title: "抖音", caption: "节奏版", cover },
      wechatChannels: { timeline: timeline(800), title: "视频号", caption: "口播开场", cover }, stills: [] };
  } };
  return { segmentAnalyzer, tagAnalyzer, storyAnalyzer, editAnalyzer };
}
function promptInterpreter(): PromptEditInterpreter {
  return { interpret: async ({ revision }) => ({ targetVariants: ["wechat-channels"],
    replacementTimelines: { "wechat-channels": [...revision.variants.find(
      (item) => item.kind === "wechat-channels")!.timeline] }, removePeople: [],
    explanation: "视频号开头已改为直接交代第三次返工" }) };
}
function localPhonePublisher(): PhonePublisherAdapter & { receipts: string[] } {
  const receipts: string[] = [];
  return { receipts, deliver: async ({ idempotencyKey }) => { receipts.push(idempotencyKey); return {
    receiptId: `local-phone-${idempotencyKey.slice(-12)}`, receivedAt: new Date().toISOString() }; } };
}

async function seedHistoricalMaterial(library: JsonContentStore, workDir: string) {
  const selectedPath = join(workDir, "history-rework.mp4");
  const unselectedPath = join(workDir, "history-unselected.mp4");
  await makeSyntheticClip(selectedPath, "blue", 1.5);
  await makeSyntheticClip(unselectedPath, "yellow", 1.5);
  const selected = await historicalAsset("history_asset_rework", selectedPath, library);
  const unselected = await historicalAsset("history_asset_unselected", unselectedPath, library);
  await library.putSegments([historicalSegment(SELECTED_HISTORY_ID, selected, "rework", 0),
    historicalSegment(UNSELECTED_HISTORY_ID, unselected, "material-selection", 1)]);
}
async function historicalAsset(id: string, path: string, library: JsonContentStore) {
  const bytes = await readFile(path);
  return library.putAsset({ id, batchId: "historical_batch", sessionId: "historical_session",
    digest: createHash("sha256").update(bytes).digest("hex"), localPath: path, mediaKind: "video",
    importedAt: "2025-09-04T00:00:00.000Z", permissionState: "allowed", origin: "historical", technicalFlags: [] });
}
function historicalSegment(id: string, asset: LibraryAsset, processStage: string, useCount: number): ContentSegment {
  return { id, assetId: asset.id, batchId: asset.batchId, startMs: 0, endMs: 1_000, permissionState: "allowed",
    tags: { baseTypes: ["handmade"], people: [DESIGNER_ID], products: [{ id: "turquoise_ring_01", status: "confirmed" }],
      processStages: [processStage], topics: ["product-making"], emotions: ["persistence"], transcript: "historical rework",
      technicalFlags: [], usage: { storyIds: [], variants: [], useCount, lastUsedAt: null } } };
}
function countAction(events: Awaited<ReturnType<JsonContentStore["listCreativeSessionEvents"]>>, action: string) {
  return events.filter((event) => event.action === action).length;
}
function requiredString(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) throw new Error("creative-pilot-required-string");
  return value;
}
function assertPilotInvariant(value: unknown): asserts value {
  if (!value) throw new Error("creative-director-pilot-invariant-failed");
}
