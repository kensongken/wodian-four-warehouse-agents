import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { createContext, runInContext } from "node:vm";

import { describe, expect, it } from "vitest";

const publicRoot = join(process.cwd(), "packages/review-app/public");

describe("studio model review workbench UI", () => {
  it("keeps the existing demand and draft feedback loop while adding every reviewable model area", async () => {
    const html = await readFile(join(publicRoot, "studio.html"), "utf8");
    const javascript = await readFile(join(publicRoot, "studio.js"), "utf8");
    const source = `${html}\n${javascript}`;

    for (const label of [
      "告诉我这次想讲什么",
      "目标内容效果",
      "必须出现",
      "避免内容",
      "分析队列与状态",
      "技术质量",
      "转写",
      "模型建议",
      "待人工确认",
      "不确定性",
      "重复组",
      "需求匹配与原因",
      "素材是否合适",
      "本地模型身份",
      "看完觉得怎么样？",
      "一句话意见（可选）",
    ]) expect(source).toContain(label);

    expect(html).not.toMatch(/<button[^>]*(?:delete-original|upload-provider|data-publish)/i);
    expect(html).not.toMatch(/<form[^>]*(?:delete-original|upload-provider|data-publish)/i);
  });

  it("renders source-bound evidence, uncertainty, duplicate groups, local identity, and escaped text", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderAnalysisCard({
      job: { id: "analysis-a", status: "succeeded", sourceAssetId: "asset-a", sourceStartMs: 1000, sourceEndMs: 5000 },
      receipts: [{
        jobId: "receipt-vision", layer: "vision", sourceAssetId: "asset-a",
        sourceDigest: "${"a".repeat(64)}", sourceStartMs: 1000, sourceEndMs: 5000,
        adapterName: "qwen-local", modelName: "qwen3-vl", modelVersion: "2b-4bit",
        checkpointDigest: "${"b".repeat(64)}", status: "succeeded", providerMode: "local",
        synthetic: false, confidence: 0.72, observations: ["<img src=x onerror=alert(1)>"],
        suggestedTags: ["戒指特写"], warnings: [], qualitySignals: {
          uncertainty: ["材质未经人工确认"],
          demandEvidenceCandidates: [{ requirement: "必须出现产品近景", reason: "00:01.000—00:05.000 可见产品" }],
          forbiddenContentWarnings: [{ requirement: "避免客户身份", reason: "没有足够证据" }],
          duplicateGroups: [{ id: "duplicate-a", sourceAssetIds: ["asset-a", "asset-b"] }]
        }
      }]
    })`, context) as string;

    expect(markup).toContain("模型建议");
    expect(markup).toContain("待人工确认");
    expect(markup).toContain("不确定性");
    expect(markup).toContain("重复组");
    expect(markup).toContain("需求匹配与原因");
    expect(markup).toContain("素材是否合适");
    expect(markup).toContain("本地模型");
    expect(markup).toContain("00:01.000—00:05.000");
    expect(markup).toContain("qwen3-vl · 2b-4bit");
    expect(markup).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(markup).not.toContain("<img src=x");
  });

  it("renders the repairable runtime block without private runtime identity or paths", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderModelStatus({
      mode: "offline-fallback",
      localOnly: true,
      degraded: true,
      artifacts: [],
      visionProbe: "not-run: runtime-installed-probe-required",
      workerRuntime: { status: "runtime-installed-probe-required" }
    })`, context) as string;

    expect(markup).toContain("本地模型暂不可用");
    expect(markup).toContain("还没有完成可用的合成样例验证");
    expect(markup).toContain("目前没有已确认可用的模型");
    expect(markup).not.toContain("runtime-installed-probe-required");
    expect(markup).not.toMatch(/(?:\/private\/|models\.lock|worker-runtime-bundle-v1|[a-f0-9]{64}|https?:\/\/)/i);
  });

  it("shows actionable final suitability feedback and an exact failure degradation reason", async () => {
    const context = await loadStudioUiContext();
    const successful = runInContext(`renderAnalysisCard({
      job: { id: "analysis-ok", status: "succeeded", sourceAssetId: "asset-a", sourceStartMs: 0, sourceEndMs: 5000 },
      receipts: [{
        jobId: "receipt-ok", layer: "vision", sourceAssetId: "asset-a",
        sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 5000,
        status: "succeeded", suggestedTags: ["戒指特写"], observations: [], qualitySignals: {},
        modelName: "local-fixture", modelVersion: "v1", confidence: 0.8
      }]
    }, { demandId: "demand-a", matchAssessmentId: "match-a" })`, context) as string;
    const failed = runInContext(`renderAnalysisCard({
      job: { id: "analysis-failed", status: "failed-manual-review", sourceAssetId: "asset-a",
        sourceStartMs: 0, sourceEndMs: 5000,
        failure: { code: "studio-analysis-invalid-receipt", message: null } },
      receipts: [{
        jobId: "receipt-failed", layer: "vision", sourceAssetId: "asset-a",
        sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 5000,
        status: "failed-manual-review", failure: { code: "studio-model-artifact-unapproved" },
        adapterName: "mlx-vlm-local", modelName: "qwen3-vl-2b-instruct-4bit",
        modelVersion: "unavailable", observations: [], suggestedTags: [], qualitySignals: {}
      }]
    })`, context) as string;

    expect(successful).toMatch(/data-material-suitability="suitable"/);
    expect(successful).toMatch(/data-material-suitability="not-suitable"/);
    expect(successful).toContain("标签正确");
    expect(successful).toContain("标签不正确");
    expect(failed).toContain("降级原因");
    expect(failed).toContain("studio-analysis-invalid-receipt");
    expect(failed).toContain("studio-model-artifact-unapproved");
    expect(failed).toContain("mlx-vlm-local · qwen3-vl-2b-instruct-4bit · unavailable");
  });

  it("sends only asset, requested layers, and operation key when analysis starts", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        materials: [{ sourceAssetId: "asset-a" }]
      };
      const calls = [];
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/analysis/jobs") return { analysis: {
          job: { id: "analysis-a", status: "succeeded", sourceAssetId: "asset-a" }, receipts: []
        } };
        if (path === "/api/studio/analysis/history") return { history: { entries: [], reviews: [], duplicateGroups: [] } };
        if (path === "/api/studio/models/status") return { models: {
          mode: "offline-fallback", localOnly: true, degraded: true, artifacts: [],
          visionProbe: "not-run: runtime-unapproved"
        } };
        throw new Error("unexpected-call:" + path);
      };
      await startStudioAnalysis("asset-a", ["technical", "transcript", "vision"]);
      return calls;
    })()`, context) as Array<{ path: string; body: Record<string, unknown> }>;

    expect(result[0]?.path).toBe("/api/studio/analysis/jobs");
    expect(Object.keys(result[0]!.body).sort()).toEqual(["assetId", "layers", "operationKey"]);
    expect(result[0]!.body).toMatchObject({
      assetId: "asset-a",
      layers: ["technical", "transcript", "vision"],
    });
    expect(result[0]!.body).not.toHaveProperty("customerId");
    expect(result[0]!.body).not.toHaveProperty("sessionId");
    expect(result[0]!.body).not.toHaveProperty("sourcePath");
  });

  it("submits only allowed exact-bound review actions and refreshes analysis history", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" }
      };
      const calls = [];
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/analysis/review") return { review: { id: "review-a", ...options.body } };
        return { history: { entries: [], reviews: [], duplicateGroups: [] } };
      };
      const receipt = {
        jobId: "receipt-a", sourceAssetId: "asset-a", sourceDigest: "${"a".repeat(64)}",
        sourceStartMs: 1000, sourceEndMs: 5000
      };
      await submitStudioAnalysisReview(receipt, "correct-suggestion", "戒指特写", "戒指制作特写");
      await submitStudioAnalysisReview(receipt, "publish", "戒指特写", null);
      return calls;
    })()`, context) as Array<{ path: string; body: Record<string, unknown> }>;

    expect(result.map((call) => call.path)).toEqual([
      "/api/studio/analysis/review",
      "/api/studio/analysis/history",
    ]);
    expect(result[0]!.body).toMatchObject({
      action: "correct-suggestion",
      receiptJobId: "receipt-a",
      sourceAssetId: "asset-a",
      sourceStartMs: 1000,
      sourceEndMs: 5000,
      target: "戒指特写",
      correction: "戒指制作特写",
    });
  });

  it("submits a distinct demand-bound suitability event rather than a suggestion action", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" }
      };
      const calls = [];
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/analysis/suitability") return { suitability: { id: "suitability-a", ...options.body } };
        return { history: { entries: [], reviews: [], suitabilityReviews: [], duplicateGroups: [] } };
      };
      const receipt = {
        jobId: "receipt-a", sourceAssetId: "asset-a", sourceDigest: "${"a".repeat(64)}",
        sourceStartMs: 1000, sourceEndMs: 5000
      };
      await submitStudioMaterialSuitability(receipt, {
        demandId: "demand-a", matchAssessmentId: "match-a"
      }, "suitable", "人工确认适合");
      return calls;
    })()`, context) as Array<{ path: string; body: Record<string, unknown> }>;

    expect(result.map((call) => call.path)).toEqual([
      "/api/studio/analysis/suitability",
      "/api/studio/analysis/history",
    ]);
    expect(result[0]!.body).toMatchObject({
      decision: "suitable",
      demandId: "demand-a",
      matchAssessmentId: "match-a",
      receiptJobId: "receipt-a",
      sourceAssetId: "asset-a",
      sourceDigest: "a".repeat(64),
      sourceStartMs: 1000,
      sourceEndMs: 5000,
      reason: "人工确认适合",
    });
    expect(result[0]!.body).not.toHaveProperty("action");
  });

  it("contains no original deletion, model upload, cloud provider, or publishing request", async () => {
    const javascript = await readFile(join(publicRoot, "studio.js"), "utf8");

    expect(javascript).not.toMatch(/["'`]\/api\/studio\/(?:delete-original|models\/upload|providers\/upload|publish)/i);
    expect(javascript).not.toMatch(/data-(?:delete-original|upload-provider|publish)/i);
  });
});

