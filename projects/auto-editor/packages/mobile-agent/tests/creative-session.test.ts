import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ContentSegment, LibraryAsset } from "@wearable/contracts";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  answerCreativeIntent,
  answerCreativeIntentRecoverably,
  decideMaterialViewing,
  JsonContentStore,
  startCreativeSession,
  transitionCreativeSession,
} from "../src";

describe("creative session state machine", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "wearable-creative-session-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
  });

  afterEach(async () => {
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("keeps a new session in draft intent until the customer supplies every required fact", async () => {
    const result = await startCreativeSession(sessionInput(), interpreter({
      theme: "A pendant returns to the bench",
      platforms: ["Xiaohongshu"],
      audience: "Jewelry collectors",
      shootConditions: [],
      prohibitedAreas: ["Do not show customer faces"],
      tone: "Quiet and crafted",
      durationSeconds: 30,
      products: [{ id: "product_from_interpreter", status: "confirmed" }],
    }), library);

    expect(result.session.stage).toBe("draft_intent");
    expect(result.questions).toContain("shootConditions");
    expect(result.intent.products).toEqual([
      { id: "product_from_interpreter", status: "suggested" },
    ]);
  });

  it("awaits material consent only after all required intent facts are explicit", async () => {
    const result = await startCreativeSession(sessionInput(), interpreter(completeDraft()), library);

    expect(result.session.stage).toBe("awaiting_material_consent");
    expect(result.questions).toEqual([]);
  });

  it("fails closed when a transition skips required stages", async () => {
    const { session } = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(),
      shootConditions: undefined,
    }), library);

    await expect(transitionCreativeSession({
      sessionId: session.id,
      to: "ready_to_dispatch",
      actorId: "designer_01",
      reason: "skip the gates",
      now: "2026-09-04T08:01:00.000Z",
    }, library)).rejects.toThrow("creative-session-transition-not-allowed");
  });

  it("rejects a direct draft-to-consent transition when the persisted intent remains incomplete", async () => {
    const { session } = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(),
      shootConditions: undefined,
    }), library);

    await expect(transitionCreativeSession({
      sessionId: session.id,
      to: "awaiting_material_consent",
      actorId: "designer_01",
      reason: "pretend every fact is present",
      now: "2026-09-04T08:01:00.000Z",
    }, library)).rejects.toThrow("creative-intent-incomplete");
  });

  it("treats an unspecified persisted text placeholder as an incomplete customer answer", async () => {
    const { session } = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(),
      theme: undefined,
    }), library);

    await expect(transitionCreativeSession({
      sessionId: session.id,
      to: "awaiting_material_consent",
      actorId: "designer_01",
      reason: "pretend the theme was answered",
      now: "2026-09-04T08:01:00.000Z",
    }, library)).rejects.toThrow("creative-intent-incomplete");
  });

  it("creates immutable intent version two from explicit answers and unlocks material consent", async () => {
    const answerAwareInterpreter = {
      interpret: async ({ answers }: { text: string; answers: Record<string, string> }) => {
        if (answers.availability) {
          return { shootConditions: [answers.availability] };
        }
        return {
          ...completeDraft(),
          shootConditions: undefined,
          products: [{ id: "product_01", status: "confirmed" as const }],
        };
      },
    };
    const started = await startCreativeSession(sessionInput(), answerAwareInterpreter, library);

    const answered = await answerCreativeIntent({
      sessionId: started.session.id,
      text: "The maker and workbench are available.",
      answers: { availability: "The maker and workbench are available" },
      actorId: "designer_01",
      now: "2026-09-04T08:01:00.000Z",
    }, answerAwareInterpreter, library);

    expect(answered.session).toMatchObject({
      stage: "awaiting_material_consent",
      currentIntentVersion: 2,
      prohibitedAreas: ["Do not show customer faces"],
    });
    expect(answered.intent).toMatchObject({
      version: 2,
      platforms: ["Xiaohongshu"],
      audience: "Jewelry collectors",
      products: [{ id: "product_01", status: "suggested" }],
      shootConditions: ["The maker and workbench are available"],
    });
    expect(await library.listCreativeIntents()).toHaveLength(2);
    expect(await library.listAuditEvents(started.session.id)).toContainEqual(expect.objectContaining({
      actorId: "designer_01",
      action: "creative-session-transitioned",
      before: { stage: "draft_intent" },
      after: { stage: "awaiting_material_consent" },
    }));
  });

  it("resumes a persisted intent answer after reopening without interpreting it again", async () => {
    const path = join(testDirectory, "recoverable-intent-library.json");
    const firstStore = await JsonContentStore.open(path);
    let interpretations = 0;
    const started = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(), shootConditions: undefined,
    }), firstStore);
    const input = {
      sessionId: started.session.id,
      text: "The bench is available.",
      answers: { shootConditions: "bench" },
      actorId: "designer_01",
      customerId: "customer_01",
      clientInstanceId: "creative_client_recoverable_01",
      route: `/api/creative-sessions/${started.session.id}/intent-answers`,
      bodyDigest: "body-digest-01",
      now: "2026-09-04T08:01:00.000Z",
      operationKey: "recoverable-answer-01",
    } as const;
    await expect(answerCreativeIntentRecoverably(input, {
      library: firstStore,
      intentInterpreter: {
        interpret: async () => {
          interpretations += 1;
          return { shootConditions: ["bench"] };
        },
      },
      materialMatcher: { rank: async () => { throw new Error("internal-error"); } },
    })).rejects.toThrow("creative-material-matcher-retryable");
    expect((await firstStore.listCreativeSessions())[0]).toMatchObject({
      stage: "draft_intent", currentIntentVersion: 2,
    });
    expect(await firstStore.listMaterialSuggestionSets()).toEqual([]);

    const reopened = await JsonContentStore.open(path);
    const resumed = await answerCreativeIntentRecoverably({
      ...input, now: "2026-09-04T08:02:00.000Z",
    }, {
      library: reopened,
      materialMatcher: { rank: async () => [] },
    });

    expect(resumed.session).toMatchObject({
      id: started.session.id, currentIntentVersion: 2, stage: "awaiting_material_consent",
    });
    expect(resumed.suggestionSet).toMatchObject({ sessionId: started.session.id, suggestions: [] });
    expect(interpretations).toBe(1);
    expect(await reopened.listCreativeIntents()).toHaveLength(2);
    expect(await reopened.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey: input.operationKey, phase: "complete", intentVersion: 2,
      suggestionSetId: resumed.suggestionSet!.id,
    }));
    await expect(answerCreativeIntentRecoverably({
      ...input, bodyDigest: "different-body-digest", now: "2026-09-04T08:03:00.000Z",
    }, {
      library: reopened,
      materialMatcher: { rank: async () => [] },
    })).rejects.toThrow("creative-intent-answer-operation-conflict");
    expect(await reopened.listCreativeIntents()).toHaveLength(2);
  });

  it.each(["segment-metadata", "asset-metadata"] as const)(
    "keeps a recoverable intent suggestion retryable when %s changes during matching",
    async (mutation) => {
      const path = join(testDirectory, `recoverable-content-race-${mutation}.json`);
      const store = await JsonContentStore.open(path);
      const segment = testHistoricalSegment(`recoverable_content_race_${mutation}`);
      await store.putAsset(testParentAsset(segment));
      await store.putSegments([segment]);
      const started = await startCreativeSession(sessionInput(), interpreter({
        ...completeDraft(), shootConditions: undefined,
      }), store);
      let matcherEntered!: () => void;
      let releaseMatcher!: () => void;
      const entered = new Promise<void>((resolve) => { matcherEntered = resolve; });
      const released = new Promise<void>((resolve) => { releaseMatcher = resolve; });
      const input = recoverableInput(started.session.id, `content_race_${mutation}`);
      const pending = answerCreativeIntentRecoverably(input, {
        library: store,
        intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
        materialMatcher: { rank: async () => {
          matcherEntered();
          await released;
          return [];
        } },
      });

      await entered;
      const writer = await JsonContentStore.open(path);
      if (mutation === "segment-metadata") {
        await writer.putSegments([{
          ...segment,
          tags: {
            ...segment.tags,
            people: ["late-person"],
            emotions: ["late-emotion"],
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

      await expect(pending).rejects.toThrow("creative-material-catalog-race-retryable");
      expect(await readFile(path)).toEqual(afterMutation);
      const reopened = await JsonContentStore.open(path);
      expect(await reopened.listMaterialSuggestionSets()).toEqual([]);
      expect((await reopened.listCreativeSessions())[0]).toMatchObject({
        stage: "draft_intent", currentIntentVersion: 2,
      });
      expect(await reopened.listCreativeIntents()).toHaveLength(2);
      expect(await reopened.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
        operationKey: input.operationKey, phase: "answer-stored", suggestionSetId: null,
      }));
    },
  );

  it("retries recoverable matching from answer-stored after eligible segment metadata changes", async () => {
    const path = join(testDirectory, "recoverable-eligible-content-race.json");
    const store = await JsonContentStore.open(path);
    const segment = testHistoricalSegment("recoverable_eligible_content_race");
    await store.putAsset(testParentAsset(segment));
    await store.putSegments([segment]);
    const started = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(), shootConditions: undefined,
    }), store);
    let matcherCalls = 0;
    let matcherEntered!: () => void;
    let releaseMatcher!: () => void;
    const entered = new Promise<void>((resolve) => { matcherEntered = resolve; });
    const released = new Promise<void>((resolve) => { releaseMatcher = resolve; });
    const matcher = { rank: async () => {
      matcherCalls += 1;
      if (matcherCalls === 1) {
        matcherEntered();
        await released;
      }
      return [{ segmentId: segment.id, reason: "current eligible source", score: 1 }];
    } };
    const wrappedStore = new Proxy(store, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof store.commitCreativeBundle>[0],
        ) => {
          try {
            return await target.commitCreativeBundle(bundle);
          } catch (cause) {
            throw new Error("catalog-commit-adapter-failed", { cause });
          }
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const input = recoverableInput(started.session.id, "eligible_content_race");
    const pending = answerCreativeIntentRecoverably(input, {
      library: wrappedStore,
      intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
      materialMatcher: matcher,
    });

    await entered;
    const writer = await JsonContentStore.open(path);
    const changedTranscript = "The catalog transcript changed while matching.";
    await writer.putSegments([{
      ...segment,
      tags: { ...segment.tags, topics: ["late catalog topic"], transcript: changedTranscript },
    }]);
    releaseMatcher();

    await expect(pending).rejects.toThrow("creative-material-catalog-race-retryable");
    const reopened = await JsonContentStore.open(path);
    const resumed = await answerCreativeIntentRecoverably({
      ...input, now: "2026-09-04T08:02:00.000Z",
    }, { library: reopened, materialMatcher: matcher });

    expect(resumed.session).toMatchObject({
      id: started.session.id, currentIntentVersion: 2, stage: "awaiting_material_consent",
    });
    expect(resumed.suggestionSet?.suggestions).toEqual([
      expect.objectContaining({ segmentId: segment.id }),
    ]);
    expect((await reopened.listSegments()).find((item) => item.id === segment.id)?.tags.transcript)
      .toBe(changedTranscript);
    expect(matcherCalls).toBe(2);
    expect((await reopened.listCreativeIntents()).map((intent) => intent.version)).toEqual([1, 2]);
    expect(await reopened.listMaterialSuggestionSets()).toHaveLength(1);
    expect(await reopened.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey: input.operationKey, phase: "complete", intentVersion: 2,
      suggestionSetId: resumed.suggestionSet!.id,
    }));
  });

  it("does not classify an adapter's forged material error message as a catalog race", async () => {
    const store = await JsonContentStore.open(join(testDirectory, "forged-catalog-race-message.json"));
    const started = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(), shootConditions: undefined,
    }), store);
    const forgedFailure = new Error("material-no-longer-eligible");
    Object.defineProperty(forgedFailure, "cause", { value: forgedFailure });
    const adapter = new Proxy(store, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof store.commitCreativeBundle>[0],
        ) => {
          if ((bundle.materialSuggestionSets?.length ?? 0) > 0) throw forgedFailure;
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const input = recoverableInput(started.session.id, "forged_catalog_race_message");

    await expect(answerCreativeIntentRecoverably(input, {
      library: adapter,
      intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
      materialMatcher: { rank: async () => [] },
    })).rejects.toBe(forgedFailure);
    expect(await store.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      operationKey: input.operationKey, phase: "answer-stored", suggestionSetId: null,
    }));
  });

  it.each([
    {
      name: "throwing cause getter",
      createFailure: () => {
        const failure = new Error("ordinary-commit-failure");
        Object.defineProperty(failure, "cause", {
          get() { throw new Error("stale-content-store"); },
        });
        return failure;
      },
    },
    {
      name: "hostile proxy introspection",
      createFailure: () => new Proxy(new Error("ordinary-commit-failure"), {
        get() { throw new Error("hostile-error-get-trap"); },
        getPrototypeOf() { throw new Error("hostile-error-prototype-trap"); },
        has() { throw new Error("hostile-error-has-trap"); },
        ownKeys() { throw new Error("hostile-error-own-keys-trap"); },
      }),
    },
  ])("rethrows the same ordinary adapter object for $name", async ({ createFailure }) => {
    const store = await JsonContentStore.open(join(testDirectory, "hostile-adapter-error.json"));
    const started = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(), shootConditions: undefined,
    }), store);
    const failure = createFailure();
    const adapter = new Proxy(store, {
      get(target, property) {
        if (property === "commitCreativeBundle") return async (
          bundle: Parameters<typeof store.commitCreativeBundle>[0],
        ) => {
          if ((bundle.materialSuggestionSets?.length ?? 0) > 0) throw failure;
          return target.commitCreativeBundle(bundle);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    let caught: unknown;
    try {
      await answerCreativeIntentRecoverably(
        recoverableInput(started.session.id, `hostile_${createFailure.name || "failure"}`),
        {
          library: adapter,
          intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
          materialMatcher: { rank: async () => [] },
        },
      );
    } catch (error) {
      caught = error;
    }

    expect(caught === failure).toBe(true);
  });

  it("finalizes a stored suggestion after another page legally advances material viewing", async () => {
    const path = join(testDirectory, "advanced-intent-answer-library.json");
    const store = await JsonContentStore.open(path);
    const started = await startCreativeSession(sessionInput(), interpreter({
      ...completeDraft(), shootConditions: undefined,
    }), store);
    let failFinalization = true;
    const interrupted = new Proxy(store, {
      get(target, property) {
        if (property === "putCreativeIntentAnswerOperation") return async (
          value: Parameters<typeof store.putCreativeIntentAnswerOperation>[0],
        ) => {
          if (value.phase === "complete" && failFinalization) {
            failFinalization = false;
            throw new Error("content-store-lock-timeout");
          }
          return target.putCreativeIntentAnswerOperation(value);
        };
        const value = target[property as keyof typeof target];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const input = {
      sessionId: started.session.id,
      text: "The bench is available.",
      answers: { shootConditions: "bench" },
      actorId: "designer_01",
      customerId: "customer_01",
      clientInstanceId: "creative_client_advanced_01",
      route: `/api/creative-sessions/${started.session.id}/intent-answers`,
      bodyDigest: "body-digest-advanced-01",
      now: "2026-09-04T08:01:00.000Z",
      operationKey: "advanced-answer-01",
    } as const;

    await expect(answerCreativeIntentRecoverably(input, {
      library: interrupted,
      intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
      materialMatcher: { rank: async () => [] },
    })).rejects.toThrow("content-store-lock-timeout");
    const storedOperation = (await store.listCreativeIntentAnswerOperations())[0]!;
    expect(storedOperation).toMatchObject({ phase: "suggestion-stored", intentVersion: 2 });
    await decideMaterialViewing({
      sessionId: started.session.id,
      suggestionSetId: storedOperation.suggestionSetId!,
      decision: "accept",
      actorId: "customer_01",
      now: "2026-09-04T08:02:00.000Z",
    }, store);

    const reopened = await JsonContentStore.open(path);
    const resumed = await answerCreativeIntentRecoverably({
      ...input, now: "2026-09-04T08:03:00.000Z",
    }, { library: reopened, materialMatcher: { rank: async () => [] } });

    expect(resumed.session.stage).toBe("reviewing_materials");
    expect(await reopened.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
      id: storedOperation.id,
      phase: "complete",
      intentVersion: 2,
      suggestionSetId: storedOperation.suggestionSetId,
    }));
    expect(await reopened.listCreativeIntents()).toHaveLength(2);
  });

  it.each([
    { parentCase: "none", rejected: false },
    { parentCase: "valid", rejected: false },
    { parentCase: "blocked", rejected: true },
    { parentCase: "missing", rejected: true },
    { parentCase: "current-batch", rejected: true },
    { parentCase: "mismatched-batch", rejected: true },
  ] as const)(
    "settles first intent suggestion safely for $parentCase historical catalog",
    async ({ parentCase, rejected }) => {
      const segment = testHistoricalSegment(`first_${parentCase}`);
      if (parentCase === "valid") {
        await library.putAsset(testParentAsset(segment));
        await library.putSegments([segment]);
      } else if (parentCase !== "none") {
        await seedInvalidParentCase(library, segment, parentCase);
      }
      const started = await startCreativeSession(sessionInput(), interpreter({
        ...completeDraft(), shootConditions: undefined,
      }), library);
      let matcherCalls = 0;

      const result = answerCreativeIntentRecoverably(recoverableInput(started.session.id, parentCase), {
        library,
        intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
        materialMatcher: { rank: async () => {
          matcherCalls += 1;
          return [];
        } },
      });

      if (!rejected) {
        await expect(result).resolves.toMatchObject({
          session: { stage: "awaiting_material_consent" },
          suggestionSet: { suggestions: [] },
        });
        expect(matcherCalls).toBe(1);
        expect(await library.listMaterialSuggestionSets()).toHaveLength(1);
        return;
      }

      await expect(result).rejects.toThrow("historical-material-catalog-invalid");

      expect((await library.listCreativeSessions())[0]).toMatchObject({
        stage: "draft_intent", currentIntentVersion: 2,
      });
      expect(matcherCalls).toBe(0);
      expect(await library.listMaterialSuggestionSets()).toEqual([]);
      expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
        phase: "answer-stored", suggestionSetId: null,
      }));
    },
  );

  it.each(["blocked", "missing", "current-batch", "mismatched-batch"] as const)(
    "rejects a now-%s parent asset when resuming suggestion-stored",
    async (parentCase) => {
      const segment = testHistoricalSegment(`resume_${parentCase}`);
      await library.putAsset(testParentAsset(segment));
      await library.putSegments([segment]);
      const started = await startCreativeSession(sessionInput(), interpreter({
        ...completeDraft(), shootConditions: undefined,
      }), library);
      let interruptFinalization = true;
      const interrupted = new Proxy(library, {
        get(target, property) {
          if (property === "putCreativeIntentAnswerOperation") return async (
            value: Parameters<typeof library.putCreativeIntentAnswerOperation>[0],
          ) => {
            if (value.phase === "complete" && interruptFinalization) {
              interruptFinalization = false;
              throw new Error("content-store-lock-timeout");
            }
            return target.putCreativeIntentAnswerOperation(value);
          };
          const value = target[property as keyof typeof target];
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
      const input = recoverableInput(started.session.id, `resume_${parentCase}`);
      await expect(answerCreativeIntentRecoverably(input, {
        library: interrupted,
        intentInterpreter: { interpret: async () => ({ shootConditions: ["bench"] }) },
        materialMatcher: { rank: async () => [{ segmentId: segment.id, reason: "valid first", score: 1 }] },
      })).rejects.toThrow("content-store-lock-timeout");
      await reparentSegment(library, segment, parentCase);

      await expect(answerCreativeIntentRecoverably({ ...input, now: "2026-09-04T08:03:00.000Z" }, {
        library,
        materialMatcher: { rank: async () => [] },
      })).rejects.toThrow("creative-intent-answer-operation-state-invalid");

      expect((await library.listCreativeSessions())[0]?.stage).toBe("awaiting_material_consent");
      expect(await library.listCreativeIntentAnswerOperations()).toContainEqual(expect.objectContaining({
        phase: "suggestion-stored",
      }));
    },
  );

  it("allows multiple non-shooting sessions but only one shooting session", async () => {
    const first = await startCreativeSession(sessionInput(), interpreter(completeDraft()), library);
    const second = await startCreativeSession({ ...sessionInput(), title: "A second story" }, interpreter(completeDraft()), library);

    expect((await library.listCreativeSessions()).map((session) => session.stage))
      .toEqual(["awaiting_material_consent", "awaiting_material_consent"]);

    await moveToShooting(first.session.id);
    await moveToReadyToDispatch(second.session.id);

    await expect(transitionCreativeSession({
      sessionId: second.session.id,
      to: "shooting",
      actorId: "designer_01",
      reason: "begin capture",
      now: "2026-09-04T08:09:00.000Z",
    }, library)).rejects.toThrow("active-shooting-session-exists");
  });

  it("records actor, reason, and before-after stages for every successful transition", async () => {
    const { session } = await startCreativeSession(sessionInput(), interpreter(completeDraft()), library);

    const transitioned = await transitionCreativeSession({
      sessionId: session.id,
      to: "reviewing_materials",
      actorId: "designer_01",
      reason: "customer agreed to review materials",
      now: "2026-09-04T08:01:00.000Z",
    }, library);

    expect(transitioned.stage).toBe("reviewing_materials");
    expect(await library.listAuditEvents(session.id)).toContainEqual(expect.objectContaining({
      actorId: "designer_01",
      reason: "customer agreed to review materials",
      before: { stage: "awaiting_material_consent" },
      after: { stage: "reviewing_materials" },
    }));
  });

  it("leaves at most one shooting session after two stores attempt shooting concurrently", async () => {
    const path = join(testDirectory, "concurrent-library.json");
    const seed = await JsonContentStore.open(path);
    const first = await startCreativeSession(sessionInput(), interpreter(completeDraft()), seed);
    const second = await startCreativeSession({ ...sessionInput(), title: "A second story" }, interpreter(completeDraft()), seed);
    await moveToReadyToDispatch(first.session.id, seed);
    await moveToReadyToDispatch(second.session.id, seed);
    const firstStore = await JsonContentStore.open(path);
    const secondStore = await JsonContentStore.open(path);

    await Promise.allSettled([
      transitionCreativeSession({
        sessionId: first.session.id, to: "shooting", actorId: "designer_01",
        reason: "begin first capture", now: "2026-09-04T08:04:00.000Z",
      }, firstStore),
      transitionCreativeSession({
        sessionId: second.session.id, to: "shooting", actorId: "designer_01",
        reason: "begin second capture", now: "2026-09-04T08:04:00.000Z",
      }, secondStore),
    ]);

    const reopened = await JsonContentStore.open(path);
    expect((await reopened.listCreativeSessions()).filter(
      (session) => session.stage === "shooting" && session.status !== "invalidated",
    )).toHaveLength(1);
  });

  async function moveToShooting(sessionId: string): Promise<void> {
    await moveToReadyToDispatch(sessionId, library);
    await transitionCreativeSession({
      sessionId,
      to: "shooting",
      actorId: "designer_01",
      reason: "begin capture",
      now: "2026-09-04T08:04:00.000Z",
    }, library);
  }

  async function moveToReadyToDispatch(
    sessionId: string,
    targetLibrary: JsonContentStore = library,
  ): Promise<void> {
    for (const [to, now] of [
      ["reviewing_materials", "2026-09-04T08:01:00.000Z"],
      ["awaiting_script_confirmation", "2026-09-04T08:02:00.000Z"],
      ["ready_to_dispatch", "2026-09-04T08:03:00.000Z"],
    ] as const) {
      await transitionCreativeSession({
        sessionId,
        to,
        actorId: "designer_01",
        reason: `move to ${to}`,
        now,
      }, targetLibrary);
    }
  }
});

function sessionInput() {
  return {
    customerId: "customer_01",
    title: "Pendant origin story",
    text: "I want to tell the story of this pendant.",
    actorId: "designer_01",
    now: "2026-09-04T08:00:00.000Z",
  };
}

function interpreter(draft: Record<string, unknown>) {
  return {
    interpret: async () => draft,
  };
}

function recoverableInput(sessionId: string, suffix: string) {
  return {
    sessionId, text: "The bench is available.", answers: { shootConditions: "bench" },
    actorId: "designer_01", customerId: "customer_01",
    clientInstanceId: `creative_client_${suffix}`,
    route: `/api/creative-sessions/${sessionId}/intent-answers`,
    bodyDigest: `body-digest-${suffix}`, now: "2026-09-04T08:01:00.000Z",
    operationKey: `answer-${suffix}`,
  } as const;
}

function testHistoricalSegment(id: string): ContentSegment {
  return {
    id, assetId: `asset_${id}`, batchId: `history_batch_${id}`, startMs: 0, endMs: 10_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["scene"], people: [], products: [], processStages: [], topics: [],
      emotions: [], transcript: "", technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}

function testParentAsset(segment: ContentSegment, patch: Partial<LibraryAsset> = {}): LibraryAsset {
  return {
    id: segment.assetId, batchId: segment.batchId, sessionId: "historical_session",
    digest: `digest_${segment.id}_${patch.id ?? segment.assetId}`,
    localPath: `/library/${segment.id}.mp4`, mediaKind: "video",
    importedAt: "2026-09-03T08:00:00.000Z", permissionState: "allowed",
    origin: "historical", technicalFlags: [], ...patch,
  };
}

type InvalidParentCase = "blocked" | "missing" | "current-batch" | "mismatched-batch";

async function seedInvalidParentCase(
  store: JsonContentStore,
  segment: ContentSegment,
  parentCase: InvalidParentCase,
): Promise<void> {
  if (parentCase !== "missing") {
    await store.putAsset(testParentAsset(segment, parentCase === "blocked"
      ? { permissionState: "blocked" }
      : parentCase === "current-batch"
        ? { origin: "new" }
        : { batchId: `${segment.batchId}_other` }));
  }
  await store.putSegments([segment]);
}

async function reparentSegment(
  store: JsonContentStore,
  segment: ContentSegment,
  parentCase: InvalidParentCase,
): Promise<void> {
  if (parentCase === "mismatched-batch") {
    await store.putSegments([{ ...segment, batchId: `${segment.batchId}_other` }]);
    return;
  }
  const replacement = { ...segment, assetId: `${segment.assetId}_${parentCase}` };
  if (parentCase !== "missing") {
    await store.putAsset(testParentAsset(replacement, parentCase === "blocked"
      ? { permissionState: "blocked" }
      : { origin: "new" }));
  }
  await store.putSegments([replacement]);
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
    products: [{ id: "product_01", status: "suggested" }],
  };
}
