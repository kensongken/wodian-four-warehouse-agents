import type { ContentSegment } from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import { retrieveHistoricalSegments } from "../src/historical-retriever";

describe("historical retriever", () => {
  const anchor = makeSegment("new_anchor", {
    products: [{ id: "prod_01", status: "confirmed" }],
    people: ["designer_01"],
    processStages: ["polishing"],
    topics: ["three-reworks"],
    emotions: ["persistence"],
  });

  it("filters permission before ranking relevance", () => {
    const sameProductWithdrawn = makeSegment("withdrawn", {
      permissionState: "withdrawn",
      products: [{ id: "prod_01", status: "confirmed" }],
    });
    const sameTopicAllowed = makeSegment("same_topic", {
      topics: ["three-reworks"],
    });
    const unrelatedAllowed = makeSegment("unrelated");

    const matches = retrieveHistoricalSegments(
      anchor,
      [sameProductWithdrawn, sameTopicAllowed, unrelatedAllowed],
      5,
    );

    expect(matches.map((item) => item.segment.id)).toEqual(["same_topic"]);
    expect(matches.some((item) => item.segment.id === "withdrawn")).toBe(false);
  });

  it("ranks story contribution above decoration and penalizes repetition", () => {
    const beforeStage = makeSegment("before_stage", {
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["making"],
    });
    const prettyButUnrelated = makeSegment("pretty", {
      baseTypes: ["product-image"],
    });
    const overusedBeforeStage = makeSegment("overused_before_stage", {
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["making"],
      usage: {
        storyIds: ["story_01", "story_02"],
        variants: ["xiaohongshu", "wechat-channels"],
        useCount: 12,
        lastUsedAt: "2026-09-03T10:00:00.000Z",
      },
    });

    const matches = retrieveHistoricalSegments(
      anchor,
      [overusedBeforeStage, prettyButUnrelated, beforeStage],
      2,
    );

    expect(matches[0]).toMatchObject({ segment: { id: "before_stage" } });
    expect(matches[0].reasons).toContain("same-product-before-stage");
    expect(matches.map((item) => item.segment.id)).not.toContain("pretty");
  });

  it("uses stable segment-id ordering when scores tie", () => {
    const matches = retrieveHistoricalSegments(
      anchor,
      [
        makeSegment("topic_b", { topics: ["three-reworks"] }),
        makeSegment("topic_a", { topics: ["three-reworks"] }),
      ],
      2,
    );

    expect(matches.map((item) => item.segment.id)).toEqual([
      "topic_a",
      "topic_b",
    ]);
  });
});

function makeSegment(
  id: string,
  patch: Partial<ContentSegment> &
    Partial<ContentSegment["tags"]> = {},
): ContentSegment {
  const { permissionState, ...tagPatch } = patch;
  return {
    id,
    assetId: `asset_${id}`,
    batchId: id.startsWith("new_") ? "batch_new" : "batch_history",
    startMs: 0,
    endMs: 10_000,
    permissionState: permissionState ?? "allowed",
    tags: {
      baseTypes: ["scene"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: {
        storyIds: [],
        variants: [],
        useCount: 0,
        lastUsedAt: null,
      },
      ...tagPatch,
    },
  };
}
