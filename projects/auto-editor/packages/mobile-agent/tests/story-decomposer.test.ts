import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ContentSegment, CreativeSession } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JsonContentStore } from "../src/json-content-store";
import { resolveHistoricalMaterialCatalog } from "../src/historical-source";
import {
  decomposeStories,
  inferStoryEvidenceForSegments,
  prepareStoryCandidates,
  validateStoryEvidence,
  type StoryAnalyzer,
  type StoryProposal,
} from "../src/story-decomposer";

describe("story decomposer", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "story-decomposer-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it.each([decomposeStories, prepareStoryCandidates])("treats omitted authorization as zero historical permission at the exported boundary", async (decompose) => {
    // @ts-expect-error Legacy omission must be rejected at compile time and fail closed at runtime.
    const stories = await decompose({
      batchId: "batch_new", newSegments: richNewBatch(),
      historicalSegments: [makeSegment("unconsented_history", "batch_old", {
        products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
      })],
    }, analyzerReturning([{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }]), library);
    expect(stories.flatMap((story) => story.historicalSegmentIds)).toEqual([]);
  });

  it.each([decomposeStories, prepareStoryCandidates])("rejects consent superseded while the analyzer is running", async (decompose) => {
    await seedConsent(library, ["history_process"]);
    const stories = decompose({ batchId: "batch_new", authorization: authorization(["history_process"]),
      newSegments: richNewBatch(), historicalSegments: [makeSegment("history_process", "batch_old", {
        products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
      })],
    }, { propose: async () => {
      const current = (await library.listMaterialSuggestionSets())[0]!;
      const next = { ...current, id: "new_set_mid_analysis", version: 2 };
      await library.putMaterialSuggestionSet(next, suggestionPrecondition(next, [historySegment()]));
      return [{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }];
    } }, library);
    await expect(stories).rejects.toThrow("historical-authorization-invalid");
    expect(await library.listStories()).toEqual([]);
  });

  it("atomically rejects consent changed after analysis validation but before candidate persistence", async () => {
    await seedConsent(library, ["history_process"]);
    const commit = library.commitStoryCandidates.bind(library);
    vi.spyOn(library, "commitStoryCandidates").mockImplementationOnce(async (input) => {
      const current = (await library.listMaterialSuggestionSets())[0]!;
      const next = { ...current, id: "new_set_before_story_write", version: 2 };
      await library.putMaterialSuggestionSet(next, suggestionPrecondition(next, [historySegment()]));
      return commit(input);
    });
    await expect(decomposeStories({ batchId: "batch_new", authorization: authorization(["history_process"]),
      newSegments: richNewBatch(), historicalSegments: [makeSegment("history_process", "batch_old", {
        products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
      })],
    }, analyzerReturning([{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }]), library))
      .rejects.toThrow("historical-authorization-invalid");
    expect(await library.listStories()).toEqual([]);
  });

  it.each([
    { decompose: decomposeStories, boundary: "decomposeStories", revoked: "segment" },
    { decompose: prepareStoryCandidates, boundary: "prepareStoryCandidates", revoked: "segment" },
    { decompose: decomposeStories, boundary: "decomposeStories", revoked: "asset" },
    { decompose: prepareStoryCandidates, boundary: "prepareStoryCandidates", revoked: "asset" },
  ] as const)("rejects current $revoked permission withdrawn during analysis at $boundary", async ({ decompose, revoked }) => {
    const historical = historySegment();
    await seedHistoricalSources(library, [historical]);
    await seedConsent(library, [historical.id]);
    await expect(decompose({ batchId: "batch_new", authorization: authorization([historical.id]),
      newSegments: richNewBatch(), historicalSegments: [historical],
    }, { propose: async () => {
      await revokeHistoricalSource(library, historical, revoked);
      return [{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }];
    } }, library)).rejects.toThrow("historical-source-permission-invalid");
    expect(await library.listStories()).toEqual([]);
    expect(await (await JsonContentStore.open(join(testDirectory, "library.json"))).listStories()).toEqual([]);
  });

  it.each(["segment", "asset"] as const)("atomically rejects current %s permission withdrawn just before story persistence", async (revoked) => {
    const historical = historySegment();
    await seedHistoricalSources(library, [historical]);
    await seedConsent(library, [historical.id]);
    const commit = library.commitStoryCandidates.bind(library);
    vi.spyOn(library, "commitStoryCandidates").mockImplementationOnce(async (input) => {
      await revokeHistoricalSource(library, historical, revoked);
      return commit(input);
    });
    await expect(decomposeStories({ batchId: "batch_new", authorization: authorization([historical.id]),
      newSegments: richNewBatch(), historicalSegments: [historical],
    }, analyzerReturning([{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }]), library))
      .rejects.toThrow("historical-source-permission-invalid");
    expect(await library.listStories()).toEqual([]);
    expect(await (await JsonContentStore.open(join(testDirectory, "library.json"))).listStories()).toEqual([]);
  });

  it.each([decomposeStories, prepareStoryCandidates])("resolves historical matches from persisted segments after analysis instead of stale caller objects", async (decompose) => {
    const historical = historySegment();
    await seedHistoricalSources(library, [historical]);
    await seedConsent(library, [historical.id]);
    const stories = await decompose({ batchId: "batch_new", authorization: authorization([historical.id]),
      newSegments: richNewBatch(), historicalSegments: [historical],
    }, { propose: async () => {
      await library.putSegments([{ ...historical, tags: { ...historical.tags, products: [], processStages: [] } }]);
      return [{ type: "product-journey", anchorSegmentIds: ["new_process_01"] }];
    } }, library);
    expect(stories).toHaveLength(1);
    expect(stories[0]!.historicalSegmentIds).toEqual([]);
  });

  it("rejects unverified selected IDs rather than treating them as authorization", async () => {
    await expect(decomposeStories({
      batchId: "batch_new", newSegments: richNewBatch(), historicalSegments: [],
      authorization: { selectedSegmentIds: ["unconsented_history"] },
    } as unknown as Parameters<typeof decomposeStories>[0], analyzerReturning([]), library))
      .rejects.toThrow("historical-authorization-invalid");
  });

  it("creates multiple stories from one new batch and keeps new anchors in every story", async () => {
    await seedConsent(library, ["history_process"]);
    const newSegments = richNewBatch();
    const proposals: StoryProposal[] = [
      {
        type: "product-journey",
        anchorSegmentIds: ["new_process_01", "new_result_01"],
      },
      { type: "product-journey", anchorSegmentIds: ["new_rework_01"] },
      {
        type: "inner-journey",
        anchorSegmentIds: ["new_quote_01", "new_rework_01"],
      },
      { type: "persona-monologue", anchorSegmentIds: ["new_quote_02"] },
    ];

    const stories = await decomposeStories(
      {
        batchId: "batch_new",
        authorization: authorization(["history_process"]),
        newSegments,
        historicalSegments: [
          makeSegment("history_process", "batch_history", {
            products: [{ id: "prod_01", status: "confirmed" }],
            processStages: ["design"],
          }),
        ],
      },
      analyzerReturning(proposals),
      library,
    );

    expect(stories).toHaveLength(4);
    expect(
      stories.every((story) =>
        story.anchorSegmentIds.every((id) => id.startsWith("new_")),
      ),
    ).toBe(true);
    expect(stories.some((story) => story.historicalSegmentIds.length > 0)).toBe(
      true,
    );
    expect(await library.listStories()).toEqual(stories);
  });

  it("returns zero stories and no reshoot request when the batch has no complete story", async () => {
    const result = await decomposeStories(
      {
        batchId: "batch_new",
        newSegments: [makeSegment("new_thin", "batch_new")],
        authorization: { selectedSegmentIds: [] },
        historicalSegments: [],
      },
      analyzerReturning([]),
      library,
    );

    expect(result).toEqual([]);
    expect(JSON.stringify(result)).not.toMatch(
      /reshoot|supplement|missingAsset/i,
    );
  });

  it("rejects a proposal that tries to use history as its new-story anchor", async () => {
    await expect(
      decomposeStories(
        {
          batchId: "batch_new",
          authorization: { selectedSegmentIds: [] },
          newSegments: [makeSegment("new_01", "batch_new")],
          historicalSegments: [makeSegment("history_01", "batch_history")],
        },
        analyzerReturning([
          { type: "product-journey", anchorSegmentIds: ["history_01"] },
        ]),
        library,
      ),
    ).rejects.toThrow("new-batch-anchor-required");
  });

  it("does not join process stages from different confirmed products", async () => {
    const first = makeSegment("new_product_a", "batch_new", {
      products: [{ id: "product_a", status: "confirmed" }],
      processStages: ["making"],
    });
    const second = makeSegment("new_product_b", "batch_new", {
      products: [{ id: "product_b", status: "confirmed" }],
      processStages: ["finished"],
    });
    const result = await decomposeStories(
      { batchId: "batch_new", authorization: { selectedSegmentIds: [] }, newSegments: [first, second], historicalSegments: [] },
      analyzerReturning([{ type: "product-journey", anchorSegmentIds: [first.id, second.id] }]),
      library,
    );
    expect(result).toEqual([]);
  });

  it("uses only exact post-shoot historical selections for the current cut", async () => {
    const anchors = richNewBatch().slice(0, 2);
    const selected = makeSegment("history_selected", "batch_history", {
      products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
    });
    const rejected = makeSegment("history_rejected", "batch_history", {
      products: [{ id: "prod_01", status: "confirmed" }], processStages: ["making"],
    });
    const proposal = analyzerReturning([{ type: "product-journey", anchorSegmentIds: anchors.map((item) => item.id) }]);

    const withoutConsent = await decomposeStories({
      batchId: "batch_new", authorization: { selectedSegmentIds: [] },
      newSegments: anchors, historicalSegments: [selected, rejected],
    }, proposal, library);
    expect(withoutConsent[0]?.historicalSegmentIds).toEqual([]);

    const consentedLibrary = await JsonContentStore.open(join(testDirectory, "consented.json"));
    await seedHistoricalSources(consentedLibrary, [selected, rejected]);
    await consentedLibrary.putCreativeSession(consentSession());
    const postSet = {
      id: "post_set_01", sessionId: "creative_post_01", version: 1, purpose: "post-shoot-edit",
      suggestions: [selected, rejected].map((segment) => ({ segmentId: segment.id,
        thumbnailPath: `/tmp/${segment.id}.mp4`, capturedAt: "2026-09-06T08:00:00.000Z",
        productId: segment.tags.products[0]?.id ?? null,
        processStage: segment.tags.processStages[0] ?? null, reason: "current cut evidence",
        source: "historical", permissionState: "allowed" as const, lastUsedAt: null, technicallyAvailable: true })),
      createdAt: "2026-09-06T08:00:00.000Z",
    } as const;
    await consentedLibrary.putMaterialSuggestionSet(postSet,
      suggestionPrecondition(postSet, [selected, rejected]));
    await consentedLibrary.putMaterialUseDecision({
      id: "post_decision_01", sessionId: "creative_post_01", suggestionSetId: "post_set_01",
      suggestionVersion: 1, purpose: "post-shoot-edit", action: "confirm-selection",
      shownSegmentIds: [selected.id, rejected.id], selectedSegmentIds: [selected.id],
      rejectedSegmentIds: [rejected.id], actorId: "designer_01", createdAt: "2026-09-06T08:01:00.000Z",
    });
    const withConsent = await decomposeStories({
      batchId: "batch_new", authorization: authorization([selected.id]),
      newSegments: anchors, historicalSegments: [selected, rejected],
    }, proposal, consentedLibrary);
    expect(withConsent[0]?.historicalSegmentIds).toEqual([selected.id]);
  });

  it.each([
    { creativeSessionId: "other_session" },
    { suggestionSetId: "other_set" },
    { suggestionSetVersion: 2 },
    { decisionId: "other_decision" },
    { selectedSegmentIds: ["not_a_candidate"] },
  ])("rejects historical consent with mismatched identity %j", async (patch) => {
    await seedConsent(library, ["history_process"]);
    await expect(decomposeStories({
      batchId: "batch_new", newSegments: richNewBatch(), historicalSegments: [],
      authorization: { ...authorization(["history_process"]), ...patch },
    } as Parameters<typeof decomposeStories>[0], analyzerReturning([]), library))
      .rejects.toThrow("historical-authorization-invalid");
  });

  it("rejects a formerly exact decision after the candidate set changes", async () => {
    await seedConsent(library, ["history_process"]);
    const oldSet = (await library.listMaterialSuggestionSets())[0]!;
    const newSet = { ...oldSet, id: "new_set", version: 2 };
    await library.putMaterialSuggestionSet(newSet, suggestionPrecondition(newSet, [historySegment()]));
    await expect(decomposeStories({
      batchId: "batch_new", newSegments: richNewBatch(), historicalSegments: [],
      authorization: authorization(["history_process"]),
    } as Parameters<typeof decomposeStories>[0], analyzerReturning([]), library))
      .rejects.toThrow("historical-authorization-invalid");
  });

  it.each([
    ["inner-journey", ["emotion-change", "first-person-choice"]],
    ["product-journey", ["confirmed-product", "process-change"]],
    ["persona-monologue", ["clear-opinion", "supporting-evidence"]],
  ] as const)("requires %s evidence", (type, evidence) => {
    expect(validateStoryEvidence(type, [...evidence])).toEqual({
      eligible: true,
      reasons: [...evidence],
    });
    expect(validateStoryEvidence(type, [evidence[0]])).toEqual({
      eligible: false,
      reasons: [evidence[0]],
    });
  });

  it("exposes the exact evidence inference used by story decomposition", () => {
    const segments = [
      makeSegment("design", "batch_new", {
        products: [{ id: "ring-01", status: "confirmed" }],
        processStages: ["design"],
      }),
      makeSegment("finished", "batch_new", {
        products: [{ id: "ring-01", status: "confirmed" }],
        processStages: ["finished"],
      }),
    ];

    expect(inferStoryEvidenceForSegments("product-journey", segments)).toEqual({
      eligible: true,
      reasons: ["confirmed-product", "process-change"],
    });
  });
});

