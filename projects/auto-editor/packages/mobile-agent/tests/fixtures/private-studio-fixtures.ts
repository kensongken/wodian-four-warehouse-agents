import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { StudioMediaAsset } from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";

import {
  CustomerMaterialLibrary,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  StudioPreferenceRanker,
  type EditorialDecisionAdapter,
  type StudioEditorialTranscriptSpan,
  type StudioEditorialBeatEvidence,
  type StudioEditorialVisualEvidence,
  type StudioEditorialMusicTrack,
  type StudioRenderSource,
  type StudioScope,
} from "../../src";

export const studioScope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "studio-session-1",
};

const syntheticClipCache = new Map<string, Buffer>();

export async function prepareRenderableStudio(options: Readonly<{
  editorial?: EditorialDecisionAdapter;
  resolveEditorialTranscriptSpans?: (
    cards: readonly import("@wearable/contracts").MaterialCard[],
  ) => Promise<readonly StudioEditorialTranscriptSpan[]>;
  resolveEditorialVisualEvidence?: (
    cards: readonly import("@wearable/contracts").MaterialCard[],
  ) => Promise<readonly StudioEditorialVisualEvidence[]>;
  resolveEditorialBeatEvidence?: (
    cards: readonly import("@wearable/contracts").MaterialCard[],
    sources: readonly StudioRenderSource[],
  ) => Promise<readonly StudioEditorialBeatEvidence[]>;
  segmentDurationMs?: number | readonly [number, number, number, number];
  withPreferenceRanker?: boolean;
  withMusicTrack?: boolean;
}> = {}) {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const segmentDurationMs = options.segmentDurationMs ?? 10_500;
  const root = await mkdtemp(join(tmpdir(), "private-studio-rendering-"));
  const library = new CustomerMaterialLibrary(join(root, "library"), () => true);
  const preferenceRanker = options.withPreferenceRanker
    ? new StudioPreferenceRanker(library, { now: () => "2026-09-10T08:00:00.000Z" })
    : undefined;
  let musicTrack: StudioEditorialMusicTrack | null = null;
  const studio = new PrivateContentStudio(
    library,
    new OfflineFixtureUnderstandingAdapter(),
    {
      now: () => "2026-09-10T08:00:00.000Z",
      outputRoot: join(root, "drafts"),
      editorial: options.editorial,
      resolveEditorialTranscriptSpans: options.resolveEditorialTranscriptSpans === undefined
        ? undefined
        : async (_scope, cards) => options.resolveEditorialTranscriptSpans!(cards),
      resolveEditorialVisualEvidence: options.resolveEditorialVisualEvidence === undefined
        ? undefined
        : async (_scope, cards) => options.resolveEditorialVisualEvidence!(cards),
      resolveEditorialBeatEvidence: options.resolveEditorialBeatEvidence === undefined
        ? undefined
        : async (_scope, cards, sources) => options.resolveEditorialBeatEvidence!(cards, sources),
      resolveEditorialMusicTrack: async () => musicTrack,
      preferenceRanker,
    },
  );
  const definitions = [
    ["red", true],
    ["blue", false],
    ["green", true],
    ["yellow", true],
  ] as const;
  const assets: StudioMediaAsset[] = [];
  for (const [index, [color, withAudio]] of definitions.entries()) {
    const assetDurationMs = Array.isArray(segmentDurationMs)
      ? segmentDurationMs[index]!
      : segmentDurationMs;
    const segmentDurationSeconds = assetDurationMs / 1_000;
    const path = join(root, `source-${index + 1}.mp4`);
    await makeSyntheticClip(path, color, withAudio, segmentDurationSeconds);
    const asset = await library.importOriginal(studioScope.actorId, studioScope.customerId, {
      requestId: `import-${index + 1}`,
      bytes: await readFile(path),
      mediaKind: "video",
      fileName: `source-${index + 1}.mp4`,
      capturedAt: `2026-09-10T08:00:0${index}.000Z`,
      sessionId: studioScope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    assets.push(asset);
    await studio.analyze(studioScope, asset.id, {
      synthetic: true,
      contentType: "single-product",
      segment: { startMs: 0, endMs: assetDurationMs },
      ffprobe: {
        durationSeconds: segmentDurationSeconds,
        hasVideo: true,
        hasAudio: withAudio,
        width: 180,
        height: 320,
      },
      observations: [`synthetic-${color}-shot`],
      merchandiseFacts: ["fact-material-18k: 材质为 18K 金"],
    });
  }
  if (options.withMusicTrack) {
    const path = join(root, "test-music.m4a");
    await run(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
      "sine=frequency=220:sample_rate=44100:duration=8", "-c:a", "aac", "-y", path,
    ]);
    const bytes = await readFile(path);
    const asset = await library.importOriginal(studioScope.actorId, studioScope.customerId, {
      requestId: "fixture-music-1", bytes, mediaKind: "audio", fileName: "test-music.m4a",
      capturedAt: null, sessionId: studioScope.sessionId, viewPermission: "allowed", usePermission: "allowed",
    });
    musicTrack = {
      assetId: asset.id, digest: asset.digest, fileName: asset.fileName, bytes,
      durationSeconds: 8,
      mixPreference: { trackStartMs: 1_000, originalGainDb: -2, musicGainDb: -18 },
      rights: {
        sourceKind: "local-generated-test", licenseNote: "测试生成音轨，仅用于本地自动混音验收",
        previewUseAllowed: true, publishUseAllowed: false,
        confirmedBy: studioScope.actorId, confirmedAt: "2026-09-10T08:00:00.000Z",
      },
    };
  }
  const demand = await studio.createDemand(studioScope, {
    contentType: "single-product",
    goal: "用合成镜头讲解戒指",
    targetAudience: "本地合成测试观众",
    desiredEffect: "理解示例流程",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: "fixture-demand-1",
  });
  const assessment = await studio.assessDemand(studioScope, demand.id, "fixture-match-1");
  const discovery = await studio.discoverOpportunities(studioScope, {
    demandId: demand.id,
    matchAssessmentId: assessment.id,
  });
  const opportunity = discovery.opportunities[0];
  if (!opportunity) throw new Error("fixture-opportunity-missing");
  const brief = await studio.confirmBrief(studioScope, {
    opportunityId: opportunity.id,
    matchAssessmentId: assessment.id,
    selectedAssetIds: assets.map((asset) => asset.id),
    confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
    reviewRequirements: ["采用前人工复核"],
    operationKey: "brief-op-1",
  });
  return { root, library, studio, assets, demand, assessment, brief, preferenceRanker };
}

async function makeSyntheticClip(
  path: string,
  color: string,
  withAudio: boolean,
  durationSeconds: number,
): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const cacheKey = `${color}\0${withAudio}\0${durationSeconds}`;
  const cached = syntheticClipCache.get(cacheKey);
  if (cached) {
    await writeFile(path, cached);
    return;
  }
  const args = [
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    `color=c=${color}:s=180x320:r=5:d=${durationSeconds}`,
  ];
  if (withAudio) {
    args.push(
      "-f",
      "lavfi",
      "-i",
      `sine=frequency=440:sample_rate=44100:duration=${durationSeconds}`,
      "-shortest",
    );
  }
  args.push(
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-pix_fmt",
    "yuv420p",
  );
  if (withAudio) args.push("-c:a", "aac");
  args.push("-y", path);
  await run(ffmpegPath, args);
  syntheticClipCache.set(cacheKey, await readFile(path));
}

export async function run(command: string, args: string[]): Promise<{ stdout: string; stderr: string }> {
  return await new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`fixture-command-failed:${stderr}`));
    });
  });
}
