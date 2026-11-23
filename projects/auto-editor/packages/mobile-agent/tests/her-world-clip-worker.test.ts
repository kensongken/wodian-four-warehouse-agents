import { describe, expect, it, vi } from "vitest";
import type { HerWorldClipHandoffV1 } from "@wearable/contracts";

import { HerWorldClipWorker } from "../src";

const handoff: HerWorldClipHandoffV1 = {
  schemaVersion: 1, kind: "her-world-story-render", jobId: "a".repeat(32), operationKey: "worker-test-1",
  story: { id: "b".repeat(32), revision: 1, place: "斯里兰卡", visitedAt: null, title: "源头的生活",
    ownerConfirmed: true, confirmedAt: "2026-10-25T00:00:00.000Z",
    acts: [{ sequence: 1, stageId: "people", mediaId: "photo-1", storyFunction: "people", heading: "先看见人",
      narration: "真实旁白", sourceBasis: "owner-confirmed", productReference: null }] },
  media: [{ id: "photo-1", order: 0, kind: "image", mimeType: "image/jpeg", byteLength: 10, sha256: "c".repeat(64),
    fetch: { url: `https://her-world.test/api/clip/jobs/${"a".repeat(32)}/media/photo-1`, expiresAt: "2026-10-26T00:00:00.000Z", authorization: "bearer-job-token" },
    rights: { confirmed: true, scope: "render-this-story", confirmedAt: "2026-10-25T00:00:00.000Z" } }],
  narration: { mode: "provided-audio", audioMediaId: "voice-owner" },
  output: { aspectRatio: "9:16", width: 720, height: 1280, durationSeconds: 15, profiles: ["editorial-master"], captions: true },
  publishClaimed: false, createdAt: "2026-10-25T00:00:00.000Z",
};

describe("her-world clip worker", () => {
  it("returns quietly when there is no owner-started job", async () => {
    const worker = new HerWorldClipWorker({
      runner: { claimNext: async () => null }, gatewayFor: vi.fn() as never,
      intake: {} as never, renderer: {} as never,
    });
    await expect(worker.runOnce()).resolves.toEqual({ outcome: "no-job" });
  });

  it("uploads a candidate and leaves final acceptance to the owner", async () => {
    const report = vi.fn(async () => undefined);
    const uploadOutput = vi.fn(async () => undefined);
    const worker = new HerWorldClipWorker({
      runner: { claimNext: async () => ({ jobId: handoff.jobId, jobToken: "j".repeat(64), handoff }) },
      gatewayFor: () => ({
        stage: async () => ({ snapshot: {} as never, staged: { jobId: handoff.jobId } as never }), report, uploadOutput,
      }),
      intake: {} as never,
      renderer: { renderVisualDraft: async () => ({
        readiness: "review-ready", outputPath: "/private/candidate.mp4",
        progressReceipt: { schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId,
          storyId: handoff.story.id, storyRevision: 1, status: "review-ready", progress: 95,
          outputs: [{ profile: "editorial-master", relativePath: `${handoff.jobId}/outputs/editorial-master.mp4`, sha256: "d".repeat(64),
            durationSeconds: 15, width: 720, height: 1280, sourceMap: [{ mediaId: "photo-1", timelineStartMs: 0, timelineEndMs: 15_000 }] }],
          ownerAccepted: false, published: false, error: null, updatedAt: "2026-10-25T01:00:00.000Z" },
      }) } as never,
      now: () => "2026-10-25T00:30:00.000Z",
    });
    await expect(worker.runOnce()).resolves.toEqual({ outcome: "review-ready", jobId: handoff.jobId });
    expect(uploadOutput).toHaveBeenCalledTimes(1);
    expect(report).toHaveBeenLastCalledWith(handoff, expect.objectContaining({ status: "review-ready", ownerAccepted: false, published: false }));
  });

  it("reports a retryable, non-publishing failure without leaking the thrown message", async () => {
    const report = vi.fn(async () => undefined);
    const worker = new HerWorldClipWorker({
      runner: { claimNext: async () => ({ jobId: handoff.jobId, jobToken: "j".repeat(64), handoff }) },
      gatewayFor: () => ({ stage: async () => { throw new Error("/private/customer-secret-path"); }, report, uploadOutput: vi.fn() }),
      intake: {} as never, renderer: {} as never,
      now: () => "2026-10-25T00:30:00.000Z",
    });
    await expect(worker.runOnce()).resolves.toEqual({
      outcome: "failed", jobId: handoff.jobId, failureReported: true, failureCode: "clip-media-download-failed",
    });
    expect(JSON.stringify(report.mock.calls)).not.toContain("customer-secret-path");
    expect(report).toHaveBeenCalledWith(handoff, expect.objectContaining({ status: "failed", published: false,
      error: expect.objectContaining({ code: "clip-media-download-failed", retryable: true }) }));
  });

  it("reports that the finished candidate could not be returned without leaking transport details", async () => {
    const report = vi.fn(async () => undefined);
    const worker = new HerWorldClipWorker({
      runner: { claimNext: async () => ({ jobId: handoff.jobId, jobToken: "j".repeat(64), handoff }) },
      gatewayFor: () => ({
        stage: async () => ({ snapshot: {} as never, staged: { jobId: handoff.jobId } as never }),
        report,
        uploadOutput: async () => { throw new Error("her-world-gateway-output-timeout:/private/secret.mp4"); },
      }),
      intake: {} as never,
      renderer: { renderVisualDraft: async () => ({
        readiness: "review-ready", outputPath: "/private/candidate.mp4",
        progressReceipt: { schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId,
          storyId: handoff.story.id, storyRevision: 1, status: "review-ready", progress: 95,
          outputs: [{ profile: "editorial-master", relativePath: `${handoff.jobId}/outputs/editorial-master.mp4`, sha256: "d".repeat(64),
            durationSeconds: 15, width: 720, height: 1280, sourceMap: [{ mediaId: "photo-1", timelineStartMs: 0, timelineEndMs: 15_000 }] }],
          ownerAccepted: false, published: false, error: null, updatedAt: "2026-10-25T01:00:00.000Z" },
      }) } as never,
      now: () => "2026-10-25T00:30:00.000Z",
    });
    await expect(worker.runOnce()).resolves.toEqual({
      outcome: "failed", jobId: handoff.jobId, failureReported: true, failureCode: "clip-output-upload-failed",
    });
    expect(JSON.stringify(report.mock.calls)).not.toContain("private/secret.mp4");
    expect(report).toHaveBeenLastCalledWith(handoff, expect.objectContaining({
      status: "failed", error: expect.objectContaining({ code: "clip-output-upload-failed" }),
    }));
  });
});
