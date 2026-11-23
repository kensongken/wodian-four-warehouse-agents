import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

import { buildLocalPersonaModelProbeReport } from "../run-local-persona-model-probe.mjs";


const digest = (character) => character.repeat(64);
const execFileAsync = promisify(execFile);
const probePath = fileURLToPath(new URL("../run-local-persona-model-probe.mjs", import.meta.url));
const lockPath = fileURLToPath(new URL("../../models.lock.json", import.meta.url));

function probeInput() {
  return {
    startedAt: "2026-09-22T18:00:00.000Z",
    completedAt: "2026-09-22T18:00:15.000Z",
    device: { model: "Mac16,13", memoryBytes: 13800138000, architecture: "arm64" },
    artifacts: {
      whisperBinary: { id: "whisper.cpp-1.9.2-cli-darwin-arm64", sha256: digest("1") },
      whisperModel: { id: "whisper.cpp-1.9.2-small-q5_1", sha256: digest("2") },
      mlxRuntime: { id: "mlx-vlm-0.6.6", sha256: digest("3") },
      qwenModel: { id: "qwen3-vl-2b-instruct-4bit", sha256: digest("4") },
    },
    whisper: {
      status: "succeeded",
      realModel: true,
      syntheticInput: true,
      inputSha256: digest("5"),
      outputSha256: digest("6"),
      runtimeMs: 9260,
      maximumResidentSetBytes: 496812032,
      suggestionCount: 2,
      privatePath: "/Users/test-user/private.wav",
      rawOutput: "客户原始转写不应进入报告",
    },
    qwen: {
      status: "succeeded",
      realModel: true,
      syntheticInput: true,
      inputSha256: digest("7"),
      outputSha256: digest("8"),
      runtimeMs: 5580,
      maximumResidentSetBytes: 752107520,
      peakMemoryFootprintBytes: 3272755840,
      privatePath: "/Users/test-user/private.png",
      rawOutput: "模型原始回答不应进入报告",
    },
  };
}

test("combined probe report keeps real-model evidence but removes paths and raw model output", () => {
  const report = buildLocalPersonaModelProbeReport(probeInput());
  const serialized = JSON.stringify(report);

  assert.equal(report.schemaVersion, 1);
  assert.equal(report.syntheticInput, true);
  assert.equal(report.realModel, true);
  assert.equal(report.customerContentUsed, false);
  assert.equal(report.transcript.status, "succeeded");
  assert.equal(report.vision.status, "succeeded");
  assert.equal(report.transcript.suggestionCount, 2);
  assert.equal(report.publishedClaimed, false);
  assert.doesNotMatch(serialized, /\/Users\//);
  assert.doesNotMatch(serialized, /客户原始转写|模型原始回答/);
});

test("combined probe report rejects synthetic adapters or incomplete runs", () => {
  for (const mutate of [
    (input) => { input.whisper.realModel = false; },
    (input) => { input.qwen.status = "failed"; },
    (input) => { input.qwen.outputSha256 = null; },
  ]) {
    const input = probeInput();
    mutate(input);
    assert.throws(
      () => buildLocalPersonaModelProbeReport(input),
      /local-persona-model-probe-invalid/,
    );
  }
});

test("probe CLI accepts a repository-relative evidence output path", async () => {
  await assert.rejects(
    execFileAsync(process.execPath, [
      probePath,
      "--output", ".superpowers/evidence/probe.json",
      "--lock", lockPath,
      "--root", "/private/tmp/local-persona-probe-missing-root",
      "--mlx-generate", "/usr/bin/true",
    ]),
    (error) => {
      assert.match(error.stderr, /local-persona-model-probe-artifact-not-ready/);
      assert.doesNotMatch(error.stderr, /arguments-invalid/);
      return true;
    },
  );
});
