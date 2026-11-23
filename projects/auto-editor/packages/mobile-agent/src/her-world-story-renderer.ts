import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { join, relative, resolve } from "node:path";

import { EditBlueprintSchema, type HerWorldClipReceiptV1 } from "@wearable/contracts";
import ffmpegPathDefault from "ffmpeg-static";

import { createStillMotionArtifact, type StillMotionKind } from "./still-motion-adapter";
import { DeterministicStudioRenderer, inspectStudioOutputQuality, type StudioRenderSource } from "./studio-ffmpeg-renderer";
import type { HerWorldStagedClipJob } from "./her-world-clip-intake";

type StoryRendererOptions = Readonly<{
  workRoot: string;
  now?: () => string;
  ffmpegPath?: string;
  ffprobePath?: string;
  captionFontFile?: string;
}>;

export type HerWorldVisualDraft = Readonly<{
  jobId: string;
  storyId: string;
  storyRevision: number;
  readiness: "needs-voice";
  reason: "owner-voice-or-approved-voice-required";
  outputPath: string;
  outputDigest: string;
  durationSeconds: number;
  width: 720;
  height: 1280;
  sourceMap: readonly Readonly<{
    mediaId: string;
    stageId: string;
    timelineStartMs: number;
    timelineEndMs: number;
  }>[];
  qualityStatus: "passed" | "needs-review" | "failed";
  progressReceipt: HerWorldClipReceiptV1;
  publishClaimed: false;
}>;

export type HerWorldStoryCandidate = Readonly<{
  jobId: string;
  storyId: string;
  storyRevision: number;
  readiness: "review-ready" | "needs-review";
  reason: "approved-owner-voice-mixed" | "audio-quality-review-required";
  outputPath: string;
  outputDigest: string;
  durationSeconds: number;
  width: 720;
  height: 1280;
  sourceMap: HerWorldVisualDraft["sourceMap"];
  qualityStatus: "passed" | "needs-review" | "failed";
  progressReceipt: HerWorldClipReceiptV1;
  publishClaimed: false;
}>;

const motionFor = (index: number): StillMotionKind => (["push-in", "pull-out", "still"] as const)[index % 3]!;

export function buildHerWorldNarrativeCards(job: HerWorldStagedClipJob) {
  return (job.themeMaterials ?? [])
    .filter((material) => material.kind !== "missing_shot")
    .map((material) => ({
      kind: material.kind,
      heading: material.kind === "theme_intro" ? (job.theme || job.title) : job.place,
      content: material.content,
    }));
}

/**
 * Builds a real local MP4 from the owner's confirmed images. Text-only jobs
 * deliberately stop at a visual draft: no substitute voice is invented and
 * no customer-ready receipt is produced until an approved voice track exists.
 */
export class HerWorldStoryRenderer {
  private readonly workRoot: string;
  private readonly now: () => string;
  private readonly ffmpegPath?: string;
  private readonly ffprobePath?: string;
  private readonly captionFontFile?: string;

  constructor(options: StoryRendererOptions) {
    this.workRoot = resolve(options.workRoot);
    this.now = options.now ?? (() => new Date().toISOString());
    this.ffmpegPath = options.ffmpegPath;
    this.ffprobePath = options.ffprobePath;
    this.captionFontFile = options.captionFontFile;
  }

