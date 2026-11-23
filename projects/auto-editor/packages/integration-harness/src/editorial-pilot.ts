import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

import type {
  CaptureSession,
  ContentSegment,
  LibraryAsset,
  SegmentTags,
  StoryType,
  VideoVariant,
} from "@wearable/contracts";
import {
  JsonContentStore,
  approveVariant,
  confirmSegmentTags,
  confirmPostShootMaterialSelection,
  decidePostShootMaterialViewing,
  suggestPostShootHistoricalMaterials,
  resolveHistoricalMaterialAuthorization,
  createExportPackage,
  decomposeStories,
  deliverExportPackage,
  importGlass3LocalOriginal,
  ingestNewBatch,
  planInitialRevision,
  renderRevision,
  reviseWithPrompt,
  segmentNewBatch,
  tagSegments,
  type BoundarySuggestion,
  type EditPlanAnalyzer,
  type ImportedMedia,
  type PhonePublisherAdapter,
  type PromptEditInterpreter,
  type SegmentTagAnalyzer,
  type SegmentationAnalyzer,
  type StoryAnalyzer,
} from "@wearable/mobile-agent";
import ffmpegPath from "ffmpeg-static";

export type EditorialPilotReport = Readonly<{
  newBatchStoryCounts: Record<StoryType, number>;
  everyStoryHasNewAnchor: boolean;
  historicalMatchesHaveReasons: boolean;
  variantKinds: VideoVariant[];
  promptRevision: { from: number; to: number; oldRevisionPreserved: boolean };
  approvedVariant: { revision: number; variant: VideoVariant };
  delivery: { status: "received"; receiptId: string; publishedClaimed: false };
  renderedArtifactPaths: string[];
  renderedStillPaths: string[];
  duplicateAssetCount: number;
  damagedAssetPreserved: boolean;
  reshootRequestCount: 0;
  unauthorizedPublicSegmentCount: 0;
}>;

