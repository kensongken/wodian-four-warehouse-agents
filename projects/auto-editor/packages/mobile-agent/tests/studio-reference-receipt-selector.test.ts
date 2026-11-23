import { createHash } from "node:crypto";

import type { ModelAnalysisReceiptV1 } from "@wearable/contracts";
import { describe, expect, it } from "vitest";

import {
  selectFinalReferenceAnalysisAttempt,
  type StudioAnalysisHistoryEntry,
} from "../src";

const binding = {
  customerId: "customer-a",
  sessionId: "session-a",
  sourceAssetId: "asset-a",
  sourceDigest: "a".repeat(64),
  sourcePath: "/trusted/reference.mov",
  sourceStartMs: 0,
  sourceEndMs: 2_000,
};

describe("final reference analysis attempt selector", () => {
  it("keeps optional failures in audit receipts but excludes them from grammar evidence", () => {
    const finalReceipts = [
      receipt("scene", 2),
      receipt("transcript", 2, "failed-manual-review", { synthetic: true, providerMode: "offline-fallback" }),
      receipt("vision", 2, "failed-retryable", { synthetic: true, providerMode: "offline-fallback" }),
    ];
    const selected = selectFinalReferenceAnalysisAttempt(
      entry([receipt("scene", 1, "failed-retryable"), ...finalReceipts], 2),
      binding,
    );

    expect(selected.allReceipts).toEqual(finalReceipts);
    expect(selected.evidenceReceipts).toEqual([finalReceipts[0]]);
  });

  it("selects only the last attempt and preserves canonical layer order", () => {
    const previous = [receipt("scene", 1, "failed-retryable")];
    const finalReceipts = [receipt("scene", 2), receipt("transcript", 2), receipt("vision", 2)];

    const selected = selectFinalReferenceAnalysisAttempt(
      entry([...previous, ...finalReceipts], 2),
      binding,
    );

    expect(selected.allReceipts.map((row) => row.jobId)).toEqual(finalReceipts.map((row) => row.jobId));
    expect(selected.evidenceReceipts.map((row) => row.layer)).toEqual(["scene", "transcript", "vision"]);
  });

  it("rejects receipts from an older attempt masquerading as the final attempt", () => {
    expect(() => selectFinalReferenceAnalysisAttempt(
      entry([receipt("scene", 1), receipt("transcript", 1), receipt("vision", 1)], 2),
      binding,
    )).toThrow("reference-analysis-final-attempt-layer-invalid");
  });

  it.each([
    ["ordinary authorization", { sourceAuthorization: "output-use-authorized" as const }],
    ["reordered layers", {
      layers: ["scene", "vision", "transcript"] as StudioAnalysisHistoryEntry["job"]["layers"],
    }],
    ["missing layer", {
      layers: ["scene", "transcript"] as StudioAnalysisHistoryEntry["job"]["layers"],
    }],
  ])("rejects a noncanonical parent with %s", (_name, override) => {
    expect(() => selectFinalReferenceAnalysisAttempt(
      entry([receipt("scene", 1), receipt("transcript", 1), receipt("vision", 1)], 1, override),
      binding,
    )).toThrow("reference-analysis-final-attempt-layer-invalid");
  });

  it("rejects a failed required scene receipt", () => {
    expect(() => selectFinalReferenceAnalysisAttempt(
      entry([
        receipt("scene", 1, "failed-retryable"),
        receipt("transcript", 1),
        receipt("vision", 1),
      ], 1),
      binding,
    )).toThrow("reference-analysis-scene-not-succeeded");
  });

  it.each(["scene", "transcript", "vision"] as const)(
    "rejects successful %s evidence that is not real local output",
    (layer) => {
      const receipts = [receipt("scene", 1), receipt("transcript", 1), receipt("vision", 1)]
        .map((row) => row.layer === layer
          ? { ...row, synthetic: true, providerMode: "offline-fallback" as const }
          : row);
      expect(() => selectFinalReferenceAnalysisAttempt(entry(receipts, 1), binding))
        .toThrow("reference-receipt-real-local-evidence-required");
    },
  );

  it.each([
    ["customer", { customerId: "customer-b" }],
    ["session", { sessionId: "session-b" }],
    ["asset", { sourceAssetId: "asset-b" }],
    ["digest", { sourceDigest: "b".repeat(64) }],
    ["path", { sourcePath: "/other/reference.mov" }],
    ["start", { sourceStartMs: 1 }],
    ["end", { sourceEndMs: 1_999 }],
  ])("rejects a final attempt with a conflicting %s binding", (_name, override) => {
    const receipts = [receipt("scene", 1), receipt("transcript", 1), receipt("vision", 1)];
    receipts[1] = { ...receipts[1]!, ...override };
    expect(() => selectFinalReferenceAnalysisAttempt(entry(receipts, 1), binding))
      .toThrow("reference-receipt-binding-mismatch");
  });
});

function entry(
  receipts: ModelAnalysisReceiptV1[],
  attempt: number,
  override: Partial<StudioAnalysisHistoryEntry["job"]> = {},
): StudioAnalysisHistoryEntry {
  return {
    job: {
      schemaVersion: 1,
      id: "parent-job",
      actorId: "actor-a",
      ...binding,
      sourceDurationMs: 2_000,
      sourceAssetSessionId: "session-a",
      sourceAuthorization: "reference-view-authorized",
      layers: ["scene", "transcript", "vision"],
      operationKey: "reference-analysis",
      requestDigest: "b".repeat(64),
      status: "succeeded",
      attempt,
      receiptJobIds: receipts.map((row) => row.jobId),
      runnerInstanceId: null,
      failure: null,
      cancellationReason: null,
      createdAt: "2026-09-20T00:00:00.000Z",
      updatedAt: "2026-09-20T00:00:01.000Z",
      ...override,
    },
    receipts,
  };
}

function receipt(
  layer: ModelAnalysisReceiptV1["layer"],
  attempt: number,
  status: ModelAnalysisReceiptV1["status"] = "succeeded",
  override: Partial<ModelAnalysisReceiptV1> = {},
): ModelAnalysisReceiptV1 {
  const layerIndex = ["scene", "transcript", "vision"].indexOf(layer);
  const jobId = `model_${createHash("sha256")
    .update(`parent-job\0${attempt}\0${layerIndex}\0${layer}`)
    .digest("hex")}`;
  return {
    schemaVersion: 1,
    jobId,
    ...binding,
    layer,
    operationKey: "reference-analysis",
    adapterName: `adapter-${layer}`,
    modelName: `model-${layer}`,
    modelVersion: "v1",
    checkpointDigest: null,
    inputDigest: createHash("sha256").update(`${attempt}-${layer}`).digest("hex"),
    promptVersion: "v1",
    observations: [],
    suggestedTags: [],
    qualitySignals: {},
    confidence: status === "succeeded" ? 0.9 : 0,
    warnings: [],
    startedAt: "2026-09-20T00:00:00.000Z",
    completedAt: "2026-09-20T00:00:01.000Z",
    runtimeMs: 1,
    status,
    failure: status === "succeeded" ? null : { code: "failed" },
    synthetic: false,
    providerMode: "local",
    ...override,
  };
}
