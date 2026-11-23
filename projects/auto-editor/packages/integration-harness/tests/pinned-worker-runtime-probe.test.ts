import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, link, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import type { ModelAnalysisJobV1, ModelAnalysisReceiptV1 } from "@wearable/contracts";
import {
  LocalProcessStudioModelWorker,
  StudioModelWorkerFailureError,
  StudioModelWorkerTimeoutError,
} from "@wearable/mobile-agent";
import { afterEach, describe, expect, it } from "vitest";

import {
  buildPinnedWorkerRuntimeProbeReport,
  canonicalPinnedWorkerRuntimeProbeReceipt,
  createPinnedWorkerRuntimeProbeWorker,
  generatePinnedWorkerRuntimeProbeMedia,
  installPinnedWorkerRuntimeProbeReceipt,
  PinnedWorkerRuntimeProbeAnalysisFailureError,
  publicPinnedWorkerRuntimeProbeFailureCode,
  runPinnedWorkerRuntimeProbeCommand,
  writePinnedWorkerRuntimeDiagnosticOutput,
  writePinnedWorkerRuntimeProbeOutput,
} from "../src/pinned-worker-runtime-probe";
// @ts-expect-error The checker is an ESM CLI with an exported pure readiness mapper.
import { isStudioModelCheckerReady, normalizeWorkerRuntimeReadiness } from "../../../scripts/check-studio-models.mjs";

const roots: string[] = [];
const execFileAsync = promisify(execFile);
const probePath = fileURLToPath(new URL("../src/pinned-worker-runtime-probe.ts", import.meta.url));
const probeModuleUrl = new URL("../src/pinned-worker-runtime-probe.ts", import.meta.url).href;
const tsxPath = fileURLToPath(new URL("../../../node_modules/tsx/dist/cli.mjs", import.meta.url));
const FORMAL_INPUT_SHA256 = "1ca0727bf07eb75b2d54a7060fb06b7e86afca54cdb763f47883d292b89b873a";
const STRUCTURED_OUTPUT_FAILURE_DETAILS = [
  "top-level-object-keys",
  "suggested-tag-shape-content-duplication",
  "visual-observation-shape-kind",
  "visual-observation-interval",
  "demand-evidence-without-trusted-demand",
  "demand-evidence-shape-trusted-text",
  "warning-uncertainty-text-arrays",
  "scalar-confidence-optional-claim-shape",
] as const;

