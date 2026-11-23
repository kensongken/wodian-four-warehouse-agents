import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ContentDemand, StudioMediaAsset } from "@wearable/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CustomerMaterialLibrary,
  STUDIO_PREFERENCE_POLICY_VERSION,
  StudioPreferenceRanker,
  type StudioPreferenceCandidate,
  type StudioScope,
} from "../src";

const scope: StudioScope = {
  actorId: "actor-a",
  customerId: "customer-a",
  sessionId: "session-a",
};

describe("customer-isolated studio preference ranking", () => {
  const roots: string[] = [];

  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("waits for three explicit accepted reviews and never learns an unreviewed model suggestion", async () => {
    const { ranker, assets } = await harness(roots, 4);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordSuggestionReview(scope, reviewEvent(assets[index]!, index, {
        action: "accept-suggestion",
        target: "designer-viewpoint",
      }));
    }

    const beforeThreshold = await ranker.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-a" })],
      demand(),
    );
    expect(beforeThreshold[0]).toMatchObject({
      baseScore: 0.6,
      preferenceAdjustment: 0.05,
      score: 0.65,
      reasons: ["approved-preference:designer-viewpoint"],
      preferencePolicyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
    });

    const unreviewed = candidate(assets[3]!, { materialCardId: "card-unreviewed" });
    const ranked = await ranker.rank(scope, [unreviewed], demand());
    expect(ranked[0]).toMatchObject({ preferenceAdjustment: 0, reasons: [] });
    await expect(ranker.rank(scope, [{
      ...unreviewed,
      observations: ["designer-viewpoint"],
    } as StudioPreferenceCandidate], demand())).rejects.toThrow("studio-preference-candidate-invalid");
  });

  it("does not apply a preference until the third matching approved event", async () => {
    const { ranker, assets } = await harness(roots, 3);
    const target = candidate(assets[0]!, { materialCardId: "card-a" });
    for (let index = 0; index < 2; index += 1) {
      await ranker.recordSuggestionReview(scope, reviewEvent(assets[index]!, index, {
        action: "accept-suggestion",
        target: "making-process",
      }));
    }
    expect((await ranker.rank(scope, [target], demand()))[0]!.preferenceAdjustment).toBe(0);

    await ranker.recordSuggestionReview(scope, reviewEvent(assets[2]!, 2, {
      action: "accept-suggestion",
      target: "making-process",
    }));
    expect((await ranker.rank(scope, [target], demand()))[0]!.preferenceAdjustment).toBe(0.05);
  });

  it("does not let three receipt ids for one reviewed source satisfy the threshold", async () => {
    const { ranker, assets } = await harness(roots, 1);
    const target = candidate(assets[0]!, { materialCardId: "card-a" });
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordSuggestionReview(scope, {
        ...reviewEvent(assets[0]!, index, {
          action: "accept-suggestion",
          target: "making-process",
        }),
        receiptJobId: `different-receipt-${index}`,
      });
    }

    expect((await ranker.rank(scope, [target], demand()))[0]).toMatchObject({
      preferenceAdjustment: 0,
      reasons: [],
    });
  });

  it("does not let three old targets corrected to one value on one source satisfy the threshold", async () => {
    const { ranker, assets } = await harness(roots, 1);
    for (const [index, target] of ["旧标签甲", "旧标签乙", "旧标签丙"].entries()) {
      await ranker.recordSuggestionReview(scope, {
        ...reviewEvent(assets[0]!, index, { action: "correct-suggestion", target }),
        correction: "同一个人工新标签",
      });
    }

    expect((await ranker.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-corrected-once" })],
      demand(),
    ))[0]).toMatchObject({ preferenceAdjustment: 0, reasons: [] });
  });

  it("does not let three suitability receipts for one source satisfy the threshold", async () => {
    const { ranker, assets } = await harness(roots, 1);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordMaterialSuitability(scope, {
        operationKey: `suitability-operation-${index}`,
        eventId: `suitability-event-${index}`,
        decision: "suitable",
        demandId: "demand-a",
        matchAssessmentId: `match-${index}`,
        receiptJobId: `receipt-${index}`,
        source: binding(assets[0]!),
        reason: "human approved this source for the demand",
        createdAt: `2026-09-13T00:00:${String(index).padStart(2, "0")}.000Z`,
      });
    }

    expect((await ranker.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-a" })],
      demand(),
    ))[0]).toMatchObject({ preferenceAdjustment: 0, reasons: [] });
  });

  it("normalizes a multi-source draft sample independently of draft id and source order", async () => {
    const { ranker, assets } = await harness(roots, 2);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordDraftFeedback(scope, {
        operationKey: `draft-operation-${index}`,
        eventId: `draft-event-${index}`,
        action: "reject",
        draftId: `different-draft-${index}`,
        demandId: "demand-a",
        sources: (index % 2 === 0 ? assets : [...assets].reverse()).map(binding),
        createdAt: `2026-09-13T00:01:${String(index).padStart(2, "0")}.000Z`,
      });
    }

    const ranked = await ranker.rank(
      scope,
      assets.map((asset, index) => candidate(asset, { materialCardId: `card-${index}` })),
      demand(),
    );
    expect(ranked.every((row) => row.preferenceAdjustment === 0)).toBe(true);
    expect(ranked.every((row) => row.reasons.length === 0)).toBe(true);
  });

  it("drops derived-only draft feedback when the persisted canonical ledger is empty", async () => {
    const { ranker, library, assets } = await harness(roots, 3);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordDraftFeedback(scope, {
        operationKey: `orphan-draft-operation-${index}`,
        eventId: `orphan-draft-event-${index}`,
        action: "reject",
        draftId: `orphan-draft-${index}`,
        demandId: "demand-a",
        sources: [binding(assets[index]!)],
        createdAt: `2026-09-13T00:02:${String(index).padStart(2, "0")}.000Z`,
      });
    }
    const candidates = assets.map((asset, index) => candidate(asset, {
      materialCardId: `orphan-card-${index}`,
    }));
    expect((await ranker.rank(scope, candidates, demand())).every((row) =>
      row.preferenceAdjustment === -0.05)).toBe(true);

    const restarted = new StudioPreferenceRanker(library);
    await restarted.reconcilePersistedDraftFeedback(scope);
    expect((await restarted.rank(scope, candidates, demand())).every((row) =>
      row.preferenceAdjustment === 0 && row.reasons.length === 0)).toBe(true);
  });

  it("persists scope-bound canonical draft feedback with order-independent idempotency", async () => {
    const { ranker, library, assets } = await harness(roots, 2);
    const event = {
      operationKey: "canonical-draft-operation",
      eventId: "canonical-draft-event",
      action: "reject" as const,
      draftId: "canonical-draft",
      demandId: "demand-a",
      sources: assets.map(binding),
      createdAt: "2026-09-13T00:03:00.000Z",
    };
    const feedback = canonicalDraftFeedback(event, "人工明确拒绝这版草稿");
    await ranker.commitCanonicalDraftFeedback(scope, event, feedback);
    await expect(ranker.commitCanonicalDraftFeedback(scope, {
      ...event,
      sources: [...event.sources].reverse(),
      createdAt: "2026-09-13T00:04:00.000Z",
    }, { ...feedback, createdAt: "2026-09-13T00:04:00.000Z" })).resolves.toBeUndefined();

    await expect(library.readStudioDraftFeedbackState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    )).resolves.toMatchObject({
      schemaVersion: 1,
      policyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
      actorId: scope.actorId,
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      events: [{
        feedback: {
          id: event.eventId,
          reason: "人工明确拒绝这版草稿",
          action: "reject",
        },
        preference: {
          eventId: event.eventId,
          operationKey: event.operationKey,
        },
      }],
    });
    await expect(ranker.commitCanonicalDraftFeedback(scope, {
      ...event,
      action: "adopt",
    }, { ...feedback, action: "adopt" })).rejects.toThrow(
      "studio-draft-feedback-operation-key-conflict",
    );
  });

  it("rejects wrong bindings and foreign or unknown canonical draft state fields", async () => {
    const { ranker, library, assets } = await harness(roots, 1);
    const event = {
      operationKey: "invalid-canonical-draft-operation",
      eventId: "invalid-canonical-draft-event",
      action: "reject" as const,
      draftId: "invalid-canonical-draft",
      demandId: "demand-a",
      sources: [{ ...binding(assets[0]!), sourceDigest: "f".repeat(64) }],
      createdAt: "2026-09-13T00:05:00.000Z",
    };
    await expect(ranker.commitCanonicalDraftFeedback(
      scope,
      event,
      canonicalDraftFeedback(event, "绑定错误"),
    ))
      .rejects.toThrow("studio-preference-source-binding-mismatch");

    const baseState = {
      schemaVersion: 1,
      policyVersion: STUDIO_PREFERENCE_POLICY_VERSION,
      actorId: "actor-b",
      customerId: scope.customerId,
      sessionId: scope.sessionId,
      events: [],
    };
    await library.writeStudioDraftFeedbackState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
      baseState,
    );
    await expect(ranker.reconcilePersistedDraftFeedback(scope))
      .rejects.toThrow("studio-draft-feedback-state-invalid");

    await library.writeStudioDraftFeedbackState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
      { ...baseState, actorId: scope.actorId, unknown: true },
    );
    await expect(ranker.reconcilePersistedDraftFeedback(scope))
      .rejects.toThrow("studio-draft-feedback-state-invalid");
  });

  it("does not publish canonical or derived draft state when the atomic commit fails", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-preference-atomic-failure-"));
    roots.push(root);
    const library = new CustomerMaterialLibrary(root, () => true, {
      atomicRename: async () => {
        throw new Error("injected-atomic-rename-failure");
      },
    });
    const asset = await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: "atomic-failure-import",
      bytes: Buffer.from("synthetic-atomic-failure-source"),
      mediaKind: "video",
      fileName: "synthetic-atomic-failure.mp4",
      capturedAt: "2026-09-13T00:06:00.000Z",
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    });
    const ranker = new StudioPreferenceRanker(library);
    const event = {
      operationKey: "atomic-failure-operation",
      eventId: "atomic-failure-event",
      action: "reject",
      draftId: "atomic-failure-draft",
      demandId: "demand-a",
      sources: [binding(asset)],
      createdAt: "2026-09-13T00:06:01.000Z",
    } as const;
    await expect(ranker.commitCanonicalDraftFeedback(
      scope,
      event,
      canonicalDraftFeedback(event, "原子写失败"),
    )).rejects.toThrow("injected-atomic-rename-failure");
    await expect(library.readStudioDraftFeedbackState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    )).resolves.toBeNull();
    await expect(library.readStudioPreferenceState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    )).resolves.toBeNull();
  });

  it("caps additive review adjustments at plus or minus 0.15", async () => {
    const { ranker, assets } = await harness(roots, 12);
    const target = candidate(assets[0]!, { materialCardId: "card-a" });
    for (let index = 0; index < 12; index += 1) {
      await ranker.recordSuggestionReview(scope, reviewEvent(assets[index]!, index, {
        action: "accept-suggestion",
        target: "designer-viewpoint",
      }));
    }
    expect((await ranker.rank(scope, [target], demand()))[0]!.preferenceAdjustment).toBe(0.15);

    const negative = await harness(roots, 12);
    for (let index = 0; index < 12; index += 1) {
      await negative.ranker.recordSuggestionReview(scope, reviewEvent(negative.assets[index]!, index, {
        action: "reject-suggestion",
        target: "designer-viewpoint",
      }));
    }
    expect((await negative.ranker.rank(
      scope,
      [candidate(negative.assets[0]!, { materialCardId: "card-negative" })],
      demand(),
    ))[0]!.preferenceAdjustment).toBe(-0.15);
  }, 30_000);

  it("keeps customer, session, and actor preferences isolated", async () => {
    const { ranker, assets } = await harness(roots, 3);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordSuggestionReview(scope, reviewEvent(assets[index]!, index, {
        action: "accept-suggestion",
        target: "designer-viewpoint",
      }));
    }
    const candidateA = candidate(assets[0]!, { materialCardId: "card-a" });
    for (const otherScope of [
      { ...scope, actorId: "actor-b" },
      { ...scope, sessionId: "session-b" },
      { ...scope, customerId: "customer-b" },
    ]) {
      const ranked = await ranker.rank(otherScope, [{
        ...candidateA,
        customerId: otherScope.customerId,
        sessionId: otherScope.sessionId,
        materialCardId: `card-${otherScope.actorId}-${otherScope.sessionId}-${otherScope.customerId}`,
      }], { ...demand(), customerId: otherScope.customerId, sessionId: otherScope.sessionId });
      expect(ranked[0]!.preferenceAdjustment).toBe(0);
    }
  });

  it("records material suitability and adopted or rejected draft feedback only as explicit human events", async () => {
    const { ranker, assets } = await harness(roots, 3);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordMaterialSuitability(scope, {
        operationKey: `suitability-${index}`,
        eventId: `suitability-event-${index}`,
        decision: "suitable",
        demandId: "demand-a",
        matchAssessmentId: "match-a",
        receiptJobId: `receipt-${index}`,
        source: binding(assets[index]!),
        reason: "human approved this interval for the current demand",
        createdAt: `2026-09-13T00:00:${String(index).padStart(2, "0")}.000Z`,
      });
    }
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordDraftFeedback(scope, {
        operationKey: `draft-${index}`,
        eventId: `draft-feedback-${index}`,
        action: "adopt",
        draftId: `draft-${index}`,
        demandId: "demand-a",
        sources: [binding(assets[index]!)],
        createdAt: `2026-09-13T00:01:${String(index).padStart(2, "0")}.000Z`,
      });
    }

    const ranked = await ranker.rank(
      scope,
      assets.map((asset, index) => candidate(asset, { materialCardId: `card-${index}` })),
      demand(),
    );
    expect(ranked.every((row) => row.preferenceAdjustment === 0.1)).toBe(true);
    expect(ranked[0]!.reasons).toEqual([
      "approved-preference:draft-adoption:single-product",
      "approved-preference:material-suitability:single-product",
    ]);

    await expect(ranker.recordDraftFeedback(scope, {
      operationKey: "unreviewed-model-output",
      eventId: "model-output",
      action: "model-suggestion" as "adopt",
      draftId: "draft-model",
      demandId: "demand-a",
      sources: [binding(assets[0]!)],
      createdAt: "2026-09-13T00:02:00.000Z",
    })).rejects.toThrow("studio-preference-event-invalid");
  });

  it("is idempotent, detects operation conflicts, and recovers atomically after restart", async () => {
    const { ranker, library, assets } = await harness(roots, 3);
    const events = assets.map((asset, index) => reviewEvent(asset, index, {
      action: "accept-suggestion",
      target: "designer-viewpoint",
    }));
    for (const event of events) await ranker.recordSuggestionReview(scope, event);
    await expect(ranker.recordSuggestionReview(scope, events[0]!)).resolves.toMatchObject({
      sourceEventId: events[0]!.eventId,
    });
    await expect(ranker.recordSuggestionReview(scope, {
      ...events[0]!,
      createdAt: "2026-09-13T01:00:00.000Z",
    })).resolves.toMatchObject({
      sourceEventId: events[0]!.eventId,
      createdAt: events[0]!.createdAt,
    });
    await expect(ranker.recordSuggestionReview(scope, {
      ...events[0]!,
      target: "different-target",
    })).rejects.toThrow("studio-preference-operation-key-conflict");

    const restarted = new StudioPreferenceRanker(library, {
      now: () => "2026-09-13T01:00:00.000Z",
    });
    const ranked = await restarted.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-after-restart" })],
      demand(),
    );
    expect(ranked[0]!.preferenceAdjustment).toBe(0.05);
  });

  it("rebuilds from the authoritative review set and drops a derived orphan after restart", async () => {
    const { ranker, library, assets } = await harness(roots, 3);
    const canonical = assets.slice(0, 2).map((asset, index) => reviewEvent(asset, index, {
      action: "accept-suggestion",
      target: "designer-viewpoint",
    }));
    const orphan = reviewEvent(assets[2]!, 2, {
      action: "accept-suggestion",
      target: "designer-viewpoint",
    });
    for (const event of [...canonical, orphan]) {
      await ranker.recordSuggestionReview(scope, event);
    }
    expect((await ranker.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-before-canonical-rebuild" })],
      demand(),
    ))[0]!.preferenceAdjustment).toBe(0.05);

    const restarted = new StudioPreferenceRanker(library);
    await restarted.reconcileSuggestionReviews(scope, canonical);

    expect((await restarted.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-after-canonical-rebuild" })],
      demand(),
    ))[0]).toMatchObject({ preferenceAdjustment: 0, reasons: [] });
    await expect(library.readStudioPreferenceState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    )).resolves.toMatchObject({
      events: canonical.map((event) => ({ sourceEventId: event.eventId })),
    });
  });

  it("discards an old-version derived ledger and rebuilds only authoritative canonical reviews", async () => {
    const { ranker, library, assets } = await harness(roots, 3);
    const canonical = assets.slice(0, 2).map((asset, index) => reviewEvent(asset, index, {
      action: "accept-suggestion",
      target: "old-version-derived-signal",
    }));
    for (const event of [
      ...canonical,
      reviewEvent(assets[2]!, 2, {
        action: "accept-suggestion",
        target: "old-version-derived-signal",
      }),
    ]) await ranker.recordSuggestionReview(scope, event);
    const oldDerived = await library.readStudioPreferenceState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
    ) as Record<string, unknown>;
    await library.writeStudioPreferenceState(
      scope.actorId,
      scope.customerId,
      scope.sessionId,
      { ...oldDerived, policyVersion: "studio-preference-policy-v0" },
    );

    const restarted = new StudioPreferenceRanker(library);
    await expect(restarted.reconcileSuggestionReviews(scope, canonical)).resolves.toBeUndefined();
    await expect(restarted.rank(
      scope,
      [candidate(assets[0]!, { materialCardId: "card-after-old-version-rebuild" })],
      demand(),
    )).resolves.toEqual([
      expect.objectContaining({ preferenceAdjustment: 0, reasons: [] }),
    ]);
  });

  it("never changes eligibility, permission, technical gates, evidence bindings, or confirmed facts", async () => {
    const { ranker, assets } = await harness(roots, 3);
    for (let index = 0; index < 3; index += 1) {
      await ranker.recordSuggestionReview(scope, reviewEvent(assets[index]!, index, {
        action: "accept-suggestion",
        target: "confirmed-style",
      }));
    }
    const blocked = candidate(assets[0]!, {
      materialCardId: "blocked-card",
      baseScore: 0.99,
      eligible: false,
      permission: "blocked",
      technicalUsable: false,
      technicalBlockers: ["codec-invalid"],
      confirmedFacts: ["confirmed-style"],
    });
    const eligible = candidate(assets[1]!, { materialCardId: "eligible-card", baseScore: 0.1 });
    const ranked = await ranker.rank(scope, [blocked, eligible], demand());

    expect(ranked.map((row) => row.materialCardId)).toEqual(["eligible-card", "blocked-card"]);
    const result = ranked[1]!;
    expect(result).toMatchObject({
      eligible: false,
      permission: "blocked",
      technicalUsable: false,
      technicalBlockers: ["codec-invalid"],
      sourceAssetId: blocked.sourceAssetId,
      sourceDigest: blocked.sourceDigest,
      sourceStartMs: blocked.sourceStartMs,
      sourceEndMs: blocked.sourceEndMs,
      confirmedFacts: ["confirmed-style"],
    });
  });

  it("is deterministic for equal scores regardless of input order", async () => {
    const { ranker, assets } = await harness(roots, 2);
    const left = candidate(assets[0]!, { materialCardId: "card-b" });
    const right = candidate(assets[1]!, { materialCardId: "card-a" });
    const first = await ranker.rank(scope, [left, right], demand());
    const second = await ranker.rank(scope, [right, left], demand());
    expect(first).toEqual(second);
    expect(first.map((row) => row.materialCardId)).toEqual(["card-a", "card-b"]);
  });

  it("uses a code-point comparator without consulting the host locale", async () => {
    const { ranker, assets } = await harness(roots, 2);
    const localeCompare = vi.spyOn(String.prototype, "localeCompare")
      .mockImplementation(() => {
        throw new Error("host-locale-comparator-used");
      });
    try {
      const ranked = await ranker.rank(scope, [
        candidate(assets[0]!, { materialCardId: "card-甲" }),
        candidate(assets[1]!, { materialCardId: "card-A" }),
      ], demand());
      expect(ranked.map((row) => row.materialCardId)).toEqual(["card-A", "card-甲"]);
    } finally {
      localeCompare.mockRestore();
    }
  });

  it("orders BMP and astral identifiers by Unicode scalar value", async () => {
    const { ranker, assets } = await harness(roots, 2);
    const ranked = await ranker.rank(scope, [
      candidate(assets[0]!, { materialCardId: "card-\u{10000}" }),
      candidate(assets[1]!, { materialCardId: "card-\uE000" }),
    ], demand());

    expect(ranked.map((row) => row.materialCardId)).toEqual([
      "card-\uE000",
      "card-\u{10000}",
    ]);
  });
});

