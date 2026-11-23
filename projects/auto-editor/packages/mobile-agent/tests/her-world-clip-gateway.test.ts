import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";
import type { HerWorldClipHandoffV1, HerWorldClipReceiptV1 } from "@wearable/contracts";

import { HerWorldClipGatewayClient, HerWorldClipIntake, HerWorldClipRunnerClient } from "../src";

const bytes = Buffer.from("gateway-media");
const handoff: HerWorldClipHandoffV1 = {
  schemaVersion: 1, kind: "her-world-story-render", jobId: "a".repeat(32), operationKey: "op-1",
  createdAt: "2026-10-25T02:00:00.000Z", publishClaimed: false,
  story: { id: "b".repeat(32), revision: 1, title: "故事", place: "产地", visitedAt: null,
    ownerConfirmed: true, confirmedAt: "2026-10-25T02:00:00.000Z",
    acts: [{ sequence: 1, stageId: "arrive", mediaId: "image-1", storyFunction: "place-entry", heading: "抵达",
      narration: "本人确认的旁白", sourceBasis: "owner-confirmed", productReference: null }] },
  media: [{ id: "image-1", order: 0, kind: "image", mimeType: "image/jpeg", byteLength: bytes.byteLength,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    fetch: { url: "https://her-world.test/api/clip/jobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/media/image-1", expiresAt: "2026-10-26T02:00:00.000Z", authorization: "bearer-job-token" },
    rights: { confirmed: true, scope: "render-this-story", confirmedAt: "2026-10-25T02:00:00.000Z" } }],
  narration: { mode: "text-only", audioMediaId: null },
  output: { aspectRatio: "9:16", width: 720, height: 1280, durationSeconds: 20, profiles: ["editorial-master"], captions: true },
};
const token = "t".repeat(64);

