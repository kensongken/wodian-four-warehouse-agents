import { z } from "zod";


const DIGEST = /^[a-f0-9]{64}$/u;
const SAFE_IDENTITY = /^[A-Za-z0-9._-]{1,200}$/u;
const MAX_ELAPSED_MS = 86_400_000;
const STAGES = [
  "runtime-preaccept",
  "frontstage-extraction",
  "model-load",
  "processor-prepare",
  "model-generation",
  "output-validation",
] as const;

const elapsed = z.number().int().min(0).max(MAX_ELAPSED_MS);
const completedStageMs = z.object({
  "runtime-preaccept": elapsed.optional(),
  "frontstage-extraction": elapsed.optional(),
  "model-load": elapsed.optional(),
  "processor-prepare": elapsed.optional(),
  "model-generation": elapsed.optional(),
  "output-validation": elapsed.optional(),
}).strict();
const structuredOutputFailureDetail = z.enum([
  "top-level-object-keys",
  "suggested-tag-shape-content-duplication",
  "visual-observation-shape-kind",
  "visual-observation-interval",
  "demand-evidence-without-trusted-demand",
  "demand-evidence-shape-trusted-text",
  "warning-uncertainty-text-arrays",
  "scalar-confidence-optional-claim-shape",
]);

export type StructuredOutputFailureDetail = z.infer<typeof structuredOutputFailureDetail>;

export const PinnedWorkerRuntimeDiagnosticV1Schema = z.object({
  schemaVersion: z.literal(1),
  status: z.enum(["timeout", "failed"]),
  safeFailureCategory: z.enum([
    "model-timeout",
    "runtime-integrity",
    "media-extraction",
    "model-execution-dependency",
    "model-execution-load",
    "model-execution-contract",
    "model-execution-generation",
    "structured-output-json",
    "structured-output-number",
    "structured-output-constraint",
    "structured-output-schema",
    "structured-output-size",
  ]),
  runtimeId: z.string().regex(SAFE_IDENTITY),
  runtimeTreeSha256: z.string().regex(DIGEST),
  modelId: z.literal("qwen3-vl-2b-instruct-4bit"),
  modelSha256: z.string().regex(DIGEST),
  promptVersion: z.literal("studio-qwen3-vl-zh-json-v5"),
  inputSha256: z.literal("1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a"),
  syntheticInput: z.literal(true),
  customerContentUsed: z.literal(false),
  totalElapsedMs: elapsed,
  lastStage: z.enum(STAGES),
  completedStageMs,
  activeStageElapsedMs: elapsed.nullable(),
  structuredOutputFailureDetail: structuredOutputFailureDetail.optional(),
}).strict().superRefine((value, context) => {
  for (const stage of STAGES) {
    if (
      Object.prototype.hasOwnProperty.call(value.completedStageMs, stage)
      && value.completedStageMs[stage] === undefined
    ) context.addIssue({ code: "custom", message: "undefined-stage-duration" });
  }
  const recorded = Object.values(value.completedStageMs).reduce<number>(
    (total, duration) => total + (duration ?? 0),
    value.activeStageElapsedMs ?? 0,
  );
  if (recorded > value.totalElapsedMs) {
    context.addIssue({ code: "custom", message: "elapsed-exceeds-total" });
  }
  if (value.status === "timeout" && value.safeFailureCategory !== "model-timeout") {
    context.addIssue({ code: "custom", message: "timeout-category-invalid" });
  }
  if (
    value.structuredOutputFailureDetail !== undefined
    && value.safeFailureCategory !== "structured-output-schema"
  ) {
    context.addIssue({ code: "custom", message: "structured-output-detail-category-invalid" });
  }
  const lastStageIndex = STAGES.indexOf(value.lastStage);
  const expectedCompleted = STAGES.slice(
    0,
    value.activeStageElapsedMs === null ? lastStageIndex + 1 : lastStageIndex,
  );
  const actualCompleted = Object.entries(value.completedStageMs)
    .filter(([, duration]) => duration !== undefined)
    .map(([stage]) => stage);
  if (
    actualCompleted.length !== expectedCompleted.length
    || actualCompleted.some((stage, index) => stage !== expectedCompleted[index])
  ) context.addIssue({ code: "custom", message: "stage-prefix-invalid" });
});

export type PinnedWorkerRuntimeDiagnosticV1 = Readonly<z.infer<
  typeof PinnedWorkerRuntimeDiagnosticV1Schema
>>;

export function buildPinnedWorkerRuntimeDiagnostic(
  input: unknown,
): PinnedWorkerRuntimeDiagnosticV1 {
  try {
    const parsed = PinnedWorkerRuntimeDiagnosticV1Schema.parse(input);
    return Object.freeze({
      ...parsed,
      completedStageMs: Object.freeze({ ...parsed.completedStageMs }),
    });
  } catch {
    throw new Error("pinned-worker-runtime-diagnostic-invalid");
  }
}

export function canonicalPinnedWorkerRuntimeDiagnostic(input: unknown): string {
  const diagnostic = buildPinnedWorkerRuntimeDiagnostic(input);
  return `${canonicalJson(diagnostic)}\n`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
