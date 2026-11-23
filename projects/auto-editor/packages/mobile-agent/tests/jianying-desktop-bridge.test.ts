import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DraftPackage, ExternalEditHandoff, ExternalEditPackage } from "@wearable/contracts";

import {
  JianyingDesktopBridge,
  type JianyingDraftAdapter,
  type JianyingInstallReceipt,
} from "../src";

const draft: DraftPackage = {
  id: "draft-1", customerId: "customer-a", sessionId: "session-1", version: 1,
  demandId: "demand-1", revisionProposalId: null, briefId: "brief-1", blueprintId: "blueprint-1",
  blueprintDigest: "b".repeat(64), outputDigest: "c".repeat(64), outputPath: "/internal/draft.mp4",
  durationSeconds: 35,
  sourceMap: [{ shotId: "shot-1", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64), sourceStartMs: 0, sourceEndMs: 10_000, reason: "process" }],
  status: "reviewable", capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 1, provider: null },
  publishClaimed: false, createdAt: "2026-09-30T08:00:00.000Z",
};
const handoff: ExternalEditHandoff = {
  id: "handoff-1", customerId: "customer-a", sessionId: "session-1", parentDraftId: "draft-1",
  parentDraftVersion: 1, parentDraftDigest: "c".repeat(64), platformVariant: "wechat-channels",
  revisionInstruction: "保留细节", mustKeep: ["打磨"], includeSources: false, sourceAssetBindings: [],
  mode: "file-package", adapterId: null, adapterVersion: null, status: "preparing", operationKey: "op-1",
  packageDigest: null, publishClaimed: false, createdAt: "2026-09-30T08:00:00.000Z", updatedAt: "2026-09-30T08:00:00.000Z",
};
const packageValue: ExternalEditPackage = {
  id: "package-1", handoffId: "handoff-1", customerId: "customer-a", parentDraftId: "draft-1",
  packageDigest: "d".repeat(64),
  files: [
    { relativePath: "video/parent.mp4", role: "parent-video", digest: "c".repeat(64), sizeBytes: 128 },
    { relativePath: "support/subtitle.srt", role: "subtitle", digest: "e".repeat(64), sizeBytes: 64 },
  ],
  createdAt: "2026-09-30T08:00:00.000Z",
};

