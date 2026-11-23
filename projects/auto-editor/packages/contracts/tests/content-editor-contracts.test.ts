import { describe, expect, it } from "vitest";

import {
  ContentSegmentSchema,
  EditRevisionSchema,
  StoryCandidateSchema,
} from "../src/index";

describe("content editor contracts", () => {
  it("allows one segment to carry multiple factual asset types", () => {
    const segment = ContentSegmentSchema.parse({
      id: "seg_new_01",
      assetId: "asset_new_01",
      batchId: "batch_01",
      startMs: 0,
      endMs: 18_000,
      permissionState: "allowed",
      tags: {
        baseTypes: ["handmade", "persona-copy"],
        people: ["designer_01"],
        products: [{ id: "prod_01", status: "suggested" }],
        processStages: ["rework"],
        topics: ["坚持返工"],
        emotions: ["persistence"],
        transcript: "我愿意再返工一次",
        technicalFlags: [],
        usage: {
          storyIds: [],
          variants: [],
          useCount: 0,
          lastUsedAt: null,
        },
      },
    });

    expect(segment.tags.baseTypes).toEqual(["handmade", "persona-copy"]);
  });

  it("requires every story to have at least one new-batch anchor", () => {
    expect(() =>
      StoryCandidateSchema.parse({
        id: "story_01",
        batchId: "batch_01",
        type: "inner-journey",
        anchorSegmentIds: [],
        historicalSegmentIds: ["seg_old_01"],
        historicalReasons: { seg_old_01: "same product before rework" },
        status: "candidate",
      }),
    ).toThrow("new-batch-anchor-required");
  });

  it("requires four distinct video variants in a revision", () => {
    expect(() =>
      EditRevisionSchema.parse({
        id: "revision_01_v1",
        storyId: "story_01",
        revision: 1,
        parentRevisionId: null,
        designerPrompt: null,
        changeExplanation: null,
        variants: [
          {
            kind: "editorial-master",
            timeline: [],
            title: "返工三次",
            caption: "",
            cover: { segmentId: "seg_new_01", atMs: 1_000 },
          },
        ],
        selectedStills: [],
        createdAt: "2026-09-03T12:00:00.000Z",
      }),
    ).toThrow("four-distinct-variants-required");
  });
});
