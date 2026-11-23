import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type {
  CaptureSession,
  ContentSegment,
  CreativeSession,
  DeviceBrief,
  DirectorCard,
  LibraryAsset,
  ScriptPackage,
  ShotPlan,
} from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ingestCreativeCapture } from "../src/creative-ingest";
import { ingestNewBatch } from "../src/batch-ingest";
import { canonicalCaptureContextKey } from "../src/asset-identity";
import { importGlass3LocalOriginal } from "../src/glass3-import-adapter";
import { JsonContentStore } from "../src/json-content-store";
import { segmentNewBatch } from "../src/semantic-segmenter";
import { decomposeStories } from "../src/story-decomposer";
import { recordCreativeSessionAction } from "../src/creative-action";

const NOW = "2026-09-05T10:00:00.000Z";
const CREATIVE_SESSION_ID = "creative_session_capture_01";

describe("creative capture ingest", () => {
  let testDirectory: string;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-creative-ingest-"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("stores exact capture provenance, preserves a distinct take, and keeps the editorial batch compatible", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "take.mp4", "same captured bytes");
    const first = await ingestCreativeCapture(captureInput(media), library);
    const retry = await ingestCreativeCapture(captureInput(media), library);
    await library.putCreativeSessionEvent({ ...eventFor("step_01", "retake", 1), id: "event_step_01_retake_take_1" });
    await library.putCreativeSessionEvent({ ...eventFor("step_01", "completed", 2), id: "event_step_01_completed_take_2" });
    const secondTake = await ingestCreativeCapture(captureInput(media, { take: 2 }), library);

    expect(first).toHaveLength(1);
    expect(retry.map((asset) => asset.id)).toEqual(first.map((asset) => asset.id));
    expect(secondTake).toHaveLength(1);
    expect(secondTake[0]!.id).not.toBe(first[0]!.id);
    expect(first[0]!.captureContext).toEqual({
      creativeSessionId: CREATIVE_SESSION_ID,
      stepId: "step_01",
      deviceId: "stone_a_sim_01",
      deviceRole: "main-stone-a",
      carrier: "pendant",
      scriptVersion: 1,
      shotPlanVersion: 1,
      take: 1,
      stepOutcome: "completed",
    });
    expect((await library.listAssets()).map((asset) => asset.captureContext?.take)).toEqual([1, 2]);
    expect((await library.listCreativeSessionEvents()).find((event) =>
      event.action === "media-returned" && event.take === 1,
    )).toBeDefined();

    const segments = await segmentNewBatch("batch_capture_01", first, {
      analyze: async () => [{ startMs: 0, endMs: 1, transcript: "" , signals: ["pause"] }],
    }, library);
    expect(segments[0]!.batchId).toBe("batch_capture_01");
    expect((await library.listAssets())[0]!.captureContext?.creativeSessionId)
      .toBe(CREATIVE_SESSION_ID);
    const editorialSegment: ContentSegment = {
      ...segments[0]!,
      tags: {
        baseTypes: ["product-image"], people: [],
        products: [{ id: "pendant_01", status: "confirmed" }],
        processStages: ["repair", "polish"], topics: [], emotions: [], transcript: "",
        technicalFlags: [], usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
      },
    };
    const stories = await decomposeStories({
      batchId: "batch_capture_01", authorization: { selectedSegmentIds: [] }, newSegments: [editorialSegment], historicalSegments: [],
    }, {
      propose: async () => [{ type: "product-journey", anchorSegmentIds: [editorialSegment.id] }],
    }, library);
    expect(stories.map((story) => story.batchId)).toEqual(["batch_capture_01"]);
  });

  it("moves from shooting to ingesting, then waits for every non-skipped step before editorial review", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "stage.mp4", "stage bytes");

    await ingestCreativeCapture(captureInput(media), library);
    expect((await creativeSession(library)).stage).toBe("ingesting");

    await returnSkippedSecondStep(library);
    expect((await creativeSession(library)).stage).toBe("editorial_review");
  });

  it("keeps the remaining director steps actionable after the first completed media return", async () => {
    const library = await seededLibrary(testDirectory);
    const first = await importedMedia(testDirectory, "first-step.mp4", "first step bytes");
    await ingestCreativeCapture(captureInput(first), library);
    expect((await creativeSession(library)).stage).toBe("ingesting");

    await expect(recordCreativeSessionAction({
      sessionId: CREATIVE_SESSION_ID, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1,
      stepId: "step_02", action: "completed", actorId: "director_01",
      deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", reason: null,
      transcript: null, operationId: "complete-step-two-0001", now: NOW,
    }, { draft: async () => { throw new Error("unused"); }, revise: async () => { throw new Error("unused"); } }, library))
      .resolves.toMatchObject({ event: { stepId: "step_02", action: "completed" } });

    const second = await importedMedia(testDirectory, "second-step.mp4", "second step bytes");
    await ingestCreativeCapture(captureInput(second, { stepId: "step_02" }), library);
    expect((await creativeSession(library)).stage).toBe("editorial_review");
  });

  it("closes an all-skipped plan into a traceable no-assets editorial handoff", async () => {
    const library = await seededLibrary(testDirectory, { actions: false });
    for (const stepId of ["step_01", "step_02"]) {
      await library.putCreativeSessionEvent(eventFor(stepId, "skipped"));
      await ingestCreativeCapture(captureInput([], { stepId, stepOutcome: "skipped" }), library);
    }
    const session = await creativeSession(library);
    expect(session.stage).toBe("editorial_review");
    expect(session.editorialBatchId).toBe("no-assets:batch_capture_01");
    expect((await library.listAuditEvents(session.id)).at(-1)?.reason)
      .toBe("all planned steps skipped; editorial handoff has no captured assets");
  });

  it("returns an identical ingesting retry with a later request timestamp as a no-op", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "ingesting-retry.mp4", "ingesting retry bytes");
    const first = await ingestCreativeCapture(captureInput(media), library);
    const sessionBefore = await creativeSession(library);
    const eventsBefore = await library.listCreativeSessionEvents();
    const auditsBefore = await library.listAuditEvents();

    await expect(ingestCreativeCapture({
      ...captureInput(media),
      now: "2026-09-05T11:00:00.000Z",
    }, library)).resolves.toEqual(first);
    expect(await creativeSession(library)).toEqual(sessionBefore);
    expect(await library.listCreativeSessionEvents()).toEqual(eventsBefore);
    expect(await library.listAuditEvents()).toEqual(auditsBefore);
  });

  it("returns an exact skipped retry during ingesting as a zero-write no-op", async () => {
    const library = await seededLibrary(testDirectory);
    await moveToIngestingWithSeedBatch(library);
    await library.putCreativeSessionEvent(eventFor("step_02", "skipped"));
    const input = captureInput([], { stepId: "step_02", stepOutcome: "skipped" });
    await ingestCreativeCapture(input, library);
    const sessionBefore = await creativeSession(library);
    const eventsBefore = await library.listCreativeSessionEvents();
    const auditsBefore = await library.listAuditEvents();
    const assetsBefore = await library.listAssets();

    await expect(ingestCreativeCapture({
      ...input,
      now: "2026-09-05T11:00:00.000Z",
    }, library)).resolves.toEqual([]);
    expect(await creativeSession(library)).toEqual(sessionBefore);
    expect(await library.listCreativeSessionEvents()).toEqual(eventsBefore);
    expect(await library.listAuditEvents()).toEqual(auditsBefore);
    expect(await library.listAssets()).toEqual(assetsBefore);
  });

  it("returns an exact skipped retry during editorial review as a zero-write no-op", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "skipped-terminal.mp4", "terminal setup bytes");
    await ingestCreativeCapture(captureInput(media), library);
    await returnSkippedSecondStep(library);
    const input = captureInput([], { stepId: "step_02", stepOutcome: "skipped" });
    const sessionBefore = await creativeSession(library);
    const eventsBefore = await library.listCreativeSessionEvents();
    const auditsBefore = await library.listAuditEvents();
    const assetsBefore = await library.listAssets();

    await expect(ingestCreativeCapture({
      ...input,
      now: "2026-09-05T11:00:00.000Z",
    }, library)).resolves.toEqual([]);
    expect(await creativeSession(library)).toEqual(sessionBefore);
    expect(await library.listCreativeSessionEvents()).toEqual(eventsBefore);
    expect(await library.listAuditEvents()).toEqual(auditsBefore);
    expect(await library.listAssets()).toEqual(assetsBefore);
  });

  it("rejects an empty-media terminal near-miss when skipped provenance changes", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "skipped-near-miss.mp4", "near miss setup bytes");
    await ingestCreativeCapture(captureInput(media), library);
    await returnSkippedSecondStep(library);
    const input = captureInput([], { stepId: "step_02", stepOutcome: "skipped" });
    const sessionBefore = await creativeSession(library);
    const eventsBefore = await library.listCreativeSessionEvents();
    const auditsBefore = await library.listAuditEvents();
    const assetsBefore = await library.listAssets();

    await expect(ingestCreativeCapture({
      ...input,
      batchId: "batch_capture_provenance_mismatch",
    }, library)).rejects.toThrow("creative-capture-not-accepted");
    expect(await creativeSession(library)).toEqual(sessionBefore);
    expect(await library.listCreativeSessionEvents()).toEqual(eventsBefore);
    expect(await library.listAuditEvents()).toEqual(auditsBefore);
    expect(await library.listAssets()).toEqual(assetsBefore);
  });

  it("rejects an ingesting retry whose matching ID has newly damaged local bytes", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "ingesting-damage.mp4", "ingesting clean bytes");
    await ingestCreativeCapture(captureInput(media), library);
    const assetsBefore = await library.listAssets();
    const sessionBefore = await creativeSession(library);
    await writeFile(media.localPath, "newly damaged bytes");

    await expect(ingestCreativeCapture(captureInput(media), library))
      .rejects.toThrow("creative-capture-retry-identity-mismatch");
    expect(await library.listAssets()).toEqual(assetsBefore);
    expect(await creativeSession(library)).toEqual(sessionBefore);
  });

  it("returns an exact network retry after editorial review without duplicating the event or audit", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "terminal-retry.mp4", "terminal retry bytes");
    const first = await ingestCreativeCapture(captureInput(media), library);
    await returnSkippedSecondStep(library);
    const eventCount = (await library.listCreativeSessionEvents()).length;
    const auditCount = (await library.listAuditEvents()).length;

    await expect(ingestCreativeCapture(captureInput(media), library)).resolves.toEqual(first);
    expect(await library.listAssets()).toHaveLength(1);
    expect(await library.listCreativeSessionEvents()).toHaveLength(eventCount);
    expect(await library.listAuditEvents()).toHaveLength(auditCount);
  });

  it("accepts a skipped step only without media and rejects a privacy-paused capture session", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "skipped.mp4", "skipped bytes");
    await library.putCreativeSessionEvent(eventFor("step_01", "skipped"));

    await expect(ingestCreativeCapture(captureInput([], { stepOutcome: "skipped" }), library))
      .resolves.toEqual([]);
    await expect(ingestCreativeCapture(captureInput([media], { stepOutcome: "skipped" }), library))
      .rejects.toThrow("skipped-step-media-not-accepted");
    await expect(ingestCreativeCapture(captureInput([media], {
      captureSession: { ...captureSession(), privacyState: "paused", status: "stopped" },
    }), library)).rejects.toThrow("privacy-paused-session-not-ingestable");
  });

  it("retains a checksum-damaged return as evidence with a technical flag", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "damaged.mp4", "original bytes");
    await writeFile(media.localPath, "damaged bytes");

    const [asset] = await ingestCreativeCapture(captureInput(media), library);
    expect(asset.technicalFlags).toContain("glass3-path-digest-mismatch");
    expect((await library.listAssets()).map((item) => item.id)).toContain(asset.id);
  });

  it("does not retain an earlier asset when a later medium is invalid", async () => {
    const library = await seededLibrary(testDirectory);
    const first = await importedMedia(testDirectory, "first.mp4", "first bytes");
    const second = await importedMedia(testDirectory, "second.mp4", "second bytes");
    const input = captureInput([first, second]);
    delete input.mediaKinds[second.id];
    const eventCount = (await library.listCreativeSessionEvents()).length;

    await expect(ingestCreativeCapture(input, library)).rejects.toThrow("media-kind-required");
    expect(await library.listAssets()).toEqual([]);
    expect((await creativeSession(library)).captureBatchIds).toEqual([]);
    expect(await library.listCreativeSessionEvents()).toHaveLength(eventCount);
  });

  it("does not retain media when its final creative transaction fails", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "commit-fail.mp4", "commit failure bytes");
    const eventCount = (await library.listCreativeSessionEvents()).length;
    vi.spyOn(library, "commitCreativeBundle").mockRejectedValueOnce(new Error("commit-failed"));

    await expect(ingestCreativeCapture(captureInput(media), library)).rejects.toThrow("commit-failed");
    expect(await library.listAssets()).toEqual([]);
    expect((await creativeSession(library)).stage).toBe("shooting");
    expect(await library.listCreativeSessionEvents()).toHaveLength(eventCount);
  });

  it("keeps both concurrent capture batches instead of overwriting the earlier batch state", async () => {
    const library = await seededLibrary(testDirectory);
    const first = await importedMedia(testDirectory, "concurrent-a.mp4", "concurrent a");
    const second = await importedMedia(testDirectory, "concurrent-b.mp4", "concurrent b");
    await library.putCreativeSessionEvent(eventFor("step_02", "completed"));

    await expect(Promise.all([
      ingestCreativeCapture({ ...captureInput(first), batchId: "batch_capture_a" }, library),
      ingestCreativeCapture({ ...captureInput(second, { stepId: "step_02" }), batchId: "batch_capture_b" }, library),
    ])).resolves.toHaveLength(2);
    expect((await creativeSession(library)).captureBatchIds.sort())
      .toEqual(["batch_capture_a", "batch_capture_b"]);
    expect(await library.listAssets()).toHaveLength(2);
  });

  it("merges two concurrent ingesting batches with the existing batch rather than dropping one", async () => {
    const library = await seededLibrary(testDirectory);
    await moveToIngestingWithSeedBatch(library);
    const first = await importedMedia(testDirectory, "ingesting-a.mp4", "ingesting a");
    const second = await importedMedia(testDirectory, "ingesting-b.mp4", "ingesting b");
    await library.putCreativeSessionEvent(eventFor("step_02", "completed"));

    await expect(Promise.all([
      ingestCreativeCapture({ ...captureInput(first), batchId: "batch_a" }, library),
      ingestCreativeCapture({ ...captureInput(second, { stepId: "step_02" }), batchId: "batch_b" }, library),
    ])).resolves.toHaveLength(2);
    expect((await creativeSession(library)).captureBatchIds.sort())
      .toEqual(["batch_a", "batch_b", "seed_batch"]);
    expect(await library.listAssets()).toHaveLength(2);
  });

  it("rejects caller-declared capture facts without a matching recorded device action", async () => {
    const library = await seededLibrary(testDirectory, { actions: false });
    const media = await importedMedia(testDirectory, "unrecorded.mp4", "unrecorded bytes");

    await expect(ingestCreativeCapture(captureInput(media), library))
      .rejects.toThrow("creative-capture-action-not-recorded");
    expect(await library.listAssets()).toEqual([]);
  });

  it("rejects a forged take or outcome even when the step and device are otherwise valid", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "forged-action.mp4", "forged action bytes");

    await expect(ingestCreativeCapture(captureInput(media, { take: 2 }), library))
      .rejects.toThrow("creative-capture-action-not-recorded");
    await expect(ingestCreativeCapture(captureInput(media, { stepOutcome: "retake" }), library))
      .rejects.toThrow("creative-capture-action-not-recorded");
    await expect(ingestCreativeCapture({ ...captureInput(media), carrier: "forged-carrier" }, library))
      .rejects.toThrow("creative-capture-carrier-mismatch");
  });

  it("rejects a legacy take two return after the recorded retake makes take three current", async () => {
    const library = await seededLibrary(testDirectory, { actions: false });
    for (const event of [
      eventFor("step_01", "completed", 1),
      eventFor("step_01", "retake", 1),
      { ...eventFor("step_01", "completed", 2), id: "event_step_01_completed_take_2" },
      { ...eventFor("step_01", "retake", 2), id: "event_step_01_retake_take_2" },
    ]) await library.putCreativeSessionEvent(event);
    const stale = await importedMedia(testDirectory, "stale-take-two.mp4", "stale take two bytes");

    await expect(ingestCreativeCapture(captureInput(stale, { take: 2 }), library))
      .rejects.toThrow("creative-capture-return-not-current");
    expect(await library.listAssets()).toEqual([]);
    expect((await library.listCreativeSessionEvents()).filter((event) => event.action === "media-returned"))
      .toEqual([]);

    const first = await importedMedia(testDirectory, "current-take-three-a.mp4", "current take three a");
    const second = await importedMedia(testDirectory, "current-take-three-b.mp4", "current take three b");
    await library.putCreativeSessionEvent({
      ...eventFor("step_01", "completed", 3), id: "event_step_01_completed_take_3",
    });
    const accepted = await ingestCreativeCapture(captureInput([first, second], { take: 3 }), library);
    const replay = await ingestCreativeCapture(captureInput([first, second], { take: 3 }), library);
    expect(accepted).toHaveLength(2);
    expect(replay).toEqual(accepted);
    expect(await library.listAssets()).toHaveLength(2);
  });

  it("uses every capture-context field as stable asset identity, including a new script and plan version", async () => {
    const library = await JsonContentStore.open(join(testDirectory, "identity-library.json"));
    const media = await importedMedia(testDirectory, "identity.mp4", "identity bytes");
    const context = {
      creativeSessionId: CREATIVE_SESSION_ID,
      stepId: "step_01",
      deviceId: "stone_a_sim_01",
      deviceRole: "main-stone-a" as const,
      carrier: "pendant",
      scriptVersion: 1,
      shotPlanVersion: 1,
      take: 1,
      stepOutcome: "completed" as const,
    };
    const batch = {
      batchId: "batch_identity_01", session: captureSession(), media: [media],
      mediaKinds: { [media.id]: "video" as const }, captureContext: context,
    };

    const first = await ingestNewBatch(batch, library);
    const nextContext = { ...context, scriptVersion: 2, shotPlanVersion: 2 };
    const second = await ingestNewBatch({ ...batch, captureContext: nextContext }, library);

    expect(first.assets[0]!.id).not.toBe(second.assets[0]!.id);
    expect(canonicalCaptureContextKey(context)).not.toBe(canonicalCaptureContextKey(nextContext));
    expect(await library.listAssets()).toHaveLength(2);
  });

  it("records distinct return event and audit lineage when the same capture repeats on script and plan version two", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "versioned-return.mp4", "versioned bytes");
    await moveToIngestingWithSeedBatch(library);
    await ingestCreativeCapture(captureInput(media), library);
    await advanceToVersionTwo(library);
    await library.putCreativeSessionEvent({ ...eventFor("step_01", "completed"),
      id: "event_step_01_completed_v2", idempotencyKey: "step_01:completed:1:v2",
      scriptVersion: 2, shotPlanVersion: 2,
    });

    await ingestCreativeCapture(captureInput(media), library);
    const returned = (await library.listCreativeSessionEvents()).filter((event) =>
      event.action === "media-returned");
    const audits = (await library.listAuditEvents()).filter((event) =>
      event.action === "creative-capture-returned");
    expect(returned.map((event) => event.scriptVersion).sort()).toEqual([1, 2]);
    expect(new Set(returned.map((event) => event.idempotencyKey)).size).toBe(2);
    expect(new Set(audits.map((event) => event.id)).size).toBe(2);
  });

  it("requires watch media to match a trusted active director card and fixed watch carrier", async () => {
    const library = await seededLibrary(testDirectory, { actions: false });
    const media = await importedMedia(testDirectory, "watch.mp4", "watch bytes");
    await library.putCreativeSessionEvent({
      ...eventFor("step_01", "completed"),
      id: "event_watch_completed",
      idempotencyKey: "watch:completed:1",
      deviceId: "watch_sim_01",
      deviceRole: "watch-director",
    });
    const input = {
      ...captureInput(media), deviceId: "watch_sim_01", deviceRole: "watch-director" as const,
      carrier: "watch",
    };

    await expect(ingestCreativeCapture(input, library)).resolves.toHaveLength(1);
    await expect(ingestCreativeCapture({ ...input, carrier: "forged-watch" }, library))
      .rejects.toThrow("creative-capture-carrier-mismatch");
  });

  it("does not treat a changed local path or media kind as a terminal exact retry", async () => {
    const library = await seededLibrary(testDirectory);
    const original = await importedMedia(testDirectory, "terminal-original.mp4", "terminal identity bytes");
    await ingestCreativeCapture(captureInput(original), library);
    await returnSkippedSecondStep(library);
    const movedPath = await importedMedia(testDirectory, "terminal-moved.mp4", "terminal identity bytes");

    await expect(ingestCreativeCapture(captureInput(movedPath), library))
      .rejects.toThrow("creative-capture-retry-identity-mismatch");
    await expect(ingestCreativeCapture({
      ...captureInput(original),
      mediaKinds: { [original.id]: "image" },
    }, library)).rejects.toThrow("creative-capture-retry-identity-mismatch");
  });

  it("does not bypass a removed current device binding on a terminal exact retry", async () => {
    const library = await seededLibrary(testDirectory);
    const media = await importedMedia(testDirectory, "removed-binding.mp4", "binding bytes");
    await ingestCreativeCapture(captureInput(media), library);
    await returnSkippedSecondStep(library);
    const prior = await creativeSession(library);
    const withoutStone = {
      ...prior,
      deviceBindings: prior.deviceBindings.filter((binding) => binding.deviceId !== "stone_a_sim_01"),
      updatedAt: "2026-09-05T10:01:00.000Z",
    };
    await library.commitCreativeBundle({
      session: withoutStone,
      auditEvents: [sessionAudit(prior, withoutStone, "remove binding")],
    });

    await expect(ingestCreativeCapture(captureInput(media), library))
      .rejects.toThrow("creative-capture-device-not-assigned");
  });

  it("rejects foreign or context-free assets inside a creative bundle", async () => {
    const library = await seededLibrary(testDirectory);
    const session = await creativeSession(library);
    const foreign: LibraryAsset = {
      id: "asset_foreign", batchId: "batch_foreign", sessionId: "capture_foreign", digest: "digest_foreign",
      localPath: "/tmp/foreign.mp4", mediaKind: "video", importedAt: NOW, permissionState: "allowed",
      origin: "new", technicalFlags: [],
      captureContext: {
        creativeSessionId: "creative_other", stepId: "step_01", deviceId: "stone_a_sim_01",
        deviceRole: "main-stone-a", carrier: "pendant", scriptVersion: 1, shotPlanVersion: 1,
        take: 1, stepOutcome: "completed",
      },
    };
    const contextFree = { ...foreign, id: "asset_context_free", captureContext: undefined };

    await expect(library.commitCreativeBundle({ session, assets: [foreign], auditEvents: [] }))
      .rejects.toThrow("creative-bundle-asset-session-mismatch");
    await expect(library.commitCreativeBundle({ session, assets: [contextFree], auditEvents: [] }))
      .rejects.toThrow("creative-bundle-asset-session-mismatch");
    expect(await library.listAssets()).toEqual([]);
  });
});

