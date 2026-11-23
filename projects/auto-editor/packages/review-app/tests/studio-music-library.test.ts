import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import ffmpegPath from "ffmpeg-static";
import { afterEach, describe, expect, it } from "vitest";

import { analyzeStudioMusicAudio, StudioMusicLibrary } from "../src/studio-music-library";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const execFileAsync = promisify(execFile);

describe("studio music analysis", () => {
  const roots: string[] = [];
  afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

  it("extracts a compact waveform and recognizes a 120 BPM click track locally", async () => {
    if (!ffmpegPath || !ffprobePath) throw new Error("music-analysis-tools-unavailable");
    const root = await mkdtemp(join(tmpdir(), "studio-music-analysis-"));
    roots.push(root);
    const path = join(root, "click-track.wav");
    await execFileAsync(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
      "aevalsrc=if(lt(mod(t\\,0.5)\\,0.05)\\,0.8\\,0):s=8000:d=8",
      "-c:a", "pcm_s16le", "-y", path,
    ], { timeout: 30_000 });
    const profile = await analyzeStudioMusicAudio(ffmpegPath, await readFile(path), "click-track.wav", 8);
    expect(profile.analyzer).toBe("local-audio-energy-v1");
    expect(profile.tempoBpm).toBe(120);
    expect(profile.waveform.length).toBeGreaterThanOrEqual(70);
    expect(profile.waveform.every((value) => value >= 0 && value <= 1)).toBe(true);
    expect(profile.highlightStartMs).toBeGreaterThanOrEqual(0);
    expect(profile.reason).toContain("约 120 BPM");
  });

  it("reuses one music asset while keeping each video brief selection and mix independent", async () => {
    if (!ffmpegPath || !ffprobePath) throw new Error("music-analysis-tools-unavailable");
    const root = await mkdtemp(join(tmpdir(), "studio-music-briefs-"));
    roots.push(root);
    await writeFile(join(root, "music-library.json"), `${JSON.stringify({
      schemaVersion: 1,
      selectedTrackId: null,
      tracks: [{
        assetId: "music-shared", digest: "a".repeat(64), fileName: "共享配乐.m4a", durationSeconds: 12,
        mixPreference: { trackStartMs: 0, originalGainDb: 0, musicGainDb: -16 },
        audioProfile: { analyzer: "local-audio-energy-v1", energy: "strong", tempoBpm: 120,
          highlightStartMs: 4_000, waveform: Array(96).fill(0.6), reason: "fixture" },
        rights: { sourceKind: "customer-owned", licenseNote: "客户自有", previewUseAllowed: true,
          publishUseAllowed: false, confirmedBy: "actor-a", confirmedAt: "2026-10-14T08:00:00.000Z" },
        createdAt: "2026-10-14T08:00:00.000Z",
      }],
    })}\n`);
    const library = await StudioMusicLibrary.open({
      root,
      library: { readOriginal: async () => Buffer.from("music") } as never,
      scope: { actorId: "actor-a", customerId: "customer-a", sessionId: "session-a" },
      ffprobePath, ffmpegPath, now: () => "2026-10-14T08:00:00.000Z",
    });

    await library.select("music-shared", "brief-fast");
    await library.updateMixPreference("music-shared", {
      trackStartMs: 500, originalGainDb: -3, musicGainDb: -20,
    }, "brief-fast");
    await library.select("music-shared", "brief-soft");

    expect(library.list("brief-fast")).toMatchObject({
      selectedTrackId: "music-shared",
      tracks: [{ mixPreference: { trackStartMs: 500, originalGainDb: -3, musicGainDb: -20 } }],
    });
    expect(library.list("brief-soft")).toMatchObject({
      selectedTrackId: "music-shared",
      tracks: [{ mixPreference: { trackStartMs: 4_000, originalGainDb: 0, musicGainDb: -16 } }],
    });
    expect(library.list("brief-new").selectedTrackId).toBeNull();
    await expect(library.currentTrack("brief-fast")).resolves.toMatchObject({
      assetId: "music-shared",
      mixPreference: { trackStartMs: 500, originalGainDb: -3, musicGainDb: -20 },
    });
    const feedback = { briefId: "brief-feedback", contentType: "jewelry-making-process" as const };
    await library.select("music-shared", "brief-feedback", feedback);
    await library.updateMixPreference("music-shared", {
      trackStartMs: 800, originalGainDb: -2, musicGainDb: -18,
    }, "brief-feedback", feedback);
    await library.select(null, "brief-feedback", feedback);
    expect(library.preferenceSummaries("jewelry-making-process")).toEqual([
      expect.objectContaining({
        trackId: "music-shared", selectedCount: 1, clearedCount: 1,
        mixAdjustedCount: 1, preferenceScore: 2,
      }),
    ]);
  });
});
