import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ModelAnalysisReceiptV1 } from "@wearable/contracts";

import {
  deriveEvidenceCards,
  JsonContentStore,
  reviewEvidenceCard,
} from "../src";

const sha256 = (value: string) => createHash("sha256").update(value).digest("hex");
const reviewedAt = "2026-09-23T02:00:00.000Z";

describe("persona evidence derivation", () => {
  it("creates cards only from source-bound transcript spans and allowlisted visual kinds", () => {
    const result = deriveEvidenceCards([
      transcriptReceipt(),
      visionReceipt({
        visualObservations: [
          {
            kind: "visible-product",
            text: "画面中央是一枚戒指",
            startMs: 2_000,
            endMs: 3_000,
            confidence: 0.88,
            status: "pending-human-confirmation",
          },
          {
            kind: "visible-person",
            text: "设计师看起来很焦虑",
            startMs: 3_000,
            endMs: 4_000,
            confidence: 0.71,
            status: "pending-human-confirmation",
          },
        ],
        pendingObservations: [{
          kind: "ownership",
          value: "这枚戒指属于受访者",
          status: "pending-human-confirmation",
        }],
      }),
    ]);

    expect(result.cards).toHaveLength(2);
    expect(result.cards).toEqual(expect.arrayContaining([
      expect.objectContaining({
        kind: "transcript-quote",
        value: "我希望它保留手工的温度",
        sourceStartMs: 1_100,
        sourceEndMs: 1_900,
        reviewStatus: "pending",
      }),
      expect.objectContaining({
        kind: "visible-product",
        value: "画面中央是一枚戒指",
        sourceStartMs: 2_000,
        sourceEndMs: 3_000,
        reviewStatus: "pending",
      }),
    ]));
    expect(result.rejectedSuggestions).toEqual(expect.arrayContaining([
      expect.objectContaining({
        value: "设计师看起来很焦虑",
        rejectionCode: "unsupported-emotion-claim",
        status: "rejected",
      }),
      expect.objectContaining({
        value: "这枚戒指属于受访者",
        rejectionCode: "unsupported-ownership-claim",
        status: "rejected",
      }),
    ]));
  });

  it("does not invent quote cards for successful no-speech receipts", () => {
    const result = deriveEvidenceCards([
      transcriptReceipt({
        speechState: "no-speech",
        qualitySignals: {
          detectedLanguage: "zh",
          noSpeechProbability: 0.99,
          spans: [],
        },
      }),
    ]);

    expect(result.cards).toEqual([]);
    expect(result.rejectedSuggestions).toEqual([]);
  });

  it("rejects untyped visual model text instead of exposing it for confirmation", () => {
    const result = deriveEvidenceCards([
      visionReceipt({
        visualObservations: [{
          text: "一名人物站在工作台旁",
          startMs: 2_000,
          endMs: 3_000,
          confidence: 0.8,
          status: "pending-human-confirmation",
        }],
      }),
    ]);

    expect(result.cards).toEqual([]);
    expect(result.rejectedSuggestions).toEqual([
      expect.objectContaining({
        value: "一名人物站在工作台旁",
        rejectionCode: "unsupported-untyped-visual-observation",
      }),
    ]);
  });

  it("never upgrades a legacy v1 visual observation into confirmable evidence", () => {
    const result = deriveEvidenceCards([
      visionReceipt({
        visualObservations: [{
          kind: "visible-product",
          text: "旧协议里没有受约束的类型字段",
          startMs: 2_000,
          endMs: 3_000,
          confidence: 0.8,
          status: "pending-human-confirmation",
        }],
      }, "studio-qwen3-vl-zh-json-v1"),
    ]);

    expect(result.cards).toEqual([]);
    expect(result.rejectedSuggestions).toEqual([
      expect.objectContaining({
        value: "旧协议里没有受约束的类型字段",
        rejectionCode: "unsupported-untyped-visual-observation",
      }),
    ]);
  });
});