describe("formal pinned worker runtime probe", () => {
  afterEach(async () => {
    await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
  });

  it("accepts only formal real-model synthetic evidence and emits no raw content", () => {
    const input = validProbeInput();
    const report = buildPinnedWorkerRuntimeProbeReport(input);
    const serialized = JSON.stringify(report);

    expect(report).toEqual({
      schemaVersion: 1,
      runtime: input.runtime,
      model: input.model,
      promptVersion: "studio-qwen3-vl-zh-json-v5",
      inputSha256: FORMAL_INPUT_SHA256,
      outputSha256: "50bcb56bf6f34b665794e79ef3e491be2ffee4b390ef2e04ad8e63caaf601735",
      observationCount: 1,
      observationKinds: ["visible-product"],
      runtimeMs: 1_250,
      maximumResidentSetBytes: 32_000_000,
      syntheticInput: true,
      customerContentUsed: false,
      realModel: true,
      formalWorkerPath: true,
      networkDenied: true,
      providerMode: "local",
      publishedClaimed: false,
    });
    expect(Object.isFrozen(report)).toBe(true);
    expect(Object.isFrozen(report.runtime)).toBe(true);
    expect(Object.isFrozen(report.model)).toBe(true);
    expect(Object.isFrozen(report.observationKinds)).toBe(true);
    expect(serialized).not.toMatch(
      /synthetic customer observation|private-tag|rawOutput|sourcePath|\/Users\/|https?:\/\/|token/i,
    );
  });

  it("generates fixed programmatic workbench media without customer input", async () => {
    const firstRoot = await privateRoot();
    const secondRoot = await privateRoot();
    const first = await generatePinnedWorkerRuntimeProbeMedia(firstRoot);
    const second = await generatePinnedWorkerRuntimeProbeMedia(secondRoot);

    expect(first.durationMs).toBe(2_000);
    expect(first.sha256).toBe(FORMAL_INPUT_SHA256);
    expect(second.sha256).toBe(first.sha256);
    expect(first.path).toBe(await realpath(first.path));
    expect(second.path).toBe(await realpath(second.path));
    expect((await lstat(first.path)).isFile()).toBe(true);
    expect((await readFile(first.path)).byteLength).toBeGreaterThan(0);
  }, 30_000);

  it("constructs the dedicated probe through the default verified config and forces telemetry", async () => {
    const root = await privateRoot();
    const runtimeRoot = join(root, "installed-runtime");
    const worker = createPinnedWorkerRuntimeProbeWorker(root, {
      lockPath: join(root, "models.lock.json"),
      modelRoot: join(root, "models"),
      runtimeId: "synthetic-worker-runtime",
      sandboxPath: "/synthetic/sandbox-exec",
      probeSandbox: () => true,
      enableProbeStageTelemetry: false,
      resolveRuntime: () => ({
        runtimeRoot,
        launcherPath: join(runtimeRoot, "bin", "run-offline-worker"),
        launcherIdentity: {
          device: "1",
          inode: "2",
          byteLength: 3,
          sha256: digest("5"),
        },
        status: "runtime-installed-probe-required",
        bundle: { id: "synthetic-worker-runtime", treeSha256: digest("4") },
      }),
    });

    expect(worker).toBeInstanceOf(LocalProcessStudioModelWorker);
    expect((worker as unknown as {
      config: { stageTelemetry: unknown };
    }).config.stageTelemetry).toEqual({
      protocol: "studio-worker-stage-telemetry-v1",
      maxBytes: 4096,
    });
  });

  it("rejects stale runtime, model, prompt, and input bindings", () => {
    for (const mutate of [
      (input: ReturnType<typeof validProbeInput>) => { input.runtime.treeSha256 = digest("9"); },
      (input: ReturnType<typeof validProbeInput>) => { input.model.sha256 = digest("6"); },
      (input: ReturnType<typeof validProbeInput>) => {
        input.receipt.promptVersion = "studio-qwen3-vl-zh-json-v1";
      },
      (input: ReturnType<typeof validProbeInput>) => { input.inputSha256 = digest("7"); },
      (input: ReturnType<typeof validProbeInput>) => { input.receipt.inputDigest = digest("0"); },
    ]) {
      const input = validProbeInput();
      mutate(input);
      expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(
        /probe-(?:artifact-|prompt-|input-)?binding-mismatch/,
      );
    }
  });

  it("reports whether a formal receipt failed schema parsing or job binding", () => {
    const invalidSchema = validProbeInput();
    delete (invalidSchema.receipt as Partial<ModelAnalysisReceiptV1>).runtimeMs;
    expect(() => buildPinnedWorkerRuntimeProbeReport(invalidSchema)).toThrow(
      "pinned-worker-runtime-probe-receipt-schema-invalid",
    );

    const invalidBinding = validProbeInput();
    invalidBinding.receipt.sourcePath = "/private/tmp/formal-probe/different.mp4";
    expect(() => buildPinnedWorkerRuntimeProbeReport(invalidBinding)).toThrow(
      "pinned-worker-runtime-probe-receipt-binding-invalid",
    );
  });

  it("exposes only the exact outer worker timeout as the safe model-timeout category", () => {
    expect(publicPinnedWorkerRuntimeProbeFailureCode(
      new Error("studio-model-worker-timeout"),
    )).toBe("pinned-worker-runtime-probe-model-analysis-model-timeout");
    expect(publicPinnedWorkerRuntimeProbeFailureCode(
      new Error("pinned-worker-runtime-probe-receipt-schema-invalid"),
    )).toBe("pinned-worker-runtime-probe-receipt-schema-invalid");
    expect(publicPinnedWorkerRuntimeProbeFailureCode(
      new Error("private path /Users/test-user/customer.mov"),
    )).toBe("pinned-worker-runtime-probe-failed");
  });

  it("distinguishes unsuccessful model analysis from input and artifact binding failures", () => {
    const unsuccessful = validProbeInput();
    unsuccessful.receipt.status = "failed-manual-review";
    unsuccessful.receipt.failure = { code: "studio-vision-output-schema-invalid" };
    expect(() => buildPinnedWorkerRuntimeProbeReport(unsuccessful)).toThrow(
      "pinned-worker-runtime-probe-model-analysis-structured-output",
    );

    const inputMismatch = validProbeInput();
    inputMismatch.receipt.inputDigest = digest("0");
    expect(() => buildPinnedWorkerRuntimeProbeReport(inputMismatch)).toThrow(
      "pinned-worker-runtime-probe-input-binding-mismatch",
    );

    const artifactMismatch = validProbeInput();
    artifactMismatch.receipt.artifactBinding!.adapter.sha256 = digest("9");
    expect(() => buildPinnedWorkerRuntimeProbeReport(artifactMismatch)).toThrow(
      "pinned-worker-runtime-probe-artifact-binding-mismatch",
    );
  });

  it("maps only allowlisted worker failures to non-sensitive model-analysis categories", () => {
    const cases = [
      ["runtime-integrity", [
        "studio-model-artifact-digest-mismatch",
        "studio-model-artifact-missing",
        "studio-model-runtime-manifest-incomplete",
        "studio-runtime-model-binding-mismatch",
        "studio-runtime-tree-mismatch",
        "studio-runtime-module-origin-invalid",
        "studio-runtime-offline-environment-required",
      ]],
      ["media-extraction", [
        "studio-technical-dependency-unavailable",
        "studio-technical-scene-detection-failed",
        "studio-technical-frame-decode-failed",
        "studio-vision-media-unavailable",
        "studio-vision-media-invalid",
        "studio-vision-frame-extraction-failed",
        "studio-vision-frame-digest-mismatch",
        "studio-vision-frame-invalid",
        "studio-vision-context-binding-mismatch",
        "studio-vision-source-digest-mismatch",
      ]],
      ["model-execution-dependency", ["studio-vision-dependency-unavailable"]],
      ["model-execution-load", ["studio-vision-model-unavailable"]],
      ["model-execution-contract", ["studio-vision-model-invalid"]],
      ["model-execution-generation", ["studio-vision-model-failed"]],
      ["model-timeout", ["studio-vision-model-timeout"]],
      ["structured-output-json", ["studio-vision-output-invalid-json"]],
      ["structured-output-number", ["studio-vision-output-invalid-number"]],
      ["structured-output-constraint", ["studio-vision-structured-output-unavailable"]],
      ["structured-output-schema", ["studio-vision-output-schema-invalid"]],
      ["structured-output-size", ["studio-vision-output-too-large"]],
      ["unknown", [
        "studio-technical-new-unapproved-code",
        "studio-vision-new-unapproved-code",
        "private-path-/Users/test-user/customer.mov",
      ]],
    ] as const;

    for (const [category, codes] of cases) {
      for (const code of codes) {
        const input = validProbeInput();
        input.receipt.status = code === "studio-vision-model-timeout"
          ? "failed-retryable"
          : "failed-manual-review";
        input.receipt.failure = { code };
        let message = "";
        try {
          buildPinnedWorkerRuntimeProbeReport(input);
        } catch (error) {
          message = error instanceof Error ? error.message : String(error);
        }
        expect(message).toBe(`pinned-worker-runtime-probe-model-analysis-${category}`);
        expect(message).not.toContain(code);
        expect(message).not.toContain("/Users/");
      }
    }
  });

  it("keeps fixed schema failures public as structured-output-schema", () => {
    for (const detail of STRUCTURED_OUTPUT_FAILURE_DETAILS) {
      const input = validProbeInput();
      input.receipt.status = "failed-manual-review";
      input.receipt.failure = { code: `studio-vision-output-schema-${detail}` };
      expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(
        "pinned-worker-runtime-probe-model-analysis-structured-output-schema",
      );
    }
  });

  it("classifies a preaccept runtime failure before rejecting fallback receipt semantics", () => {
    const input = validProbeInput();
    input.receipt.status = "failed-manual-review";
    input.receipt.failure = { code: "studio-runtime-artifact-unapproved" };
    input.receipt.synthetic = true;
    input.receipt.providerMode = "offline-fallback";

    expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(
      "pinned-worker-runtime-probe-model-analysis-runtime-integrity",
    );
  });

  it("rejects a self-consistent receipt for any input other than the fixed synthetic media", () => {
    const input = validProbeInput();
    const differentDigest = digest("8");
    input.job.sourceDigest = differentDigest;
    input.receipt.sourceDigest = differentDigest;
    input.receipt.inputBinding!.sourceDigest = differentDigest;
    input.inputSha256 = differentDigest;
    input.receipt.inputDigest = sha256(canonicalJson(input.job));

    expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(
      /probe-binding-mismatch/,
    );
  });

  it("rejects empty or unknown protocol-v2 observations and unsafe evidence claims", () => {
    for (const [expected, mutate] of [
      ["probe-observation-invalid", (input: ReturnType<typeof validProbeInput>) => {
        input.receipt.qualitySignals.visualObservations = [];
      }],
      ["probe-observation-invalid", (input: ReturnType<typeof validProbeInput>) => {
        input.receipt.qualitySignals.visualObservations[0]!.kind = "identity";
      }],
      ["probe-real-model-required", (input: ReturnType<typeof validProbeInput>) => {
        input.realModel = false;
      }],
      ["probe-customer-content-forbidden", (input: ReturnType<typeof validProbeInput>) => {
        input.customerContentUsed = true;
      }],
      ["probe-formal-worker-required", (input: ReturnType<typeof validProbeInput>) => {
        input.formalWorkerPath = false;
      }],
      ["probe-network-policy-required", (input: ReturnType<typeof validProbeInput>) => {
        input.networkPolicyEnforced = false;
      }],
      ["probe-network-policy-required", (input: ReturnType<typeof validProbeInput>) => {
        input.networkConnectionObserved = true;
      }],
      ["probe-publish-claim-forbidden", (input: ReturnType<typeof validProbeInput>) => {
        input.publishedClaimed = true;
      }],
    ] as const) {
      const input = validProbeInput();
      mutate(input);
      expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(expected);
    }
  });

  it("rejects invalid time ordering and non-finite, negative, or unsafe metrics", () => {
    for (const mutate of [
      (input: ReturnType<typeof validProbeInput>) => {
        input.receipt.completedAt = "2026-09-23T00:00:00.000Z";
        input.receipt.startedAt = "2026-09-23T00:00:01.000Z";
      },
      (input: ReturnType<typeof validProbeInput>) => { input.receipt.runtimeMs = -1; },
      (input: ReturnType<typeof validProbeInput>) => { input.receipt.runtimeMs = Number.NaN; },
      (input: ReturnType<typeof validProbeInput>) => {
        input.receipt.runtimeMs = Number.MAX_SAFE_INTEGER + 1;
        input.receipt.runtimeMetrics!.runtimeMs = Number.MAX_SAFE_INTEGER + 1;
      },
      (input: ReturnType<typeof validProbeInput>) => {
        input.maximumResidentSetBytes = Number.MAX_SAFE_INTEGER + 1;
      },
      (input: ReturnType<typeof validProbeInput>) => { input.maximumResidentSetBytes = -1; },
      (input: ReturnType<typeof validProbeInput>) => { input.maximumResidentSetBytes = Number.NaN; },
    ]) {
      const input = validProbeInput();
      mutate(input);
      expect(() => buildPinnedWorkerRuntimeProbeReport(input)).toThrow(
        /probe-(receipt(?:-schema)?|metric)-invalid/,
      );
    }
  });

  it("computes outputSha256 only from the canonical safe projection", () => {
    const original = validProbeInput();
    const changedRawText = validProbeInput();
    changedRawText.receipt.observations = ["a completely different private observation"];
    changedRawText.receipt.suggestedTags = ["a-private-tag"];
    changedRawText.receipt.qualitySignals.visualObservations[0]!.text = "private model text changed";
    const changedSafeKind = validProbeInput();
    changedSafeKind.receipt.qualitySignals.visualObservations[0]!.kind = "visible-action";

    expect(buildPinnedWorkerRuntimeProbeReport(changedRawText).outputSha256)
      .toBe(buildPinnedWorkerRuntimeProbeReport(original).outputSha256);
    expect(buildPinnedWorkerRuntimeProbeReport(changedSafeKind).outputSha256)
      .not.toBe(buildPinnedWorkerRuntimeProbeReport(original).outputSha256);
  });

  it("installs canonical sanitized bytes privately and returns the lock binding without editing a lock", async () => {
    const root = await privateRoot();
    const source = join(root, "source.json");
    const lock = join(root, "models.lock.json");
    const sourceBytes = canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(validProbeInput()),
    );
    await writeFile(source, sourceBytes, { mode: 0o600 });
    await writeFile(lock, "lock-must-not-change\n", { mode: 0o600 });

    const binding = await installPinnedWorkerRuntimeProbeReceipt({
      sourcePath: source,
      modelRoot: root,
    });
    const destination = join(root, binding.receiptPath);

    expect(binding).toEqual({
      receiptPath: "runtime/probes/synthetic-worker-runtime.json",
      receiptSha256: sha256(sourceBytes),
      runtimeTreeSha256: digest("4"),
      modelSha256: digest("7"),
      promptVersion: "studio-qwen3-vl-zh-json-v5",
      inputSha256: FORMAL_INPUT_SHA256,
    });
    expect(await readFile(destination, "utf8")).toBe(sourceBytes);
    expect((await lstat(destination)).mode & 0o777).toBe(0o600);
    expect(await readFile(lock, "utf8")).toBe("lock-must-not-change\n");
    expect((await readdir(join(root, "runtime", "probes"))).sort()).toEqual([
      "synthetic-worker-runtime.json",
    ]);

    await expect(installPinnedWorkerRuntimeProbeReceipt({
      sourcePath: source,
      modelRoot: root,
    })).resolves.toEqual(binding);
  });

  it("publishes at most one of two different receipts under a 100-round concurrent install race", async () => {
    const root = await privateRoot();
    const firstInput = validProbeInput();
    const secondInput = validProbeInput();
    secondInput.maximumResidentSetBytes += 1;
    const firstBytes = canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(firstInput),
    );
    const secondBytes = canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(secondInput),
    );

    for (let round = 0; round < 100; round += 1) {
      const modelRoot = join(root, `round-${round}`);
      await mkdir(modelRoot, { mode: 0o700 });
      const firstSource = join(modelRoot, "first.json");
      const secondSource = join(modelRoot, "second.json");
      await Promise.all([
        writeFile(firstSource, firstBytes, { mode: 0o600 }),
        writeFile(secondSource, secondBytes, { mode: 0o600 }),
      ]);

      const settled = await Promise.allSettled([
        installPinnedWorkerRuntimeProbeReceipt({ sourcePath: firstSource, modelRoot }),
        installPinnedWorkerRuntimeProbeReceipt({ sourcePath: secondSource, modelRoot }),
      ]);
      const fulfilled = settled.filter((result) => result.status === "fulfilled");
      const rejected = settled.filter((result) => result.status === "rejected");
      const failures = rejected.map((result) => (
        (result as PromiseRejectedResult).reason as Error
      ).message).join(",");
      expect(fulfilled, `round ${round}: ${failures}`).toHaveLength(1);
      expect(rejected, `round ${round}`).toHaveLength(1);
      expect((rejected[0] as PromiseRejectedResult).reason).toMatchObject({
        message: "pinned-worker-runtime-probe-install-destination-conflict",
      });
      const installed = await readFile(join(
        modelRoot,
        "runtime/probes/synthetic-worker-runtime.json",
      ));
      expect(
        installed.equals(Buffer.from(firstBytes)) || installed.equals(Buffer.from(secondBytes)),
        `round ${round}`,
      ).toBe(true);
    }
  });

  it("recovers an install crash after target link without accepting stale or different bytes", async () => {
    const root = await privateRoot();
    const source = join(root, "source.json");
    const report = buildPinnedWorkerRuntimeProbeReport(validProbeInput());
    const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(report);
    await writeFile(source, canonical, { mode: 0o600 });

    await expect(execFileAsync(process.execPath, [tsxPath, "--eval", `
      import(${JSON.stringify(probeModuleUrl)}).then(async (module) => {
        await module.installPinnedWorkerRuntimeProbeReceipt({
          sourcePath: process.env.PROBE_SOURCE,
          modelRoot: process.env.PROBE_ROOT,
        }, { afterLink: () => process.exit(91) });
      });
    `], { env: { ...process.env, PROBE_SOURCE: source, PROBE_ROOT: root } })).rejects.toMatchObject({
      code: 91,
    });

    const probes = join(root, "runtime", "probes");
    const destination = join(probes, "synthetic-worker-runtime.json");
    const crashedEntries = (await readdir(probes)).sort();
    expect(crashedEntries).toHaveLength(2);
    const temporaryName = crashedEntries.find((entry) => entry !== "synthetic-worker-runtime.json")!;
    const temporary = join(probes, temporaryName);
    const destinationBefore = await lstat(destination, { bigint: true });
    const temporaryBefore = await lstat(temporary, { bigint: true });
    expect({ dev: temporaryBefore.dev, ino: temporaryBefore.ino, nlink: temporaryBefore.nlink }).toEqual({
      dev: destinationBefore.dev,
      ino: destinationBefore.ino,
      nlink: 2n,
    });

    const changed = validProbeInput();
    changed.maximumResidentSetBytes += 1;
    const changedSource = join(root, "changed.json");
    await writeFile(changedSource, canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(changed),
    ), { mode: 0o600 });
    await expect(installPinnedWorkerRuntimeProbeReceipt({
      sourcePath: changedSource,
      modelRoot: root,
    })).rejects.toThrow("pinned-worker-runtime-probe-install-destination-conflict");
    expect((await readdir(probes)).sort()).toEqual(crashedEntries);

    await expect(installPinnedWorkerRuntimeProbeReceipt({
      sourcePath: source,
      modelRoot: root,
    })).resolves.toMatchObject({ receiptSha256: sha256(canonical) });
    expect(await readdir(probes)).toEqual(["synthetic-worker-runtime.json"]);
    expect((await lstat(destination)).nlink).toBe(1);
  });

  it("lets a live publisher finish when an identical installer recovers its linked temporary", async () => {
    const root = await privateRoot();
    const source = join(root, "source.json");
    const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(validProbeInput()),
    );
    await writeFile(source, canonical, { mode: 0o600 });
    let linked!: () => void;
    let resume!: () => void;
    const linkedPromise = new Promise<void>((resolve) => { linked = resolve; });
    const resumePromise = new Promise<void>((resolve) => { resume = resolve; });

    const first = installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot: root }, {
      afterLink: async () => { linked(); await resumePromise; },
    });
    await linkedPromise;
    const second = installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot: root });
    await expect(second).resolves.toMatchObject({ receiptSha256: sha256(canonical) });
    resume();
    await expect(first).resolves.toMatchObject({ receiptSha256: sha256(canonical) });
    const destination = join(root, "runtime/probes/synthetic-worker-runtime.json");
    expect((await lstat(destination)).nlink).toBe(1);
    expect(await readdir(join(root, "runtime", "probes"))).toEqual([
      "synthetic-worker-runtime.json",
    ]);
  });

  it("recovers an ordinary-output crash through the same private no-clobber protocol", async () => {
    const root = await privateRoot();
    const report = buildPinnedWorkerRuntimeProbeReport(validProbeInput());
    const reportPath = join(root, "report-source.json");
    const output = join(root, "probe.json");
    await writeFile(reportPath, canonicalPinnedWorkerRuntimeProbeReceipt(report), { mode: 0o600 });

    await expect(execFileAsync(process.execPath, [tsxPath, "--eval", `
      Promise.all([
        import("node:fs/promises"),
        import(${JSON.stringify(probeModuleUrl)}),
      ]).then(async ([fs, module]) => {
        const report = JSON.parse(await fs.readFile(process.env.PROBE_REPORT, "utf8"));
        await module.writePinnedWorkerRuntimeProbeOutput(
          process.env.PROBE_OUTPUT,
          report,
          { afterLink: () => process.exit(92) },
        );
      });
    `], { env: { ...process.env, PROBE_REPORT: reportPath, PROBE_OUTPUT: output } })).rejects.toMatchObject({
      code: 92,
    });

    const crashed = (await readdir(root)).filter((entry) => entry !== "report-source.json");
    expect(crashed).toHaveLength(2);
    expect((await lstat(output)).nlink).toBe(2);
    await expect(writePinnedWorkerRuntimeProbeOutput(output, report)).resolves.toBe(
      sha256(canonicalPinnedWorkerRuntimeProbeReceipt(report)),
    );
    expect((await lstat(output)).nlink).toBe(1);
    expect((await readdir(root)).sort()).toEqual(["probe.json", "report-source.json"]);
  });

  it("preserves suspicious crash residue and reports cleanup failure without blind deletion", async () => {
    const report = buildPinnedWorkerRuntimeProbeReport(validProbeInput());
    const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(report);
    for (const scenario of [
      "unknown-name", "invalid-owned-name", "different-inode", "symlink", "wrong-mode",
      "extra-hardlink",
    ] as const) {
      const root = await privateRoot();
      const source = join(root, "source.json");
      const probes = join(root, "runtime", "probes");
      const destination = join(probes, "synthetic-worker-runtime.json");
      const owned = join(probes, ".synthetic-worker-runtime.json.tmp-00000000-0000-4000-8000-000000000001");
      await writeFile(source, canonical, { mode: 0o600 });
      await mkdir(probes, { recursive: true, mode: 0o700 });
      await chmod(join(root, "runtime"), 0o700);
      await writeFile(destination, canonical, { mode: scenario === "wrong-mode" ? 0o644 : 0o600 });
      if (scenario === "unknown-name") await link(destination, join(probes, ".foreign-temporary"));
      else if (scenario === "invalid-owned-name") {
        await writeFile(join(probes, ".synthetic-worker-runtime.json.tmp-not-a-uuid"), canonical, {
          mode: 0o600,
        });
      }
      else if (scenario === "different-inode") await writeFile(owned, canonical, { mode: 0o600 });
      else if (scenario === "symlink") await symlink(source, owned);
      else {
        await link(destination, owned);
        if (scenario === "extra-hardlink") await link(destination, join(root, "foreign-hardlink"));
      }
      const before = (await readdir(probes)).sort();
      await expect(installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot: root }), scenario)
        .rejects.toThrow("pinned-worker-runtime-probe-install-destination-conflict");
      expect((await readdir(probes)).sort(), scenario).toEqual(before);
    }

    const root = await privateRoot();
    const source = join(root, "source.json");
    const probes = join(root, "runtime", "probes");
    const destination = join(probes, "synthetic-worker-runtime.json");
    const owned = join(probes, ".synthetic-worker-runtime.json.tmp-00000000-0000-4000-8000-000000000001");
    await writeFile(source, canonical, { mode: 0o600 });
    await mkdir(probes, { recursive: true, mode: 0o700 });
    await chmod(join(root, "runtime"), 0o700);
    await writeFile(destination, canonical, { mode: 0o600 });
    await link(destination, owned);
    await expect(installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot: root }, {
      unlinkFile: async () => { throw Object.assign(new Error("synthetic-cleanup-failure"), { code: "EACCES" }); },
    })).rejects.toThrow("pinned-worker-runtime-probe-install-temporary-cleanup-failed");
    expect((await readdir(probes)).sort()).toEqual([
      ".synthetic-worker-runtime.json.tmp-00000000-0000-4000-8000-000000000001",
      "synthetic-worker-runtime.json",
    ]);
  });

  it("rejects an invalid source before creating runtime or probe directories", async () => {
    const root = await privateRoot();
    const source = join(root, "invalid.json");
    const modelRoot = join(root, "model-root");
    await writeFile(source, "{}\n", { mode: 0o600 });
    await mkdir(modelRoot, { mode: 0o700 });

    await expect(installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot }))
      .rejects.toThrow("pinned-worker-runtime-probe-install-receipt-invalid");
    await expect(lstat(join(modelRoot, "runtime"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("keeps the source descriptor stable through the final pre-link validation", async () => {
    const root = await privateRoot();
    const source = join(root, "source.json");
    const retained = join(root, "retained-source.json");
    const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(
      buildPinnedWorkerRuntimeProbeReport(validProbeInput()),
    );
    await writeFile(source, canonical, { mode: 0o600 });

    await expect(installPinnedWorkerRuntimeProbeReceipt({ sourcePath: source, modelRoot: root }, {
      beforeLink: async () => {
        await rename(source, retained);
        await writeFile(source, canonical, { mode: 0o600 });
      },
    })).rejects.toThrow("pinned-worker-runtime-probe-install-source-changed");
    await expect(lstat(join(root, "runtime/probes/synthetic-worker-runtime.json")))
      .rejects.toMatchObject({ code: "ENOENT" });
  });

  it("rejects symlink, hardlink, loose-mode, non-canonical, raw-field, traversal, and foreign receipts", async () => {
    for (const scenario of [
      "symlink", "hardlink", "loose-mode", "non-canonical", "raw-field", "traversal",
      "dot-id", "wrong-prompt", "wrong-model", "foreign",
    ] as const) {
      const root = await privateRoot();
      const report = buildPinnedWorkerRuntimeProbeReport(validProbeInput());
      const canonical = canonicalPinnedWorkerRuntimeProbeReceipt(report);
      const regular = join(root, `${scenario}-regular.json`);
      const source = join(root, `${scenario}.json`);
      await writeFile(regular, canonical, { mode: 0o600 });
      if (scenario === "symlink") await symlink(regular, source);
      else if (scenario === "hardlink") await link(regular, source);
      else {
        let bytes = canonical;
        if (scenario === "non-canonical") bytes = `${JSON.stringify(report, null, 2)}\n`;
        if (scenario === "raw-field") bytes = canonicalReceipt({ ...report, rawOutput: "secret" });
        if (scenario === "traversal") {
          bytes = canonicalReceipt({ ...report, runtime: { ...report.runtime, id: "../escape" } });
        }
        if (scenario === "dot-id") bytes = canonicalReceipt({
          ...report,
          runtime: { ...report.runtime, id: ".." },
        });
        if (scenario === "wrong-prompt") bytes = canonicalReceipt({
          ...report,
          promptVersion: "unbound-prompt",
        });
        if (scenario === "wrong-model") bytes = canonicalReceipt({
          ...report,
          model: { ...report.model, id: "unbound-model" },
        });
        await writeFile(source, bytes, { mode: 0o600 });
      }
      if (scenario === "loose-mode") await chmod(source, 0o644);
      if (scenario === "foreign") {
        await mkdir(join(root, "runtime", "probes"), { recursive: true, mode: 0o700 });
        await writeFile(
          join(root, "runtime", "probes", "synthetic-worker-runtime.json"),
          "foreign\n",
          { mode: 0o600 },
        );
      }

      await expect(installPinnedWorkerRuntimeProbeReceipt({
        sourcePath: source,
        modelRoot: root,
      }), scenario).rejects.toThrow(/^pinned-worker-runtime-probe-install-/);
      const probeDirectory = join(root, "runtime", "probes");
      const entries = await readdir(probeDirectory).catch(() => []);
      expect(entries.some((entry) => entry.includes(".tmp-")), scenario).toBe(false);
      if (scenario === "traversal") {
        await expect(lstat(join(root, "escape.json"))).rejects.toMatchObject({ code: "ENOENT" });
      }
    }
  });

  it("keeps CLI modes mutually exclusive and reports stable argument errors", async () => {
    for (const args of [
      [],
      ["--output", "out.json", "--source", "source.json", "--install-receipt"],
      ["--install-receipt"],
      ["--source", "source.json"],
    ]) {
      await expect(execFileAsync(process.execPath, [tsxPath, probePath, ...args])).rejects.toMatchObject({
        stderr: expect.stringMatching(/^pinned-worker-runtime-probe-arguments-invalid\n$/),
      });
    }
    await expect(execFileAsync(process.execPath, [
      tsxPath,
      probePath,
      "--output", "relative-probe.json",
      "--diagnostic-output", "/tmp/pinned-worker-runtime-diagnostic.json",
    ])).rejects.toMatchObject({
      stderr: expect.stringMatching(/^pinned-worker-runtime-probe-output-arguments-invalid\n$/),
    });
    await expect(execFileAsync(process.execPath, [
      tsxPath,
      probePath,
      "--output", "/tmp/pinned-worker-runtime-probe.json",
      "--diagnostic-output", "relative-diagnostic.json",
    ])).rejects.toMatchObject({
      stderr: expect.stringMatching(/^pinned-worker-runtime-diagnostic-output-arguments-invalid\n$/),
    });
  });

  it("requires distinct private success and diagnostic paths before entering Worker", async () => {
    const root = await privateRoot();
    const output = join(root, "probe.json");
    let runs = 0;

    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "models.lock.json"),
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: output,
      diagnosticOutputPath: output,
    }, {
      runProbe: async () => { runs += 1; return buildPinnedWorkerRuntimeProbeReport(validProbeInput()); },
      resolveBindings: async () => probeBindings(),
    })).rejects.toThrow("pinned-worker-runtime-probe-output-paths-invalid");

    const loose = join(root, "loose");
    await mkdir(loose, { mode: 0o755 });
    await chmod(loose, 0o755);
    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "models.lock.json"),
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: output,
      diagnosticOutputPath: join(loose, "diagnostic.json"),
    }, {
      runProbe: async () => { runs += 1; return buildPinnedWorkerRuntimeProbeReport(validProbeInput()); },
      resolveBindings: async () => probeBindings(),
    })).rejects.toThrow("pinned-worker-runtime-diagnostic-output-directory-unsafe");
    expect(runs).toBe(0);
  });

  it("writes one mode-0600 diagnostic after a model-timeout fixture and no success receipt", async () => {
    const root = await privateRoot();
    const output = join(root, "probe.json");
    const diagnostic = join(root, "diagnostic.json");
    const lock = join(root, "models.lock.json");
    await writeFile(lock, "fixed-lock\n", { mode: 0o600 });
    let runs = 0;
    const telemetry = {
      schemaVersion: 1 as const,
      lastStage: "model-generation" as const,
      completedStageMs: {
        "runtime-preaccept": 10,
        "frontstage-extraction": 20,
        "model-load": 30,
        "processor-prepare": 40,
      },
      activeStageElapsedMs: 50,
    };

    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: lock,
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: output,
      diagnosticOutputPath: diagnostic,
    }, {
      nowMs: (() => { const values = [1_000, 1_200]; return () => values.shift() ?? 1_200; })(),
      resolveBindings: async () => probeBindings(),
      runProbe: async () => { runs += 1; throw new StudioModelWorkerTimeoutError(telemetry); },
    })).rejects.toThrow("studio-model-worker-timeout");

    expect(runs).toBe(1);
    await expect(lstat(output)).rejects.toMatchObject({ code: "ENOENT" });
    const parsed = JSON.parse(await readFile(diagnostic, "utf8"));
    expect(parsed).toMatchObject({
      schemaVersion: 1,
      status: "timeout",
      safeFailureCategory: "model-timeout",
      lastStage: "model-generation",
      totalElapsedMs: 200,
      activeStageElapsedMs: 50,
    });
    const details = await lstat(diagnostic);
    expect(details.mode & 0o777).toBe(0o600);
    expect(details.nlink).toBe(1);
    expect(await readFile(lock, "utf8")).toBe("fixed-lock\n");
  });

  it("never writes a diagnostic for malformed telemetry or an unknown failure", async () => {
    for (const failure of [
      new StudioModelWorkerTimeoutError(null),
      new StudioModelWorkerTimeoutError({
        schemaVersion: 1,
        lastStage: null,
        completedStageMs: {},
        activeStageElapsedMs: null,
      }),
      new Error("unknown-private-failure /Users/customer.mov"),
    ]) {
      const root = await privateRoot();
      const diagnostic = join(root, "diagnostic.json");
      await expect(runPinnedWorkerRuntimeProbeCommand({
        lockPath: join(root, "models.lock.json"),
        modelRoot: root,
        runtimeId: "synthetic-worker-runtime",
        outputPath: join(root, "probe.json"),
        diagnosticOutputPath: diagnostic,
      }, {
        resolveBindings: async () => probeBindings(),
        runProbe: async () => { throw failure; },
      })).rejects.toBe(failure);
      await expect(lstat(diagnostic)).rejects.toMatchObject({ code: "ENOENT" });
    }
  });

  it("rejects existing symlinked noncanonical and loose diagnostic targets without Worker entry", async () => {
    for (const scenario of ["existing", "symlink", "noncanonical", "loose"] as const) {
      const root = await privateRoot();
      const diagnostic = scenario === "noncanonical"
        ? `${root}/nested/../diagnostic.json`
        : join(root, "diagnostic.json");
      if (scenario === "existing") await writeFile(diagnostic, "foreign\n", { mode: 0o600 });
      if (scenario === "symlink") {
        const source = join(root, "source.json");
        await writeFile(source, "foreign\n", { mode: 0o600 });
        await symlink(source, diagnostic);
      }
      if (scenario === "loose") {
        const loose = join(root, "loose");
        await mkdir(loose, { mode: 0o755 });
        await chmod(loose, 0o755);
      }
      const actualDiagnostic = scenario === "loose" ? join(root, "loose", "diagnostic.json") : diagnostic;
      let runs = 0;
      await expect(runPinnedWorkerRuntimeProbeCommand({
        lockPath: join(root, "models.lock.json"),
        modelRoot: root,
        runtimeId: "synthetic-worker-runtime",
        outputPath: join(root, "probe.json"),
        diagnosticOutputPath: actualDiagnostic,
      }, {
        resolveBindings: async () => probeBindings(),
        runProbe: async () => { runs += 1; return buildPinnedWorkerRuntimeProbeReport(validProbeInput()); },
      }), scenario).rejects.toThrow(/^pinned-worker-runtime-diagnostic-output-/);
      expect(runs, scenario).toBe(0);
    }
  });

  it("does not retry or mutate the lock when diagnostic publication fails", async () => {
    const root = await privateRoot();
    const lock = join(root, "models.lock.json");
    await writeFile(lock, "fixed-lock\n", { mode: 0o600 });
    let runs = 0;
    let writes = 0;
    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: lock,
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: join(root, "probe.json"),
      diagnosticOutputPath: join(root, "diagnostic.json"),
    }, {
      resolveBindings: async () => probeBindings(),
      runProbe: async () => {
        runs += 1;
        throw new StudioModelWorkerTimeoutError({
          schemaVersion: 1,
          lastStage: "model-load",
          completedStageMs: { "runtime-preaccept": 1, "frontstage-extraction": 1 },
          activeStageElapsedMs: 1,
        });
      },
      writeDiagnostic: async () => { writes += 1; throw new Error("synthetic-write-failure"); },
    })).rejects.toThrow("pinned-worker-runtime-probe-diagnostic-publish-failed");
    expect({ runs, writes }).toEqual({ runs: 1, writes: 1 });
    expect(await readFile(lock, "utf8")).toBe("fixed-lock\n");
  });

  it("writes one failed diagnostic for an allowlisted internal model timeout", async () => {
    const root = await privateRoot();
    const diagnostic = join(root, "diagnostic.json");
    const failure = new PinnedWorkerRuntimeProbeAnalysisFailureError(
      "model-timeout",
      {
        schemaVersion: 1,
        lastStage: "model-generation",
        completedStageMs: {
          "runtime-preaccept": 1,
          "frontstage-extraction": 1,
          "model-load": 1,
          "processor-prepare": 1,
        },
        activeStageElapsedMs: 1,
      },
    );
    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "models.lock.json"),
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: join(root, "probe.json"),
      diagnosticOutputPath: diagnostic,
    }, {
      resolveBindings: async () => probeBindings(),
      runProbe: async () => { throw failure; },
    })).rejects.toBe(failure);

    expect(JSON.parse(await readFile(diagnostic, "utf8"))).toMatchObject({
      status: "failed",
      safeFailureCategory: "model-timeout",
      lastStage: "model-generation",
    });
  });

  it("maps every allowlisted schema failure into only its fixed private detail", async () => {
    for (const detail of STRUCTURED_OUTPUT_FAILURE_DETAILS) {
      const root = await privateRoot();
      const diagnostic = join(root, "diagnostic.json");
      const input = validProbeInput();
      input.receipt.status = "failed-manual-review";
      input.receipt.failure = { code: `studio-vision-output-schema-${detail}` };
      const failure = new StudioModelWorkerFailureError(input.receipt, {
        schemaVersion: 1,
        lastStage: "output-validation",
        completedStageMs: {
          "runtime-preaccept": 1,
          "frontstage-extraction": 1,
          "model-load": 1,
          "processor-prepare": 1,
          "model-generation": 1,
        },
        activeStageElapsedMs: 1,
      });
      await expect(runPinnedWorkerRuntimeProbeCommand({
        lockPath: join(root, "models.lock.json"),
        modelRoot: root,
        runtimeId: "synthetic-worker-runtime",
        outputPath: join(root, "probe.json"),
        diagnosticOutputPath: diagnostic,
      }, {
        resolveBindings: async () => probeBindings(),
        runProbe: async () => { throw failure; },
      })).rejects.toBe(failure);

      const serialized = await readFile(diagnostic, "utf8");
      expect(JSON.parse(serialized)).toMatchObject({
        safeFailureCategory: "structured-output-schema",
        structuredOutputFailureDetail: detail,
      });
      expect(serialized).not.toContain("studio-vision-output-schema-");
      expect(serialized).not.toContain("/Users/");
    }
  });

  it("preserves a safe schema detail through the probe analysis error", async () => {
    const root = await privateRoot();
    const diagnostic = join(root, "diagnostic.json");
    const failure = new PinnedWorkerRuntimeProbeAnalysisFailureError(
      "structured-output-schema",
      {
        schemaVersion: 1,
        lastStage: "output-validation",
        completedStageMs: {
          "runtime-preaccept": 1,
          "frontstage-extraction": 1,
          "model-load": 1,
          "processor-prepare": 1,
          "model-generation": 1,
        },
        activeStageElapsedMs: 1,
      },
      "visual-observation-interval",
    );
    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "models.lock.json"),
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: join(root, "probe.json"),
      diagnosticOutputPath: diagnostic,
    }, {
      resolveBindings: async () => probeBindings(),
      runProbe: async () => { throw failure; },
    })).rejects.toBe(failure);

    expect(JSON.parse(await readFile(diagnostic, "utf8"))).toMatchObject({
      safeFailureCategory: "structured-output-schema",
      structuredOutputFailureDetail: "visual-observation-interval",
    });
  });

  it("omits structured-output detail for the legacy generic schema failure", async () => {
    const root = await privateRoot();
    const diagnostic = join(root, "diagnostic.json");
    const input = validProbeInput();
    input.receipt.status = "failed-manual-review";
    input.receipt.failure = { code: "studio-vision-output-schema-invalid" };
    const failure = new StudioModelWorkerFailureError(input.receipt, {
      schemaVersion: 1,
      lastStage: "output-validation",
      completedStageMs: {
        "runtime-preaccept": 1,
        "frontstage-extraction": 1,
        "model-load": 1,
        "processor-prepare": 1,
        "model-generation": 1,
      },
      activeStageElapsedMs: 1,
    });
    await expect(runPinnedWorkerRuntimeProbeCommand({
      lockPath: join(root, "models.lock.json"),
      modelRoot: root,
      runtimeId: "synthetic-worker-runtime",
      outputPath: join(root, "probe.json"),
      diagnosticOutputPath: diagnostic,
    }, {
      resolveBindings: async () => probeBindings(),
      runProbe: async () => { throw failure; },
    })).rejects.toBe(failure);

    expect(JSON.parse(await readFile(diagnostic, "utf8")))
      .not.toHaveProperty("structuredOutputFailureDetail");
  });

  it("publishes ordinary output only into an existing private real directory without overwrite", async () => {
    const probeModule = await import("../src/pinned-worker-runtime-probe") as Record<string, unknown>;
    const writeOutput = probeModule.writePinnedWorkerRuntimeProbeOutput as (
      path: string,
      report: ReturnType<typeof buildPinnedWorkerRuntimeProbeReport>,
    ) => Promise<string>;
    const root = await privateRoot();
    const report = buildPinnedWorkerRuntimeProbeReport(validProbeInput());
    const output = join(root, "probe.json");

    await expect(writeOutput("relative-probe.json", report)).rejects.toThrow(
      "pinned-worker-runtime-probe-output-arguments-invalid",
    );

    const looseParent = join(root, "loose");
    await mkdir(looseParent, { mode: 0o755 });
    await chmod(looseParent, 0o755);
    await expect(writeOutput(join(looseParent, "probe.json"), report)).rejects.toThrow(
      "pinned-worker-runtime-probe-output-directory-unsafe",
    );

    const realParent = join(root, "real-parent");
    const linkedParent = join(root, "linked-parent");
    await mkdir(realParent, { mode: 0o700 });
    await symlink(realParent, linkedParent);
    await expect(writeOutput(join(linkedParent, "probe.json"), report)).rejects.toThrow(
      "pinned-worker-runtime-probe-output-directory-unsafe",
    );

    const protectedTarget = join(root, "models.lock.json");
    await writeFile(protectedTarget, "must-not-change\n", { mode: 0o600 });
    await expect(writeOutput(protectedTarget, report)).rejects.toThrow(
      "pinned-worker-runtime-probe-output-exists",
    );
    expect(await readFile(protectedTarget, "utf8")).toBe("must-not-change\n");

    const hardlinkSource = join(root, "hardlink-source.json");
    const hardlinkTarget = join(root, "hardlink-target.json");
    await writeFile(hardlinkSource, "foreign\n", { mode: 0o600 });
    await link(hardlinkSource, hardlinkTarget);
    await expect(writeOutput(hardlinkTarget, report)).rejects.toThrow(
      "pinned-worker-runtime-probe-output-exists",
    );
    expect(await readFile(hardlinkSource, "utf8")).toBe("foreign\n");

    await expect(writeOutput(output, report)).resolves.toBe(
      sha256(canonicalPinnedWorkerRuntimeProbeReceipt(report)),
    );
    expect(await readFile(output, "utf8")).toBe(
      canonicalPinnedWorkerRuntimeProbeReceipt(report),
    );
    const details = await lstat(output);
    expect(details.mode & 0o777).toBe(0o600);
    expect(details.nlink).toBe(1);
    expect((await readdir(root)).some((entry) => entry.includes(".tmp-"))).toBe(false);
  });

  it("distinguishes a missing probe from a declared but failed real probe", () => {
    expect(normalizeWorkerRuntimeReadiness(
      { probe: null },
      { status: "runtime-installed-probe-required" },
    )).toBe("runtime-installed-probe-required");
    expect(normalizeWorkerRuntimeReadiness(
      { probe: { receiptPath: "runtime/probes/runtime.json" } },
      { status: "runtime-integrity-failed" },
    )).toBe("runtime-integrity-failed");
    expect(normalizeWorkerRuntimeReadiness(
      { probe: { receiptPath: "runtime/probes/runtime.json" } },
      { status: "runtime-real-probe-succeeded" },
    )).toBe("runtime-real-probe-succeeded");
    expect(normalizeWorkerRuntimeReadiness(
      {},
      { status: "runtime-integrity-failed" },
    )).toBe("runtime-integrity-failed");
  });

  it("requires the exact formal probe as well as ready model files", () => {
    const artifacts = [{ status: "ready" }];
    expect(isStudioModelCheckerReady(artifacts, {
      status: "runtime-installed-probe-required",
    })).toBe(false);
    expect(isStudioModelCheckerReady(artifacts, {
      status: "runtime-real-probe-failed",
    })).toBe(false);
    expect(isStudioModelCheckerReady(artifacts, {
      status: "runtime-real-probe-succeeded",
    })).toBe(true);
    expect(isStudioModelCheckerReady([{ status: "missing" }], {
      status: "runtime-real-probe-succeeded",
    })).toBe(false);
    expect(isStudioModelCheckerReady(artifacts, undefined)).toBe(false);
  });
});

