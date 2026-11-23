import { createHash } from "node:crypto";
import { lstat, mkdir, mkdtemp, open, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { DraftPackage, ExternalEditHandoff } from "@wearable/contracts";

import { ExternalEditPackageBuilder, type ExternalEditPackageSource } from "../src";

const hash = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");
const scopeHandoff: ExternalEditHandoff = {
  id: "handoff-1",
  customerId: "customer-a",
  sessionId: "session-1",
  parentDraftId: "draft-1",
  parentDraftVersion: 1,
  parentDraftDigest: "99f95df5262a9127e525f84cab1ef7d51ef4f6411a80ab3449641872d1a620a6",
  platformVariant: "wechat-channels",
  revisionInstruction: "开头自然一点，保留打磨细节。",
  mustKeep: ["打磨细节"],
  includeSources: true,
  sourceAssetBindings: [{ sourceAssetId: "asset-a", digest: "c0b5d6971c11c81c6879704d4c47bd6dcba370ad38a6ce10b40a64d10ba9bad8" }],
  mode: "file-package",
  adapterId: null,
  adapterVersion: null,
  status: "preparing",
  operationKey: "operation-1",
  packageDigest: null,
  publishClaimed: false,
  createdAt: "2026-09-30T08:00:00.000Z",
  updatedAt: "2026-09-30T08:00:00.000Z",
};
const parentDraft: DraftPackage = {
  id: "draft-1",
  customerId: "customer-a",
  sessionId: "session-1",
  version: 1,
  demandId: "demand-1",
  revisionProposalId: null,
  briefId: "brief-1",
  blueprintId: "blueprint-1",
  blueprintDigest: "b".repeat(64),
  outputDigest: "99f95df5262a9127e525f84cab1ef7d51ef4f6411a80ab3449641872d1a620a6",
  outputPath: "/synthetic/draft-1.mp4",
  durationSeconds: 35,
  sourceMap: [{ shotId: "shot-1", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64), sourceStartMs: 0, sourceEndMs: 10_000, reason: "show-process-proof" }],
  status: "reviewable",
  capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
  publishClaimed: false,
  createdAt: "2026-09-30T08:00:00.000Z",
};

