import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { runSyntheticEditorialPilot } from "../src/editorial-pilot";

describe("synthetic editorial pilot", () => {
  let tempDir: string;

  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), "editorial-pilot-"));
  });

  afterEach(async () => {
    await rm(tempDir, { recursive: true, force: true });
  });

  it("proves C-primary/B-secondary through exact approval and receipt", async () => {
    const report = await runSyntheticEditorialPilot(tempDir);

    expect(report.newBatchStoryCounts).toEqual({
      "inner-journey": 1,
      "product-journey": 1,
      "persona-monologue": 1,
    });
    expect(report.everyStoryHasNewAnchor).toBe(true);
    expect(report.historicalMatchesHaveReasons).toBe(true);
    expect(report.variantKinds).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(report.promptRevision).toEqual({
      from: 1,
      to: 2,
      oldRevisionPreserved: true,
    });
    expect(report.approvedVariant).toEqual({
      revision: 2,
      variant: "wechat-channels",
    });
    expect(report.delivery).toMatchObject({
      status: "received",
      publishedClaimed: false,
    });
    expect(report.renderedArtifactPaths).toHaveLength(4);
    expect(report.renderedStillPaths).toHaveLength(1);
    expect(report.duplicateAssetCount).toBe(1);
    expect(report.damagedAssetPreserved).toBe(true);
    expect(report.reshootRequestCount).toBe(0);
    expect(report.unauthorizedPublicSegmentCount).toBe(0);
  }, 120_000);
});
