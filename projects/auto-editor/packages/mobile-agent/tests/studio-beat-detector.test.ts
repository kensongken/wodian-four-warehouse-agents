import { describe, expect, it } from "vitest";

import { detectBeatTimesFromPcm } from "../src";

describe("studio beat detector", () => {
  it("finds a regular local pulse track with reviewable confidence", () => {
    const sampleRate = 8_000;
    const samples = new Int16Array(sampleRate * 10);
    for (let beatMs = 500; beatMs < 9_500; beatMs += 500) {
      const start = Math.floor(beatMs / 1_000 * sampleRate);
      for (let index = start; index < start + sampleRate * 0.04; index += 1) {
        samples[index] = index % 2 === 0 ? 24_000 : -24_000;
      }
    }

    const result = detectBeatTimesFromPcm(samples, sampleRate);

    expect(result.beatTimesMs.length).toBeGreaterThanOrEqual(15);
    expect(result.confidence).toBeGreaterThanOrEqual(0.45);
    expect(result.beatTimesMs[0]).toBeGreaterThanOrEqual(450);
    expect(result.beatTimesMs[0]).toBeLessThanOrEqual(550);
  });

  it("fails closed for silence", () => {
    expect(detectBeatTimesFromPcm(new Int16Array(8_000 * 3), 8_000)).toEqual({
      beatTimesMs: [],
      confidence: 0,
    });
  });
});