export async function runSyntheticEditorialPilot(
  workDir: string,
): Promise<EditorialPilotReport> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  await mkdir(workDir, { recursive: true });
  const currentPath = join(workDir, "current-batch.mp4");
  const historyPath = join(workDir, "history-before.mp4");
  const damagedPath = join(workDir, "damaged.mp4");
  const unrelatedStillPath = join(workDir, "overused-unrelated.jpg");
  await makeSyntheticClip(currentPath, "green", 5.5);
  await makeSyntheticClip(historyPath, "blue", 1.5);
  await writeFile(damagedPath, Buffer.from("deliberately-damaged-video"));
  await writeFile(unrelatedStillPath, Buffer.from("synthetic-unrelated-still"));

  const library = await JsonContentStore.open(join(workDir, "content-library.json"));
  const currentMedia = await importedMedia(currentPath);
  const damagedMedia = await importedMedia(damagedPath);
  const session: CaptureSession = {
    id: "pilot_session_01",
    startedAt: "2026-09-04T00:00:00.000Z",
    mode: "maker-brooch",
    privacyState: "creator",
    visitorConsent: "not-required",
    status: "stopped",
    productRef: "prod_01",
  };
  const ingestResult = await ingestNewBatch(
    {
      batchId: "batch_new",
      session,
      media: [currentMedia, currentMedia, damagedMedia],
      mediaKinds: {
        [currentMedia.id]: "video",
        [damagedMedia.id]: "video",
      },
    },
    library,
  );
  const duplicateAssetCount =
    ingestResult.assets.length - new Set(ingestResult.assets.map((asset) => asset.id)).size;
  const currentAssets = (await library.listAssets()).filter(
    (asset) => asset.batchId === "batch_new",
  );

  const segmentationAnalyzer: SegmentationAnalyzer = {
    analyze: async (asset) => {
      if (asset.localPath === damagedPath) throw new Error("decoder-failed");
      return currentBoundaries();
    },
  };
  const untyped = await segmentNewBatch(
    "batch_new",
    currentAssets,
    segmentationAnalyzer,
    library,
  );
  const suggested = await tagSegments(untyped, pilotTagAnalyzer(), library);
  const tagged = await Promise.all(suggested.map(async (segment) => {
    if (!["PROCESS_START", "PRODUCT_RESULT"].includes(segment.tags.transcript)) return segment;
    return confirmSegmentTags(
      segment.id,
      { products: segment.tags.products.map((product) => ({ ...product, status: "confirmed" })) },
      "designer_01",
      library,
    );
  }));

  const historyAsset = await historicalAsset(
    "asset_history_before",
    historyPath,
    "video",
    library,
  );
  const visitorAsset = await historicalAsset(
    "asset_visitor",
    unrelatedStillPath,
    "image",
    library,
  );
  const unrelatedAsset = await historicalAsset(
    "asset_unrelated",
    unrelatedStillPath,
    "image",
    library,
  );
  const historicalSegments = [
    historySegment(historyAsset),
    visitorSegment(visitorAsset),
    overusedUnrelatedSegment(unrelatedAsset),
  ];
  await library.putSegments(historicalSegments);
  await library.putCreativeSession({ id: "editorial_pilot_creative", customerId: "designer_01", title: "A-0 synthetic consent fixture",
    stage: "editorial_review", currentIntentVersion: 1, currentBriefVersion: 0, currentScriptVersion: 0,
    currentShotPlanVersion: 0, materialViewDecisionIds: [], materialUseDecisionIds: [], deviceBindings: [],
    captureBatchIds: ["batch_new"], editorialBatchId: "batch_new", privacyMode: "creator", prohibitedAreas: [],
    status: "active", createdAt: "2026-09-04T00:00:00.000Z", updatedAt: "2026-09-04T00:00:00.000Z" });
  await library.putCreativeIntent({ id: "editorial_pilot_intent", sessionId: "editorial_pilot_creative", version: 1,
    platforms: ["editorial-master"], audience: "A-0 fixture", theme: "craft", products: [], shootConditions: [],
    prohibitedAreas: [], tone: "synthetic", durationSeconds: 5, createdAt: "2026-09-04T00:00:00.000Z" });
  const authorization = await confirmSyntheticPostShootConsent("editorial_pilot_creative", [historySegment(historyAsset).id], library);

  const storyAnalyzer: StoryAnalyzer = {
    propose: async (segments) => {
      const byTranscript = new Map(
        segments.map((segment) => [segment.tags.transcript, segment.id]),
      );
      return [
        {
          type: "product-journey",
          anchorSegmentIds: [
            requiredId(byTranscript, "PROCESS_START"),
            requiredId(byTranscript, "PRODUCT_RESULT"),
          ],
        },
        {
          type: "product-journey",
          anchorSegmentIds: [requiredId(byTranscript, "REWORK")],
        },
        {
          type: "inner-journey",
          anchorSegmentIds: [
            requiredId(byTranscript, "我犹豫过，但最后决定再做一次"),
            requiredId(byTranscript, "REWORK"),
          ],
        },
        {
          type: "persona-monologue",
          anchorSegmentIds: [
            requiredId(byTranscript, "我认为好珠宝不应该掩盖手工的痕迹"),
          ],
        },
      ];
    },
  };
  const stories = await decomposeStories(
    {
      batchId: "batch_new",
      newSegments: tagged,
      authorization,
      historicalSegments,
    },
    storyAnalyzer,
    library,
  );

  const productStory = stories.find(
    (story) => story.type === "product-journey" && story.anchorSegmentIds.length === 2,
  );
  if (!productStory) throw new Error("pilot-product-story-missing");
  const allSegments = await library.listSegments();
  const storySegments = allSegments.filter((segment) =>
    [...productStory.anchorSegmentIds, ...productStory.historicalSegmentIds].includes(
      segment.id,
    ),
  );
  const planner = pilotPlanner(productStory.anchorSegmentIds, productStory.historicalSegmentIds);
  const revision1 = await planInitialRevision(productStory, storySegments, planner);
  await library.putRevision(revision1);
  const firstRender = await renderRevision(
    revision1,
    allSegments,
    await library.listAssets(),
    join(workDir, "renders"),
  );
  for (const artifact of firstRender.artifacts) await library.putArtifact(artifact);

  const withdrawnId = visitorSegment(visitorAsset).id;
  const unauthorizedInterpreter: PromptEditInterpreter = {
    interpret: async () => ({
      targetVariants: ["wechat-channels"],
      replacementTimelines: {
        "wechat-channels": [timelineItem(withdrawnId)],
      },
      removePeople: [],
      explanation: "invalid visitor request",
    }),
  };
  let permissionRejected = false;
  try {
    await reviseWithPrompt(
      {
        revision: revision1,
        prompt: "use withdrawn visitor",
        allowedSegments: allSegments,
        designerId: "designer_01",
      },
      unauthorizedInterpreter,
      library,
    );
  } catch (error) {
    permissionRejected =
      error instanceof Error &&
      error.message === "requested-material-not-in-authorized-library";
  }
  if (!permissionRejected) throw new Error("pilot-permission-rejection-missing");

  const historyId = productStory.historicalSegmentIds[0];
  if (!historyId) throw new Error("pilot-history-match-missing");
  const validInterpreter: PromptEditInterpreter = {
    interpret: async () => ({
      targetVariants: ["wechat-channels"],
      replacementTimelines: {
        "wechat-channels": [
          timelineItem(historyId),
          timelineItem(productStory.anchorSegmentIds[0]),
        ],
      },
      removePeople: ["visitor_01"],
      explanation: "open with the authorized historical before-stage shot",
    }),
  };
  const revision2 = await reviseWithPrompt(
    {
      revision: revision1,
      prompt: "视频号开头换成去年返工前的旧镜头，不要访客",
      allowedSegments: allSegments,
      designerId: "designer_01",
    },
    validInterpreter,
    library,
  );
  const secondRender = await renderRevision(
    revision2,
    allSegments,
    await library.listAssets(),
    join(workDir, "renders"),
  );
  for (const artifact of secondRender.artifacts) await library.putArtifact(artifact);
  const approvedArtifact = secondRender.artifacts.find(
    (artifact) => artifact.kind === "wechat-channels",
  );
  if (!approvedArtifact) throw new Error("pilot-approved-artifact-missing");
  const approval = await approveVariant(
    {
      storyId: productStory.id,
      revisionId: revision2.id,
      revision: revision2.revision,
      artifactId: approvedArtifact.id,
      variant: approvedArtifact.kind,
      reviewerId: "designer_01",
    },
    library,
  );
  const exportPackage = await createExportPackage(
    {
      approvalId: approval.id,
      artifactId: approvedArtifact.id,
      revisionId: approvedArtifact.revisionId,
      creativeSessionId: "editorial_pilot_creative",
    },
    library,
  );
  let deliveryCalls = 0;
  const phoneAdapter: PhonePublisherAdapter = {
    deliver: async () => {
      deliveryCalls += 1;
      return {
        receiptId: "synthetic-phone-receipt-01",
        receivedAt: "2026-09-04T02:00:00.000Z",
      };
    },
  };
  const delivery = await deliverExportPackage(exportPackage.id, phoneAdapter, library, "editorial_pilot_creative");
  await deliverExportPackage(exportPackage.id, phoneAdapter, library, "editorial_pilot_creative");
  if (deliveryCalls !== 1) throw new Error("pilot-idempotent-delivery-failed");
  if (delivery.status !== "received" || !delivery.receiptId) {
    throw new Error("pilot-delivery-receipt-missing");
  }

  const storedAssets = await library.listAssets();
  const revisions = await library.listRevisions(productStory.id);
  const segmentById = new Map((await library.listSegments()).map((segment) => [segment.id, segment]));
  const unauthorizedPublicSegmentCount = approvedArtifact.sourceSegmentIds.filter(
    (segmentId) => segmentById.get(segmentId)?.permissionState !== "allowed",
  ).length;
  if (unauthorizedPublicSegmentCount !== 0) {
    throw new Error("pilot-unauthorized-public-segment");
  }

  return {
    newBatchStoryCounts: countStories(stories.map((story) => story.type)),
    everyStoryHasNewAnchor: stories.every((story) =>
      story.anchorSegmentIds.every((segmentId) =>
        tagged.some(
          (segment) => segment.id === segmentId && segment.batchId === "batch_new",
        ),
      ),
    ),
    historicalMatchesHaveReasons: stories.every((story) =>
      story.historicalSegmentIds.every(
        (segmentId) => (story.historicalReasons[segmentId] ?? "").length > 0,
      ),
    ),
    variantKinds: revision2.variants.map((variant) => variant.kind),
    promptRevision: {
      from: revision1.revision,
      to: revision2.revision,
      oldRevisionPreserved: revisions.some((revision) => revision.id === revision1.id),
    },
    approvedVariant: { revision: approval.revision, variant: approval.variant },
    delivery: {
      status: "received",
      receiptId: delivery.receiptId,
      publishedClaimed: false,
    },
    renderedArtifactPaths: secondRender.artifacts.map((artifact) => artifact.videoPath),
    renderedStillPaths: secondRender.stillPaths,
    duplicateAssetCount,
    damagedAssetPreserved: storedAssets.some(
      (asset) =>
        asset.localPath === damagedPath &&
        asset.technicalFlags.includes("segmentation-failed"),
    ),
    reshootRequestCount: 0,
    unauthorizedPublicSegmentCount: 0,
  };
}

