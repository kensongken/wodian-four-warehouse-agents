import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { Approval, CreativeSession, CreativeSessionEvent, DeviceBrief, DirectorCard, ShotPlan } from "@wearable/contracts";
import { CreativeProcessingTaskSchema } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ffmpegPath from "ffmpeg-static";
import * as handoff from "../src/creative-handoff";
import { renderRevision, confirmPostShootMaterialSelection, decidePostShootMaterialViewing,
  suggestPostShootHistoricalMaterials, segmentNewBatch, tagSegments, confirmSegmentTags,
  approveVariant, createExportPackage, deliverExportPackage,
  type EditPlanAnalyzer, type SegmentTagAnalyzer, type StoryAnalyzer } from "../src";

import {
  acceptCreativeCapture,
  LocalCreativeOriginalStore,
  JsonContentStore,
  resumeCreativePrivacy,
  settleCreativeCapture,
} from "../src";

const now = "2026-09-07T08:00:00.000Z";
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACXBIWXMAAAABAAAAAQBPJcTWAAAAC0lEQVR4nGNgQAYAAA4AAamRc7EAAAAASUVORK5CYII=",
  "base64",
);
const SECOND_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACXBIWXMAAAABAAAAAQBPJcTWAAAADklEQVR4nGP4CwYMEAoAUxIL3XOZJD8AAAAASUVORK5CYII=",
  "base64",
);

