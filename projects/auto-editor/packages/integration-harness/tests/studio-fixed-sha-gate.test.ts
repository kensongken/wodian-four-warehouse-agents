import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  createFixedShaGateReport,
  digestGateOutputs,
  summarizeFullTestOutput,
} from "../src/studio-fixed-sha-gate";

const cleanup: string[] = [];

describe("studio fixed-SHA gate evidence", () => {
  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("derives exact full-test counts instead of accepting a generic exit zero", () => {
    const summary = summarizeFullTestOutput(`
 Test Files  54 passed (54)
      Tests  841 passed (841)
ℹ tests 16
ℹ pass 16
creative-api deterministic shards: 208/208 tests passed
 Test Files  1 passed (1)
      Tests  1 passed (1)
studio model Chromium pilot: isolated pass
`);

    expect(summary).toEqual({
      mainVitest: 841,
      nodeInstaller: 16,
      creativeApi: 208,
      modelChromium: 1,
      total: 1066,
    });
    expect(() => summarizeFullTestOutput("Tests 841 passed (841)")).toThrow(
      "studio-fixed-gate-test-summary-incomplete",
    );
  });

  it("parses the same complete summary when CI forces ANSI color codes", () => {
    const summary = summarizeFullTestOutput(`
\u001b[2m Tests \u001b[22m \u001b[32m1341 passed\u001b[39m \u001b[90m(1341)\u001b[39m
ℹ tests 16
ℹ pass 16
creative-api deterministic shards: 208/208 tests passed
\u001b[2m Tests \u001b[22m \u001b[32m3 passed\u001b[39m \u001b[90m(3)\u001b[39m
studio model Chromium pilot: isolated pass
`);

    expect(summary).toEqual({
      mainVitest: 1341,
      nodeInstaller: 16,
      creativeApi: 208,
      modelChromium: 3,
      total: 1568,
    });
  });

  it("digests every evidence output without claiming the gate file digests itself", async () => {
    const root = await mkdtemp(join(tmpdir(), "studio-fixed-gate-output-"));
    cleanup.push(root);
    await mkdir(join(root, "browser"));
    await writeFile(join(root, "browser", "trace.zip"), "trace");
    await writeFile(join(root, "evaluation.json"), "evaluation");
    await writeFile(join(root, "fixed-sha-gate.json"), "old-self");

    const outputs = await digestGateOutputs(root);

    expect(outputs.map((output) => output.relativePath)).toEqual([
      "browser/trace.zip", "evaluation.json",
    ]);
    expect(outputs[0]?.sha256).toBe(createHash("sha256").update("trace").digest("hex"));
    expect(outputs.every((output) => output.byteLength > 0)).toBe(true);
  });

  it("records an expected checker failure as blocked, never as real-provider PASS", () => {
    const report = createFixedShaGateReport({
      candidateSha: "a".repeat(40),
      cleanBefore: true,
      cleanAfter: true,
      commands: [
        command("model-checker", 1),
        command("full-tests", 0),
        command("typecheck", 0),
        command("evaluation", 0),
        command("browser", 0),
        command("diff-check", 0),
      ],
      testCounts: { mainVitest: 841, nodeInstaller: 16, creativeApi: 208, modelChromium: 1, total: 1066 },
      modelGate: {
        exitCode: 1,
        unapprovedArtifactCount: 6,
        visionProbe: "not-run: runtime-unapproved",
      },
      outputs: [],
    });

    expect(report).toMatchObject({
      candidateSha: "a".repeat(40),
      clean: { before: true, after: true },
      modelGate: { status: "blocked", exitCode: 1, unapprovedArtifactCount: 6 },
      realProviderAcceptance: { status: "blocked" },
      gateStatus: "fixture-mechanical-pass-real-provider-blocked",
    });
  });
});

function command(name: string, exitCode: number) {
  return {
    name,
    argv: [name],
    exitCode,
    stdoutDigest: "b".repeat(64),
    stderrDigest: "c".repeat(64),
    summary: "synthetic-test-summary",
  };
}
