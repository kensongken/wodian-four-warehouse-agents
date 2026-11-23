import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { CaptureEnvelopeV1Input, MaterialCard, StudioCapability } from "@wearable/contracts";

import {
  CustomerMaterialLibrary,
  OFFLINE_STUDIO_CAPABILITY,
  OfflineFixtureUnderstandingAdapter,
  PrivateContentStudio,
  ReferenceSampleService,
  type CaptureEnvelopeAdapter,
  type MediaUnderstandingAdapter,
  type MediaUnderstandingRequest,
  type OfflineAnalysisFixture,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "studio-session-1",
};

const declaredFixture: OfflineAnalysisFixture = {
  synthetic: true,
  contentType: "single-product",
  segment: { startMs: 0, endMs: 10_000 },
  ffprobe: {
    durationSeconds: 10,
    hasVideo: true,
    width: 720,
    height: 1280,
  },
  observations: ["合成夹具声明：戒指近景"],
  merchandiseFacts: ["fact-material-18k: 材质为 18K 金"],
};

describe("private content studio", () => {
  let root: string;
  let library: CustomerMaterialLibrary;
  let studio: PrivateContentStudio;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "private-content-studio-"));
    library = new CustomerMaterialLibrary(root, () => true);
    studio = new PrivateContentStudio(
      library,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => "2026-09-10T08:00:00.000Z" },
    );
  });

  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("labels deterministic understanding as offline fallback and never invents facts", async () => {
    const asset = await importVideo(library, "one");

    const card = await studio.analyze(scope, asset.id, declaredFixture);

    expect(card.capability).toMatchObject({
      mode: "offline-fallback",
      label: "离线/模拟分析",
      provider: null,
    });
    expect(card.confirmedFacts).toEqual([]);
    expect(card.pendingFacts).toEqual(["fact-material-18k: 材质为 18K 金"]);
    expect(card.technicalFlags).toContain("ffprobe:video=720x1280");
  });

  it("puts an authorized hardware capture into the same usable material inventory", async () => {
    const adapter = captureAdapter({ viewPermission: "allowed", usePermission: "allowed" });

    const result = await studio.ingestCapture(scope, adapter, "hardware-request-1", declaredFixture);
    const inventory = await studio.inventory(scope, "2026-09-10T08:00:00.000Z");

    expect(result.status).toBe("ready");
    expect(result.material).toMatchObject({
      sourceAssetId: result.asset.id,
      contentType: "single-product",
    });
    expect(result.asset.captureContext).toEqual({
      envelopeId: "capture-hardware-request-1",
      adapterName: "synthetic-phone-hardware-bridge",
      adapterVersion: "1.0.0",
    });
    expect(inventory.items).toEqual([
      expect.objectContaining({ sourceAssetId: result.asset.id, materialCardId: result.material!.id }),
    ]);
  });

  it("keeps an unapproved hardware capture out of the usable material inventory", async () => {
    const result = await studio.ingestCapture(
      scope,
      captureAdapter(),
      "hardware-request-pending",
      declaredFixture,
    );

    expect(result.status).toBe("review-required");
    expect(result.material).toBeNull();
    await expect(studio.inventory(scope, "2026-09-10T08:00:00.000Z"))
      .resolves.toMatchObject({ items: [] });
    await expect(library.readOriginal(scope.actorId, scope.customerId, result.asset.id))
      .rejects.toThrow("asset-view-not-allowed");
  });

  it("rejects a hardware capture bound to another studio session", async () => {
    const adapter = captureAdapter({
      sessionId: "different-session",
      binding: { customerId: scope.customerId, sessionId: "different-session", verified: true },
      viewPermission: "allowed",
      usePermission: "allowed",
    });

    await expect(studio.ingestCapture(scope, adapter, "hardware-request-wrong-session", declaredFixture))
      .rejects.toThrow("capture-session-mismatch");
    await expect(library.listAssets(scope.actorId, scope.customerId)).resolves.toEqual([]);
  });

  it("rejects provider capability injection when the offline adapter is called directly", async () => {
    const asset = await importVideo(library, "direct-offline-adapter");
    const originalBytes = await library.readOriginal(scope.actorId, scope.customerId, asset.id);
    const adapter = new OfflineFixtureUnderstandingAdapter();

    await expect(adapter.analyze({
      scope,
      asset,
      originalBytes,
      fixture: declaredFixture,
      capability: {
        mode: "provider",
        label: "伪装成供应商分析",
        version: 1,
        provider: "evil",
      },
      createdAt: "2026-09-10T08:00:00.000Z",
    })).rejects.toThrow("offline-capability-mismatch");
  });

  it("cannot be changed into a provider by rewriting the offline adapter capability", async () => {
    const asset = await importVideo(library, "mutated-offline-adapter");
    const originalBytes = await library.readOriginal(scope.actorId, scope.customerId, asset.id);
    const adapter = new OfflineFixtureUnderstandingAdapter();
    let mutationRejected = false;

    try {
      (adapter as { capability: StudioCapability }).capability = {
        mode: "provider",
        label: "运行时伪装能力",
        version: 1,
        provider: "evil",
      };
    } catch {
      mutationRejected = true;
    }

    const card = await adapter.analyze({
      scope,
      asset,
      originalBytes,
      fixture: declaredFixture,
      capability: OFFLINE_STUDIO_CAPABILITY,
      createdAt: "2026-09-10T08:00:00.000Z",
    });

    expect(mutationRejected).toBe(true);
    expect(card.capability).toEqual(OFFLINE_STUDIO_CAPABILITY);
  });

  it("rejects undeclared or non-synthetic fallback input", async () => {
    const asset = await importVideo(library, "one");

    await expect(studio.analyze(scope, asset.id, {
      ...declaredFixture,
      synthetic: false,
    } as unknown as OfflineAnalysisFixture)).rejects.toThrow("offline-fixture-synthetic-declaration-required");
  });

  it("accepts only runtime booleans for the optional hasAudio flag", async () => {
    const asset = await importVideo(library, "runtime-audio-flag");

    for (const invalidHasAudio of ["false", 0, 1]) {
      await expect(studio.analyze(scope, asset.id, {
        ...declaredFixture,
        ffprobe: { ...declaredFixture.ffprobe, hasAudio: invalidHasAudio },
      } as unknown as OfflineAnalysisFixture)).rejects.toThrow("ffprobe-audio-flag-invalid");
    }

    const card = await studio.analyze(scope, asset.id, {
      ...declaredFixture,
      ffprobe: { ...declaredFixture.ffprobe, hasAudio: false },
    });
    expect(card.technicalFlags).toContain("ffprobe:audio=absent");
  });

  it("rejects caller-injected confirmed facts in an offline fixture", async () => {
    const asset = await importVideo(library, "one");

    await expect(studio.analyze(scope, asset.id, {
      ...declaredFixture,
      confirmedFacts: ["材质已确认"],
    } as unknown as OfflineAnalysisFixture)).rejects.toThrow("offline-fixture-field-not-allowed");
  });

  it("checks the library's live use authorization before making an analysis judgment", async () => {
    const guarded = new CustomerMaterialLibrary(root, ({ action }) => action !== "use");
    const guardedStudio = new PrivateContentStudio(
      guarded,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => "2026-09-10T08:00:00.000Z" },
    );
    const asset = await importVideo(guarded, "live-use-denied");

    await expect(guardedStudio.analyze(scope, asset.id, declaredFixture))
      .rejects.toThrow("not-authorized");
  });

  it("keeps reference-only uploads outside the ordinary studio analysis path", async () => {
    const references = new ReferenceSampleService(library, root, {
      now: () => "2026-09-10T08:00:00.000Z",
    });
    const { asset } = await references.importDedicated(scope, {
      requestId: "ordinary-studio-reference-only",
      bytes: Buffer.from("reference-only-upload"),
      fileName: "reference.mov",
      sourceKind: "external-style-only",
      operationKey: "ordinary_studio_reference_only",
    });

    await expect(studio.analyze(scope, asset.id, declaredFixture))
      .rejects.toThrow("asset-use-not-allowed");
  });

  it("returns the immutable original card when identical analysis is retried later", async () => {
    let now = "2026-09-10T08:00:00.000Z";
    const retryStudio = new PrivateContentStudio(
      library,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => now },
    );
    const asset = await importVideo(library, "analysis-retry");

    const first = await retryStudio.analyze(scope, asset.id, declaredFixture);
    now = "2026-09-10T09:00:00.000Z";
    const retry = await retryStudio.analyze(scope, asset.id, declaredFixture);

    expect(retry).toEqual(first);
  });

  it("rejects every adapter card that disagrees with the bound analysis request", async () => {
    const decoy = await importVideo(library, "adapter-decoy");
    const rewrites: Array<readonly [string, (card: MaterialCard) => MaterialCard]> = [
      ["customer", (card) => ({ ...card, customerId: "customer-b" })],
      ["session", (card) => ({ ...card, sessionId: "studio-session-2" })],
      ["source asset", (card) => ({
        ...card,
        sourceAssetId: decoy.id,
        sourceDigest: decoy.digest,
      })],
      ["source digest", (card) => ({ ...card, sourceDigest: "f".repeat(64) })],
      ["capability", (card) => ({
        ...card,
        capability: { mode: "provider", label: "伪造能力", version: 1, provider: "evil" },
      })],
      ["confirmed facts", (card) => ({ ...card, confirmedFacts: ["未经过人工确认"] })],
    ];

    for (const [name, rewrite] of rewrites) {
      const asset = await importVideo(library, `malicious-${name}`);
      const guardedStudio = new PrivateContentStudio(
        library,
        new RewritingUnderstandingAdapter(rewrite),
        { now: () => "2026-09-10T08:00:00.000Z" },
      );

      await expect(guardedStudio.analyze(scope, asset.id, declaredFixture), name)
        .rejects.toThrow("analysis-adapter-contract-mismatch");
    }
  });

  it("uses the pre-call capability snapshot when an adapter mutates during analysis", async () => {
    const asset = await importVideo(library, "mutating-capability");
    const mutatingAdapter = new MutatingCapabilityAdapter();
    const guardedStudio = new PrivateContentStudio(
      library,
      mutatingAdapter,
      { now: () => "2026-09-10T08:00:00.000Z" },
    );

    await expect(guardedStudio.analyze(scope, asset.id, declaredFixture))
      .rejects.toThrow("analysis-adapter-contract-mismatch");
  });

  it("returns an explainable GapList instead of a story when evidence is insufficient", async () => {
    const asset = await importVideo(library, "one");
    await studio.analyze(scope, asset.id, declaredFixture);

    const { assessment } = await createDemandMatch(studio, scope, "讲解戒指", "gap-one");

    expect(assessment.verdict).toBe("partial");
    expect(assessment.unmetNeeds).toContain("还缺 3 段可用素材");
    expect(assessment.recommendedCaptures[0]).toContain("历史素材库");
    expect(assessment.recommendedCaptures.join(" ")).not.toMatch(/(?<!不)补拍|重拍/);
    expect(assessment.safeScope).toContain("不补拍");
    await expect(studio.discoverOpportunities(scope, {
      demandId: assessment.demandId,
      matchAssessmentId: assessment.id,
    })).rejects.toThrow("match-not-ready");
  });

  it("returns an explainable gap when exactly three clips cannot provide different source selections", async () => {
    const assets = await Promise.all(
      ["exactly-three-one", "exactly-three-two", "exactly-three-three"]
        .map((name) => importVideo(library, name)),
    );
    for (const asset of assets) await studio.analyze(scope, asset.id, declaredFixture);

    const { assessment } = await createDemandMatch(
      studio,
      scope,
      "形成两套真正不同选片的戒指短片",
      "gap-three",
    );

    expect(assessment.verdict).toBe("partial");
    expect(assessment.unmetNeeds).toContain("还缺 1 段可用素材");
    expect(assessment.recommendedCaptures[0]).toContain("历史素材库再找 1 段");
    expect(assessment.recommendedCaptures.join(" ")).not.toMatch(/(?<!不)补拍|重拍/);
  });

  it("returns an explainable gap when four eight-second clips cannot reach 30 seconds", async () => {
    const eightSecondFixture: OfflineAnalysisFixture = {
      ...declaredFixture,
      segment: { startMs: 0, endMs: 8_000 },
      ffprobe: { ...declaredFixture.ffprobe, durationSeconds: 8 },
    };
    const assets = await Promise.all(
      ["short-total-one", "short-total-two", "short-total-three", "short-total-four"]
        .map((name) => importVideo(library, name)),
    );
    for (const asset of assets) await studio.analyze(scope, asset.id, eightSecondFixture);

    const { assessment } = await createDemandMatch(
      studio,
      scope,
      "形成两套 30—60 秒戒指短片",
      "gap-short-total",
    );

    expect(assessment.verdict).toBe("partial");
    expect(assessment.unmetNeeds).toContain("现有素材无法组成两套 30—60 秒且选片不同的草稿");
    expect(assessment.recommendedCaptures).toContain("重新组合历史素材；若仍不够，就缩短或收窄内容范围，不补拍");
  });

  it("searches past four eligible clips when a fifth clip makes both directions renderable", async () => {
    let fixtureTime = "2026-09-10T08:00:00.000Z";
    const searchingStudio = new PrivateContentStudio(
      library,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => fixtureTime },
    );
    const durationsMs = [8_000, 8_000, 14_000, 8_000, 14_000] as const;
    const assets = await Promise.all(
      durationsMs.map((_, index) => importVideo(library, `search-all-${index + 1}`)),
    );
    const cards: MaterialCard[] = [];
    for (const [index, asset] of assets.entries()) {
      fixtureTime = `2026-09-10T08:00:0${index}.000Z`;
      cards.push(await searchingStudio.analyze(scope, asset.id, {
        ...declaredFixture,
        segment: { startMs: 0, endMs: durationsMs[index]! },
        ffprobe: {
          ...declaredFixture.ffprobe,
          durationSeconds: durationsMs[index]! / 1_000,
        },
      }));
    }
    fixtureTime = "2026-09-10T08:01:00.000Z";

    const result = await discoverWithDemand(
      searchingStudio,
      scope,
      "第五段补足第二套草稿",
      "search-all",
    );

    expect(result.gaps).toEqual([]);
    expect(result.opportunities).toHaveLength(1);
    expect(result.opportunities[0]!.materialCardIds).toEqual([
      cards[0]!.id,
      cards[1]!.id,
      cards[2]!.id,
      cards[4]!.id,
    ]);
  });

  it("assigns later clips as shared roles when that is the only renderable combination", async () => {
    let fixtureTime = "2026-09-10T08:00:00.000Z";
    const searchingStudio = new PrivateContentStudio(
      library,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => fixtureTime },
    );
    const durationsMs = [8_000, 8_000, 12_000, 12_000] as const;
    const assets = await Promise.all(
      durationsMs.map((_, index) => importVideo(library, `search-roles-${index + 1}`)),
    );
    const cards: MaterialCard[] = [];
    for (const [index, asset] of assets.entries()) {
      fixtureTime = `2026-09-10T08:00:0${index}.000Z`;
      cards.push(await searchingStudio.analyze(scope, asset.id, {
        ...declaredFixture,
        segment: { startMs: 0, endMs: durationsMs[index]! },
        ffprobe: {
          ...declaredFixture.ffprobe,
          durationSeconds: durationsMs[index]! / 1_000,
        },
      }));
    }
    fixtureTime = "2026-09-10T08:01:00.000Z";

    const result = await discoverWithDemand(
      searchingStudio,
      scope,
      "完整搜索共享与补充镜头角色",
      "search-roles",
    );

    expect(result.gaps).toEqual([]);
    expect(result.opportunities).toHaveLength(1);
    expect(result.opportunities[0]!.materialCardIds).toEqual([
      cards[2]!.id,
      cards[3]!.id,
      cards[0]!.id,
      cards[1]!.id,
    ]);
  });

  it("rejects a brief that drops the fourth clip needed for different source selections", async () => {
    const { request } = await prepareBriefRequest(studio, library, "brief-drops-fourth");

    await expect(studio.confirmBrief(scope, {
      ...request,
      selectedAssetIds: request.selectedAssetIds.slice(0, 3),
    })).rejects.toThrow("four-selected-assets-required");
  });

  it("never inventories or selects material cards created for another session", async () => {
    const assets = await Promise.all(
      ["session-a-one", "session-a-two", "session-a-three"].map((name) => importVideo(library, name)),
    );
    for (const asset of assets) await studio.analyze(scope, asset.id, declaredFixture);
    const otherSession = { ...scope, sessionId: "studio-session-2" };

    const inventory = await studio.inventory(otherSession, "2026-09-10T08:00:00.000Z");
    const { assessment } = await createDemandMatch(studio, otherSession, "B 会话讲解戒指", "session-b");

    expect(inventory.items).toEqual([]);
    expect(assessment.verdict).toBe("blocked");
    expect(assessment.unmetNeeds).toContain("还缺 4 段可用素材");
  });

  it("uses only four allowed, technically usable 8—20 second video cards", async () => {
    const first = await importVideo(library, "one");
    const second = await importVideo(library, "two");
    const third = await importVideo(library, "three");
    const fourth = await importVideo(library, "four");
    const tooShort = await importVideo(library, "short");
    const blocked = await importVideo(library, "blocked", "blocked");

    await studio.analyze(scope, first.id, declaredFixture);
    await studio.analyze(scope, second.id, {
      ...declaredFixture,
      segment: { startMs: 1_000, endMs: 13_000 },
      ffprobe: { ...declaredFixture.ffprobe, durationSeconds: 13 },
    });
    await studio.analyze(scope, third.id, {
      ...declaredFixture,
      segment: { startMs: 2_000, endMs: 17_000 },
      ffprobe: { ...declaredFixture.ffprobe, durationSeconds: 17 },
    });
    await studio.analyze(scope, fourth.id, {
      ...declaredFixture,
      segment: { startMs: 3_000, endMs: 18_000 },
      ffprobe: { ...declaredFixture.ffprobe, durationSeconds: 18 },
    });
    await studio.analyze(scope, tooShort.id, {
      ...declaredFixture,
      segment: { startMs: 0, endMs: 7_999 },
      ffprobe: { ...declaredFixture.ffprobe, durationSeconds: 7.999 },
    });
    await expect(studio.analyze(scope, blocked.id, declaredFixture))
      .rejects.toThrow("asset-use-not-allowed");

    const inventory = await studio.inventory(scope, "2026-09-10T08:00:00.000Z");
    const result = await discoverWithDemand(studio, scope, "讲解戒指", "eligible-four");

    expect(inventory.items).toHaveLength(5);
    expect(inventory.items.every((item) => item.selectionReason.length > 0)).toBe(true);
    expect(result.gaps).toEqual([]);
    expect(result.opportunities).toHaveLength(1);
    expect(result.opportunities[0]?.materialCardIds).toHaveLength(4);
  });

  it("fails closed when a selected source is withdrawn before brief confirmation", async () => {
    const assets = await Promise.all(["one", "two", "three", "four"].map((name) => importVideo(library, name)));
    for (const asset of assets) await studio.analyze(scope, asset.id, declaredFixture);
    const discovery = await discoverWithDemand(studio, scope, "讲解戒指", "withdraw-before-brief");
    await library.withdraw(scope.actorId, scope.customerId, assets[0]!.id, "withdraw-before-brief");

    await expect(studio.confirmBrief(scope, {
      opportunityId: discovery.opportunities[0]!.id,
      matchAssessmentId: discovery.opportunities[0]!.matchAssessmentId,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "brief-op-withdrawn",
    })).rejects.toThrow("match-inventory-not-current");
  });

  it("rejects committed brief replay and every new operation after withdrawal", async () => {
    const { assets, request } = await prepareBriefRequest(studio, library, "replay-after-withdrawal");
    await studio.confirmBrief(scope, request);
    await library.withdraw(scope.actorId, scope.customerId, assets[0]!.id, "withdraw-after-brief");

    await expect(studio.confirmBrief(scope, request))
      .rejects.toThrow("match-inventory-not-current");
    await expect(studio.confirmBrief(scope, {
      ...request,
      operationKey: "new-operation-after-withdrawal",
    })).rejects.toThrow("match-inventory-not-current");
  });

  it("never replays an actor-bound historical operation to a different unauthorized actor", async () => {
    const guardedLibrary = new CustomerMaterialLibrary(
      root,
      ({ actorId }) => actorId === scope.actorId,
    );
    const guardedStudio = new PrivateContentStudio(
      guardedLibrary,
      new OfflineFixtureUnderstandingAdapter(),
      { now: () => "2026-09-10T08:00:00.000Z" },
    );
    const { assets, request } = await prepareBriefRequest(
      guardedStudio,
      guardedLibrary,
      "actor-bound-replay",
    );
    await guardedStudio.confirmBrief(scope, request);
    await guardedLibrary.withdraw(
      scope.actorId,
      scope.customerId,
      assets[0]!.id,
      "actor-bound-withdrawal",
    );

    await expect(guardedStudio.confirmBrief(
      { ...scope, actorId: "actor-b" },
      request,
    )).rejects.toThrow("brief-operation-actor-mismatch");
  });

  it("keeps confirmed brief retries immutable and rejects changed operation bodies", async () => {
    const assets = await Promise.all(["one", "two", "three", "four"].map((name) => importVideo(library, name)));
    for (const asset of assets) await studio.analyze(scope, asset.id, declaredFixture);
    const discovery = await discoverWithDemand(studio, scope, "讲解戒指", "brief-immutable");
    const request = {
      opportunityId: discovery.opportunities[0]!.id,
      matchAssessmentId: discovery.opportunities[0]!.matchAssessmentId,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
      reviewRequirements: ["采用前人工复核"],
      operationKey: "brief-op-1",
    };

    const first = await studio.confirmBrief(scope, request);
    const retry = await studio.confirmBrief(scope, request);

    expect(retry).toEqual(first);
    expect(first).toMatchObject({
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      version: 1,
      status: "confirmed",
    });
    await expect(studio.confirmBrief(scope, {
      ...request,
      reviewRequirements: ["采用前人工复核", "材质事实复核"],
    })).rejects.toThrow("operation-key-conflict");
  });

  it("serializes concurrent identical brief confirmations into one immutable result", async () => {
    const { request } = await prepareBriefRequest(studio, library, "concurrent-same");

    const [first, second] = await Promise.all([
      studio.confirmBrief(scope, request),
      studio.confirmBrief(scope, request),
    ]);

    expect(second).toEqual(first);
    expect(first.version).toBe(1);
  });

  it("allows only one concurrent body to claim an operation key", async () => {
    const { request } = await prepareBriefRequest(studio, library, "concurrent-conflict");

    const results = await Promise.allSettled([
      studio.confirmBrief(scope, request),
      studio.confirmBrief(scope, {
        ...request,
        reviewRequirements: ["采用前人工复核", "并发异体"],
      }),
    ]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    const rejected = results.find((result) => result.status === "rejected");
    expect(rejected).toMatchObject({
      status: "rejected",
      reason: expect.objectContaining({ message: expect.stringContaining("operation-key-conflict") }),
    });
  });
});

