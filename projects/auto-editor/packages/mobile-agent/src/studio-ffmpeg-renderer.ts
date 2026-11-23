import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { mkdtemp, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  EditBlueprintSchema,
  StudioDraftQualityReportSchema,
  type EditBlueprint,
  type StudioCaptionCue,
  type StudioCaptionStyle,
  type StudioDraftQualityCheck,
  type StudioDraftQualityReport,
  type StudioTransitionPlan,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";

const bundledFfprobePath = (
  createRequire(import.meta.url)("ffprobe-static") as { path?: unknown }
).path;

export type StudioRenderSource = Readonly<{
  assetId: string;
  blobDigest: string;
  bytes: Uint8Array;
}>;

export type StudioRenderResult = Readonly<{
  outputPath: string;
  outputDigest: string;
  durationSeconds: number;
  videoCodec: "h264";
  audioCodec: "aac";
  width: 720;
  height: 1280;
  qualityReport: StudioDraftQualityReport;
}>;

type RendererOptions = Readonly<{
  outputRoot: string;
  ffmpegPath?: string | null;
  ffprobePath?: string | null;
  captionFontFile?: string | null;
}>;

export type StudioShareOutputOptions = Readonly<{
  outputRoot: string;
  ffmpegPath?: string | null;
  ffprobePath?: string | null;
  captionCues?: readonly StudioCaptionCue[];
  captionStyle?: StudioCaptionStyle;
}>;

type CaptionFont = Readonly<{ file: string; family: string }>;

const DEFAULT_CAPTION_FONTS: readonly CaptionFont[] = Object.freeze([
  Object.freeze({ file: "/System/Library/Fonts/STHeiti Medium.ttc", family: "STHeiti" }),
  Object.freeze({ file: "/System/Library/Fonts/Hiragino Sans GB.ttc", family: "Hiragino Sans GB" }),
  Object.freeze({ file: "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", family: "Noto Sans CJK SC" }),
  Object.freeze({ file: "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", family: "WenQuanYi Zen Hei" }),
]);

/** Local-only renderer for bytes already authorized by CustomerMaterialLibrary. */
export class DeterministicStudioRenderer {
  private readonly sources: ReadonlyMap<string, StudioRenderSource>;
  private readonly outputRoot: string;
  private readonly ffmpeg: string | null;
  private readonly ffprobe: string | null;
  private readonly captionFontFile: string | null | undefined;

  constructor(sources: readonly StudioRenderSource[], options: RendererOptions) {
    this.sources = new Map(sources.map((source) => [source.assetId, source]));
    if (this.sources.size !== sources.length) throw new Error("duplicate-render-source-asset-id");
    this.outputRoot = resolve(options.outputRoot);
    this.ffmpeg = options.ffmpegPath === undefined ? ffmpegPath : options.ffmpegPath;
    this.ffprobe = options.ffprobePath === undefined
      ? (typeof bundledFfprobePath === "string" ? bundledFfprobePath : null)
      : options.ffprobePath;
    this.captionFontFile = options.captionFontFile;
  }

  async render(blueprintValue: EditBlueprint, outputPathValue: string): Promise<StudioRenderResult> {
    const blueprint = EditBlueprintSchema.parse(blueprintValue);
    const outputPath = await resolveContainedOutputPath(this.outputRoot, outputPathValue);
    await assertExecutable(this.ffmpeg, "ffmpeg-binary-unavailable");
    await assertExecutable(this.ffprobe, "ffprobe-binary-unavailable");
    if (await pathExists(outputPath)) throw new Error("immutable-render-output-exists");

    const calculatedDuration = blueprint.shots.reduce(
      (total, shot) => total + shot.sourceEndMs - shot.sourceStartMs,
      0,
    ) / 1_000;
    if (Math.abs(calculatedDuration - blueprint.durationSeconds) > 0.001) {
      throw new Error("blueprint-duration-mismatch");
    }

    const temporaryDirectory = await mkdtemp(join(
      dirname(outputPath),
      `.${basename(outputPath, ".mp4")}.tmp-`,
    ));
    try {
      const clipPaths: string[] = [];
      for (const [index, shot] of blueprint.shots.entries()) {
        const source = this.sources.get(shot.sourceAssetId);
        if (!source) throw new Error(`studio-render-source-missing:${shot.sourceAssetId}`);
        if (!(source.bytes instanceof Uint8Array) || source.bytes.byteLength === 0) {
          throw new Error(`studio-render-source-empty:${shot.sourceAssetId}`);
        }
        if (source.blobDigest !== shot.sourceDigest || sha256(source.bytes) !== shot.sourceDigest) {
          throw new Error(`studio-render-source-digest-mismatch:${shot.sourceAssetId}`);
        }
        const sourcePath = join(temporaryDirectory, `source-${String(index).padStart(3, "0")}.bin`);
        const clipPath = join(temporaryDirectory, `clip-${String(index).padStart(3, "0")}.mp4`);
        await writeFile(sourcePath, source.bytes, { flag: "wx" });
        const sourceProbe = await probe(this.ffprobe!, sourcePath);
        if (!sourceProbe.video) throw new Error(`studio-render-source-video-missing:${shot.sourceAssetId}`);
        if (shot.sourceEndMs > sourceProbe.durationSeconds * 1_000 + 50) {
          throw new Error(`studio-render-source-range-invalid:${shot.sourceAssetId}`);
        }
        await normalizeClip(
          this.ffmpeg!,
          sourcePath,
          clipPath,
          shot.sourceStartMs,
          shot.sourceEndMs - shot.sourceStartMs,
          sourceProbe.audio !== null,
          blueprint.transitionPlan,
          index > 0,
          index < blueprint.shots.length - 1,
        );
        clipPaths.push(clipPath);
      }

      const concatPath = join(temporaryDirectory, "concat.txt");
      await writeFile(
        concatPath,
        `${clipPaths.map((path) => `file '${escapeConcatPath(path)}'`).join("\n")}\n`,
        { encoding: "utf8", flag: "wx" },
      );
      const pendingPath = join(temporaryDirectory, "pending.mp4");
      await runChecked(this.ffmpeg!, [
        "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concatPath,
        "-c", "copy", "-movflags", "+faststart", "-y", pendingPath,
      ], "ffmpeg-studio-concat-failed");
      await assertNonEmptyFile(pendingPath);
      let finalPendingPath = pendingPath;
      if ((blueprint.captionCues?.length ?? 0) > 0) {
        const font = await resolveCaptionFont(this.captionFontFile);
        const subtitlePath = join(temporaryDirectory, "captions.ass");
        const captionedPath = join(temporaryDirectory, "captioned.mp4");
        await writeFile(
          subtitlePath,
          captionCuesToAss(blueprint.captionCues!, blueprint.captionStyle!, font.family),
          { encoding: "utf8", flag: "wx" },
        );
        await burnCaptions(this.ffmpeg!, pendingPath, captionedPath, subtitlePath, dirname(font.file));
        await assertNonEmptyFile(captionedPath);
        finalPendingPath = captionedPath;
      }
      if (blueprint.musicMixPlan) {
        const music = this.sources.get(blueprint.musicMixPlan.trackAssetId);
        if (!music) throw new Error(`studio-music-source-missing:${blueprint.musicMixPlan.trackAssetId}`);
        if (music.blobDigest !== blueprint.musicMixPlan.trackDigest ||
            sha256(music.bytes) !== blueprint.musicMixPlan.trackDigest) {
          throw new Error(`studio-music-source-digest-mismatch:${blueprint.musicMixPlan.trackAssetId}`);
        }
        const musicPath = join(temporaryDirectory, "music-source.bin");
        const mixedPath = join(temporaryDirectory, "mixed.mp4");
        await writeFile(musicPath, music.bytes, { flag: "wx" });
        const musicProbe = await probe(this.ffprobe!, musicPath);
        if (!musicProbe.audio) throw new Error(`studio-music-source-audio-missing:${blueprint.musicMixPlan.trackAssetId}`);
        await mixBackgroundMusic(
          this.ffmpeg!, finalPendingPath, musicPath, mixedPath,
          blueprint.durationSeconds, blueprint.musicMixPlan,
        );
        await assertNonEmptyFile(mixedPath);
        finalPendingPath = mixedPath;
      }
      const metadata = await verifyOutput(this.ffprobe!, finalPendingPath);
      const qualityReport = await inspectStudioOutputQuality(finalPendingPath, {
        ffmpegPath: this.ffmpeg!,
        captionCues: blueprint.captionCues ?? [],
        captionStyle: blueprint.captionStyle,
        durationSeconds: metadata.durationSeconds,
      });
      const outputDigest = sha256(await readFile(finalPendingPath));
      await rename(finalPendingPath, outputPath);
      return {
        outputPath,
        outputDigest,
        durationSeconds: metadata.durationSeconds,
        videoCodec: "h264",
        audioCodec: "aac",
        width: 720,
        height: 1280,
        qualityReport,
      };
    } finally {
      await rm(temporaryDirectory, { recursive: true, force: true });
    }
  }
}

/**
 * Creates a separate vertical delivery candidate from an already-rendered
 * local draft. It never edits the parent file and uses fit+pad instead of
 * cropping, so reviewed pixels are not silently discarded.
 */
export async function createStudioShareOutput(
  inputPathValue: string,
  outputPathValue: string,
  options: StudioShareOutputOptions,
): Promise<StudioRenderResult> {
  const outputRoot = resolve(options.outputRoot);
  const ffmpeg = options.ffmpegPath === undefined ? ffmpegPath : options.ffmpegPath;
  const ffprobe = options.ffprobePath === undefined
    ? (typeof bundledFfprobePath === "string" ? bundledFfprobePath : null)
    : options.ffprobePath;
  await assertExecutable(ffmpeg, "ffmpeg-binary-unavailable");
  await assertExecutable(ffprobe, "ffprobe-binary-unavailable");
  const outputPath = await resolveContainedOutputPath(outputRoot, outputPathValue);
  if (await pathExists(outputPath)) throw new Error("immutable-render-output-exists");
  const [realRoot, inputPath] = await Promise.all([realpath(outputRoot), realpath(inputPathValue)]);
  assertContainedPath(realRoot, inputPath);
  if (inputPath === outputPath) throw new Error("studio-share-output-overwrites-parent");

  const temporaryDirectory = await mkdtemp(join(
    dirname(outputPath), `.${basename(outputPath, ".mp4")}.tmp-`,
  ));
  try {
    const pendingPath = join(temporaryDirectory, "pending-share.mp4");
    await runChecked(ffmpeg!, [
      "-hide_banner", "-loglevel", "error",
      "-i", inputPath,
      "-map", "0:v:0", "-map", "0:a:0",
      "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
      "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
      "-c:a", "aac", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
      "-y", pendingPath,
    ], "ffmpeg-studio-share-normalize-failed");
    await assertNonEmptyFile(pendingPath);
    const metadata = await verifyOutput(ffprobe!, pendingPath);
    const qualityReport = await inspectStudioOutputQuality(pendingPath, {
      ffmpegPath: ffmpeg!,
      captionCues: options.captionCues ?? [],
      captionStyle: options.captionStyle,
      durationSeconds: metadata.durationSeconds,
    });
    if (qualityReport.status === "failed") throw new Error("share-optimization-quality-check-failed");
    const outputDigest = sha256(await readFile(pendingPath));
    await rename(pendingPath, outputPath);
    return {
      outputPath,
      outputDigest,
      durationSeconds: metadata.durationSeconds,
      videoCodec: "h264",
      audioCodec: "aac",
      width: 720,
      height: 1280,
      qualityReport,
    };
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}

export async function inspectStudioOutputQuality(
  path: string,
  input: Readonly<{
    ffmpegPath?: string | null;
    captionCues?: readonly StudioCaptionCue[];
    captionStyle?: StudioCaptionStyle;
    durationSeconds: number;
  }>,
): Promise<StudioDraftQualityReport> {
  const ffmpeg = input.ffmpegPath === undefined ? ffmpegPath : input.ffmpegPath;
  await assertExecutable(ffmpeg, "ffmpeg-binary-unavailable");
  const result = await runChecked(ffmpeg!, [
    "-hide_banner", "-nostats", "-i", path,
    "-vf", "blackdetect=d=0.5:pic_th=0.98,freezedetect=n=-50dB:d=2",
    "-af", "silencedetect=noise=-45dB:d=2,volumedetect",
    "-f", "null", "-",
  ], "ffmpeg-studio-quality-inspection-failed");
  const log = result.stderr;
  const blackSeconds = maximumMetric(log, /black_duration:([0-9.]+)/g);
  const frozenSeconds = maximumMetric(log, /freeze_duration: ([0-9.]+)/g);
  const silenceSeconds = maximumMetric(log, /silence_duration: ([0-9.]+)/g);
  const meanVolumeDb = lastMetric(log, /mean_volume: (-?(?:inf|[0-9.]+)) dB/g);
  const blackRatio = input.durationSeconds > 0 ? blackSeconds / input.durationSeconds : 1;
  const containerDurationValid = input.durationSeconds >= 6 && input.durationSeconds <= 60;
  const captionSafe = captionTimelineIsSafe(
    input.captionCues ?? [],
    input.captionStyle,
    input.durationSeconds,
  );
  const checks: StudioDraftQualityCheck[] = [
    qualityCheck(
      "container-format",
      containerDurationValid ? "passed" : "failed",
      input.durationSeconds,
      "6-60 秒、H.264/AAC、720x1280",
      containerDurationValid ? "视频格式和时长合格" : `成片时长 ${input.durationSeconds.toFixed(2)} 秒，不在 6-60 秒范围`,
    ),
    qualityCheck(
      "black-frames",
      blackSeconds >= 2 || blackRatio >= 0.08 ? "failed" : blackSeconds >= 0.5 ? "needs-review" : "passed",
      blackSeconds,
      "连续黑屏少于 2 秒，且不超过总时长 8%",
      blackSeconds === 0 ? "未发现连续黑屏" : `最长连续黑屏 ${blackSeconds.toFixed(2)} 秒`,
    ),
    qualityCheck(
      "frozen-video",
      frozenSeconds >= 15 ? "failed" : frozenSeconds >= 5 ? "needs-review" : "passed",
      frozenSeconds,
      "冻结少于 5 秒；15 秒以上判定失败",
      frozenSeconds === 0 ? "未发现明显冻结" : `最长疑似冻结 ${frozenSeconds.toFixed(2)} 秒`,
    ),
    qualityCheck(
      "long-silence",
      silenceSeconds >= 12 ? "failed" : silenceSeconds >= 4 ? "needs-review" : "passed",
      silenceSeconds,
      "连续静音少于 4 秒；12 秒以上判定失败",
      silenceSeconds === 0 ? "未发现长静音" : `最长连续静音 ${silenceSeconds.toFixed(2)} 秒`,
    ),
    qualityCheck(
      "audio-level",
      meanVolumeDb === null || meanVolumeDb <= -45 ? "failed" : meanVolumeDb < -32 ? "needs-review" : "passed",
      meanVolumeDb,
      "平均响度高于 -32 dB；低于 -45 dB 判定失败",
      meanVolumeDb === null ? "无法读取平均响度" : `平均响度 ${meanVolumeDb.toFixed(2)} dB`,
    ),
    qualityCheck(
      "caption-safe-area",
      captionSafe ? "passed" : "failed",
      input.captionStyle?.bottomMarginPx ?? null,
      "字幕不重叠、不越过成片时长、与原声片段等长，底部安全距离至少 100 px",
      captionSafe ? "字幕时间、原声绑定和安全区合格" : "字幕重叠、原声绑定或安全区不合格",
    ),
  ];
  const status = checks.some((check) => check.status === "failed")
    ? "failed"
    : checks.some((check) => check.status === "needs-review")
      ? "needs-review"
      : "passed";
  return StudioDraftQualityReportSchema.parse({ version: 1, status, checks });
}

function captionTimelineIsSafe(
  cues: readonly StudioCaptionCue[],
  style: StudioCaptionStyle | undefined,
  durationSeconds: number,
): boolean {
  if (cues.length === 0) return true;
  if (!style || style.bottomMarginPx < 100 || style.fontSizePx > 64) return false;
  const seenCueIds = new Set<string>();
  let previousTimelineEndMs = 0;
  for (const cue of cues) {
    if (seenCueIds.has(cue.id)) return false;
    seenCueIds.add(cue.id);
    if (cue.timelineStartMs < previousTimelineEndMs) return false;
    if (cue.timelineEndMs > durationSeconds * 1_000 + 1) return false;
    if (cue.timelineEndMs - cue.timelineStartMs !== cue.sourceEndMs - cue.sourceStartMs) return false;
    previousTimelineEndMs = cue.timelineEndMs;
  }
  return true;
}

function qualityCheck(
  id: StudioDraftQualityCheck["id"],
  status: StudioDraftQualityCheck["status"],
  measuredValue: number | null,
  threshold: string,
  message: string,
): StudioDraftQualityCheck {
  return { id, status, measuredValue, threshold, message };
}

function maximumMetric(log: string, pattern: RegExp): number {
  const values = [...log.matchAll(pattern)].map((match) => Number(match[1])).filter(Number.isFinite);
  return values.length === 0 ? 0 : Math.max(...values);
}

function lastMetric(log: string, pattern: RegExp): number | null {
  const matches = [...log.matchAll(pattern)];
  const raw = matches.at(-1)?.[1];
  if (raw === undefined || raw === "-inf" || raw === "inf") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function captionCuesToAss(
  cues: readonly StudioCaptionCue[],
  style: StudioCaptionStyle,
  fontFamily = "STHeiti",
): string {
  const lines = [
    "[Script Info]",
    "ScriptType: v4.00+",
    "PlayResX: 720",
    "PlayResY: 1280",
    "WrapStyle: 0",
    "ScaledBorderAndShadow: yes",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    `Style: Default,${sanitizeAssField(fontFamily)},${style.fontSizePx},${assColor(style.primaryColor)},${assColor(style.primaryColor)},${assColor(style.outlineColor)},&H80000000,-1,0,0,0,100,100,0,0,1,${style.outlineWidthPx},1,2,48,48,${style.bottomMarginPx},1`,
    "",
    "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ...[...cues]
      .sort((left, right) => left.timelineStartMs - right.timelineStartMs || left.id.localeCompare(right.id))
      .map((cue) => `Dialogue: 0,${assTimestamp(cue.timelineStartMs)},${assTimestamp(cue.timelineEndMs)},Default,,0,0,0,,${wrapCaptionText(cue.text, style.maxCharsPerLine)}`),
    "",
  ];
  return lines.join("\n");
}

type ProbeMetadata = Readonly<{
  durationSeconds: number;
  video: Readonly<{ codec: string; width: number; height: number }> | null;
  audio: Readonly<{ codec: string }> | null;
}>;

async function verifyOutput(ffprobe: string, path: string): Promise<ProbeMetadata> {
  const metadata = await probe(ffprobe, path);
  if (!metadata.video) throw new Error("studio-render-output-video-missing");
  if (metadata.video.codec !== "h264" || metadata.video.width !== 720 || metadata.video.height !== 1280) {
    throw new Error("studio-render-output-video-format-invalid");
  }
  if (!metadata.audio || metadata.audio.codec !== "aac") {
    throw new Error("studio-render-output-audio-format-invalid");
  }
  if (metadata.durationSeconds < 6 || metadata.durationSeconds > 60) {
    throw new Error("studio-render-output-duration-invalid");
  }
  return metadata;
}

async function probe(ffprobe: string, path: string): Promise<ProbeMetadata> {
  const result = await runChecked(ffprobe, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
    "-of", "json",
    path,
  ], "ffprobe-studio-inspection-failed");
  let parsed: {
    streams?: Array<{ codec_type?: unknown; codec_name?: unknown; width?: unknown; height?: unknown }>;
    format?: { duration?: unknown };
  };
  try {
    parsed = JSON.parse(result.stdout) as typeof parsed;
  } catch {
    throw new Error("ffprobe-studio-output-invalid");
  }
  const durationSeconds = Number(parsed.format?.duration);
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error("ffprobe-studio-duration-invalid");
  }
  const videoStream = parsed.streams?.find((stream) => stream.codec_type === "video");
  const audioStream = parsed.streams?.find((stream) => stream.codec_type === "audio");
  return {
    durationSeconds,
    video: videoStream && typeof videoStream.codec_name === "string"
      ? {
          codec: videoStream.codec_name,
          width: Number(videoStream.width),
          height: Number(videoStream.height),
        }
      : null,
    audio: audioStream && typeof audioStream.codec_name === "string"
      ? { codec: audioStream.codec_name }
      : null,
  };
}

async function normalizeClip(
  ffmpeg: string,
  sourcePath: string,
  outputPath: string,
  startMs: number,
  durationMs: number,
  hasAudio: boolean,
  transitionPlan: StudioTransitionPlan | undefined,
  fadeIn: boolean,
  fadeOut: boolean,
): Promise<void> {
  const args = [
    "-hide_banner", "-loglevel", "error",
    "-ss", seconds(startMs), "-i", sourcePath,
  ];
  if (hasAudio) {
    args.push("-map", "0:v:0", "-map", "0:a:0");
  } else {
    args.push(
      "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
      "-map", "0:v:0", "-map", "1:a:0",
    );
  }
  const baseVideoFilter = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25";
  const transitionMs = transitionPlan?.style === "fade-through-black"
    ? Math.min(transitionPlan.durationMs, Math.max(0, Math.floor(durationMs / 3)))
    : 0;
  const transitionSeconds = transitionMs / 1_000;
  const videoFades = [
    fadeIn && transitionMs > 0 ? `fade=t=in:st=0:d=${transitionSeconds.toFixed(3)}` : null,
    fadeOut && transitionMs > 0
      ? `fade=t=out:st=${Math.max(0, durationMs / 1_000 - transitionSeconds).toFixed(3)}:d=${transitionSeconds.toFixed(3)}`
      : null,
  ].filter((value): value is string => value !== null);
  const audioFades = [
    fadeIn && transitionMs > 0 ? `afade=t=in:st=0:d=${transitionSeconds.toFixed(3)}` : null,
    fadeOut && transitionMs > 0
      ? `afade=t=out:st=${Math.max(0, durationMs / 1_000 - transitionSeconds).toFixed(3)}:d=${transitionSeconds.toFixed(3)}`
      : null,
  ].filter((value): value is string => value !== null);
  args.push(
    "-t", seconds(durationMs),
    "-vf", [baseVideoFilter, ...videoFades].join(","),
    ...(audioFades.length > 0 ? ["-af", audioFades.join(",")] : []),
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
    "-y", outputPath,
  );
  await runChecked(ffmpeg, args, "ffmpeg-studio-normalize-failed");
  await assertNonEmptyFile(outputPath);
}

async function burnCaptions(
  ffmpeg: string,
  inputPath: string,
  outputPath: string,
  subtitlePath: string,
  fontDirectory: string,
): Promise<void> {
  const filter = `ass='${escapeFilterValue(subtitlePath)}':fontsdir='${escapeFilterValue(fontDirectory)}'`;
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-i", inputPath,
    "-map", "0:v:0", "-map", "0:a:0",
    "-vf", filter,
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "25",
    "-c:a", "copy", "-movflags", "+faststart", "-y", outputPath,
  ], "ffmpeg-studio-caption-burn-failed");
}

