import { describe, expect, it } from "vitest";

import {
  PinnedWorkerRuntimeDiagnosticV1Schema,
  buildPinnedWorkerRuntimeDiagnostic,
  canonicalPinnedWorkerRuntimeDiagnostic,
} from "../src/pinned-worker-runtime-diagnostic";


const digest = (marker: string) => marker.repeat(64);

const validInput = () => ({
  schemaVersion: 1 as const,
  status: "timeout" as const,
  safeFailureCategory: "model-timeout" as const,
  runtimeId: "worker-runtime-bundle-v1",
  runtimeTreeSha256: digest("a"),
  modelId: "qwen3-vl-2b-instruct-4bit",
  modelSha256: digest("b"),
  promptVersion: "studio-qwen3-vl-zh-json-v5",
  inputSha256: "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a",
  syntheticInput: true as const,
  customerContentUsed: false as const,
  totalElapsedMs: 330_000,
  lastStage: "model-generation" as const,
  completedStageMs: {
    "runtime-preaccept": 1_000,
    "frontstage-extraction": 2_000,
    "model-load": 30_000,
    "processor-prepare": 4_000,
  },
  activeStageElapsedMs: 293_000,
});

describe("pinned worker runtime diagnostic", () => {
  it("accepts only the fixed sanitized timeout fields and freezes the result", () => {
    const result = buildPinnedWorkerRuntimeDiagnostic(validInput());

    expect(result).toEqual(validInput());
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.completedStageMs)).toBe(true);
    expect(PinnedWorkerRuntimeDiagnosticV1Schema.parse(result)).toEqual(result);
    const canonical = canonicalPinnedWorkerRuntimeDiagnostic(result);
    expect(JSON.parse(canonical)).toEqual(result);
    expect(canonical).toBe(canonicalPinnedWorkerRuntimeDiagnostic({
      ...validInput(),
      completedStageMs: {
        "processor-prepare": 4_000,
        "model-load": 30_000,
        "frontstage-extraction": 2_000,
        "runtime-preaccept": 1_000,
      },
    }));
  });

  it("rejects unknown keys paths prompts outputs and exception text", () => {
    for (const [field, value] of [
      ["sourcePath", "/Users/test-user/private.mov"],
      ["prompt", "请分析客户素材"],
      ["modelOutput", "raw private output"],
      ["exception", "Traceback at /private/model.py"],
      ["stderr", "secret"],
      ["customerId", "customer-a"],
    ]) {
      expect(() => buildPinnedWorkerRuntimeDiagnostic({
        ...validInput(),
        [field]: value,
      })).toThrow(/^pinned-worker-runtime-diagnostic-invalid$/);
    }
  });

  it("accepts only fixed structured-output failure details on schema failures", () => {
    const details = [
      "top-level-object-keys",
      "suggested-tag-shape-content-duplication",
      "visual-observation-shape-kind",
      "visual-observation-interval",
      "demand-evidence-without-trusted-demand",
      "demand-evidence-shape-trusted-text",
      "warning-uncertainty-text-arrays",
      "scalar-confidence-optional-claim-shape",
    ] as const;
    for (const detail of details) {
      expect(buildPinnedWorkerRuntimeDiagnostic({
        ...validInput(),
        status: "failed",
        safeFailureCategory: "structured-output-schema",
        structuredOutputFailureDetail: detail,
      })).toMatchObject({ structuredOutputFailureDetail: detail });
    }
    for (const input of [
      {
        ...validInput(),
        status: "failed",
        safeFailureCategory: "structured-output-schema",
        structuredOutputFailureDetail: "private-path-/Users/customer.mov",
      },
      {
        ...validInput(),
        structuredOutputFailureDetail: "visual-observation-interval",
      },
    ]) {
      expect(() => buildPinnedWorkerRuntimeDiagnostic(input))
        .toThrow(/^pinned-worker-runtime-diagnostic-invalid$/);
    }
  });

  it("rejects invalid bindings stages flags and timing values", () => {
    const mutations: Array<(value: Record<string, any>) => void> = [
      (value) => { value.runtimeTreeSha256 = "x"; },
      (value) => { value.modelSha256 = "x"; },
      (value) => { value.promptVersion = "raw prompt with spaces"; },
      (value) => { value.inputSha256 = digest("c"); },
      (value) => { value.syntheticInput = false; },
      (value) => { value.customerContentUsed = true; },
      (value) => { value.lastStage = "unknown-stage"; },
      (value) => { value.totalElapsedMs = Number.POSITIVE_INFINITY; },
      (value) => { value.activeStageElapsedMs = -1; },
      (value) => { value.completedStageMs["model-generation"] = 330_001; },
      (value) => { value.lastStage = "model-load"; value.completedStageMs["processor-prepare"] = 1; },
      (value) => { value.lastStage = "processor-prepare"; delete value.completedStageMs["model-load"]; },
    ];
    for (const mutate of mutations) {
      const value = structuredClone(validInput()) as Record<string, any>;
      mutate(value);
      expect(() => buildPinnedWorkerRuntimeDiagnostic(value))
        .toThrow(/^pinned-worker-runtime-diagnostic-invalid$/);
    }
  });

  it("rejects explicitly undefined completed-stage values before canonicalization", () => {
    const value = validInput() as ReturnType<typeof validInput> & {
      completedStageMs: Record<string, number | undefined>;
    };
    value.completedStageMs["model-generation"] = undefined;
    expect(() => canonicalPinnedWorkerRuntimeDiagnostic(value))
      .toThrow(/^pinned-worker-runtime-diagnostic-invalid$/);
  });
});
