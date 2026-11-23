import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ContentSegment, CreativeIntent, CreativeSession, LibraryAsset } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  confirmMaterialSelection,
  decideMaterialViewing,
  JsonContentStore,
  startCreativeSession,
  suggestHistoricalMaterials,
  suggestPostShootHistoricalMaterials,
  type MaterialMatcher,
} from "../src";
import { resolveHistoricalMaterialCatalog } from "../src/historical-source";

describe("permission-first historical material advisor", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-material-advisor-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("ranks only technically clean allowed historical segments and hides their identities before viewing consent", async () => {
    const session = await readyForMaterialConsent("first");
    await seedHistoricalSegments([
      makeSegment("allowed_confirmed", { products: [{ id: "product_01", status: "confirmed" }] }),
      makeSegment("allowed_untagged"),
      makeSegment("review_required", { permissionState: "review-required" }),
      makeSegment("blocked", { permissionState: "blocked" }),
      makeSegment("withdrawn", { permissionState: "withdrawn" }),
      makeSegment("damaged", { technicalFlags: ["blurred"] }),
      makeSegment("product_unconfirmed", { products: [{ id: "product_02", status: "suggested" }] }),
      makeSegment("unrelated"),
    ]);
    const matcher = capturingMatcher(["allowed_confirmed", "allowed_untagged"]);

    const result = await suggestHistoricalMaterials({
      sessionId: session.id,
      actorId: "designer_01",
      now: "2026-09-04T08:01:00.000Z",
    }, matcher, library);

    expect(matcher.receivedIds).toEqual(["allowed_confirmed", "allowed_untagged", "unrelated"]);
    expect(result).toMatchObject({ count: 2, hasSameProductMatches: true, suggestions: [] });
    expect((result as unknown as { suggestions?: unknown[] }).suggestions).toEqual([]);
    expect((await library.listMaterialSuggestionSets())[0]?.suggestions.map((item) => item.segmentId))
      .toEqual(["allowed_confirmed", "allowed_untagged"]);
  });

  it("records viewing acceptance before any material is authorized for a script", async () => {
    const session = await readyForMaterialConsent("accept");
    await seedHistoricalSegments([makeSegment("allowed")]);
    const suggestionSet = await suggestHistoricalMaterials(suggestionInput(session.id), capturingMatcher(["allowed"]), library);
    const stored = (await library.listMaterialSuggestionSets())[0]!;

    const decision = await decideMaterialViewing({
      sessionId: session.id,
      suggestionSetId: stored.id,
      decision: "accept",
      actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);

    expect(suggestionSet.suggestions).toEqual([]);
    expect(decision).toMatchObject({
      action: "accept-view",
      shownSegmentIds: ["allowed"],
      selectedSegmentIds: [],
    });
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "reviewing_materials",
      materialViewDecisionIds: [decision.id],
      materialUseDecisionIds: [],
    });
    expect(await library.listAuditEvents(session.id)).toContainEqual(expect.objectContaining({
      actorId: "customer_01",
      reason: expect.any(String),
      before: { stage: "awaiting_material_consent" },
      after: { stage: "reviewing_materials" },
    }));
  });

  it("rejects selection outside the accepted current set and rechecks live eligibility", async () => {
    const session = await readyForMaterialConsent("selection");
    await seedHistoricalSegments([makeSegment("allowed")]);
    await suggestHistoricalMaterials(suggestionInput(session.id), capturingMatcher(["allowed"]), library);
    const set = (await library.listMaterialSuggestionSets())[0]!;
    await decideMaterialViewing({
      sessionId: session.id, suggestionSetId: set.id, decision: "accept", actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);

    await expect(confirmMaterialSelection({
      sessionId: session.id, suggestionSetId: set.id, selectedSegmentIds: ["not_shown"],
      actorId: "customer_01", now: "2026-09-04T08:03:00.000Z",
    }, library)).rejects.toThrow("material-selection-segment-not-shown");

    await library.putSegments([makeSegment("allowed", { permissionState: "withdrawn" })]);

    await expect(confirmMaterialSelection({
      sessionId: session.id, suggestionSetId: set.id, selectedSegmentIds: ["allowed"],
      actorId: "customer_01", now: "2026-09-04T08:04:00.000Z",
    }, library)).rejects.toThrow("material-selection-segment-no-longer-eligible");
  });

  it("records a decline with no selections and moves only this session to materials declined", async () => {
    const session = await readyForMaterialConsent("decline");
    await seedHistoricalSegments([makeSegment("allowed")]);
    await suggestHistoricalMaterials(suggestionInput(session.id), capturingMatcher(["allowed"]), library);
    const set = (await library.listMaterialSuggestionSets())[0]!;

    const decision = await decideMaterialViewing({
      sessionId: session.id, suggestionSetId: set.id, decision: "decline", actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);

    expect(decision).toMatchObject({
      action: "decline-view",
      shownSegmentIds: ["allowed"],
      selectedSegmentIds: [],
      rejectedSegmentIds: ["allowed"],
    });
    expect((await library.listCreativeSessions())[0]).toMatchObject({
      stage: "materials_declined",
      materialViewDecisionIds: [decision.id],
      materialUseDecisionIds: [],
    });
  });

  it("does not let a decision or suggestion set from another session authorize this one", async () => {
    const first = await readyForMaterialConsent("first");
    const second = await readyForMaterialConsent("second");
    await seedHistoricalSegments([makeSegment("allowed")]);
    await suggestHistoricalMaterials(suggestionInput(first.id), capturingMatcher(["allowed"]), library);
    const firstSet = (await library.listMaterialSuggestionSets())[0]!;
    await decideMaterialViewing({
      sessionId: first.id, suggestionSetId: firstSet.id, decision: "accept", actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);

    await expect(decideMaterialViewing({
      sessionId: second.id, suggestionSetId: firstSet.id, decision: "accept", actorId: "customer_01",
      now: "2026-09-04T08:03:00.000Z",
    }, library)).rejects.toThrow("material-suggestion-set-session-mismatch");
    await expect(confirmMaterialSelection({
      sessionId: second.id, suggestionSetId: firstSet.id, selectedSegmentIds: ["allowed"],
      actorId: "customer_01", now: "2026-09-04T08:03:00.000Z",
    }, library)).rejects.toThrow("material-suggestion-set-session-mismatch");
  });

  it("never persists a withdrawn selected segment when withdrawal and selection race from two stores", async () => {
    const session = await readyForMaterialConsent("race");
    await seedHistoricalSegments([makeSegment("allowed")]);
    await suggestHistoricalMaterials(suggestionInput(session.id), capturingMatcher(["allowed"]), library);
    const set = (await library.listMaterialSuggestionSets())[0]!;
    await decideMaterialViewing({
      sessionId: session.id, suggestionSetId: set.id, decision: "accept", actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);
    const path = join(testDirectory, "library.json");
    const selectionStore = await JsonContentStore.open(path);
    const withdrawalStore = await JsonContentStore.open(path);

    await Promise.allSettled([
      confirmMaterialSelection({
        sessionId: session.id, suggestionSetId: set.id, selectedSegmentIds: ["allowed"],
        actorId: "customer_01", now: "2026-09-04T08:03:00.000Z",
      }, selectionStore),
      withdrawalStore.putSegments([makeSegment("allowed", { permissionState: "withdrawn" })]),
    ]);

    const reopened = await JsonContentStore.open(path);
    const selected = (await reopened.listMaterialUseDecisions()).some(
      (decision) => decision.action === "confirm-selection" && decision.selectedSegmentIds.includes("allowed"),
    );
    const withdrawn = (await reopened.listSegments()).find((segment) => segment.id === "allowed")
      ?.permissionState === "withdrawn";
    expect(selected && withdrawn).toBe(false);
  });

  it("leaves no decision when withdrawal wins before a stale store selects", async () => {
    const session = await readyForMaterialConsent("withdrawal-first");
    await seedHistoricalSegments([makeSegment("allowed")]);
    await suggestHistoricalMaterials(suggestionInput(session.id), capturingMatcher(["allowed"]), library);
    const set = (await library.listMaterialSuggestionSets())[0]!;
    await decideMaterialViewing({
      sessionId: session.id, suggestionSetId: set.id, decision: "accept", actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, library);
    const path = join(testDirectory, "library.json");
    const selectionStore = await JsonContentStore.open(path);
    const withdrawalStore = await JsonContentStore.open(path);

    await withdrawalStore.putSegments([makeSegment("allowed", { permissionState: "withdrawn" })]);

    await expect(confirmMaterialSelection({
      sessionId: session.id, suggestionSetId: set.id, selectedSegmentIds: ["allowed"],
      actorId: "customer_01", now: "2026-09-04T08:03:00.000Z",
    }, selectionStore)).rejects.toThrow(/stale-content-store|material-no-longer-eligible/);
    const reopened = await JsonContentStore.open(path);
    expect((await reopened.listMaterialUseDecisions()).filter(
      (decision) => decision.action === "confirm-selection",
    )).toEqual([]);
  });

  it.each(["pre-shoot", "post-shoot"] as const)(
    "atomically rejects a %s suggestion when its parent is rebound during matching",
    async (purpose) => {
      const path = join(testDirectory, "library.json");
      const segment = makeSegment(`race_${purpose}`);
      const session = purpose === "pre-shoot"
        ? await readyForMaterialConsent(`race_${purpose}`)
        : await readyForPostShootSuggestion(`race_${purpose}`);
      await seedHistoricalSegments([segment]);
      let matcherEntered!: () => void;
      let releaseMatcher!: () => void;
      const entered = new Promise<void>((resolve) => { matcherEntered = resolve; });
      const released = new Promise<void>((resolve) => { releaseMatcher = resolve; });
      const matcher: MaterialMatcher = { rank: async () => {
        matcherEntered();
        await released;
        return [{ segmentId: segment.id, reason: "race candidate", score: 1 }];
      } };

      const pending = purpose === "pre-shoot"
        ? suggestHistoricalMaterials(suggestionInput(session.id), matcher, library)
        : suggestPostShootHistoricalMaterials(suggestionInput(session.id), matcher, library);
      await entered;
      const writer = await JsonContentStore.open(path);
      const rebound = { ...segment, assetId: `${segment.assetId}_blocked` };
      await writer.putAsset({
        ...makeHistoricalAsset(rebound), permissionState: "blocked",
      });
      await writer.putSegments([rebound]);
      const afterRebind = await readFile(path);
      releaseMatcher();

      await expect(pending).rejects.toThrow("historical-material-catalog-invalid");
      expect(await readFile(path)).toEqual(afterRebind);
      const reopened = await JsonContentStore.open(path);
      expect(await reopened.listMaterialSuggestionSets()).toEqual([]);
      expect((await reopened.listCreativeSessions()).find((item) => item.id === session.id)?.stage)
        .toBe(purpose === "pre-shoot" ? "awaiting_material_consent" : "editorial_review");
    },
  );

  it.each([
    { purpose: "pre-shoot", mutation: "segment-metadata" },
    { purpose: "pre-shoot", mutation: "asset-metadata" },
    { purpose: "post-shoot", mutation: "segment-metadata" },
    { purpose: "post-shoot", mutation: "asset-metadata" },
  ] as const)(
    "atomically rejects a $purpose suggestion when $mutation changes during matching",
    async ({ purpose, mutation }) => {
      const path = join(testDirectory, "library.json");
      const segment = makeSegment(`content_race_${purpose}_${mutation}`);
      const session = purpose === "pre-shoot"
        ? await readyForMaterialConsent(`content_race_${purpose}_${mutation}`)
        : await readyForPostShootSuggestion(`content_race_${purpose}_${mutation}`);
      await seedHistoricalSegments([segment]);
      let matcherEntered!: () => void;
      let releaseMatcher!: () => void;
      const entered = new Promise<void>((resolve) => { matcherEntered = resolve; });
      const released = new Promise<void>((resolve) => { releaseMatcher = resolve; });
      const matcher: MaterialMatcher = { rank: async () => {
        matcherEntered();
        await released;
        return [];
      } };

      const pending = purpose === "pre-shoot"
        ? suggestHistoricalMaterials(suggestionInput(session.id), matcher, library)
        : suggestPostShootHistoricalMaterials(suggestionInput(session.id), matcher, library);
      await entered;
      const writer = await JsonContentStore.open(path);
      if (mutation === "segment-metadata") {
        await writer.putSegments([{
          ...segment,
          tags: {
            ...segment.tags,
            topics: ["late catalog topic"],
            transcript: "Catalog text changed after the matcher read it.",
          },
        }]);
      } else {
        const parent = (await writer.listAssets()).find((asset) => asset.id === segment.assetId)!;
        await writer.putAsset({
          ...parent,
          technicalFlags: [...parent.technicalFlags, "late-parent-quality-flag"],
        });
      }
      const afterMutation = await readFile(path);
      releaseMatcher();

      await expect(pending).rejects.toThrow("material-no-longer-eligible");
      expect(await readFile(path)).toEqual(afterMutation);
      const reopened = await JsonContentStore.open(path);
      expect(await reopened.listMaterialSuggestionSets()).toEqual([]);
      expect((await reopened.listCreativeSessions()).find((item) => item.id === session.id)?.stage)
        .toBe(purpose === "pre-shoot" ? "awaiting_material_consent" : "editorial_review");
    },
  );

  it("derives a stable full-record source digest from schema-parsed content", async () => {
    const segment = makeSegment("stable-digest", {
      topics: ["craft"], transcript: "A stable catalog record.",
    });
    const asset = makeHistoricalAsset(segment);
    const reorderedSegment = {
      ...segment,
      tags: Object.fromEntries(Object.entries(segment.tags).reverse()) as ContentSegment["tags"],
    };
    const reorderedAsset = Object.fromEntries(Object.entries({
      ...asset, derivedFromAssetId: undefined,
    }).reverse()) as LibraryAsset;
    const session = await readyForMaterialConsent("stable-digest");

    const first = resolveHistoricalMaterialCatalog(session, [segment], [asset], [session]).sources;
    const sameParsedContent = resolveHistoricalMaterialCatalog(
      session, [reorderedSegment], [reorderedAsset], [session],
    ).sources;
    const changedDate = resolveHistoricalMaterialCatalog(
      session, [segment], [{ ...asset, importedAt: "2026-09-03T08:00:01.000Z" }], [session],
    ).sources;

    expect(sameParsedContent).toEqual(first);
    expect(changedDate[0]?.contentDigest).not.toBe(first[0]?.contentDigest);
  });

  it("rejects a forged suggestion through the public store writer without changing bytes", async () => {
    const session = await readyForMaterialConsent("forged-public-write");
    const segment = makeSegment("forged-source");
    await seedHistoricalSegments([segment]);
    const path = join(testDirectory, "library.json");
    const before = await readFile(path);
    const forged = {
      id: "forged_set", sessionId: session.id, version: 1,
      suggestions: [{
        segmentId: segment.id, thumbnailPath: "/forged/not-the-asset.jpg",
        capturedAt: "2026-09-04T08:00:00.000Z", productId: null, processStage: null,
        reason: "forged", source: "historical", permissionState: "allowed" as const,
        lastUsedAt: null, technicallyAvailable: true,
      }],
      createdAt: "2026-09-04T08:01:00.000Z",
    };

    await expect((library.putMaterialSuggestionSet as (
      value: typeof forged,
      precondition?: unknown,
    ) => Promise<void>)(forged)).rejects.toThrow("material-suggestion-precondition-required");
    const legacyIdOnlyPrecondition = {
      suggestionSetId: forged.id,
      purpose: "pre-shoot-script",
      sources: [{ segmentId: segment.id, assetId: segment.assetId, batchId: segment.batchId }],
    } as unknown as Parameters<typeof library.putMaterialSuggestionSet>[1];
    await expect(library.putMaterialSuggestionSet(forged, legacyIdOnlyPrecondition))
      .rejects.toThrow("material-suggestion-precondition-invalid");
    await expect(library.commitCreativeBundle({
      session,
      expectedSessionSnapshot: session,
      materialSuggestionSets: [forged],
      materialSuggestionPreconditions: [legacyIdOnlyPrecondition],
      auditEvents: [],
    })).rejects.toThrow("material-suggestion-precondition-invalid");
    await expect(library.putMaterialSuggestionSet(forged, {
      suggestionSetId: forged.id,
      purpose: "pre-shoot-script",
      sources: resolveHistoricalMaterialCatalog(
        session, [segment], [makeHistoricalAsset(segment)], [session],
      ).sources,
    })).rejects.toThrow("material-suggestion-source-invalid");
    expect(await readFile(path)).toEqual(before);
    expect(await library.listMaterialSuggestionSets()).toEqual([]);
    expect((await library.listCreativeSessions()).find((item) => item.id === session.id)?.stage)
      .toBe("awaiting_material_consent");
  });

  async function readyForMaterialConsent(suffix: string) {
    return (await startCreativeSession({
      customerId: `customer_${suffix}`,
      title: `Pendant story ${suffix}`,
      text: "Tell the pendant's history.",
      actorId: "designer_01",
      now: "2026-09-04T08:00:00.000Z",
    }, { interpret: async () => completeDraft() }, library)).session;
  }

  async function readyForPostShootSuggestion(suffix: string) {
    const session: CreativeSession = {
      id: `creative_post_${suffix}`, customerId: `customer_${suffix}`,
      title: `Pendant story ${suffix}`,
      stage: "editorial_review" as const,
      currentIntentVersion: 1, currentBriefVersion: 1, currentScriptVersion: 1,
      currentShotPlanVersion: 1, materialViewDecisionIds: [], materialUseDecisionIds: [],
      deviceBindings: [],
      captureBatchIds: ["batch_current_shoot"],
      editorialBatchId: "batch_current_shoot",
      privacyMode: "creator", prohibitedAreas: ["Do not show customer faces"], status: "active",
      createdAt: "2026-09-04T08:00:00.000Z",
      updatedAt: "2026-09-04T08:00:30.000Z",
    };
    const draft = completeDraft();
    const intent: CreativeIntent = {
      id: `intent_post_${suffix}`, sessionId: session.id, version: 1,
      platforms: draft.platforms,
      audience: draft.audience, theme: draft.theme, products: draft.products,
      shootConditions: draft.shootConditions, prohibitedAreas: draft.prohibitedAreas,
      tone: draft.tone, durationSeconds: draft.durationSeconds, createdAt: session.createdAt,
    };
    await library.putCreativeSession(session);
    await library.putCreativeIntent(intent);
    return session;
  }

  async function seedHistoricalSegments(segments: ContentSegment[]): Promise<void> {
    for (const segment of segments) await library.putAsset(makeHistoricalAsset(segment));
    await library.putSegments(segments);
  }
});

function suggestionInput(sessionId: string) {
  return { sessionId, actorId: "designer_01", now: "2026-09-04T08:01:00.000Z" };
}

function capturingMatcher(segmentIds: string[]): MaterialMatcher & { receivedIds: string[] } {
  const matcher = {
    receivedIds: [] as string[],
    rank: async ({ segments }: { segments: ContentSegment[] }) => {
      matcher.receivedIds = segments.map((segment) => segment.id);
      return segmentIds.map((segmentId, index) => ({
        segmentId,
        reason: `match-${index + 1}`,
        score: 100 - index,
      }));
    },
  };
  return matcher;
}

function completeDraft() {
  return {
    theme: "A pendant returns to the bench",
    platforms: ["Xiaohongshu"],
    audience: "Jewelry collectors",
    shootConditions: ["The maker and workbench are available"],
    prohibitedAreas: ["Do not show customer faces"],
    tone: "Quiet and crafted",
    durationSeconds: 30,
    products: [{ id: "product_01", status: "suggested" as const }],
  };
}

function makeHistoricalAsset(segment: ContentSegment): LibraryAsset {
  return {
    id: segment.assetId,
    batchId: segment.batchId,
    sessionId: "historical_session",
    digest: `digest_${segment.id}`,
    localPath: `/library/${segment.id}.mp4`,
    mediaKind: "video",
    importedAt: "2026-09-03T08:00:00.000Z",
    permissionState: segment.permissionState,
    origin: "historical",
    technicalFlags: [],
  };
}

function makeSegment(
  id: string,
  patch: Partial<ContentSegment> & Partial<ContentSegment["tags"]> = {},
): ContentSegment {
  const { permissionState, ...tagPatch } = patch;
  return {
    id,
    assetId: `asset_${id}`,
    batchId: "batch_history",
    startMs: 0,
    endMs: 10_000,
    permissionState: permissionState ?? "allowed",
    tags: {
      baseTypes: ["scene"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
      ...tagPatch,
    },
  };
}
