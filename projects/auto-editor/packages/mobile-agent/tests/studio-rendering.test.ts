import { access, realpath, rm, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { isAbsolute, join, relative } from "node:path";

import type { EditBlueprint } from "@wearable/contracts";
import { afterEach, describe, expect, it } from "vitest";

import {
  DeterministicStudioRenderer,
  OFFLINE_STUDIO_CAPABILITY,
  PlatformAutomaticEditorialAdapter,
  STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES,
  interpretOfflineStudioFeedback,
  type EditorialDecisionAdapter,
} from "../src";
import { prepareRenderableStudio, run, studioScope } from "./fixtures/private-studio-fixtures";

const ffprobePath = (createRequire(import.meta.url)("ffprobe-static") as { path: string }).path;

describe("private studio rendering", () => {
  const cleanup: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("creates two 30—60 second drafts with materially different timelines and exact lineage", async () => {
    const { root, studio, assets, demand, brief } = await prepareRenderableStudio();
    cleanup.push(root);

    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-1");

    expect(drafts).toHaveLength(2);
    expect(drafts.every((draft) =>
      draft.demandId === demand.id && draft.revisionProposalId === null)).toBe(true);
    expect(drafts[0]!.blueprintDigest).not.toBe(drafts[1]!.blueprintDigest);
    const blueprints = await Promise.all(drafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    expect(blueprints.map((blueprint) => blueprint.direction)).toEqual([
      "chronological-evidence-first",
      "designer-viewpoint-first",
    ]);
    expect(blueprints[0]!.shots.map((shot) => shot.sourceAssetId))
      .not.toEqual(blueprints[1]!.shots.map((shot) => shot.sourceAssetId));
    expect(blueprints[0]!.shots.map((shot) => shot.reason))
      .not.toEqual(blueprints[1]!.shots.map((shot) => shot.reason));
    const assetDigestById = new Map(assets.map((asset) => [asset.id, asset.blobDigest]));
    for (const draft of drafts) {
      expect(draft.durationSeconds).toBeGreaterThanOrEqual(30);
      expect(draft.durationSeconds).toBeLessThanOrEqual(60);
      expect(draft.sourceMap.every((row) =>
        row.sourceAssetId.startsWith("asset_") &&
        row.sourceDigest === assetDigestById.get(row.sourceAssetId) &&
        row.sourceEndMs > row.sourceStartMs &&
        row.reason.length > 0,
      )).toBe(true);
      expect((await stat(draft.outputPath)).size).toBeGreaterThan(0);
    }
    await studio.createDemand(studioScope, {
      contentType: "single-product",
      goal: "新版合成戒指目标",
      targetAudience: "本地合成测试观众",
      desiredEffect: "理解新版示例流程",
      mustInclude: [],
      mustAvoid: [],
      submittedBy: "operator-on-behalf",
      operationKey: "fixture-demand-2",
    });
    await expect(studio.createDrafts(studioScope, brief.id, "draft-op-1"))
      .rejects.toThrow("demand-version-not-current");
  }, 120_000);

  it("creates four source-bound platform versions through the automatic editor", async () => {
    const { root, studio, assets, demand, brief } = await prepareRenderableStudio({
      editorial: new PlatformAutomaticEditorialAdapter(),
      withMusicTrack: true,
      resolveEditorialTranscriptSpans: async (cards) => cards.flatMap((card) => [
        { sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest, startMs: 1_000, endMs: 2_500, text: "现在展示制作过程" },
        { sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest, startMs: 4_000, endMs: 6_000, text: "重点是自动完成剪辑" },
        { sourceAssetId: card.sourceAssetId, sourceDigest: card.sourceDigest, startMs: 7_000, endMs: 9_000, text: "最后检查成片结果" },
      ]),
      resolveEditorialVisualEvidence: async (cards) => cards.map((card, index) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        sourceStartMs: card.sourceStartMs,
        sourceEndMs: card.sourceEndMs,
        receiptJobId: `vision-job-${index + 1}`,
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-worker",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "2026-02",
          checkpointDigest: "a".repeat(64),
        },
        observations: [
          `visible-product|${card.sourceStartMs}|${card.sourceEndMs}|商品主体 ${index + 1}`,
          `visible-scene|${card.sourceStartMs}|${card.sourceEndMs}|珠宝制作场景`,
          "未确认人物身份",
        ],
      })),
      resolveEditorialBeatEvidence: async (cards) => cards.map((card) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        beatTimesMs: Array.from({ length: 42 }, (_value, index) => (index + 1) * 250),
        confidence: 0.91,
        detector: "audio-energy-onset-v1" as const,
      })),
    });
    cleanup.push(root);

    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-platforms");

    expect(drafts.map((draft) => draft.platformVariant)).toEqual([
      "editorial-master",
      "xiaohongshu",
      "douyin",
      "wechat-channels",
    ]);
    expect(new Set(drafts.map((draft) => draft.blueprintDigest)).size).toBe(4);
    expect(new Set(drafts.map((draft) => draft.outputDigest)).size).toBe(4);
    const assetDigestById = new Map(assets.map((asset) => [asset.id, asset.blobDigest]));
    for (const draft of drafts) {
      expect(draft.demandId).toBe(demand.id);
      expect(draft.durationSeconds).toBeGreaterThanOrEqual(30);
      expect(draft.durationSeconds).toBeLessThanOrEqual(60);
      expect(draft.sourceMap.every((row) =>
        row.sourceDigest === assetDigestById.get(row.sourceAssetId) &&
        row.sourceEndMs > row.sourceStartMs,
      )).toBe(true);
      expect(draft.captionCues?.length).toBeGreaterThan(0);
      expect(draft.captionCues?.every((cue) => cue.reviewStatus === "model-suggestion")).toBe(true);
      expect(draft.visualEvidence?.status).toBe("used");
      expect(draft.beatSyncPlan?.mode).toBe("beat-synced");
      expect(draft.beatSyncPlan?.alignedCutCount).toBe(draft.sourceMap.length - 1);
      expect(draft.musicMixPlan).toEqual(expect.objectContaining({
        mode: "mixed", trackStartMs: 1_000, musicGainDb: -18, originalGainDb: -2, loopToFit: true,
        rights: expect.objectContaining({ previewUseAllowed: true, publishUseAllowed: false }),
      }));
      expect(draft.visualEvidence?.items).toHaveLength(draft.sourceMap.length);
      expect(draft.visualEvidence?.items.every((item) =>
        item.provider.layer === "vision"
        && item.receiptJobId.startsWith("vision-job-")
        && item.observations.some((observation) => observation.endsWith("|珠宝制作场景"))
        && item.selectionReason.length > 0,
      )).toBe(true);
      expect((await stat(draft.outputPath)).size).toBeGreaterThan(0);
    }
  }, 180_000);

  it("uses real ffprobe to verify H.264/AAC 720x1280 output even when one input has no audio", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    if (!ffprobePath) throw new Error("ffprobe-binary-unavailable");

    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-probe");

    await access(ffprobePath);
    for (const draft of drafts) {
      const { stdout } = await run(ffprobePath, [
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
        "-of", "json",
        draft.outputPath,
      ]);
      const probe = JSON.parse(stdout) as {
        streams: Array<{ codec_type: string; codec_name: string; width?: number; height?: number }>;
        format: { duration: string };
      };
      expect(probe.streams).toContainEqual(expect.objectContaining({
        codec_type: "video",
        codec_name: "h264",
        width: 720,
        height: 1280,
      }));
      expect(probe.streams).toContainEqual(expect.objectContaining({
        codec_type: "audio",
        codec_name: "aac",
      }));
      expect(Number(probe.format.duration)).toBeGreaterThanOrEqual(30);
      expect(Number(probe.format.duration)).toBeLessThanOrEqual(60);
    }
  }, 120_000);

  it("replays the same draft operation and rejects the same key for another brief body", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const first = await studio.createDrafts(studioScope, brief.id, "draft-op-replay");

    await expect(studio.createDrafts(studioScope, brief.id, "draft-op-replay"))
      .resolves.toEqual(first);
    const revised = await confirmStructuredRevision(
      studio,
      first[0]!.id,
      "请让设计师观点更突出",
      "render-conflict",
    );
    await expect(studio.createDrafts(studioScope, revised.id, "draft-op-replay"))
      .rejects.toThrow("draft-operation-key-conflict");
  }, 120_000);

  it.each([
    {
      reason: " 1. 请让设计师观点更突出。 ",
      adjustment: "increase-opening-shot-share",
      applicableDirection: "designer-viewpoint-first",
      targetRole: "designer-viewpoint-open",
      auditText: "提高设计师观点开场角色的相对占比",
    },
    {
      reason: " • 请让成品结尾更突出！ ",
      adjustment: "increase-ending-shot-share",
      applicableDirection: "chronological-evidence-first",
      targetRole: "finished-product-close",
      auditText: "提高成品结尾角色的相对占比",
    },
  ] as const)("applies exact positive feedback only to the direction containing its semantic target role: $adjustment", async ({
    reason,
    adjustment,
    applicableDirection,
    targetRole,
    auditText,
  }) => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    expect(interpretOfflineStudioFeedback(reason)).toEqual({
      interpreter: "offline-rule-v2",
      adjustments: [adjustment],
    });
    expect(interpretOfflineStudioFeedback("随便改得高级一点")).toEqual({
      interpreter: "offline-rule-v2",
      adjustments: [],
    });

    const firstDrafts = await studio.createDrafts(studioScope, brief.id, "draft-op-feedback-v1");
    const firstBlueprints = await Promise.all(firstDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    const revised = await confirmStructuredRevision(
      studio,
      firstDrafts[0]!.id,
      reason,
      `audited-${adjustment}`,
    );
    expect(revised.reviewRequirements).toEqual(expect.arrayContaining([
      "离线反馈解释器：offline-rule-v2",
      `离线剪辑规则：${adjustment}`,
    ]));

    const revisedDrafts = await studio.createDrafts(
      studioScope,
      revised.id,
      `draft-op-feedback-v2-${adjustment}`,
    );
    const revisedBlueprints = await Promise.all(revisedDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    const timeline = (blueprint: EditBlueprint) => blueprint.shots.map((shot) => ({
      sourceAssetId: shot.sourceAssetId,
      sourceStartMs: shot.sourceStartMs,
      sourceEndMs: shot.sourceEndMs,
      timelineStartMs: shot.timelineStartMs,
    }));
    for (const index of [0, 1]) {
      const firstBlueprint = firstBlueprints[index]!;
      const revisedBlueprint = revisedBlueprints[index]!;
      const targetIndex = revisedBlueprint.shots.findIndex((shot) => shot.role === targetRole);
      const applies = revisedBlueprint.editorialDirection === applicableDirection;
      expect(targetIndex >= 0).toBe(applies);
      if (applies) {
        expect(timeline(revisedBlueprint)).not.toEqual(timeline(firstBlueprint));
        expect(revisedDrafts[index]!.outputDigest).not.toBe(firstDrafts[index]!.outputDigest);
        expect(revisedBlueprint.shots.some((shot) => shot.reason.includes(auditText))).toBe(true);
        expect(revisedBlueprint.shots[targetIndex]!.sourceEndMs - revisedBlueprint.shots[targetIndex]!.sourceStartMs)
          .toBe(firstBlueprint.shots[targetIndex]!.sourceEndMs - firstBlueprint.shots[targetIndex]!.sourceStartMs);
      } else {
        expect(timeline(revisedBlueprint)).toEqual(timeline(firstBlueprint));
        expect(revisedDrafts[index]!.outputDigest).toBe(firstDrafts[index]!.outputDigest);
        expect(revisedBlueprint.shots.some((shot) =>
          shot.reason.includes("不适用") && shot.reason.includes("镜头不变"),
        )).toBe(true);
      }
      expect(revisedDrafts[index]!.durationSeconds).toBeGreaterThanOrEqual(30);
      expect(revisedDrafts[index]!.durationSeconds).toBeLessThanOrEqual(60);
    }
  }, 120_000);

  it.each([
    {
      reason: "请让设计师观点更突出",
      adjustment: "increase-opening-shot-share",
      applicableDirection: "designer-viewpoint-first",
      targetRole: "designer-viewpoint-open",
    },
    {
      reason: "请让成品结尾更突出",
      adjustment: "increase-ending-shot-share",
      applicableDirection: "chronological-evidence-first",
      targetRole: "finished-product-close",
    },
  ] as const)("keeps exact-30-second drafts renderable and audits duration-floor as not-applicable: $adjustment", async ({
    reason,
    adjustment,
    applicableDirection,
    targetRole,
  }) => {
    const { root, studio, brief } = await prepareRenderableStudio({ segmentDurationMs: 10_000 });
    cleanup.push(root);
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, `draft-floor-v1-${adjustment}`);
    const firstBlueprints = await Promise.all(firstDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    expect(firstBlueprints.map((blueprint) => blueprint.durationSeconds)).toEqual([30, 30]);

    const revised = await confirmStructuredRevision(
      studio,
      firstDrafts[0]!.id,
      reason,
      `floor-${adjustment}`,
    );
    const revisedDrafts = await studio.createDrafts(
      studioScope,
      revised.id,
      `draft-floor-v2-${adjustment}`,
    );
    const revisedBlueprints = await Promise.all(revisedDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    const timeline = (blueprint: EditBlueprint) => blueprint.shots.map((shot) => ({
      sourceAssetId: shot.sourceAssetId,
      sourceStartMs: shot.sourceStartMs,
      sourceEndMs: shot.sourceEndMs,
      timelineStartMs: shot.timelineStartMs,
    }));

    for (const index of [0, 1]) {
      const firstBlueprint = firstBlueprints[index]!;
      const revisedBlueprint = revisedBlueprints[index]!;
      expect(revisedBlueprint.durationSeconds).toBe(30);
      expect(timeline(revisedBlueprint)).toEqual(timeline(firstBlueprint));
      expect(revisedDrafts[index]!.outputDigest).toBe(firstDrafts[index]!.outputDigest);
      expect(revisedBlueprint.shots.some((shot) => shot.reason.includes("不适用"))).toBe(true);
      expect(revisedBlueprint.shots.every((shot) => !shot.reason.includes("提高") && !shot.reason.includes("压缩")))
        .toBe(true);
      if (revisedBlueprint.editorialDirection === applicableDirection) {
        expect(revisedBlueprint.shots.some((shot) =>
          shot.role === targetRole && shot.reason.includes("duration-floor"),
        )).toBe(true);
      } else {
        expect(revisedBlueprint.shots.some((shot) => shot.reason.includes("本方向不含"))).toBe(true);
      }
    }
  }, 120_000);

  it("caps semantic-role trimming to the remaining duration budget", async () => {
    const { root, studio, brief } = await prepareRenderableStudio({ segmentDurationMs: 10_100 });
    cleanup.push(root);
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, "draft-budget-v1");
    const firstBlueprints = await Promise.all(firstDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    expect(firstBlueprints.map((blueprint) => blueprint.durationSeconds)).toEqual([30.3, 30.3]);

    const revised = await confirmStructuredRevision(
      studio,
      firstDrafts[0]!.id,
      "请让设计师观点更突出",
      "budget",
    );
    const revisedDrafts = await studio.createDrafts(studioScope, revised.id, "draft-budget-v2");
    const revisedBlueprints = await Promise.all(revisedDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    const timeline = (blueprint: EditBlueprint) => blueprint.shots.map((shot) => ({
      sourceAssetId: shot.sourceAssetId,
      sourceStartMs: shot.sourceStartMs,
      sourceEndMs: shot.sourceEndMs,
      timelineStartMs: shot.timelineStartMs,
    }));
    const evidenceFirstIndex = revisedBlueprints.findIndex((blueprint) =>
      blueprint.editorialDirection === "chronological-evidence-first");
    const viewpointFirstIndex = revisedBlueprints.findIndex((blueprint) =>
      blueprint.editorialDirection === "designer-viewpoint-first");
    expect(evidenceFirstIndex).toBeGreaterThanOrEqual(0);
    expect(viewpointFirstIndex).toBeGreaterThanOrEqual(0);

    expect(revisedBlueprints[evidenceFirstIndex]!.durationSeconds).toBe(30.3);
    expect(timeline(revisedBlueprints[evidenceFirstIndex]!)).toEqual(timeline(firstBlueprints[evidenceFirstIndex]!));
    expect(revisedDrafts[evidenceFirstIndex]!.outputDigest).toBe(firstDrafts[evidenceFirstIndex]!.outputDigest);
    expect(revisedBlueprints[viewpointFirstIndex]!.durationSeconds).toBeGreaterThanOrEqual(30);
    expect(revisedBlueprints[viewpointFirstIndex]!.durationSeconds).toBeLessThan(30.3);
    expect(timeline(revisedBlueprints[viewpointFirstIndex]!)).not.toEqual(timeline(firstBlueprints[viewpointFirstIndex]!));
    expect(revisedDrafts[viewpointFirstIndex]!.outputDigest).not.toBe(firstDrafts[viewpointFirstIndex]!.outputDigest);
    expect(revisedBlueprints[viewpointFirstIndex]!.shots.some((shot) =>
      shot.role === "designer-viewpoint-open" && shot.reason.includes("通过压缩其他镜头共"),
    )).toBe(true);
  }, 180_000);

  it("executes pacing feedback only on the applicable direction and changes real output when budget exists", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    expect(interpretOfflineStudioFeedback("节奏太慢")).toEqual({
      interpreter: "offline-rule-v2",
      adjustments: ["tighten-pacing"],
    });
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, "draft-pacing-v1");
    const firstBlueprints = await Promise.all(firstDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));

    const revised = await confirmStructuredRevision(studio, firstDrafts[0]!.id, "节奏太慢", "pacing");
    expect(revised.reviewRequirements).toContain("离线剪辑规则：tighten-pacing");
    const revisedDrafts = await studio.createDrafts(studioScope, revised.id, "draft-pacing-v2");
    const revisedBlueprints = await Promise.all(revisedDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    const timeline = (blueprint: EditBlueprint) => blueprint.shots.map((shot) => ({
      sourceStartMs: shot.sourceStartMs,
      sourceEndMs: shot.sourceEndMs,
      timelineStartMs: shot.timelineStartMs,
    }));
    const applicable = revisedBlueprints.findIndex((blueprint) =>
      blueprint.editorialDirection === "chronological-evidence-first");
    const notApplicable = revisedBlueprints.findIndex((blueprint) =>
      blueprint.editorialDirection === "designer-viewpoint-first");
    expect(timeline(revisedBlueprints[applicable]!)).not.toEqual(timeline(firstBlueprints[applicable]!));
    expect(revisedDrafts[applicable]!.outputDigest).not.toBe(firstDrafts[applicable]!.outputDigest);
    expect(revisedBlueprints[applicable]!.shots.some((shot) =>
      shot.role === "process-evidence-progress" && shot.reason.includes("压缩非核心镜头"),
    )).toBe(true);
    expect(timeline(revisedBlueprints[notApplicable]!)).toEqual(timeline(firstBlueprints[notApplicable]!));
    expect(revisedDrafts[notApplicable]!.outputDigest).toBe(firstDrafts[notApplicable]!.outputDigest);
    expect(revisedBlueprints[notApplicable]!.shots.some((shot) =>
      shot.reason.includes("不适用") && shot.reason.includes("镜头不变"),
    )).toBe(true);
  }, 180_000);

  it.each([
    { label: "30s", durations: [10_000, 10_000, 10_000, 10_000] },
    { label: "30.25s", durations: [10_084, 10_083, 10_083, 10_084] },
  ] as const)("marks pacing not-applicable at the $label safety floor", async ({ label, durations }) => {
    const { root, studio, brief } = await prepareRenderableStudio({ segmentDurationMs: durations });
    cleanup.push(root);
    const firstDrafts = await studio.createDrafts(studioScope, brief.id, `draft-pacing-floor-v1-${label}`);
    const firstBlueprints = await Promise.all(firstDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    expect(firstBlueprints[0]!.durationSeconds).toBe(label === "30s" ? 30 : 30.25);

    const revised = await confirmStructuredRevision(studio, firstDrafts[0]!.id, "节奏太慢", `pacing-floor-${label}`);
    const revisedDrafts = await studio.createDrafts(studioScope, revised.id, `draft-pacing-floor-v2-${label}`);
    const revisedBlueprints = await Promise.all(revisedDrafts.map((draft) =>
      studio.getBlueprint(studioScope, draft.blueprintId),
    ));
    expect(revisedBlueprints.map((blueprint) => blueprint.durationSeconds))
      .toEqual(firstBlueprints.map((blueprint) => blueprint.durationSeconds));
    expect(revisedDrafts.map((draft) => draft.outputDigest)).toEqual(firstDrafts.map((draft) => draft.outputDigest));
    expect(revisedBlueprints.every((blueprint) => blueprint.shots.some((shot) =>
      shot.reason.includes("不适用") && shot.reason.includes("镜头不变"),
    ))).toBe(true);
  }, 180_000);

  it.each([
    "不要让设计师观点提前",
    "不要让成品停留更久",
    "请把设计师观点提前",
    "请让成品停留更久",
    "请让设计师观点更突出，成品多停留",
    "请让设计师观点更突出，但不要改变工序证据",
    "请让成品结尾更突出并删除工序证据",
  ])("rejects negated, conflicting, combined, or extra feedback: %s", (reason) => {
    expect(interpretOfflineStudioFeedback(reason)).toEqual({
      interpreter: "offline-rule-v2",
      adjustments: [],
    });
  });

  it("fails closed before creating a revised brief for every non-whitelisted expression", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    for (const [index, reason] of [
      "不要让设计师观点提前",
      "不要让成品停留更久",
      "请让设计师观点更突出，成品多停留",
      "请让设计师观点更突出，但不要改变工序证据",
      "请让成品结尾更突出并删除工序证据",
    ].entries()) {
      const [draft] = await studio.createDrafts(studioScope, brief.id, "draft-op-rejected-feedback");
      const proposalId = await proposeStructuredRevision(
        studio,
        draft!.id,
        reason,
        `rejected-${index}`,
      );
      await expect(studio.confirmRevisionProposal(
        studioScope,
        proposalId,
        `confirm-rejected-${index}`,
      )).rejects.toThrow("revision-proposal-has-unsupported-requests");
    }
    expect(await studio.getBrief(studioScope, brief.id)).toEqual(brief);
  }, 120_000);

  it("rejects an unsupported change request instead of pretending offline language understanding", async () => {
    const { root, studio, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const [draft] = await studio.createDrafts(studioScope, brief.id, "draft-op-unsupported-feedback");
    const proposalId = await proposeStructuredRevision(
      studio,
      draft!.id,
      "随便改得高级一点",
      "unsupported",
    );

    await expect(studio.confirmRevisionProposal(
      studioScope,
      proposalId,
      "confirm-unsupported",
    )).rejects.toThrow("revision-proposal-has-unsupported-requests");
  }, 120_000);

  it("fails closed when a blueprint interval exceeds the real probed source duration", async () => {
    const { root, library, assets, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const selected = assets.slice(0, 3);
    const sources = await Promise.all(selected.map(async (asset) => ({
      assetId: asset.importBindingId,
      blobDigest: asset.blobDigest,
      bytes: await library.readOriginal(studioScope.actorId, studioScope.customerId, asset.id),
    })));
    const blueprint: EditBlueprint = {
      id: "blueprint_out_of_range",
      customerId: studioScope.customerId,
      sessionId: studioScope.sessionId,
      version: brief.version,
      briefId: brief.id,
      direction: "invalid-out-of-range-fixture",
      durationSeconds: 41,
      shots: selected.map((asset, index) => ({
        id: `shot-${index + 1}`,
        sourceAssetId: asset.id,
        sourceDigest: asset.blobDigest,
        sourceStartMs: 0,
        sourceEndMs: index === 2 ? 20_000 : 10_500,
        timelineStartMs: index * 10_500,
        reason: "synthetic range boundary",
      })),
      capability: OFFLINE_STUDIO_CAPABILITY,
      publishClaimed: false,
      createdAt: "2026-09-10T08:00:00.000Z",
    };
    const outputPath = join(root, "out-of-range.mp4");

    await expect(new DeterministicStudioRenderer(sources, { outputRoot: root }).render(blueprint, outputPath))
      .rejects.toThrow("studio-render-source-range-invalid");
    await expect(access(outputPath)).rejects.toThrow();
  }, 120_000);

  it("rejects alpha beta directions and foo bar roles despite different source sets", async () => {
    const { root, studio, brief } = await prepareRenderableStudio({ editorial: freeTextDifferenceAdapter });
    cleanup.push(root);

    await expect(studio.createDrafts(studioScope, brief.id, "draft-op-free-text-difference"))
      .rejects.toThrow("editorial-adapter-contract-mismatch");
  }, 120_000);

  it("never turns an adapter blueprint id into a path outside outputRoot", async () => {
    const { root, studio, brief } = await prepareRenderableStudio({ editorial: traversalIdAdapter });
    cleanup.push(root);
    const outputRoot = join(root, "drafts");
    const escapedPath = join(root, "escaped-review.mp4");

    const drafts = await studio.createDrafts(studioScope, brief.id, "draft-op-traversal-id");

    const realOutputRoot = await realpath(outputRoot);
    for (const draft of drafts) {
      const childPath = relative(realOutputRoot, await realpath(draft.outputPath));
      expect(childPath.length > 0 && !childPath.startsWith("..") && !isAbsolute(childPath)).toBe(true);
    }
    await expect(access(escapedPath)).rejects.toThrow();
  }, 120_000);

  it("makes the renderer reject a resolved output path outside its trusted root", async () => {
    const { root, library, assets, brief } = await prepareRenderableStudio();
    cleanup.push(root);
    const selected = assets.slice(0, 3);
    const sources = await Promise.all(selected.map(async (asset) => ({
      assetId: asset.importBindingId,
      blobDigest: asset.blobDigest,
      bytes: await library.readOriginal(studioScope.actorId, studioScope.customerId, asset.id),
    })));
    let timelineStartMs = 0;
    const shots = selected.map((asset, index) => {
      const shot = {
        id: `contained-shot-${index}`,
        sourceAssetId: asset.id,
        sourceDigest: asset.blobDigest,
        sourceStartMs: 0,
        sourceEndMs: 10_500,
        timelineStartMs,
        reason: `边界验证-${index}`,
      };
      timelineStartMs += 10_500;
      return shot;
    });
    const blueprint: EditBlueprint = {
      id: "contained-blueprint",
      customerId: studioScope.customerId,
      sessionId: studioScope.sessionId,
      version: brief.version,
      briefId: brief.id,
      direction: "containment-check",
      durationSeconds: 31.5,
      shots,
      capability: OFFLINE_STUDIO_CAPABILITY,
      publishClaimed: false,
      createdAt: "2026-09-10T08:00:00.000Z",
    };
    const trustedRoot = join(root, "trusted-output");
    const outsidePath = join(root, "outside.mp4");
    const renderer = new DeterministicStudioRenderer(
      sources,
      { outputRoot: trustedRoot },
    );

    await expect(renderer.render(blueprint, outsidePath))
      .rejects.toThrow("studio-render-output-outside-root");
    await expect(access(outsidePath)).rejects.toThrow();
  }, 120_000);
});

type RenderableStudio = Awaited<ReturnType<typeof prepareRenderableStudio>>["studio"];

async function proposeStructuredRevision(
  studio: RenderableStudio,
  draftId: string,
  comment: string,
  operationSuffix: string,
): Promise<string> {
  const feedback = await studio.recordCustomerFeedback(studioScope, {
    draftId,
    satisfaction: "partially-satisfied",
    issueCategories: ["style"],
    comment,
    mustKeep: [],
    mustRemove: [],
    needsReshoot: [],
    submittedBy: "customer-self-declared",
    operationKey: `structured-feedback-${operationSuffix}`,
  });
  const proposal = await studio.proposeRevision(
    studioScope,
    feedback.id,
    `structured-proposal-${operationSuffix}`,
  );
  return proposal.id;
}

async function confirmStructuredRevision(
  studio: RenderableStudio,
  draftId: string,
  comment: string,
  operationSuffix: string,
) {
  const proposalId = await proposeStructuredRevision(studio, draftId, comment, operationSuffix);
  return studio.confirmRevisionProposal(
    studioScope,
    proposalId,
    `structured-confirm-${operationSuffix}`,
  );
}

const freeTextDifferenceAdapter: EditorialDecisionAdapter = {
  capability: OFFLINE_STUDIO_CAPABILITY,
  async createBlueprints(input) {
    const selections = [
      [input.cards[0]!, input.cards[1]!, input.cards[2]!],
      [input.cards[3]!, input.cards[1]!, input.cards[0]!],
    ];
    return [0, 1].map((variant): EditBlueprint => {
      let timelineStartMs = 0;
      const shots = selections[variant]!.map((card, index) => {
        const shot: EditBlueprint["shots"][number] = {
          id: `free-text-shot-${variant}-${index}`,
          sourceAssetId: card.sourceAssetId,
          sourceDigest: card.sourceDigest,
          sourceStartMs: card.sourceStartMs,
          sourceEndMs: card.sourceEndMs,
          timelineStartMs,
          reason: `${variant === 0 ? "foo" : "bar"}-${index}`,
        };
        timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
        return shot;
      });
      return {
        id: `free-text-blueprint-${variant}`,
        customerId: input.scope.customerId,
        sessionId: input.scope.sessionId,
        version: input.brief.version,
        briefId: input.brief.id,
        direction: variant === 0 ? "alpha" : "beta",
        durationSeconds: timelineStartMs / 1_000,
        shots,
        capability: OFFLINE_STUDIO_CAPABILITY,
        publishClaimed: false,
        createdAt: input.createdAt,
      };
    });
  },
};

const traversalIdAdapter: EditorialDecisionAdapter = {
  capability: OFFLINE_STUDIO_CAPABILITY,
  async createBlueprints(input) {
    const selections = [
      [input.cards[0]!, input.cards[1]!, input.cards[2]!],
      [input.cards[3]!, input.cards[1]!, input.cards[0]!],
    ];
    return selections.map((cards, variant): EditBlueprint => {
      let timelineStartMs = 0;
      const shots = cards.map((card, index) => {
        const shot: EditBlueprint["shots"][number] = {
          id: `traversal-shot-${variant}-${index}`,
          sourceAssetId: card.sourceAssetId,
          sourceDigest: card.sourceDigest,
          sourceStartMs: card.sourceStartMs,
          sourceEndMs: card.sourceEndMs,
          timelineStartMs,
          role: STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[variant]!.roles[index]!,
          reason: STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[variant]!.reasons[index]!,
        };
        timelineStartMs += shot.sourceEndMs - shot.sourceStartMs;
        return shot;
      });
      return {
        id: variant === 0 ? "../../escaped-review" : "safe-review",
        customerId: input.scope.customerId,
        sessionId: input.scope.sessionId,
        version: input.brief.version,
        briefId: input.brief.id,
        editorialDirection: STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[variant]!.direction,
        direction: STUDIO_EDITORIAL_BLUEPRINT_STRUCTURES[variant]!.direction,
        durationSeconds: timelineStartMs / 1_000,
        shots,
        capability: OFFLINE_STUDIO_CAPABILITY,
        publishClaimed: false,
        createdAt: input.createdAt,
      };
    });
  },
};
