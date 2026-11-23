import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  evaluateStudioModelCase,
  evaluateRealProviderAcceptance,
  runStudioModelEvaluation,
} from "../src/studio-model-evaluation";

const cleanup: string[] = [];

describe("fixed studio model evaluation", () => {
  afterEach(async () => {
    await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true, force: true })));
  });

  it("reports all five layers separately without promoting fixture suggestions to facts", async () => {
    const root = await fixtureEvaluationRoot();
    const report = await runStudioModelEvaluation({
      manifestPath: join(root, "manifest.json"),
      outputDirectory: join(root, "output"),
    });

    expect(report).toMatchObject({
      schemaVersion: 1,
      boundary: "synthetic-desensitized-only",
      providerEvidence: "fixture/synthetic",
      fixtureMechanicalPass: true,
      falseConfirmedFactCount: 0,
      crossCustomerExposureCount: 0,
      networkModelRequestCount: 0,
      originalMutationCount: 0,
      deleteActionCount: 0,
      uploadActionCount: 0,
      publishActionCount: 0,
      realProviderAcceptance: {
        status: "blocked",
        reason: "approved-local-browser-evidence-not-provided",
      },
    });
    expect(report.layers.map((layer) => layer.name)).toEqual([
      "technical", "scene", "transcript", "embedding", "vision",
    ]);
    expect(report.layers.every((layer) => layer.totalCases === 1 && layer.passedCases === 1)).toBe(true);
    expect(report.accuracy).toEqual({
      expectedSceneCount: 2,
      observedSceneCount: 2,
      matchedSceneCount: 2,
      unexpectedSceneCount: 0,
      expectedTranscriptSpanCount: 1,
      observedTranscriptSpanCount: 1,
      matchedTranscriptSpanCount: 1,
      unexpectedTranscriptSpanCount: 0,
      expectedExactDuplicateGroupCount: 1,
      observedExactDuplicateGroupCount: 1,
      matchedExactDuplicateGroupCount: 1,
      unexpectedExactDuplicateGroupCount: 0,
      expectedNearDuplicateGroupCount: 1,
      observedNearDuplicateGroupCount: 1,
      matchedNearDuplicateGroupCount: 1,
      unexpectedNearDuplicateGroupCount: 0,
      expectedSuggestedTagCount: 1,
      observedSuggestedTagCount: 1,
      matchedSuggestedTagCount: 1,
      unexpectedSuggestedTagCount: 0,
      expectedDemandEvidenceCount: 1,
      observedDemandEvidenceCount: 1,
      matchedDemandEvidenceCount: 1,
      unexpectedDemandEvidenceCount: 0,
    });
    expect(report.modelLockDigest).toMatch(/^[a-f0-9]{64}$/u);
    expect(report.gitSha).toMatch(/^[a-f0-9]{40}$/u);
    expect(report.runtimeVersions.node).toBe(process.version);
    expect(report.reportPath).not.toBeNull();
    if (report.reportPath === null) throw new Error("studio-model-eval-report-path-missing");
    expect(JSON.parse(await readFile(report.reportPath, "utf8"))).toEqual(report);
  });

  it("fails closed before evaluation when a manifest source digest is wrong", async () => {
    const root = await fixtureEvaluationRoot();
    const manifestPath = join(root, "manifest.json");
    await writeFile(join(root, "synthetic-case-a.json"), "tampered-source");

    await expect(runStudioModelEvaluation({ manifestPath }))
      .rejects.toThrow("studio-model-eval-source-digest-mismatch:synthetic-case-a");
  });

  it.each([
    ["expected scenes", (manifest: any) => manifest.cases[0].expectedScenes.push({ startMs: 2500, endMs: 3000 })],
    ["suggested tags", (manifest: any) => manifest.cases[0].suggestedTags.push("额外标签")],
    ["exact duplicate groups", (manifest: any) => manifest.cases[0].exactDuplicateGroups.push("exact-extra")],
    ["near duplicate groups", (manifest: any) => manifest.cases[0].nearDuplicateGroups.push("near-extra")],
    ["authorization class", (manifest: any) => { manifest.cases[0].authorizationClass = "desensitized-approved"; }],
    ["demand evidence", (manifest: any) => manifest.cases[0].demandEvidence.push("额外需求证据")],
  ])("rejects an unapproved manifest mutation before parsing cases: %s", async (_label, mutate) => {
    const root = await fixtureEvaluationRoot();
    const manifestPath = join(root, "manifest.json");
    const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
    mutate(manifest);
    await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

    await expect(runStudioModelEvaluation({ manifestPath }))
      .rejects.toThrow("studio-model-eval-manifest-digest-unapproved");
  });

  it("marks missing layer evidence as a mechanical failure instead of success", async () => {
    const { expected, fixture } = await evaluationPair();
    delete fixture.layers.transcript;

    const result = evaluateStudioModelCase(expected, fixture);

    expect(result.passed).toBe(false);
    expect(result.errors).toContain("synthetic-case-a:missing-layer-evidence:transcript");
  });

  it.each([
    ["scene", (fixture: any) => fixture.layers.scene.scenes.push({ startMs: 2500, endMs: 3000 }), "unexpected-scene-output"],
    ["exact duplicate", (fixture: any) => fixture.layers.embedding.exactDuplicateGroups.push("exact-extra"), "unexpected-exact-duplicate-output"],
    ["near duplicate", (fixture: any) => fixture.layers.embedding.nearDuplicateGroups.push("near-extra"), "unexpected-near-duplicate-output"],
    ["suggested tag", (fixture: any) => fixture.layers.vision.suggestedTags.push("额外标签"), "unexpected-suggested-tag-output"],
    ["demand evidence", (fixture: any) => fixture.layers.vision.demandEvidence.push("额外需求证据"), "unexpected-demand-evidence-output"],
  ])("fails an otherwise matching case when observed output has a false positive: %s", async (_label, mutate, errorCode) => {
    const { expected, fixture } = await evaluationPair();
    mutate(fixture);

    const result = evaluateStudioModelCase(expected, fixture);

    expect(result.passed).toBe(false);
    expect(result.errors).toContain(`synthetic-case-a:${errorCode}`);
  });

  it("compares normalized exact multisets independent of array order", async () => {
    const { expected, fixture } = await evaluationPair();
    fixture.layers.scene.scenes.reverse();

    const result = evaluateStudioModelCase(expected, fixture);

    expect(result.passed).toBe(true);
    expect(result.errors).toEqual([]);
  });

  it("allows real-provider PASS only when both require-ready and approved-browser evidence pass", () => {
    expect(evaluateRealProviderAcceptance({
      checkerRequireReady: true,
      browserProviderEvidence: "fixture/synthetic",
    })).toEqual({ status: "blocked", reason: "approved-local-browser-evidence-not-provided" });
    expect(evaluateRealProviderAcceptance({
      checkerRequireReady: false,
      browserProviderEvidence: "approved-local-model",
    })).toEqual({ status: "blocked", reason: "model-checker-require-ready-failed" });
    expect(evaluateRealProviderAcceptance({
      checkerRequireReady: true,
      browserProviderEvidence: "approved-local-model",
    })).toEqual({ status: "passed", reason: null });
  });
});

