import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  createStillMotionArtifact,
  type StillMotionKind,
} from "../src/still-motion-adapter";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const roots: string[] = [];

beforeAll(() => {
  if (!ffmpegPath) throw new Error("test-ffmpeg-unavailable");
});

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("still motion adapter", () => {
  it.each<StillMotionKind>(["still", "push-in", "pull-out"])(
    "creates a traceable 4-second %s shot without mutating the source",
    async (motion) => {
      const fixture = await createFixture();
      const sourcePath = motion === "pull-out" ? fixture.jpegPath : fixture.pngPath;
      const sourceDigest = await sha256File(sourcePath);

      const artifact = await createStillMotionArtifact({
        sourcePath,
        sourceDigest,
        motion,
        outputRoot: fixture.outputRoot,
        operationKey: `motion-${motion}`,
      });

      expect(artifact).toMatchObject({
        sourceDigest,
        motion,
        durationMs: 4_000,
        width: 720,
        height: 1280,
        fps: 25,
      });
      expect(artifact.id).toBe(createHash("sha256").update(JSON.stringify({
        schemaVersion: 1,
        sourceDigest,
        motion,
        durationMs: 4_000,
        width: 720,
        height: 1280,
        fps: 25,
        operationKey: `motion-${motion}`,
      })).digest("hex"));
      expect(artifact.outputDigest).toBe(await sha256File(artifact.outputPath));
      expect(artifact.ffmpegVersion).toMatch(/^ffmpeg version /);
      expect(await sha256File(sourcePath)).toBe(sourceDigest);
      expect((await stat(join(fixture.outputRoot, `motion-${motion}`))).mode & 0o777).toBe(0o700);
      expect(await probe(artifact.outputPath)).toMatchObject({
        codec_name: "h264",
        width: 720,
        height: 1280,
        frameRate: 25,
        frameCount: 100,
        durationMs: 4_000,
      });
    },
    120_000,
  );

  it("rejects a source whose locked digest no longer matches", async () => {
    const fixture = await createFixture();

    await expect(createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: "f".repeat(64),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "wrong-digest",
    })).rejects.toThrow("still-motion-source-digest-mismatch");
  });

  it("rejects non-image input", async () => {
    const fixture = await createFixture();
    const textPath = join(fixture.root, "not-an-image.txt");
    await writeFile(textPath, "not image bytes");

    await expect(createStillMotionArtifact({
      sourcePath: textPath,
      sourceDigest: await sha256File(textPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "not-image",
    })).rejects.toThrow("still-motion-source-not-image");
  });

  it("rejects an operation key that could escape the output root", async () => {
    const fixture = await createFixture();

    await expect(createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: await sha256File(fixture.pngPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "../escape",
    })).rejects.toThrow("still-motion-operation-key-invalid");
  });

  it("does not reuse an operation key for changed input", async () => {
    const fixture = await createFixture();
    await createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: await sha256File(fixture.pngPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "same-operation",
    });

    await expect(createStillMotionArtifact({
      sourcePath: fixture.jpegPath,
      sourceDigest: await sha256File(fixture.jpegPath),
      motion: "push-in",
      outputRoot: fixture.outputRoot,
      operationKey: "same-operation",
    })).rejects.toThrow("still-motion-operation-key-already-used");
  }, 120_000);

  it("detects source mutation during rendering", async () => {
    const fixture = await createFixture();
    const wrapperPath = join(fixture.root, "mutating-ffmpeg.mjs");
    await writeFile(wrapperPath, `#!/usr/bin/env node\n`
      + `import { spawnSync } from "node:child_process";\n`
      + `import { writeFileSync } from "node:fs";\n`
      + `if (process.argv[2] !== "-version") writeFileSync(${JSON.stringify(fixture.pngPath)}, "changed-during-render");\n`
      + `const result = spawnSync(${JSON.stringify(ffmpegPath)}, process.argv.slice(2), { stdio: "inherit" });\n`
      + `process.exit(result.status ?? 1);\n`);
    await chmod(wrapperPath, 0o700);

    await expect(createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: await sha256File(fixture.pngPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "mutated-source",
      ffmpegPath: wrapperPath,
    })).rejects.toThrow("still-motion-source-changed-during-render");
  }, 120_000);

  it("fails closed when FFmpeg is unavailable", async () => {
    const fixture = await createFixture();

    await expect(createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: await sha256File(fixture.pngPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "missing-ffmpeg",
      ffmpegPath: join(fixture.root, "missing-ffmpeg"),
    })).rejects.toThrow("still-motion-ffmpeg-unavailable");
  });

  it("fails closed when ffprobe is unavailable", async () => {
    const fixture = await createFixture();

    await expect(createStillMotionArtifact({
      sourcePath: fixture.pngPath,
      sourceDigest: await sha256File(fixture.pngPath),
      motion: "still",
      outputRoot: fixture.outputRoot,
      operationKey: "missing-ffprobe",
      ffprobePath: join(fixture.root, "missing-ffprobe"),
    })).rejects.toThrow("still-motion-ffprobe-unavailable");
  });
});

async function createFixture() {
  const root = await mkdtemp(join(tmpdir(), "still-motion-adapter-"));
  roots.push(root);
  const outputRoot = join(root, "outputs");
  const pngPath = join(root, "portrait-rgba.png");
  const jpegPath = join(root, "product.jpg");
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
    "color=c=0x8a3ffc@0.55:s=721x1279:d=0.04,format=rgba",
    "-frames:v", "1", "-y", pngPath,
  ]);
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
    "color=c=0xd9a441:s=913x611:d=0.04",
    "-frames:v", "1", "-y", jpegPath,
  ]);
  return { root, outputRoot, pngPath, jpegPath };
}

async function probe(path: string) {
  const output = await run(ffprobePath, [
    "-v", "error", "-select_streams", "v:0",
    "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration",
    "-of", "json", path,
  ]);
  const parsed = JSON.parse(output) as {
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
  if (!stream) throw new Error("test-video-stream-missing");
  const [numerator, denominator] = (stream.avg_frame_rate ?? "0/1").split("/").map(Number);
  return {
    codec_name: stream.codec_name,
    width: stream.width,
    height: stream.height,
    frameRate: numerator! / denominator!,
    frameCount: Number(stream.nb_frames),
    durationMs: Math.round(Number(parsed.format?.duration) * 1_000),
  };
}

async function sha256File(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

async function run(command: string, args: readonly string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => {
      if (code === 0) resolve(Buffer.concat(stdout).toString("utf8"));
      else reject(new Error(Buffer.concat(stderr).toString("utf8")));
    });
  });
}
