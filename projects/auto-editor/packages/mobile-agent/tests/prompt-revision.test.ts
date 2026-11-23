import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  ContentSegment,
  EditRevision,
  LibraryAsset,
  TimelineItem,
  VariantArtifact,
  VideoVariant,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { JsonContentStore } from "../src/json-content-store";
import { calculateRenderInputDigest } from "../src/ffmpeg-renderer";
import {
  commitPromptRevision,
  reviseWithPrompt,
  type PromptEditInterpreter,
} from "../src/prompt-revision";

describe("prompt revisions", () => {
  let testDirectory: string;
  let library: JsonContentStore;
  let firstRevision: EditRevision;
  let allowedSegments: ContentSegment[];

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "prompt-revision-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
    firstRevision = makeRevision();
    allowedSegments = [
      makeSegment("new_quote_01"),
      makeSegment("new_process_01"),
      makeSegment("old_quote_01"),
    ];
    await library.putStories([{
      id: firstRevision.storyId,
      batchId: "batch_new",
      type: "persona-monologue",
      anchorSegmentIds: ["new_quote_01", "new_process_01"],
      historicalSegmentIds: ["old_quote_01"],
      historicalReasons: { old_quote_01: "same-person" },
      status: "candidate",
    }]);
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("creates revision 2 without overwriting revision 1", async () => {
    const next = await reviseWithPrompt(
      {
        revision: firstRevision,
        prompt: "视频号开头换成去年讲返工的旧口播，不要客户声音",
        allowedSegments,
        designerId: "designer_01",
      },
      interpreterSelecting("old_quote_01"),
      library,
    );

    expect(next).toMatchObject({
      revision: 2,
      parentRevisionId: firstRevision.id,
      designerPrompt: "视频号开头换成去年讲返工的旧口播，不要客户声音",
      changeExplanation: "用历史返工口播替换视频号开头",
    });
    expect(
      (await library.listRevisions(firstRevision.storyId)).map(
        (item) => item.revision,
      ),
    ).toEqual([1, 2]);
    expect(next.variants.find((item) => item.kind === "wechat-channels")?.timeline[0].segmentId).toBe(
      "old_quote_01",
    );
    expect(next.variants.find((item) => item.kind === "xiaohongshu")).toEqual(
      firstRevision.variants.find((item) => item.kind === "xiaohongshu"),
    );
    await expect(
      library.putRevision({
        ...firstRevision,
        variants: firstRevision.variants.map((variant) => ({
          ...variant,
          title: "silently rewritten old title",
        })),
      }),
    ).rejects.toThrow("immutable-revision-conflict");
  });

  it("returns an explicit refusal instead of inventing a missing shot", async () => {
    const interpreterRequestingMissingSegment: PromptEditInterpreter = {
      interpret: async () => ({
        targetVariants: ["xiaohongshu"],
        replacementTimelines: {
          xiaohongshu: [timelineItem("never_filmed_award_scene")],
        },
        removePeople: [],
        explanation: "add award scene",
      }),
    };

    await expect(
      reviseWithPrompt(
        {
          revision: firstRevision,
          prompt: "加入从未拍过的获奖现场",
          allowedSegments,
          designerId: "designer_01",
        },
        interpreterRequestingMissingSegment,
        library,
      ),
    ).rejects.toThrow("requested-material-not-in-authorized-library");
  });

  it("refuses revision when the story lineage record is missing", async () => {
    const empty = await JsonContentStore.open(join(testDirectory, "empty.json"));
    await expect(reviseWithPrompt({
      revision: firstRevision,
      prompt: "只用旧素材",
      allowedSegments,
      designerId: "designer_01",
    }, interpreterSelecting("old_quote_01"), empty)).rejects.toThrow("story-lineage-required");
  });

  it("rejects a blocked segment even if its id exists", async () => {
    allowedSegments.push(
      makeSegment("visitor_blocked", { permissionState: "blocked" }),
    );

    await expect(
      reviseWithPrompt(
        {
          revision: firstRevision,
          prompt: "use visitor",
          allowedSegments,
          designerId: "designer_01",
        },
        interpreterSelecting("visitor_blocked"),
        library,
      ),
    ).rejects.toThrow("requested-material-not-in-authorized-library");
  });

  it("invalidates platform approvals when the editorial master changes", async () => {
    const assets = allowedSegments.map((segment): LibraryAsset => ({
      id: segment.assetId,
      batchId: segment.batchId,
      sessionId: `session_${segment.id}`,
      digest: `digest_${segment.id}`,
      localPath: join(testDirectory, `${segment.id}.mp4`),
      mediaKind: "video",
      importedAt: "2026-09-04T00:00:00.000Z",
      permissionState: "allowed",
      origin: segment.id.startsWith("old") ? "historical" : "new",
      technicalFlags: [],
    }));
    for (const asset of assets) await library.putAsset(asset);
    await library.putSegments(allowedSegments);
    await library.putRevision(firstRevision);
    const artifact: VariantArtifact = {
      id: "artifact_xhs",
      revisionId: firstRevision.id,
      kind: "xiaohongshu",
      videoPath: join(testDirectory, "xhs.mp4"),
      digest: "video",
      sourceSegmentIds: ["new_quote_01", "new_process_01"],
      coverPath: join(testDirectory, "xhs-cover.jpg"),
      coverDigest: "cover",
      stillPaths: [],
      stillDigests: [],
      visualSourceSegmentIds: ["new_process_01"],
      renderInputDigest: calculateRenderInputDigest(firstRevision, allowedSegments, assets),
      status: "active",
    };
    await library.putArtifact(artifact);
    await library.commitApproval({
      id: "approval_story_01_xhs_v1", storyId: "story_01",
      revisionId: firstRevision.id, revision: 1, artifactId: "artifact_xhs",
      artifactDigest: "video", coverDigest: "cover", stillDigests: [],
      renderInputDigest: artifact.renderInputDigest, variant: "xiaohongshu", reviewerId: "designer_01",
      approvedAt: "2026-09-04T00:00:00.000Z", status: "active",
    });
    const next: EditRevision = {
      ...firstRevision,
      id: "revision_story_01_2",
      revision: 2,
      parentRevisionId: firstRevision.id,
      variants: firstRevision.variants.map((variant) => variant.kind === "editorial-master"
        ? { ...variant, title: "changed master" }
        : variant),
    };
    await commitPromptRevision({
      previous: firstRevision, next, artifacts: [], designerId: "designer_01",
    }, library);
    expect((await library.listApprovals())[0].status).toBe("invalidated");
  });

  // Removing the lineage filter would disclose a permitted but unrelated transcript to the model.
  it("gives the interpreter only exact story sources before it can read transcripts", async () => {
    const outsider = makeSegment("unrelated_story", { tags: {
      ...makeSegment("unrelated_story").tags, transcript: "Private unrelated conversation",
    } });
    const next = await reviseWithPrompt({ revision: firstRevision, prompt: "Tighten the opening",
      allowedSegments: [...allowedSegments, outsider], designerId: "designer_01" }, {
      interpret: async ({ allowedSegments: visible }) => ({ targetVariants: ["wechat-channels"], replacementTimelines: {},
        removePeople: [], explanation: visible.map((segment) => segment.tags.transcript || segment.id).join(" | ") }),
    }, library);
    expect(next.changeExplanation).toBe("new_quote_01 | new_process_01 | old_quote_01");
  });

  // Platform labels/copy/role metadata cannot disguise the same ordered cuts.
  it("rejects a duplicate timeline proposal before persisting any revision", async () => {
    await expect(reviseWithPrompt({ revision: firstRevision, prompt: "Use one cut everywhere",
      allowedSegments, designerId: "designer_01" }, {
      interpret: async () => ({ targetVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"],
        replacementTimelines: { "editorial-master": firstRevision.variants[0].timeline,
          xiaohongshu: firstRevision.variants[0].timeline, douyin: firstRevision.variants[0].timeline,
          "wechat-channels": firstRevision.variants[0].timeline }, removePeople: [], explanation: "Three labels" }),
    }, library)).rejects.toThrow("variant-timelines-must-differ");
    expect(await library.listRevisions(firstRevision.storyId)).toEqual([]);
  });

  it("rejects a duplicate timeline at the direct domain commit boundary", async () => {
    const next = { ...firstRevision, id: "revision_story_01_2", revision: 2, parentRevisionId: firstRevision.id,
      variants: firstRevision.variants.map((variant) => ({ ...variant, timeline: firstRevision.variants[0].timeline })) };
    await expect(commitPromptRevision({ previous: firstRevision, next, artifacts: [], designerId: "designer_01" }, library))
      .rejects.toThrow("variant-timelines-must-differ");
    expect(await library.listRevisions(firstRevision.storyId)).toEqual([]);
  });
});

