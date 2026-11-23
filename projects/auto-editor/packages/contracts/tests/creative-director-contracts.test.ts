import { describe, expect, it } from "vitest";

import {
  AuditEventSchema,
  createCreativeFieldEvent,
  creativeFieldEventIdempotencyKey,
  CreativeIntentSchema,
  CreativeProcessingTaskSchema,
  HistoricalMaterialAuthorizationSchema,
  CreativeSessionEventSchema,
  CreativeSessionSchema,
  DirectorCardSchema,
  LibraryAssetSchema,
  MaterialUseDecisionSchema,
  legacyCreativeFieldEventIdempotencyKey,
  ScriptPackageSchema,
  ShotPlanSchema,
} from "../src";

describe("creative director contracts", () => {
  it("does not accept reviewable without persisted output checkpoint or a false publication boundary", () => {
    const task = { id: "task", creativeSessionId: "creative_01", editorialBatchId: "batch_01",
      status: "reviewable", phase: "complete", errorCode: null, updatedAt: "2026-09-07T08:00:00.000Z" };
    expect(CreativeProcessingTaskSchema.safeParse(task).success).toBe(false);
    expect(CreativeProcessingTaskSchema.safeParse({ ...task, publishedClaimed: true }).success).toBe(false);
  });

  it("requires exact historical identity and does not accept duplicated selected IDs", () => {
    expect(HistoricalMaterialAuthorizationSchema.safeParse({ selectedSegmentIds: ["history"] }).success).toBe(false);
    const authorization = { creativeSessionId: "creative_01", suggestionSetId: "set_01", suggestionSetVersion: 1,
      decisionId: "decision_01", selectedSegmentIds: ["history"] };
    expect(HistoricalMaterialAuthorizationSchema.parse(authorization)).toEqual(authorization);
    expect(HistoricalMaterialAuthorizationSchema.safeParse({ ...authorization, selectedSegmentIds: ["history", "history"] }).success).toBe(false);
  });
  it("persists a refresh-safe creative processing task", () => {
    expect(CreativeProcessingTaskSchema.parse({
      id: "editorial_task_01", creativeSessionId: "creative_01", editorialBatchId: "batch_01",
      status: "ready-for-editorial", phase: "capture", errorCode: null,
      updatedAt: "2026-09-07T08:00:00.000Z",
    })).toMatchObject({ status: "ready-for-editorial", phase: "capture" });
  });
  it("persists an incomplete intent so the customer can answer it in the same session", () => {
    expect(CreativeIntentSchema.parse({
      id: "intent_incomplete", sessionId: "session_incomplete", version: 1,
      platforms: [], audience: "Unspecified", theme: "想拍一条视频", products: [],
      shootConditions: [], prohibitedAreas: [], tone: "Unspecified", durationSeconds: 30,
      createdAt: "2026-09-06T08:00:00.000Z",
    }).platforms).toEqual([]);
  });
  it("rejects a material selection that was not shown to the customer", () => {
    expect(() =>
      MaterialUseDecisionSchema.parse({
        id: "decision_01",
        sessionId: "creative_01",
        suggestionSetId: "suggestions_01",
        suggestionVersion: 1,
        action: "confirm-selection",
        shownSegmentIds: ["segment_a"],
        selectedSegmentIds: ["segment_b"],
        rejectedSegmentIds: [],
        actorId: "designer_01",
        createdAt: "2026-09-04T08:00:00.000Z",
      }),
    ).toThrow("decision-segment-not-shown");
  });

  it("rejects duplicate segment IDs in every material decision list", () => {
    for (const [duplicateList, duplicateIds] of [
      ["shownSegmentIds", ["segment_a", "segment_a", "segment_b"]],
      ["selectedSegmentIds", ["segment_a", "segment_a"]],
      ["rejectedSegmentIds", ["segment_b", "segment_b"]],
    ] as const) {
      const decision = validMaterialUseDecision();
      decision[duplicateList] = [...duplicateIds];

      expect(() => MaterialUseDecisionSchema.parse(decision)).toThrow(
        "duplicate-decision-segment-id",
      );
    }
  });

  it("rejects a segment that is both selected and rejected", () => {
    const decision = validMaterialUseDecision();
    decision.selectedSegmentIds = ["segment_a"];
    decision.rejectedSegmentIds = ["segment_a"];

    expect(() => MaterialUseDecisionSchema.parse(decision)).toThrow(
      "selected-and-rejected-segment-overlap",
    );
  });

  it("requires every script section to identify its source", () => {
    expect(() => ScriptPackageSchema.parse(scriptWithoutSource())).toThrow();
  });

  it("requires the full session and version key on a director card", () => {
    expect(() =>
      DirectorCardSchema.parse(cardWithoutScriptVersion()),
    ).toThrow();
  });

  it("preserves explicit non-forcing capture gaps", () => {
    expect(ShotPlanSchema.parse(validShotPlan()).steps[0]?.optionalGapSuggestions)
      .toEqual([{ suggestion: "Try a close-up if time allows", required: false }]);
    expect(() => ShotPlanSchema.parse({
      ...validShotPlan(),
      steps: [{
        ...validShotPlan().steps[0],
        optionalGapSuggestions: [{ suggestion: "Retake this", required: true }],
      }],
    })).toThrow();
  });

  it("normalizes legacy string capture gaps while still rejecting required true", () => {
    expect(ShotPlanSchema.parse({
      ...validShotPlan(),
      steps: [{
        ...validShotPlan().steps[0],
        optionalGapSuggestions: ["Try a close-up if time allows"],
      }],
    }).steps[0]?.optionalGapSuggestions).toEqual([
      { suggestion: "Try a close-up if time allows", required: false },
    ]);
    expect(() => ShotPlanSchema.parse({
      ...validShotPlan(),
      steps: [{
        ...validShotPlan().steps[0],
        optionalGapSuggestions: [{ suggestion: "Retake this", required: true }],
      }],
    })).toThrow();
  });

  it("accepts a session waiting for material consent", () => {
    expect(CreativeSessionSchema.parse(validCreativeSession()).stage).toBe(
      "awaiting_material_consent",
    );
  });

  it("preserves optional capture provenance without requiring old assets to have it", () => {
    expect(LibraryAssetSchema.parse(libraryAssetWithCaptureContext()).captureContext)
      .toEqual({
        creativeSessionId: "creative_01",
        stepId: "step_01",
        deviceId: "device_watch_01",
        deviceRole: "watch-director",
        carrier: "wrist",
        scriptVersion: 1,
        shotPlanVersion: 1,
        take: 1,
        stepOutcome: "completed",
      });
  });

  it("records creative-session audit events alongside existing audit entities", () => {
    expect(
      AuditEventSchema.parse({
        id: "audit_01",
        entityType: "creative-session",
        entityId: "creative_01",
        actorId: "designer_01",
        action: "created",
        reason: null,
        before: null,
        after: { stage: "draft_intent" },
        createdAt: "2026-09-04T08:00:00.000Z",
      }).entityType,
    ).toBe("creative-session");
  });

  it("keeps a voice-rewrite transcript only on a voice-rewrite event", () => {
    expect(CreativeSessionEventSchema.parse(creativeEvent({
      action: "voice-rewrite",
      transcript: "Please make the opening calmer.",
    })).transcript).toBe("Please make the opening calmer.");
    expect(() => CreativeSessionEventSchema.parse(creativeEvent({
      action: "voice-rewrite",
      transcript: " ",
    }))).toThrow("voice-rewrite-transcript-required");
    expect(() => CreativeSessionEventSchema.parse(creativeEvent({
      action: "note",
      transcript: "This must not be retained here.",
    }))).toThrow("unexpected-action-transcript");
  });

  it("encodes creative field operation tuples without delimiter collisions", () => {
    const first = fieldAction({ sessionId: "a:b", stepId: "c", operationId: "d" });
    const collidingUnderColonJoin = fieldAction({
      sessionId: "a",
      stepId: "b:c",
      operationId: "d",
    });
    const exactReplay = fieldAction({ sessionId: "a:b", stepId: "c", operationId: "d" });
    const firstEvent = createCreativeFieldEvent(first, fieldActionContext());
    const otherEvent = createCreativeFieldEvent(collidingUnderColonJoin, fieldActionContext());
    const replayEvent = createCreativeFieldEvent(exactReplay, fieldActionContext());

    expect(creativeFieldEventIdempotencyKey(first))
      .toBe("creative-field-event:v1|3:a:b|1:c|1:d");
    expect(firstEvent.idempotencyKey).not.toBe(otherEvent.idempotencyKey);
    expect(firstEvent.id).not.toBe(otherEvent.id);
    expect(replayEvent.idempotencyKey).toBe(firstEvent.idempotencyKey);
    expect(replayEvent.id).toBe(firstEvent.id);
  });

  it("preserves ASCII and Unicode whitespace in a nonblank operation identity", () => {
    const plain = fieldAction({ sessionId: "creative_01", stepId: "step_01", operationId: "op-1" });
    const asciiSpaced = fieldAction({
      sessionId: "creative_01",
      stepId: "step_01",
      operationId: " op-1 ",
    });
    const unicodeSpaced = fieldAction({
      sessionId: "creative_01",
      stepId: "step_01",
      operationId: "\u2003op-1\u3000",
    });
    const plainEvent = createCreativeFieldEvent(plain, fieldActionContext());
    const asciiEvent = createCreativeFieldEvent(asciiSpaced, fieldActionContext());
    const unicodeEvent = createCreativeFieldEvent(unicodeSpaced, fieldActionContext());

    expect(plainEvent.idempotencyKey)
      .toBe("creative-field-event:v1|11:creative_01|7:step_01|4:op-1");
    expect(asciiEvent.idempotencyKey)
      .toBe("creative-field-event:v1|11:creative_01|7:step_01|6: op-1 ");
    expect(unicodeEvent.idempotencyKey)
      .toBe("creative-field-event:v1|11:creative_01|7:step_01|6:\u2003op-1\u3000");
    expect(new Set([plainEvent.id, asciiEvent.id, unicodeEvent.id])).toHaveProperty("size", 3);
    expect(legacyCreativeFieldEventIdempotencyKey(plain))
      .not.toBe(legacyCreativeFieldEventIdempotencyKey(asciiSpaced));
    expect(() => createCreativeFieldEvent(
      fieldAction({ sessionId: "creative_01", stepId: "step_01", operationId: "\u2003\u3000" }),
      fieldActionContext(),
    )).toThrow("device-action-operation-required");
  });
});

