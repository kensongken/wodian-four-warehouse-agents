import type {
  ContentSegment,
  StoryCandidate,
  VariantPlan,
} from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import {
  planInitialRevision,
  type EditPlanAnalyzer,
} from "../src/edit-planner";

describe("edit planner", () => {
  const story: StoryCandidate = {
    id: "story_01",
    batchId: "batch_new",
    type: "product-journey",
    anchorSegmentIds: ["new_open", "new_process"],
    historicalSegmentIds: ["history_context"],
    historicalReasons: { history_context: "same-product-before-stage" },
    status: "candidate",
  };
  const segments = [
    makeSegment("new_open"),
    makeSegment("new_process"),
    makeSegment("history_context"),
  ];
  const segmentIds = new Set(segments.map((segment) => segment.id));

  it("plans a master and three structurally distinct platform variants", async () => {
    const revision = await planInitialRevision(
      story,
      segments,
      deterministicPlanner,
    );

    expect(revision.variants.map((item) => item.kind)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(revision.variants[1].timeline).not.toEqual(
      revision.variants[0].timeline,
    );
    expect(revision.variants[2].timeline).not.toEqual(
      revision.variants[1].timeline,
    );
    expect(
      revision.variants
        .flatMap((item) => item.timeline)
        .every((item) => segmentIds.has(item.segmentId)),
    ).toBe(true);
    expect(
      revision.variants.every(
        (item) => item.title.length > 0 && segmentIds.has(item.cover.segmentId),
      ),
    ).toBe(true);
    expect(revision).toMatchObject({
      id: "revision_story_01_1",
      revision: 1,
      parentRevisionId: null,
      designerPrompt: null,
      changeExplanation: null,
    });
  });

  it("selects stills only from story segments and records a reason", async () => {
    const revision = await planInitialRevision(
      story,
      segments,
      deterministicPlanner,
    );

    expect(
      revision.selectedStills.every(
        (still) => segmentIds.has(still.segmentId) && still.reason.length > 0,
      ),
    ).toBe(true);
  });

  it("rejects a platform plan that only renames the master", async () => {
    const master = makeVariant(["new_open", "new_process"], "母版");
    const duplicatePlanner: EditPlanAnalyzer = {
      plan: async () => ({
        master,
        xiaohongshu: { ...master, title: "小红书标题" },
        douyin: makeVariant(["new_open"], "抖音标题"),
        wechatChannels: makeVariant(
          ["new_process", "new_open"],
          "视频号标题",
        ),
        stills: [],
      }),
    };

    await expect(
      planInitialRevision(story, segments, duplicatePlanner),
    ).rejects.toThrow("variant-timelines-must-differ");
  });

  it("rejects media outside the approved story lineage", async () => {
    const invalidPlanner: EditPlanAnalyzer = {
      plan: async () => ({
        ...(await deterministicPlanner.plan({ story, segments })),
        stills: [{ segmentId: "not_in_story", atMs: 0, reason: "pretty" }],
      }),
    };

    await expect(
      planInitialRevision(story, segments, invalidPlanner),
    ).rejects.toThrow("segment-outside-story-lineage");
  });
});

const deterministicPlanner: EditPlanAnalyzer = {
  plan: async () => ({
    master: makeVariant(
      ["history_context", "new_open", "new_process"],
      "从选材到完成",
    ),
    xiaohongshu: makeVariant(
      ["new_process", "new_open"],
      "为什么这一步决定成败",
    ),
    douyin: makeVariant(
      ["new_open", "new_process"],
      "抖音快节奏",
    ),
    wechatChannels: makeVariant(
      ["new_open", "history_context", "new_process"],
      "一件珠宝的诞生",
    ),
    stills: [
      { segmentId: "new_process", atMs: 2_000, reason: "手工细节清楚" },
    ],
  }),
};

function makeVariant(
  ids: string[],
  title: string,
): Omit<VariantPlan, "kind"> {
  return {
    timeline: ids.map((segmentId) => ({
      segmentId,
      inMs: 0,
      outMs: 5_000,
      role: "anchor" as const,
    })),
    title,
    caption: `${title}的文案`,
    cover: { segmentId: ids[0], atMs: 1_000 },
  };
}

function makeSegment(id: string): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId: id.startsWith("history") ? "batch_history" : "batch_new",
    startMs: 0,
    endMs: 10_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["handmade"],
      people: ["designer_01"],
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["making"],
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
    },
  };
}