function authorization(selectedSegmentIds: string[]) {
  return { creativeSessionId: "creative_post_01", suggestionSetId: "post_set_01",
    suggestionSetVersion: 1, decisionId: "post_decision_01", selectedSegmentIds };
}

function consentSession(): CreativeSession {
  return { id: "creative_post_01", customerId: "designer_01", title: "Consent fixture", stage: "editorial_review",
    currentIntentVersion: 1, currentBriefVersion: 1, currentScriptVersion: 1, currentShotPlanVersion: 1,
    materialViewDecisionIds: [], materialUseDecisionIds: ["post_decision_01"], deviceBindings: [],
    captureBatchIds: ["batch_new"], editorialBatchId: "batch_new", privacyMode: "creator", prohibitedAreas: [],
    status: "active", createdAt: "2026-09-06T08:00:00.000Z", updatedAt: "2026-09-06T08:00:00.000Z" };
}

function historySegment() {
  return makeSegment("history_process", "batch_old", {
    products: [{ id: "prod_01", status: "confirmed" }], processStages: ["design"],
  });
}

async function seedHistoricalSources(library: JsonContentStore, segments: ContentSegment[]) {
  for (const segment of segments) await library.putAsset({
    id: segment.assetId, batchId: segment.batchId, sessionId: `session_${segment.id}`,
    digest: `digest_${segment.id}`, localPath: `/tmp/${segment.id}.mp4`, mediaKind: "video",
    importedAt: "2026-09-06T08:00:00.000Z", permissionState: "allowed", origin: "historical", technicalFlags: [],
  });
  await library.putSegments(segments);
}