describe("external edit package builder", () => {
  let root: string;
  let sourceRoot: string;
  let parentPath: string;
  let assetPath: string;
  let parentBytes: Buffer;
  let assetBytes: Buffer;
  let sources: ExternalEditPackageSource[];

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "external-edit-package-"));
    sourceRoot = join(root, "input");
    await mkdir(sourceRoot, { mode: 0o700 });
    parentPath = join(sourceRoot, "parent.mp4");
    assetPath = join(sourceRoot, "source.mov");
    parentBytes = Buffer.from("synthetic-parent-video");
    assetBytes = Buffer.from("synthetic-authorized-source");
    await (await open(parentPath, "w", 0o600)).writeFile(parentBytes);
    await (await open(assetPath, "w", 0o600)).writeFile(assetBytes);
    sources = [
      { role: "parent-video", sourcePath: parentPath, relativePath: "ignored-name.mp4", digest: hash(parentBytes) },
      { role: "source-video", sourceAssetId: "asset-a", sourcePath: assetPath, relativePath: "ignored-source.mov", digest: hash(assetBytes) },
    ];
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("copies only resolved authorized files and writes relative digest manifest plus plain-language instructions", async () => {
    const builder = new ExternalEditPackageBuilder({
      root: join(root, "packages"),
      resolveSources: async () => sources,
    });

    const result = await builder.build(scopeHandoff, parentDraft);
    const manifest = JSON.parse(await readFile(join(result.packageRoot, "manifest.json"), "utf8"));
    const instructions = await readFile(join(result.packageRoot, "README.txt"), "utf8");

    expect(result.package.files.map((file) => file.relativePath)).toEqual([
      "video/parent.mp4", `sources/source-${hash("asset-a").slice(0, 16)}.mov`, "README.txt",
    ]);
    expect(manifest.package.files[0]).toMatchObject({ relativePath: "video/parent.mp4", digest: hash(parentBytes), sizeBytes: parentBytes.length });
    expect(JSON.stringify(manifest)).not.toMatch(/\/Users\/|\/tmp\//);
    expect(instructions).toContain("这是一份剪映交接包，不代表已经导入剪映");
    expect(instructions).toContain("剪映精修版.mp4");
    expect(await readFile(join(result.packageRoot, "video/parent.mp4"))).toEqual(parentBytes);
    expect(await readFile(join(result.packageRoot, `sources/source-${hash("asset-a").slice(0, 16)}.mov`))).toEqual(assetBytes);
  });

  it("builds idempotently with private directory and file permissions", async () => {
    const builder = new ExternalEditPackageBuilder({ root: join(root, "packages"), resolveSources: async () => sources });
    const first = await builder.build(scopeHandoff, parentDraft);
    const retry = await builder.build(scopeHandoff, parentDraft);
    const packageInfo = await stat(first.packageRoot);
    const videoInfo = await stat(join(first.packageRoot, "video/parent.mp4"));

    expect(retry.package.packageDigest).toBe(first.package.packageDigest);
    expect(retry.packageRoot).toBe(first.packageRoot);
    expect(packageInfo.mode & 0o777).toBe(0o700);
    expect(videoInfo.mode & 0o777).toBe(0o600);
    expect(await readFile(join(first.packageRoot, "video/parent.mp4"))).toEqual(parentBytes);
  });

  it("fails closed for digest drift, symlinks, hard links, and assets without explicit handoff consent", async () => {
    const digestMismatch = new ExternalEditPackageBuilder({
      root: join(root, "digest-mismatch"),
      resolveSources: async () => [{ ...sources[0]!, digest: "f".repeat(64) }],
    });
    await expect(digestMismatch.build({ ...scopeHandoff, includeSources: false, sourceAssetBindings: [] }, parentDraft))
      .rejects.toThrow("external-edit-source-digest-mismatch");

    const symlinkPath = join(sourceRoot, "symlink.mp4");
    await (await import("node:fs/promises")).symlink(parentPath, symlinkPath);
    const symlinkBuilder = new ExternalEditPackageBuilder({
      root: join(root, "symlink"),
      resolveSources: async () => [{ ...sources[0]!, sourcePath: symlinkPath }],
    });
    await expect(symlinkBuilder.build({ ...scopeHandoff, includeSources: false, sourceAssetBindings: [] }, parentDraft))
      .rejects.toThrow("external-edit-source-not-regular-file");

    const hardLinkPath = join(sourceRoot, "hardlink.mp4");
    await (await import("node:fs/promises")).link(parentPath, hardLinkPath);
    const hardLinkBuilder = new ExternalEditPackageBuilder({
      root: join(root, "hardlink"),
      resolveSources: async () => [{ ...sources[0]!, sourcePath: hardLinkPath }],
    });
    await expect(hardLinkBuilder.build({ ...scopeHandoff, includeSources: false, sourceAssetBindings: [] }, parentDraft))
      .rejects.toThrow("external-edit-source-not-regular-file");

    const unauthorizedBuilder = new ExternalEditPackageBuilder({
      root: join(root, "unauthorized"),
      resolveSources: async () => sources,
    });
    await expect(unauthorizedBuilder.build({ ...scopeHandoff, includeSources: false, sourceAssetBindings: [] }, parentDraft))
      .rejects.toThrow("external-edit-source-consent-required");
  });

  it("rejects parent ownership or digest changes before creating any package", async () => {
    const builder = new ExternalEditPackageBuilder({ root: join(root, "ownership"), resolveSources: async () => sources });
    await expect(builder.build(scopeHandoff, { ...parentDraft, customerId: "customer-b" }))
      .rejects.toThrow("external-edit-parent-scope-mismatch");
    await expect(builder.build(scopeHandoff, { ...parentDraft, outputDigest: "f".repeat(64) }))
      .rejects.toThrow("external-edit-parent-scope-mismatch");
    await expect(lstat(join(root, "ownership"))).rejects.toMatchObject({ code: "ENOENT" });
  });
});