async function loadStudioUiContext() {
  const elements = new Map<string, ReturnType<typeof fakeElement>>([
    ["#studio-status", fakeElement()],
    ["#studio-release", fakeElement()],
    ["#current-demand", fakeElement()],
    ["#materials", fakeElement()],
    ["#match-assessment", fakeElement()],
    ["#opportunities", fakeElement()],
    ["#brief", fakeElement()],
    ["#drafts", fakeElement()],
    ["#revision-proposal", fakeElement()],
    ["#studio-history", fakeElement()],
    ["#analysis-queue", fakeElement()],
    ["#analysis-history", fakeElement()],
    ["#model-status", fakeElement()],
  ]);
  const document = {
    addEventListener() {},
    querySelector: (selector: string) => elements.get(selector) ?? fakeElement(),
  };
  const context = createContext({
    document,
    window: { prompt: () => "修正建议" },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    crypto: { randomUUID: () => "ui-operation-1" },
    AbortController,
    console,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
  });
  runInContext(await readFile(join(publicRoot, "studio.js"), "utf8"), context);
  return context;
}

function fakeElement() {
  return {
    textContent: "",
    innerHTML: "",
    value: "",
    disabled: false,
    hidden: false,
    dataset: {} as Record<string, string>,
    addEventListener() {},
    querySelector: () => fakeElement(),
    querySelectorAll: () => [],
  };
}
