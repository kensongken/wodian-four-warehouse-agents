import { createHash } from "node:crypto";
import { access, mkdir, mkdtemp, readFile, rm, stat, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  defaultStudioModelBrowserExecutable,
  countStudioOriginalMutations,
  runStudioModelBrowserPilot,
  snapshotStudioOriginals,
} from "../src/studio-model-browser-pilot";

const cleanup: string[] = [];

describe("studio model real Chromium pilot", () => {
  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("captures the full fixture-backed human review loop without claiming real-provider acceptance", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-model-browser-pilot-"));
    cleanup.push(root);
    const executablePath = defaultStudioModelBrowserExecutable();
    await access(executablePath);

    const report = await runStudioModelBrowserPilot(root, { executablePath });

    expect(report).toMatchObject({
      schemaVersion: 1,
      browserEngine: "chromium",
      providerEvidence: "fixture/synthetic",
      syntheticOnly: true,
      modelMode: "offline-fallback",
      modelDegraded: true,
      postAnalysisMatchVerdict: "ready",
      fixtureMechanicalPass: true,
      requestedLayers: ["technical", "scene", "transcript", "embedding", "vision"],
      analysisLayerStatuses: {
        technical: "succeeded",
        scene: "succeeded",
        transcript: "succeeded",
        embedding: "succeeded",
        vision: "succeeded",
      },
      correctedSuggestionCount: 1,
      duplicateKeepCount: 1,
      materialSuitabilityCounts: { suitable: 1, "not-suitable": 1 },
      customerSatisfaction: ["partially-satisfied", "satisfied"],
      revisionProposalCount: 1,
      draftRequestCountBeforeBusinessMatch: 0,
      networkWorkflowOrder: [
        "analysis", "analysis", "correct-suggestion", "keep-duplicate",
        "suitable", "not-suitable", "business-match", "brief-v1", "drafts-v1",
        "partial-feedback-v1", "revision-proposal", "confirm-revision", "drafts-v2",
        "satisfied-feedback-v2",
      ],
      falseConfirmedFactCount: 0,
      crossCustomerExposureCount: 0,
      originalSnapshotCount: 4,
      originalMutationCount: 0,
      deleteOriginalButtonCount: 0,
      uploadButtonCount: 0,
      publishButtonCount: 0,
      deleteRequestCount: 0,
      uploadRequestCount: 0,
      publishRequestCount: 0,
      externalNetworkRequestCount: 0,
      modelNetworkRequestCount: 0,
      realProviderAcceptance: {
        status: "blocked",
        reason: "approved-local-browser-evidence-not-provided",
      },
    });
    expect(new URL(report.url).port).not.toBe("4287");
    expect(report.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/studio$/u);
    expect(report.demandGoal).toBe("用合成素材讲清楚设计灵感");
    expect(report.reviewedReceiptBindings.every((binding) =>
      /^[a-f0-9]{64}$/u.test(binding.sourceDigest) && binding.sourceEndMs > binding.sourceStartMs,
    )).toBe(true);
    expect(report.lineage).toMatchObject({
      briefVersions: [1, 2],
      initialBriefParentId: null,
      revisedBriefParentId: report.lineage.initialBriefId,
      revisedBriefRevisionProposalId: report.lineage.revisionProposalId,
      partialFeedbackDraftVersion: 1,
      satisfiedFeedbackDraftVersion: 2,
    });
    expect(report.lineage.initialDraftBriefIds).toEqual([report.lineage.initialBriefId]);
    expect(report.lineage.revisedDraftBriefIds).toEqual([report.lineage.revisedBriefId]);
    for (const path of [
      report.tracePath,
      report.domSnapshotPath,
      report.screenshotPath,
      report.networkLogPath,
      report.reportPath,
    ]) expect((await stat(path)).size).toBeGreaterThan(0);
    const dom = await readFile(report.domSnapshotPath, "utf8");
    expect(dom).toContain("告诉我这次想讲什么");
    expect(dom).toContain("用合成素材讲清楚设计灵感");
    expect(dom).toContain("人工复核历史");
    expect(dom).toContain("素材对当前需求的人工结论");
    expect(dom).toContain("客户反馈");
    expect(dom).toContain("部分满意 · 请让设计师观点更突出");
    expect(dom).toContain("满意 · 无补充说明");
    expect(dom).not.toMatch(/data-(?:delete-original|upload-provider|publish)/iu);
  }, 180_000);

  it.each(["modify", "delete"] as const)("counts an original.bin %s by following asset and blob records", async (mode) => {
    const root = await mkdtemp(join(tmpdir(), "studio-model-original-snapshot-"));
    cleanup.push(root);
    const originalPath = await writeOriginalFixture(root);
    const before = await snapshotStudioOriginals(root);

    if (mode === "modify") await writeFile(originalPath, "changed-original");
    else await unlink(originalPath);
    const after = await snapshotStudioOriginals(root);

    expect(before.size).toBe(1);
    expect(countStudioOriginalMutations(before, after)).toBe(1);
  });
});

async function writeOriginalFixture(root: string): Promise<string> {
  const bytes = Buffer.from("fixed-original");
  const digest = createHash("sha256").update(bytes).digest("hex");
  const customerRoot = join(root, "library", "customers", "customer-hash");
  const assetRoot = join(customerRoot, "assets", "asset-test");
  const blobId = `blob_${digest}`;
  const blobRoot = join(customerRoot, "blobs", blobId);
  await mkdir(assetRoot, { recursive: true });
  await mkdir(blobRoot, { recursive: true });
  await writeFile(join(assetRoot, "asset.json"), JSON.stringify({
    id: "asset-test", blobId, digest, byteLength: bytes.byteLength,
  }));
  await writeFile(join(blobRoot, "blob.json"), JSON.stringify({
    id: blobId, digest, byteLength: bytes.byteLength,
  }));
  const originalPath = join(blobRoot, "original.bin");
  await writeFile(originalPath, bytes);
  return originalPath;
}
