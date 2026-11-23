import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";

import ffmpegPath from "ffmpeg-static";
import { afterEach, describe, expect, it } from "vitest";
import type { HerWorldStagedClipJob } from "../src";
import { HerWorldStoryRenderer, buildHerWorldNarrativeCards } from "../src";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const roots: string[] = [];

describe("her-world story renderer", () => {
  afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

  it("uses the theme opening and place context in the edit while keeping missing-shot advice out of customer pixels", () => {
    const cards = buildHerWorldNarrativeCards({
      title: "源头的生活", place: "斯里兰卡", theme: "去源头看见好宝石背后的生活",
      themeMaterials: [
        { kind: "theme_intro", title: "主题开场", content: "先看见生活，再理解宝石。", basis: "店主确认的主题" },
        { kind: "place_context", title: "地点进入", content: "从街巷和人情进入斯里兰卡。", basis: "真实地点线索" },
        { kind: "missing_shot", title: "还值得补一张", content: "补一张真实蓝宝石细节。", basis: "只作为补拍提醒" },
      ],
    } as HerWorldStagedClipJob);
    expect(cards).toEqual([
      { kind: "theme_intro", heading: "去源头看见好宝石背后的生活", content: "先看见生活，再理解宝石。" },
      { kind: "place_context", heading: "斯里兰卡", content: "从街巷和人情进入斯里兰卡。" },
    ]);
  });

  it("creates a real vertical MP4 from a confirmed image story but stops before customer-ready without approved voice", async () => {
    if (!ffmpegPath) throw new Error("test-ffmpeg-unavailable");
    const root = await mkdtemp(join(tmpdir(), "her-world-story-render-"));
    roots.push(root);
    const sourcePath = join(root, "source.jpg");
    await run(ffmpegPath, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x8b6847:s=900x1200:d=0.04", "-frames:v", "1", "-y", sourcePath]);
    const sourceDigest = createHash("sha256").update(await readFile(sourcePath)).digest("hex");
    const job: HerWorldStagedClipJob = {
      jobId: "a".repeat(32), storyId: "b".repeat(32), storyRevision: 2,
      title: "源头的生活", place: "斯里兰卡",
      acts: [{ sequence: 1, stageId: "origin", mediaId: "image-1", storyFunction: "people", heading: "先看见人",
        narration: "先看到当地人的生活，再理解一颗宝石。", sourceBasis: "owner-confirmed", productReference: null }],
      output: { aspectRatio: "9:16", width: 720, height: 1280, durationSeconds: 15, profiles: ["editorial-master"], captions: true },
      narration: { mode: "text-only", audioMediaId: null },
      media: [{ id: "image-1", kind: "image", mimeType: "image/jpeg", sha256: sourceDigest,
        byteLength: (await readFile(sourcePath)).byteLength, relativePath: "job/source.jpg", absolutePath: sourcePath }],
      publishClaimed: false,
    };
    const result = await new HerWorldStoryRenderer({
      workRoot: join(root, "work"), ffmpegPath, ffprobePath,
      now: () => "2026-10-25T05:00:00.000Z",
    }).renderVisualDraft(job);

    expect(result).toMatchObject({
      readiness: "needs-voice", reason: "owner-voice-or-approved-voice-required",
      width: 720, height: 1280, publishClaimed: false,
      progressReceipt: { status: "rendering", progress: 80, outputs: [], published: false },
    });
    expect(result.durationSeconds).toBeCloseTo(15, 1);
    expect(result.sourceMap).toHaveLength(4);
    expect(result.outputDigest).toBe(createHash("sha256").update(await readFile(result.outputPath)).digest("hex"));
    expect(result.qualityStatus).toBe("failed");
  }, 180_000);

  it("mixes only the provided owner-approved voice and returns a review candidate", async () => {
    if (!ffmpegPath) throw new Error("test-ffmpeg-unavailable");
    const root = await mkdtemp(join(tmpdir(), "her-world-story-render-"));
    roots.push(root);
    const sourcePath = join(root, "source.jpg");
    const voicePath = join(root, "voice.m4a");
    await run(ffmpegPath, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x8b6847:s=900x1200:d=0.04", "-frames:v", "1", "-y", sourcePath]);
    await run(ffmpegPath, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=15", "-c:a", "aac", "-y", voicePath]);
    const sourceBytes = await readFile(sourcePath);
    const voiceBytes = await readFile(voicePath);
    const job: HerWorldStagedClipJob = {
      jobId: "d".repeat(32), storyId: "e".repeat(32), storyRevision: 3,
      title: "源头的生活", place: "斯里兰卡",
      acts: [{ sequence: 1, stageId: "origin", mediaId: "image-1", storyFunction: "people", heading: "先看见人",
        narration: "先看到当地人的生活，再理解一颗宝石。", sourceBasis: "owner-confirmed", productReference: null }],
      output: { aspectRatio: "9:16", width: 720, height: 1280, durationSeconds: 15, profiles: ["editorial-master"], captions: true },
      narration: { mode: "provided-audio", audioMediaId: "voice-owner" },
      media: [
        { id: "image-1", kind: "image", mimeType: "image/jpeg", sha256: createHash("sha256").update(sourceBytes).digest("hex"),
          byteLength: sourceBytes.byteLength, relativePath: "job/source.jpg", absolutePath: sourcePath },
        { id: "voice-owner", kind: "audio", mimeType: "audio/mp4", sha256: createHash("sha256").update(voiceBytes).digest("hex"),
          byteLength: voiceBytes.byteLength, relativePath: "job/voice.m4a", absolutePath: voicePath },
      ],
      publishClaimed: false,
    };
    const result = await new HerWorldStoryRenderer({ workRoot: join(root, "work"), ffmpegPath, ffprobePath,
      now: () => "2026-10-25T05:00:00.000Z" }).renderVisualDraft(job);
    expect(result).toMatchObject({ readiness: "review-ready", reason: "approved-owner-voice-mixed", qualityStatus: "needs-review",
      progressReceipt: { status: "review-ready", progress: 95, ownerAccepted: false, published: false } });
    expect(result.progressReceipt.outputs).toHaveLength(1);
    expect(result.outputDigest).toBe(createHash("sha256").update(await readFile(result.outputPath)).digest("hex"));
  }, 180_000);

  it("fails closed for unimplemented video paths", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-story-render-"));
    roots.push(root);
    const base = {
      jobId: "a".repeat(32), storyId: "b".repeat(32), storyRevision: 1, title: "故事", place: "产地",
      acts: [{ sequence: 1, stageId: "one", mediaId: "media-1", storyFunction: "people" as const, heading: "人",
        narration: "真实旁白", sourceBasis: "owner-confirmed", productReference: null }],
      output: { aspectRatio: "9:16" as const, width: 720 as const, height: 1280 as const, durationSeconds: 15,
        profiles: ["editorial-master" as const], captions: true as const },
      media: [{ id: "media-1", kind: "video" as const, mimeType: "video/mp4", sha256: "c".repeat(64), byteLength: 1,
        relativePath: "job/video.mp4", absolutePath: join(root, "video.mp4") }], publishClaimed: false as const,
    };
    const renderer = new HerWorldStoryRenderer({ workRoot: join(root, "work") });
    await expect(renderer.renderVisualDraft({ ...base, narration: { mode: "text-only", audioMediaId: null } })).rejects.toThrow("media-kind-not-yet-supported");
  });
});

function run(command: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(stderr || `command-exit-${code}`)));
  });
}