async function prepareBriefRequest(
  targetStudio: PrivateContentStudio,
  targetLibrary: CustomerMaterialLibrary,
  operationKey: string,
) {
  const assets = await Promise.all(
    ["one", "two", "three", "four"].map((name) => importVideo(targetLibrary, `${operationKey}-${name}`)),
  );
  for (const asset of assets) await targetStudio.analyze(scope, asset.id, declaredFixture);
  const discovery = await discoverWithDemand(targetStudio, scope, "讲解戒指", `${operationKey}-gate`);
  return {
    assets,
    request: {
      opportunityId: discovery.opportunities[0]!.id,
      matchAssessmentId: discovery.opportunities[0]!.matchAssessmentId,
      selectedAssetIds: assets.map((asset) => asset.id),
      confirmedFactIds: ["fact-material-18k: 材质为 18K 金"],
      reviewRequirements: ["采用前人工复核"],
      operationKey,
    },
  };
}

function captureAdapter(
  overrides: Partial<CaptureEnvelopeV1Input> = {},
): CaptureEnvelopeAdapter {
  return {
    capability: OFFLINE_STUDIO_CAPABILITY,
    async capture({ scope: captureScope, requestId }) {
      return {
        id: `capture-${requestId}`,
        customerId: captureScope.customerId,
        sessionId: captureScope.sessionId,
        version: 1,
        requestId,
        mediaKind: "video",
        fileName: `${requestId}.mp4`,
        bytes: new Uint8Array([1, 2, 3, 4]),
        binding: {
          customerId: captureScope.customerId,
          sessionId: captureScope.sessionId,
          verified: true,
        },
        adapter: {
          name: "synthetic-phone-hardware-bridge",
          version: "1.0.0",
        },
        capturedAt: "2026-09-10T07:59:00.000Z",
        ...overrides,
      };
    },
  };
}

