import { createHash } from "node:crypto";
import {
  chmod,
  mkdir,
  mkdtemp,
  realpath,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  assertLockedMaterialUnchanged,
  loadLockedRealMaterialManifest,
  publicInputReceipts,
  readLockedMaterialBytes,
} from "../src/real-material-manifest";

const roots: string[] = [];
const NOW = "2026-09-22T00:00:00.000Z";

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("real material manifest locking", () => {
  it("locks canonical files and emits path-free public receipts", async () => {
    const fixture = await createFixture();

    const locked = await loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    });

    expect(locked.inputs[0]!.canonicalPath).toBe(await realpath(fixture.sourcePath));
    expect(locked.inputs[0]!.byteLength).toBe(fixture.sourceBytes.byteLength);
    expect(locked.inputs[0]!.lockedDigest).toBe(digest(fixture.sourceBytes));
    const receipts = publicInputReceipts(locked.inputs);
    expect(receipts).toEqual([{
      inputId: "current_01",
      role: "current",
      mediaKind: "video",
      sourceDigest: digest(fixture.sourceBytes),
      byteLength: fixture.sourceBytes.byteLength,
    }]);
    expect(JSON.stringify(receipts)).not.toContain(fixture.approvedRoot);
    expect(JSON.stringify(receipts)).not.toContain("sourcePath");
  });

  it("requires the private manifest to have exact mode 0600", async () => {
    const fixture = await createFixture();
    await chmod(fixture.manifestPath, 0o640);

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("real-material-manifest-mode-must-be-0600");
  });

  it("rejects a relative source path before reading media", async () => {
    const fixture = await createFixture({ sourcePathInManifest: "current.mp4" });

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("absolute-source-path-required");
  });

  it("rejects a symlink that escapes an approved root", async () => {
    const fixture = await createFixture();
    const outsideRoot = await trackedTemp("real-material-outside-");
    const outsidePath = join(outsideRoot, "outside.mp4");
    await writeFile(outsidePath, fixture.sourceBytes);
    const linkPath = join(fixture.approvedRoot, "escape.mp4");
    await symlink(outsidePath, linkPath);
    await writeManifest(fixture.manifestPath, manifestFor(linkPath, fixture.sourceBytes));

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("real-material-source-outside-approved-roots");
  });

  it("rejects declared digest mismatch", async () => {
    const fixture = await createFixture({ declaredDigest: "f".repeat(64) });

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("real-material-source-digest-mismatch");
  });

  it("rejects one canonical file declared under two input IDs", async () => {
    const fixture = await createFixture();
    const manifest = manifestFor(fixture.sourcePath, fixture.sourceBytes);
    manifest.inputs.push({ ...manifest.inputs[0]!, inputId: "historical_alias", role: "historical" });
    await writeManifest(fixture.manifestPath, manifest);

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("duplicate-real-material-canonical-path");
  });

  it("rejects duplicate digest and role pairs even across separate files", async () => {
    const fixture = await createFixture();
    const copyPath = join(fixture.approvedRoot, "copy.mp4");
    await writeFile(copyPath, fixture.sourceBytes);
    const manifest = manifestFor(fixture.sourcePath, fixture.sourceBytes);
    manifest.inputs.push({ ...manifest.inputs[0]!, inputId: "current_copy", sourcePath: copyPath });
    await writeManifest(fixture.manifestPath, manifest);

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    })).rejects.toThrow("duplicate-real-material-digest-role");
  });

  it("rejects an empty approved-root list", async () => {
    const fixture = await createFixture();

    await expect(loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [],
    })).rejects.toThrow("real-material-approved-root-required");
  });

  it("detects source mutation after locking", async () => {
    const fixture = await createFixture();
    const locked = await loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    });

    await writeFile(fixture.sourcePath, Buffer.from("changed-source-bytes"));

    await expect(assertLockedMaterialUnchanged(locked.inputs)).rejects.toThrow(
      "real-material-source-changed",
    );
  });

  it("never returns bytes that differ from the digest locked in the manifest", async () => {
    const fixture = await createFixture();
    const locked = await loadLockedRealMaterialManifest({
      manifestPath: fixture.manifestPath,
      approvedRoots: [fixture.approvedRoot],
    });
    await writeFile(fixture.sourcePath, Buffer.alloc(fixture.sourceBytes.byteLength, 0x78));

    await expect(readLockedMaterialBytes(locked.inputs[0]!)).rejects.toThrow(
      "real-material-source-changed",
    );
  });
});

async function createFixture(overrides: Readonly<{
  sourcePathInManifest?: string;
  declaredDigest?: string;
}> = {}) {
  const root = await trackedTemp("real-material-manifest-");
  const approvedRoot = join(root, "approved");
  await mkdir(approvedRoot, { recursive: true });
  const sourcePath = join(approvedRoot, "current.mp4");
  const sourceBytes = Buffer.from("authorized-real-media");
  await writeFile(sourcePath, sourceBytes);
  const manifestPath = join(root, "manifest.json");
  const manifest = manifestFor(
    overrides.sourcePathInManifest ?? sourcePath,
    sourceBytes,
    overrides.declaredDigest,
  );
  await writeManifest(manifestPath, manifest);
  return { root, approvedRoot, sourcePath, sourceBytes, manifestPath };
}

function manifestFor(sourcePath: string, bytes: Uint8Array, declaredDigest = digest(bytes)) {
  return {
    schemaVersion: 1 as const,
    runId: "real_run_01",
    customerId: "customer_01",
    actorId: "designer_01",
    createdAt: NOW,
    purpose: "real-material-acceptance" as const,
    inputs: [{
      inputId: "current_01",
      role: "current" as "current" | "historical",
      mediaKind: "video" as const,
      sourcePath,
      sourceDigest: declaredDigest,
      permissionState: "allowed" as const,
      allowedUses: ["story-analysis", "render"] as const,
      declaredOwnership: "customer-owned" as const,
      confirmedFacts: [],
    }],
    referencePolicy: "external-style-only" as const,
    requestedStoryTypes: ["product-journey"] as const,
    requestedVariants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"] as const,
  };
}

async function writeManifest(path: string, manifest: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
  await chmod(path, 0o600);
}

async function trackedTemp(prefix: string): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), prefix));
  roots.push(root);
  return root;
}

function digest(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}
