import { createHash } from "node:crypto";
import { link, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ExternalEditHandoff } from "@wearable/contracts";

import { ExternalEditInbox } from "../src";

const digest = (bytes: Buffer | string) => createHash("sha256").update(bytes).digest("hex");
const handoff: ExternalEditHandoff = {
  id: "handoff-1", customerId: "customer-a", sessionId: "session-1", parentDraftId: "draft-1",
  parentDraftVersion: 1, parentDraftDigest: "c".repeat(64), platformVariant: "wechat-channels",
  revisionInstruction: "保留打磨过程", mustKeep: ["打磨"], includeSources: false, sourceAssetBindings: [],
  mode: "file-package", adapterId: null, adapterVersion: null, status: "editing", operationKey: "op-1",
  packageDigest: "d".repeat(64), publishClaimed: false,
  createdAt: "2026-09-30T08:00:00.000Z", updatedAt: "2026-09-30T08:00:00.000Z",
};

describe("external edit inbox", () => {
  let root: string;
  let currentTime: number;
  let inbox: ExternalEditInbox;
  let returnDirectory: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "external-edit-inbox-"));
    currentTime = Date.parse("2026-09-30T08:00:00.000Z");
    inbox = new ExternalEditInbox({
      root: join(root, "inbox"),
      stableForMs: 1_000,
      now: () => new Date(currentTime).toISOString(),
      probeVideo: async () => ({ durationSeconds: 35, width: 1080, height: 1920 }),
    });
    returnDirectory = await inbox.createReturnDirectory(handoff);
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("lists files only after their size and modification time remain stable", async () => {
    const bytes = Buffer.from("synthetic-export");
    const candidatePath = join(returnDirectory, "final.mp4");
    await writeFile(candidatePath, bytes);
    expect(await inbox.scan(handoff)).toEqual([]);
    currentTime += 900;
    expect(await inbox.scan(handoff)).toEqual([]);
    currentTime += 100;
    const candidates = await inbox.scan(handoff);

    expect(candidates).toHaveLength(1);
    expect(candidates[0]).toMatchObject({
      handoffId: "handoff-1", parentDraftId: "draft-1", digest: digest(bytes), status: "pending-review",
      durationSeconds: 35, mediaFacts: { width: 1080, height: 1920 },
    });
    expect(candidates[0]!.relativePath).toBe(`${digest("customer-a").slice(0, 32)}/${digest("handoff-1").slice(0, 32)}/final.mp4`);
  });

  it("resets the stability window when an export is still being written", async () => {
    const candidatePath = join(returnDirectory, "changing.mp4");
    await writeFile(candidatePath, "part-one");
    expect(await inbox.scan(handoff)).toHaveLength(0);
    currentTime += 800;
    await writeFile(candidatePath, "part-one-and-two");
    currentTime += 500;
    expect(await inbox.scan(handoff)).toHaveLength(0);
    currentTime += 1_500;
    expect(await inbox.scan(handoff)).toHaveLength(1);
  });

  it("rejects empty, symlinked, hard-linked, unsupported, and out-of-scope files", async () => {
    const outside = join(root, "outside.mp4");
    await writeFile(outside, "synthetic-outside");
    await writeFile(join(returnDirectory, "empty.mp4"), "");
    await symlink(outside, join(returnDirectory, "linked.mp4"));
    await link(outside, join(returnDirectory, "hardlinked.mp4"));
    await writeFile(join(returnDirectory, "notes.txt"), "not video");
    currentTime += 1_000;

    expect(await inbox.scan(handoff)).toEqual([]);
    expect(await inbox.scan({ ...handoff, customerId: "customer-b" })).toEqual([]);
  });

  it("lists all stable candidates instead of choosing the newest", async () => {
    await writeFile(join(returnDirectory, "one.mp4"), "synthetic-one");
    await writeFile(join(returnDirectory, "two.mov"), "synthetic-two");
    expect(await inbox.scan(handoff)).toHaveLength(0);
    currentTime += 1_000;

    const candidates = await inbox.scan(handoff);
    expect(candidates.map((candidate) => candidate.relativePath.split("/").at(-1)).sort()).toEqual(["one.mp4", "two.mov"]);
    expect(new Set(candidates.map((candidate) => candidate.digest)).size).toBe(2);
  });

  it("imports one fully received manual return without claiming its editor", async () => {
    const bytes = Buffer.from("fully-received-manual-return");
    const candidate = await inbox.importCandidate(handoff, {
      bytes,
      fileName: "剪映导出候选.mp4",
    });

    expect(candidate).toMatchObject({
      handoffId: handoff.id,
      parentDraftId: handoff.parentDraftId,
      digest: digest(bytes),
      durationSeconds: 35,
      status: "pending-review",
    });
    expect(candidate.relativePath.split("/").at(-1)).toMatch(/^manual-[a-f0-9]{24}\.mp4$/);
    expect(await inbox.scan(handoff)).toEqual([candidate]);
    const retried = await inbox.importCandidate(handoff, { bytes, fileName: "再次选择同一文件.mp4" });
    expect(retried).toEqual(candidate);
    expect((await readdir(returnDirectory)).filter((name) => name.startsWith("manual-"))).toHaveLength(1);
    expect((await readdir(returnDirectory)).filter((name) => name.endsWith(".part"))).toEqual([]);
    await expect(inbox.importCandidate(handoff, { bytes, fileName: "不是视频.txt" }))
      .rejects.toThrow("external-edit-upload-file-name-invalid");
  });

  it("copies a selected candidate through a no-follow descriptor and verifies its digest", async () => {
    const bytes = Buffer.from("synthetic-export-for-copy");
    await writeFile(join(returnDirectory, "final.mp4"), bytes);
    expect(await inbox.scan(handoff)).toHaveLength(0);
    currentTime += 1_000;
    const candidate = (await inbox.scan(handoff))[0]!;
    const destination = join(root, "private", "accepted.mp4");
    await mkdir(join(root, "private"));
    const copied = await inbox.copyCandidateTo(handoff, candidate, destination);

    expect(copied).toMatchObject({ digest: digest(bytes), sizeBytes: bytes.length });
    expect(await readFile(destination)).toEqual(bytes);
  });
});
