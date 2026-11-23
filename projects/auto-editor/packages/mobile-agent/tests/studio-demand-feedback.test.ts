import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import type { ContentDemand, MaterialCard, StudioMediaAsset } from "@wearable/contracts";

import {
  assertCustomerFeedbackConsistency,
  buildOfflineRevisionProposal,
  CustomerMaterialLibrary,
  OFFLINE_STUDIO_CAPABILITY,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  explicitlyRequestedMaterialCards,
  normalizeDemandText,
  searchDemandBoundDraftCards,
  VISUAL_CONTENT_MISMATCH_NEED,
  type CreateDemandInput,
  type StudioEditorialVisualEvidence,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "demand-session-1",
};

const baseDemand: Omit<CreateDemandInput, "operationKey"> = {
  contentType: "single-product",
  goal: "讲解合成戒指",
  targetAudience: "本地合成测试观众",
  desiredEffect: "理解示例流程",
  mustInclude: [],
  mustAvoid: [],
  submittedBy: "operator-on-behalf",
};

describe("studio demand and evidence matching", () => {
  const roots: string[] = [];

  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("normalizes only text shape and does not infer arbitrary meaning", () => {
    expect(normalizeDemandText("  Ａ  戒指\n近景  ")).toBe("A 戒指 近景");
    expect(normalizeDemandText("高端感")).toBe("高端感");
  });

  it("repairs an uploaded Chinese filename that was stored as UTF-8 mojibake", async () => {
    const { studio, cards } = await buildStudio(roots, [
      { observation: "客户上传素材：å¾®ä¿¡è§é¢2026-10-02_110736_447.mp4", durationMs: 17_903 },
      { observation: "纯色合成商品夹具 1" },
      { observation: "纯色合成商品夹具 2" },
      { observation: "纯色合成商品夹具 3" },
    ]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      goal: "只使用微信视频2026-10-02_110736_447.mp4，不使用纯色合成夹具。",
      operationKey: "mojibake-file-name-demand",
    });

    const assessment = await studio.assessDemand(scope, demand.id, "mojibake-file-name-match");

    expect(assessment.verdict).toBe("ready");
    expect(explicitlyRequestedMaterialCards(cards, demand).map((card) => card.id))
      .toEqual([cards[0]!.id]);
  });

  it("blocks a named scenic video during matching when the goal asks for a product video", async () => {
    const { studio } = await buildStudio(roots, [
      { observation: "客户上传素材：clip-1.mp4", durationMs: 17_903 },
    ], {
      resolveEditorialVisualEvidence: async (cards) => cards.map((card) => ({
        sourceAssetId: card.sourceAssetId,
        sourceDigest: card.sourceDigest,
        sourceStartMs: card.sourceStartMs,
        sourceEndMs: card.sourceEndMs,
        receiptJobId: "vision-scenic-1",
        provider: {
          layer: "vision" as const,
          adapterName: "mlx-vlm-local",
          modelName: "qwen3-vl-2b-instruct-4bit",
          modelVersion: "test",
          checkpointDigest: "a".repeat(64),
        },
        observations: [
          "visible-scene|0|17903|飞机机翼、天空、云层和黄昏地平线",
          "visible-product|0|17903|飞机机翼",
        ],
      })),
    });
    const productDemand = await studio.createDemand(scope, {
      ...baseDemand,
      goal: "只使用 clip-1.mp4 生成商品介绍短片",
      operationKey: "visual-mismatch-product-demand",
    });

    const blocked = await studio.assessDemand(scope, productDemand.id, "visual-mismatch-product-match");

    expect(blocked.verdict).toBe("blocked");
    expect(blocked.unmetNeeds).toContain(VISUAL_CONTENT_MISMATCH_NEED);
    expect(blocked.safeScope).toContain("不会进入任务书或生成成片");

    const scenicDemand = await studio.createDemand(scope, {
      ...baseDemand,
      goal: "只使用 clip-1.mp4 生成黄昏飞行风景短片",
      operationKey: "visual-mismatch-scenic-demand",
    });
    const ready = await studio.assessDemand(scope, scenicDemand.id, "visual-mismatch-scenic-match");
    expect(ready.verdict).toBe("ready");
  });

  it("turns a decorated fixed offline phrase into a reviewable revision proposal", () => {
    const feedback = {
      id: "customer-feedback-1",
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      demandId: "demand-1",
      briefId: "brief-1",
      draftId: "draft-1",
      satisfaction: "partially-satisfied" as const,
      issueCategories: ["style" as const],
      comment: "1. 请让设计师观点更突出。",
      mustKeep: ["成品结尾"],
      mustRemove: [],
      needsReshoot: [],
      submittedBy: "customer-self-declared" as const,
      actorId: scope.actorId,
      publishClaimed: false as const,
      createdAt: "2026-09-11T08:00:00.000Z",
    };

    const proposal = buildOfflineRevisionProposal({
      identity: {
        id: "proposal-1",
        customerId: scope.customerId,
        sessionId: scope.sessionId,
        version: 1,
        demandId: feedback.demandId,
        feedbackId: feedback.id,
        parentDraftId: feedback.draftId,
        parentBriefId: feedback.briefId,
        capability: OFFLINE_STUDIO_CAPABILITY,
      },
      feedback,
      createdAt: "2026-09-11T08:01:00.000Z",
    });

    expect(proposal).toMatchObject({
      understoodFeedback: "客户表示部分满意；1. 请让设计师观点更突出。；保留：成品结尾",
      plannedChanges: ["提高设计师观点开场的相对占比"],
      retainedElements: ["成品结尾"],
      unsupportedRequests: [],
      captureGaps: [],
      status: "proposed",
      confirmedByActorId: null,
      confirmedAt: null,
    });

    const pacingProposal = buildOfflineRevisionProposal({
      identity: { ...proposal, id: "proposal-pacing", feedbackId: "feedback-pacing" },
      feedback: { ...feedback, id: "feedback-pacing", comment: "节奏太慢" },
      createdAt: "2026-09-11T08:02:00.000Z",
    });
    expect(pacingProposal.plannedChanges).toEqual([
      "仅在有安全时长预算的适用方向缩短非核心镜头；达到 30 秒下限时标记不适用",
    ]);
    expect(pacingProposal.unsupportedRequests).toEqual([]);
  });

  it.each([
    { issueCategories: ["fact-error"], mustRemove: [], needsReshoot: [] },
    { issueCategories: [], mustRemove: ["错误镜头"], needsReshoot: [] },
    { issueCategories: [], mustRemove: [], needsReshoot: ["补拍新场景"] },
  ] as const)("rejects contradictory satisfied feedback: %#", (blocking) => {
    expect(() => assertCustomerFeedbackConsistency({
      satisfaction: "satisfied",
      issueCategories: [...blocking.issueCategories],
      mustRemove: [...blocking.mustRemove],
      needsReshoot: [...blocking.needsReshoot],
    })).toThrow("satisfied-customer-feedback-contradiction");
  });

  it("disables the legacy revision write path before reading any brief or feedback", async () => {
    const { studio } = await buildStudio(roots, []);

    await expect(studio.reviseBrief(scope, "legacy-brief", "legacy-feedback", "legacy-revise"))
      .rejects.toThrow("structured-revision-confirmation-required");
    expect((await studio.listStudioHistory(scope)).briefs).toEqual([]);
  });

  it("creates a new demand version without overwriting the old one", async () => {
    const { studio } = await buildStudio(roots, []);
    const v1 = await studio.createDemand(scope, { ...baseDemand, operationKey: "demand-1" });
    const v2 = await studio.createDemand(scope, {
      ...baseDemand,
      goal: "新版目标",
      operationKey: "demand-2",
    });

    expect(v2.version).toBe(2);
    expect((await studio.getDemand(scope, v1.id)).status).toBe("superseded");
    expect((await studio.getDemand(scope, v2.id)).status).toBe("current");
    await expect(studio.assessDemand(scope, v1.id, "match-old"))
      .rejects.toThrow("demand-version-not-current");
  });

  it("accepts exact demand limits and rejects each overflow with a stable domain error", async () => {
    const { studio } = await buildStudio(roots, []);
    const items = Array.from({ length: 20 }, (_, index) =>
      `${String(index).padStart(2, "0")}${"项".repeat(198)}`);
    await expect(studio.createDemand(scope, {
      ...baseDemand,
      goal: "g".repeat(500),
      targetAudience: "a".repeat(200),
      desiredEffect: "e".repeat(500),
      mustInclude: items,
      mustAvoid: items.map((item) => item.replace("项", "避")),
      operationKey: "k".repeat(200),
    })).resolves.toMatchObject({ version: 1 });

    const failures: Array<[CreateDemandInput, string]> = [
      [{ ...baseDemand, goal: "g".repeat(501), operationKey: "goal-over" }, "demand-goal-too-long"],
      [{ ...baseDemand, targetAudience: "a".repeat(201), operationKey: "audience-over" }, "demand-target-audience-too-long"],
      [{ ...baseDemand, desiredEffect: "e".repeat(501), operationKey: "effect-over" }, "demand-desired-effect-too-long"],
      [{ ...baseDemand, mustInclude: Array.from({ length: 21 }, (_, index) => `item-${index}`), operationKey: "array-over" }, "demand-must-include-too-many"],
      [{ ...baseDemand, mustAvoid: ["x".repeat(201)], operationKey: "item-over" }, "demand-must-avoid-item-too-long"],
      [{ ...baseDemand, operationKey: "k".repeat(201) }, "demand-operation-key-too-long"],
    ];
    for (const [input, error] of failures) {
      await expect(studio.createDemand(scope, input)).rejects.toThrow(error);
    }
  });

  it.each([
    ["ready", 4, [], []],
    ["partial", 2, [], ["还缺 2 段可用素材"]],
    ["blocked", 0, [], ["没有与需求类型匹配的可用素材"]],
  ] as const)("returns an explainable %s assessment", async (verdict, count, mustInclude, unmetNeeds) => {
    const { studio, cards } = await buildStudio(roots, Array.from({ length: count }, (_, index) => ({
      observation: index === 0 ? "戒指近景" : `合成镜头-${index + 1}`,
    })));
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      mustInclude: [...mustInclude],
      operationKey: `demand-${verdict}`,
    });

    const assessment = await studio.assessDemand(scope, demand.id, `match-${verdict}`);

    expect(assessment.verdict).toBe(verdict);
    expect(assessment.unmetNeeds).toEqual(expect.arrayContaining([...unmetNeeds]));
    expect(assessment.matchedEvidence.every((row) => row.materialCardIds.length > 0)).toBe(true);
    expect(assessment.matchedEvidence.flatMap((row) => row.materialCardIds))
      .toEqual(expect.arrayContaining(cards.map((card) => card.id)));
    expect(assessment.safeScope).toContain("受众与预期效果");
    expect(JSON.stringify(assessment)).not.toMatch(/%|百分比/u);
  });

  it("uses explicit card evidence for required and forbidden phrases", async () => {
    const { studio, cards } = await buildStudio(roots, [
      { observation: "戒指近景 成品" },
      { observation: "戒指细节" },
      { observation: "包含禁用标识" },
      { observation: "佩戴效果" },
      { observation: "设计师观点" },
    ]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      mustInclude: ["成品"],
      mustAvoid: ["禁用标识"],
      operationKey: "demand-evidence",
    });

    const assessment = await studio.assessDemand(scope, demand.id, "match-evidence");

    expect(assessment.verdict).toBe("ready");
    expect(assessment.matchedEvidence).toContainEqual({
      requirement: "成品",
      materialCardIds: [cards[0]!.id],
      explanation: "该必须项能追溯到当前素材证据",
    });
    expect(assessment.unmetNeeds).toContain("1 段素材包含明确要求避开的内容");
    expect(assessment.matchedEvidence.flatMap((row) => row.materialCardIds)).not.toContain(cards[2]!.id);
  });

  it("keeps every required-evidence clip in the opportunity and confirmed brief", async () => {
    const { studio, assets, cards } = await buildStudio(roots, [
      { observation: "戒指近景" },
      { observation: "戒指细节" },
      { observation: "合成过程" },
      { observation: "佩戴效果" },
      { observation: "必须镜头" },
    ]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      mustInclude: ["必须镜头"],
      operationKey: "required-selection-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "required-selection-match");
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    const opportunity = discovery.opportunities[0]!;

    expect(opportunity.materialCardIds).toContain(cards[4]!.id);
    const assetsByCardId = new Map(cards.map((card, index) => [card.id, assets[index]!.id]));
    const selectedAssetIds = opportunity.materialCardIds.map((cardId) => assetsByCardId.get(cardId)!);
    const brief = await studio.confirmBrief(scope, {
      opportunityId: opportunity.id,
      matchAssessmentId: assessment.id,
      selectedAssetIds,
      confirmedFactIds: [],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "required-selection-brief",
    });
    expect(brief.selectedAssetIds).toContain(assets[4]!.id);
  });

  it("fails closed when required evidence cannot fit a renderable two-draft selection", async () => {
    const { studio } = await buildStudio(roots, [
      { durationMs: 10_000 },
      { durationMs: 10_000 },
      { durationMs: 10_000 },
      { durationMs: 10_000 },
      { durationMs: 8_000, observation: "必须镜头" },
    ]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      mustInclude: ["必须镜头"],
      operationKey: "required-unrenderable-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "required-unrenderable-match");

    expect(assessment.verdict).toBe("partial");
    expect(assessment.unmetNeeds)
      .toContain("现有素材无法组成两套 30—60 秒且选片不同的草稿");
    await expect(studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    })).rejects.toThrow("match-not-ready");
  });

  it("bounds a 100-card search when five mutually exclusive requirements cannot fit four clips", () => {
    const cards = Array.from({ length: 100 }, (_, index) => buildSearchCard(index));
    const requiredEvidence = Array.from({ length: 5 }, (_, requirementIndex) =>
      cards.filter((_, cardIndex) => cardIndex % 5 === requirementIndex).map((card) => card.id));
    const result = searchDemandBoundDraftCards(cards, requiredEvidence);
    expect(result.cards).toBeNull();
    expect(result.diagnostics).toMatchObject({
      candidateCount: 100,
      requirementCount: 5,
      evidenceLinksVisited: 100,
      budgetExceeded: false,
    });
    expect(result.diagnostics.searchOperationsVisited).toBe(0);
  });

  it("reports deterministic budget exhaustion without claiming more footage is needed", async () => {
    const cards = Array.from({ length: 80 }, (_, index) => ({
      ...buildSearchCard(index),
      sourceEndMs: index === 0 ? 10_000 : index >= 77 ? 10_500 : 8_000,
    }));
    const requiredEvidence = [[cards[0]!.id]];
    const bounded = searchDemandBoundDraftCards(cards, requiredEvidence);

    expect(bounded).toMatchObject({
      cards: null,
      diagnostics: {
        searchOperationsVisited: 50_000,
        budgetExceeded: true,
      },
    });
    const lateFeasible = searchDemandBoundDraftCards(
      [cards[0]!, ...cards.slice(-3)],
      requiredEvidence,
    );
    expect(lateFeasible.cards).not.toBeNull();

    const { studio } = await buildStudio(
      roots,
      Array.from({ length: 80 }, (_, index) => ({
        durationMs: index === 0 ? 10_000 : index >= 77 ? 10_500 : 8_000,
        observation: index === 0 ? "预算必选" : `其他镜头-${index}`,
      })),
    );
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      mustInclude: ["预算必选"],
      operationKey: "budget-exhaustion-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "budget-exhaustion-match");
    const budgetMessage = "搜索预算耗尽，需要人工复核或缩小候选范围";

    expect(assessment.verdict).not.toBe("ready");
    expect.soft(assessment.unmetNeeds).toEqual([budgetMessage]);
    expect.soft(assessment.recommendedCaptures).toEqual([budgetMessage]);
    expect(assessment.safeScope).toBe(
      `${budgetMessage}；当前结果不能证明素材不可用；` +
      "受众与预期效果仅为策划上下文，不是已核实素材事实",
    );
  }, 120_000);

  it("binds demand and match retries to actor, scope, normalized body, and operation key", async () => {
    const { studio } = await buildStudio(roots, []);
    const request = {
      ...baseDemand,
      goal: "  讲解   合成戒指 ",
      operationKey: "demand-idempotent",
    };
    const first = await studio.createDemand(scope, request);
    const replay = await studio.createDemand(scope, {
      ...request,
      goal: "讲解 合成戒指",
    });
    expect(replay).toEqual(first);
    await expect(studio.createDemand({ ...scope, actorId: "actor-b" }, request))
      .rejects.toThrow("demand-operation-actor-mismatch");
    await expect(studio.createDemand(scope, { ...request, desiredEffect: "其他效果" }))
      .rejects.toThrow("demand-operation-key-conflict");

    const match = await studio.assessDemand(scope, first.id, "match-idempotent");
    await expect(studio.assessDemand(scope, first.id, "match-idempotent")).resolves.toEqual(match);
    await expect(studio.assessDemand({ ...scope, actorId: "actor-b" }, first.id, "match-idempotent"))
      .rejects.toThrow("match-operation-actor-mismatch");
  });

  it("never replays an assessment or brief after its demand is superseded", async () => {
    const { studio, assets } = await buildStudio(roots, [{}, {}, {}, {}]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      operationKey: "current-gate-demand-v1",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "current-gate-match-v1");
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    const request = {
      opportunityId: discovery.opportunities[0]!.id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: [],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "current-gate-brief-v1",
    };
    await studio.confirmBrief(scope, request);
    await studio.createDemand(scope, {
      ...baseDemand,
      goal: "新版目标",
      operationKey: "current-gate-demand-v2",
    });

    await expect(studio.assessDemand(scope, demand.id, "current-gate-match-v1"))
      .rejects.toThrow("demand-version-not-current");
    await expect(studio.confirmBrief(scope, request))
      .rejects.toThrow("demand-version-not-current");
  });

  it("blocks opportunity discovery unless the demand and inventory still match a ready assessment", async () => {
    const partialSetup = await buildStudio(roots, [{}, {}]);
    const partialDemand = await partialSetup.studio.createDemand(scope, {
      ...baseDemand,
      operationKey: "partial-demand",
    });
    const partial = await partialSetup.studio.assessDemand(scope, partialDemand.id, "partial-match");
    await expect(partialSetup.studio.discoverOpportunities(scope, {
      demandId: partialDemand.id,
      matchAssessmentId: partial.id,
    })).rejects.toThrow("match-not-ready");

    const readySetup = await buildStudio(roots, [{}, {}, {}, {}]);
    const demand = await readySetup.studio.createDemand(scope, {
      ...baseDemand,
      operationKey: "ready-demand",
    });
    const assessment = await readySetup.studio.assessDemand(scope, demand.id, "ready-match");
    await readySetup.addCard({ observation: "后到素材" });
    await expect(readySetup.studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    })).rejects.toThrow("match-inventory-not-current");
  });

  it("ignores a caller-supplied historical asOf when checking match freshness", async () => {
    const { studio, addCard } = await buildStudio(roots, [{}, {}, {}, {}]);
    const demand = await studio.createDemand(scope, {
      ...baseDemand,
      operationKey: "historical-as-of-demand",
    });
    const assessment = await studio.assessDemand(scope, demand.id, "historical-as-of-match");
    await addCard({ observation: "评估后新增素材" });
    const historicalInput = {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
      asOf: assessment.createdAt,
    };

    await expect(studio.discoverOpportunities(scope, historicalInput))
      .rejects.toThrow("match-inventory-not-current");
  });

  it("propagates mandatory demand, match, and null revision lineage through opportunity and brief", async () => {
    const { studio, assets } = await buildStudio(roots, [{}, {}, {}, {}]);
    const demand = await studio.createDemand(scope, { ...baseDemand, operationKey: "lineage-demand" });
    const assessment = await studio.assessDemand(scope, demand.id, "lineage-match");
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    const opportunity = discovery.opportunities[0]!;

    expect(opportunity).toMatchObject({
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });
    const brief = await studio.confirmBrief(scope, {
      opportunityId: opportunity.id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: [],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "lineage-brief",
    });
    expect(brief).toMatchObject({
      demandId: demand.id,
      matchAssessmentId: assessment.id,
      parentBriefId: null,
      feedbackId: null,
      revisionProposalId: null,
    });

    const evidenceCardIds = new Set(assessment.matchedEvidence.flatMap((row) => row.materialCardIds));
    expect(evidenceCardIds.size).toBe(4);
  });

  it("rejects a selected asset that is not named by matched material evidence", async () => {
    const { studio, library, assets } = await buildStudio(roots, [{}, {}, {}, {}]);
    const extra = await importVideo(library, "unmatched-extra");
    const demand = await studio.createDemand(scope, { ...baseDemand, operationKey: "asset-gate-demand" });
    const assessment = await studio.assessDemand(scope, demand.id, "asset-gate-match");
    const discovery = await studio.discoverOpportunities(scope, {
      demandId: demand.id,
      matchAssessmentId: assessment.id,
    });

    await expect(studio.confirmBrief(scope, {
      opportunityId: discovery.opportunities[0]!.id,
      matchAssessmentId: assessment.id,
      selectedAssetIds: [extra.id, ...assets.slice(1).map((asset) => asset.id)],
      confirmedFactIds: [],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "asset-gate-brief",
    })).rejects.toThrow("selected-asset-not-in-match");
  });
});

