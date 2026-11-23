import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { chmod, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { RealMaterialRunDecisionSchema } from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  prepareRealMaterialAcceptance,
  renderRealMaterialAcceptance,
} from "../src/real-material-acceptance";
import { parseCommand } from "../src/run-real-material-acceptance";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const roots: string[] = [];
const NOW = "2026-09-22T09:00:00.000Z";

beforeAll(() => {
  if (!ffmpegPath) throw new Error("test-ffmpeg-unavailable");
});

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("real material acceptance orchestrator", () => {
  it("keeps all stories unready and renders zero outputs without confirmed story facts", async () => {
    const fixture = await createFixture(false);

    const prepared = await prepareRealMaterialAcceptance(fixture.input);

    expect(prepared.report.storyResults.map(({ status }) => status)).toEqual([
      "insufficient-evidence",
      "insufficient-evidence",
      "insufficient-evidence",
    ]);
    expect(prepared.report.outputs).toEqual([]);
    expect(prepared.report.status).toBe("awaiting-decision");
    expect(prepared.report.publishedClaimed).toBe(false);
    expect(JSON.stringify(prepared.report)).not.toContain(fixture.root);
    expect(await readFile(prepared.reviewPacketPath, "utf8")).not.toContain(fixture.root);
    expect(await mediaOutputs(fixture.runDirectory)).toEqual([]);
    expect(sha256(await readFile(prepared.reviewPacketPath))).toBe(prepared.reviewPacketDigest);
  }, 180_000);

  it("rejects stale decisions, then renders one ready story into four bound variants", async () => {
    const fixture = await createFixture(true);
    const prepared = await prepareRealMaterialAcceptance(fixture.input);
    const packet = JSON.parse(await readFile(prepared.reviewPacketPath, "utf8")) as {
      proposedReferenceRuleIds: string[];
      mappingPlan: { id: string; version: number; digest: string };
    };
    expect(prepared.report.storyResults.find(({ storyType }) => storyType === "product-journey"))
      .toMatchObject({ status: "ready", errorCode: null });
    const decisionPath = join(fixture.root, "decision.json");
    const decision = RealMaterialRunDecisionSchema.parse({
      schemaVersion: 1,
      decisionId: "designer-decision-01",
      runId: fixture.manifest.runId,
      reviewPacketDigest: prepared.reviewPacketDigest,
      selectedStoryTypes: ["product-journey"],
      selectedReferenceRuleIds: packet.proposedReferenceRuleIds,
      mappingPlan: packet.mappingPlan,
      confirmedBy: "designer-01",
      confirmedAt: NOW,
      operationKey: "real-acceptance-render-01",
    });
    await writePrivateJson(decisionPath, { ...decision, reviewPacketDigest: "0".repeat(64) });
    await expect(renderRealMaterialAcceptance({
      ...fixture.input,
      decisionPath,
    })).rejects.toThrow("real-material-review-packet-stale");
    expect(await mediaOutputs(fixture.runDirectory)).toEqual([]);

    await expect(renderRealMaterialAcceptance({
      ...fixture.input,
      decisionPath: join(fixture.root, "missing-decision.json"),
    })).rejects.toThrow();
    await writePrivateJson(decisionPath, { ...decision, selectedStoryTypes: ["inner-journey"] });
    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-selected-story-not-ready");
    await writePrivateJson(decisionPath, { ...decision, selectedReferenceRuleIds: ["unknown-rule"] });
    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-reference-rule-selection-mismatch");
    await writePrivateJson(decisionPath, {
      ...decision,
      mappingPlan: { ...decision.mappingPlan, version: decision.mappingPlan.version + 1 },
    });
    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-mapping-plan-mismatch");
    const changedManifest = { ...fixture.manifest, createdAt: "2026-09-22T09:00:01.000Z" };
    await writePrivateJson(fixture.input.manifestPath, changedManifest);
    await writePrivateJson(decisionPath, decision);
    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-run-conflict");
    expect(await mediaOutputs(fixture.runDirectory)).toEqual([]);
    await writePrivateJson(fixture.input.manifestPath, fixture.manifest);

    await writePrivateJson(decisionPath, decision);
    const report = await renderRealMaterialAcceptance({ ...fixture.input, decisionPath });

    expect(report.status).toBe("rendered");
    expect(report.outputs).toHaveLength(4);
    expect(new Set(report.outputs.map(({ variant }) => variant))).toEqual(new Set([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]));
    expect(new Set(report.outputs.map(({ timelineDigest }) => timelineDigest))).toHaveLength(4);
    expect(new Set(report.outputs.map(({ artifactDigest }) => artifactDigest))).toHaveLength(4);
    expect(report.outputs.every(({ referenceBytesUsed, width, height, durationMs }) =>
      referenceBytesUsed === false && width === 720 && height === 1280 && durationMs >= 29_900
    )).toBe(true);
    expect(JSON.stringify(report)).not.toContain(fixture.root);
    expect(report.publishedClaimed).toBe(false);
    const outputPaths = await mediaOutputs(fixture.runDirectory);
    expect(outputPaths).toHaveLength(4);
    await Promise.all(outputPaths.map(async (path) => {
      const output = JSON.parse(await run(ffprobePath, [
        "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", path,
      ])) as { streams: Array<{ codec_type: string; codec_name: string }> };
      expect(output.streams).toEqual(expect.arrayContaining([
        expect.objectContaining({ codec_type: "video", codec_name: "h264" }),
        expect.objectContaining({ codec_type: "audio", codec_name: "aac" }),
      ]));
    }));

    const replay = await renderRealMaterialAcceptance({ ...fixture.input, decisionPath });
    expect(replay).toEqual(report);
    expect(await mediaOutputs(fixture.runDirectory)).toHaveLength(4);

    await writePrivateJson(decisionPath, { ...decision, decisionId: "designer-decision-changed" });
    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-decision-conflict");
    expect(await mediaOutputs(fixture.runDirectory)).toHaveLength(4);
  }, 300_000);

  it("fails closed when a locked source changes after preparation", async () => {
    const fixture = await createFixture(true);
    const prepared = await prepareRealMaterialAcceptance(fixture.input);
    const packet = JSON.parse(await readFile(prepared.reviewPacketPath, "utf8")) as {
      proposedReferenceRuleIds: string[];
      mappingPlan: { id: string; version: number; digest: string };
    };
    const decisionPath = join(fixture.root, "decision.json");
    await writePrivateJson(decisionPath, {
      schemaVersion: 1,
      decisionId: "designer-decision-source-change",
      runId: fixture.manifest.runId,
      reviewPacketDigest: prepared.reviewPacketDigest,
      selectedStoryTypes: ["product-journey"],
      selectedReferenceRuleIds: packet.proposedReferenceRuleIds,
      mappingPlan: packet.mappingPlan,
      confirmedBy: "designer-01",
      confirmedAt: NOW,
      operationKey: "real-acceptance-source-change",
    });
    await writeFile(fixture.currentPath, Buffer.from("source-mutated-after-review"));

    await expect(renderRealMaterialAcceptance({ ...fixture.input, decisionPath }))
      .rejects.toThrow("real-material-source-changed");
    expect(await mediaOutputs(fixture.runDirectory)).toEqual([]);
  }, 180_000);

  it("parses only the strict absolute-path CLI shape", () => {
    expect(parseCommand([
      "prepare",
      "--manifest", "/tmp/manifest.json",
      "--approved-root", "/tmp/materials",
      "--run-directory", "/tmp/run",
    ])).toMatchObject({ subcommand: "prepare", decisionPath: null });
    expect(parseCommand([
      "--",
      "prepare",
      "--manifest", "/tmp/manifest.json",
      "--approved-root", "/tmp/materials",
      "--run-directory", "/tmp/run",
    ])).toMatchObject({ subcommand: "prepare", decisionPath: null });
    expect(() => parseCommand([
      "prepare", "--manifest", "relative.json", "--approved-root", "/tmp", "--run-directory", "/tmp/run",
    ])).toThrow("real-material-cli-absolute-path-required");
    expect(() => parseCommand([
      "render", "--manifest", "/tmp/manifest.json", "--approved-root", "/tmp", "--run-directory", "/tmp/run",
    ])).toThrow("real-material-cli-decision-required-on-render");
    expect(() => parseCommand([
      "prepare", "--manifest", "/tmp/m.json", "--approved-root", "/tmp", "--run-directory", "/tmp/run",
      "--mystery", "/tmp/value",
    ])).toThrow("real-material-cli-unknown-flag");
  });
});

