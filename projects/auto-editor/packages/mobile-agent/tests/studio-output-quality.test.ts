import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import ffmpegPath from "ffmpeg-static";
import { afterEach, describe, expect, it } from "vitest";

import { inspectStudioOutputQuality } from "../src";

const execFileAsync = promisify(execFile);
const cleanup: string[] = [];

afterEach(async () => {
  await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
});

describe("studio output quality gate", () => {
  it("passes moving video with audible audio", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-quality-good-"));
    cleanup.push(root);
    const path = join(root, "good.mp4");
    await makeVideo(path, "testsrc2=size=720x1280:rate=24", "sine=frequency=880:sample_rate=48000");

    const report = await inspectStudioOutputQuality(path, {
      durationSeconds: 30,
      captionStyle,
      captionCues: [captionCue({
        id: "caption-1",
        sourceStartMs: 1_000,
        sourceEndMs: 3_000,
        timelineStartMs: 1_000,
        timelineEndMs: 3_000,
      })],
    });

    expect(report.status).toBe("passed");
    expect(report.checks).toHaveLength(6);
    expect(report.checks.every((check) => check.status === "passed")).toBe(true);

    const overlapping = await inspectStudioOutputQuality(path, {
      durationSeconds: 30,
      captionStyle,
      captionCues: [
        captionCue({
          id: "caption-1",
          sourceStartMs: 1_000,
          sourceEndMs: 3_000,
          timelineStartMs: 1_000,
          timelineEndMs: 3_000,
        }),
        captionCue({
          id: "caption-2",
          sourceStartMs: 2_500,
          sourceEndMs: 4_000,
          timelineStartMs: 2_500,
          timelineEndMs: 4_000,
        }),
      ],
    });
    expect(overlapping.status).toBe("failed");
    expect(overlapping.checks.find((check) => check.id === "caption-safe-area"))
      .toMatchObject({ status: "failed", message: "字幕重叠、原声绑定或安全区不合格" });

    const detachedFromSource = await inspectStudioOutputQuality(path, {
      durationSeconds: 30,
      captionStyle,
      captionCues: [captionCue({
        id: "caption-1",
        sourceStartMs: 1_000,
        sourceEndMs: 3_000,
        timelineStartMs: 1_000,
        timelineEndMs: 4_000,
      })],
    });
    expect(detachedFromSource.status).toBe("failed");
    expect(detachedFromSource.checks.find((check) => check.id === "caption-safe-area")?.status)
      .toBe("failed");
  }, 30_000);

  it("fails a black and silent output before approval", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-quality-bad-"));
    cleanup.push(root);
    const path = join(root, "bad.mp4");
    await makeVideo(path, "color=c=black:size=720x1280:rate=24", "anullsrc=r=48000:cl=stereo");

    const report = await inspectStudioOutputQuality(path, { durationSeconds: 30 });

    expect(report.status).toBe("failed");
    expect(report.checks.find((check) => check.id === "black-frames")?.status).toBe("failed");
    expect(report.checks.find((check) => check.id === "audio-level")?.status).toBe("failed");
  }, 30_000);
});

const captionStyle = {
  template: "clean-outline" as const,
  fontSizePx: 48,
  primaryColor: "#FFFFFF",
  outlineColor: "#000000",
  outlineWidthPx: 3,
  bottomMarginPx: 160,
  maxCharsPerLine: 14,
};

function captionCue(timing: Readonly<{
  id: string;
  sourceStartMs: number;
  sourceEndMs: number;
  timelineStartMs: number;
  timelineEndMs: number;
}>) {
  return {
    ...timing,
    sourceAssetId: "asset-1",
    sourceDigest: "a".repeat(64),
    text: "这是一句本地测试字幕",
    reviewStatus: "model-suggestion" as const,
  };
}

async function makeVideo(path: string, videoSource: string, audioSource: string): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  await execFileAsync(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", videoSource,
    "-f", "lavfi", "-i", audioSource,
    "-t", "30", "-shortest",
    "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-y", path,
  ]);
}
