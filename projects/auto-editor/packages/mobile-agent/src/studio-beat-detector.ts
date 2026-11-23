import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import ffmpegPath from "ffmpeg-static";

import type { StudioEditorialBeatEvidence } from "./studio-capabilities";
import type { StudioRenderSource } from "./studio-ffmpeg-renderer";

const SAMPLE_RATE = 8_000;
const WINDOW_SAMPLES = 160;
const MIN_BEAT_GAP_MS = 320;

/** Extracts local audio and returns only reliable beat evidence. */
export async function detectStudioBeatEvidence(
  sources: readonly StudioRenderSource[],
  options: Readonly<{ ffmpegPath?: string | null }> = {},
): Promise<readonly StudioEditorialBeatEvidence[]> {
  const executable = options.ffmpegPath === undefined ? ffmpegPath : options.ffmpegPath;
  if (!executable) return [];
  const results = await Promise.all(sources.map(async (source) => {
    const root = await mkdtemp(join(tmpdir(), "studio-beat-"));
    try {
      const input = join(root, "source.bin");
      await writeFile(input, source.bytes, { flag: "wx" });
      const pcm = await extractMonoPcm(executable, input);
      const detection = detectBeatTimesFromPcm(pcm, SAMPLE_RATE);
      if (detection.beatTimesMs.length < 3 || detection.confidence < 0.45) return null;
      return {
        sourceAssetId: source.assetId,
        sourceDigest: source.blobDigest,
        beatTimesMs: detection.beatTimesMs,
        confidence: detection.confidence,
        detector: "audio-energy-onset-v1" as const,
      };
    } catch {
      return null;
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }));
  return results.filter((value): value is StudioEditorialBeatEvidence => value !== null);
}

/** Pure signal core kept exported so beat decisions can be tested without media fixtures. */
export function detectBeatTimesFromPcm(
  pcm: Int16Array,
  sampleRate: number,
): Readonly<{ beatTimesMs: readonly number[]; confidence: number }> {
  if (!Number.isSafeInteger(sampleRate) || sampleRate < 1_000 || pcm.length < sampleRate) {
    return { beatTimesMs: [], confidence: 0 };
  }
  const windowSamples = Math.max(32, Math.round(WINDOW_SAMPLES * sampleRate / SAMPLE_RATE));
  const energies: number[] = [];
  for (let offset = 0; offset + windowSamples <= pcm.length; offset += windowSamples) {
    let sum = 0;
    for (let index = offset; index < offset + windowSamples; index += 1) {
      const normalized = pcm[index]! / 32_768;
      sum += normalized * normalized;
    }
    energies.push(Math.sqrt(sum / windowSamples));
  }
  if (energies.length < 8) return { beatTimesMs: [], confidence: 0 };
  const novelty = energies.map((energy, index) => {
    const history = energies.slice(Math.max(0, index - 8), index);
    const baseline = history.length === 0 ? energy : history.reduce((sum, value) => sum + value, 0) / history.length;
    return Math.max(0, energy - baseline);
  });
  if (novelty.every((value) => value === 0)) return { beatTimesMs: [], confidence: 0 };
  const medianNovelty = median(novelty);
  const deviation = median(novelty.map((value) => Math.abs(value - medianNovelty)));
  const threshold = medianNovelty + Math.max(0.012, deviation * 3.5);
  const windowMs = windowSamples / sampleRate * 1_000;
  const candidates = novelty.flatMap((value, index) => {
    if (value < threshold || index < 2 || index >= novelty.length - 2) return [];
    const local = novelty.slice(index - 2, index + 3);
    if (value < Math.max(...local)) return [];
    return [{ timeMs: Math.round((index + 0.5) * windowMs), strength: value }];
  });
  const beats: typeof candidates = [];
  for (const candidate of candidates) {
    const previous = beats.at(-1);
    if (!previous || candidate.timeMs - previous.timeMs >= MIN_BEAT_GAP_MS) {
      beats.push(candidate);
    } else if (candidate.strength > previous.strength) {
      beats[beats.length - 1] = candidate;
    }
  }
  if (beats.length < 2) return { beatTimesMs: beats.map((beat) => beat.timeMs), confidence: 0 };
  const intervals = beats.slice(1).map((beat, index) => beat.timeMs - beats[index]!.timeMs);
  const middle = median(intervals);
  const regularity = middle <= 0
    ? 0
    : Math.max(0, 1 - median(intervals.map((value) => Math.abs(value - middle))) / middle);
  const prominence = Math.min(1, median(beats.map((beat) => beat.strength)) / Math.max(threshold, 0.001));
  const density = Math.min(1, beats.length / 8);
  const confidence = Number((regularity * 0.5 + prominence * 0.3 + density * 0.2).toFixed(3));
  return { beatTimesMs: beats.map((beat) => beat.timeMs), confidence };
}

function extractMonoPcm(executable: string, input: string): Promise<Int16Array> {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [
      "-hide_banner", "-loglevel", "error", "-i", input,
      "-vn", "-ac", "1", "-ar", String(SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ], { stdio: ["ignore", "pipe", "pipe"] });
    const chunks: Buffer[] = [];
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => chunks.push(chunk));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code !== 0) return reject(new Error(`studio-beat-audio-extract-failed:${stderr.slice(-500)}`));
      const buffer = Buffer.concat(chunks);
      const samples = new Int16Array(Math.floor(buffer.length / 2));
      for (let index = 0; index < samples.length; index += 1) samples[index] = buffer.readInt16LE(index * 2);
      resolve(samples);
    });
  });
}

function median(values: readonly number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[middle - 1]! + sorted[middle]!) / 2 : sorted[middle]!;
}