async function mixBackgroundMusic(
  ffmpeg: string,
  videoPath: string,
  musicPath: string,
  outputPath: string,
  durationSeconds: number,
  plan: NonNullable<EditBlueprint["musicMixPlan"]>,
): Promise<void> {
  const filter = [
    `[0:a]volume=${plan.originalGainDb}dB,asplit=2[sidechain][original]`,
    `[1:a]volume=${plan.musicGainDb}dB[music]`,
    `[music][sidechain]sidechaincompress=threshold=${plan.ducking.threshold}:ratio=${plan.ducking.ratio}:attack=${plan.ducking.attackMs}:release=${plan.ducking.releaseMs}[ducked]`,
    "[original][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
  ].join(";");
  await runChecked(ffmpeg, [
    "-hide_banner", "-loglevel", "error",
    "-i", videoPath,
    ...(plan.loopToFit ? ["-stream_loop", "-1"] : []),
    ...(plan.trackStartMs > 0 ? ["-ss", seconds(plan.trackStartMs)] : []),
    "-i", musicPath,
    "-filter_complex", filter,
    "-map", "0:v:0", "-map", "[aout]",
    "-t", durationSeconds.toFixed(3),
    "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2",
    "-movflags", "+faststart", "-y", outputPath,
  ], "ffmpeg-studio-music-mix-failed");
}