async function createFixture(ready: boolean) {
  const root = await mkdtemp(join(tmpdir(), "real-material-acceptance-"));
  roots.push(root);
  const sourceRoot = join(root, "authorized");
  const runDirectory = join(root, "run");
  await mkdir(sourceRoot, { recursive: true });
  const currentPath = join(sourceRoot, ready ? "current.mp4" : "finished.png");
  const supportPaths = [2, 3, 4].map((index) => join(sourceRoot, `support-${index}.mp4`));
  const referencePath = join(sourceRoot, "reference.mp4");
  if (ready) {
    await makeCustomerClips([currentPath, ...supportPaths]);
  }
  else await makeImage(currentPath);
  await makeSyntheticReference(referencePath);
  const currentDigest = sha256(await readFile(currentPath));
  const supportDigests = ready
    ? await Promise.all(supportPaths.map(async (path) => sha256(await readFile(path))))
    : [];
  const referenceDigest = sha256(await readFile(referencePath));
  const timedFact = (kind: string, value: string, start: number, end: number) => ({
    kind,
    value,
    sourceStartMs: ready ? start : null,
    sourceEndMs: ready ? end : null,
    confirmedBy: "designer-01",
    confirmedAt: NOW,
  });
  const manifest = {
    schemaVersion: 1 as const,
    runId: ready ? "test-ready-real-run" : "test-zero-story-run",
    customerId: ready ? "test-ready-customer" : "test-zero-customer",
    actorId: "designer-01",
    createdAt: NOW,
    purpose: "real-material-acceptance" as const,
    inputs: [
      {
        inputId: "current-material",
        role: "current" as const,
        mediaKind: ready ? "video" as const : "image" as const,
        sourcePath: currentPath,
        sourceDigest: currentDigest,
        permissionState: "allowed" as const,
        allowedUses: ready
          ? ["story-analysis", "render"] as const
          : ["story-analysis", "render", "still-motion"] as const,
        declaredOwnership: "customer-owned" as const,
        confirmedFacts: ready ? [
          timedFact("product-id", "ring-01", 0, 5_000),
          timedFact("process-stage", "design", 0, 5_000),
          timedFact("product-id", "ring-01", 5_000, 10_000),
          timedFact("process-stage", "finished", 5_000, 10_000),
        ] : [timedFact("product-id", "ring-01", 0, 0)],
      },
      ...(ready ? supportPaths.map((sourcePath, index) => ({
        inputId: `support-material-${index + 2}`,
        role: "historical" as const,
        mediaKind: "video" as const,
        sourcePath,
        sourceDigest: supportDigests[index]!,
        permissionState: "allowed" as const,
        allowedUses: ["story-analysis", "render"] as const,
        declaredOwnership: "customer-owned" as const,
        confirmedFacts: [timedFact("product-id", "ring-01", 0, 10_000)],
      })) : []),
      {
        inputId: "reference-video",
        role: "reference" as const,
        mediaKind: "video" as const,
        sourcePath: referencePath,
        sourceDigest: referenceDigest,
        permissionState: "allowed" as const,
        allowedUses: ["reference-analysis"] as const,
        declaredOwnership: "external-style-only" as const,
        confirmedFacts: [],
      },
    ],
    referencePolicy: "external-style-only" as const,
    requestedStoryTypes: ["product-journey", "inner-journey", "persona-monologue"] as const,
    requestedVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const,
  };
  const manifestPath = join(root, "manifest.json");
  await writePrivateJson(manifestPath, manifest);
  return {
    root,
    runDirectory,
    currentPath,
    manifest,
    input: { manifestPath, approvedRoots: [sourceRoot], runDirectory },
  };
}