async function fixtureEvaluationRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "studio-model-evaluation-"));
  cleanup.push(root);
  const fixture = {
    schemaVersion: 1,
    providerEvidence: "fixture/synthetic",
    synthetic: true,
    customerId: "synthetic-customer-a",
    crossCustomerExposureCount: 0,
    networkModelRequestCount: 0,
    peakWorkerMemoryBytes: 0,
    originalMutationCount: 0,
    actions: { delete: 0, upload: 0, publish: 0 },
    confirmedFacts: [],
    layers: {
      technical: { status: "succeeded", runtimeMs: 0, error: null },
      scene: { status: "succeeded", runtimeMs: 0, error: null, scenes: [
        { startMs: 0, endMs: 1000 }, { startMs: 1000, endMs: 2500 },
      ] },
      transcript: { status: "succeeded", runtimeMs: 0, error: null, spans: [
        { startMs: 100, endMs: 900, text: "合成设计讲解" },
      ] },
      embedding: { status: "succeeded", runtimeMs: 0, error: null, exactDuplicateGroups: ["exact-a"], nearDuplicateGroups: ["near-a"] },
      vision: { status: "succeeded", runtimeMs: 0, error: null, suggestedTags: ["合成商品特写"], demandEvidence: ["画面包含合成商品"] },
    },
  };
  const sourcePath = join(root, "synthetic-case-a.json");
  await writeFile(sourcePath, `${JSON.stringify(fixture, null, 2)}\n`, "utf8");
  const sourceDigest = createHash("sha256").update(await readFile(sourcePath)).digest("hex");
  await writeFile(join(root, "manifest.json"), `${JSON.stringify({
    schemaVersion: 1,
    boundary: "synthetic-desensitized-only",
    providerEvidence: "fixture/synthetic",
    cases: [{
      id: "synthetic-case-a",
      sourcePath: "synthetic-case-a.json",
      sourceDigest,
      authorizationClass: "repository-generated-synthetic",
      expectedScenes: [{ startMs: 0, endMs: 1000 }, { startMs: 1000, endMs: 2500 }],
      transcriptReference: [{ startMs: 100, endMs: 900, text: "合成设计讲解" }],
      exactDuplicateGroups: ["exact-a"],
      nearDuplicateGroups: ["near-a"],
      suggestedTags: ["合成商品特写"],
      demandEvidence: ["画面包含合成商品"],
    }],
  }, null, 2)}\n`, "utf8");
  return root;
}

async function evaluationPair(): Promise<{ expected: any; fixture: any }> {
  const root = await fixtureEvaluationRoot();
  const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
  const fixture = JSON.parse(await readFile(join(root, "synthetic-case-a.json"), "utf8"));
  return { expected: manifest.cases[0], fixture };
}