async function revokeHistoricalSource(library: JsonContentStore, segment: ContentSegment, revoked: "segment" | "asset") {
  if (revoked === "segment") {
    await library.commitPrivacyInvalidation({ segmentId: segment.id, actorId: "designer_01", reason: "permission withdrawn during analysis" });
  } else {
    const asset = (await library.listAssets()).find((item) => item.id === segment.assetId)!;
    const blockedAsset = await library.putAsset({ ...asset, id: "blocked_history_asset", digest: "blocked_history_digest", permissionState: "blocked" });
    await library.putSegments([{ ...segment, assetId: blockedAsset.id }]);
  }
}

async function seedConsent(library: JsonContentStore, ids: string[]) {
  const persistedIds = new Set((await library.listSegments()).map((segment) => segment.id));
  await seedHistoricalSources(library, ids.filter((id) => !persistedIds.has(id)).map((id) =>
    ({ ...historySegment(), id, assetId: `asset_${id}` })));
  await library.putCreativeSession(consentSession());
  const suggestionSet = {
    id: "post_set_01", sessionId: "creative_post_01", version: 1, purpose: "post-shoot-edit",
    suggestions: ids.map((segmentId) => ({ segmentId, thumbnailPath: `/tmp/${segmentId}.mp4`,
      capturedAt: "2026-09-06T08:00:00.000Z", productId: "prod_01", processStage: "design",
      reason: "Historical evidence", source: "historical", permissionState: "allowed" as const,
      lastUsedAt: null, technicallyAvailable: true })), createdAt: "2026-09-06T08:00:00.000Z",
  } as const;
  await library.putMaterialSuggestionSet(suggestionSet,
    suggestionPrecondition(suggestionSet, ids.map((id) => ({ ...historySegment(), id, assetId: `asset_${id}` }))));
  await library.putMaterialUseDecision({ id: "post_decision_01", sessionId: "creative_post_01",
    suggestionSetId: "post_set_01", suggestionVersion: 1, action: "confirm-selection", purpose: "post-shoot-edit",
    shownSegmentIds: ids, selectedSegmentIds: ids, rejectedSegmentIds: [], actorId: "designer_01",
    createdAt: "2026-09-06T08:01:00.000Z" });
}