async function seededLibrary(
  testDirectory: string,
  options: { actions?: boolean } = {},
): Promise<JsonContentStore> {
  const library = await JsonContentStore.open(join(testDirectory, "library.json"));
  const session: CreativeSession = {
    id: CREATIVE_SESSION_ID,
    customerId: "customer_01",
    title: "Pendant story",
    stage: "shooting",
    currentIntentVersion: 1,
    currentBriefVersion: 1,
    currentScriptVersion: 1,
    currentShotPlanVersion: 1,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [
      { deviceId: "watch_sim_01", deviceRole: "watch-director", transport: "simulated" },
      { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", transport: "simulated" },
    ],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: [],
    status: "active",
    createdAt: NOW,
    updatedAt: NOW,
  };
  const key = { sessionId: session.id, briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1 };
  const script: ScriptPackage = {
    id: "script_01", sessionId: session.id, briefVersion: 1, scriptVersion: 1, status: "active",
    title: "Pendant story", platformDirection: "Local", opening: "Open", narrativeOrder: ["section_01"],
    sections: [{ id: "section_01", expression: "Show pendant", source: "customer-input", referencedSegmentIds: [] }],
    confirmedAt: NOW, createdAt: NOW,
  };
  const plan: ShotPlan = {
    id: "plan_01", ...key, status: "active", createdAt: NOW,
    steps: [shotStep("step_01", 1), shotStep("step_02", 2)],
  };
  const briefs: DeviceBrief[] = ["step_01", "step_02"].map((stepId) => ({
    id: `brief_${stepId}`, ...key, stepId, status: "active", deviceId: "stone_a_sim_01",
    deviceRole: "main-stone-a", carrier: "pendant", startCondition: "start", endCondition: "stop",
    composition: "close-up", prohibitedAreas: [], privacyState: "creator", createdAt: NOW,
  }));
  const cards: DirectorCard[] = ["step_01", "step_02"].map((stepId) => ({
    id: `card_${stepId}`, ...key, stepId, status: "active", purpose: "Show pendant",
    spokenPrompt: "Show it", shotDescription: "close-up", durationSeconds: 3,
    mustCapture: ["pendant"], prohibitedCapture: [],
    availableActions: ["completed", "retake", "skipped"], createdAt: NOW,
  }));
  await library.commitCreativeBundle({
    session,
    scripts: [script],
    shotPlans: [plan],
    directorCards: cards,
    deviceBriefs: briefs,
    events: options.actions === false
      ? []
      : [eventFor("step_01", "completed")],
    auditEvents: [],
  });
  return library;
}

function shotStep(id: string, order: number) {
  return {
    id, order, purpose: "Show pendant", expressionSuggestion: "Show it", visualDirection: "close-up",
    durationSeconds: 3, mustCapture: ["pendant"], optionalGapSuggestions: [], prohibitedCapture: [],
    completionCriteria: ["pendant visible"], deviceRoles: ["watch-director" as const, "main-stone-a" as const],
  };
}

async function importedMedia(testDirectory: string, fileName: string, bytes: string) {
  const localPath = join(testDirectory, fileName);
  const originalBytes = Buffer.from(bytes);
  await writeFile(localPath, originalBytes);
  return importGlass3LocalOriginal({
    localPath,
    originalBytes,
    manifest: {
      origin: "phone-local-original",
      deviceModel: "Rokid Glass3-test-unit",
      importedAt: NOW,
      digest: createHash("sha256").update(originalBytes).digest("hex"),
    },
  });
}

function captureInput(
  media: Awaited<ReturnType<typeof importedMedia>> | Awaited<ReturnType<typeof importedMedia>>[],
  overrides: Partial<{
    take: number;
    stepId: string;
    stepOutcome: "completed" | "retake" | "skipped";
    captureSession: CaptureSession;
  }> = {},
) {
  const returnedMedia = Array.isArray(media) ? media : [media];
  return {
    creativeSessionId: CREATIVE_SESSION_ID,
    stepId: overrides.stepId ?? "step_01",
    deviceId: "stone_a_sim_01",
    deviceRole: "main-stone-a" as const,
    carrier: "pendant",
    take: overrides.take ?? 1,
    stepOutcome: overrides.stepOutcome ?? "completed",
    captureSession: overrides.captureSession ?? captureSession(),
    batchId: "batch_capture_01",
    media: returnedMedia,
    mediaKinds: Object.fromEntries(returnedMedia.map((item) => [item.id, "video" as const])),
    actorId: "director_01",
    now: NOW,
  };
}

function captureSession(): CaptureSession {
  return {
    id: "capture_session_01", startedAt: NOW, mode: "daily-pendant", privacyState: "creator",
    visitorConsent: "not-required", status: "stopped", productRef: "pendant_01",
  };
}

function eventFor(
  stepId: string,
  action: "completed" | "retake" | "skipped",
  take = 1,
) {
  return {
    id: `event_${stepId}_${action}`, sessionId: CREATIVE_SESSION_ID, briefVersion: 1,
    scriptVersion: 1, shotPlanVersion: 1, stepId, deviceId: "stone_a_sim_01",
    deviceRole: "main-stone-a" as const, action, actorId: "director_01", reason: "not needed",
    idempotencyKey: `${stepId}:${action}:${take}`, createdAt: NOW, take,
  };
}

async function creativeSession(library: JsonContentStore): Promise<CreativeSession> {
  return (await library.listCreativeSessions()).find((item) => item.id === CREATIVE_SESSION_ID)!;
}

async function moveToIngestingWithSeedBatch(library: JsonContentStore): Promise<void> {
  const prior = await creativeSession(library);
  const next = {
    ...prior,
    stage: "ingesting" as const,
    captureBatchIds: ["seed_batch"],
    updatedAt: "2026-09-05T10:01:00.000Z",
  };
  await library.commitCreativeBundle({
    session: next,
    auditEvents: [sessionAudit(prior, next, "seed batch")],
  });
}

async function returnSkippedSecondStep(library: JsonContentStore): Promise<void> {
  await library.putCreativeSessionEvent(eventFor("step_02", "skipped"));
  await ingestCreativeCapture(captureInput([], {
    stepId: "step_02",
    stepOutcome: "skipped",
  }), library);
}

async function advanceToVersionTwo(library: JsonContentStore): Promise<void> {
  const prior = await creativeSession(library);
  const priorScript = (await library.listScriptPackages()).find((item) => item.scriptVersion === 1)!;
  const priorPlan = (await library.listShotPlans()).find((item) => item.shotPlanVersion === 1)!;
  const priorCard = (await library.listDirectorCards()).find((item) => item.shotPlanVersion === 1)!;
  const priorBriefs = (await library.listDeviceBriefs()).filter((item) => item.shotPlanVersion === 1);
  const next = {
    ...prior,
    currentScriptVersion: 2,
    currentShotPlanVersion: 2,
    updatedAt: "2026-09-05T10:02:00.000Z",
  };
  await library.commitCreativeBundle({
    session: next,
    scripts: [{ ...priorScript, id: "script_02", scriptVersion: 2, parentScriptId: priorScript.id,
      createdAt: next.updatedAt }],
    shotPlans: [{ ...priorPlan, id: "plan_02", scriptVersion: 2, shotPlanVersion: 2,
      createdAt: next.updatedAt }],
    directorCards: [{ ...priorCard, id: "card_02", scriptVersion: 2, shotPlanVersion: 2,
      createdAt: next.updatedAt }],
    deviceBriefs: priorBriefs.map((brief) => ({ ...brief, id: `${brief.id}_v2`, scriptVersion: 2,
      shotPlanVersion: 2, createdAt: next.updatedAt })),
    auditEvents: [sessionAudit(prior, next, "version two")],
  });
}

function sessionAudit(before: CreativeSession, after: CreativeSession, reason: string) {
  const transitioned = before.stage !== after.stage;
  return {
    id: `audit_${reason.replaceAll(" ", "_")}_${after.updatedAt}`,
    entityType: "creative-session" as const,
    entityId: after.id,
    actorId: "director_01",
    action: transitioned ? "creative-session-transitioned" : "creative-session-updated",
    reason,
    before: transitioned ? { stage: before.stage } : { deviceBindings: before.deviceBindings },
    after: transitioned ? { stage: after.stage } : { deviceBindings: after.deviceBindings },
    createdAt: after.updatedAt,
  };
}
