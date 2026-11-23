import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  lstat,
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  symlink,
  unlink,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { join, relative, sep } from "node:path";
import { tmpdir } from "node:os";

import type {
  MaterialCard,
  ReferenceMaterialMappingPlan,
  ReferenceRightsDeclaration,
  ReferenceSample,
  ReferenceStyleAnalysis,
  ReferenceStyleSelection,
  StudioMediaAsset,
} from "@wearable/contracts";
import ffmpegPath from "ffmpeg-static";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  confirmReferenceMaterialMappingPlan,
  createReferenceMaterialMappingPlan,
  type ReferenceMappingAssetSnapshot,
} from "../src/reference-material-mapper";
import {
  renderReferenceVariants,
  type RenderReferenceVariantsInput,
  type ReferenceRenderAsset,
} from "../src/reference-studio-renderer";
import type { StudioScope } from "../src/studio-capabilities";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;
const createdAt = "2026-09-19T08:00:00.000Z";
const confirmedAt = "2026-09-19T09:00:00.000Z";
const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};
let anchorFixtureRoot = "";

describe("reference studio renderer", () => {
  let fixtureRoot: string;
  let fixtureMedia: Awaited<ReturnType<typeof createFixtureMedia>>;

  beforeAll(async () => {
    fixtureRoot = await mkdtemp(join(tmpdir(), "reference-studio-renderer-"));
    anchorFixtureRoot = fixtureRoot;
    fixtureMedia = await createFixtureMedia(fixtureRoot);
  }, 120_000);

  afterAll(async () => {
    await rm(fixtureRoot, { recursive: true, force: true });
  });

  it("renders four decodable and materially different transition variants with exact lineage", async () => {
    const input = await renderInput("transition-variants", { transitionMode: "fade-crossfade", overlay: true });

    const result = await renderReferenceVariants(input);

    expect(result.plans.map((item) => item.kind)).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
    expect(result.drafts.map((item) => item.id)).toHaveLength(4);
    expect(new Set(result.drafts.map((item) => item.outputDigest)).size).toBe(4);
    expect(result.plans[0]!.platformChangeCodes).toEqual([]);
    expect(result.plans[0]!.timeline.map((entry) => entry.sourceEndMs - entry.sourceStartMs))
      .toEqual([8_000, 8_000, 8_000, 8_000]);
    expect(result.plans[1]!.platformChangeCodes.length).toBeGreaterThan(0);
    expect(result.plans[2]!.platformChangeCodes.length).toBeGreaterThan(0);
    expect(result.plans[3]!.platformChangeCodes.length).toBeGreaterThan(0);
    expect(timelineIntervals(result.plans[1]!)).not.toEqual(timelineIntervals(result.plans[0]!));
    expect(timelineIntervals(result.plans[2]!)).not.toEqual(timelineIntervals(result.plans[0]!));
    expect(timelineIntervals(result.plans[3]!)).not.toEqual(timelineIntervals(result.plans[0]!));
    expect(result.plans.flatMap((plan) => plan.timeline.map((entry) => entry.transition)))
      .toEqual(expect.arrayContaining(["cut", "fade", "crossfade"]));
    expect(result.plans.every((plan) => plan.publishClaimed === false)).toBe(true);
    expect(result.drafts.every((draft) => draft.publishClaimed === false)).toBe(true);
    expect(result.drafts.every((draft, index) =>
      draft.bindings.variantPlan.id === result.plans[index]!.id &&
      draft.bindings.mappingPlan.id === input.mappingPlan.id &&
      !("demandId" in draft) && !("briefId" in draft),
    )).toBe(true);

    const expectedDigestById = new Map(input.assets.map((source) => [source.asset.id, source.asset.digest]));
    for (const plan of result.plans) {
      expect(plan.authorizedSources.every((source) =>
        source.sourceDigest === expectedDigestById.get(source.sourceAssetId) &&
        source.permission.sourceAssetId === source.sourceAssetId &&
        source.permission.sourceDigest === source.sourceDigest,
      )).toBe(true);
    }
    const probeEvidence = [];
    for (const [index, draft] of result.drafts.entries()) {
      const probe = await ffprobeVideo(draft.outputPath);
      probeEvidence.push({ path: draft.outputPath, digest: draft.outputDigest, ...probe });
      expect(probe).toMatchObject({ videoCodec: "h264", audioCodec: "aac", width: 720, height: 1280 });
      expect(result.reportInput.artifacts[index]).toMatchObject({
        outputDigest: draft.outputDigest,
        durationSeconds: probe.durationSeconds,
        videoCodec: probe.videoCodec,
        audioCodec: probe.audioCodec,
        width: probe.width,
        height: probe.height,
      });
      expect(digest(await readFile(draft.outputPath))).toBe(draft.outputDigest);
      expect(probe.durationSeconds).toBeGreaterThanOrEqual(30);
      expect(probe.durationSeconds).toBeLessThanOrEqual(60);
      expect(isContained(await realpath(input.outputRoot), await realpath(draft.outputPath))).toBe(true);
    }
    expect(result.reportInput.artifacts.map((artifact) => artifact.outputDigest))
      .toEqual(result.drafts.map((draft) => draft.outputDigest));
    expect(result.reportInput.executedTransitions.map((transition) => transition.kind))
      .toEqual(expect.arrayContaining(["fade", "crossfade"]));
    expect(result.reportInput.publishClaimed).toBe(false);
    console.info("REFERENCE_FFPROBE_EVIDENCE", JSON.stringify(probeEvidence));
  }, 180_000);

  it("shortens the editorial opening and derives all four produced variants from that revised master", async () => {
    const input = await renderInput("revise-editorial-opening", {
      transitionMode: "cut",
      overlay: false,
    });
    const opening = input.mappingPlan.mappings.find((mapping) => mapping.ruleId === "rule-rhythm")!;

    const result = await renderReferenceVariants({
      ...input,
      revisionOverride: {
        kind: "shorten-opening",
        target: "editorial-master",
        requestedSeconds: 1,
        trimMs: 1_000,
      },
    });

    expect(result.plans.map((plan) => plan.kind)).toEqual([
      "editorial-master", "xiaohongshu", "douyin", "wechat-channels",
    ]);
    expect(result.drafts).toHaveLength(4);
    expect(result.reportInput.artifacts).toHaveLength(4);
    for (const plan of result.plans) {
      const revisedOpening = plan.timeline.find((entry) => entry.mappingId === opening.id)!;
      // All platforms derive from the requested one-second master trim.
      // Douyin also applies its separately recorded, existing one-frame trim.
      expect(revisedOpening.sourceStartMs).toBe(opening.sourceStartMs! + 1_000
        + (plan.kind === "douyin" ? 40 : 0));
    }
  }, 180_000);

  it("shortens only the requested platform and produces no unrelated artifacts", async () => {
    const input = await renderInput("revise-platform-opening", {
      transitionMode: "cut",
      overlay: false,
    });

    const result = await renderReferenceVariants({
      ...input,
      revisionOverride: {
        kind: "shorten-opening",
        target: "xiaohongshu",
        requestedSeconds: 1,
        trimMs: 1_000,
      },
    });

    expect(result.plans.map((plan) => plan.kind)).toEqual(["xiaohongshu"]);
    expect(result.drafts.map((draft) => draft.bindings.variantPlan.kind)).toEqual(["xiaohongshu"]);
    expect(result.reportInput.artifacts.map((artifact) => artifact.kind)).toEqual(["xiaohongshu"]);
    expect(result.reportInput.sourceLineage.every((lineage) => lineage.kind === "xiaohongshu")).toBe(true);
    expect(result.reportInput.executedTransitions.every((transition) =>
      transition.variantKind === "xiaohongshu")).toBe(true);
    expect(result.plans[0]!.timeline[0]!.sourceStartMs).toBe(1_000);
  }, 180_000);

  it("rejects an opening trim that cannot leave a renderable first clip", async () => {
    const input = await renderInput("revise-opening-unachievable", {
      transitionMode: "cut",
      overlay: false,
      singleSegmentMs: 500,
    });

    await expect(renderReferenceVariants({
      ...input,
      revisionOverride: {
        kind: "shorten-opening",
        target: "editorial-master",
        requestedSeconds: 0.5,
        trimMs: 520,
      },
    })).rejects.toThrow("reference-revision-prompt-unachievable");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("renders an all-cut timeline and synthesizes AAC for sources without audio", async () => {
    const input = await renderInput("cut-no-audio", { transitionMode: "cut", overlay: false });

    const result = await renderReferenceVariants(input);

    expect(result.plans.every((plan) => plan.timeline.every((entry) => entry.transition === "cut"))).toBe(true);
    for (const draft of result.drafts) {
      await expect(ffprobeVideo(draft.outputPath)).resolves.toMatchObject({ audioCodec: "aac" });
    }
    await expect(renderReferenceVariants(input)).rejects.toThrow("reference-render-output-exists");
  }, 180_000);

  it("never places unsupported transition evidence in an executable graph", async () => {
    const input = await renderInput("unsupported-transition", {
      transitionMode: "unsupported",
      overlay: false,
    });

    const result = await renderReferenceVariants(input);

    expect(result.plans.every((plan) => plan.timeline.every((entry) => entry.transition === "cut"))).toBe(true);
    expect(result.reportInput.executedTransitions).toEqual([]);
  }, 180_000);

  it("refuses external reference audio and forged external reference clips", async () => {
    const audioInput = await renderInput("external-audio", { transitionMode: "cut", overlay: false });
    audioInput.audioOverlaySourceAssetId = audioInput.mappingPlan.bindings.sample.sourceAssetId;
    await expect(renderReferenceVariants(audioInput)).rejects.toThrow("external-reference-content-forbidden");

    const clipInput = await renderInput("external-clip", { transitionMode: "cut", overlay: false });
    const forged = structuredClone(clipInput.mappingPlan);
    forged.mappings[0]!.sourceAssetId = forged.bindings.sample.sourceAssetId;
    forged.mappings[0]!.sourceDigest = forged.bindings.sample.sourceDigest;
    await expect(renderReferenceVariants({ ...clipInput, mappingPlan: forged }))
      .rejects.toThrow("external-reference-content-forbidden");
  });

  it("does not relabel a video-clips grant as an audio-track grant", async () => {
    const input = await renderInput("self-owned-audio", {
      transitionMode: "cut",
      overlay: true,
      sourceKind: "self-owned",
      reusableElements: ["video-clips"],
      selfOwnedAudio: true,
    });

    await expect(renderReferenceVariants(input)).rejects.toThrow("reference-audio-rule-binding-required");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("renders a self-owned audio rule only with its exact audio-track grant", async () => {
    const input = await renderInput("self-owned-audio-authorized", {
      transitionMode: "cut",
      overlay: true,
      sourceKind: "self-owned",
      reusableElements: ["audio-track"],
      selfOwnedAudio: true,
    });
    const result = await renderReferenceVariants(input);
    const overlay = result.plans[0]!.audioOverlay!;
    const source = result.plans[0]!.authorizedSources.find((row) => row.id === overlay.sourceBindingId)!;
    expect(source.reuseRights).toEqual({ id: input.rights.id, version: input.rights.version, element: "audio-track" });
    expect(result.drafts[0]!.lineage.some((row) => row.use === "audio-overlay")).toBe(true);
  }, 180_000);

  it("rejects output path traversal rather than using caller text as a path", async () => {
    const input = await renderInput("path-escape", { transitionMode: "cut", overlay: false });

    await expect(renderReferenceVariants({ ...input, operationKey: "../outside" }))
      .rejects.toThrow("reference-render-operation-key-invalid");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it.each([
    ["permission version", (input: RenderReferenceVariantsInput) => {
      input.assets[0]!.permission.version += 1;
    }, "reference-render-permission-mismatch"],
    ["withdrawal", (input: RenderReferenceVariantsInput) => {
      input.assets[0]!.withdrawn = true;
    }, "reference-render-source-not-authorized"],
    ["verified digest", (input: RenderReferenceVariantsInput) => {
      input.assets[0]!.verifiedDigest = digest("other-verified-digest");
    }, "reference-render-source-digest-mismatch"],
    ["source bytes", (input: RenderReferenceVariantsInput) => {
      input.assets[0]!.bytes = new Uint8Array([1, 2, 3]);
    }, "reference-render-source-digest-mismatch"],
  ] as const)("rejects a current-asset %s mismatch before creating partial outputs", async (
    suffix,
    mutate,
    expected,
  ) => {
    const input = await renderInput(`mismatch-${suffix.replaceAll(" ", "-")}`, {
      transitionMode: "cut",
      overlay: false,
    });
    mutate(input);

    await expect(renderReferenceVariants(input)).rejects.toThrow(expected);
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("rejects unconfirmed and digest-mutated mapping plans", async () => {
    const unconfirmedInput = await renderInput("unconfirmed", { transitionMode: "cut", overlay: false });
    const unconfirmed = structuredClone(unconfirmedInput.mappingPlan);
    unconfirmed.status = "draft";
    unconfirmed.confirmedByActorId = null;
    unconfirmed.confirmationReceiptId = null;
    unconfirmed.confirmedAt = null;
    await expect(renderReferenceVariants({ ...unconfirmedInput, mappingPlan: unconfirmed }))
      .rejects.toThrow("confirmed-reference-mapping-plan-required");

    const staleInput = await renderInput("stale-plan", { transitionMode: "cut", overlay: false });
    const stale = structuredClone(staleInput.mappingPlan);
    stale.mappings[0]!.sourceStartMs! += 1;
    await expect(renderReferenceVariants({ ...staleInput, mappingPlan: stale }))
      .rejects.toThrow("reference-mapping-input-stale");
  });

  it("rejects current rights that relabel the bound reference sample", async () => {
    const input = await renderInput("rights-sample-relabel", { transitionMode: "cut", overlay: false });
    const relabeledRights = structuredClone(input.rights);
    relabeledRights.sample.sourceAssetId = "other-reference-asset";

    await expect(renderReferenceVariants({
      ...input,
      rights: relabeledRights,
      ffmpegPath: null,
      ffprobePath: null,
    }))
      .rejects.toThrow("reference-render-rights-binding-mismatch");
  });

  it("snapshots caller-owned bytes before any await boundary", async () => {
    const input = await renderInput("immutable-bytes", { transitionMode: "cut", overlay: false });
    const expected = input.assets[0]!.asset.digest;
    const pending = renderReferenceVariants(input);
    input.assets[0]!.bytes.fill(0);
    const result = await pending;
    expect(result.plans[0]!.authorizedSources.find((row) => row.sourceAssetId === input.assets[0]!.asset.id)?.sourceDigest)
      .toBe(expected);
  }, 180_000);

  it("rejects a staged pathname replacement instead of publishing bytes under an old digest", async () => {
    const input = await renderInput("stage-byte-race", { transitionMode: "cut", overlay: false });
    const preload = await enableAnchorPreload("replace-stage", {
      replacementPath: fixtureMedia.paths[3]!,
    });
    try {
      await expect(renderReferenceVariants(input)).rejects.toThrow("reference-render-stage-identity-changed");
    } finally {
      preload.restore();
    }
    expect(await readFile(preload.markerPath, "utf8")).toBe("stage-replaced");
    expect(await readdir(input.outputRoot)).toEqual([]);
  }, 180_000);

  it("rejects a deterministic metadata-probe-to-pin pathname replacement", async () => {
    const input = await renderInput("metadata-pin-race", { transitionMode: "cut", overlay: false });
    const wrapper = join(fixtureRoot, "metadata-pin-race-probe.cjs");
    const marker = `${wrapper}.marker`;
    await writeFile(wrapper, probeWrapper([
      `const findStage = (directory) => {\n  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {\n    const candidate = require("node:path").join(directory, entry.name);\n    if (entry.isDirectory()) { const found = findStage(candidate); if (found) return found; }\n    else if (entry.name === "00-editorial-master.mp4") return candidate;\n  }\n  return null;\n};`,
      `if (!fs.existsSync(${JSON.stringify(marker)})) {\n  const stagePath = findStage(${JSON.stringify(fixtureRoot)});\n  if (stagePath) {\n    const replacement = stagePath + ".replacement";\n    fs.copyFileSync(${JSON.stringify(fixtureMedia.paths[3]!)}, replacement);\n    fs.renameSync(replacement, stagePath);\n    fs.writeFileSync(${JSON.stringify(marker)}, "metadata-probed-before-stage-replacement");\n  }\n}`,
    ]));
    await chmod(wrapper, 0o755);

    await expect(renderReferenceVariants({ ...input, ffprobePath: wrapper }))
      .rejects.toThrow("reference-render-stage-identity-changed");
    expect(await readFile(marker, "utf8")).toBe("metadata-probed-before-stage-replacement");
    expect(await readdir(input.outputRoot)).toEqual([]);
  }, 180_000);

  it("rechecks final descriptor bytes after the promotion helper returns", async () => {
    const input = await renderInput("final-byte-race", { transitionMode: "cut", overlay: false });
    const preload = await enableAnchorPreload("mutate-final-after-promote");
    try {
      await expect(renderReferenceVariants(input)).rejects.toThrow("reference-render-final-digest-mismatch");
    } finally {
      preload.restore();
    }
    expect(await readFile(preload.markerPath, "utf8")).toBe("final-mutated");
    expect(await readdir(input.outputRoot)).toEqual([]);
  }, 180_000);

  it("rejects an overlay asset that is not bound to an audio rule", async () => {
    const input = await renderInput("non-audio-overlay", { transitionMode: "cut", overlay: false });
    input.audioOverlaySourceAssetId = input.assets[0]!.asset.id;
    await expect(renderReferenceVariants(input)).rejects.toThrow("reference-audio-rule-binding-required");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("bounds a hung probe and cleans temporary output", async () => {
    const input = await renderInput("probe-timeout", { transitionMode: "cut", overlay: false });
    const script = join(fixtureRoot, "hung-probe.sh");
    await writeFile(script, "#!/bin/sh\nsleep 2\n", "utf8");
    await chmod(script, 0o755);
    await expect(renderReferenceVariants({
      ...input,
      ffprobePath: script,
      processLimits: { timeoutMs: 100, maxOutputBytes: 1024, killGraceMs: 50 },
    })).rejects.toThrow("ffprobe-reference-inspection-timeout");
    expect(await readdir(input.outputRoot)).toEqual([]);
  }, 10_000);

  it("bounds subprocess output and cleans temporary output", async () => {
    const input = await renderInput("probe-overflow", { transitionMode: "cut", overlay: false });
    const script = join(fixtureRoot, "loud-probe.sh");
    await writeFile(script, "#!/bin/sh\nyes x | head -c 4096\n", "utf8");
    await chmod(script, 0o755);
    await expect(renderReferenceVariants({
      ...input,
      ffprobePath: script,
      processLimits: { timeoutMs: 2_000, maxOutputBytes: 256, killGraceMs: 50 },
    })).rejects.toThrow("ffprobe-reference-inspection-output-overflow");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("cleans an owned run when a later failure merely mentions the output-exists sentinel", async () => {
    const input = await renderInput("owned-run-sentinel-text", { transitionMode: "cut", overlay: false });
    const script = join(fixtureRoot, "sentinel-text-probe.sh");
    await writeFile(script, "#!/bin/sh\necho downstream-reference-render-output-exists >&2\nexit 1\n", "utf8");
    await chmod(script, 0o755);

    await expect(renderReferenceVariants({ ...input, ffprobePath: script }))
      .rejects.toThrow("ffprobe-reference-inspection-failed");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("does not classify an EEXIST after run ownership as a preexisting run", async () => {
    const input = await renderInput("owned-create-eexist", { transitionMode: "cut", overlay: false });
    const preload = await enableAnchorPreload("fail-owned-output-create-eexist");
    try {
      await expect(renderReferenceVariants(input)).rejects.toThrow("injected-owned-output-eexist");
    } finally {
      preload.restore();
    }
    expect(await readFile(preload.markerPath, "utf8")).toBe("owned-output-create-eexist");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("removes its uniquely owned run when the output helper times out after mkdir", async () => {
    const input = await renderInput("anchor-timeout-after-create", { transitionMode: "cut", overlay: false });
    const preload = await enableAnchorPreload("hang-after-create");
    try {
      await expect(renderReferenceVariants({
        ...input,
        processLimits: { timeoutMs: 350, killGraceMs: 50 },
      })).rejects.toThrow("reference-render-output-create-timeout");
    } finally {
      preload.restore();
    }
    expect(await readFile(preload.markerPath, "utf8")).toBe("run-created");
    expect(await readdir(input.outputRoot)).toEqual([]);
  }, 30_000);

  it("reports cleanup failure honestly when the trusted root stays moved", async () => {
    const input = await renderInput("anchor-timeout-root-moved", { transitionMode: "cut", overlay: false });
    const backup = `${input.outputRoot}-backup`;
    const external = join(fixtureRoot, "anchor-timeout-root-external");
    await mkdir(external);
    const preload = await enableAnchorPreload("hang-after-create");
    const rendering = renderReferenceVariants({
      ...input,
      processLimits: { timeoutMs: 1_000, killGraceMs: 50 },
    }).then(
      (value) => ({ value, error: null as Error | null }),
      (error: unknown) => ({ value: null, error: error instanceof Error ? error : new Error(String(error)) }),
    );
    await waitFor(async () => (await lstat(preload.markerPath).catch(() => null))?.isFile() === true);
    await rename(input.outputRoot, backup);
    await symlink(external, input.outputRoot);
    const outcome = await rendering;
    const message = outcome.error?.message ?? "";
    preload.restore();
    await unlink(input.outputRoot);
    await rename(backup, input.outputRoot);
    expect(message).toContain("reference-render-output-create-timeout");
    expect(message).toContain("reference-render-output-cleanup-failed");
    expect(await readdir(external)).toEqual([]);
    expect((await readdir(input.outputRoot)).some((name) => name.startsWith("reference-run-"))).toBe(true);
  }, 30_000);

  it("does not treat two mapping IDs for the same interval as a platform reorder", async () => {
    const input = await renderInput("duplicate-interval", {
      transitionMode: "cut", overlay: false, duplicateOpeningInterval: true,
    });
    const result = await renderReferenceVariants(input);
    const identities = result.plans.map((plan) => plan.timeline.map((entry) => {
      const source = plan.authorizedSources.find((row) => row.id === entry.sourceBindingId)!;
      return [source.sourceAssetId, source.sourceDigest, entry.sourceStartMs, entry.sourceEndMs];
    }));
    expect(new Set(identities.map((rows) => JSON.stringify(rows))).size).toBe(4);
    // A real distinct interval may still be moved ahead of the duplicates.
    // Verify the source interval changed, not merely the mapping identifier.
    expect(identities[1]![0]).not.toEqual(identities[0]![0]);
  }, 180_000);

  it("renders four bounded variants from one source interval longer than 30 seconds", async () => {
    const input = await renderInput("single-segment", {
      transitionMode: "cut", overlay: false, singleSegmentMs: 32_000,
    });
    const result = await renderReferenceVariants(input);
    expect(result.plans.every((plan) => plan.timeline.length === 1)).toBe(true);
    expect(new Set(result.plans.map((plan) => JSON.stringify([
      plan.timeline[0]!.sourceStartMs, plan.timeline[0]!.sourceEndMs,
    ]))).size).toBe(4);
    expect(result.drafts.every((draft) => draft.durationSeconds >= 30)).toBe(true);
  }, 180_000);

  it.each([30_000, 30_100, 30_490])(
    "uses frame-aligned interval changes for a %dms single source",
    async (singleSegmentMs) => {
      const input = await renderInput(`single-edge-${singleSegmentMs}`, {
        transitionMode: "cut", overlay: false, singleSegmentMs,
      });
      const result = await renderReferenceVariants(input);
      expect(result.drafts.every((draft) => draft.durationSeconds >= 30)).toBe(true);
      expect(new Set(result.plans.map((plan) => JSON.stringify(plan.timeline.map((entry) => [
        entry.sourceStartMs, entry.sourceEndMs,
      ])))).size).toBe(4);
      expect(result.plans[1]!.platformChangeCodes).toEqual(["platform.xiaohongshu.trim-start-frame"]);
      expect(result.plans[2]!.platformChangeCodes).toEqual(["platform.douyin.tighten-cut-boundaries"]);
      expect(result.plans[3]!.platformChangeCodes).toEqual(["platform.wechat-channels.trim-end-frame"]);
    },
    180_000,
  );

  it.each([30_000, 30_080])("keeps a %dms two-clip edge case renderable and distinct", async (twoSegmentTotalMs) => {
    const input = await renderInput(`two-edge-${twoSegmentTotalMs}`, {
      transitionMode: "cut", overlay: false, twoSegmentTotalMs,
    });
    const result = await renderReferenceVariants(input);
    expect(result.drafts.every((draft) => draft.durationSeconds >= 30)).toBe(true);
    expect(new Set(result.plans.map((plan) => JSON.stringify(timelineIntervals(plan)))).size).toBe(4);
  }, 180_000);

  it.each([30_000, 60_000])(
    "keeps an exact %dms two-clip timeline bounded when reorder moves a fade to or from the opening",
    async (twoSegmentTransitionTimelineMs) => {
      const input = await renderInput(`two-transition-edge-${twoSegmentTransitionTimelineMs}`, {
        transitionMode: "fade-crossfade",
        overlay: false,
        twoSegmentTransitionTimelineMs,
      });
      const result = await renderReferenceVariants(input);
      expect(result.plans[0]!.timeline[1]!.transition).toBe("fade");
      expect(result.plans[1]!.timeline[0]!.mappingId).toBe(result.plans[0]!.timeline[1]!.mappingId);
      for (const [index, plan] of result.plans.entries()) {
        expect(Math.max(30_000, effectiveTimelineMs(plan.timeline))).toBeLessThanOrEqual(60_000);
        expect(result.drafts[index]!.durationSeconds).toBeGreaterThanOrEqual(30);
        expect(result.drafts[index]!.durationSeconds).toBeLessThanOrEqual(60);
      }
    },
    180_000,
  );

  it.each([
    [30_000, "fade", "activate"],
    [30_000, "fade", "deactivate"],
    [30_000, "crossfade", "activate"],
    [30_000, "crossfade", "deactivate"],
    [60_000, "fade", "activate"],
    [60_000, "fade", "deactivate"],
    [60_000, "crossfade", "activate"],
    [60_000, "crossfade", "deactivate"],
  ] as const)(
    "keeps an exact %dms %s transition bounded when reorder must %s it",
    async (transitionTimelineMs, transitionKind, direction) => {
      const input = await renderInput(
        `reverse-transition-${transitionTimelineMs}-${transitionKind}-${direction}`,
        {
          transitionMode: "fade-crossfade",
          overlay: false,
          transitionReorder: { transitionTimelineMs, transitionKind, direction },
        },
      );
      const result = await renderReferenceVariants(input);
      const transitionRuleId = `rule-transition-${transitionKind}`;
      const masterIndex = result.plans[0]!.timeline.findIndex((entry) => entry.ruleId === transitionRuleId);
      const reorderedIndex = result.plans[1]!.timeline.findIndex((entry) => entry.ruleId === transitionRuleId);
      expect([masterIndex, reorderedIndex]).toEqual(direction === "activate" ? [0, 1] : [1, 0]);
      expect(result.plans[0]!.timeline[masterIndex]!.transition)
        .toBe(direction === "activate" ? "cut" : transitionKind);
      expect(result.plans[1]!.timeline[reorderedIndex]!.transition)
        .toBe(direction === "activate" ? transitionKind : "cut");
      for (const [index, plan] of result.plans.entries()) {
        const expectedDurationMs = Math.max(30_000, effectiveTimelineMs(plan.timeline));
        expect(Math.abs(result.drafts[index]!.durationSeconds * 1_000 - expectedDurationMs))
          .toBeLessThanOrEqual(120);
        expect(result.drafts[index]!.durationSeconds).toBeGreaterThanOrEqual(30);
        expect(result.drafts[index]!.durationSeconds).toBeLessThanOrEqual(60);
      }
    },
    180_000,
  );

  it("rejects a transition longer than an adjacent clip before ffmpeg graph execution", async () => {
    const input = await renderInput("short-transition-boundary", {
      transitionMode: "fade-crossfade", overlay: false, shortTransitionBoundary: true,
    });
    await expect(renderReferenceVariants(input)).rejects.toThrow("reference-transition-duration-invalid");
    expect(await readdir(input.outputRoot)).toEqual([]);
  });

  it("supports control-like path characters without a concat manifest", async () => {
    const input = await renderInput("path-characters", { transitionMode: "cut", overlay: false });
    const unusual = `${input.outputRoot}-quote-'\\line\nbreak`;
    await rename(input.outputRoot, unusual);
    input.outputRoot = unusual;
    const result = await renderReferenceVariants(input);
    expect(result.drafts).toHaveLength(4);
  }, 180_000);

  it("pins promotion to the trusted root while a symlink swap overlaps the protected write", async () => {
    const input = await renderInput("root-swap-owned-run", { transitionMode: "cut", overlay: false });
    const backup = `${input.outputRoot}-backup`;
    const external = join(fixtureRoot, "root-swap-external");
    await mkdir(external);
    const preload = await enableAnchorPreload("swap-root-during-promote", {
      outputRoot: input.outputRoot,
      backup,
      external,
    });
    let result;
    try {
      result = await renderReferenceVariants(input);
    } finally {
      preload.restore();
    }
    expect(await readFile(preload.markerPath, "utf8")).toBe("promotion-root-swapped");
    expect(await readdir(external)).toEqual([]);
    const rootEntries = await readdir(input.outputRoot, { withFileTypes: true });
    expect(rootEntries).toHaveLength(1);
    expect(rootEntries[0]!.isDirectory()).toBe(true);
    const realRoot = await realpath(input.outputRoot);
    expect(result.drafts.every((draft) =>
      draft.outputPath.startsWith(join(realRoot, rootEntries[0]!.name, sep)))).toBe(true);
    for (const draft of result.drafts) {
      expect(digest(await readFile(draft.outputPath))).toBe(draft.outputDigest);
      await expect(ffprobeVideo(draft.outputPath)).resolves.toMatchObject({
        videoCodec: "h264", audioCodec: "aac", width: 720, height: 1280,
      });
    }
  }, 180_000);

  it("does not depend on or overwrite a fixed legacy target", async () => {
    const input = await renderInput("legacy-target", { transitionMode: "cut", overlay: false });
    const runDigest = digest(`${input.mappingPlan.id}\0${input.mappingPlan.contentDigest}\0${input.operationKey}`).slice(0, 24);
    const racedTarget = join(input.outputRoot, `reference-${runDigest}-editorial-master.mp4`);
    await writeFile(racedTarget, "do-not-overwrite", { flag: "wx" });
    const result = await renderReferenceVariants(input);
    expect(await readFile(racedTarget, "utf8")).toBe("do-not-overwrite");
    expect(result.drafts.every((draft) => draft.outputPath !== racedTarget)).toBe(true);
  }, 180_000);

  it("excludes a current-customer audio rule backed by video from the visual timeline", async () => {
    const input = await renderInput("current-video-audio-rule", {
      transitionMode: "cut", overlay: true, audioFromVideo: true,
    });
    const result = await renderReferenceVariants(input);
    expect(result.plans.every((plan) => plan.timeline.every((entry) => entry.ruleId !== "rule-audio"))).toBe(true);
    const overlayBinding = result.plans[0]!.audioOverlay!.sourceBindingId;
    const overlaySource = result.plans[0]!.authorizedSources.find((source) => source.id === overlayBinding)!;
    expect(overlaySource.sourceAssetId).toBe("asset-video-1");
  }, 180_000);

  it("uses a self-owned video audio rule with audio-track rights only", async () => {
    const input = await renderInput("self-owned-video-audio-rule", {
      transitionMode: "cut", overlay: true, audioFromVideo: true,
      selfOwnedAudio: true, sourceKind: "self-owned", reusableElements: ["audio-track"],
    });
    const result = await renderReferenceVariants(input);
    const overlayBinding = result.plans[0]!.audioOverlay!.sourceBindingId;
    const overlaySource = result.plans[0]!.authorizedSources.find((source) => source.id === overlayBinding)!;
    expect(overlaySource.reuseRights?.element).toBe("audio-track");
    expect(result.plans.every((plan) => plan.timeline.every((entry) => entry.sourceBindingId !== overlayBinding))).toBe(true);
  }, 180_000);

  it("renders a confirmed plan whose authorized materials come from a prior customer session", async () => {
    const input = await renderInput("historical-material-render", {
      transitionMode: "cut",
      overlay: false,
      historicalMaterials: true,
    });

    expect(input.mappingPlan.mappings.every((mapping) =>
      mapping.sourceProvenance === "customer-authorized-history")).toBe(true);
    const result = await renderReferenceVariants(input);

    expect(result.drafts).toHaveLength(4);
    expect(result.drafts.every((draft) => draft.status === "reviewable")).toBe(true);
  }, 180_000);

  async function renderInput(
    operationKey: string,
    options: Readonly<{
      transitionMode: "cut" | "fade-crossfade" | "unsupported";
      overlay: boolean;
      sourceKind?: ReferenceSample["sourceKind"];
      reusableElements?: ReferenceRightsDeclaration["reusableElements"];
      selfOwnedAudio?: boolean;
      duplicateOpeningInterval?: boolean;
      singleSegmentMs?: number;
      shortTransitionBoundary?: boolean;
      twoSegmentTotalMs?: number;
      twoSegmentTransitionTimelineMs?: number;
      transitionReorder?: Readonly<{
        transitionTimelineMs: 30_000 | 60_000;
        transitionKind: "fade" | "crossfade";
        direction: "activate" | "deactivate";
      }>;
      audioFromVideo?: boolean;
      historicalMaterials?: boolean;
    }>,
  ): Promise<RenderReferenceVariantsInput> {
    const outputRoot = join(fixtureRoot, `outputs-${operationKey}`);
    await mkdir(outputRoot, { recursive: false });
    const sourceKind = options.sourceKind ?? "external-style-only";
    const selfOwnedAudio = options.selfOwnedAudio === true;
    const materialAssets = options.historicalMaterials
      ? fixtureMedia.assets.map((asset) => ({ ...asset, sessionId: "prior-customer-session" }))
      : fixtureMedia.assets;
    const sampleSource = selfOwnedAudio
      ? materialAssets[options.audioFromVideo ? 1 : 4]!
      : null;
    const sample: ReferenceSample = {
      id: `reference-sample-${operationKey}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 2,
      sourceAssetId: sampleSource?.id ?? `external-reference-${operationKey}`,
      sourceDigest: sampleSource?.digest ?? digest(`external-reference-${operationKey}`),
      sourceKind,
      collectionMode: "historical-pointer",
      candidateScope: "reference-only",
      status: "ready",
      viewPermission: "allowed",
      usePermission: "allowed",
      createdAt,
    };
    const sampleBinding = {
      id: sample.id,
      version: sample.version,
      customerId: sample.customerId,
      sessionId: sample.sessionId,
      sourceAssetId: sample.sourceAssetId,
      sourceDigest: sample.sourceDigest,
      sourceKind: sample.sourceKind,
      collectionMode: sample.collectionMode,
      candidateScope: sample.candidateScope,
    } as const;
    const reusableElements = options.reusableElements ?? [];
    const rights: ReferenceRightsDeclaration = {
      id: `reference-rights-${operationKey}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 3,
      contentDigest: digest(`rights-${operationKey}-${reusableElements.join("-")}`),
      sample: sampleBinding,
      sourceKind,
      reusableElements,
      actorId: scope.actorId,
      createdAt,
    };
    const rightsBinding = {
      id: rights.id,
      version: rights.version,
      customerId: rights.customerId,
      sessionId: rights.sessionId,
      referenceSampleId: sample.id,
      referenceSampleVersion: sample.version,
      referenceSampleDigest: sample.sourceDigest,
      sourceKind: rights.sourceKind,
      reusableElements: rights.reusableElements,
      contentDigest: rights.contentDigest,
    } as const;
    const transitionEvidence = options.transitionMode === "fade-crossfade"
      ? [
          { id: "transition-fade", atMs: 16_000, kind: "fade" as const, confidence: 0.95 },
          { id: "transition-crossfade", atMs: 24_000, kind: "crossfade" as const, confidence: 0.92 },
        ]
      : options.transitionMode === "unsupported"
        ? [
            { id: "transition-fade", atMs: 16_000, kind: "unsupported" as const, confidence: 0.4 },
            { id: "transition-crossfade", atMs: 24_000, kind: "unsupported" as const, confidence: 0.3 },
          ]
        : [];
    const rules: ReferenceStyleAnalysis["rules"] = [
      {
        id: "rule-rhythm",
        kind: "rhythm",
        description: "confirmed opening rhythm",
        referenceStartMs: 0,
        referenceEndMs: 8_000,
        evidenceIds: ["section-1"],
      },
      {
        id: "rule-narrative",
        kind: "narrative",
        description: "confirmed second chapter",
        referenceStartMs: 8_000,
        referenceEndMs: 16_000,
        evidenceIds: ["section-2"],
      },
      {
        id: "rule-transition-fade",
        kind: "transition",
        description: "confirmed fade",
        referenceStartMs: 16_000,
        referenceEndMs: 24_000,
        evidenceIds: options.transitionMode === "cut" ? ["section-3"] : ["transition-fade"],
      },
      {
        id: "rule-transition-crossfade",
        kind: "transition",
        description: "confirmed crossfade",
        referenceStartMs: 24_000,
        referenceEndMs: 32_000,
        evidenceIds: options.transitionMode === "cut" ? ["section-4"] : ["transition-crossfade"],
      },
      {
        id: "rule-audio",
        kind: "audio",
        description: "confirmed customer audio overlay",
        referenceStartMs: 0,
        referenceEndMs: 32_000,
        evidenceIds: ["beat-1"],
      },
    ];
    if (options.shortTransitionBoundary) {
      rules[0] = { ...rules[0]!, referenceEndMs: 200 };
      rules[2] = { ...rules[2]!, referenceStartMs: 200, referenceEndMs: 32_000 };
    }
    const analysis: ReferenceStyleAnalysis = {
      id: `reference-analysis-${operationKey}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 4,
      contentDigest: digest(`analysis-${operationKey}`),
      bindings: { sample: sampleBinding, rights: rightsBinding },
      cuts: [],
      sections: [0, 1, 2, 3].map((index) => ({
        id: `section-${index + 1}`,
        startMs: index * 8_000,
        endMs: (index + 1) * 8_000,
        summary: `section ${index + 1}`,
      })),
      transcriptSpans: [],
      beatMarkers: [],
      transitions: transitionEvidence,
      rules,
      warnings: [],
      confidence: 0.9,
      status: "complete",
      capability: {
        mode: "provider",
        label: "local reference evidence",
        version: 1,
        provider: "local-reference-v1",
      },
      createdAt,
    };
    const selection: ReferenceStyleSelection = {
      id: `reference-selection-${operationKey}`,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 5,
      contentDigest: digest(`selection-${operationKey}`),
      bindings: {
        sample: sampleBinding,
        rights: rightsBinding,
        analysis: {
          id: analysis.id,
          version: analysis.version,
          customerId: analysis.customerId,
          sessionId: analysis.sessionId,
          contentDigest: analysis.contentDigest,
        },
      },
      selectedRuleIds: options.transitionReorder
        ? options.transitionReorder.direction === "activate"
          ? [`rule-transition-${options.transitionReorder.transitionKind}`, "rule-rhythm"]
          : ["rule-rhythm", `rule-transition-${options.transitionReorder.transitionKind}`]
        : options.singleSegmentMs
        ? ["rule-rhythm"]
        : options.twoSegmentTransitionTimelineMs ? ["rule-rhythm", "rule-transition-fade"]
        : options.twoSegmentTotalMs ? ["rule-rhythm", "rule-narrative"]
        : options.shortTransitionBoundary ? ["rule-rhythm", "rule-transition-fade"] : rules.map((rule) => rule.id),
      actorId: scope.actorId,
      confirmationReceiptId: `selection-confirmation-${operationKey}`,
      confirmedAt: createdAt,
      status: "confirmed",
      createdAt,
    };
    const cards = materialAssets.map((asset, index) => ({
      ...materialCard(asset, index),
      sessionId: options.historicalMaterials ? "prior-customer-session" : scope.sessionId,
    }));
    if (options.duplicateOpeningInterval) cards[1] = { ...cards[1]!, confirmedFacts: [] };
    if (options.singleSegmentMs) cards[0] = { ...cards[0]!, sourceEndMs: options.singleSegmentMs };
    if (options.twoSegmentTotalMs) {
      cards[0] = { ...cards[0]!, sourceEndMs: Math.floor(options.twoSegmentTotalMs / 2) };
      cards[1] = { ...cards[1]!, sourceEndMs: Math.ceil(options.twoSegmentTotalMs / 2) };
    }
    if (options.twoSegmentTransitionTimelineMs) {
      const authorizedTotalMs = options.twoSegmentTransitionTimelineMs + 350;
      cards[0] = { ...cards[0]!, sourceEndMs: Math.floor(authorizedTotalMs / 2) };
      cards[2] = { ...cards[2]!, sourceEndMs: Math.ceil(authorizedTotalMs / 2) };
    }
    if (options.transitionReorder) {
      const transitionIndex = options.transitionReorder.transitionKind === "fade" ? 2 : 3;
      const authorizedTotalMs = options.transitionReorder.transitionTimelineMs +
        (options.transitionReorder.direction === "deactivate" ? 350 : 0);
      cards[0] = { ...cards[0]!, sourceEndMs: Math.floor(authorizedTotalMs / 2) };
      cards[transitionIndex] = {
        ...cards[transitionIndex]!,
        ...(options.transitionReorder.transitionKind === "crossfade" ? {
          sourceAssetId: fixtureMedia.assets[2]!.id,
          sourceDigest: fixtureMedia.assets[2]!.digest,
        } : {}),
        sourceEndMs: Math.ceil(authorizedTotalMs / 2),
      };
    }
    if (options.audioFromVideo) {
      cards[4] = {
        ...cards[4]!,
        sourceAssetId: fixtureMedia.assets[1]!.id,
        sourceDigest: fixtureMedia.assets[1]!.digest,
      };
    }
    if (options.shortTransitionBoundary) {
      cards[0] = { ...cards[0]!, sourceEndMs: 200 };
      cards[2] = { ...cards[2]!, sourceEndMs: 31_800 };
    }
    if (selfOwnedAudio) {
      cards[4] = {
        ...cards[4]!,
        sourceAssetId: sample.sourceAssetId,
        sourceDigest: sample.sourceDigest,
      };
    }
    const mappingAssets = cards.map((card) => mappingSnapshot(
      materialAssets.find((asset) => asset.id === card.sourceAssetId)!,
      card,
      operationKey,
    ));
    const draft = createReferenceMaterialMappingPlan({
      scope,
      sample,
      rights,
      analysis,
      selection,
      currentMaterials: options.historicalMaterials ? [] : cards,
      authorizedHistoricalMaterials: options.historicalMaterials ? cards : [],
      assets: mappingAssets,
    });
    const mappingPlan = confirmReferenceMaterialMappingPlan(
      draft,
      { id: draft.id, version: draft.version, contentDigest: draft.contentDigest },
      "reviewer-a",
      confirmedAt,
    );
    const renderAssets = mappingAssets.map((source): ReferenceRenderAsset => ({
      ...structuredClone(source),
      bytes: new Uint8Array(fixtureMedia.bytes[materialAssets.findIndex((asset) => asset.id === source.asset.id)]!),
    }));
    return {
      mappingPlan,
      rights,
      analysis,
      assets: renderAssets,
      outputRoot,
      operationKey,
      audioOverlaySourceAssetId: options.overlay
        ? fixtureMedia.assets[options.audioFromVideo ? 1 : 4]!.id
        : null,
      now: createdAt,
    };
  }
});

function materialCard(asset: StudioMediaAsset, index: number): MaterialCard {
  const ruleFacts = [
    "reference-rhythm:rule-rhythm",
    "reference-narrative:rule-narrative",
    "reference-transition:fade:rule-transition-fade",
    "reference-transition:crossfade:rule-transition-crossfade",
    "reference-audio:rule-audio",
  ];
  return {
    id: `card-${index}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    sourceStartMs: 0,
    sourceEndMs: index === 4 ? 32_000 : [9_000, 8_500, 8_000, 9_000][index]!,
    contentType: "single-product",
    technicalUsable: true,
    technicalFlags: [asset.mediaKind === "audio" ? "ffprobe:audio" : "ffprobe:video"],
    observations: [`fixture-${index}`],
    confirmedFacts: [ruleFacts[index]!],
    pendingFacts: [],
    capability: {
      mode: "provider",
      label: "local material evidence",
      version: 1,
      provider: "local-material-v1",
    },
    createdAt,
  };
}

function mappingSnapshot(
  asset: StudioMediaAsset,
  card: MaterialCard,
  operationKey: string,
): ReferenceMappingAssetSnapshot {
  return {
    asset,
    permission: {
      id: `permission-${operationKey}-${asset.id}`,
      version: 1,
      epoch: 1,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      sourceAssetId: card.sourceAssetId,
      sourceDigest: card.sourceDigest,
      view: "allowed",
      use: "allowed",
    },
    withdrawn: false,
    verifiedDigest: card.sourceDigest,
  };
}

async function createFixtureMedia(root: string) {
  if (!ffmpegPath || !ffprobePath) throw new Error("bundled-ffmpeg-tools-required");
  const paths = [0, 1, 2, 3].map((index) => join(root, `video-${index}.mp4`));
  const colors = ["red", "blue", "green", "yellow"];
  for (const [index, path] of paths.entries()) {
    const videoSource = index === 0
      ? "testsrc2=s=320x568:r=25:d=32"
      : `color=c=${colors[index]}:s=320x568:r=25:d=${index <= 2 ? 32 : 9}`;
    const args = [
      "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", videoSource,
    ];
    if (index % 2 === 1) {
      args.push(
        "-f", "lavfi", "-i",
        `sine=frequency=${440 + index * 100}:sample_rate=44100:duration=${index <= 2 ? 32 : 9}`,
      );
    }
    args.push("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p");
    if (index % 2 === 1) args.push("-c:a", "aac", "-shortest");
    args.push("-y", path);
    await run(ffmpegPath, args);
  }
  const audioPath = join(root, "audio.m4a");
  await run(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=32",
    "-c:a", "aac", "-y", audioPath,
  ]);
  const mediaPaths = [...paths, audioPath];
  const bytes = await Promise.all(mediaPaths.map((path) => readFile(path)));
  const assets = bytes.map((value, index): StudioMediaAsset => ({
    id: index === 4 ? "asset-audio" : `asset-video-${index}`,
    importBindingId: index === 4 ? "asset-audio" : `asset-video-${index}`,
    blobId: `blob_${digest(value)}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    digest: digest(value),
    blobDigest: digest(value),
    mediaKind: index === 4 ? "audio" : "video",
    byteLength: value.byteLength,
    fileName: index === 4 ? "audio.m4a" : `video-${index}.mp4`,
    capturedAt: null,
    importedAt: createdAt,
    viewPermission: "allowed",
    usePermission: "allowed",
    captureContext: null,
  }));
  return { assets, bytes, paths, audioPath };
}

function timelineIntervals(plan: { timeline: Array<{ sourceAssetId?: never; sourceBindingId: string; sourceStartMs: number; sourceEndMs: number }> }) {
  return plan.timeline.map((entry) => [entry.sourceBindingId, entry.sourceStartMs, entry.sourceEndMs]);
}

function effectiveTimelineMs(timeline: Array<{
  sourceStartMs: number;
  sourceEndMs: number;
  transition: "cut" | "fade" | "crossfade";
}>): number {
  return timeline.reduce((total, entry, index) => total + entry.sourceEndMs - entry.sourceStartMs -
    (index > 0 && entry.transition !== "cut" ? 350 : 0), 0);
}

async function enableAnchorPreload(
  mode: "replace-stage" | "mutate-final-after-promote" | "hang-after-create" |
    "swap-root-during-promote" | "fail-owned-output-create-eexist",
  values: Readonly<{
    replacementPath?: string;
    outputRoot?: string;
    backup?: string;
    external?: string;
  }> = {},
): Promise<{ markerPath: string; restore: () => void }> {
  const preloadPath = join(fixturePreloadRoot(), `anchor-preload-${mode}.cjs`);
  const markerPath = `${preloadPath}.marker`;
  await rm(markerPath, { force: true });
  await writeFile(preloadPath, `
const fs = require("node:fs");
const path = require("node:path");
const mode = process.env.REFERENCE_RENDER_ANCHOR_TEST_MODE;
let input = null;
try { input = JSON.parse(Buffer.from(process.argv.at(-1), "base64url").toString("utf8")); } catch {}
if (mode === "replace-stage" && input && input.action === "promote") {
  const target = input.outputs[0].stagePath;
  const replacement = target + ".replacement";
  fs.copyFileSync(process.env.REFERENCE_RENDER_REPLACEMENT_PATH, replacement);
  fs.renameSync(replacement, target);
  fs.writeFileSync(process.env.REFERENCE_RENDER_MARKER_PATH, "stage-replaced");
}
if (mode === "mutate-final-after-promote" && input && input.action === "promote") {
  process.on("exit", () => {
    fs.writeFileSync(path.join(input.run.name, input.outputs[0].name), "tampered-final-bytes");
    fs.writeFileSync(process.env.REFERENCE_RENDER_MARKER_PATH, "final-mutated");
  });
}
if (mode === "hang-after-create") {
  const originalWriteFile = fs.writeFileSync;
  fs.writeFileSync = function(name, value, options) {
    const result = originalWriteFile.call(fs, name, value, options);
    if (path.basename(String(name)) === ".reference-owner") {
      fs.writeFileSync(process.env.REFERENCE_RENDER_MARKER_PATH, "run-created");
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10000);
    }
    return result;
  };
}
if (mode === "fail-owned-output-create-eexist") {
  const originalOpen = fs.openSync;
  fs.openSync = function(name, flags, ...rest) {
    if (String(name).endsWith(".mp4") && (flags & fs.constants.O_CREAT) !== 0) {
      fs.writeFileSync(process.env.REFERENCE_RENDER_MARKER_PATH, "owned-output-create-eexist");
      const error = new Error("injected-owned-output-eexist");
      error.code = "EEXIST";
      throw error;
    }
    return originalOpen.call(fs, name, flags, ...rest);
  };
}
if (mode === "swap-root-during-promote" && input && input.action === "promote") {
  const root = process.env.REFERENCE_RENDER_OUTPUT_ROOT;
  const backup = process.env.REFERENCE_RENDER_OUTPUT_BACKUP;
  fs.renameSync(root, backup);
  fs.symlinkSync(process.env.REFERENCE_RENDER_EXTERNAL_ROOT, root);
  fs.writeFileSync(process.env.REFERENCE_RENDER_MARKER_PATH, "promotion-root-swapped");
  process.on("exit", () => {
    fs.unlinkSync(root);
    fs.renameSync(backup, root);
  });
}
`, "utf8");
  const names = [
    "NODE_OPTIONS",
    "REFERENCE_RENDER_ANCHOR_TEST_MODE",
    "REFERENCE_RENDER_MARKER_PATH",
    "REFERENCE_RENDER_REPLACEMENT_PATH",
    "REFERENCE_RENDER_OUTPUT_ROOT",
    "REFERENCE_RENDER_OUTPUT_BACKUP",
    "REFERENCE_RENDER_EXTERNAL_ROOT",
  ] as const;
  const previous = new Map(names.map((name) => [name, process.env[name]]));
  process.env.NODE_OPTIONS = [previous.get("NODE_OPTIONS"), `--require=${preloadPath}`]
    .filter((value): value is string => Boolean(value)).join(" ");
  process.env.REFERENCE_RENDER_ANCHOR_TEST_MODE = mode;
  process.env.REFERENCE_RENDER_MARKER_PATH = markerPath;
  if (values.replacementPath) process.env.REFERENCE_RENDER_REPLACEMENT_PATH = values.replacementPath;
  if (values.outputRoot) process.env.REFERENCE_RENDER_OUTPUT_ROOT = values.outputRoot;
  if (values.backup) process.env.REFERENCE_RENDER_OUTPUT_BACKUP = values.backup;
  if (values.external) process.env.REFERENCE_RENDER_EXTERNAL_ROOT = values.external;
  return {
    markerPath,
    restore: () => {
      for (const name of names) {
        const value = previous.get(name);
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    },
  };
}

function fixturePreloadRoot(): string {
  if (!anchorFixtureRoot) throw new Error("reference-render-test-fixture-root-missing");
  return anchorFixtureRoot;
}

async function waitFor(predicate: () => Promise<boolean>, timeoutMs = 5_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("timed-out-waiting-for-test-condition");
}

async function ffprobeVideo(path: string): Promise<{
  durationSeconds: number;
  videoCodec: string;
  audioCodec: string;
  width: number;
  height: number;
}> {
  const { stdout } = await run(ffprobePath, [
    "-v", "error",
    "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
    "-of", "json",
    path,
  ]);
  const parsed = JSON.parse(stdout) as {
    streams: Array<{ codec_type: string; codec_name: string; width?: number; height?: number }>;
    format: { duration: string };
  };
  const video = parsed.streams.find((stream) => stream.codec_type === "video");
  const audio = parsed.streams.find((stream) => stream.codec_type === "audio");
  if (!video || !audio) throw new Error("expected-video-and-audio-streams");
  return {
    durationSeconds: Number(parsed.format.duration),
    videoCodec: video.codec_name,
    audioCodec: audio.codec_name,
    width: video.width!,
    height: video.height!,
  };
}

function isContained(root: string, candidate: string): boolean {
  const child = relative(root, candidate);
  return child.length > 0 && child !== ".." && !child.startsWith(`..${sep}`);
}

function digest(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

async function run(command: string, args: string[]): Promise<{ stdout: string; stderr: string }> {
  const result = await new Promise<{ code: number | null; stdout: string; stderr: string }>((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += String(chunk); });
    child.stderr.on("data", (chunk) => { stderr += String(chunk); });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
  if (result.code !== 0) throw new Error(`command-failed:${result.stderr}`);
  return result;
}

function probeWrapper(afterProbeStatements: readonly string[]): string {
  return `#!/usr/bin/env node
const cp = require("node:child_process");
const fs = require("node:fs");
const args = process.argv.slice(2);
const options = ["/dev/fd/3", "/proc/self/fd/3"].includes(args.at(-1))
  ? { encoding: "utf8", stdio: ["ignore", "pipe", "pipe", 3] }
  : { encoding: "utf8" };
const result = cp.spawnSync(${JSON.stringify(ffprobePath)}, args, options);
${afterProbeStatements.join("\n")}
process.stdout.write(result.stdout || "");
process.stderr.write(result.stderr || "");
process.exit(result.status ?? 1);
`;
}
