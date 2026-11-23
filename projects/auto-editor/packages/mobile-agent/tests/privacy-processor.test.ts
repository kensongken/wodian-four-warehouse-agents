import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { JsonContentStore } from "../src/json-content-store";
import { createPrivacyDerivative } from "../src/privacy-processor";

describe("privacy processor", () => {
  let directory: string;
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), "privacy-processor-")); });
  afterEach(async () => { await rm(directory, { recursive: true, force: true }); });

  it("creates and registers a byte-verified privacy derivative", async () => {
    if (!ffmpegPath) throw new Error("ffmpeg unavailable");
    const sourcePath = join(directory, "source.mp4");
    await run(ffmpegPath, [
      "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.3",
      "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
      "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", sourcePath,
    ]);
    const sourceDigest = digest(await readFile(sourcePath));
    const library = await JsonContentStore.open(join(directory, "library.json"));
    await library.putAsset({
      id: "raw_visitor", batchId: "batch_1", sessionId: "session_1",
      digest: sourceDigest, localPath: sourcePath, mediaKind: "video",
      importedAt: "2026-09-04T00:00:00.000Z", permissionState: "review-required",
      origin: "new", technicalFlags: [],
    });

    const derivative = await createPrivacyDerivative({
      sourceAssetId: "raw_visitor",
      outputAssetId: "safe_visitor",
      outputPath: join(directory, "safe.mp4"),
      actions: ["voice-muted", "full-frame-blurred"],
    }, library);

    expect(derivative.digest).toBe(digest(await readFile(derivative.localPath)));
    expect(derivative.digest).not.toBe(sourceDigest);
    expect(derivative.privacyEvidence).toMatchObject({
      processor: "ffmpeg-privacy-v1", sourceDigest, outputDigest: derivative.digest,
    });
  });
});

function digest(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

async function run(command: string, args: string[]): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, { stdio: "ignore" });
    child.on("error", reject);
    child.on("close", (code) => code === 0 ? resolve() : reject(new Error(`ffmpeg:${code}`)));
  });
}