function validProbeInput() {
  const job: ModelAnalysisJobV1 = {
    schemaVersion: 1,
    jobId: "formal-synthetic-qwen-probe",
    customerId: "synthetic-probe-customer",
    sessionId: "synthetic-probe-session",
    sourceAssetId: "synthetic-programmatic-workbench",
    sourceDigest: FORMAL_INPUT_SHA256,
    sourcePath: "/private/tmp/formal-probe/synthetic-workbench.mp4",
    sourceStartMs: 0,
    sourceEndMs: 2_000,
    layer: "vision",
    operationKey: "formal-synthetic-qwen-probe-v1",
    requestedAt: "2026-09-23T00:00:00.000Z",
  };
  const { requestedAt: _requestedAt, ...receiptBinding } = job;
  const receipt: ModelAnalysisReceiptV1 & {
    qualitySignals: {
      visualObservations: Array<Record<string, any>>;
      privatePath: string;
      rawOutput: string;
    };
  } = {
    ...receiptBinding,
    adapterName: "mlx-vlm-local",
    modelName: "qwen3-vl-2b-instruct-4bit",
    modelVersion: "2b-instruct-4bit-local",
    checkpointDigest: digest("7"),
    inputDigest: sha256(canonicalJson(job)),
    promptVersion: "studio-qwen3-vl-zh-json-v5",
    observations: ["synthetic customer observation must not escape"],
    suggestedTags: ["private-tag"],
    qualitySignals: {
      visualObservations: [{
        kind: "visible-product",
        text: "synthetic customer observation must not escape",
        startMs: 0,
        endMs: 2_000,
        confidence: 0.75,
        status: "pending-human-confirmation",
      }],
      privatePath: "/Users/test-user/private.mov",
      rawOutput: "rawOutput must not escape",
    },
    confidence: 0.75,
    warnings: ["vision-output-is-model-suggestion"],
    startedAt: "2026-09-23T00:00:01.000Z",
    completedAt: "2026-09-23T00:00:02.250Z",
    runtimeMs: 1_250,
    status: "succeeded",
    failure: null,
    synthetic: false,
    providerMode: "local",
    inputBinding: { sourceDigest: FORMAL_INPUT_SHA256, sourceStartMs: 0, sourceEndMs: 2_000 },
    artifactBinding: {
      adapter: {
        id: "synthetic-worker-runtime",
        version: "0.6.6-py312",
        sha256: digest("4"),
      },
      model: {
        id: "qwen3-vl-2b-instruct-4bit",
        version: "2b-instruct-4bit-local",
        sha256: digest("7"),
      },
    },
    runtimeMetrics: { runtimeMs: 1_250 },
  };
  return {
    runtime: {
      id: "synthetic-worker-runtime",
      version: "0.6.6-py312",
      treeSha256: digest("4"),
    },
    model: {
      id: "qwen3-vl-2b-instruct-4bit",
      version: "2b-instruct-4bit-local",
      sha256: digest("7"),
    },
    job,
    receipt,
    inputSha256: FORMAL_INPUT_SHA256,
    maximumResidentSetBytes: 32_000_000,
    syntheticInput: true,
    customerContentUsed: false,
    realModel: true,
    formalWorkerPath: true,
    networkPolicyEnforced: true,
    networkConnectionObserved: false,
    publishedClaimed: false,
  };
}

function probeBindings() {
  const input = validProbeInput();
  return Object.freeze({ runtime: input.runtime, model: input.model });
}

async function privateRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "pinned-worker-runtime-probe-"));
  roots.push(root);
  await chmod(root, 0o700);
  return root;
}

function canonicalReceipt(value: unknown): string {
  return `${canonicalJson(value)}\n`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function digest(character: string): string {
  return character.repeat(64);
}