function suggestionPrecondition(
  suggestionSet: { id: string; purpose?: "pre-shoot-script" | "post-shoot-edit" },
  segments: ContentSegment[],
) {
  const session = consentSession();
  const assets = segments.map((segment) => ({
    id: segment.assetId, batchId: segment.batchId, sessionId: `session_${segment.id}`,
    digest: `digest_${segment.id}`, localPath: `/tmp/${segment.id}.mp4`, mediaKind: "video" as const,
    importedAt: "2026-09-06T08:00:00.000Z", permissionState: "allowed" as const,
    origin: "historical" as const, technicalFlags: [],
  }));
  return {
    suggestionSetId: suggestionSet.id,
    purpose: suggestionSet.purpose ?? "pre-shoot-script" as const,
    sources: resolveHistoricalMaterialCatalog(session, segments, assets, [session]).sources,
  };
}

function analyzerReturning(proposals: StoryProposal[]): StoryAnalyzer {
  return { propose: async () => proposals };
}

function richNewBatch(): ContentSegment[] {
  return [
    makeSegment("new_process_01", "batch_new", {
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["material-selection", "making"],
    }),
    makeSegment("new_result_01", "batch_new", {
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["finished"],
    }),
    makeSegment("new_rework_01", "batch_new", {
      baseTypes: ["handmade"],
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["making", "rework"],
      emotions: ["persistence"],
    }),
    makeSegment("new_quote_01", "batch_new", {
      baseTypes: ["persona-copy"],
      transcript: "我犹豫过，但最后决定再做一次",
      emotions: ["frustration"],
    }),
    makeSegment("new_quote_02", "batch_new", {
      baseTypes: ["persona-copy", "handmade"],
      products: [{ id: "prod_01", status: "confirmed" }],
      transcript: "我认为好珠宝不应该掩盖手工的痕迹",
    }),
  ];
}

function makeSegment(
  id: string,
  batchId: string,
  tagPatch: Partial<ContentSegment["tags"]> = {},
): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId,
    startMs: 0,
    endMs: 10_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["scene"],
      people: [],
      products: [],
      processStages: [],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: {
        storyIds: [],
        variants: [],
        useCount: 0,
        lastUsedAt: null,
      },
      ...tagPatch,
    },
  };
}