type CardDefinition = Readonly<{
  observation?: string;
  contentType?: ContentDemand["contentType"];
  durationMs?: number;
}>;

async function buildStudio(
  roots: string[],
  definitions: readonly CardDefinition[],
  options: Readonly<{
    resolveEditorialVisualEvidence?: (
      cards: readonly MaterialCard[],
    ) => Promise<readonly StudioEditorialVisualEvidence[]>;
  }> = {},
) {
  const root = await mkdtemp(join(tmpdir(), "studio-demand-feedback-"));
  roots.push(root);
  const library = new CustomerMaterialLibrary(root, () => true);
  let nowIndex = 0;
  const studio = new PrivateContentStudio(
    library,
    new OfflineFixtureUnderstandingAdapter(),
    {
      now: () => new Date(Date.UTC(2026, 8, 11, 8, 0, nowIndex)).toISOString(),
      resolveEditorialVisualEvidence: options.resolveEditorialVisualEvidence === undefined
        ? undefined
        : async (_scope, cards) => options.resolveEditorialVisualEvidence!(cards),
    },
  );
  const assets: StudioMediaAsset[] = [];
  const cards: MaterialCard[] = [];
  const addCard = async (definition: CardDefinition = {}) => {
    nowIndex += 1;
    const asset = await importVideo(library, `clip-${nowIndex}`);
    const durationMs = definition.durationMs ?? 10_500;
    const card = await studio.analyze(scope, asset.id, {
      synthetic: true,
      contentType: definition.contentType ?? "single-product",
      segment: { startMs: 0, endMs: durationMs },
      ffprobe: {
        durationSeconds: durationMs / 1_000,
        hasVideo: true,
        width: 720,
        height: 1280,
      },
      observations: [definition.observation ?? `合成镜头-${nowIndex}`],
      merchandiseFacts: [],
    });
    assets.push(asset);
    cards.push(card);
    return card;
  };
  for (const definition of definitions) await addCard(definition);
  return { root, library, studio, assets, cards, addCard };
}

async function importVideo(library: CustomerMaterialLibrary, name: string) {
  return library.importOriginal(scope.actorId, scope.customerId, {
    requestId: `import-${name}`,
    bytes: Buffer.from(`synthetic-video-${name}`),
    mediaKind: "video",
    fileName: `${name}.mp4`,
    capturedAt: "2026-09-11T07:59:00.000Z",
    sessionId: scope.sessionId,
    viewPermission: "allowed",
    usePermission: "allowed",
  });
}

function buildSearchCard(index: number): MaterialCard {
  return {
    id: `search-card-${index}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    sourceAssetId: `search-asset-${index}`,
    sourceDigest: String(index).padStart(64, "0"),
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    contentType: "single-product",
    technicalUsable: true,
    technicalFlags: ["synthetic-scale-test"],
    observations: [`requirement-${index % 5}`],
    confirmedFacts: [],
    pendingFacts: [],
    capability: {
      mode: "offline-fallback",
      label: "离线/模拟分析",
      version: 1,
      provider: null,
    },
    createdAt: "2026-09-11T08:00:00.000Z",
  };
}
