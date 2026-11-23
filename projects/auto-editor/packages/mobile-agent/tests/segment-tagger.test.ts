import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { SegmentTags, UntypedSegment } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { JsonContentStore } from "../src/json-content-store";
import {
  confirmSegmentTags,
  tagSegments,
  type SegmentTagAnalyzer,
} from "../src/segment-tagger";

describe("segment tagger", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "segment-tagger-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("keeps multiple factual types and marks product identity as suggested", async () => {
    const tags = makeTags({
      baseTypes: ["handmade", "persona-copy"],
      people: ["designer_01"],
      products: [{ id: "prod_01", status: "suggested" }],
      processStages: ["rework"],
      topics: ["为什么返工"],
      emotions: ["persistence"],
      transcript: "我还想再做一次",
    });

    const tagged = await tagSegments(
      [makeSegment()],
      analyzerReturning(tags),
      library,
    );

    expect(tagged[0].tags.baseTypes).toEqual(["handmade", "persona-copy"]);
    expect(tagged[0].tags.products[0].status).toBe("suggested");
    expect(await library.listSegments()).toEqual(tagged);
  });

  it("does not silently promote uncertain people or permissions", async () => {
    const visitorSegment = makeSegment({ permissionState: "review-required" });
    const tagged = await tagSegments(
      [visitorSegment],
      analyzerReturning(
        makeTags({ technicalFlags: ["identity-review-required"] }),
      ),
      library,
    );

    expect(tagged[0].permissionState).toBe("review-required");
    expect(tagged[0].tags.technicalFlags).toContain(
      "identity-review-required",
    );
  });

  it("downgrades an AI-claimed confirmed product until a designer confirms it", async () => {
    const tagged = await tagSegments(
      [makeSegment()],
      analyzerReturning(makeTags({ products: [{ id: "prod_ai_guess", status: "confirmed" }] })),
      library,
    );
    expect(tagged[0].tags.products).toEqual([{ id: "prod_ai_guess", status: "suggested" }]);
  });

  it("records designer corrections without erasing the prior state", async () => {
    await tagSegments(
      [makeSegment()],
      analyzerReturning(makeTags()),
      library,
    );

    const corrected = await confirmSegmentTags(
      "segment_01",
      {
        people: ["designer_confirmed"],
        products: [{ id: "prod_01", status: "confirmed" }],
      },
      "designer_01",
      library,
    );

    expect(corrected.tags.people).toEqual(["designer_confirmed"]);
    expect(corrected.tags.products[0].status).toBe("confirmed");
    expect(await library.listAuditEvents("segment_01")).toEqual([
      expect.objectContaining({
        actorId: "designer_01",
        action: "segment-tags-confirmed",
        before: expect.objectContaining({ people: [] }),
        after: expect.objectContaining({ people: ["designer_confirmed"] }),
      }),
    ]);
  });
});

function analyzerReturning(tags: SegmentTags): SegmentTagAnalyzer {
  return { suggest: async () => tags };
}

function makeSegment(
  patch: Partial<UntypedSegment> = {},
): UntypedSegment {
  return {
    id: "segment_01",
    assetId: "asset_01",
    batchId: "batch_01",
    startMs: 0,
    endMs: 10_000,
    permissionState: "allowed",
    transcript: "我还想再做一次",
    technicalFlags: [],
    ...patch,
  };
}

function makeTags(patch: Partial<SegmentTags> = {}): SegmentTags {
  return {
    baseTypes: ["persona-copy"],
    people: [],
    products: [],
    processStages: [],
    topics: [],
    emotions: [],
    transcript: "我还想再做一次",
    technicalFlags: [],
    usage: {
      storyIds: [],
      variants: [],
      useCount: 0,
      lastUsedAt: null,
    },
    ...patch,
  };
}
