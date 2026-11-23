import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { dirname, extname, join } from "node:path";

import { LibraryAssetSchema, type LibraryAsset } from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";

import type { ContentLibrary } from "./content-library";

const PROCESSOR = "ffmpeg-privacy-v1";

export async function createPrivacyDerivative(
  input: Readonly<{
    sourceAssetId: string;
    outputAssetId: string;
    outputPath: string;
    actions: Array<"voice-muted" | "full-frame-blurred">;
  }>,
  library: ContentLibrary,
): Promise<LibraryAsset> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const source = (await library.listAssets()).find((asset) => asset.id === input.sourceAssetId);
  if (!source) throw new Error("privacy-source-asset-not-found");
  if (source.mediaKind === "audio") throw new Error("privacy-audio-source-unsupported");
  const actions = [...new Set(input.actions)];
  if (actions.length === 0) throw new Error("privacy-actions-required");
  if ((await digestFile(source.localPath)) !== source.digest) throw new Error("source-digest-mismatch");
  await mkdir(dirname(input.outputPath), { recursive: true });

  await transform(source, actions, input.outputPath);
  const info = await stat(input.outputPath);
  if (!info.isFile() || info.size === 0) throw new Error("privacy-output-empty");
  const outputDigest = await digestFile(input.outputPath);
  const result = LibraryAssetSchema.parse({
    ...source,
    id: input.outputAssetId,
    digest: outputDigest,
    localPath: input.outputPath,
    importedAt: new Date().toISOString(),
    permissionState: "allowed",
    derivedFromAssetId: source.id,
    privacyActions: actions,
    privacyEvidence: {
      processor: PROCESSOR,
      sourceDigest: source.digest,
      outputDigest,
      processedAt: new Date().toISOString(),
    },
  });
  await library.putAsset(result);
  return result;
}

export async function verifyPrivacyDerivative(
  source: LibraryAsset,
  derivative: LibraryAsset,
): Promise<void> {
  if (!ffmpegPath || !derivative.privacyEvidence || !derivative.privacyActions) {
    throw new Error("privacy-processing-evidence-invalid");
  }
  const actions = derivative.privacyActions;
  if (actions.some((action) => action !== "voice-muted" && action !== "full-frame-blurred")) {
    throw new Error("privacy-processing-evidence-invalid");
  }
  const directory = await mkdtemp(join(dirname(derivative.localPath), ".privacy-verify-"));
  const expectedPath = join(directory, `expected${extname(derivative.localPath) || ".mp4"}`);
  try {
    await transform(source, actions as Array<"voice-muted" | "full-frame-blurred">, expectedPath);
    if ((await digestFile(expectedPath)) !== derivative.digest) {
      throw new Error("privacy-derivative-byte-verification-failed");
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

async function transform(
  source: LibraryAsset,
  actions: Array<"voice-muted" | "full-frame-blurred">,
  outputPath: string,
): Promise<void> {
  if (!ffmpegPath) throw new Error("ffmpeg-binary-unavailable");
  const args = ["-hide_banner", "-loglevel", "error", "-i", source.localPath];
  if (actions.includes("full-frame-blurred")) args.push("-vf", "boxblur=10:1");
  if (source.mediaKind === "video") {
    if (actions.includes("voice-muted")) args.push("-an");
    args.push("-c:v", "libx264", "-pix_fmt", "yuv420p");
  }
  args.push("-y", outputPath);
  await run(ffmpegPath, args);
}

async function run(command: string, args: string[]): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.on("error", reject);
    child.on("close", (code) => code === 0 ? resolve() : reject(new Error(`privacy-processing-failed:${stderr.slice(-500)}`)));
  });
}

async function digestFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolve);
  });
  return hash.digest("hex");
}
