import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  assertReceiptMatchesJob,
  ModelAnalysisJobV1Schema,
  ModelAnalysisReceiptV1Schema,
  STUDIO_MODEL_ANALYSIS_LIMITS,
  StudioAnalysisStatusSchema,
  StudioModelArtifactRegistryV1Schema,
  StudioModelReceiptArtifactBindingV1Schema,
  StudioModelStateV1Schema,
} from "../src";

const QUALITY_SIGNAL_TOTAL_NODES = 4096;
const QUALITY_SIGNAL_TOTAL_STRING_BYTES = 65_536;
const FIXTURE_COMMIT = "a".repeat(40);
const OTHER_FIXTURE_COMMIT = "b".repeat(40);

const validJob = (overrides: Record<string, unknown> = {}) => ({
  schemaVersion: 1 as const,
  jobId: "job-1",
  customerId: "customer-a",
  sessionId: "session-1",
  sourceAssetId: "asset-a",
  sourceDigest: "a".repeat(64),
  sourcePath: "/authorized-media/customer-a/asset-a.mp4",
  sourceStartMs: 1_000,
  sourceEndMs: 4_000,
  layer: "vision" as const,
  operationKey: "analyze-asset-a-vision-v1",
  requestedAt: "2026-09-12T02:00:00.000Z",
  ...overrides,
});

const validReceipt = (overrides: Record<string, unknown> = {}) => ({
  schemaVersion: 1 as const,
  jobId: "job-1",
  customerId: "customer-a",
  sessionId: "session-1",
  sourceAssetId: "asset-a",
  sourceDigest: "a".repeat(64),
  sourcePath: "/authorized-media/customer-a/asset-a.mp4",
  sourceStartMs: 1_000,
  sourceEndMs: 4_000,
  layer: "vision" as const,
  operationKey: "analyze-asset-a-vision-v1",
  adapterName: "fixture-vision-adapter",
  modelName: "fixture-qwen",
  modelVersion: "2b-fixture-v1",
  checkpointDigest: "c".repeat(64),
  inputDigest: "d".repeat(64),
  promptVersion: "studio-vision-zh-v1",
  observations: ["possibly a jewelry close-up"],
  suggestedTags: ["single-product"],
  qualitySignals: { frameCount: 3, usable: true },
  confidence: 0.72,
  warnings: ["model-observation-requires-human-review"],
  startedAt: "2026-09-12T02:00:01.000Z",
  completedAt: "2026-09-12T02:00:01.120Z",
  runtimeMs: 120,
  status: "succeeded" as const,
  failure: null,
  synthetic: true,
  providerMode: "local" as const,
  ...overrides,
});