async function makeImage(path: string): Promise<void> {
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
    "color=c=0xd78e44:s=721x1279:d=0.04,format=rgba", "-frames:v", "1", "-y", path,
  ]);
}

async function makeSyntheticReference(path: string): Promise<void> {
  const pulses = [0.75, 2, 3.25, 4.5, 5.75, 7, 8.25, 9.5]
    .map((start) => `between(t\\,${start}\\,${(start + 0.08).toFixed(2)})`)
    .join("+");
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", "smptebars=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=3",
    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
    "-f", "lavfi", "-i", `aevalsrc=if(gt(${pulses}\\,0)\\,0.8*sin(2*PI*880*t)\\,0):s=8000:d=11`,
    "-filter_complex",
    "[0:v]settb=AVTB,format=yuv420p[a];[1:v]settb=AVTB,format=yuv420p[b];" +
      "[2:v]settb=AVTB,format=yuv420p[c];[3:v]settb=AVTB,format=yuv420p[d];" +
      "[a][b]concat=n=2:v=1:a=0[ab];[ab]settb=AVTB[ab2];" +
      "[ab2][c]xfade=transition=fadeblack:duration=0.5:offset=5.5[abc];" +
      "[abc]settb=AVTB[abc2];[abc2][d]xfade=transition=fade:duration=0.5:offset=8,format=yuv420p[v]",
    "-map", "[v]", "-map", "4:a", "-t", "11",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", path,
  ]);
}

