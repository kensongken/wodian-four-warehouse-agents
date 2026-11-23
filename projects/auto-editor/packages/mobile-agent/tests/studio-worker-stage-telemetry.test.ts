import { describe, expect, it } from "vitest";

import { createStudioWorkerStageTelemetryTracker } from "../src/studio-worker-stage-telemetry";


const line = (
  sequence: number,
  stage: string,
  state: "started" | "completed",
) => `${JSON.stringify({ schemaVersion: 1, sequence, stage, state })}\n`;

const fullStream = () => [
  "runtime-preaccept",
  "frontstage-extraction",
  "model-load",
  "processor-prepare",
  "model-generation",
  "output-validation",
].flatMap((stage, index) => [
  line(index * 2 + 1, stage, "started"),
  line(index * 2 + 2, stage, "completed"),
]).join("");

describe("studio worker stage telemetry", () => {
  it("reassembles canonical telemetry split across UTF-8 and line chunks", () => {
    const tracker = createStudioWorkerStageTelemetryTracker({
      nowMs: (() => {
        let value = 0;
        return () => value++;
      })(),
      maxBytes: 4_096,
    });
    const bytes = Buffer.from(fullStream());
    for (let index = 0; index < bytes.byteLength; index += 3) {
      tracker.push(bytes.subarray(index, index + 3));
    }
    tracker.complete();

    expect(tracker.snapshot()).toEqual({
      schemaVersion: 1,
      lastStage: "output-validation",
      completedStageMs: {
        "runtime-preaccept": 1,
        "frontstage-extraction": 1,
        "model-load": 1,
        "processor-prepare": 1,
        "model-generation": 1,
        "output-validation": 1,
      },
      activeStageElapsedMs: null,
    });
  });

  it("rejects unknown fields stages states duplicate skipped reversed and trailing events", () => {
    const invalidLines = [
      '{"schemaVersion":1,"sequence":1,"stage":"runtime-preaccept","state":"started","path":"/Users/private.mov"}\n',
      line(1, "unknown-stage", "started"),
      '{"schemaVersion":1,"sequence":1,"stage":"runtime-preaccept","state":"waiting"}\n',
      line(2, "runtime-preaccept", "started"),
      line(1, "frontstage-extraction", "started"),
      line(1, "runtime-preaccept", "completed"),
      `${fullStream()}${line(13, "output-validation", "completed")}`,
    ];

    for (const payload of invalidLines) {
      const tracker = createStudioWorkerStageTelemetryTracker({ nowMs: () => 0, maxBytes: 4_096 });
      expect(() => tracker.push(Buffer.from(payload))).toThrow(/^telemetry-invalid$/);
    }

    const incomplete = createStudioWorkerStageTelemetryTracker({ nowMs: () => 0, maxBytes: 4_096 });
    incomplete.push(Buffer.from(line(1, "runtime-preaccept", "started")));
    expect(() => incomplete.complete()).toThrow(/^telemetry-invalid$/);
  });

  it("rejects invalid UTF-8 line overflow and total overflow without echoing bytes", () => {
    const injected = "/Users/test-user/private.mov prompt=steal";
    const cases: Array<{ chunk: Uint8Array; maxBytes: number }> = [
      { chunk: Uint8Array.from([0xff, 0x0a]), maxBytes: 4_096 },
      { chunk: Buffer.from(`{"${"x".repeat(1_025)}":"${injected}"}\n`), maxBytes: 4_096 },
      { chunk: Buffer.from(`${line(1, "runtime-preaccept", "started")}${injected}`), maxBytes: 80 },
    ];

    for (const fixture of cases) {
      const tracker = createStudioWorkerStageTelemetryTracker({
        nowMs: () => 0,
        maxBytes: fixture.maxBytes,
      });
      try {
        tracker.push(fixture.chunk);
        tracker.complete();
        throw new Error("fixture-did-not-fail");
      } catch (error) {
        expect((error as Error).message).toMatch(/^telemetry-(?:invalid|overflow)$/);
        expect(String(error)).not.toContain(injected);
        expect(String(error)).not.toContain("private.mov");
      }
    }
  });

  it("computes completed and active elapsed milliseconds only from the Node clock", () => {
    const times = [10, 25, 40, 55, 90];
    const tracker = createStudioWorkerStageTelemetryTracker({
      nowMs: () => times.shift()!,
      maxBytes: 4_096,
    });
    tracker.push(Buffer.from(line(1, "runtime-preaccept", "started")));
    tracker.push(Buffer.from(line(2, "runtime-preaccept", "completed")));
    tracker.push(Buffer.from(line(3, "frontstage-extraction", "started")));

    expect(tracker.snapshot()).toEqual({
      schemaVersion: 1,
      lastStage: "frontstage-extraction",
      completedStageMs: { "runtime-preaccept": 15 },
      activeStageElapsedMs: 15,
    });
    expect(tracker.snapshot(90).activeStageElapsedMs).toBe(50);
    expect(Object.isFrozen(tracker.snapshot(90))).toBe(true);
  });

  it("finalizes one valid active-stage prefix only after timeout stream closure", () => {
    const times = [10, 20, 30, 40];
    const tracker = createStudioWorkerStageTelemetryTracker({
      nowMs: () => times.shift()!,
      maxBytes: 4_096,
    });
    tracker.push(Buffer.from([
      line(1, "runtime-preaccept", "started"),
      line(2, "runtime-preaccept", "completed"),
      line(3, "frontstage-extraction", "started"),
    ].join("")));

    expect(tracker.completeTerminated(40)).toEqual({
      schemaVersion: 1,
      lastStage: "frontstage-extraction",
      completedStageMs: { "runtime-preaccept": 10 },
      activeStageElapsedMs: 10,
    });
  });

  it("rejects a partial JSON or UTF-8 tail when finalizing a timeout", () => {
    for (const tail of [Buffer.from('{"schemaVersion":1'), Uint8Array.from([0xe4, 0xb8])]) {
      const tracker = createStudioWorkerStageTelemetryTracker({ nowMs: () => 0, maxBytes: 4_096 });
      tracker.push(Buffer.from(line(1, "runtime-preaccept", "started")));
      tracker.push(tail);
      expect(() => tracker.completeTerminated(10)).toThrow(/^telemetry-invalid$/);
    }
  });
});
