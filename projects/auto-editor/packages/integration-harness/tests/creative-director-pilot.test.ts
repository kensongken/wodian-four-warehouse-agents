import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { JsonContentStore } from "@wearable/mobile-agent";

import { runCreativeDirectorPilot } from "../src/creative-director-pilot";

describe("three-terminal creative director A-0 pilot", () => {
  let tempDir: string;

  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), "creative-director-pilot-"));
  });

  afterEach(async () => {
    await rm(tempDir, { recursive: true, force: true });
  });

  it("proves consent, script, simulated field, editor, delivery, and withdrawal lineage", async () => {
    const report = await runCreativeDirectorPilot(tempDir);

    expect(Object.keys(report).sort()).toEqual([
      "approvedVariant",
      "creativeSessionId",
      "dispatch",
      "duplicateEventCount",
      "fieldActions",
      "historicalConsent",
      "publishedClaimed",
      "returnedAssetCount",
      "script",
      "staleTaskRejected",
      "storyTypes",
      "variantKinds",
    ]);
    expect(Object.keys(report.dispatch).sort()).toEqual([
      "preConfirmationReceiptCount",
      "receiptCount",
      "transport",
    ]);
    expect(Object.keys(report.fieldActions).sort()).toEqual([
      "completed",
      "forcedReshootCount",
      "notes",
      "retake",
      "skipped",
    ]);

    expect(report.historicalConsent).toEqual({
      declinedHistoryCount: 0,
      viewedButUnselectedCount: 0,
      exactSelectedIds: ["history_rework_01"],
    });
    expect(report.script).toEqual({
      versions: [1, 2],
      confirmedVersion: 2,
      unselectedReferenceCount: 0,
    });
    expect(report.dispatch).toEqual({
      transport: "simulated",
      receiptCount: 4,
      preConfirmationReceiptCount: 0,
    });
    expect(report.fieldActions).toEqual({
      completed: 1,
      retake: 1,
      skipped: 1,
      notes: 1,
      forcedReshootCount: 0,
    });
    expect(report.staleTaskRejected).toBe(true);
    expect(report.duplicateEventCount).toBe(0);
    expect(report.returnedAssetCount).toBe(1);
    expect(report.storyTypes).toEqual([
      "inner-journey",
      "persona-monologue",
    ]);
    expect(report.variantKinds).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(report.approvedVariant).toEqual({
      variant: "wechat-channels",
      revision: 2,
    });
    expect(report.publishedClaimed).toBe(false);

    const library = await JsonContentStore.open(join(tempDir, "content-library.json"));
    const currentTask = (await library.listCreativeProcessingTasks()).find((task) =>
      task.creativeSessionId === report.creativeSessionId && !task.supersededByTaskId &&
      task.status === "reviewable");
    expect(currentTask).toMatchObject({
      status: "reviewable",
      phase: "complete",
      publishedClaimed: false,
    });
    const currentBundle = await library.readCreativeEditorialBundle(currentTask!.id);
    expect(currentBundle.artifacts).toHaveLength(currentBundle.revisions.length * 4);
    const captureAssets = (await library.listAssets()).filter((asset) =>
      asset.captureContext?.creativeSessionId === report.creativeSessionId);
    expect(captureAssets.map((asset) => ({
      stepId: asset.captureContext?.stepId,
      deviceRole: asset.captureContext?.deviceRole,
      take: asset.captureContext?.take,
      outcome: asset.captureContext?.stepOutcome,
    })).sort((left, right) => left.take! - right.take!)).toEqual([
      { stepId: expect.any(String), deviceRole: "main-stone-a", take: 2, outcome: "completed" },
    ]);
    expect(new Set(captureAssets.map((asset) => asset.id)).size).toBe(1);
    const takeEvents = (await library.listCreativeSessionEvents()).filter((event) =>
      event.sessionId === report.creativeSessionId && ["retake", "completed"].includes(event.action));
    expect(takeEvents.map((event) => ({
      stepId: event.stepId,
      deviceRole: event.deviceRole,
      action: event.action,
      take: event.take,
    }))).toEqual([
      { stepId: expect.any(String), deviceRole: "main-stone-a", action: "retake", take: 1 },
      { stepId: expect.any(String), deviceRole: "main-stone-a", action: "completed", take: 2 },
    ]);
    expect(new Set(captureAssets.map((asset) => asset.captureContext?.stepId))).toEqual(
      new Set(takeEvents.filter((event) => event.action === "completed").map((event) => event.stepId)),
    );
    expect(await library.listDeliveries()).toEqual([
      expect.objectContaining({ status: "received", publishedClaimed: false }),
    ]);
  }, 120_000);
});