async function createDemandMatch(
  targetStudio: PrivateContentStudio,
  targetScope: StudioScope,
  goal: string,
  operationKey: string,
) {
  const demand = await targetStudio.createDemand(targetScope, {
    contentType: "single-product",
    goal,
    targetAudience: "本地合成测试观众",
    desiredEffect: "理解示例流程",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    operationKey: `${operationKey}-demand`,
  });
  const assessment = await targetStudio.assessDemand(
    targetScope,
    demand.id,
    `${operationKey}-match`,
  );
  return { demand, assessment };
}

async function discoverWithDemand(
  targetStudio: PrivateContentStudio,
  targetScope: StudioScope,
  goal: string,
  operationKey: string,
) {
  const { demand, assessment } = await createDemandMatch(targetStudio, targetScope, goal, operationKey);
  return targetStudio.discoverOpportunities(targetScope, {
    demandId: demand.id,
    matchAssessmentId: assessment.id,
  });
}

class RewritingUnderstandingAdapter implements MediaUnderstandingAdapter {
  readonly capability = OFFLINE_STUDIO_CAPABILITY;
  private readonly base = new OfflineFixtureUnderstandingAdapter();

  constructor(private readonly rewrite: (card: MaterialCard) => MaterialCard) {}

  async analyze(request: MediaUnderstandingRequest): Promise<MaterialCard> {
    return this.rewrite(await this.base.analyze(request));
  }
}

class MutatingCapabilityAdapter implements MediaUnderstandingAdapter {
  capability: StudioCapability = { ...OFFLINE_STUDIO_CAPABILITY };
  private readonly base = new OfflineFixtureUnderstandingAdapter();

  async analyze(request: MediaUnderstandingRequest): Promise<MaterialCard> {
    const card = await this.base.analyze(request);
    this.capability = {
      mode: "provider",
      label: "调用中伪造能力",
      version: 1,
      provider: "evil",
    };
    return { ...card, capability: this.capability };
  }
}

async function importVideo(
  library: CustomerMaterialLibrary,
  name: string,
  usePermission: "allowed" | "blocked" = "allowed",
) {
  return library.importOriginal(scope.actorId, scope.customerId, {
    requestId: `import-${name}`,
    bytes: Buffer.from(`synthetic-video-${name}`),
    mediaKind: "video",
    fileName: `${name}.mp4`,
    capturedAt: "2026-09-09T08:00:00.000Z",
    viewPermission: "allowed",
    usePermission,
  });
}