/** Scripted customer decisions for the local A-0 fixture only. */
export async function confirmSyntheticPostShootConsent(sessionId: string, selectedSegmentIds: string[], library: JsonContentStore) {
  const session = (await library.listCreativeSessions()).find((item) => item.id === sessionId)!;
  const input = { sessionId, actorId: session.customerId, now: "2026-09-04T08:29:30.000Z" };
  const set = await suggestPostShootHistoricalMaterials(input, {
    rank: async ({ segments }) => segments.filter((segment) => selectedSegmentIds.includes(segment.id))
      .map((segment) => ({ segmentId: segment.id, score: 1, reason: "A-0 scripted exact post-shoot selection" })),
  }, library);
  await decidePostShootMaterialViewing({ ...input, suggestionSetId: set.id, decision: "accept" }, library);
  await confirmPostShootMaterialSelection({ ...input, suggestionSetId: set.id, selectedSegmentIds }, library);
  return resolveHistoricalMaterialAuthorization(sessionId, library);
}

export function currentBoundaries(): BoundarySuggestion[] {
  return [
    { startMs: 0, endMs: 1_000, transcript: "PROCESS_START", signals: ["action-change"] },
    { startMs: 1_000, endMs: 2_000, transcript: "PRODUCT_RESULT", signals: ["product-change"] },
    { startMs: 2_000, endMs: 3_000, transcript: "REWORK", signals: ["action-change"] },
    { startMs: 3_000, endMs: 4_000, transcript: "我犹豫过，但最后决定再做一次", signals: ["topic-change"] },
    { startMs: 4_000, endMs: 5_000, transcript: "我认为好珠宝不应该掩盖手工的痕迹", signals: ["topic-change"] },
  ];
}