async function harness(roots: string[], assetCount: number) {
  const root = await mkdtemp(join(tmpdir(), "studio-preference-ranker-"));
  roots.push(root);
  const library = new CustomerMaterialLibrary(root, () => true);
  const assets: StudioMediaAsset[] = [];
  for (let index = 0; index < assetCount; index += 1) {
    assets.push(await library.importOriginal(scope.actorId, scope.customerId, {
      requestId: `request-${index}`,
      bytes: Buffer.from(`synthetic-preference-fixture-${index}`),
      mediaKind: "video",
      fileName: `synthetic-${index}.mp4`,
      capturedAt: `2026-09-13T00:00:${String(index).padStart(2, "0")}.000Z`,
      sessionId: scope.sessionId,
      viewPermission: "allowed",
      usePermission: "allowed",
    }));
  }
  return {
    library,
    assets,
    ranker: new StudioPreferenceRanker(library, {
      now: () => "2026-09-13T01:00:00.000Z",
    }),
  };
}

function demand(): ContentDemand {
  return {
    id: "demand-a",
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    contentType: "single-product",
    goal: "synthetic demand",
    targetAudience: "synthetic audience",
    desiredEffect: "synthetic effect",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
    status: "current",
    createdAt: "2026-09-13T00:00:00.000Z",
  };
}

