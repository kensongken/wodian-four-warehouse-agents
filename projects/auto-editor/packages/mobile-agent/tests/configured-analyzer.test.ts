import type { ContentSegment, EditRevision } from "@wearable/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfiguredAnalyzer } from "../src/configured-analyzer";

describe("configured analyzer", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("refuses to pretend a production model exists without full config", () => {
    expect(() =>
      ConfiguredAnalyzer.create({ endpoint: "https://model.example/edit" }),
    ).toThrow("production-model-unconfigured");
  });

  it("posts only authorized segment metadata and validates the response", async () => {
    const responseEdit = {
      targetVariants: ["xiaohongshu"],
      replacementTimelines: {
        xiaohongshu: [
          { segmentId: "segment_01", inMs: 0, outMs: 1_000, role: "anchor" },
        ],
      },
      removePeople: [],
      explanation: "move product close-up first",
    };
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify(responseEdit), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const analyzer = ConfiguredAnalyzer.create({
      endpoint: "https://model.example/edit",
      model: "configured-editor-v1",
      apiKey: "secret-test-key",
    });

    await expect(
      analyzer.interpretPrompt({
        prompt: "put product first",
        revision: makeRevision(),
        segments: [makeSegment()],
      }),
    ).resolves.toEqual(responseEdit);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [endpoint, request] = fetchMock.mock.calls[0];
    if (!request) throw new Error("expected-fetch-request-options");
    expect(endpoint).toBe("https://model.example/edit");
    expect(request.headers).toMatchObject({
      authorization: "Bearer secret-test-key",
      "content-type": "application/json",
    });
    const body = JSON.parse(String(request.body));
    expect(body.model).toBe("configured-editor-v1");
    expect(body.input.segments[0]).toMatchObject({
      id: "segment_01",
      permissionState: "allowed",
      tags: expect.any(Object),
    });
    expect(JSON.stringify(body)).not.toMatch(/localPath|originalBytes|secret-test-key/);
  });

  it("rejects a model response outside the edit contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () =>
        new Response(JSON.stringify({ madeUp: true }), { status: 200 }),
      ),
    );
    const analyzer = ConfiguredAnalyzer.create({
      endpoint: "https://model.example/edit",
      model: "configured-editor-v1",
      apiKey: "secret-test-key",
    });

    await expect(
      analyzer.interpretPrompt({
        prompt: "change it",
        revision: makeRevision(),
        segments: [makeSegment()],
      }),
    ).rejects.toThrow();
  });
});

function makeRevision(): EditRevision {
  return {
    id: "revision_story_01_1",
    storyId: "story_01",
    revision: 1,
    parentRevisionId: null,
    designerPrompt: null,
    changeExplanation: null,
    variants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"].map(
      (kind) => ({
        kind: kind as "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels",
        timeline: [
          { segmentId: "segment_01", inMs: 0, outMs: 1_000, role: "anchor" },
        ],
        title: `${kind} title`,
        caption: "caption",
        cover: { segmentId: "segment_01", atMs: 100 },
      }),
    ),
    selectedStills: [],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}

function makeSegment(): ContentSegment {
  return {
    id: "segment_01",
    assetId: "asset_01",
    batchId: "batch_01",
    startMs: 0,
    endMs: 2_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["product-image"],
      people: [],
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}