export function pilotTagAnalyzer(): SegmentTagAnalyzer {
  return {
    suggest: async (segment) => {
      const common = baseTags(segment.transcript);
      if (segment.transcript === "PROCESS_START") {
        return { ...common, baseTypes: ["handmade"], products: [{ id: "prod_01", status: "confirmed" }], processStages: ["material-selection", "making"] };
      }
      if (segment.transcript === "PRODUCT_RESULT") {
        return { ...common, baseTypes: ["product-image"], products: [{ id: "prod_01", status: "confirmed" }], processStages: ["finished"] };
      }
      if (segment.transcript === "REWORK") {
        return { ...common, baseTypes: ["handmade"], products: [{ id: "prod_01", status: "confirmed" }], processStages: ["making", "rework"], emotions: ["persistence"] };
      }
      if (segment.transcript.startsWith("我犹豫")) {
        return { ...common, baseTypes: ["persona-copy"], emotions: ["frustration"] };
      }
      return { ...common, baseTypes: ["persona-copy", "handmade"], products: [{ id: "prod_01", status: "confirmed" }] };
    },
  };
}

function baseTags(transcript: string): SegmentTags {
  return {
    baseTypes: ["scene"], people: ["designer_01"], products: [], processStages: [],
    topics: ["product-making"], emotions: [], transcript, technicalFlags: [],
    usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
  };
}

