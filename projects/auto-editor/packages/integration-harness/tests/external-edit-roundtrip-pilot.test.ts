import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { runExternalEditRoundtripPilot } from "../src/external-edit-roundtrip-pilot";

describe("external edit roundtrip pilot", () => {
  let root: string | null = null;

  afterEach(async () => {
    if (root) await rm(root, { recursive: true, force: true });
    root = null;
  });

  it("creates a package fallback and an explicitly confirmed immutable V2 from synthetic media", async () => {
    root = await mkdtemp(join(tmpdir(), "external-edit-pilot-test-"));
    const report = await runExternalEditRoundtripPilot(root);

    expect(report).toMatchObject({
      syntheticOnly: true,
      publishingClaimed: false,
      mode: "file-package",
      degradedReason: "jianying-not-installed",
      v1Preserved: true,
      explicitCandidateConfirmation: true,
      v2Playable: true,
      handoffReturned: true,
      fullRuntimeRestartRecovery: "not-verified",
    });
    expect(report.initialDraftCount).toBe(2);
    expect(report.finalDraftCount).toBe(3);
    expect(report.packageFiles.length).toBeGreaterThan(1);
  }, 120_000);
});
