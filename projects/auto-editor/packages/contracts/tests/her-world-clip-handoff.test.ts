import { describe, expect, it } from "vitest";

import {
  HerWorldClipHandoffV1Schema,
  HerWorldClipReceiptV1Schema,
  assertHerWorldClipHandoffFresh,
  assertHerWorldClipReceiptMatches,
} from "../src";

const validHandoff = {
  schemaVersion: 1 as const,
  kind: "her-world-story-render" as const,
  jobId: "a".repeat(32),
  operationKey: "story-render-a-3",
  createdAt: "2026-10-25T02:00:00.000Z",
  story: {
    id: "b".repeat(32), revision: 3, title: "在源头，看见生活", place: "斯里兰卡",
    visitedAt: "2026-10-01T00:00:00.000Z", ownerConfirmed: true as const,
    theme: "去源头看见好宝石背后的生活",
    themeMaterials: [
      { kind: "theme_intro" as const, title: "主题开场", content: "先看见生活，再理解一颗好宝石。", basis: "店主确认的主题" },
      { kind: "place_context" as const, title: "地点进入", content: "从斯里兰卡的街巷与人情进入故事。", basis: "真实地点与照片线索" },
      { kind: "missing_shot" as const, title: "还值得补一张", content: "补一张真实源头细节，不生成替代商品图。", basis: "只作为补拍提醒" },
    ],
    confirmedAt: "2026-10-25T01:58:00.000Z",
    acts: [{
      sequence: 1, stageId: "origin", mediaId: "media-1", storyFunction: "product-origin" as const,
      heading: "去源头", narration: "先看见人，再理解宝石。", sourceBasis: "店主本人确认的经历",
      productReference: { productId: "sapphire-1", role: "origin" as const },
    }],
  },
  media: [{
    id: "media-1", order: 0, kind: "image" as const, mimeType: "image/jpeg" as const,
    byteLength: 2048, sha256: "c".repeat(64),
    fetch: { url: "https://example.test/media/1", expiresAt: "2026-10-26T02:00:00.000Z", authorization: "bearer-job-token" as const },
    rights: { confirmed: true as const, scope: "render-this-story" as const, confirmedAt: "2026-10-25T01:58:00.000Z" },
  }],
  narration: { mode: "text-only" as const, audioMediaId: null },
  output: { aspectRatio: "9:16" as const, width: 720 as const, height: 1280 as const, durationSeconds: 45, profiles: ["editorial-master" as const], captions: true as const },
  publishClaimed: false as const,
};

describe("her-world clip handoff", () => {
  it("accepts a confirmed, traceable story and a review receipt", () => {
    const handoff = HerWorldClipHandoffV1Schema.parse(validHandoff);
    expect(handoff.story.themeMaterials?.[2]?.kind).toBe("missing_shot");
    const receipt = HerWorldClipReceiptV1Schema.parse({
      schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId,
      storyId: handoff.story.id, storyRevision: handoff.story.revision,
      status: "review-ready", progress: 100, ownerAccepted: false, published: false, error: null,
      outputs: [{ profile: "editorial-master", relativePath: "outputs/master.mp4", sha256: "d".repeat(64), durationSeconds: 44.8, width: 720, height: 1280,
        sourceMap: [{ mediaId: "media-1", timelineStartMs: 0, timelineEndMs: 44800 }] }],
      updatedAt: "2026-10-25T03:10:00.000Z",
    });
    expect(() => assertHerWorldClipReceiptMatches(handoff, receipt)).not.toThrow();
  });

  it("fails closed for missing consent, wrong media, expiry, and fake publishing", () => {
    expect(() => HerWorldClipHandoffV1Schema.parse({ ...validHandoff, publishClaimed: true })).toThrow();
    expect(() => HerWorldClipHandoffV1Schema.parse({ ...validHandoff, story: { ...validHandoff.story, ownerConfirmed: false } })).toThrow();
    expect(() => HerWorldClipHandoffV1Schema.parse({ ...validHandoff, story: { ...validHandoff.story, acts: [{ ...validHandoff.story.acts[0], mediaId: "missing" }] } })).toThrow("act-media-not-found");
    const parsed = HerWorldClipHandoffV1Schema.parse(validHandoff);
    expect(() => assertHerWorldClipHandoffFresh(parsed, new Date("2026-10-27T00:00:00.000Z"))).toThrow("media-fetch-expired");
  });

  it("rejects receipts that cannot be tied back to the exact story revision", () => {
    const handoff = HerWorldClipHandoffV1Schema.parse(validHandoff);
    const receipt = HerWorldClipReceiptV1Schema.parse({
      schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId,
      storyId: handoff.story.id, storyRevision: handoff.story.revision,
      status: "queued", progress: 0, outputs: [], ownerAccepted: false, published: false, error: null,
      updatedAt: "2026-10-25T02:00:01.000Z",
    });
    expect(() => assertHerWorldClipReceiptMatches(handoff, { ...receipt, jobId: "e".repeat(32) })).toThrow("binding-mismatch");
    expect(() => HerWorldClipReceiptV1Schema.parse({ ...receipt, published: true })).toThrow();
  });
});
