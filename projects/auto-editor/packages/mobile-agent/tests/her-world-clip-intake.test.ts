import { createHash } from "node:crypto";
import { lstat, mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";
import type { HerWorldClipHandoffV1 } from "@wearable/contracts";

import { HerWorldClipIntake } from "../src";

const bytes = Buffer.from("real-authorized-media-bytes");
const digest = createHash("sha256").update(bytes).digest("hex");
const handoff: HerWorldClipHandoffV1 = {
  schemaVersion: 1, kind: "her-world-story-render", jobId: "a".repeat(32), operationKey: "render-1",
  createdAt: "2026-10-25T02:00:00.000Z", publishClaimed: false,
  story: {
    id: "b".repeat(32), revision: 2, title: "源头的生活", place: "斯里兰卡", visitedAt: null,
    theme: "去源头看见好宝石背后的生活",
    themeMaterials: [
      { kind: "theme_intro", title: "主题开场", content: "先看见生活，再理解宝石。", basis: "店主确认的主题" },
      { kind: "missing_shot", title: "还值得补一张", content: "补一张真实源头细节。", basis: "只作为补拍提醒" },
    ],
    ownerConfirmed: true, confirmedAt: "2026-10-25T01:59:00.000Z",
    acts: [{ sequence: 1, stageId: "origin", mediaId: "media-1", storyFunction: "people", heading: "先看见人",
      narration: "先看到当地人的生活，再理解一颗宝石。", sourceBasis: "店主本人确认", productReference: null }],
  },
  media: [{
    id: "media-1", order: 0, kind: "image", mimeType: "image/jpeg", byteLength: bytes.byteLength, sha256: digest,
    fetch: { url: "https://media.her-world.test/assets/1", expiresAt: "2026-10-26T02:00:00.000Z", authorization: "bearer-job-token" },
    rights: { confirmed: true, scope: "render-this-story", confirmedAt: "2026-10-25T01:59:00.000Z" },
  }],
  narration: { mode: "text-only", audioMediaId: null },
  output: { aspectRatio: "9:16", width: 720, height: 1280, durationSeconds: 45, profiles: ["editorial-master"], captions: true },
};

describe("her-world clip intake", () => {
  const roots: string[] = [];
  afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

  it("downloads only authorized-origin media, verifies it, and stages a private idempotent job", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-intake-"));
    roots.push(root);
    let calls = 0;
    let authorization = "";
    const intake = new HerWorldClipIntake({
      root: join(root, "private"),
      allowedMediaOrigins: ["https://media.her-world.test"],
      now: () => new Date("2026-10-25T03:00:00.000Z"),
      fetcher: async (_url, init) => {
        calls += 1;
        authorization = new Headers(init.headers).get("authorization") ?? "";
        return new Response(bytes, { status: 200, headers: { "content-type": "image/jpeg", "content-length": String(bytes.byteLength) } });
      },
    });
    const staged = await intake.stage(handoff, "one-time-secret");
    const retried = await intake.stage(handoff, "one-time-secret");

    expect(calls).toBe(1);
    expect(authorization).toBe("Bearer one-time-secret");
    expect(staged).toEqual(retried);
    expect(staged).toMatchObject({ storyId: handoff.story.id, storyRevision: 2, theme: handoff.story.theme,
      themeMaterials: handoff.story.themeMaterials, publishClaimed: false });
    expect(staged.media[0]?.relativePath).toMatch(new RegExp(`^${handoff.jobId}/000-[a-f0-9]{24}\\.jpg$`));
    expect(staged.media[0]?.relativePath).not.toContain("one-time-secret");
    expect((await lstat(staged.media[0]!.absolutePath)).mode & 0o777).toBe(0o600);
  });

  it("fails closed and removes the partial job for a wrong digest", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-intake-"));
    roots.push(root);
    const intake = new HerWorldClipIntake({
      root: join(root, "private"), allowedMediaOrigins: ["https://media.her-world.test"],
      now: () => new Date("2026-10-25T03:00:00.000Z"),
      fetcher: async () => new Response(Buffer.from("wrong-but-same-length-xxxx"), { status: 200, headers: { "content-type": "image/jpeg" } }),
    });
    await expect(intake.stage(handoff, "token")).rejects.toThrow();
    expect(await readdir(join(root, "private"))).toEqual([]);
  });

  it("rejects foreign origins, expired authorization, and empty credentials before accepting media", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-intake-"));
    roots.push(root);
    let calls = 0;
    const intake = new HerWorldClipIntake({
      root: join(root, "private"), allowedMediaOrigins: ["https://trusted.test"],
      now: () => new Date("2026-10-25T03:00:00.000Z"),
      fetcher: async () => { calls += 1; return new Response(bytes); },
    });
    await expect(intake.stage(handoff, "token")).rejects.toThrow("origin-blocked");
    expect(calls).toBe(0);
    await expect(intake.stage(handoff, " ")).rejects.toThrow("job-token-required");
    await expect(intake.stage({ ...handoff, media: [{ ...handoff.media[0], fetch: { ...handoff.media[0]!.fetch, expiresAt: "2026-10-25T02:30:00.000Z" } }] }, "token"))
      .rejects.toThrow("media-fetch-expired");
  });

  it("rejects path-shaped identifiers and embedded URL credentials", async () => {
    const root = await mkdtemp(join(tmpdir(), "her-world-intake-"));
    roots.push(root);
    const intake = new HerWorldClipIntake({
      root: join(root, "private"), allowedMediaOrigins: ["https://media.her-world.test"],
      now: () => new Date("2026-10-25T03:00:00.000Z"), fetcher: async () => new Response(bytes),
    });
    await expect(intake.stage({ ...handoff, media: [{ ...handoff.media[0], id: "../escape" }] }, "token")).rejects.toThrow();
    await expect(intake.stage({ ...handoff, media: [{ ...handoff.media[0], fetch: { ...handoff.media[0]!.fetch, url: "https://user:secret@media.her-world.test/assets/1" } }] }, "token"))
      .rejects.toThrow("url-credentials-blocked");
  });
});