function interpreterSelecting(segmentId: string): PromptEditInterpreter {
  return {
    interpret: async () => ({
      targetVariants: ["wechat-channels"],
      replacementTimelines: {
        "wechat-channels": [timelineItem(segmentId), timelineItem("new_process_01")],
      },
      removePeople: ["customer_01"],
      explanation: "用历史返工口播替换视频号开头",
    }),
  };
}

function makeRevision(): EditRevision {
  const kinds: VideoVariant[] = [
    "editorial-master",
    "xiaohongshu",
    "douyin",
    "wechat-channels",
  ];
  return {
    id: "revision_story_01_1",
    storyId: "story_01",
    revision: 1,
    parentRevisionId: null,
    designerPrompt: null,
    changeExplanation: null,
    variants: kinds.map((kind, index) => ({
      kind,
      timeline: [timelineItem("new_quote_01"), { ...timelineItem("new_process_01"), outMs: 1_000 + index * 100 }],
      title: `${kind} title`,
      caption: `${kind} caption`,
      cover: { segmentId: "new_process_01", atMs: 500 },
    })),
    selectedStills: [
      { segmentId: "new_process_01", atMs: 500, reason: "clear" },
    ],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}

function timelineItem(segmentId: string): TimelineItem {
  return { segmentId, inMs: 0, outMs: 1_000, role: "anchor" };
}

function makeSegment(
  id: string,
  patch: Partial<ContentSegment> = {},
): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId: id.startsWith("old") ? "batch_history" : "batch_new",
    startMs: 0,
    endMs: 10_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["persona-copy"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
    ...patch,
  };
}