export function pilotPlanner(anchorIds: string[], historyIds: string[]): EditPlanAnalyzer {
  const historyId = historyIds[0];
  if (!historyId) throw new Error("pilot-history-match-missing");
  return {
    plan: async () => ({
      master: variant([historyId, ...anchorIds], "产品完整历程"),
      xiaohongshu: variant([...anchorIds].reverse(), "为什么这件珠宝要这样做"),
      douyin: variant([...anchorIds, historyId], "抖音节奏版"),
      wechatChannels: variant(anchorIds, "从选材到完成"),
      stills: [{ segmentId: anchorIds[1], atMs: 300, reason: "clear finished-product frame" }],
    }),
  };
}

function variant(ids: string[], title: string) {
  return { timeline: ids.map(timelineItem), title, caption: `${title}的完整记录`, cover: { segmentId: ids[0], atMs: 200 } };
}

export function timelineItem(segmentId: string) {
  return { segmentId, inMs: 0, outMs: 700, role: "anchor" as const };
}

async function historicalAsset(
  id: string,
  localPath: string,
  mediaKind: LibraryAsset["mediaKind"],
  library: JsonContentStore,
): Promise<LibraryAsset> {
  const bytes = await readFile(localPath);
  return await library.putAsset({
    id, batchId: "batch_history", sessionId: "history_session", digest: sha256(bytes),
    localPath, mediaKind, importedAt: "2025-09-04T00:00:00.000Z", permissionState: "allowed",
    origin: "historical", technicalFlags: [],
  });
}

function historySegment(asset: LibraryAsset): ContentSegment {
  return segment("history_before_01", asset, "allowed", {
    baseTypes: ["handmade"], products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
  });
}

function visitorSegment(asset: LibraryAsset): ContentSegment {
  return segment("visitor_withdrawn_01", asset, "withdrawn", { baseTypes: ["customer-interaction"], people: ["visitor_01"] });
}

function overusedUnrelatedSegment(asset: LibraryAsset): ContentSegment {
  return segment("unrelated_overused_01", asset, "allowed", {
    baseTypes: ["product-image"], products: [{ id: "other_product", status: "confirmed" }],
    usage: { storyIds: ["old_story"], variants: ["xiaohongshu"], useCount: 20, lastUsedAt: "2026-09-03T00:00:00.000Z" },
  });
}

function segment(
  id: string,
  asset: LibraryAsset,
  permissionState: ContentSegment["permissionState"],
  patch: Partial<SegmentTags>,
): ContentSegment {
  return {
    id, assetId: asset.id, batchId: "batch_history", startMs: 0, endMs: 1_000, permissionState,
    tags: { ...baseTags(""), ...patch },
  };
}

function countStories(types: StoryType[]): Record<StoryType, number> {
  const counts: Record<StoryType, number> = { "inner-journey": 0, "product-journey": 0, "persona-monologue": 0 };
  types.forEach((type) => { counts[type] += 1; });
  return counts;
}

export async function importedMedia(path: string): Promise<ImportedMedia> {
  const originalBytes = await readFile(path);
  const digest = sha256(originalBytes);
  return importGlass3LocalOriginal({
    localPath: path,
    originalBytes,
    manifest: { origin: "phone-local-original", deviceModel: "Glass3-synthetic", importedAt: "2026-09-04T00:00:00.000Z", digest },
  });
}

function requiredId(index: Map<string, string>, key: string): string {
  const id = index.get(key);
  if (!id) throw new Error(`pilot-segment-missing:${key}`);
  return id;
}

export async function makeSyntheticClip(path: string, color: string, duration: number): Promise<void> {
  await run(ffmpegPath!, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", `color=c=${color}:s=160x120:r=25:d=${duration}`, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", path]);
}

async function run(command: string, args: string[]): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(`synthetic-media-failed:${stderr}`)));
  });
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}