function fieldAction(patch: { sessionId: string; stepId: string; operationId: string }) {
  return {
    ...patch,
    briefVersion: 1,
    scriptVersion: 1,
    shotPlanVersion: 1,
    taskId: "card_01",
    action: "note" as const,
    actorId: "designer_01",
    deviceId: "watch_01",
    deviceRole: "watch-director" as const,
    reason: "Keep this light.",
    transcript: null,
    now: "2026-09-05T08:00:00.000Z",
  };
}

function fieldActionContext() {
  return {
    availableActions: ["note"] as const,
    currentTake: 1,
    privacyPaused: false,
  };
}

function creativeEvent(patch: Record<string, unknown>) {
  return {
    id: "event_01", sessionId: "creative_01", briefVersion: 1, scriptVersion: 1,
    shotPlanVersion: 1, stepId: "step_01", deviceId: "watch_01",
    deviceRole: "watch-director", action: "note", actorId: "designer_01",
    reason: null, idempotencyKey: "event_operation_01",
    createdAt: "2026-09-05T08:00:00.000Z", transcript: null,
    ...patch,
  };
}

function validCreativeSession() {
  return {
    id: "creative_01",
    customerId: "customer_01",
    title: "Make a pendant story",
    stage: "awaiting_material_consent",
    currentIntentVersion: 1,
    currentBriefVersion: 0,
    currentScriptVersion: 0,
    currentShotPlanVersion: 0,
    materialViewDecisionIds: [],
    materialUseDecisionIds: [],
    deviceBindings: [],
    captureBatchIds: [],
    editorialBatchId: null,
    privacyMode: "creator",
    prohibitedAreas: [],
    status: "active",
    createdAt: "2026-09-04T08:00:00.000Z",
    updatedAt: "2026-09-04T08:00:00.000Z",
  };
}