function binding(asset: StudioMediaAsset) {
  return {
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    sourceStartMs: 0,
    sourceEndMs: 10_000,
  };
}

function reviewEvent(
  asset: StudioMediaAsset,
  index: number,
  input: Readonly<{
    action: "accept-suggestion" | "correct-suggestion" | "reject-suggestion";
    target: string;
  }>,
) {
  return {
    operationKey: `review-operation-${index}`,
    eventId: `review-event-${index}`,
    action: input.action,
    receiptJobId: `receipt-${index}`,
    target: input.target,
    correction: input.action === "correct-suggestion" ? `${input.target}-corrected` : null,
    source: binding(asset),
    createdAt: `2026-09-13T00:00:${String(index).padStart(2, "0")}.000Z`,
  };
}

function canonicalDraftFeedback(
  event: Readonly<{
    eventId: string;
    action: "adopt" | "reject";
    draftId: string;
    createdAt: string;
  }>,
  reason: string | null,
) {
  return {
    id: event.eventId,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    version: 1,
    draftId: event.draftId,
    action: event.action,
    reason,
    actorId: scope.actorId,
    publishClaimed: false as const,
    createdAt: event.createdAt,
  };
}

function candidate(
  asset: StudioMediaAsset,
  overrides: Partial<StudioPreferenceCandidate> = {},
): StudioPreferenceCandidate {
  return {
    materialCardId: `card-${asset.id}`,
    customerId: scope.customerId,
    sessionId: scope.sessionId,
    sourceAssetId: asset.id,
    sourceDigest: asset.digest,
    sourceStartMs: 0,
    sourceEndMs: 10_000,
    contentType: "single-product",
    eligible: true,
    permission: "allowed",
    technicalUsable: true,
    technicalBlockers: [],
    confirmedFacts: [],
    baseScore: 0.6,
    ...overrides,
  };
}