async function makeCustomerClips(paths: readonly string[]): Promise<void> {
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x6d3b2d:s=180x320:r=25:d=3",
    "-f", "lavfi", "-i", "color=c=0x326a63:s=180x320:r=25:d=2.68",
    "-f", "lavfi", "-i", "color=c=0x876942:s=180x320:r=25:d=5.32",
    "-f", "lavfi", "-i", "sine=frequency=392:sample_rate=44100:duration=11",
    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
    "-map", "[v]", "-map", "3:a", "-t", "11", "-c:v", "libx264", "-preset", "ultrafast",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-y", paths[0]!,
  ]);
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=25:duration=3",
    "-f", "lavfi", "-i", "smptebars=size=180x320:rate=25:duration=8",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=11",
    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
    "-map", "[v]", "-map", "2:a", "-t", "11", "-c:v", "libx264", "-preset", "ultrafast",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-y", paths[1]!,
  ]);
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "testsrc=size=180x320:rate=25:duration=8.5",
    "-f", "lavfi", "-i", "color=c=0x425c78:s=180x320:r=25:d=3",
    "-f", "lavfi", "-i", "sine=frequency=494:sample_rate=44100:duration=11",
    "-filter_complex", "[0:v]settb=AVTB,format=yuv420p[a];[1:v]settb=AVTB,format=yuv420p[b];" +
      "[a][b]xfade=transition=fadeblack:duration=0.5:offset=8,format=yuv420p[v]",
    "-map", "[v]", "-map", "2:a", "-t", "11", "-c:v", "libx264", "-preset", "ultrafast",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-y", paths[2]!,
  ]);
  await run(ffmpegPath!, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x5a416f:s=180x320:r=25:d=11",
    "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=44100:duration=11",
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart", "-y", paths[3]!,
  ]);
}

async function mediaOutputs(runDirectory: string): Promise<string[]> {
  const root = join(runDirectory, "outputs");
  const results: string[] = [];
  async function walk(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true }).catch(() => [])) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile() && entry.name.endsWith(".mp4")) results.push(path);
    }
  }
  await walk(root);
  return results.sort();
}

async function writePrivateJson(path: string, value: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  await chmod(path, 0o600);
}

function sha256(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function run(command: string, args: readonly string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], { stdio: ["ignore", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => code === 0
      ? resolve(Buffer.concat(stdout).toString("utf8"))
      : reject(new Error(Buffer.concat(stderr).toString("utf8"))));
  });
}