describe("studio model analysis contracts", () => {
  it("binds the adapter identity to a runtime bundle without free-form runtime data", () => {
    const binding = {
      adapter: {
        id: "mlx-vlm-worker-runtime",
        version: "0.6.6-py312",
        sha256: "e".repeat(64),
      },
      model: {
        id: "qwen3-vl-2b-instruct-4bit",
        version: "2b-4bit",
        sha256: "c".repeat(64),
      },
    };
    expect(StudioModelReceiptArtifactBindingV1Schema.parse(binding).adapter).toEqual({
      id: "mlx-vlm-worker-runtime",
      version: "0.6.6-py312",
      sha256: "e".repeat(64),
    });
    expect(() => StudioModelReceiptArtifactBindingV1Schema.parse({
      ...binding,
      adapter: { ...binding.adapter, root: "/private/runtime" },
    })).toThrow();
  });

  it("accepts one strict versioned job and source-bound receipt", () => {
    expect(ModelAnalysisJobV1Schema.parse(validJob())).toMatchObject({
      schemaVersion: 1,
      customerId: "customer-a",
      layer: "vision",
    });
    expect(ModelAnalysisReceiptV1Schema.parse(validReceipt())).toMatchObject({
      schemaVersion: 1,
      status: "succeeded",
      providerMode: "local",
      synthetic: true,
    });
    expect(() => assertReceiptMatchesJob(validJob(), validReceipt())).not.toThrow();
  });

  it("requires exact input, artifact, runtime, and speech bindings for real transcript receipts", () => {
    const receipt = {
      ...validReceipt({
        layer: "transcript",
        promptVersion: "studio-transcript-zh-v1",
        synthetic: false,
      }),
      inputBinding: {
        sourceDigest: "a".repeat(64),
        sourceStartMs: 1_000,
        sourceEndMs: 4_000,
      },
      artifactBinding: {
        adapter: { id: "whisper-cli", version: "1.9.2", sha256: "e".repeat(64) },
        model: { id: "fixture-qwen", version: "2b-fixture-v1", sha256: "c".repeat(64) },
      },
      runtimeMetrics: { runtimeMs: 120 },
      speechState: "speech",
    };
    expect(() => ModelAnalysisReceiptV1Schema.parse(receipt)).not.toThrow();

    for (const key of ["inputBinding", "artifactBinding", "runtimeMetrics", "speechState"] as const) {
      const invalid = { ...receipt };
      delete invalid[key];
      expect(() => ModelAnalysisReceiptV1Schema.parse(invalid)).toThrow();
    }
    expect(() => ModelAnalysisReceiptV1Schema.parse({
      ...receipt,
      inputBinding: { ...receipt.inputBinding, sourceDigest: "b".repeat(64) },
    })).toThrow("analysis-input-binding-mismatch");
    expect(() => ModelAnalysisReceiptV1Schema.parse({
      ...receipt,
      runtimeMetrics: { runtimeMs: 121 },
    })).toThrow("analysis-runtime-binding-mismatch");
  });

  it("requires exact bindings for the typed visual-evidence prompt version", () => {
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      promptVersion: "studio-qwen3-vl-zh-json-v2",
      layer: "vision",
      synthetic: false,
    }))).toThrow("analysis-input-binding-mismatch");
  });

  it.each([
    ["jobId", "job-2"],
    ["customerId", "customer-b"],
    ["sessionId", "session-2"],
    ["sourceAssetId", "asset-b"],
    ["sourceDigest", "b".repeat(64)],
    ["sourcePath", "/authorized-media/customer-a/asset-b.mp4"],
    ["sourceStartMs", 1_001],
    ["sourceEndMs", 4_001],
    ["layer", "transcript"],
    ["operationKey", "another-operation"],
  ])("rejects a receipt whose %s binding differs from its job", (field, value) => {
    expect(() => assertReceiptMatchesJob(
      validJob(),
      validReceipt({ [field]: value }),
    )).toThrow("studio-model-receipt-binding-mismatch");
  });

  it("requires local model identity and checkpoint digest", () => {
    expect(() => StudioModelStateV1Schema.parse({
      mode: "local",
      installed: true,
      modelName: "qwen",
      checkpointDigest: null,
    })).toThrow();
    expect(() => StudioModelStateV1Schema.parse({
      mode: "local",
      installed: true,
      modelName: "qwen",
      checkpointDigest: "e".repeat(64),
    })).not.toThrow();
  });

  it("rejects confirmed facts from a model receipt", () => {
    expect(() => ModelAnalysisReceiptV1Schema.parse({
      ...validReceipt(),
      confirmedFacts: ["18K gold"],
    })).toThrow();
  });

  it("rejects unknown fields and invalid receipt bounds", () => {
    expect(() => ModelAnalysisJobV1Schema.parse({ ...validJob(), browserCustomerId: "customer-b" })).toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse({ ...validReceipt(), secretOutput: "raw transcript" })).toThrow();
    expect(() => ModelAnalysisJobV1Schema.parse(validJob({ sourceEndMs: 1_000 }))).toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ confidence: 1.01 }))).toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ completedAt: "2026-09-12T01:59:59.000Z" }))).toThrow();
  });

  it("rejects a non-terminal job status as an analysis receipt", () => {
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ status: "running" }))).toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ status: "cancelled" }))).toThrow();
    expect(StudioAnalysisStatusSchema.parse("cancelled")).toBe("cancelled");
  });

  it.each(["failed-retryable", "failed-manual-review"] as const)(
    "requires an auditable failure reason for %s receipts",
    (status) => {
      expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ status, failure: null }))).toThrow(
        "failed-analysis-receipt-requires-reason",
      );
      expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
        status,
        failure: { code: "model-output-invalid", message: "worker returned invalid JSON" },
      }))).not.toThrow();
    },
  );

  it("forbids an opaque or contradictory success failure", () => {
    const missingFailure = validReceipt();
    delete (missingFailure as { failure?: unknown }).failure;
    expect(() => ModelAnalysisReceiptV1Schema.parse(missingFailure)).toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      failure: { code: "unexpected", message: "success cannot carry failure" },
    }))).toThrow("succeeded-analysis-receipt-must-not-have-failure");
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      status: "failed-retryable",
      failure: { code: "worker-timeout", rawOutput: "secret" },
    }))).toThrow();
  });

  it("accepts model-controlled strings and arrays at their exact limits", () => {
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      adapterName: "a".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity),
      modelName: "m".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity),
      modelVersion: "v".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity),
      promptVersion: "p".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity),
      observations: Array.from(
        { length: STUDIO_MODEL_ANALYSIS_LIMITS.observations },
        () => "o".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.observation),
      ),
      suggestedTags: Array.from(
        { length: STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTags },
        () => "t".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTag),
      ),
      warnings: Array.from(
        { length: STUDIO_MODEL_ANALYSIS_LIMITS.warnings },
        () => "w".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.warning),
      ),
    }))).not.toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      status: "failed-retryable",
      failure: {
        code: "c".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.failureCode),
        message: "m".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.failureMessage),
      },
    }))).not.toThrow();
  });

  it("rejects model-controlled strings and arrays beyond their exact limits", () => {
    for (const receipt of [
      validReceipt({ adapterName: "a".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity + 1) }),
      validReceipt({ modelName: "m".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity + 1) }),
      validReceipt({ modelVersion: "v".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity + 1) }),
      validReceipt({ promptVersion: "p".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.identity + 1) }),
      validReceipt({ observations: ["o".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.observation + 1)] }),
      validReceipt({ observations: Array(STUDIO_MODEL_ANALYSIS_LIMITS.observations + 1).fill("o") }),
      validReceipt({ suggestedTags: ["t".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTag + 1)] }),
      validReceipt({ suggestedTags: Array(STUDIO_MODEL_ANALYSIS_LIMITS.suggestedTags + 1).fill("t") }),
      validReceipt({ warnings: ["w".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.warning + 1)] }),
      validReceipt({ warnings: Array(STUDIO_MODEL_ANALYSIS_LIMITS.warnings + 1).fill("w") }),
      validReceipt({
        status: "failed-retryable",
        failure: { code: "c".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.failureCode + 1) },
      }),
      validReceipt({
        status: "failed-retryable",
        failure: {
          code: "failure",
          message: "m".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.failureMessage + 1),
        },
      }),
    ]) expect(() => ModelAnalysisReceiptV1Schema.parse(receipt)).toThrow();
  });

  it("bounds nested JSON quality signals at the exact limits", () => {
    const qualitySignalsAtLimit: Record<string, unknown> = Object.fromEntries(Array.from(
      { length: STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalObjectKeys },
      (_, index) => [`key-${index}`, index],
    ));
    qualitySignalsAtLimit["k".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalKey)] = "s".repeat(
      STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString,
    );
    delete qualitySignalsAtLimit["key-0"];
    qualitySignalsAtLimit.values = Array(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems).fill(0);
    delete qualitySignalsAtLimit["key-1"];
    qualitySignalsAtLimit.nested = nestedQualitySignal(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalDepth - 1);
    delete qualitySignalsAtLimit["key-2"];
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: qualitySignalsAtLimit,
    }))).not.toThrow();

    for (const qualitySignals of [
      { ["k".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalKey + 1)]: true },
      { text: "s".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString + 1) },
      { values: Array(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems + 1).fill(0) },
      Object.fromEntries(Array.from(
        { length: STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalObjectKeys + 1 },
        (_, index) => [`key-${index}`, index],
      )),
      { nested: nestedQualitySignal(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalDepth) },
      { invalidNumber: Number.POSITIVE_INFINITY },
      { invalidValue: () => "not-json" },
    ]) expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ qualitySignals }))).toThrow();
  });

  it("accepts quality signals at the aggregate node and string-byte budgets", () => {
    expect(STUDIO_MODEL_ANALYSIS_LIMITS?.qualitySignalTotalNodes).toBe(QUALITY_SIGNAL_TOTAL_NODES);
    expect(STUDIO_MODEL_ANALYSIS_LIMITS?.qualitySignalTotalStringBytes).toBe(QUALITY_SIGNAL_TOTAL_STRING_BYTES);
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: {
        first: Array(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems).fill(0),
        second: Array(
          QUALITY_SIGNAL_TOTAL_NODES
          - STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems
          - 3,
        ).fill(0),
      },
    }))).not.toThrow();
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: {
        values: [
          ...Array(16).fill("s".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString)),
          "s".repeat(
            QUALITY_SIGNAL_TOTAL_STRING_BYTES
            - 16 * STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString
            - 6,
          ),
        ],
      },
    }))).not.toThrow();
  });

  it("rejects locally valid quality signals over an aggregate budget", () => {
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: {
        first: Array(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems).fill(0),
        second: Array(
          QUALITY_SIGNAL_TOTAL_NODES
          - STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalArrayItems
          - 2,
        ).fill(0),
      },
    }))).toThrow("studio-model-quality-signal-total-node-limit");
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: {
        values: [
          ...Array(16).fill("s".repeat(STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString)),
          "s".repeat(
            QUALITY_SIGNAL_TOTAL_STRING_BYTES
            - 16 * STUDIO_MODEL_ANALYSIS_LIMITS.qualitySignalString
            - 6
            + 1,
          ),
        ],
      },
    }))).toThrow("studio-model-quality-signal-total-string-byte-limit");
  });

  it("counts multibyte object keys at the exact aggregate UTF-8 byte boundary", () => {
    const qualitySignals = multibyteKeySignalsAtBudget();
    expect(utf8KeyBytes(qualitySignals)).toBe(QUALITY_SIGNAL_TOTAL_STRING_BYTES);
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ qualitySignals }))).not.toThrow();

    const oneByteOver = { ...qualitySignals, y: null };
    expect(utf8KeyBytes(oneByteOver)).toBe(QUALITY_SIGNAL_TOTAL_STRING_BYTES + 1);
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({
      qualitySignals: oneByteOver,
    }))).toThrow("studio-model-quality-signal-total-string-byte-limit");
  });

  it("combines object-key and string-value UTF-8 bytes in one aggregate budget", () => {
    const qualitySignals: Record<string, unknown> = Object.fromEntries(Array.from(
      { length: 200 },
      (_, index) => [String.fromCodePoint(0x4e00 + index).repeat(100), null],
    ));
    const firstKey = Object.keys(qualitySignals)[0]!;
    qualitySignals[firstKey] = "😀".repeat(1_384);
    expect(utf8KeyBytes(qualitySignals)).toBe(60_000);
    expect(new TextEncoder().encode(qualitySignals[firstKey] as string).byteLength).toBe(5_536);
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ qualitySignals }))).not.toThrow();

    qualitySignals[firstKey] = `${qualitySignals[firstKey]}x`;
    expect(() => ModelAnalysisReceiptV1Schema.parse(validReceipt({ qualitySignals }))).toThrow(
      "studio-model-quality-signal-total-string-byte-limit",
    );
  });

  it("parses the approved local persona bundle while keeping unrelated models fail-closed", () => {
    const registry = StudioModelArtifactRegistryV1Schema.parse(JSON.parse(
      readFileSync(new URL("../../../models.lock.json", import.meta.url), "utf8"),
    ));

    expect(registry.artifacts.map((artifact) => artifact.id)).toEqual([
      "pyscenedetect-0.7.1",
      "whisper.cpp-1.9.2-small-q5_0",
      "whisper.cpp-1.9.2-small-q5_1",
      "whisper.cpp-1.9.2-cli-darwin-arm64",
      "openclip-3.3.0-vit-b-32",
      "mlx-vlm-0.6.6",
      "qwen3-vl-2b-instruct-4bit",
    ]);
    const approved = registry.artifacts.filter((artifact) => artifact.approved);
    expect(approved.map((artifact) => artifact.id)).toEqual([
      "whisper.cpp-1.9.2-small-q5_1",
      "whisper.cpp-1.9.2-cli-darwin-arm64",
      "mlx-vlm-0.6.6",
      "qwen3-vl-2b-instruct-4bit",
    ]);
    for (const artifact of approved) {
      expect(artifact).toMatchObject({
        enabled: true,
        licenseReview: "approved",
        approvedBy: "wumuze",
        testSetVersion: "local-persona-model-probe-v1",
      });
    }
    const pending = registry.artifacts.filter((artifact) => !artifact.approved);
    for (const artifact of pending) {
      expect(artifact).toMatchObject({
        enabled: false,
        approved: false,
        licenseReview: "pending",
      });
      expect(artifact.licenseEvidence).toBeDefined();
      expect(artifact.sources).toBeDefined();
      expect(artifact.licenseEvidence.code).toMatchObject({
        sourceId: null,
        sourceRevision: null,
        spdxExpression: null,
        evidenceUrl: null,
      });
      expect(artifact.licenseEvidence.weights).toMatchObject({
        sourceId: null,
        sourceRevision: null,
        spdxExpression: null,
        evidenceUrl: null,
      });
      for (const source of artifact.sources) {
        expect(source.sourceUrl).toMatch(/^https:\/\//);
        expect(source.sourceRevision).toBeNull();
        expect(source.inventoryVerified).toBe(false);
        expect(source.files.length).toBeGreaterThan(0);
        for (const file of source.files) {
          expect(file).toMatchObject({
            required: true,
            sourceUrl: null,
            sha256: null,
            byteLength: null,
          });
          expect(typeof file.executable).toBe("boolean");
          expect(file.relativePath.length).toBeGreaterThan(0);
        }
      }
    }
    const qwen = registry.artifacts.find((artifact) => artifact.id === "qwen3-vl-2b-instruct-4bit");
    const qwenFiles = qwen?.sources.flatMap((source) => source.files) ?? [];
    expect(qwenFiles.length).toBeGreaterThan(1);
    expect(qwenFiles.some((file) => file.relativePath.endsWith(".safetensors"))).toBe(true);
    expect(qwen?.runtimeFiles?.length).toBe(13);
    expect(qwen?.runtimeFiles?.some(
      (file) => file.relativePath.endsWith("/model.safetensors.index.json"),
    )).toBe(true);
    expect(qwen?.derivationReceipt).toMatchObject({
      status: "succeeded",
      syntheticInput: true,
      realModel: true,
      probeReportSha256: "526426a52cb29da3609fc959eca641e0a5b91641effdefec2ed78c65d4ec0a3b",
    });
    expect(registry.workerRuntimes).toHaveLength(2);
    expect(registry.workerRuntimes?.filter((runtime) => runtime.enabled)).toEqual([
      expect.objectContaining({
        id: "mlx-vlm-worker-runtime-0.6.6-pyscenedetect-0.7.1-py312-darwin-arm64",
        workerDependencyLockSha256: "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041",
        materialInventorySha256: "505b5f9ad6b9d009bea1c5f4ab8d5b7b5fb6513277a11985d114fcbd2669730f",
        approvalPacketId: "worker-runtime-bundle-v1-sha256-7b37714f6dedaf9d6cf50e1a50fbe4806cad683bd1764297ae7f74e0f8c86cbc",
        treeSha256: "83e3bf8f96824226f3fd4a65320313472f2e5fb29e39bedb6861ffe45e05fd9f",
        probe: {
          receiptPath: "runtime/probes/mlx-vlm-worker-runtime-0.6.6-pyscenedetect-0.7.1-py312-darwin-arm64.json",
          receiptSha256: "604c008d03f008821cfd174fcb87aa1be3088dc125e8d9dfc42db32b675b146c",
          runtimeTreeSha256: "83e3bf8f96824226f3fd4a65320313472f2e5fb29e39bedb6861ffe45e05fd9f",
          modelSha256: "fcab0d97620da9e03cc12a6fc07e6a390ca34aef13c1d7519236096dfd0b839b",
          promptVersion: "studio-qwen3-vl-zh-json-v5",
          inputSha256: "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a",
        },
      }),
    ]);
  });

  it("accepts approval only with commit-SHA revisions and root-bound file URLs", () => {
    const registry = reviewRegistry();
    registry.artifacts[0] = approvedArtifact(registry.artifacts[0]);
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).not.toThrow();

    registry.artifacts[0].sources[0].files[0].sourceUrl =
      `https://github.com/example/code/archive/${FIXTURE_COMMIT}.tar.gz`;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).not.toThrow();

    registry.artifacts[0].sources[0].files[0].sha256 = null;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "approved-model-artifact-requires-complete-file-manifest",
    );
  });

  it("requires every manifest file to declare a strict executable policy", () => {
    const missing = reviewRegistry();
    delete missing.artifacts[0].sources[0].files[0].executable;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(missing)).toThrow();

    for (const executable of [null, 0, "false"]) {
      const invalid = reviewRegistry();
      invalid.artifacts[0].sources[0].files[0].executable = executable;
      expect(() => StudioModelArtifactRegistryV1Schema.parse(invalid)).toThrow();
    }
  });

  it("rejects unapproved enablement and incomplete approval workflow states", () => {
    const registry = reviewRegistry();
    registry.artifacts[0].enabled = true;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "enabled-model-artifact-must-be-approved",
    );

    for (const overrides of [
      { licenseReview: "pending" },
      { licenseReview: "rejected" },
      { approvedBy: null },
      { approvedAt: null },
    ]) {
      const candidate = reviewRegistry();
      candidate.artifacts[0] = approvedArtifact(candidate.artifacts[0], overrides);
      expect(() => StudioModelArtifactRegistryV1Schema.parse(candidate)).toThrow();
    }
  });

  it("rejects duplicate artifact IDs and duplicate required file paths", () => {
    const duplicateArtifact = reviewRegistry();
    duplicateArtifact.artifacts.push(structuredClone(duplicateArtifact.artifacts[0]));
    expect(() => StudioModelArtifactRegistryV1Schema.parse(duplicateArtifact)).toThrow(
      "duplicate-model-artifact-id",
    );

    const duplicateFile = reviewRegistry();
    duplicateFile.artifacts[0].sources[0].files.push(structuredClone(
      duplicateFile.artifacts[0].sources[0].files[0],
    ));
    expect(() => StudioModelArtifactRegistryV1Schema.parse(duplicateFile)).toThrow(
      "duplicate-model-artifact-file-path",
    );

    const crossSourceDuplicate = reviewRegistry();
    crossSourceDuplicate.artifacts[1].sources[1].files[0].relativePath =
      crossSourceDuplicate.artifacts[1].sources[0].files[0].relativePath;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(crossSourceDuplicate)).toThrow(
      "duplicate-model-artifact-file-path",
    );
  });

  it("rejects two different sources with the same code or weights kind", () => {
    const duplicateCodeKind = reviewRegistry();
    duplicateCodeKind.artifacts[0] = approvedArtifact(duplicateCodeKind.artifacts[0]);
    duplicateCodeKind.artifacts[0].sources.push(verifiedSource({
      ...structuredClone(duplicateCodeKind.artifacts[0].sources[0]),
      id: "code-secondary",
      sourceUrl: "https://github.com/example/code-secondary",
      files: [{
        relativePath: "packages/code-secondary.tar.gz",
        sourceUrl: null,
        required: true,
        executable: false,
        sha256: null,
        byteLength: null,
      }],
    }));
    expect(() => StudioModelArtifactRegistryV1Schema.parse(duplicateCodeKind)).toThrow(
      "duplicate-model-artifact-source-kind",
    );

    const duplicateWeightsKind = reviewRegistry();
    duplicateWeightsKind.artifacts[2] = approvedArtifact(duplicateWeightsKind.artifacts[2]);
    duplicateWeightsKind.artifacts[2].sources.push(verifiedSource({
      ...structuredClone(duplicateWeightsKind.artifacts[2].sources[0]),
      id: "weights-secondary",
      sourceUrl: "https://huggingface.co/example/weights-secondary",
      files: [{
        relativePath: "qwen/model-secondary.safetensors",
        sourceUrl: null,
        required: true,
        executable: false,
        sha256: null,
        byteLength: null,
      }],
    }));
    expect(() => StudioModelArtifactRegistryV1Schema.parse(duplicateWeightsKind)).toThrow(
      "duplicate-model-artifact-source-kind",
    );
  });

  it("rejects tags, floating branches, uppercase, and short hashes as source revisions", () => {
    for (const sourceRevision of [
      "HEAD",
      "develop",
      "refs/heads/release",
      "main",
      "v1.2.3",
      "A".repeat(40),
      "a".repeat(39),
      { kind: "release-tag", value: "v1.2.3" },
    ]) {
      const registry = reviewRegistry();
      registry.artifacts[0].sources[0].sourceRevision = sourceRevision;
      expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
        "immutable-model-artifact-source-revision-required",
      );
    }
  });

  it("rejects nested pseudo-bindings and floating, mismatched, or different-provider file URLs", () => {
    for (const sourceUrl of [
      "https://github.com/example/code/resolve/HEAD/packages/code.tar.gz",
      "https://github.com/example/code/resolve/develop/packages/code.tar.gz",
      "https://github.com/example/code/refs/heads/release/packages/code.tar.gz",
      "https://github.com/example/code/resolve/main/packages/code.tar.gz",
      `https://github.com/example/code/resolve/${OTHER_FIXTURE_COMMIT}/packages/code.tar.gz`,
      `https://github.com/example/code/blob/main/resolve/${FIXTURE_COMMIT}/packages/code.tar.gz`,
      `https://github.com/example/code/tree/main/raw/${FIXTURE_COMMIT}/packages/code.tar.gz`,
      `https://example.com/example/code/resolve/${FIXTURE_COMMIT}/packages/code.tar.gz`,
    ]) {
      const registry = reviewRegistry();
      registry.artifacts[0] = approvedArtifact(registry.artifacts[0]);
      registry.artifacts[0].sources[0].files[0].sourceUrl = sourceUrl;
      expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
        "model-artifact-file-source-revision-mismatch",
      );
    }
  });

  it("rejects a branch page disguised as the provider repository root", () => {
    const registry = reviewRegistry();
    registry.artifacts[0] = approvedArtifact(registry.artifacts[0]);
    const source = registry.artifacts[0].sources[0];
    source.sourceUrl = "https://github.com/example/code/blob/main";
    source.files[0].sourceUrl = `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/packages/code.tar.gz`;
    registry.artifacts[0].licenseEvidence.code.evidenceUrl =
      `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/LICENSE`;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "model-artifact-repository-root-source-required",
    );
  });

  it("rejects a reserved example domain disguised as a provider repository", () => {
    const registry = reviewRegistry();
    registry.artifacts[0] = approvedArtifact(registry.artifacts[0]);
    const source = registry.artifacts[0].sources[0];
    source.sourceUrl = "https://example.com/example/code";
    source.files[0].sourceUrl = `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/packages/code.tar.gz`;
    registry.artifacts[0].licenseEvidence.code.evidenceUrl =
      `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/LICENSE`;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "model-artifact-repository-root-source-required",
    );
  });

  it.each([
    ["code-only", 0],
    ["mixed", 1],
    ["weights-only", 2],
  ])("accepts complete revision-bound license evidence for %s artifacts", (_label, index) => {
    const registry = reviewRegistry();
    registry.artifacts[index] = approvedArtifact(registry.artifacts[index]);
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).not.toThrow();
  });

  it("allows todo as a provider owner when the revision-bound evidence suffix is a real license path", () => {
    const registry = reviewRegistry();
    registry.artifacts[0] = approvedArtifact(registry.artifacts[0]);
    const source = registry.artifacts[0].sources[0];
    source.sourceUrl = "https://github.com/todo/code";
    source.files[0].sourceUrl = `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/packages/code.tar.gz`;
    registry.artifacts[0].licenseEvidence.code.evidenceUrl =
      `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/LICENSE`;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).not.toThrow();
  });

  it("requires complete code and weight evidence as soon as license review is approved", () => {
    const registry = reviewRegistry();
    registry.artifacts[0].sources = registry.artifacts[0].sources.map(verifiedSource);
    registry.artifacts[0].licenseReview = "approved";
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "approved-license-review-requires-code-license-evidence",
    );

    const mixedIndex = 1;
    for (const [kind, field] of [
      ["code", "sourceId"],
      ["code", "sourceRevision"],
      ["code", "spdxExpression"],
      ["code", "evidenceUrl"],
      ["weights", "sourceId"],
      ["weights", "sourceRevision"],
      ["weights", "spdxExpression"],
      ["weights", "evidenceUrl"],
    ] as const) {
      const candidate = reviewRegistry();
      candidate.artifacts[mixedIndex] = reviewedArtifact(candidate.artifacts[mixedIndex]);
      candidate.artifacts[mixedIndex].licenseEvidence[kind][field] = null;
      expect(() => StudioModelArtifactRegistryV1Schema.parse(candidate)).toThrow(
        `approved-license-review-requires-${kind}-license-evidence`,
      );
    }
  });

  it("rejects placeholder, different-provider, and mismatched-revision license evidence", () => {
    const placeholder = reviewRegistry();
    placeholder.artifacts[0].licenseEvidence.code.spdxExpression = "review-required";
    expect(() => StudioModelArtifactRegistryV1Schema.parse(placeholder)).toThrow();

    for (const [field, value] of [
      ["sourceId", "TODO"],
      ["sourceRevision", OTHER_FIXTURE_COMMIT],
      ["evidenceUrl", `https://github.com/example/code/resolve/TODO/LICENSE`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/placeholder`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/%54%4f%44%4f`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/TODO-LICENSE`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/license-placeholder.txt`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/LICENSE?review=ToDo`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${FIXTURE_COMMIT}/LICENSE#PLACEHOLDER`],
      ["evidenceUrl", `https://github.com/example/code/resolve/${OTHER_FIXTURE_COMMIT}/LICENSE`],
      ["evidenceUrl", `https://example.com/example/code/resolve/${FIXTURE_COMMIT}/LICENSE`],
    ] as const) {
      const candidate = reviewRegistry();
      candidate.artifacts[0] = reviewedArtifact(candidate.artifacts[0]);
      candidate.artifacts[0].licenseEvidence.code[field] = value;
      expect(() => StudioModelArtifactRegistryV1Schema.parse(candidate)).toThrow(
        "code-license-evidence-source-revision-mismatch",
      );
    }
  });

  it("rejects an approved multi-file checkpoint with any unverified required file", () => {
    const registry = reviewRegistry();
    const qwenIndex = registry.artifacts.findIndex(
      (artifact: { id: string }) => artifact.id === "qwen3-vl-2b-instruct-4bit",
    );
    registry.artifacts[qwenIndex] = approvedArtifact(registry.artifacts[qwenIndex]);
    registry.artifacts[qwenIndex].sources[0].files.at(-1).byteLength = null;
    expect(() => StudioModelArtifactRegistryV1Schema.parse(registry)).toThrow(
      "approved-model-artifact-requires-complete-file-manifest",
    );
  });
});