describe("creative handoff", () => {
  let directory: string;

  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), "wearable-creative-handoff-")); });
  afterEach(async () => {
    if (process.env.TASK2_KEEP_FIXTURES === "1") {
      process.stdout.write(`TASK2_REVIEWABLE_DIRECTORY=${directory}\n`);
    } else await rm(directory, { recursive: true, force: true });
  });

  it("restores every bound privacy device before making the paused session active", async () => {
    const library = await pausedPrivacyLibrary(directory);
    const devices = [
      privacyDevice("watch_sim_01"),
      privacyDevice("stone_a_sim_01"),
      privacyDevice("stone_b_sim_01"),
    ];

    const resumed = await resumeCreativePrivacy({
      creativeSessionId: "creative_privacy_01", actorId: "customer_01",
      actorRole: "customer", confirmSafe: true, operationKey: "resume-all-01",
      now: "2026-09-07T08:01:00.000Z",
    }, { library, devices });

    expect(resumed).toMatchObject({ stage: "shooting", privacyMode: "creator" });
    expect(devices.every((device) => !device.isPrivacyPaused())).toBe(true);
    expect(await library.listAuditEvents(resumed.id)).toContainEqual(expect.objectContaining({
      action: "creative-session-transitioned",
      after: expect.objectContaining({
        stage: "shooting",
        resumedDeviceIds: ["watch_sim_01", "stone_a_sim_01", "stone_b_sim_01"],
      }),
    }));
  });

  it("re-pauses restored devices and leaves the session paused when one bound device fails", async () => {
    const library = await pausedPrivacyLibrary(directory);
    const first = privacyDevice("watch_sim_01");
    const failed = privacyDevice("stone_a_sim_01", true);
    const untouched = privacyDevice("stone_b_sim_01");

    await expect(resumeCreativePrivacy({
      creativeSessionId: "creative_privacy_01", actorId: "customer_01",
      actorRole: "customer", confirmSafe: true, operationKey: "resume-fails-01",
      now: "2026-09-07T08:01:00.000Z",
    }, { library, devices: [first, failed, untouched] }))
      .rejects.toThrow("creative-privacy-device-resume-failed");

    expect(first.isPrivacyPaused()).toBe(true);
    expect(failed.isPrivacyPaused()).toBe(true);
    expect(untouched.isPrivacyPaused()).toBe(true);
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "paused", privacyMode: "paused",
    });
    expect(await library.listAuditEvents("creative_privacy_01")).toContainEqual(expect.objectContaining({
      action: "creative-privacy-resume-failed",
      after: expect.objectContaining({ stage: "paused", failedDeviceIds: ["stone_a_sim_01"] }),
    }));
  });

  it("rejects privacy resume before touching devices without customer safety confirmation", async () => {
    const library = await pausedPrivacyLibrary(directory);
    const device = privacyDevice("watch_sim_01");
    let resumeCalls = 0;
    const tracked = { ...device, resumePrivacy() { resumeCalls += 1; device.resumePrivacy(); } };
    const base = {
      creativeSessionId: "creative_privacy_01", actorId: "customer_01",
      operationKey: "resume-rejected-01", now: "2026-09-07T08:01:00.000Z",
    };

    await expect(resumeCreativePrivacy({
      ...base, actorRole: "system", confirmSafe: true,
    } as never, { library, devices: [tracked] }))
      .rejects.toThrow("privacy-resume-customer-required");
    await expect(resumeCreativePrivacy({
      ...base, actorRole: "customer", confirmSafe: false,
    } as never, { library, devices: [tracked] }))
      .rejects.toThrow("privacy-resume-safety-confirmation-required");
    await expect(resumeCreativePrivacy({
      ...base, actorId: "designer_01", actorRole: "customer", confirmSafe: true,
    }, { library, devices: [tracked] }))
      .rejects.toThrow("privacy-resume-customer-mismatch");
    expect(resumeCalls).toBe(0);
    expect((await library.listCreativeSessions())[0]?.stage).toBe("paused");
  });

  it("attempts every compensation and records any device that cannot re-pause after commit failure", async () => {
    const library = await pausedPrivacyLibrary(directory);
    let transitionAttempted = false;
    const failingLibrary = new Proxy(library, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (bundle: Parameters<typeof library.commitCreativeBundle>[0]) => {
          if (!transitionAttempted && bundle.session.stage === "shooting") {
            transitionAttempted = true;
            throw new Error("stale-content-store");
          }
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const pauseCalls: string[] = [];
    const device = (deviceId: string, failPause = false) => {
      let paused = true;
      return {
        deviceId,
        pausePrivacy() {
          pauseCalls.push(deviceId);
          if (failPause) throw new Error(`pause-failed:${deviceId}`);
          paused = true;
        },
        resumePrivacy() { paused = false; },
        isPrivacyPaused() { return paused; },
      };
    };
    const devices = [device("watch_sim_01", true), device("stone_a_sim_01"), device("stone_b_sim_01")];

    await expect(resumeCreativePrivacy({
      creativeSessionId: "creative_privacy_01", actorId: "customer_01",
      actorRole: "customer", confirmSafe: true, operationKey: "resume-commit-fails-01",
      now: "2026-09-07T08:01:00.000Z",
    }, { library: failingLibrary, devices }))
      .rejects.toThrow("creative-privacy-compensation-failed");

    expect(pauseCalls).toEqual(["watch_sim_01", "stone_a_sim_01", "stone_b_sim_01"]);
    expect(devices[1]!.isPrivacyPaused()).toBe(true);
    expect(devices[2]!.isPrivacyPaused()).toBe(true);
    expect((await library.listCreativeSessions())[0]?.stage).toBe("paused");
    expect(await library.listAuditEvents("creative_privacy_01")).toContainEqual(expect.objectContaining({
      action: "creative-privacy-resume-compensation-failed",
      after: expect.objectContaining({ failedDeviceIds: ["watch_sim_01"] }),
    }));
  });

  it("serializes different resume keys so a loser cannot re-pause devices after a winner commits", async () => {
    const library = await pausedPrivacyLibrary(directory);
    const entered = deferred<void>();
    const release = deferred<void>();
    let held = false;
    const gatedLibrary = new Proxy(library, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (bundle: Parameters<typeof library.commitCreativeBundle>[0]) => {
          if (!held && bundle.session.stage === "shooting") {
            held = true;
            entered.resolve();
            await release.promise;
          }
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    let failNextResume = false;
    const devices = ["watch_sim_01", "stone_a_sim_01", "stone_b_sim_01"].map((deviceId) => {
      let paused = true;
      return {
        deviceId,
        pausePrivacy() { paused = true; },
        resumePrivacy() {
          if (deviceId === "stone_a_sim_01" && failNextResume) {
            failNextResume = false;
            throw new Error("second-resume-failed");
          }
          paused = false;
        },
        isPrivacyPaused() { return paused; },
      };
    });
    const input = {
      creativeSessionId: "creative_privacy_01", actorId: "customer_01",
      actorRole: "customer" as const, confirmSafe: true as const,
      now: "2026-09-07T08:01:00.000Z",
    };
    const first = resumeCreativePrivacy({ ...input, operationKey: "resume-concurrent-01" }, {
      library: gatedLibrary, devices,
    });
    await entered.promise;
    failNextResume = true;
    const second = resumeCreativePrivacy({ ...input, operationKey: "resume-concurrent-02" }, {
      library: gatedLibrary, devices,
    });
    await Promise.resolve();
    release.resolve();
    const results = await Promise.allSettled([first, second]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
    expect((await library.listCreativeSessions())[0]).toMatchObject({ stage: "shooting", privacyMode: "creator" });
    expect(devices.every((device) => !device.isPrivacyPaused())).toBe(true);
  });

  it.each(["putCreativeProcessingTask", "commitCreativeBundle", "commitCreativeCaptureBundle"] as const)(
    "rejects fabricated reviewable state through public %s", async (boundary) => {
      const library = await editorialLibrary(directory, ["step_01"]);
      const session = (await library.listCreativeSessions())[0]!;
      const story = { id: "forged_story", batchId: session.editorialBatchId!, type: "persona-monologue" as const,
        anchorSegmentIds: ["forged_segment"], historicalSegmentIds: [], historicalReasons: {}, status: "candidate" as const };
      const variants = (["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const).map((kind, index) => ({
        kind, timeline: [{ segmentId: "forged_segment", inMs: 0, outMs: 100 + index, role: "anchor" as const }],
        title: "forged", caption: "forged", cover: { segmentId: "forged_segment", atMs: 0 },
      }));
      const revision = { id: "forged_revision", storyId: story.id, revision: 1, parentRevisionId: null,
        designerPrompt: null, changeExplanation: null, variants, selectedStills: [], createdAt: now };
      const processingTask = CreativeProcessingTaskSchema.parse({
        id: "forged_reviewable", creativeSessionId: session.id, editorialBatchId: session.editorialBatchId,
        status: "reviewable", phase: "complete", publishedClaimed: false, errorCode: null, updatedAt: now,
        operationKey: "forged-op", inputDigest: "forged-digest", checkpoint: {
          authorization: { selectedSegmentIds: [] }, assetIds: ["forged_asset"], segmentIds: ["forged_segment"],
          stories: [story], revisions: [revision], artifacts: variants.map(({ kind }) => ({
            id: `forged_${kind}`, revisionId: revision.id, kind, videoPath: `/missing/${kind}.mp4`, digest: "fake",
            sourceSegmentIds: ["forged_segment"], coverPath: "/missing/cover.jpg", coverDigest: "fake",
            stillPaths: [], stillDigests: [], visualSourceSegmentIds: ["forged_segment"], renderInputDigest: "fake", status: "active",
          })),
        },
      });
      const before = await readFile(join(directory, "library.json"));
      const result = boundary === "putCreativeProcessingTask" ? library.putCreativeProcessingTask(processingTask)
        : boundary === "commitCreativeBundle" ? library.commitCreativeBundle({ session, processingTask, auditEvents: [] })
          : library.commitCreativeCaptureBundle({ session, expectedSession: sessionPrecondition(session),
            processingTask, assets: [], events: [], auditEvents: [] });
      await expect(result).rejects.toThrow("creative-editorial-bundle-required");
      expect(await readFile(join(directory, "library.json"))).toEqual(before);
      expect(await library.listStories()).toEqual([]);
      expect(await library.listAllArtifacts()).toEqual([]);
    });

  it("rejects the formerly completed take when retake is recorded during rendering", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    let retaken = false;
    const result = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory), renderer: async (...args) => {
        const rendered = await renderRevision(...args);
        if (!retaken) {
          retaken = true;
          await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 1);
        }
        return rendered;
      },
    });
    expect(result.task).toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-steps-not-settled" });
    expect(result.stories).toEqual([]);
    expect(await library.listStories()).toEqual([]);
    expect(await library.listAllArtifacts()).toEqual([]);
    expect((await library.listCreativeProcessingTasks()).filter((task) => task.status === "reviewable")).toEqual([]);
  }, 60_000);

  it("does not return a stale completed replay when another handle changes consent after artifact reads", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const first = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(first.task.status).toBe("reviewable");
    const listArtifacts = library.listAllArtifacts.bind(library);
    vi.spyOn(library, "listAllArtifacts").mockImplementationOnce(async () => {
      const artifacts = await listArtifacts();
      const writer = await JsonContentStore.open(join(directory, "library.json"));
      const suggestionSet = { id: "new_set_during_replay", sessionId: "creative_01", version: 1,
        purpose: "post-shoot-edit" as const, suggestions: [], createdAt: now };
      await writer.putMaterialSuggestionSet(suggestionSet, {
        suggestionSetId: suggestionSet.id, purpose: "post-shoot-edit", sources: [],
      });
      return artifacts;
    });
    const replay = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory))
      .then((result) => result.task.status, (error: Error) => error.message);
    expect(replay).not.toBe("reviewable");
    expect(replay).toBe("stale-content-store");
  }, 60_000);

  // Catches approval splitting its authority check, approval write and source-usage
  // write so a real second-handle withdrawal can be overwritten by a stale segment.
  it("never reverses a concurrent privacy withdrawal while atomically approving current media", async () => {
    const seed = await editorialLibrary(directory, ["step_01"]);
    const processed = await handoff.processCreativeEditorial(
      editorialInput,
      editorialDependencies(seed, directory),
    );
    expect(processed.task).toMatchObject({ status: "reviewable", publishedClaimed: false });
    const revision = processed.revisions[0]!;
    const artifact = processed.artifacts.find((item) =>
      item.revisionId === revision.id && item.kind === "editorial-master")!;
    const segmentId = artifact.sourceSegmentIds[0]!;
    const storePath = join(directory, "library.json");
    const approvingHandle = await JsonContentStore.open(storePath);
    const withdrawingHandle = await JsonContentStore.open(storePath);
    let withdrawalCommitted = false;
    const scheduledApproval = new Proxy(approvingHandle, {
      get(target, property) {
        if (property === "putApproval" || property === "commitApproval") {
          return async (...args: unknown[]) => {
            if (!withdrawalCommitted) {
              withdrawalCommitted = true;
              await withdrawingHandle.commitPrivacyInvalidation({
                segmentId,
                reason: "customer-withdrew-during-approval",
                actorId: "customer_01",
              });
            }
            const operation = Reflect.get(target, property, target) as (...values: unknown[]) => Promise<unknown>;
            return operation.apply(target, args);
          };
        }
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });

    const approvalAttempt = await approveVariant({
      storyId: revision.storyId,
      revisionId: revision.id,
      revision: revision.revision,
      artifactId: artifact.id,
      variant: artifact.kind,
      reviewerId: "designer_01",
    }, scheduledApproval).then(
      (approval) => ({ approval }),
      (error: Error) => ({ error: error.message }),
    );

    expect.soft(withdrawalCommitted).toBe(true);
    expect.soft(approvalAttempt).toEqual({ error: "exact-active-artifact-required" });
    const stored = await JsonContentStore.open(storePath);
    expect.soft((await stored.listSegments()).find((item) => item.id === segmentId)?.permissionState)
      .toBe("withdrawn");
    expect.soft((await stored.listAllArtifacts()).find((item) => item.id === artifact.id)?.status)
      .toBe("invalidated");
    expect.soft((await stored.listApprovals()).filter((item) => item.status === "active"))
      .toEqual([]);
    const approvalId = `approval_${revision.storyId}_master_v${revision.revision}`;
    await expect(createExportPackage({ approvalId, artifactId: artifact.id,
      revisionId: revision.id, creativeSessionId: "creative_01" }, stored))
      .rejects.toThrow("active-approval-required");
    let delivered = false;
    await expect(deliverExportPackage(`export_${approvalId}`, {
      deliver: async () => {
        delivered = true;
        return { receiptId: "must-not-deliver", receivedAt: now };
      },
    }, stored, "creative_01")).rejects.toThrow(`export-package-not-found:export_${approvalId}`);
    expect.soft(delivered).toBe(false);
    expect.soft(await stored.listExportPackages()).toEqual([]);
    expect.soft(await stored.listDeliveries()).toEqual([]);
  }, 120_000);

  // Catches either legacy writer bypassing commitApproval: the old approval
  // entrypoint must not activate invalid media, and a stale whole segment must
  // not undo a real withdrawal after that rejected attempt refreshes its handle.
  it("rejects the legacy active-approval writer and stale segment resurrection after withdrawal", async () => {
    const seed = await editorialLibrary(directory, ["step_01"]);
    const processed = await handoff.processCreativeEditorial(
      editorialInput,
      editorialDependencies(seed, directory),
    );
    const revision = processed.revisions[0]!;
    const artifact = processed.artifacts.find((item) =>
      item.revisionId === revision.id && item.kind === "editorial-master")!;
    const segmentId = artifact.sourceSegmentIds[0]!;
    const storePath = join(directory, "library.json");
    const legacyWriter = await JsonContentStore.open(storePath);
    const withdrawingHandle = await JsonContentStore.open(storePath);
    const staleSegment = (await legacyWriter.listSegments()).find((item) => item.id === segmentId)!;
    const approval: Approval = {
      id: `approval_${revision.storyId}_master_v${revision.revision}`,
      storyId: revision.storyId,
      revisionId: revision.id,
      revision: revision.revision,
      artifactId: artifact.id,
      artifactDigest: artifact.digest,
      coverDigest: artifact.coverDigest,
      stillDigests: artifact.stillDigests,
      renderInputDigest: artifact.renderInputDigest,
      variant: artifact.kind,
      reviewerId: "designer_01",
      approvedAt: "2026-09-08T09:10:00.000Z",
      status: "active",
    };

    await withdrawingHandle.commitPrivacyInvalidation({
      segmentId,
      reason: "customer-withdrew-before-legacy-approval",
      actorId: "customer_01",
    });
    const approvalAttempt = await (legacyWriter as unknown as {
      putApproval(value: Approval): Promise<void>;
    }).putApproval(approval).then(
      () => "accepted",
      (error: Error) => error.message,
    );
    const staleSegmentAttempt = await legacyWriter.putSegments([{
      ...staleSegment,
      tags: {
        ...staleSegment.tags,
        usage: {
          storyIds: [revision.storyId],
          variants: [artifact.kind],
          useCount: staleSegment.tags.usage.useCount + 1,
          lastUsedAt: approval.approvedAt,
        },
      },
    }]).then(
      () => "accepted",
      (error: Error) => error.message,
    );

    expect.soft(approvalAttempt).toBe("atomic-approval-commit-required");
    expect.soft(staleSegmentAttempt).toBe("withdrawn-segment-cannot-be-reactivated");
    const stored = await JsonContentStore.open(storePath);
    expect.soft((await stored.listSegments()).find((item) => item.id === segmentId)?.permissionState)
      .toBe("withdrawn");
    expect.soft((await stored.listAllArtifacts()).find((item) => item.id === artifact.id)?.status)
      .toBe("invalidated");
    expect.soft((await stored.listApprovals()).filter((item) => item.status === "active"))
      .toEqual([]);
    expect.soft((await stored.listAuditEvents(approval.id)).filter((event) =>
      event.action === "variant-approved")).toEqual([]);
  }, 120_000);

  it("rejects active approval creation through the prompt-revision bundle writer", async () => {
    const library = await JsonContentStore.open(join(directory, "library.json"));
    const activeApproval: Approval = {
      id: "approval_bundle_bypass_01",
      storyId: "story_bundle_bypass_01",
      revisionId: "revision_bundle_bypass_01",
      revision: 1,
      artifactId: "artifact_bundle_bypass_01",
      artifactDigest: "video-digest",
      coverDigest: "cover-digest",
      stillDigests: [],
      renderInputDigest: "render-input-digest",
      variant: "xiaohongshu",
      reviewerId: "designer_01",
      approvedAt: "2026-09-08T09:11:00.000Z",
      status: "active",
    };

    await expect(library.commitRevisionBundle({
      revisions: [],
      artifacts: [],
      approvals: [activeApproval as never],
      exportPackages: [],
      auditEvent: {
        id: "audit_bundle_bypass_01",
        entityType: "revision",
        entityId: activeApproval.revisionId,
        actorId: "designer_01",
        action: "designer-prompt-revision-created",
        reason: "must not smuggle an active approval",
        before: null,
        after: null,
        createdAt: "2026-09-08T09:11:00.000Z",
      },
    })).rejects.toThrow("atomic-approval-commit-required");
    expect(await library.listApprovals()).toEqual([]);
  });

  it("persists a failed replay when a same-handle retake arrives after artifact reads", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const first = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(first.task.status).toBe("reviewable");
    const listArtifacts = library.listAllArtifacts.bind(library);
    vi.spyOn(library, "listAllArtifacts").mockImplementationOnce(async () => {
      const artifacts = await listArtifacts();
      await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      return artifacts;
    });
    const replay = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory))
      .then((result) => result.task.status, (error: Error) => error.message);
    expect(replay).toBe("failed-manual-review");
    const stored = await JsonContentStore.open(join(directory, "library.json"));
    expect((await stored.listCreativeProcessingTasks()).find((task) => task.id === first.task.id))
      .toMatchObject({ status: "failed-manual-review", errorCode: "creative-editorial-steps-not-settled" });
  }, 60_000);

  it("rejects a zero-history replay when the customer selects history after artifact reads", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const first = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(first.task.status).toBe("reviewable");
    await seedHistory(library, directory);
    const listArtifacts = library.listAllArtifacts.bind(library);
    vi.spyOn(library, "listAllArtifacts").mockImplementationOnce(async () => {
      const artifacts = await listArtifacts();
      await authorizeHistory(library, ["history_rework_01"]);
      return artifacts;
    });
    const replay = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(replay.task).toMatchObject({ status: "failed-manual-review", errorCode: "historical-authorization-invalid" });
    expect(replay.stories).toEqual([]);
  }, 60_000);

  it("rebuilds the same three story identities from a new exact decision and preserves prior revisions and videos", async () => {
    const library = await editorialLibrary(directory, ["step_01", "step_02"]);
    const dependencies = editorialDependencies(library, directory);
    const assets = await library.listAssets();
    const untyped = await segmentNewBatch("creative_batch_creative_01", assets, dependencies.segmentAnalyzer, library);
    const tagged = await tagSegments(untyped, dependencies.tagAnalyzer, library);
    for (const segment of tagged) await confirmSegmentTags(segment.id, {
      products: [{ id: "product_01", status: "confirmed" }], processStages: ["making", "finished"],
    }, "designer_01", library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    const first = await handoff.processCreativeEditorial(editorialInput, dependencies);
    expect(first.task.status).toBe("reviewable");
    expect(first.stories.map((story) => story.type)).toEqual(["inner-journey", "persona-monologue", "product-journey"]);
    for (const story of first.stories) expect(story.historicalSegmentIds).toEqual(["history_rework_01"]);
    const oldVideos = await Promise.all(first.artifacts.map((artifact) => readFile(artifact.videoPath)));
    await authorizeHistory(library, ["history_rework_02"]);
    await expect(handoff.processCreativeEditorial(editorialInput, dependencies)).rejects.toThrow("creative-editorial-operation-conflict");
    const next = await handoff.processCreativeEditorial({ ...editorialInput, operationKey: "selection-2" }, dependencies);
    expect(next.task.status).toBe("reviewable");
    expect(next.stories.map((story) => story.id)).toEqual(first.stories.map((story) => story.id));
    for (const story of next.stories) expect(story.historicalSegmentIds).toEqual(["history_rework_02"]);
    for (const revision of next.revisions) {
      expect(revision.revision).toBe(2);
      expect(revision.parentRevisionId).toBe(first.revisions.find((item) => item.storyId === revision.storyId)!.id);
      expect(await library.listRevisions(revision.storyId)).toHaveLength(2);
      expect(revision.variants.flatMap((variant) => variant.timeline.map((item) => item.segmentId))).not.toContain("history_rework_01");
    }
    for (const [index, artifact] of first.artifacts.entries()) expect(await readFile(artifact.videoPath)).toEqual(oldVideos[index]);
    expect((await library.listCreativeProcessingTasks()).find((task) => task.id === first.task.id)?.supersededByTaskId).toBe(next.task.id);
    expect(await library.listAllArtifacts()).toHaveLength(24);
  }, 120_000);

  it("does not reuse an earlier completed cut after the designer confirms new evidence", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const dependencies = editorialDependencies(library, directory);
    const first = await handoff.processCreativeEditorial(editorialInput, dependencies);
    expect(first.stories).toHaveLength(2);
    for (const segment of await library.listSegments()) await confirmSegmentTags(segment.id, {
      products: [{ id: "product_01", status: "confirmed" }], processStages: ["making", "finished"],
    }, "designer_01", library);
    const next = await handoff.processCreativeEditorial({ ...editorialInput, operationKey: "confirmed-evidence" }, dependencies);
    expect(next.task.status).toBe("reviewable");
    expect(next.stories).toHaveLength(3);
  }, 60_000);

  it("rejects an old decision at the final commit when consent changes during rendering", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const dependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(), dependencies.segmentAnalyzer, library);
    await tagSegments(untyped, dependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    let changed = false;
    const result = await handoff.processCreativeEditorial(editorialInput, { ...dependencies,
      renderer: async (...args) => {
        const rendered = await renderRevision(...args);
        if (!changed) { changed = true; await authorizeHistory(library, []); }
        return rendered;
      },
    });
    expect(result.task).toMatchObject({ status: "failed-manual-review", errorCode: "historical-authorization-invalid" });
    expect(result.stories).toEqual([]);
    expect(await library.listStories()).toEqual([]);
    expect(await library.listAllArtifacts()).toEqual([]);
    expect(await library.listApprovals()).toEqual([]);
  }, 60_000);

  it("persists cumulative stories, one initial revision each, and four readable local videos per revision", async () => {
    const library = await editorialLibrary(directory, ["step_01", "step_02"]);
    const result = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(result.task.status).toBe("reviewable");
    expect(result.task.publishedClaimed).toBe(false);
    expect(result.stories.length).toBeGreaterThan(0);
    expect(result.revisions).toHaveLength(result.stories.length);
    expect(result.artifacts).toHaveLength(result.revisions.length * 4);
    const stored = await JsonContentStore.open(join(directory, "library.json"));
    expect(await stored.listCreativeProcessingTasks()).toContainEqual(result.task);
    expect(await stored.listStories()).toEqual(result.stories);
    const segments = await stored.listSegments();
    const assets = await stored.listAssets();
    const contexts = result.stories.flatMap((story) => story.anchorSegmentIds).map((id) =>
      assets.find((asset) => asset.id === segments.find((segment) => segment.id === id)?.assetId)!.captureContext!);
    expect(new Set(contexts.map((context) => context.stepId))).toEqual(new Set(["step_01", "step_02"]));
    for (const context of contexts) expect(context).toMatchObject({
      creativeSessionId: "creative_01", deviceId: "watch_sim_01", take: 1,
    });
    for (const revision of result.revisions) {
      expect(revision.revision).toBe(1);
      expect(await stored.listRevisions(revision.storyId)).toEqual([revision]);
      expect(revision.variants.map((variant) => variant.kind)).toEqual([
        "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
      ]);
      expect(new Set(revision.variants.map((variant) => JSON.stringify(variant.timeline))).size).toBe(4);
      expect(await stored.listArtifacts(revision.id)).toEqual(result.artifacts.filter((artifact) => artifact.revisionId === revision.id));
    }
    for (const artifact of result.artifacts) {
      expect((await stat(artifact.videoPath)).size).toBeGreaterThan(0);
      await probeReadableVideo(artifact.videoPath);
      const story = result.stories.find((item) => item.id === result.revisions.find((item) => item.id === artifact.revisionId)?.storyId)!;
      expect(artifact.sourceSegmentIds.every((id) => story.anchorSegmentIds.includes(id))).toBe(true);
    }
    const beforeRetry = await readFile(join(directory, "library.json"));
    const replay = await handoff.processCreativeEditorial({ ...editorialInput, now: "2026-09-07T09:00:00.000Z" },
      editorialDependencies(stored, directory));
    expect(replay).toEqual(result);
    expect(await readFile(join(directory, "library.json"))).toEqual(beforeRetry);
    expect(await stored.listApprovals()).toEqual([]);
    expect(await stored.listDeliveries()).toEqual([]);
  }, 60_000);

  it("resumes the persisted render phase after reload without replanning or duplicating completed outputs", async () => {
    const library = await editorialLibrary(directory, ["step_01", "step_02"]);
    const dependencies = editorialDependencies(library, directory);
    let renders = 0;
    const first = await handoff.processCreativeEditorial(editorialInput, { ...dependencies,
      renderer: async (...args) => {
        if (++renders === 2) throw new Error("transient-render-failure");
        return renderRevision(...args);
      },
    });
    expect(first.task).toMatchObject({ status: "failed-retryable", phase: "render", errorCode: "transient-render-failure" });
    expect(await library.listStories()).toEqual([]);
    const stored = await JsonContentStore.open(join(directory, "library.json"));
    expect(await stored.listCreativeProcessingTasks()).toContainEqual(first.task);
    const result = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(stored, directory),
      storyAnalyzer: { propose: async () => { throw new Error("must-resume-stories"); } },
      editAnalyzer: { plan: async () => { throw new Error("must-resume-revisions"); } },
    });
    expect(result.task.status).toBe("reviewable");
    for (const revision of result.revisions) expect(await stored.listRevisions(revision.storyId)).toHaveLength(1);
    expect(new Set((await stored.listAllArtifacts()).map((artifact) => artifact.id)).size).toBe(result.artifacts.length);
    expect((await readdir(join(directory, "renders"))).length).toBe(result.revisions.length);
  }, 60_000);

  it("freezes historical ranking inputs before story work and fails closed if another approval changes usage", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const seedDependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(),
      seedDependencies.segmentAnalyzer, library);
    await tagSegments(untyped, seedDependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01", "history_rework_02"]);
    const dependencies = editorialDependencies(library, directory);
    let storyAttempts = 0;
    const first = await handoff.processCreativeEditorial(editorialInput, {
      ...dependencies,
      storyAnalyzer: { propose: async () => {
        storyAttempts += 1;
        throw new Error("temporarily-unavailable");
      } },
    });
    expect(first.task).toMatchObject({
      status: "failed-retryable",
      phase: "story",
      checkpoint: {
        historicalCandidateDigest: expect.any(String),
        historicalCandidates: [
          expect.objectContaining({ id: "history_rework_01", tags: expect.objectContaining({ usage: { useCount: 0, lastUsedAt: null, storyIds: [], variants: [] } }) }),
          expect.objectContaining({ id: "history_rework_02", tags: expect.objectContaining({ usage: { useCount: 0, lastUsedAt: null, storyIds: [], variants: [] } }) }),
        ],
      },
    });

    await approveHistoricalSegment(library, directory, "history_rework_01");
    const retry = await handoff.processCreativeEditorial(editorialInput, {
      ...dependencies,
      storyAnalyzer: { propose: async (segments) => {
        storyAttempts += 1;
        return dependencies.storyAnalyzer.propose(segments);
      } },
    });
    expect(retry.task.status).toBe("reviewable");
    for (const story of retry.stories) {
      expect(story.historicalSegmentIds).toEqual(["history_rework_01", "history_rework_02"]);
    }
    expect(storyAttempts).toBe(2);
    const ownArtifact = retry.artifacts.find((item) => item.kind === "wechat-channels")!;
    const ownRevision = retry.revisions.find((item) => item.id === ownArtifact.revisionId)!;
    await approveVariant({ storyId: ownRevision.storyId, revisionId: ownRevision.id,
      revision: ownRevision.revision, artifactId: ownArtifact.id, variant: ownArtifact.kind,
      reviewerId: "designer_01" }, library);
    expect((await library.readCreativeEditorialBundle(retry.task.id)).task.status).toBe("reviewable");
  }, 120_000);

  it("rejects the final atomic reviewable commit when frozen historical evidence changes during rendering", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const initialDependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(),
      initialDependencies.segmentAnalyzer, library);
    await tagSegments(untyped, initialDependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    const dependencies = editorialDependencies(library, directory);
    let changed = false;

    const result = await handoff.processCreativeEditorial(editorialInput, {
      ...dependencies,
      renderer: async (...args) => {
        const rendered = await renderRevision(...args);
        if (!changed) {
          changed = true;
          const historical = (await library.listSegments()).find((segment) => segment.id === "history_rework_01")!;
          await library.putSegments([{ ...historical, tags: {
            ...historical.tags,
            transcript: `${historical.tags.transcript} changed while rendering`,
          } }]);
        }
        return rendered;
      },
    });

    expect(result.task).toMatchObject({
      status: "failed-manual-review",
      errorCode: "creative-editorial-historical-evidence-changed",
    });
    expect(await library.listStories()).toEqual([]);
    expect((await library.listCreativeProcessingTasks()).filter((task) => task.status === "reviewable")).toEqual([]);
  }, 120_000);

  it("allows only usage changes and rejects every frozen segment or parent-asset evidence mutation on completed reads", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const initialDependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(),
      initialDependencies.segmentAnalyzer, library);
    await tagSegments(untyped, initialDependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    const result = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(result.task.status).toBe("reviewable");
    const path = join(directory, "library.json");
    const original = JSON.parse(await readFile(path, "utf8")) as any;
    const cases: Array<{ name: string; allowed: boolean; mutate(snapshot: any): void }> = [
      { name: "usage", allowed: true, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.usage.useCount += 1;
      } },
      { name: "transcript", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.transcript += " changed";
      } },
      { name: "products", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.products = [{ id: "other", status: "confirmed" }];
      } },
      { name: "topics", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.topics = ["changed"];
      } },
      { name: "people", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.people = ["changed"];
      } },
      { name: "emotions", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.emotions = ["changed"];
      } },
      { name: "baseTypes", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.baseTypes = ["scene"];
      } },
      { name: "segment permission", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").permissionState = "review-required";
      } },
      { name: "segment technical flags", allowed: false, mutate: (snapshot) => {
        snapshot.segments.find((item: any) => item.id === "history_rework_01").tags.technicalFlags = ["changed"];
      } },
      { name: "parent source", allowed: false, mutate: (snapshot) => {
        snapshot.assets.find((item: any) => item.id === "asset_history_rework_01").origin = "new";
      } },
      { name: "parent localPath", allowed: false, mutate: (snapshot) => {
        snapshot.assets.find((item: any) => item.id === "asset_history_rework_01").localPath += ".moved";
      } },
      { name: "parent digest", allowed: false, mutate: (snapshot) => {
        snapshot.assets.find((item: any) => item.id === "asset_history_rework_01").digest = "changed";
      } },
      { name: "parent technical flags", allowed: false, mutate: (snapshot) => {
        snapshot.assets.find((item: any) => item.id === "asset_history_rework_01").technicalFlags = ["changed"];
      } },
      { name: "parent lineage", allowed: false, mutate: (snapshot) => {
        snapshot.assets.find((item: any) => item.id === "asset_history_rework_01").derivedFromAssetId = "other_parent";
      } },
    ];

    for (const item of cases) {
      const changed = structuredClone(original);
      item.mutate(changed);
      await writeFile(path, `${JSON.stringify(changed, null, 2)}\n`, "utf8");
      const reopened = await JsonContentStore.open(path);
      const read = reopened.readCreativeEditorialBundle(result.task.id);
      if (item.allowed) await expect(read, item.name).resolves.toMatchObject({ task: { status: "reviewable" } });
      else await expect(read, item.name).rejects.toThrow("creative-editorial-historical-evidence-changed");
    }
  }, 180_000);

  it("atomically migrates a safe pre-freeze checkpoint and resumes the same operation key", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const initialDependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(),
      initialDependencies.segmentAnalyzer, library);
    await tagSegments(untyped, initialDependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    const dependencies = editorialDependencies(library, directory);
    const first = await handoff.processCreativeEditorial(editorialInput, {
      ...dependencies,
      storyAnalyzer: { propose: async () => { throw new Error("temporarily-unavailable"); } },
    });
    expect(first.task.status).toBe("failed-retryable");
    const path = join(directory, "library.json");
    const legacy = JSON.parse(await readFile(path, "utf8")) as any;
    const legacyTask = legacy.creativeProcessingTasks.find((task: any) => task.id === first.task.id);
    delete legacyTask.checkpoint.historicalCandidates;
    delete legacyTask.checkpoint.historicalCandidateDigest;
    delete legacyTask.checkpoint.historicalEvidenceDigest;
    legacyTask.inputDigest = "pre-freeze-input-digest";
    await writeFile(path, `${JSON.stringify(legacy, null, 2)}\n`, "utf8");

    const reopened = await JsonContentStore.open(path);
    const resumed = await handoff.processCreativeEditorial(editorialInput,
      editorialDependencies(reopened, directory));

    expect(resumed.task.status).toBe("reviewable");
    expect(resumed.task.id).toBe(first.task.id);
    expect(resumed.task.checkpoint).toMatchObject({
      historicalCandidates: [expect.objectContaining({ id: "history_rework_01" })],
      historicalCandidateDigest: expect.any(String),
      historicalEvidenceDigest: expect.any(String),
    });
    expect((await reopened.listAuditEvents("creative_01"))).toContainEqual(expect.objectContaining({
      action: "creative-editorial-checkpoint-migrated",
      after: expect.objectContaining({ taskId: first.task.id }),
    }));
  }, 120_000);

  it("atomically stops an unsafe pre-freeze checkpoint as migration-required instead of operation-conflict", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const initialDependencies = editorialDependencies(library, directory);
    const untyped = await segmentNewBatch("creative_batch_creative_01", await library.listAssets(),
      initialDependencies.segmentAnalyzer, library);
    await tagSegments(untyped, initialDependencies.tagAnalyzer, library);
    await seedHistory(library, directory);
    await authorizeHistory(library, ["history_rework_01"]);
    const dependencies = editorialDependencies(library, directory);
    const first = await handoff.processCreativeEditorial(editorialInput, {
      ...dependencies,
      storyAnalyzer: { propose: async () => { throw new Error("temporarily-unavailable"); } },
    });
    const path = join(directory, "library.json");
    const legacy = JSON.parse(await readFile(path, "utf8")) as any;
    const legacyTask = legacy.creativeProcessingTasks.find((task: any) => task.id === first.task.id);
    delete legacyTask.checkpoint.historicalCandidates;
    delete legacyTask.checkpoint.historicalCandidateDigest;
    delete legacyTask.checkpoint.historicalEvidenceDigest;
    legacyTask.inputDigest = "pre-freeze-input-digest";
    legacy.segments = legacy.segments.filter((segment: any) => segment.id !== "history_rework_01");
    await writeFile(path, `${JSON.stringify(legacy, null, 2)}\n`, "utf8");

    const reopened = await JsonContentStore.open(path);
    const stopped = await handoff.processCreativeEditorial(editorialInput,
      editorialDependencies(reopened, directory));

    expect(stopped.task).toMatchObject({
      id: first.task.id,
      status: "failed-manual-review",
      errorCode: "creative-editorial-checkpoint-migration-required",
    });
    expect((await reopened.listCreativeProcessingTasks()).find((task) => task.id === first.task.id))
      .toEqual(stopped.task);
    expect((await reopened.listAuditEvents("creative_01"))).toContainEqual(expect.objectContaining({
      action: "creative-editorial-checkpoint-migration-required",
      after: expect.objectContaining({ taskId: first.task.id }),
    }));
  }, 120_000);

  it("reserves at most one manual restart lineage for the same failed task", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const failed = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory),
      storyAnalyzer: { propose: async () => [] },
    });
    expect(failed.task.status).toBe("failed-manual-review");

    const authorization = (suffix: string) => ({
      id: `editorial_restart_${suffix}`,
      creativeSessionId: "creative_01",
      failedTaskId: failed.task.id,
      resetOperationKey: `reset-${suffix}`,
      processOperationKey: `process-${suffix}`,
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART" as const,
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: `2026-09-08T09:00:0${suffix}.000Z`,
    });
    const attempts = await Promise.allSettled([
      library.reserveCreativeEditorialRestart(authorization("1")),
      library.reserveCreativeEditorialRestart(authorization("2")),
    ]);

    expect(attempts.filter((attempt) => attempt.status === "fulfilled")).toHaveLength(1);
    expect(attempts.filter((attempt) => attempt.status === "rejected")).toHaveLength(1);
    expect(await library.listCreativeEditorialRestartAuthorizations()).toHaveLength(1);
    expect((await library.listAuditEvents("creative_01")).filter((event) =>
      event.action === "creative-editorial-manual-restart-authorized")).toHaveLength(1);
  }, 120_000);

  it("replays the exact manual restart across two already-open store handles", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const failed = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory),
      storyAnalyzer: { propose: async () => [] },
    });
    const path = join(directory, "library.json");
    const first = await JsonContentStore.open(path);
    const second = await JsonContentStore.open(path);
    const request = {
      id: "editorial_restart_stale_handles_01",
      creativeSessionId: "creative_01",
      failedTaskId: failed.task.id,
      resetOperationKey: "reset-stale-handles-01",
      processOperationKey: "process-stale-handles-01",
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART" as const,
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: "2026-09-08T09:00:00.000Z",
    };

    const reserved = await Promise.all([
      first.reserveCreativeEditorialRestart(request),
      second.reserveCreativeEditorialRestart(request),
    ]);
    expect(reserved[0]).toEqual(reserved[1]);

    const replacement = CreativeProcessingTaskSchema.parse({
      ...failed.task,
      id: "editorial_task_stale_handles_replacement_01",
      operationKey: request.processOperationKey,
      status: "processing",
      phase: "story",
      errorCode: null,
      supersededByTaskId: undefined,
      updatedAt: "2026-09-08T09:00:01.000Z",
    });
    const begin = {
      authorizationId: request.id,
      creativeSessionId: request.creativeSessionId,
      processOperationKey: request.processOperationKey,
      replacementTask: replacement,
      consumedAt: "2026-09-08T09:00:01.000Z",
    };
    const begun = await Promise.all([
      first.beginCreativeEditorialRestart(begin),
      second.beginCreativeEditorialRestart(begin),
    ]);
    expect(begun[0]).toEqual(begun[1]);

    const persisted = await JsonContentStore.open(path);
    expect(await persisted.listCreativeEditorialRestartAuthorizations()).toHaveLength(1);
    expect((await persisted.listCreativeProcessingTasks()).filter((task) => task.id === replacement.id))
      .toHaveLength(1);
    expect((await persisted.listAuditEvents("creative_01")).filter((event) =>
      event.action === "creative-editorial-manual-restart-authorized")).toHaveLength(1);
    expect((await persisted.listAuditEvents("creative_01")).filter((event) =>
      event.action === "creative-editorial-manual-restart-consumed")).toHaveLength(1);
    await expect(first.listCreativeEditorialRestartAuthorizations()).resolves.toHaveLength(1);
    await expect(second.listCreativeProcessingTasks()).resolves.toEqual(expect.arrayContaining([
      expect.objectContaining({ id: replacement.id }),
    ]));
  }, 120_000);

  it("atomically begins one manual replacement across concurrency, restart, and processing failure", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const failed = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory),
      storyAnalyzer: { propose: async () => [] },
    });
    const authorization = await library.reserveCreativeEditorialRestart({
      id: "editorial_restart_atomic_01",
      creativeSessionId: "creative_01",
      failedTaskId: failed.task.id,
      resetOperationKey: "reset-atomic-01",
      processOperationKey: "process-atomic-01",
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: "2026-09-08T09:01:00.000Z",
    });
    const replacement = CreativeProcessingTaskSchema.parse({
      ...failed.task,
      id: "editorial_task_atomic_replacement_01",
      operationKey: authorization.processOperationKey,
      status: "processing",
      phase: "story",
      errorCode: null,
      supersededByTaskId: undefined,
      updatedAt: "2026-09-08T09:01:01.000Z",
    });
    const begin = {
      authorizationId: authorization.id,
      creativeSessionId: "creative_01",
      processOperationKey: authorization.processOperationKey,
      replacementTask: replacement,
      consumedAt: "2026-09-08T09:01:01.000Z",
    };

    const simultaneous = await Promise.all([
      library.beginCreativeEditorialRestart(begin),
      library.beginCreativeEditorialRestart(begin),
    ]);
    expect(simultaneous[0]).toEqual(simultaneous[1]);
    expect((await library.listCreativeProcessingTasks()).filter((task) =>
      task.id === replacement.id)).toHaveLength(1);
    expect((await library.listAuditEvents("creative_01")).filter((event) =>
      event.action === "creative-editorial-manual-restart-consumed")).toHaveLength(1);

    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    await expect(reopened.beginCreativeEditorialRestart(begin)).resolves.toEqual(simultaneous[0]);
    await expect(reopened.beginCreativeEditorialRestart({
      ...begin,
      replacementTask: { ...replacement, id: "editorial_task_atomic_replacement_02" },
    })).rejects.toThrow("creative-editorial-manual-authorization-conflict");
    await reopened.putCreativeProcessingTask({
      ...replacement,
      status: "failed-manual-review",
      errorCode: "creative-editorial-no-supported-story",
      updatedAt: "2026-09-08T09:01:02.000Z",
    });
    await expect(reopened.reserveCreativeEditorialRestart({
      id: "editorial_restart_atomic_02",
      creativeSessionId: "creative_01",
      failedTaskId: failed.task.id,
      resetOperationKey: "reset-atomic-02",
      processOperationKey: "process-atomic-02",
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: "2026-09-08T09:01:03.000Z",
    })).rejects.toThrow("creative-editorial-manual-task-not-current");
    expect(await reopened.listCreativeEditorialRestartAuthorizations()).toHaveLength(1);
  }, 120_000);

  it("resumes a consumed manual restart from its failed render checkpoint after service reload", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const failed = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory),
      storyAnalyzer: { propose: async () => [] },
    });
    expect(failed.task.status).toBe("failed-manual-review");
    const authorization = await library.reserveCreativeEditorialRestart({
      id: "editorial_restart_resume_checkpoint_01",
      creativeSessionId: "creative_01",
      failedTaskId: failed.task.id,
      resetOperationKey: "reset-resume-checkpoint-01",
      processOperationKey: "process-resume-checkpoint-01",
      confirmedBy: "designer_01",
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: "2026-09-08T09:02:00.000Z",
    });
    const base = editorialDependencies(library, directory);
    let storyAttempts = 0;
    let editAttempts = 0;
    let renderAttempts = 0;
    const restartInput = {
      creativeSessionId: "creative_01",
      operationKey: authorization.processOperationKey,
      manualRestartAuthorizationId: authorization.id,
      now: "2026-09-08T09:02:01.000Z",
    };
    const interrupted = await handoff.processCreativeEditorial(restartInput, {
      ...base,
      storyAnalyzer: { propose: async (segments) => {
        storyAttempts += 1;
        const proposal = await base.storyAnalyzer.propose(segments);
        return proposal.slice(0, 1);
      } },
      editAnalyzer: { plan: async (input) => {
        editAttempts += 1;
        return base.editAnalyzer.plan(input);
      } },
      renderer: async () => {
        renderAttempts += 1;
        throw new Error("transient-render-failure");
      },
    });
    expect(interrupted.task).toMatchObject({
      status: "failed-retryable",
      phase: "render",
      operationKey: authorization.processOperationKey,
    });
    expect({ storyAttempts, editAttempts, renderAttempts }).toEqual({
      storyAttempts: 1,
      editAttempts: 1,
      renderAttempts: 1,
    });

    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    const resumed = await handoff.processCreativeEditorial({
      ...restartInput,
      now: "2026-09-08T09:02:02.000Z",
    }, {
      ...editorialDependencies(reopened, directory),
      storyAnalyzer: { propose: async () => { throw new Error("must-resume-after-story"); } },
      editAnalyzer: { plan: async () => { throw new Error("must-resume-after-revision"); } },
      renderer: async (...args) => {
        renderAttempts += 1;
        return renderRevision(...args);
      },
    });

    expect(resumed.task).toMatchObject({
      id: interrupted.task.id,
      status: "reviewable",
      operationKey: authorization.processOperationKey,
    });
    expect({ storyAttempts, editAttempts, renderAttempts }).toEqual({
      storyAttempts: 1,
      editAttempts: 1,
      renderAttempts: 2,
    });
    const tasks = await reopened.listCreativeProcessingTasks();
    expect(tasks.filter((task) => task.operationKey === authorization.processOperationKey))
      .toHaveLength(1);
    expect(tasks.filter((task) => !task.supersededByTaskId)).toEqual([
      expect.objectContaining({ id: interrupted.task.id, status: "reviewable" }),
    ]);
    expect(await reopened.listCreativeEditorialRestartAuthorizations()).toEqual([
      expect.objectContaining({
        id: authorization.id,
        status: "consumed",
        restartedTaskId: interrupted.task.id,
      }),
    ]);
    const audits = await reopened.listAuditEvents("creative_01");
    expect(audits.filter((event) =>
      event.action === "creative-editorial-manual-restart-authorized")).toHaveLength(1);
    expect(audits.filter((event) =>
      event.action === "creative-editorial-manual-restart-consumed")).toHaveLength(1);
  }, 120_000);

  it("rechecks the full creative session and current editorial chain while creating an export", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const editorial = await handoff.processCreativeEditorial(editorialInput,
      editorialDependencies(library, directory));
    const artifact = editorial.artifacts.find((item) => item.kind === "wechat-channels")!;
    const revision = editorial.revisions.find((item) => item.id === artifact.revisionId)!;
    const approval = await approveVariant({ storyId: revision.storyId, revisionId: revision.id,
      revision: revision.revision, artifactId: artifact.id, variant: artifact.kind,
      reviewerId: "designer_01" }, library);
    const input = { approvalId: approval.id, artifactId: artifact.id,
      revisionId: revision.id, creativeSessionId: "creative_01" };
    const originalCommitPackage = library.commitExportPackage.bind(library);
    let releaseRead!: () => void;
    let markRead!: () => void;
    const readReached = new Promise<void>((resolve) => { markRead = resolve; });
    const continueRead = new Promise<void>((resolve) => { releaseRead = resolve; });
    let intercepted = false;
    library.commitExportPackage = async (...args) => {
      if (!intercepted) {
        intercepted = true;
        markRead();
        await continueRead;
      }
      return originalCommitPackage(...args);
    };

    const creating = createExportPackage(input, library);
    await readReached;
    const other = await JsonContentStore.open(join(directory, "library.json"));
    const session = (await other.listCreativeSessions()).find((item) => item.id === "creative_01")!;
    await other.commitCreativeBundle({
      session: { ...session, stage: "paused", privacyMode: "paused",
        updatedAt: "2026-09-08T09:05:00.000Z" },
      expectedSession: { id: session.id, stage: session.stage,
        currentIntentVersion: session.currentIntentVersion,
        currentBriefVersion: session.currentBriefVersion,
        currentScriptVersion: session.currentScriptVersion,
        currentShotPlanVersion: session.currentShotPlanVersion },
      assets: [], events: [], auditEvents: [{
        id: "audit_export_pause_race_01", entityType: "creative-session", entityId: session.id,
        actorId: "customer_01", action: "creative-session-transitioned", reason: "privacy pause during export",
        before: { stage: session.stage, privacyMode: session.privacyMode },
        after: { stage: "paused", privacyMode: "paused" }, createdAt: "2026-09-08T09:05:00.000Z",
      }],
    });
    releaseRead();

    await expect(creating).rejects.toThrow("legacy-export-creative-forbidden");
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect(await persisted.listExportPackages()).toEqual([]);
  }, 120_000);

  it("never exports a genuine creative story after its current processing task disappears", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const editorial = await handoff.processCreativeEditorial(editorialInput,
      editorialDependencies(library, directory));
    const artifact = editorial.artifacts.find((item) => item.kind === "wechat-channels")!;
    const revision = editorial.revisions.find((item) => item.id === artifact.revisionId)!;
    const approval = await approveVariant({ storyId: revision.storyId, revisionId: revision.id,
      revision: revision.revision, artifactId: artifact.id, variant: artifact.kind,
      reviewerId: "designer_01" }, library);
    const path = join(directory, "library.json");
    const snapshot = JSON.parse(await readFile(path, "utf8")) as { creativeProcessingTasks: unknown[] };
    snapshot.creativeProcessingTasks = [];
    await writeFile(path, `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");
    const reopened = await JsonContentStore.open(path);

    await expect(createExportPackage({ approvalId: approval.id, artifactId: artifact.id,
      revisionId: revision.id, creativeSessionId: "creative_01" }, reopened))
      .rejects.toThrow("legacy-export-creative-forbidden");
    expect(await reopened.listExportPackages()).toEqual([]);
  }, 120_000);

  it("does not persist a creative delivery when another handle invalidates approval during the adapter call", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const editorial = await handoff.processCreativeEditorial(editorialInput,
      editorialDependencies(library, directory));
    const artifact = editorial.artifacts.find((item) => item.kind === "wechat-channels")!;
    const revision = editorial.revisions.find((item) => item.id === artifact.revisionId)!;
    const approval = await approveVariant({ storyId: revision.storyId, revisionId: revision.id,
      revision: revision.revision, artifactId: artifact.id, variant: artifact.kind,
      reviewerId: "designer_01" }, library);
    const exported = await createExportPackage({ approvalId: approval.id, artifactId: artifact.id,
      revisionId: revision.id, creativeSessionId: "creative_01" }, library);
    let releaseAdapter!: () => void;
    let markAdapter!: () => void;
    const adapterReached = new Promise<void>((resolve) => { markAdapter = resolve; });
    const continueAdapter = new Promise<void>((resolve) => { releaseAdapter = resolve; });
    const delivering = deliverExportPackage(exported.id, {
      deliver: async () => {
        markAdapter();
        await continueAdapter;
        return { receiptId: "must-not-persist", receivedAt: "2026-09-08T09:06:00.000Z" };
      },
    }, library, "creative_01");
    await adapterReached;
    const other = await JsonContentStore.open(join(directory, "library.json"));
    await other.putApproval({ ...approval, status: "invalidated" });
    releaseAdapter();

    await expect(delivering).rejects.toThrow("active-export-package-required");
    const persisted = await JsonContentStore.open(join(directory, "library.json"));
    expect(await persisted.listDeliveries()).toEqual([]);
    expect((await persisted.listExportPackages()).find((item) => item.id === exported.id)?.status)
      .toBe("invalidated");
  }, 120_000);

  it("does not mark a non-video artifact as reviewable even when renderer metadata claims success", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const result = await handoff.processCreativeEditorial(editorialInput, {
      ...editorialDependencies(library, directory), renderer: async (...args) => {
        const rendered = await renderRevision(...args);
        const artifact = rendered.artifacts[0]!;
        await writeFile(artifact.videoPath, "not-a-video");
        artifact.digest = sha256(Buffer.from("not-a-video"));
        return rendered;
      },
    });
    expect(result.task.status).toBe("failed-manual-review");
    expect(result.task.phase).toBe("render");
    expect(await library.listStories()).toEqual([]);
    expect(await library.listAllArtifacts()).toEqual([]);
  }, 60_000);

  it("does not silently drop an accepted step when segmentation fails", async () => {
    const library = await editorialLibrary(directory, ["step_01", "step_02"]);
    const dependencies = editorialDependencies(library, directory);
    const result = await handoff.processCreativeEditorial(editorialInput, { ...dependencies,
      segmentAnalyzer: { analyze: async (asset) => {
        if (asset.captureContext?.stepId === "step_02") throw new Error("temporarily-unavailable");
        return dependencies.segmentAnalyzer.analyze();
      } },
    });
    expect(result.task).toMatchObject({ status: "failed-retryable", phase: "segment" });
    expect(await library.listStories()).toEqual([]);
  });

  it("keeps reviewable results atomic when the final content bundle write fails", async () => {
    const library = await editorialLibrary(directory, ["step_01"]);
    const commit = vi.spyOn(library, "commitCreativeEditorialBundle").mockRejectedValueOnce(new Error("storage-full"));
    const result = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(library, directory));
    expect(result.task.status).toBe("failed-retryable");
    expect(await library.listStories()).toEqual([]);
    expect(await library.listAllArtifacts()).toEqual([]);
    commit.mockRestore();
    const stored = await JsonContentStore.open(join(directory, "library.json"));
    const retry = await handoff.processCreativeEditorial(editorialInput, editorialDependencies(stored, directory));
    expect(retry.task.status).toBe("reviewable");
    expect(await stored.listAllArtifacts()).toHaveLength(retry.revisions.length * 4);
  }, 60_000);

  it.each(["prepare", "commit"] as const)("rejects a stale return when retake arrives during originals %s", async (phase) => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const other = library;
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const racedOriginals = { prepare: async (input: Parameters<typeof originals.prepare>[0]) => {
      const prepared = await originals.prepare(input);
      if (phase === "prepare") await record(other, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      return { ...prepared, commit: async () => {
        await prepared.commit();
        if (phase === "commit") await record(other, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      } };
    } };
    await expect(acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG),
      { library, originals: racedOriginals },
    )).rejects.toThrow("creative-capture-return-not-current");
    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    expect(await reopened.listAssets()).toEqual([]);
    expect((await reopened.listCreativeSessionEvents()).map((event) => event.action)).toEqual(["completed", "retake"]);
    expect((await settleCreativeCapture({ creativeSessionId: "creative_01", actorId: "designer_01",
      operationKey: "settle-after-race", now }, reopened)).status).toBe("collecting");
    await record(reopened, "step_01", "watch_sim_01", "watch-director", "completed", 2);
    const next = await acceptCreativeCapture({
      ...command("step_01", "watch_sim_01", "watch-director", "completed", PNG, 2), operationKey: "take-2-after-race",
    }, { library: reopened, originals });
    expect(next.steps).toEqual([{ stepId: "step_01", state: "completed-returned", acceptedTake: 2 }]);
    expect(await reopened.listAssets()).toHaveLength(1);
  });

  it("rejects stale settlement when retake arrives before the locked commit", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const other = library;
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    await acceptCreativeCapture(command("step_01", "watch_sim_01", "watch-director", "completed", PNG),
      { library, originals: new LocalCreativeOriginalStore(join(directory, "originals")) });
    const commit = library.commitCreativeCaptureBundle.bind(library);
    vi.spyOn(library, "commitCreativeCaptureBundle").mockImplementationOnce(async (bundle) => {
      await record(other, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      return commit(bundle);
    });
    await expect(settleCreativeCapture({ creativeSessionId: "creative_01", actorId: "designer_01",
      operationKey: "settle-race", now }, library)).rejects.toThrow("creative-capture-settlement-changed");
    expect((await other.listCreativeSessions())[0]!.stage).toBe("ingesting");
    expect((await library.listCreativeSessionEvents()).at(-1)!.action).toBe("retake");
  });

  it("rejects a colliding unrecorded device before it can replace an accepted original", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const firstBytes = Buffer.from(PNG);
    const firstCommand = command("step_01", "watch_sim_01", "watch-director", "completed", firstBytes);
    const first = await acceptCreativeCapture(firstCommand, { library, originals });
    const firstAsset = { ...first.assets[0]!, originalPath: first.assets[0]!.localPath };
    const replay = await acceptCreativeCapture(firstCommand, { library, originals });
    expect(replay.assets[0]?.localPath).toBe(firstAsset.originalPath);

    const rejected = await acceptCreativeCapture(
      command("step_01", "stone_a_sim_01", "main-stone-a", "completed", Buffer.from("different")),
      { library, originals },
    ).then(() => ({ error: null }), (error: Error) => ({ error: error.message }));

    expect(rejected.error).toBe("creative-capture-action-not-recorded");
    expect(await readFile(firstAsset.originalPath)).toEqual(firstBytes);
    expect(sha256(await readFile(firstAsset.originalPath))).toBe(firstAsset.digest);
  });

  it("rejects a second recorded device for the same step and take without changing the first original", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const firstBytes = Buffer.from(PNG);
    const first = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", firstBytes),
      { library, originals },
    );
    const firstAsset = first.assets[0]!;
    await record(library, "step_01", "stone_a_sim_01", "main-stone-a", "completed", 1);

    const rejected = await acceptCreativeCapture(
      command("step_01", "stone_a_sim_01", "main-stone-a", "completed", PNG),
      { library, originals },
    ).then(() => ({ error: null }), (error: Error) => ({ error: error.message }));

    expect(rejected.error).toBe("creative-capture-take-already-returned");
    expect(await library.listAssets()).toHaveLength(1);
    expect(await readFile(firstAsset.localPath)).toEqual(firstBytes);
    expect(sha256(await readFile(firstAsset.localPath))).toBe(firstAsset.digest);
  });

  it("accepts two distinct originals in one capture group and replays that exact group without duplication", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const grouped = {
      ...command("step_01", "watch_sim_01", "watch-director", "completed", PNG),
      operationKey: "two-originals-one-capture",
      uploads: [upload(PNG), upload(SECOND_PNG)],
    };

    const accepted = await acceptCreativeCapture(grouped, { library, originals });
    const beforeReplay = await readFile(join(directory, "library.json"));
    const commit = vi.spyOn(library, "commitCreativeCaptureBundle")
      .mockRejectedValue(new Error("replay-must-not-commit-another-capture-bundle"));
    const replay = await acceptCreativeCapture(grouped, { library, originals });
    commit.mockRestore();

    expect(accepted.assets).toHaveLength(2);
    expect(await library.listAssets()).toHaveLength(2);
    expect(replay).toEqual(accepted);
    expect(await readFile(join(directory, "library.json"))).toEqual(beforeReplay);
  });

  it("serializes concurrent independent capture groups and leaves only one accepted business take", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    await record(library, "step_01", "stone_a_sim_01", "main-stone-a", "completed", 1);
    const first = command("step_01", "watch_sim_01", "watch-director", "completed", PNG);
    const second = command("step_01", "stone_a_sim_01", "main-stone-a", "completed", SECOND_PNG);

    const settled = await Promise.allSettled([
      acceptCreativeCapture(first, { library, originals }),
      acceptCreativeCapture(second, { library, originals }),
    ]);

    expect(settled.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(settled.filter((result) => result.status === "rejected").map((result) =>
      (result as PromiseRejectedResult).reason.message)).toEqual(["creative-capture-take-already-returned"]);
    expect(await library.listAssets()).toHaveLength(1);
  });

  it("rejects a forged public capture bundle that reuses another device's acceptance identity", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals },
    );
    const session = (await library.listCreativeSessions())[0]!;
    const returnEvent = (await library.listCreativeSessionEvents()).find(
      (event) => event.action === "media-returned",
    )!;
    const processingTask = (await library.listCreativeProcessingTasks())[0]!;
    const forged = {
      ...accepted.assets[0]!, id: "asset_forged_second_device", digest: "forged-second-device-digest",
      localPath: join(directory, "forged-second-device.png"),
      captureContext: {
        ...accepted.assets[0]!.captureContext!, deviceId: "stone_a_sim_01", deviceRole: "main-stone-a" as const,
      },
    };

    const forgedBundle = {
      session,
      expectedSession: sessionPrecondition(session),
      assets: [forged],
      events: [],
      captureAcceptanceId: returnEvent.idempotencyKey,
      processingTask,
      auditEvents: [],
    };
    await expect(library.commitCreativeCaptureBundle(forgedBundle))
      .rejects.toThrow("creative-capture-acceptance-event-required");

    expect(await library.listAssets()).toHaveLength(1);
  });

  it("rejects a second asset in a public capture bundle carrying the identical persisted return event", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals },
    );
    const session = (await library.listCreativeSessions())[0]!;
    const returnEvent = (await library.listCreativeSessionEvents()).find(
      (event) => event.action === "media-returned",
    )!;
    const processingTask = (await library.listCreativeProcessingTasks())[0]!;
    const before = await readFile(join(directory, "library.json"));
    const secondAsset = {
      ...accepted.assets[0]!, id: "asset_second_same_device", digest: sha256(SECOND_PNG),
      localPath: join(directory, "second-same-device.png"),
    };

    await expect(library.commitCreativeCaptureBundle({
      session,
      expectedSession: sessionPrecondition(session),
      assets: [secondAsset],
      events: [returnEvent],
      processingTask,
      auditEvents: [],
    })).rejects.toThrow("creative-capture-acceptance-event-already-used");

    expect(await library.listAssets()).toEqual(accepted.assets);
    expect(await readFile(join(directory, "library.json"))).toEqual(before);
    expect(await readFile(accepted.assets[0]!.localPath)).toEqual(PNG);
  });

  it.each([
    ["empty events", "empty", "creative-capture-acceptance-event-required"],
    ["a new unrelated note", "note", "creative-capture-acceptance-event-required"],
    ["a new return for another step", "other-step", "creative-capture-acceptance-provenance-mismatch"],
    ["a new return for the same take", "same-take", "creative-capture-take-already-returned"],
  ] as const)("rejects a second asset in a public capture bundle with %s", async (_name, eventCase, error) => {
    const library = await preparedLibrary(directory, ["step_01", "step_02"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals },
    );
    const session = (await library.listCreativeSessions())[0]!;
    const returnEvent = (await library.listCreativeSessionEvents()).find(
      (event) => event.action === "media-returned",
    )!;
    const events: CreativeSessionEvent[] = eventCase === "empty" ? [] : [{
      ...returnEvent, id: "new_unrelated_event", idempotencyKey: "new-unrelated-operation",
      action: eventCase === "note" ? "note" : "media-returned",
      stepId: eventCase === "other-step" ? "step_02" : "step_01",
    }];
    const before = await readFile(join(directory, "library.json"));

    await expect(library.commitCreativeCaptureBundle({
      session,
      expectedSession: sessionPrecondition(session),
      assets: [{
        ...accepted.assets[0]!, id: "asset_second_same_device", digest: sha256(SECOND_PNG),
        localPath: join(directory, "second-same-device.png"),
      }],
      events,
      processingTask: (await library.listCreativeProcessingTasks())[0]!,
      auditEvents: [],
    })).rejects.toThrow(error);

    expect(await library.listAssets()).toEqual(accepted.assets);
    expect(await readFile(join(directory, "library.json"))).toEqual(before);
  });

  it.each(["retake", "skipped"] as const)("requires a return event even when a public capture asset claims %s", async (stepOutcome) => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals },
    );
    const session = (await library.listCreativeSessions())[0]!;
    const before = await readFile(join(directory, "library.json"));

    await expect(library.commitCreativeCaptureBundle({
      session,
      expectedSession: sessionPrecondition(session),
      assets: [{
        ...accepted.assets[0]!, id: "asset_with_noncompleted_outcome", digest: sha256(SECOND_PNG),
        localPath: join(directory, "noncompleted-outcome.png"),
        captureContext: { ...accepted.assets[0]!.captureContext!, stepOutcome },
      }],
      events: [],
      processingTask: (await library.listCreativeProcessingTasks())[0]!,
      auditEvents: [],
    })).rejects.toThrow("creative-capture-acceptance-event-required");

    expect(await library.listAssets()).toEqual(accepted.assets);
    expect(await readFile(join(directory, "library.json"))).toEqual(before);
  });

  it("cleans every staged original when a later upload is invalid so an exact retry is not blocked", async () => {
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    const input = {
      identity: "creative_01--step_01--watch_sim_01--1", operationKey: "partial-upload-op",
      uploads: [upload(PNG), upload(Buffer.from("not-an-image"))],
    };

    await expect(originals.prepare(input)).rejects.toThrow("creative-capture-media-not-decodable");
    await expect(readdir(join(directory, "originals"))).resolves.toEqual([]);
    await expect(originals.prepare({ ...input, uploads: [upload(PNG)] })).resolves.toBeDefined();
  });

  it("requires real decodability instead of accepting media headers or a suffix", async () => {
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await expect(originals.prepare({ identity: "valid", operationKey: "valid-png", uploads: [upload(PNG)] }))
      .resolves.toBeDefined();
    for (const [identity, value] of [
      ["signature-png", upload(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))],
      ["truncated-jpeg", { ...upload(Buffer.from([255, 216, 255, 224])), extension: ".jpg" as const }],
      ["truncated-video", { ...upload(Buffer.alloc(12)), kind: "video" as const, extension: ".mp4" as const }],
      ["truncated-audio", { ...upload(Buffer.alloc(12)), kind: "audio" as const, extension: ".m4a" as const }],
    ] as const) {
      await expect(originals.prepare({ identity, operationKey: identity, uploads: [value] }))
        .rejects.toThrow("creative-capture-media-not-decodable");
    }
  });

  it("persists collecting when a recorded retake supersedes a ready handoff", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals },
    );
    expect(accepted.status).toBe("ready-for-editorial");
    await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 2);

    const retaken = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "retake", null, 2), { library, originals },
    );

    expect(retaken.steps).toEqual([{ stepId: "step_01", state: "retake-pending", acceptedTake: null }]);
    await expect(library.listCreativeProcessingTasks()).resolves.toEqual([
      expect.objectContaining({ status: "collecting", phase: "capture" }),
    ]);
  });

  it("rejects an old completed take while the current server resolution is retake-pending", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 1);

    await expect(acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG, 1),
      { library, originals },
    )).rejects.toThrow("creative-capture-return-not-current");

    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 2);
    const accepted = await acceptCreativeCapture(
      command("step_01", "watch_sim_01", "watch-director", "completed", PNG, 2),
      { library, originals },
    );
    expect(accepted.steps).toEqual([{
      stepId: "step_01", state: "completed-returned", acceptedTake: 2,
    }]);
  });

  it.each(["putCreativeSessionEvent", "commitCreativeBundle"] as const)(
    "rejects a stale media return through generic %s after take three becomes current", async (boundary) => {
      const library = await preparedLibrary(directory, ["step_01"]);
      await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
      await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 2);
      await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 2);
      const session = (await library.listCreativeSessions())[0]!;
      const stale: CreativeSessionEvent = {
        id: `stale_return_${boundary}`, sessionId: session.id, briefVersion: 1,
        scriptVersion: 1, shotPlanVersion: 1, stepId: "step_01",
        deviceId: "watch_sim_01", deviceRole: "watch-director", action: "media-returned",
        actorId: "designer_01", reason: null, transcript: null,
        idempotencyKey: `stale-return:${boundary}`, take: 2, createdAt: now,
      };
      const before = await readFile(join(directory, "library.json"));

      const write = boundary === "putCreativeSessionEvent"
        ? library.putCreativeSessionEvent(stale)
        : library.commitCreativeBundle({ session, events: [stale], auditEvents: [] });
      await expect(write).rejects.toThrow("creative-capture-return-not-current");
      expect(await readFile(join(directory, "library.json"))).toEqual(before);
      expect((await library.listCreativeSessionEvents()).at(-1)?.action).toBe("retake");
    },
  );

  it.each(["putCreativeSessionEvent", "commitCreativeBundle"] as const)(
    "rejects a stale completed directive through generic %s after take three becomes current", async (boundary) => {
      const library = await preparedLibrary(directory, ["step_01"]);
      await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
      await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 2);
      await record(library, "step_01", "watch_sim_01", "watch-director", "retake", 2);
      const session = (await library.listCreativeSessions())[0]!;
      const stale: CreativeSessionEvent = {
        id: `stale_completed_${boundary}`, sessionId: session.id, briefVersion: 1,
        scriptVersion: 1, shotPlanVersion: 1, stepId: "step_01",
        deviceId: "watch_sim_01", deviceRole: "watch-director", action: "completed",
        actorId: "designer_01", reason: null, transcript: null,
        idempotencyKey: `stale-completed:${boundary}`, take: 2, createdAt: now,
      };
      const before = await readFile(join(directory, "library.json"));

      const write = boundary === "putCreativeSessionEvent"
        ? library.putCreativeSessionEvent(stale)
        : library.commitCreativeBundle({ session, events: [stale], auditEvents: [] });
      let accepted = false;
      try {
        await write;
        accepted = true;
      } catch (error) {
        expect(error).toEqual(expect.objectContaining({ message: "creative-capture-return-not-current" }));
      }
      if (accepted) {
        const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
        const handoffResult = await acceptCreativeCapture(
          command("step_01", "watch_sim_01", "watch-director", "completed", PNG, 2),
          { library, originals },
        );
        expect.soft(handoffResult.steps).not.toEqual([{
          stepId: "step_01", state: "completed-returned", acceptedTake: 2,
        }]);
        await settleCreativeCapture({ creativeSessionId: session.id, actorId: "designer_01",
          operationKey: `settle-stale-${boundary}`, now }, library);
        const editorial = await handoff.processCreativeEditorial({ creativeSessionId: session.id,
          operationKey: `process-stale-${boundary}`, now }, editorialDependencies(library, directory));
        expect.soft(editorial.task.status).not.toBe("reviewable");
        expect.soft(editorial.artifacts).toHaveLength(0);
      }
      expect(accepted).toBe(false);
      expect(await readFile(join(directory, "library.json"))).toEqual(before);
      expect((await library.listCreativeSessionEvents()).at(-1)?.action).toBe("retake");
    }, 300_000,
  );

  it("applies retake directives before later completions in the same atomic bundle", async () => {
    const stale = await preparedLibrary(directory, ["step_01"]);
    await record(stale, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    await record(stale, "step_01", "watch_sim_01", "watch-director", "retake", 1);
    await record(stale, "step_01", "watch_sim_01", "watch-director", "completed", 2);
    const staleSession = (await stale.listCreativeSessions())[0]!;
    const completedTake2 = (await stale.listCreativeSessionEvents()).at(-1)!;
    const before = await readFile(join(directory, "library.json"));
    await expect(stale.commitCreativeBundle({ session: staleSession, auditEvents: [], events: [
      { ...completedTake2, id: "bundle_retake_take_2", idempotencyKey: "bundle-retake-take-2", action: "retake" },
      { ...completedTake2, id: "bundle_stale_completed_take_2", idempotencyKey: "bundle-stale-completed-take-2" },
    ] })).rejects.toThrow("creative-capture-return-not-current");
    expect(await readFile(join(directory, "library.json"))).toEqual(before);

    const currentDirectory = await mkdtemp(join(tmpdir(), "wearable-current-bundle-"));
    try {
      const current = await preparedLibrary(currentDirectory, ["step_01"]);
      await record(current, "step_01", "watch_sim_01", "watch-director", "completed", 1);
      await record(current, "step_01", "watch_sim_01", "watch-director", "retake", 1);
      await record(current, "step_01", "watch_sim_01", "watch-director", "completed", 2);
      const currentSession = (await current.listCreativeSessions())[0]!;
      const completed = (await current.listCreativeSessionEvents()).at(-1)!;
      await expect(current.commitCreativeBundle({ session: currentSession, auditEvents: [], events: [
        { ...completed, id: "bundle_current_retake_take_2", idempotencyKey: "bundle-current-retake-take-2", action: "retake" },
        { ...completed, id: "bundle_current_completed_take_3", idempotencyKey: "bundle-current-completed-take-3", take: 3 },
      ] })).resolves.toBeUndefined();
      expect((await current.listCreativeSessionEvents()).slice(-2).map((event) => [event.action, event.take]))
        .toEqual([["retake", 2], ["completed", 3]]);
    } finally {
      await rm(currentDirectory, { recursive: true, force: true });
    }
  });

  it("keeps retake pending when a legacy stale media return appears later in the event log", async () => {
    const library = await preparedLibrary(directory, ["step_01"]);
    const plan = (await library.listShotPlans())[0]!;
    const session = (await library.listCreativeSessions())[0]!;
    const events: CreativeSessionEvent[] = [
      { id: "completed_take_2", sessionId: session.id, briefVersion: 1, scriptVersion: 1,
        shotPlanVersion: 1, stepId: "step_01", deviceId: "watch_sim_01", deviceRole: "watch-director",
        action: "completed", actorId: "designer_01", reason: null, transcript: null,
        idempotencyKey: "completed-take-2", take: 2, createdAt: now },
      { id: "retake_take_2", sessionId: session.id, briefVersion: 1, scriptVersion: 1,
        shotPlanVersion: 1, stepId: "step_01", deviceId: "watch_sim_01", deviceRole: "watch-director",
        action: "retake", actorId: "designer_01", reason: null, transcript: null,
        idempotencyKey: "retake-take-2", take: 2, createdAt: now },
      { id: "legacy_stale_return_take_2", sessionId: session.id, briefVersion: 1, scriptVersion: 1,
        shotPlanVersion: 1, stepId: "step_01", deviceId: "watch_sim_01", deviceRole: "watch-director",
        action: "media-returned", actorId: "designer_01", reason: null, transcript: null,
        idempotencyKey: "legacy-stale-return-take-2", take: 2, createdAt: now },
    ];
    const staleAsset = {
      id: "legacy_stale_asset", batchId: "creative_batch_creative_01", sessionId: "capture_01",
      digest: "legacy-digest", localPath: join(directory, "legacy-stale.mp4"), mediaKind: "video" as const,
      importedAt: now, permissionState: "allowed" as const, origin: "new" as const, technicalFlags: [],
      captureContext: { creativeSessionId: session.id, stepId: "step_01", deviceId: "watch_sim_01",
        deviceRole: "watch-director" as const, carrier: "watch", scriptVersion: 1, shotPlanVersion: 1,
        take: 2, stepOutcome: "completed" as const },
    };

    expect(handoff.resolveCreativeSteps(plan, events, [staleAsset])).toEqual([
      { stepId: "step_01", state: "retake-pending", acceptedTake: null },
    ]);
  });

  it("accumulates accepted steps and settles completed-and-skipped or all-skipped sessions", async () => {
    const library = await preparedLibrary(directory, ["step_01", "step_02"]);
    const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
    await record(library, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    const afterFirst = await acceptCreativeCapture(command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library, originals });
    await record(library, "step_02", "watch_sim_01", "watch-director", "completed", 1);
    const afterSecond = await acceptCreativeCapture(command("step_02", "watch_sim_01", "watch-director", "completed", PNG), { library, originals });

    expect(afterFirst.status).toBe("collecting");
    expect(afterSecond.steps.map(({ state }) => state)).toEqual([
      "completed-returned",
      "completed-returned",
    ]);

    const mixedDirectory = await mkdtemp(join(tmpdir(), "wearable-creative-handoff-mixed-"));
    const mixed = await preparedLibrary(mixedDirectory, ["step_01", "step_02"]);
    const mixedOriginals = new LocalCreativeOriginalStore(join(mixedDirectory, "originals"));
    await record(mixed, "step_01", "watch_sim_01", "watch-director", "completed", 1);
    await acceptCreativeCapture(command("step_01", "watch_sim_01", "watch-director", "completed", PNG), { library: mixed, originals: mixedOriginals });
    await record(mixed, "step_02", "watch_sim_01", "watch-director", "skipped", 1);
    const completedAndSkipped = await acceptCreativeCapture(command("step_02", "watch_sim_01", "watch-director", "skipped", null), { library: mixed, originals: mixedOriginals });
    expect(completedAndSkipped.status).toBe("ready-for-editorial");

    const skippedDirectory = await mkdtemp(join(tmpdir(), "wearable-creative-handoff-skipped-"));
    const skipped = await preparedLibrary(skippedDirectory, ["step_01"]);
    await record(skipped, "step_01", "watch_sim_01", "watch-director", "skipped", 1);
    await acceptCreativeCapture(command("step_01", "watch_sim_01", "watch-director", "skipped", null), { library: skipped, originals: new LocalCreativeOriginalStore(join(skippedDirectory, "originals")) });
    const allSkipped = await settleCreativeCapture({ creativeSessionId: "creative_01", actorId: "designer_01", operationKey: "settle-all-skipped", now }, skipped);
    expect(allSkipped.status).toBe("no-assets");
    expect((await skipped.listCreativeSessions()).filter((s) => s.stage === "shooting")).toHaveLength(0);
    await rm(mixedDirectory, { recursive: true, force: true });
    await rm(skippedDirectory, { recursive: true, force: true });
  });
});

const editorialInput = { creativeSessionId: "creative_01", operationKey: "editorial-01", now };

async function editorialLibrary(directory: string, stepIds: string[]) {
  const library = await preparedLibrary(directory, stepIds);
  const path = join(directory, "source.mp4");
  await runFfmpeg(["-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=25", "-t", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", path]);
  const originals = new LocalCreativeOriginalStore(join(directory, "originals"));
  for (const stepId of stepIds) {
    await record(library, stepId, "watch_sim_01", "watch-director", "completed", 1);
    await acceptCreativeCapture({ ...command(stepId, "watch_sim_01", "watch-director", "completed", null),
      uploads: [{ bytes: await readFile(path), kind: "video", extension: ".mp4", deviceModel: "A-0 synthetic local video" }],
    }, { library, originals });
  }
  await settleCreativeCapture({ ...editorialInput, actorId: "designer_01" }, library);
  return library;
}

async function seedHistory(library: JsonContentStore, directory: string) {
  const original = (await library.listAssets())[0]!;
  const sample = (await library.listSegments())[0]!;
  for (const id of ["history_rework_01", "history_rework_02"]) {
    await library.putAsset({ ...original, id: `asset_${id}`, sessionId: `capture_${id}`, origin: "historical", captureContext: undefined,
      batchId: "historical_batch", localPath: join(directory, "source.mp4") });
    await library.putSegments([{ ...sample, id, assetId: `asset_${id}`, batchId: "historical_batch" }]);
  }
  await library.putCreativeIntent({ id: "intent_01", sessionId: "creative_01", version: 1, platforms: ["editorial-master"],
    audience: "A-0 fixture", theme: "craft", products: [], shootConditions: [], prohibitedAreas: [], tone: "clear", durationSeconds: 5, createdAt: now });
}

async function authorizeHistory(library: JsonContentStore, selectedSegmentIds: string[]) {
  const input = { sessionId: "creative_01", actorId: "customer_01", now };
  const set = await suggestPostShootHistoricalMaterials(input, {
    rank: async ({ segments }) => segments.map((segment) => ({ segmentId: segment.id, reason: "craft evidence", score: 1 })),
  }, library);
  await decidePostShootMaterialViewing({ ...input, suggestionSetId: set.id, decision: "accept" }, library);
  await confirmPostShootMaterialSelection({ ...input, suggestionSetId: set.id, selectedSegmentIds }, library);
}

async function approveHistoricalSegment(library: JsonContentStore, directory: string, segmentId: string) {
  const story = { id: `other_session_story_${segmentId}`, batchId: "other_session_batch",
    type: "persona-monologue" as const, anchorSegmentIds: [segmentId], historicalSegmentIds: [],
    historicalReasons: {}, status: "candidate" as const };
  const variants = (["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const).map((kind, index) => ({
    kind, timeline: [{ segmentId, inMs: 0, outMs: 400 + index * 100, role: "anchor" as const }],
    title: "Other session usage", caption: "A-0 usage mutation fixture", cover: { segmentId, atMs: 100 },
  }));
  const revision = { id: `other_session_revision_${segmentId}`, storyId: story.id, revision: 1,
    parentRevisionId: null, designerPrompt: null, changeExplanation: null, variants,
    selectedStills: [], createdAt: now };
  await library.putStories([story]);
  await library.putRevision(revision);
  const rendered = await renderRevision(revision, await library.listSegments(), await library.listAssets(),
    join(directory, "other-session-renders"));
  for (const artifact of rendered.artifacts) await library.putArtifact(artifact);
  const artifact = rendered.artifacts.find((item) => item.kind === "editorial-master")!;
  await approveVariant({ storyId: story.id, revisionId: revision.id, revision: 1,
    artifactId: artifact.id, variant: artifact.kind, reviewerId: "other_designer" }, library);
}

function editorialDependencies(library: JsonContentStore, directory: string) {
  const tagAnalyzer: SegmentTagAnalyzer = { suggest: async (segment) => ({
    baseTypes: ["persona-copy", "handmade"], people: [], products: [], processStages: ["making"],
    topics: ["craft"], emotions: ["hesitation", "persistence"],
    transcript: segment.transcript, technicalFlags: [], usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
  }) };
  const storyAnalyzer: StoryAnalyzer = { propose: async (segments) => [
    { type: "persona-monologue", anchorSegmentIds: segments.map((segment) => segment.id) },
    { type: "inner-journey", anchorSegmentIds: segments.map((segment) => segment.id) },
    { type: "product-journey", anchorSegmentIds: segments.map((segment) => segment.id) },
  ] };
  const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
    const variant = (outMs: number) => ({
      timeline: [...story.anchorSegmentIds, ...story.historicalSegmentIds].map((segmentId) => ({ segmentId, inMs: 0, outMs, role: "anchor" as const })),
      title: "A-0 local cut", caption: "Synthetic fixture", cover: { segmentId: story.anchorSegmentIds[0]!, atMs: 100 },
    });
    return { master: variant(500), xiaohongshu: variant(700), douyin: variant(800),
      wechatChannels: variant(900), stills: [] };
  } };
  return { library, outputDir: join(directory, "renders"),
    segmentAnalyzer: { analyze: async () => [{ startMs: 0, endMs: 1_200,
      transcript: "我认为手工应该保留痕迹，我决定坚持再做一次", signals: [] }] },
    tagAnalyzer, storyAnalyzer, editAnalyzer, renderer: renderRevision };
}

async function probeReadableVideo(path: string): Promise<void> {
  await runFfmpeg(["-v", "error", "-i", path, "-f", "null", "-"]);
}

async function runFfmpeg(args: string[]): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(ffmpegPath!, args, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (data) => { stderr += String(data); });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(stderr)));
  });
}

function sha256(bytes: Buffer): string { return createHash("sha256").update(bytes).digest("hex"); }

function upload(bytes: Buffer) {
  return { bytes, kind: "image" as const, extension: ".png" as const, deviceModel: "A-0 simulated phone" };
}

function command(stepId: string, deviceId: string, deviceRole: "watch-director" | "main-stone-a", stepOutcome: "completed" | "skipped" | "retake", bytes: Buffer | null, take = 1) {
  return {
    creativeSessionId: "creative_01", stepId, deviceId, deviceRole,
    carrier: deviceRole === "watch-director" ? "watch" : "ring", take, stepOutcome,
    captureSession: { id: "capture_01", startedAt: now, mode: "creator-necklace" as const, privacyState: "creator" as const, visitorConsent: "not-required" as const, status: "stopped" as const, productRef: null },
    batchId: "creative_batch_creative_01", uploads: bytes ? [upload(bytes)] : [],
    actorId: "designer_01", operationKey: `capture-${stepId}-${deviceId}-${stepOutcome}`, now,
  };
}

function sessionPrecondition(session: CreativeSession) {
  return {
    id: session.id,
    stage: session.stage,
    currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion,
    currentShotPlanVersion: session.currentShotPlanVersion,
  };
}

async function preparedLibrary(directory: string, stepIds: string[]) {
  const library = await JsonContentStore.open(join(directory, "library.json"));
  const session: CreativeSession = { id: "creative_01", customerId: "customer_01", title: "Creative handoff", stage: "shooting", currentIntentVersion: 1, currentBriefVersion: 1, currentScriptVersion: 1, currentShotPlanVersion: 1, materialViewDecisionIds: [], materialUseDecisionIds: [], deviceBindings: [{ deviceId: "watch_sim_01", deviceRole: "watch-director", transport: "simulated" }, { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", transport: "simulated" }], captureBatchIds: [], editorialBatchId: null, privacyMode: "creator", prohibitedAreas: [], status: "active", createdAt: now, updatedAt: now };
  const plan: ShotPlan = { id: "plan_01", sessionId: session.id, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1, status: "active", steps: stepIds.map((id, index) => ({ id, order: index + 1, purpose: "Capture", expressionSuggestion: "Capture", visualDirection: "Close", durationSeconds: 1, mustCapture: ["asset"], optionalGapSuggestions: [], prohibitedCapture: [], completionCriteria: ["usable"], deviceRoles: ["watch-director", "main-stone-a"] })), createdAt: now };
  await library.putCreativeSession(session); await library.putShotPlan(plan);
  for (const stepId of stepIds) {
    const card: DirectorCard = { id: `card_${stepId}`, sessionId: session.id, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1, stepId, status: "active", purpose: "Capture", spokenPrompt: "Capture", shotDescription: "Close", durationSeconds: 1, mustCapture: ["asset"], prohibitedCapture: [], availableActions: ["completed", "retake", "skipped"], createdAt: now };
    const brief: DeviceBrief = { id: `brief_${stepId}`, sessionId: session.id, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1, stepId, status: "active", deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", carrier: "ring", startCondition: "start", endCondition: "end", composition: "close", prohibitedAreas: [], privacyState: "creator", createdAt: now };
    await library.putDirectorCard(card); await library.putDeviceBrief(brief);
  }
  return library;
}

async function record(library: JsonContentStore, stepId: string, deviceId: string, deviceRole: "watch-director" | "main-stone-a", action: "completed" | "skipped" | "retake", take: number) {
  const event: CreativeSessionEvent = { id: `action_${stepId}_${deviceId}_${action}_${take}`, sessionId: "creative_01", briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1, stepId, deviceId, deviceRole, action, actorId: "designer_01", reason: action === "skipped" ? "customer chose skip" : null, transcript: null, idempotencyKey: `action:${stepId}:${deviceId}:${action}:${take}`, take, createdAt: now };
  await library.putCreativeSessionEvent(event);
}

async function pausedPrivacyLibrary(directory: string): Promise<JsonContentStore> {
  const library = await JsonContentStore.open(join(directory, "privacy-library.json"));
  const session: CreativeSession = {
    id: "creative_privacy_01", customerId: "customer_01", title: "Privacy resume",
    stage: "paused", currentIntentVersion: 1, currentBriefVersion: 1,
    currentScriptVersion: 1, currentShotPlanVersion: 1,
    materialViewDecisionIds: [], materialUseDecisionIds: [], captureBatchIds: [],
    editorialBatchId: null, privacyMode: "paused", prohibitedAreas: [], status: "active",
    deviceBindings: [
      { deviceId: "watch_sim_01", deviceRole: "watch-director", transport: "simulated" },
      { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", transport: "simulated" },
      { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", transport: "simulated" },
    ],
    createdAt: now, updatedAt: now,
  };
  await library.putCreativeSession(session);
  await library.putAuditEvent({
    id: "audit_privacy_pause_01", entityType: "creative-session", entityId: session.id,
    actorId: "customer_01", action: "creative-session-transitioned", reason: "privacy pause",
    before: { stage: "shooting" }, after: { stage: "paused" }, createdAt: now,
  });
  return library;
}

function privacyDevice(deviceId: string, failResume = false) {
  let paused = true;
  return {
    deviceId,
    pausePrivacy() { paused = true; },
    resumePrivacy() {
      if (failResume) throw new Error(`resume-failed:${deviceId}`);
      paused = false;
    },
    isPrivacyPaused() { return paused; },
  };
}

function deferred<T>() {
  let resolve!: (value?: T) => void;
  const promise = new Promise<T>((done) => { resolve = done as (value?: T) => void; });
  return { promise, resolve };
}
import { spawn } from "node:child_process";