function validMaterialUseDecision() {
  return {
    id: "decision_01",
    sessionId: "creative_01",
    suggestionSetId: "suggestions_01",
    suggestionVersion: 1,
    action: "confirm-selection" as const,
    shownSegmentIds: ["segment_a", "segment_b"],
    selectedSegmentIds: ["segment_a"],
    rejectedSegmentIds: ["segment_b"],
    actorId: "designer_01",
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function scriptWithoutSource() {
  return {
    id: "script_01_v1",
    sessionId: "creative_01",
    briefVersion: 1,
    scriptVersion: 1,
    status: "active",
    title: "Make a pendant story",
    platformDirection: "Xiaohongshu",
    opening: "A maker starts again.",
    narrativeOrder: ["section_01"],
    sections: [
      {
        id: "section_01",
        expression: "Show the return to the bench.",
        referencedSegmentIds: [],
      },
    ],
    confirmedAt: null,
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function cardWithoutScriptVersion() {
  return {
    id: "card_01",
    sessionId: "creative_01",
    briefVersion: 1,
    shotPlanVersion: 1,
    stepId: "step_01",
    status: "active",
    purpose: "Show the pendant from above.",
    spokenPrompt: "Tell us why this detail matters.",
    shotDescription: "Top-down close-up.",
    durationSeconds: 15,
    mustCapture: ["pendant texture"],
    prohibitedCapture: [],
    availableActions: ["completed", "retake", "skipped"],
    createdAt: "2026-09-04T08:00:00.000Z",
  };
}

function validShotPlan() {
  return {
    id: "shot_plan_01",
    sessionId: "creative_01",
    briefVersion: 1,
    scriptVersion: 1,
    shotPlanVersion: 1,
    status: "active",
    steps: [{
      id: "step_01",
      order: 1,
      purpose: "Open the repair story",
      expressionSuggestion: "Show the maker at the bench.",
      visualDirection: "Wide bench shot.",
      durationSeconds: 10,
      mustCapture: ["maker hands"],
      optionalGapSuggestions: [{ suggestion: "Try a close-up if time allows", required: false }],
      prohibitedCapture: ["customer faces"],
      completionCriteria: ["maker hands visible"],
      deviceRoles: ["watch-director", "main-stone-a"],
    }],
    createdAt: "2026-09-05T08:00:00.000Z",
  };
}

function libraryAssetWithCaptureContext() {
  return {
    id: "asset_01",
    batchId: "batch_01",
    sessionId: "capture_01",
    digest: "digest_01",
    localPath: "/tmp/asset_01.mp4",
    mediaKind: "video",
    importedAt: "2026-09-04T08:00:00.000Z",
    permissionState: "allowed",
    origin: "new",
    technicalFlags: [],
    captureContext: {
      creativeSessionId: "creative_01",
      stepId: "step_01",
      deviceId: "device_watch_01",
      deviceRole: "watch-director",
      carrier: "wrist",
      scriptVersion: 1,
      shotPlanVersion: 1,
      take: 1,
      stepOutcome: "completed",
    },
  };
}