describe("her-world clip gateway client", () => {
  const roots: string[] = [];
  afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

  it("reads the bound job, stages its verified media, and never places the token in output", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-gateway-"));
    roots.push(root);
    const gatewayCalls: RequestInit[] = [];
    const gateway = new HerWorldClipGatewayClient({
      baseUrl: "https://her-world.test", jobId: handoff.jobId, bearerToken: token,
      fetcher: async (_url, init) => {
        gatewayCalls.push(init);
        return Response.json({ ok: true, handoff, status: "queued", progress: 0, receipt: null, expires_at: "2026-10-26T02:00:00.000Z" });
      },
    });
    const intake = new HerWorldClipIntake({
      root: join(root, "private"), allowedMediaOrigins: ["https://her-world.test"], now: () => new Date("2026-10-25T03:00:00.000Z"),
      fetcher: async () => new Response(bytes, { headers: { "content-type": "image/jpeg", "content-length": String(bytes.byteLength) } }),
    });
    const result = await gateway.stage(intake);
    expect(result.snapshot.status).toBe("queued");
    expect(result.staged.media).toHaveLength(1);
    expect(JSON.stringify(result.staged)).not.toContain(token);
    expect(new Headers(gatewayCalls[0]!.headers).get("authorization")).toBe(`Bearer ${token}`);
  });

  it("reports only a receipt bound to the exact task and rejects a different revision locally", async () => {
    let reported: unknown;
    const gateway = new HerWorldClipGatewayClient({
      baseUrl: "https://her-world.test", jobId: handoff.jobId, bearerToken: token,
      fetcher: async (_url, init) => { reported = init.body ? JSON.parse(String(init.body)) : null; return Response.json({ ok: true }); },
    });
    const receipt: HerWorldClipReceiptV1 = {
      schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId, storyId: handoff.story.id,
      storyRevision: 1, status: "rendering", progress: 40, outputs: [], ownerAccepted: false, published: false,
      error: null, updatedAt: "2026-10-25T02:10:00.000Z",
    };
    await gateway.report(handoff, receipt);
    expect(reported).toEqual(receipt);
    await expect(gateway.report(handoff, { ...receipt, storyRevision: 2 })).rejects.toThrow("binding-mismatch");
  });

  it("uploads an immutable candidate before the review-ready receipt", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-gateway-"));
    roots.push(root);
    const outputPath = join(root, "candidate.mp4");
    const outputBytes = Buffer.from("candidate-mp4");
    await writeFile(outputPath, outputBytes);
    let uploaded: { url: string; headers: Headers; body: Uint8Array } | null = null;
    const digest = createHash("sha256").update(outputBytes).digest("hex");
    const gateway = new HerWorldClipGatewayClient({
      baseUrl: "https://her-world.test", jobId: handoff.jobId, bearerToken: token,
      fetcher: async (url, init) => {
        uploaded = { url, headers: new Headers(init.headers), body: new Uint8Array(init.body as Uint8Array) };
        return Response.json({ ok: true, sha256: digest });
      },
    });
    const receipt: HerWorldClipReceiptV1 = {
      schemaVersion: 1, kind: "her-world-story-render-receipt", jobId: handoff.jobId, storyId: handoff.story.id,
      storyRevision: 1, status: "review-ready", progress: 95, ownerAccepted: false, published: false, error: null,
      updatedAt: "2026-10-25T02:10:00.000Z", outputs: [{ profile: "editorial-master",
        relativePath: `${handoff.jobId}/outputs/editorial-master.mp4`, sha256: digest, durationSeconds: 20,
        width: 720, height: 1280, sourceMap: [{ mediaId: "image-1", timelineStartMs: 0, timelineEndMs: 20000 }] }],
    };
    await gateway.uploadOutput(handoff, receipt, outputPath);
    expect(uploaded).not.toBeNull();
    expect(uploaded!.url).toContain(`/api/clip/jobs/${handoff.jobId}/output/editorial-master`);
    expect(uploaded!.headers.get("authorization")).toBe(`Bearer ${token}`);
    expect(uploaded!.headers.get("x-content-sha256")).toBe(digest);
    expect(Buffer.from(uploaded!.body)).toEqual(outputBytes);
  });

  it("rejects insecure gateways and mismatched returned jobs", async () => {
    expect(() => new HerWorldClipGatewayClient({ baseUrl: "http://her-world.test", jobId: handoff.jobId, bearerToken: token })).toThrow("url-invalid");
    const gateway = new HerWorldClipGatewayClient({
      baseUrl: "https://her-world.test", jobId: handoff.jobId, bearerToken: token,
      fetcher: async () => Response.json({ ok: true, handoff: { ...handoff, jobId: "c".repeat(32) }, status: "queued", progress: 0, receipt: null, expires_at: "2026-10-26T02:00:00.000Z" }),
    });
    await expect(gateway.read()).rejects.toThrow("job-binding-mismatch");
  });

  it("claims one queued job with runner credentials without persisting the runner secret", async () => {
    const runnerSecret = "r".repeat(64);
    const jobToken = "j".repeat(64);
    let request: RequestInit | null = null;
    const runner = new HerWorldClipRunnerClient({
      baseUrl: "https://her-world.test", runnerSecret,
      fetcher: async (_url, init) => { request = init; return Response.json({ ok: true, job_id: handoff.jobId, job_token: jobToken, handoff }); },
    });
    const claimed = await runner.claimNext();
    expect(claimed).toEqual({ jobId: handoff.jobId, jobToken, handoff });
    expect(new Headers(request!.headers).get("authorization")).toBe(`Bearer ${runnerSecret}`);
    expect(JSON.stringify(claimed)).not.toContain(runnerSecret);
  });

  it("returns null when no clip job is waiting", async () => {
    const runner = new HerWorldClipRunnerClient({
      baseUrl: "https://her-world.test", runnerSecret: "r".repeat(64),
      fetcher: async () => new Response(null, { status: 204 }),
    });
    await expect(runner.claimNext()).resolves.toBeNull();
  });
});