  async renderVisualDraft(job: HerWorldStagedClipJob): Promise<HerWorldVisualDraft | HerWorldStoryCandidate> {
    if (job.acts.length < 1 || job.acts.length > 15) throw new Error("her-world-story-act-count-unsupported");
    const mediaById = new Map(job.media.map((item) => [item.id, item]));
    for (const act of job.acts) {
      const media = mediaById.get(act.mediaId);
      if (!media) throw new Error(`her-world-render-media-missing:${act.mediaId}`);
      if (media.kind !== "image") throw new Error(`her-world-render-media-kind-not-yet-supported:${media.id}`);
    }

    const jobRoot = join(this.workRoot, job.jobId);
    const motionRoot = join(jobRoot, "motion");
    const outputRoot = join(jobRoot, "drafts");
    await mkdir(motionRoot, { recursive: true, mode: 0o700 });
    await mkdir(outputRoot, { recursive: true, mode: 0o700 });

    const durationMs = Math.min(60_000, Math.max(8_000, Math.round(job.output.durationSeconds * 1_000)));
    const slotCount = Math.ceil(durationMs / 4_000);
    const slotDurations = Array.from({ length: slotCount }, (_, index) =>
      index === slotCount - 1 ? durationMs - 4_000 * (slotCount - 1) : 4_000);
    const sources: StudioRenderSource[] = [];
    const shots: Array<{
      id: string; sourceAssetId: string; sourceDigest: string; sourceStartMs: number; sourceEndMs: number;
      timelineStartMs: number; reason: string;
    }> = [];
    const captions: Array<{
      id: string; sourceAssetId: string; sourceDigest: string; sourceStartMs: number; sourceEndMs: number;
      timelineStartMs: number; timelineEndMs: number; text: string; reviewStatus: "human-approved";
    }> = [];
    const sourceMap: Array<{ mediaId: string; stageId: string; timelineStartMs: number; timelineEndMs: number }> = [];
    const captionedActs = new Set<string>();
    const narrativeCards = buildHerWorldNarrativeCards(job);
    let timelineStartMs = 0;

    for (let index = 0; index < slotCount; index += 1) {
      const act = job.acts[index % job.acts.length]!;
      const media = mediaById.get(act.mediaId)!;
      const artifact = await createStillMotionArtifact({
        sourcePath: media.absolutePath,
        sourceDigest: media.sha256,
        motion: motionFor(index),
        outputRoot: motionRoot,
        operationKey: `slot-${String(index + 1).padStart(3, "0")}`,
        ffmpegPath: this.ffmpegPath,
        ffprobePath: this.ffprobePath,
      });
      const sourceAssetId = `story-shot-${String(index + 1).padStart(3, "0")}`;
      const slotDurationMs = slotDurations[index]!;
      sources.push({ assetId: sourceAssetId, blobDigest: artifact.outputDigest, bytes: await readFile(artifact.outputPath) });
      shots.push({
        id: `shot-${index + 1}`, sourceAssetId, sourceDigest: artifact.outputDigest,
        sourceStartMs: 0, sourceEndMs: slotDurationMs, timelineStartMs,
        reason: `owner-confirmed-story-stage:${act.stageId}`,
      });
      sourceMap.push({ mediaId: media.id, stageId: act.stageId, timelineStartMs, timelineEndMs: timelineStartMs + slotDurationMs });
      const narrativeCard = narrativeCards[index];
      if (narrativeCard) {
        captions.push({
          id: `theme-${narrativeCard.kind}-${index + 1}`, sourceAssetId, sourceDigest: artifact.outputDigest,
          sourceStartMs: 0, sourceEndMs: slotDurationMs, timelineStartMs,
          timelineEndMs: timelineStartMs + slotDurationMs,
          text: `${narrativeCard.heading}\n${narrativeCard.content}`, reviewStatus: "human-approved",
        });
      } else if (!captionedActs.has(act.stageId)) {
        captions.push({
          id: `caption-${act.sequence}`, sourceAssetId, sourceDigest: artifact.outputDigest,
          sourceStartMs: 0, sourceEndMs: slotDurationMs, timelineStartMs,
          timelineEndMs: timelineStartMs + slotDurationMs,
          text: `${act.heading}\n${act.narration}`, reviewStatus: "human-approved",
        });
        captionedActs.add(act.stageId);
      }
      timelineStartMs += slotDurationMs;
    }

    const blueprint = EditBlueprintSchema.parse({
      id: `her-world-${job.jobId}`,
      customerId: `story-${job.storyId}`,
      sessionId: `clip-${job.jobId}`,
      version: job.storyRevision,
      briefId: `story-${job.storyId}-r${job.storyRevision}`,
      platformVariant: "editorial-master",
      editorialDirection: "chronological-evidence-first",
      direction: "owner-confirmed-story-chronology",
      durationSeconds: durationMs / 1_000,
      shots,
      captionCues: captions,
      captionStyle: {
        template: "classic-bottom", fontSizePx: 44, primaryColor: "#FFFFFF",
        outlineColor: "#181818", outlineWidthPx: 3, bottomMarginPx: 160, maxCharsPerLine: 14,
      },
      transitionPlan: { style: "cut", durationMs: 0 },
      capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
      publishClaimed: false,
      createdAt: this.now(),
    });
    const outputPath = join(outputRoot, `visual-r${job.storyRevision}.mp4`);
    const rendered = await new DeterministicStudioRenderer(sources, {
      outputRoot,
      ffmpegPath: this.ffmpegPath,
      ffprobePath: this.ffprobePath,
      captionFontFile: this.captionFontFile,
    }).render(blueprint, outputPath);
    const updatedAt = this.now();
    if (job.narration.mode === "provided-audio") {
      const audio = mediaById.get(job.narration.audioMediaId ?? "");
      if (!audio || audio.kind !== "audio") throw new Error("her-world-approved-voice-missing");
      const candidateRoot = join(jobRoot, "outputs");
      await mkdir(candidateRoot, { recursive: true, mode: 0o700 });
      const candidatePath = join(candidateRoot, "editorial-master.mp4");
      const ffmpeg = this.ffmpegPath ?? ffmpegPathDefault;
      if (!ffmpeg) throw new Error("ffmpeg-binary-unavailable");
      await runChecked(ffmpeg, [
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", rendered.outputPath, "-i", audio.absolutePath,
        "-filter_complex", `[1:a]apad,atrim=0:${rendered.durationSeconds.toFixed(3)},asetpts=PTS-STARTPTS[a]`,
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", "-y", candidatePath,
      ], "her-world-approved-voice-mix-failed");
      const candidateBytes = await readFile(candidatePath);
      const qualityReport = await inspectStudioOutputQuality(candidatePath, {
        ffmpegPath: ffmpeg,
        captionCues: blueprint.captionCues,
        captionStyle: blueprint.captionStyle,
        durationSeconds: rendered.durationSeconds,
      });
      const isReady = qualityReport.status !== "failed";
      const output = {
        profile: "editorial-master" as const,
        relativePath: relative(this.workRoot, candidatePath).split("\\").join("/"),
        sha256: createHash("sha256").update(candidateBytes).digest("hex"),
        durationSeconds: rendered.durationSeconds,
        width: 720 as const,
        height: 1280 as const,
        sourceMap: sourceMap.map(({ mediaId, timelineStartMs, timelineEndMs }) => ({ mediaId, timelineStartMs, timelineEndMs })),
      };
      return {
        jobId: job.jobId, storyId: job.storyId, storyRevision: job.storyRevision,
        readiness: isReady ? "review-ready" : "needs-review",
        reason: isReady ? "approved-owner-voice-mixed" : "audio-quality-review-required",
        outputPath: candidatePath, outputDigest: output.sha256, durationSeconds: rendered.durationSeconds,
        width: 720, height: 1280, sourceMap, qualityStatus: qualityReport.status,
        progressReceipt: {
          schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: job.jobId,
          storyId: job.storyId, storyRevision: job.storyRevision,
          status: isReady ? "review-ready" : "rendering", progress: isReady ? 95 : 90,
          outputs: isReady ? [output] : [], ownerAccepted: false, published: false, error: null, updatedAt,
        },
        publishClaimed: false,
      };
    }
    return {
      jobId: job.jobId,
      storyId: job.storyId,
      storyRevision: job.storyRevision,
      readiness: "needs-voice",
      reason: "owner-voice-or-approved-voice-required",
      outputPath: rendered.outputPath,
      outputDigest: rendered.outputDigest,
      durationSeconds: rendered.durationSeconds,
      width: rendered.width,
      height: rendered.height,
      sourceMap,
      qualityStatus: rendered.qualityReport.status,
      progressReceipt: {
        schemaVersion: 1,
        kind: "her-world-story-render-receipt",
        jobId: job.jobId,
        storyId: job.storyId,
        storyRevision: job.storyRevision,
        status: "rendering",
        progress: 80,
        outputs: [],
        ownerAccepted: false,
        published: false,
        error: null,
        updatedAt,
      },
      publishClaimed: false,
    };
  }
}

function runChecked(command: string, args: string[], errorCode: string): Promise<void> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", (error) => reject(new Error(`${errorCode}:${error.message}`)));
    child.once("close", (code) => code === 0 ? resolvePromise() : reject(new Error(`${errorCode}:${stderr.trim() || `exit-${code}`}`)));
  });
}