const nestedQualitySignal = (depth: number): unknown => {
  let value: unknown = "leaf";
  for (let index = 0; index < depth; index += 1) value = { nested: value };
  return value;
};

const multibyteKeySignalsAtBudget = (): Record<string, null> => Object.fromEntries([
  ...Array.from(
    { length: 218 },
    (_, index) => [String.fromCodePoint(0x4e00 + index).repeat(100), null] as const,
  ),
  [String.fromCodePoint(0x4e00 + 218).repeat(45), null],
  ["x", null],
]);

const utf8KeyBytes = (value: Record<string, unknown>): number => Object.keys(value).reduce(
  (total, key) => total + new TextEncoder().encode(key).byteLength,
  0,
);

const lockedRegistry = (): any => JSON.parse(readFileSync(
  new URL("../../../models.lock.json", import.meta.url),
  "utf8",
));

const pendingArtifact = (
  id: string,
  codeApplies: boolean,
  weightsApply: boolean,
  filePaths = ["artifact.bin"],
) => ({
  id,
  version: "1.2.3",
  artifactKind: weightsApply ? "checkpoint" : "source-package",
  sources: [
    ...(codeApplies ? [{
      id: "code",
      kind: "code",
      sourceUrl: "https://github.com/example/code",
      sourceRevision: null,
      inventoryVerified: false,
      files: ["packages/code.tar.gz"].map((relativePath) => ({
        relativePath,
        sourceUrl: null,
        required: true,
        executable: false,
        sha256: null,
        byteLength: null,
      })),
    }] : []),
    ...(weightsApply ? [{
      id: "weights",
      kind: "weights",
      sourceUrl: "https://huggingface.co/example/weights",
      sourceRevision: null,
      inventoryVerified: false,
      files: filePaths.map((relativePath) => ({
        relativePath,
        sourceUrl: null,
        required: true,
        executable: false,
        sha256: null,
        byteLength: null,
      })),
    }] : []),
  ],
  licenseEvidence: {
    code: {
      applies: codeApplies,
      sourceId: null,
      sourceRevision: null,
      spdxExpression: null,
      evidenceUrl: null,
    },
    weights: {
      applies: weightsApply,
      sourceId: null,
      sourceRevision: null,
      spdxExpression: null,
      evidenceUrl: null,
    },
  },
  licenseReview: "pending",
  runtimeBackend: "fixture-runtime",
  quantization: weightsApply ? "fixture-quantization" : null,
  enabled: false,
  approved: false,
  approvedBy: null,
  approvedAt: null,
  testedDevices: [],
  testSetVersion: null,
});