describe("Jianying desktop bridge", () => {
  let root: string;
  const packageBuilder = { build: vi.fn(async () => ({ package: packageValue, packageRoot: "/private/handoff/package" })) };

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "jianying-bridge-"));
    packageBuilder.build.mockClear();
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("reports uninstalled and prepares a file package without claiming Jianying import", async () => {
    const openTarget = vi.fn(async (_target: { kind: "jianying" | "folder"; target: string }) => undefined);
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => null,
      openTarget,
    });

    const prepared = await bridge.prepare(handoff, draft);
    expect(prepared).toMatchObject({ health: "uninstalled", mode: "file-package", status: "degraded", degradedReason: "jianying-not-installed" });
    expect(prepared.package.packageDigest).toBe("d".repeat(64));
    expect(prepared.handoff).toMatchObject({ mode: "file-package", adapterId: null, status: "degraded" });
    const opened = await bridge.open(prepared);
    expect(opened).toMatchObject({ opened: true, targetKind: "package-folder" });
    expect(openTarget).toHaveBeenCalledWith({ kind: "folder", target: "/private/handoff/package" });
  });

  it("reports unsupported versions and safely falls back to the package", async () => {
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => ({ appVersion: "99.9.0", appPath: "/Applications/Jianying.app", draftRoot: "/Users/test-user/Movies/JianyingDrafts" }),
      openTarget: async () => undefined,
      adapters: [],
    });

    const prepared = await bridge.prepare(handoff, draft);
    expect(prepared).toMatchObject({ health: "unsupported-version", mode: "file-package", degradedReason: "jianying-version-not-verified" });
    expect(JSON.stringify(prepared)).not.toContain("/Users/person");
  });

  it("opens Jianying, the package, and the handoff-specific return folder without claiming a native project", async () => {
    const openTarget = vi.fn(async () => undefined);
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => ({ appVersion: "11.5.3", appPath: "/Applications/VideoFusion-macOS.app", draftRoot: "/private/drafts" }),
      resolveReturnFolder: async () => "/private/returns/handoff-1",
      openTarget,
      adapters: [],
    });

    const prepared = await bridge.prepare(handoff, draft);
    expect(prepared).toMatchObject({ mode: "file-package", status: "degraded", degradedReason: "jianying-version-not-verified" });
    const opened = await bridge.open(prepared);

    expect(openTarget.mock.calls).toEqual([
      [{ kind: "jianying", target: "/Applications/VideoFusion-macOS.app" }],
      [{ kind: "folder", target: "/private/handoff/package" }],
      [{ kind: "folder", target: "/private/returns/handoff-1" }],
    ]);
    expect(opened).toEqual({
      opened: true,
      targetKind: "jianying",
      jianyingOpened: true,
      packageFolderOpened: true,
      returnFolderOpened: true,
      automation: {
        attempted: false,
        projectCreated: false,
        materialImported: false,
        timelineImported: false,
        error: null,
      },
      error: null,
    });
    expect(prepared.handoff.mode).toBe("file-package");
  });

  it("continues opening the return folder if Jianying cannot launch", async () => {
    const openTarget = vi.fn(async ({ kind, target }: { kind: "jianying" | "folder"; target: string }) => {
      if (kind === "jianying") throw new Error(`failed ${target}`);
    });
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => ({ appVersion: "11.5.3", appPath: "/Applications/VideoFusion-macOS.app", draftRoot: "/private/drafts" }),
      resolveReturnFolder: async () => "/private/returns/handoff-1",
      openTarget,
      adapters: [],
    });
    const prepared = await bridge.prepare(handoff, draft);

    await expect(bridge.open(prepared)).resolves.toMatchObject({
      opened: true, jianyingOpened: false, packageFolderOpened: true, returnFolderOpened: true, error: null,
    });
    expect(JSON.stringify(await bridge.open(prepared))).not.toContain("/private");
  });

  it("uses Jianying mode only after an installed adapter returns a matching package receipt", async () => {
    const install = vi.fn(async (): Promise<JianyingInstallReceipt> => ({
      adapterId: "jianying-9-adapter", adapterVersion: "1.0.0", appVersion: "9.0.0",
      packageDigest: "d".repeat(64), draftDigest: "e".repeat(64),
    }));
    const adapter: JianyingDraftAdapter = {
      id: "jianying-9-adapter",
      adapterVersion: "1.0.0",
      supports: ({ appVersion, platform }) => appVersion === "9.0.0" && platform === "darwin",
      install,
    };
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => ({ appVersion: "9.0.0", appPath: "/Applications/Jianying.app", draftRoot: "/tmp/test-jianying-drafts" }),
      openTarget: async () => undefined,
      adapters: [adapter],
    });

    const prepared = await bridge.prepare(handoff, draft);
    expect(install).toHaveBeenCalledOnce();
    expect(prepared).toMatchObject({ health: "supported-version", mode: "jianying-draft", status: "ready", receipt: { draftDigest: "e".repeat(64) } });
    expect(prepared.handoff).toMatchObject({ mode: "jianying-draft", adapterId: adapter.id, adapterVersion: adapter.adapterVersion });
  });

  it("automates one Jianying import, avoids reopening the package folder, and reuses the receipt", async () => {
    const openTarget = vi.fn(async (_target: { kind: "jianying" | "folder"; target: string }) => undefined);
    const automate = vi.fn(async () => ({
      attempted: true as const,
      projectCreated: true,
      materialImported: true,
      timelineImported: true,
      error: null,
    }));
    const bridge = new JianyingDesktopBridge({
      root,
      packageBuilder,
      findApp: async () => ({
        appVersion: "11.6.0",
        appPath: "/Applications/VideoFusion-macOS.app",
        draftRoot: "/private/drafts",
      }),
      resolveReturnFolder: async () => "/private/returns/handoff-1",
      openTarget,
      automate,
      adapters: [],
    });

    const prepared = await bridge.prepare(handoff, draft);
    const first = await bridge.open(prepared);
    const second = await bridge.open(prepared);

    expect(automate).toHaveBeenCalledOnce();
    expect(automate).toHaveBeenCalledWith({
      app: {
        appVersion: "11.6.0",
        appPath: "/Applications/VideoFusion-macOS.app",
        draftRoot: "/private/drafts",
      },
      mediaPath: "/private/handoff/package/video/parent.mp4",
      subtitlePath: "/private/handoff/package/support/subtitle.srt",
      projectName: "内容总编-handoff-",
    });
    expect(first).toMatchObject({
      jianyingOpened: true,
      packageFolderOpened: false,
      returnFolderOpened: true,
      automation: { materialImported: true, timelineImported: true, error: null },
    });
    expect(second.automation).toEqual(first.automation);
    expect(openTarget.mock.calls.filter(([target]) => target.target === "/private/handoff/package")).toHaveLength(0);
    expect(openTarget.mock.calls.filter(([target]) => target.target === "/private/returns/handoff-1")).toHaveLength(2);
  });

  it("opens the package fallback when Jianying automation cannot import the material", async () => {
    const openTarget = vi.fn(async () => undefined);
    const bridge = new JianyingDesktopBridge({
      root,
      packageBuilder,
      findApp: async () => ({ appVersion: "11.5.3", appPath: "/Applications/VideoFusion-macOS.app", draftRoot: "/private/drafts" }),
      resolveReturnFolder: async () => "/private/returns/handoff-1",
      openTarget,
      automate: async () => ({
        attempted: true,
        projectCreated: true,
        materialImported: false,
        timelineImported: false,
        error: "jianying-ui-version-unsupported",
      }),
      adapters: [],
    });

    const result = await bridge.open(await bridge.prepare(handoff, draft));

    expect(result).toMatchObject({
      packageFolderOpened: true,
      returnFolderOpened: true,
      automation: { attempted: true, materialImported: false, error: "jianying-ui-version-unsupported" },
    });
  });

  it("keeps the package available and redacts local paths when opening Jianying fails", async () => {
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => null,
      openTarget: async () => { throw new Error("failed at /Users/test-user/secret/Jianying.app"); },
    });
    const prepared = await bridge.prepare(handoff, draft);

    const opened = await bridge.open(prepared);
    expect(opened).toMatchObject({ opened: false, targetKind: "package-folder", error: "jianying-open-failed" });
    expect(JSON.stringify(opened)).not.toContain("/Users/person");
    expect(prepared.package).toMatchObject(packageValue);
  });

  it("rejects an adapter receipt bound to another package", async () => {
    const adapter: JianyingDraftAdapter = {
      id: "jianying-9-adapter", adapterVersion: "1.0.0",
      supports: () => true,
      install: async () => ({ adapterId: "jianying-9-adapter", adapterVersion: "1.0.0", appVersion: "9.0.0", packageDigest: "f".repeat(64), draftDigest: "e".repeat(64) }),
    };
    const bridge = new JianyingDesktopBridge({
      root, packageBuilder,
      findApp: async () => ({ appVersion: "9.0.0", appPath: "/Applications/Jianying.app", draftRoot: "/tmp/test-jianying-drafts" }),
      openTarget: async () => undefined,
      adapters: [adapter],
    });

    await expect(bridge.prepare(handoff, draft)).resolves.toMatchObject({
      mode: "file-package", status: "degraded", degradedReason: "jianying-install-receipt-mismatch",
    });
  });
});
