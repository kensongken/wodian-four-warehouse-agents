import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants, createReadStream } from "node:fs";
import {
  access,
  chmod,
  lstat,
  mkdir,
  realpath,
  rename,
  rm,
  stat,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";

import bundledFfmpegPath from "ffmpeg-static";

const bundledFfprobePath = (
  createRequire(import.meta.url)("ffprobe-static") as { path?: unknown }
).path;

const DURATION_MS = 4_000 as const;
const WIDTH = 720 as const;
const HEIGHT = 1280 as const;
const FPS = 25 as const;
const FRAME_COUNT = 100;
const SAFE_OPERATION_KEY = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const PROCESS_TIMEOUT_MS = 120_000;
const MAX_PROCESS_OUTPUT_BYTES = 1_048_576;

export type StillMotionKind = "still" | "push-in" | "pull-out";

export type StillMotionArtifact = Readonly<{
  id: string;
  sourceDigest: string;
  motion: StillMotionKind;
  durationMs: 4000;
  width: 720;
  height: 1280;
  fps: 25;
  outputPath: string;
  outputDigest: string;
  ffmpegVersion: string;
}>;

export async function createStillMotionArtifact(input: Readonly<{
  sourcePath: string;
  sourceDigest: string;
  motion: StillMotionKind;
  outputRoot: string;
  operationKey: string;
  ffmpegPath?: string;
  ffprobePath?: string;
}>): Promise<StillMotionArtifact> {
  if (!SAFE_OPERATION_KEY.test(input.operationKey)) {
    throw new Error("still-motion-operation-key-invalid");
  }
  if (!SHA256.test(input.sourceDigest)) throw new Error("still-motion-source-digest-invalid");

  const sourcePath = await realpath(input.sourcePath);
  const sourceBefore = await stat(sourcePath);
  if (!sourceBefore.isFile()) throw new Error("still-motion-source-regular-file-required");
  const sourceDigestBefore = await sha256File(sourcePath);
  if (sourceDigestBefore !== input.sourceDigest) {
    throw new Error("still-motion-source-digest-mismatch");
  }

  const ffmpeg = input.ffmpegPath ?? bundledFfmpegPath;
  const ffprobe = input.ffprobePath ?? (
    typeof bundledFfprobePath === "string" ? bundledFfprobePath : undefined
  );
  await assertExecutable(ffmpeg, "still-motion-ffmpeg-unavailable");
  await assertExecutable(ffprobe, "still-motion-ffprobe-unavailable");
  const ffmpegVersion = firstLine(await runProcess(ffmpeg!, ["-version"], "still-motion-ffmpeg-failed"));
  await assertImageSource(sourcePath, ffprobe!);

  await mkdir(input.outputRoot, { recursive: true, mode: 0o700 });
  const outputRoot = await realpath(input.outputRoot);
  const rootMetadata = await lstat(outputRoot);
  if (!rootMetadata.isDirectory() || rootMetadata.isSymbolicLink()) {
    throw new Error("still-motion-output-root-directory-required");
  }
  const operationDirectory = join(outputRoot, input.operationKey);
  try {
    await mkdir(operationDirectory, { mode: 0o700 });
    await chmod(operationDirectory, 0o700);
  } catch (error) {
    if (isNodeError(error, "EEXIST")) {
      throw new Error("still-motion-operation-key-already-used", { cause: error });
    }
    throw error;
  }

  const id = artifactId(input.sourceDigest, input.motion, input.operationKey);
  const stagePath = join(operationDirectory, `.stage-${randomUUID()}.mp4`);
  const outputPath = join(operationDirectory, `${id}.mp4`);
  let renderError: unknown = null;
  try {
    await runProcess(ffmpeg!, ffmpegArguments(sourcePath, stagePath, input.motion), "still-motion-ffmpeg-failed");
  } catch (error) {
    renderError = error;
  }

  try {
    const sourceAfter = await stat(sourcePath);
    const sourceDigestAfter = sourceAfter.isFile() ? await sha256File(sourcePath) : "";
    if (
      sourceAfter.size !== sourceBefore.size
      || sourceDigestAfter !== sourceDigestBefore
      || sourceDigestAfter !== input.sourceDigest
    ) {
      throw new Error("still-motion-source-changed-during-render");
    }
    if (renderError) throw renderError;

    const probe = await probeVideo(stagePath, ffprobe!);
    if (
      probe.codecName !== "h264"
      || probe.width !== WIDTH
      || probe.height !== HEIGHT
      || probe.frameRate !== FPS
      || probe.frameCount !== FRAME_COUNT
      || Math.abs(probe.durationMs - DURATION_MS) > 40
    ) {
      throw new Error("still-motion-output-invalid");
    }
    const outputDigest = await sha256File(stagePath);
    await rename(stagePath, outputPath);
    return Object.freeze({
      id,
      sourceDigest: input.sourceDigest,
      motion: input.motion,
      durationMs: DURATION_MS,
      width: WIDTH,
      height: HEIGHT,
      fps: FPS,
      outputPath,
      outputDigest,
      ffmpegVersion,
    });
  } catch (error) {
    await rm(operationDirectory, { recursive: true, force: true });
    throw error;
  }
}

function ffmpegArguments(
  sourcePath: string,
  stagePath: string,
  motion: StillMotionKind,
): string[] {
  const fit = "scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,"
    + "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1";
  const motionFilter = motion === "still"
    ? fit
    : `${fit},zoompan=z='${motion === "push-in" ? "1+0.12*on/99" : "1.12-0.12*on/99"}'`
      + ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=100:s=720x1280:fps=25,setsar=1";
  return [
    "-hide_banner", "-loglevel", "error", "-nostdin",
    "-framerate", String(FPS), "-loop", "1", "-i", sourcePath,
    "-vf", motionFilter,
    "-frames:v", String(FRAME_COUNT), "-r", String(FPS), "-an",
    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-map_metadata", "-1", "-map_chapters", "-1",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-threads", "1",
    "-movflags", "+faststart", "-n", stagePath,
  ];
}

function artifactId(sourceDigest: string, motion: StillMotionKind, operationKey: string): string {
  return createHash("sha256").update(JSON.stringify({
    schemaVersion: 1,
    sourceDigest,
    motion,
    durationMs: DURATION_MS,
    width: WIDTH,
    height: HEIGHT,
    fps: FPS,
    operationKey,
  })).digest("hex");
}

async function assertImageSource(sourcePath: string, ffprobe: string): Promise<void> {
  let stdout: string;
  try {
    stdout = await runProcess(ffprobe, [
      "-v", "error", "-f", "image2", "-i", sourcePath,
      "-select_streams", "v:0",
      "-show_entries", "format=format_name:stream=codec_type,width,height",
      "-of", "json",
    ], "still-motion-source-not-image");
  } catch (error) {
    if (error instanceof Error && error.message === "still-motion-ffprobe-unavailable") throw error;
    throw new Error("still-motion-source-not-image", { cause: error });
  }
  try {
    const parsed = JSON.parse(stdout) as {
      streams?: Array<{ codec_type?: string; width?: number; height?: number }>;
      format?: { format_name?: string };
    };
    const stream = parsed.streams?.[0];
    const format = parsed.format?.format_name ?? "";
    if (
      stream?.codec_type !== "video"
      || !Number.isInteger(stream.width) || stream.width! <= 0
      || !Number.isInteger(stream.height) || stream.height! <= 0
      || !/(?:png|jpeg|jpg|mjpeg|image2)/i.test(format)
    ) {
      throw new Error("still-motion-source-not-image");
    }
  } catch (error) {
    if (error instanceof Error && error.message === "still-motion-source-not-image") throw error;
    throw new Error("still-motion-source-not-image", { cause: error });
  }
}

async function probeVideo(path: string, ffprobe: string): Promise<Readonly<{
  codecName: string;
  width: number;
  height: number;
  frameRate: number;
  frameCount: number;
  durationMs: number;
}>> {
  const stdout = await runProcess(ffprobe, [
    "-v", "error", "-select_streams", "v:0",
    "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration",
    "-of", "json", path,
  ], "still-motion-ffprobe-failed");
  try {
    const parsed = JSON.parse(stdout) as {
      streams?: Array<{
        codec_name?: string;
        width?: number;
        height?: number;
        avg_frame_rate?: string;
        nb_frames?: string;
      }>;
      format?: { duration?: string };
    };
    const stream = parsed.streams?.[0];
    if (!stream) throw new Error("video-stream-missing");
    const [numerator, denominator] = (stream.avg_frame_rate ?? "0/1").split("/").map(Number);
    return {
      codecName: stream.codec_name ?? "",
      width: stream.width ?? 0,
      height: stream.height ?? 0,
      frameRate: numerator! / denominator!,
      frameCount: Number(stream.nb_frames),
      durationMs: Math.round(Number(parsed.format?.duration) * 1_000),
    };
  } catch (error) {
    throw new Error("still-motion-ffprobe-output-invalid", { cause: error });
  }
}

async function assertExecutable(path: string | null | undefined, code: string): Promise<void> {
  if (!path) throw new Error(code);
  try {
    await access(path, constants.X_OK);
    if (!(await stat(path)).isFile()) throw new Error(code);
  } catch (error) {
    if (error instanceof Error && error.message === code) throw error;
    throw new Error(code, { cause: error });
  }
}

async function sha256File(path: string): Promise<string> {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

async function runProcess(command: string, args: readonly string[], failureCode: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    let settled = false;
    const finish = (error?: Error, output = "") => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (error) reject(error);
      else resolve(output);
    };
    const collect = (target: Buffer[]) => (chunk: Buffer) => {
      outputBytes += chunk.byteLength;
      if (outputBytes > MAX_PROCESS_OUTPUT_BYTES) {
        child.kill("SIGKILL");
        finish(new Error(`${failureCode}:output-limit`));
        return;
      }
      target.push(chunk);
    };
    child.stdout.on("data", collect(stdout));
    child.stderr.on("data", collect(stderr));
    child.once("error", (error) => finish(new Error(failureCode, { cause: error })));
    child.once("close", (code) => {
      if (code === 0) finish(undefined, Buffer.concat(stdout).toString("utf8"));
      else finish(new Error(`${failureCode}:${Buffer.concat(stderr).toString("utf8").trim()}`));
    });
    const timeout = setTimeout(() => {
      child.kill("SIGKILL");
      finish(new Error(`${failureCode}:timeout`));
    }, PROCESS_TIMEOUT_MS);
  });
}

function firstLine(value: string): string {
  const line = value.split(/\r?\n/, 1)[0]?.trim();
  if (!line) throw new Error("still-motion-ffmpeg-version-invalid");
  return line;
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error && error.code === code;
}
