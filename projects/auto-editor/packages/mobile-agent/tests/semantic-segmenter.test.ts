import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { LibraryAsset } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JsonContentStore } from "../src/json-content-store";
import {
  segmentNewBatch,
  type SegmentationAnalyzer,
} from "../src/semantic-segmenter";

describe("semantic segmenter", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "semantic-segmenter-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("uses analyzer topic and action boundaries instead of fixed durations", async () => {
    const videoAsset = makeAsset("video_01", "video");
    await library.putAsset(videoAsset);
    const analyzer: SegmentationAnalyzer = {
      analyze: async () => [
        {
          startMs: 0,
          endMs: 42_000,
          transcript: "为什么选这块石头",
          signals: ["topic-change"],
        },
        {
          startMs: 42_000,
          endMs: 131_000,
          transcript: "三次返工",
          signals: ["action-change", "pause"],
        },
      ],
    };

    const segments = await segmentNewBatch(
      "batch_01",
      [videoAsset],
      analyzer,
      library,
    );

    expect(segments.map((item) => [item.startMs, item.endMs])).toEqual([
      [0, 42_000],
      [42_000, 131_000],
    ]);
    expect(await library.listUntypedSegments()).toEqual(segments);
  });

  it("does not send image assets through the video analyzer", async () => {
    const imageAsset = makeAsset("image_01", "image");
    await library.putAsset(imageAsset);
    const analyze = vi.fn<SegmentationAnalyzer["analyze"]>();

    const segments = await segmentNewBatch(
      "batch_01",
      [imageAsset],
      { analyze },
      library,
    );

    expect(analyze).not.toHaveBeenCalled();
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({ startMs: 0, endMs: 1 });
  });

  it("rejects overlapping analyzer boundaries", async () => {
    const videoAsset = makeAsset("video_02", "video");
    await library.putAsset(videoAsset);
    const analyzer: SegmentationAnalyzer = {
      analyze: async () => [
        { startMs: 0, endMs: 20_000, transcript: "A", signals: [] },
        { startMs: 19_000, endMs: 30_000, transcript: "B", signals: [] },
      ],
    };

    await expect(
      segmentNewBatch("batch_01", [videoAsset], analyzer, library),
    ).rejects.toThrow("overlapping-boundaries");
  });

  it("keeps the raw asset and returns no fabricated segments when analysis fails", async () => {
    const videoAsset = makeAsset("video_03", "video");
    await library.putAsset(videoAsset);
    const analyzer: SegmentationAnalyzer = {
      analyze: async () => {
        throw new Error("decoder-failed");
      },
    };

    await expect(
      segmentNewBatch("batch_01", [videoAsset], analyzer, library),
    ).resolves.toEqual([]);
    expect(await library.listAssets()).toContainEqual(
      expect.objectContaining({
        id: videoAsset.id,
        technicalFlags: ["segmentation-failed"],
      }),
    );
  });
});

function makeAsset(
  id: string,
  mediaKind: LibraryAsset["mediaKind"],
): LibraryAsset {
  return {
    id,
    batchId: "batch_01",
    sessionId: "session_01",
    digest: `digest_${id}`,
    localPath: `/media/${id}`,
    mediaKind,
    importedAt: "2026-09-03T10:02:00.000Z",
    permissionState: "allowed",
    origin: "new",
    technicalFlags: [],
  };
}