describe("persona evidence persistence and review", () => {
  let directory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "persona-evidence-"));
    library = await JsonContentStore.open(join(directory, "library.json"));
    const derivation = deriveEvidenceCards([
      transcriptReceipt(),
      visionReceipt({
        visualObservations: [{
          kind: "visible-product",
          text: "画面中央是一枚戒指",
          startMs: 2_000,
          endMs: 3_000,
          confidence: 0.88,
          status: "pending-human-confirmation",
        }],
      }),
    ]);
    await library.commitEvidenceDerivation(derivation);
  });

  afterEach(async () => {
    await rm(directory, { recursive: true, force: true });
  });

  it("atomically persists one decision, updated card and audit event and replays idempotently", async () => {
    const card = (await library.listEvidenceCards("session-a"))
      .find((value) => value.kind === "transcript-quote")!;
    const input = reviewInput(card, {
      operationKey: "accept-transcript-once",
      action: "accepted",
    });

    const first = await reviewEvidenceCard(input, library);
    const replay = await reviewEvidenceCard(input, library);

    expect(replay).toEqual(first);
    expect(await library.listEvidenceDecisions("session-a")).toEqual([first.decision]);
    expect(await library.listAuditEvents(card.id)).toEqual([
      expect.objectContaining({
        entityType: "evidence-card",
        entityId: card.id,
        action: "evidence-accepted",
        before: expect.objectContaining({ reviewStatus: "pending" }),
        after: expect.objectContaining({ reviewStatus: "accepted" }),
      }),
    ]);
    expect(await library.listEvidenceCards("session-a")).toContainEqual(first.card);
  });

  it("persists terminal rejected suggestions across restart without exposing them as cards", async () => {
    const derivation = deriveEvidenceCards([
      visionReceipt({
        pendingObservations: [{
          kind: "ownership",
          value: "这件产品属于受访者",
          status: "pending-human-confirmation",
        }],
      }),
    ]);
    await library.commitEvidenceDerivation(derivation);

    const reopened = await JsonContentStore.open(join(directory, "library.json"));
    expect(await reopened.listRejectedEvidenceSuggestions("session-a")).toEqual([
      expect.objectContaining({
        value: "这件产品属于受访者",
        rejectionCode: "unsupported-ownership-claim",
        status: "rejected",
      }),
    ]);
    expect(await reopened.listEvidenceCards("session-a")).not.toContainEqual(
      expect.objectContaining({ value: "这件产品属于受访者" }),
    );
  });

  it("rejects reuse of an operation key with a different decision body", async () => {
    const card = (await library.listEvidenceCards("session-a"))[0]!;
    const input = reviewInput(card, {
      operationKey: "review-operation-collision",
      action: "accepted",
    });
    await reviewEvidenceCard(input, library);

    await expect(reviewEvidenceCard({
      ...input,
      action: "rejected",
      correctionReason: "画面不能支持这句话",
    }, library)).rejects.toThrow("evidence-review-operation-conflict");
    expect(await library.listEvidenceDecisions("session-a")).toHaveLength(1);
    expect(await library.listAuditEvents(card.id)).toHaveLength(1);
  });

  it("fails a replay closed when its supposedly atomic audit link was tampered", async () => {
    const card = (await library.listEvidenceCards("session-a"))[0]!;
    const input = reviewInput(card, {
      operationKey: "tampered-audit-replay",
      action: "accepted",
    });
    await reviewEvidenceCard(input, library);
    const path = join(directory, "library.json");
    const snapshot = JSON.parse(await readFile(path, "utf8")) as {
      auditEvents: Array<{ action: string }>;
    };
    snapshot.auditEvents[0]!.action = "unrelated-action";
    await writeFile(path, `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");

    const reopened = await JsonContentStore.open(path);
    await expect(reviewEvidenceCard(input, reopened))
      .rejects.toThrow("evidence-review-atomic-state-corrupt");
  });

  it("rejects cross-customer review before writing any decision or audit event", async () => {
    const card = (await library.listEvidenceCards("session-a"))[0]!;
    await expect(reviewEvidenceCard({
      ...reviewInput(card, {
        operationKey: "cross-customer-review",
        action: "accepted",
      }),
      customerId: "customer-b",
    }, library)).rejects.toThrow("evidence-card-scope-mismatch");

    expect(await library.listEvidenceDecisions("session-a")).toEqual([]);
    expect(await library.listAuditEvents(card.id)).toEqual([]);
  });

  it.each([
    ["source digest", { sourceDigest: sha256("forged-source") }, "evidence-source-binding-mismatch"],
    ["model receipt", { modelReceiptId: "forged-receipt" }, "evidence-receipt-binding-mismatch"],
  ])("rejects a forged %s binding before persistence", async (_label, override, error) => {
    const card = (await library.listEvidenceCards("session-a"))[0]!;
    await expect(reviewEvidenceCard({
      ...reviewInput(card, {
        operationKey: `forged-${String(_label).replace(" ", "-")}`,
        action: "accepted",
      }),
      ...override,
    }, library)).rejects.toThrow(error);
    expect(await library.listEvidenceDecisions("session-a")).toEqual([]);
    expect(await library.listAuditEvents(card.id)).toEqual([]);
  });

  it("never revives a rejected evidence card through a later accept operation", async () => {
    const card = (await library.listEvidenceCards("session-a"))[0]!;
    await reviewEvidenceCard(reviewInput(card, {
      operationKey: "reject-card",
      action: "rejected",
      correctionReason: "原话无法支持这个表达",
    }), library);

    const rejected = (await library.listEvidenceCards("session-a"))
      .find((value) => value.id === card.id)!;
    await expect(reviewEvidenceCard(reviewInput(rejected, {
      operationKey: "revive-card",
      action: "accepted",
    }), library)).rejects.toThrow("rejected-evidence-cannot-be-reactivated");
    expect(await library.listEvidenceDecisions("session-a")).toHaveLength(1);
    expect(await library.listAuditEvents(card.id)).toHaveLength(1);
  });
});

function reviewInput(
  card: {
    id: string;
    customerId: string;
    creativeSessionId: string;
    sourceDigest: string;
    modelReceiptId: string;
  },
  decision: {
    operationKey: string;
    action: "accepted" | "corrected" | "rejected";
    correctedValue?: string | null;
    correctionReason?: string | null;
  },
) {
  return {
    cardId: card.id,
    customerId: card.customerId,
    creativeSessionId: card.creativeSessionId,
    sourceDigest: card.sourceDigest,
    modelReceiptId: card.modelReceiptId,
    operationKey: decision.operationKey,
    action: decision.action,
    correctedValue: decision.correctedValue ?? null,
    correctionReason: decision.correctionReason ?? null,
    reviewerId: "designer-a",
    reviewedAt,
  };
}

function transcriptReceipt(overrides: Record<string, unknown> = {}): ModelAnalysisReceiptV1 {
  return modelReceipt({
    layer: "transcript",
    jobId: "transcript-receipt-a",
    operationKey: "transcript-run-a",
    adapterName: "whisper.cpp-1.9.2-cli-darwin-arm64",
    modelName: "whisper.cpp-1.9.2-small-q5_1",
    modelVersion: "1.9.2",
    checkpointDigest: "c".repeat(64),
    promptVersion: "studio-transcript-zh-v1",
    qualitySignals: {
      detectedLanguage: "zh",
      noSpeechProbability: 0.03,
      spans: [{
        startMs: 1_100,
        endMs: 1_900,
        text: "我希望它保留手工的温度",
        noSpeechProbability: 0.03,
      }],
    },
    observations: ["audio-stream:present", "transcript-span-count:1"],
    speechState: "speech",
    ...overrides,
  });
}

function visionReceipt(
  qualitySignals: Record<string, unknown>,
  promptVersion = "studio-qwen3-vl-zh-json-v2",
): ModelAnalysisReceiptV1 {
  return modelReceipt({
    layer: "vision",
    jobId: "vision-receipt-a",
    operationKey: "vision-run-a",
    adapterName: "mlx-vlm-0.6.6",
    modelName: "qwen3-vl-2b-instruct-4bit",
    modelVersion: "qwen3-vl-2b-instruct-4bit",
    checkpointDigest: "d".repeat(64),
    promptVersion,
    qualitySignals: {
      frameCount: 3,
      usable: true,
      pendingObservations: [],
      ...qualitySignals,
    },
    observations: [],
    suggestedTags: ["jewelry"],
  });
}

function modelReceipt(overrides: Record<string, unknown>): ModelAnalysisReceiptV1 {
  const receipt = {
    schemaVersion: 1 as const,
    jobId: "receipt-a",
    customerId: "customer-a",
    sessionId: "session-a",
    sourceAssetId: "asset-a",
    sourceDigest: "a".repeat(64),
    sourcePath: "/authorized/customer-a/asset-a.mp4",
    sourceStartMs: 1_000,
    sourceEndMs: 4_000,
    layer: "vision" as const,
    operationKey: "analysis-run-a",
    adapterName: "local-adapter",
    modelName: "local-model",
    modelVersion: "1.0.0",
    checkpointDigest: "c".repeat(64),
    inputDigest: "e".repeat(64),
    promptVersion: "fixture-v1",
    observations: [],
    suggestedTags: [],
    qualitySignals: {},
    confidence: 0.9,
    warnings: ["model-observation-requires-human-review"],
    startedAt: "2026-09-23T01:00:00.000Z",
    completedAt: "2026-09-23T01:00:01.000Z",
    runtimeMs: 1_000,
    status: "succeeded" as const,
    failure: null,
    synthetic: false,
    providerMode: "local" as const,
    ...overrides,
  };
  return {
    ...receipt,
    inputBinding: {
      sourceDigest: receipt.sourceDigest,
      sourceStartMs: receipt.sourceStartMs,
      sourceEndMs: receipt.sourceEndMs,
    },
    artifactBinding: {
      adapter: {
        id: receipt.adapterName,
        version: "1.0.0",
        sha256: "f".repeat(64),
      },
      model: {
        id: receipt.modelName,
        version: receipt.modelVersion,
        sha256: receipt.checkpointDigest,
      },
    },
    runtimeMetrics: { runtimeMs: receipt.runtimeMs },
  } as ModelAnalysisReceiptV1;
}
