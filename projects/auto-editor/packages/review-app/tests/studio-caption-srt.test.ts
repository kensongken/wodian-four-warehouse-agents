import { describe, expect, it } from "vitest";

import { captionCuesToSrt } from "../src/studio-server";

describe("studio caption SRT handoff", () => {
  it("sorts model suggestions, formats milliseconds, and neutralizes SRT control text", () => {
    const digest = "a".repeat(64);
    const srt = captionCuesToSrt([
      {
        id: "caption-later",
        sourceAssetId: "asset-a",
        sourceDigest: digest,
        sourceStartMs: 3_000,
        sourceEndMs: 4_250,
        timelineStartMs: 10_500,
        timelineEndMs: 11_750,
        text: "第二句\n继续",
        reviewStatus: "model-suggestion",
      },
      {
        id: "caption-first",
        sourceAssetId: "asset-a",
        sourceDigest: digest,
        sourceStartMs: 1_000,
        sourceEndMs: 2_000,
        timelineStartMs: 250,
        timelineEndMs: 1_250,
        text: "第一句 --> 待核对",
        reviewStatus: "model-suggestion",
      },
    ]);

    expect(srt).toBe([
      "1",
      "00:00:00,250 --> 00:00:01,250",
      "第一句 → 待核对",
      "",
      "2",
      "00:00:10,500 --> 00:00:11,750",
      "第二句 继续",
      "",
    ].join("\n"));
  });
});