const reviewRegistry = (): any => ({
  schemaVersion: 1,
  artifacts: [
    pendingArtifact("pyscenedetect-0.7.1", true, false),
    pendingArtifact("openclip-3.3.0-vit-b-32", true, true),
    pendingArtifact("qwen3-vl-2b-instruct-4bit", false, true, [
      "qwen/model.safetensors.index.json",
      "qwen/model.safetensors",
    ]),
  ],
});

const reviewedArtifact = (artifact: any): any => {
  const sources = artifact.sources.map(verifiedSource);
  return {
    ...artifact,
    sources,
    licenseReview: "approved",
    licenseEvidence: Object.fromEntries(Object.entries(artifact.licenseEvidence).map(
      ([kind, evidence]: [string, any]) => {
        if (!evidence.applies) return [kind, evidence];
        const source = sources.find((candidate: any) => candidate.kind === kind);
        return [kind, {
          ...evidence,
          sourceId: source.id,
          sourceRevision: source.sourceRevision,
          spdxExpression: "Apache-2.0",
          evidenceUrl: `${source.sourceUrl}/resolve/${source.sourceRevision}/LICENSE`,
        }];
      },
    )),
  };
};

const approvedArtifact = (artifact: any, overrides: Record<string, unknown> = {}) => ({
  ...reviewedArtifact(artifact),
  approved: true,
  approvedBy: "local-model-reviewer",
  approvedAt: "2026-09-12T03:00:00.000Z",
  ...overrides,
});

const verifiedSource = (source: any) => ({
  ...source,
  sourceRevision: FIXTURE_COMMIT,
  inventoryVerified: true,
  files: source.files.map((file: Record<string, unknown>) => ({
    ...file,
    sourceUrl: `${source.sourceUrl}/resolve/${FIXTURE_COMMIT}/${String(file.relativePath)}`,
    sha256: "f".repeat(64),
    byteLength: 1024,
  })),
});