async function resolveCaptionFont(configured: string | null | undefined): Promise<CaptionFont> {
  if (configured === null) throw new Error("studio-caption-font-unavailable");
  if (configured !== undefined) {
    const candidate = resolve(configured);
    if (candidate !== configured || !await isFile(candidate)) throw new Error("studio-caption-font-unavailable");
    return { file: candidate, family: captionFontFamily(candidate) };
  }
  for (const candidate of DEFAULT_CAPTION_FONTS) {
    if (await isFile(candidate.file)) return candidate;
  }
  throw new Error("studio-caption-font-unavailable");
}

async function isFile(path: string): Promise<boolean> {
  return (await stat(path).catch(() => null))?.isFile() === true;
}

function captionFontFamily(path: string): string {
  const name = basename(path).toLowerCase();
  if (name.includes("hiragino")) return "Hiragino Sans GB";
  if (name.includes("noto")) return "Noto Sans CJK SC";
  if (name.includes("wqy") || name.includes("wenquanyi")) return "WenQuanYi Zen Hei";
  return "STHeiti";
}

function assTimestamp(milliseconds: number): string {
  const totalCentiseconds = Math.max(0, Math.floor(milliseconds / 10));
  const hours = Math.floor(totalCentiseconds / 360_000);
  const minutes = Math.floor((totalCentiseconds % 360_000) / 6_000);
  const seconds = Math.floor((totalCentiseconds % 6_000) / 100);
  const centiseconds = totalCentiseconds % 100;
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(centiseconds).padStart(2, "0")}`;
}

function wrapCaptionText(value: string, maxCharsPerLine: number): string {
  const characters = Array.from(value.replace(/[\r\n]+/g, " ").replace(/\s+/g, " ").trim());
  const maximum = maxCharsPerLine * 2;
  const visible = characters.length > maximum
    ? [...characters.slice(0, maximum - 1), "…"]
    : characters;
  const first = visible.slice(0, maxCharsPerLine).join("");
  const second = visible.slice(maxCharsPerLine).join("");
  return `${sanitizeAssText(first)}${second ? `\\N${sanitizeAssText(second)}` : ""}`;
}

function sanitizeAssText(value: string): string {
  return value.replaceAll("\\", "＼").replaceAll("{", "﹛").replaceAll("}", "﹜");
}

function sanitizeAssField(value: string): string {
  return value.replaceAll(",", " ").replace(/[\r\n]+/g, " ").trim();
}

function assColor(value: string): string {
  const red = value.slice(1, 3);
  const green = value.slice(3, 5);
  const blue = value.slice(5, 7);
  return `&H00${blue}${green}${red}`;
}

function escapeFilterValue(value: string): string {
  return value.replaceAll("\\", "\\\\").replaceAll(":", "\\:").replaceAll("'", "\\'");
}

async function runChecked(
  command: string,
  args: string[],
  errorCode: string,
): Promise<{ stdout: string; stderr: string }> {
  const result = await new Promise<{ code: number | null; stdout: string; stderr: string }>((resolveResult, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => resolveResult({ code, stdout, stderr }));
  }).catch((error: unknown) => {
    throw new Error(`${errorCode}:${error instanceof Error ? error.message : "spawn-failed"}`);
  });
  if (result.code !== 0) throw new Error(`${errorCode}:${result.stderr.trim()}`);
  return result;
}

async function assertExecutable(path: string | null, error: string): Promise<void> {
  if (!path) throw new Error(error);
  const metadata = await stat(path).catch(() => null);
  if (!metadata?.isFile() || (metadata.mode & 0o111) === 0) throw new Error(error);
}

async function assertNonEmptyFile(path: string): Promise<void> {
  const metadata = await stat(path).catch(() => null);
  if (!metadata?.isFile() || metadata.size === 0) throw new Error("studio-render-output-empty");
}

async function pathExists(path: string): Promise<boolean> {
  return (await stat(path).catch(() => null)) !== null;
}

async function resolveContainedOutputPath(outputRoot: string, outputPathValue: string): Promise<string> {
  const outputPath = resolve(outputPathValue);
  if (outputPath !== outputPathValue || !outputPath.endsWith(".mp4")) {
    throw new Error("studio-render-output-path-invalid");
  }
  assertContainedPath(outputRoot, outputPath);
  const [realRoot, realParent] = await Promise.all([
    realpath(outputRoot),
    realpath(dirname(outputPath)),
  ]).catch(() => {
    throw new Error("studio-render-output-root-unavailable");
  });
  const realOutputPath = join(realParent, basename(outputPath));
  assertContainedPath(realRoot, realOutputPath);
  return realOutputPath;
}

function assertContainedPath(root: string, candidate: string): void {
  const childPath = relative(root, candidate);
  if (!childPath || childPath === ".." || childPath.startsWith(`..${sep}`) || isAbsolute(childPath)) {
    throw new Error("studio-render-output-outside-root");
  }
}

function seconds(milliseconds: number): string {
  return (milliseconds / 1_000).toFixed(3);
}

function escapeConcatPath(path: string): string {
  return path.replaceAll("'", "'\\''");
}

function sha256(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}
