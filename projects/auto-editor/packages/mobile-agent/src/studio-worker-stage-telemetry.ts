const STAGES = [
  "runtime-preaccept",
  "frontstage-extraction",
  "model-load",
  "processor-prepare",
  "model-generation",
  "output-validation",
] as const;

export type StudioWorkerStageNameV1 = (typeof STAGES)[number];

export type StudioWorkerStageTelemetrySummaryV1 = Readonly<{
  schemaVersion: 1;
  lastStage: StudioWorkerStageNameV1 | null;
  completedStageMs: Readonly<Partial<Record<StudioWorkerStageNameV1, number>>>;
  activeStageElapsedMs: number | null;
}>;

type TrackerInput = Readonly<{
  nowMs: () => number;
  maxBytes: number;
}>;

type TelemetryEvent = Readonly<{
  schemaVersion: 1;
  sequence: number;
  stage: StudioWorkerStageNameV1;
  state: "started" | "completed";
}>;

const MAX_LINE_BYTES = 1_024;

export function createStudioWorkerStageTelemetryTracker(input: TrackerInput) {
  if (!Number.isSafeInteger(input.maxBytes) || input.maxBytes <= 0) {
    throw new Error("telemetry-invalid");
  }
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let totalBytes = 0;
  let buffered = "";
  let failed = false;
  let completed = false;
  let expectedSequence = 1;
  let nextStageIndex = 0;
  let active: { stage: StudioWorkerStageNameV1; startedAtMs: number } | null = null;
  let lastStage: StudioWorkerStageNameV1 | null = null;
  const completedStageMs: Partial<Record<StudioWorkerStageNameV1, number>> = {};

  const fail = (code: "telemetry-invalid" | "telemetry-overflow"): never => {
    failed = true;
    buffered = "";
    throw new Error(code);
  };

  const timestamp = (): number => {
    let value: number;
    try {
      value = input.nowMs();
    } catch {
      return fail("telemetry-invalid");
    }
    if (!Number.isFinite(value) || value < 0 || value > Number.MAX_SAFE_INTEGER) {
      return fail("telemetry-invalid");
    }
    return Math.trunc(value);
  };

  const parseLine = (line: string): void => {
    if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) fail("telemetry-overflow");
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      return fail("telemetry-invalid");
    }
    if (
      value === null
      || typeof value !== "object"
      || Array.isArray(value)
      || Object.keys(value).join("\0") !== "schemaVersion\0sequence\0stage\0state"
    ) fail("telemetry-invalid");
    const event = value as Record<string, unknown>;
    if (
      event.schemaVersion !== 1
      || event.sequence !== expectedSequence
      || typeof event.stage !== "string"
      || !STAGES.includes(event.stage as StudioWorkerStageNameV1)
      || (event.state !== "started" && event.state !== "completed")
      || JSON.stringify(value) !== line
    ) fail("telemetry-invalid");

    const typed = event as TelemetryEvent;
    const expectedStage = STAGES[nextStageIndex];
    if (typed.state === "started") {
      if (active !== null || typed.stage !== expectedStage) fail("telemetry-invalid");
      active = { stage: typed.stage, startedAtMs: timestamp() };
      lastStage = typed.stage;
    } else {
      const activeStage = active;
      if (activeStage === null || typed.stage !== activeStage.stage || typed.stage !== expectedStage) {
        fail("telemetry-invalid");
      }
      const startedAtMs = activeStage!.startedAtMs;
      completedStageMs[typed.stage] = Math.max(0, timestamp() - startedAtMs);
      active = null;
      nextStageIndex += 1;
    }
    expectedSequence += 1;
  };

  const consume = (decoded: string): void => {
    buffered += decoded;
    while (true) {
      const newline = buffered.indexOf("\n");
      if (newline < 0) break;
      const current = buffered.slice(0, newline);
      buffered = buffered.slice(newline + 1);
      if (!current || current.endsWith("\r")) fail("telemetry-invalid");
      parseLine(current);
    }
    if (Buffer.byteLength(buffered, "utf8") > MAX_LINE_BYTES) fail("telemetry-overflow");
  };

  const finishDecoder = (): void => {
    try {
      consume(decoder.decode());
    } catch (error) {
      if (error instanceof Error && /^telemetry-(?:invalid|overflow)$/.test(error.message)) {
        throw error;
      }
      fail("telemetry-invalid");
    }
    if (buffered.length !== 0) fail("telemetry-invalid");
  };

  return Object.freeze({
    push(chunk: Uint8Array): void {
      if (failed || completed) fail("telemetry-invalid");
      totalBytes += chunk.byteLength;
      if (!Number.isSafeInteger(totalBytes) || totalBytes > input.maxBytes) {
        fail("telemetry-overflow");
      }
      try {
        consume(decoder.decode(chunk, { stream: true }));
      } catch (error) {
        if (error instanceof Error && /^telemetry-(?:invalid|overflow)$/.test(error.message)) {
          throw error;
        }
        fail("telemetry-invalid");
      }
    },
    complete(): void {
      if (failed || completed) fail("telemetry-invalid");
      finishDecoder();
      if (active !== null || nextStageIndex !== STAGES.length || expectedSequence !== 13) {
        fail("telemetry-invalid");
      }
      completed = true;
    },
    completeTerminated(nowMs: number): StudioWorkerStageTelemetrySummaryV1 {
      if (failed || completed) fail("telemetry-invalid");
      finishDecoder();
      const summary = this.snapshot(nowMs);
      completed = true;
      return summary;
    },
    snapshot(nowMs?: number): StudioWorkerStageTelemetrySummaryV1 {
      if (failed) fail("telemetry-invalid");
      let activeStageElapsedMs: number | null = null;
      if (active !== null) {
        const now = nowMs === undefined ? timestamp() : nowMs;
        if (!Number.isFinite(now) || now < 0 || now > Number.MAX_SAFE_INTEGER) {
          fail("telemetry-invalid");
        }
        activeStageElapsedMs = Math.max(0, Math.trunc(now) - active.startedAtMs);
      }
      return Object.freeze({
        schemaVersion: 1 as const,
        lastStage,
        completedStageMs: Object.freeze({ ...completedStageMs }),
        activeStageElapsedMs,
      });
    },
  });
}
