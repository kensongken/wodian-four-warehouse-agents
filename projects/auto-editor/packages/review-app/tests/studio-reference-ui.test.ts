import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { createContext, runInContext } from "node:vm";

import { describe, expect, it } from "vitest";

const publicRoot = join(process.cwd(), "packages/review-app/public");

describe("Mac reference sample workflow UI", () => {
  it("presents one semantic four-step production flow without a capture action", async () => {
    const html = await readFile(join(publicRoot, "studio.html"), "utf8");
    const section = extractReferenceSection(html);
    const labels = [
      "添加参考样片",
      "声明来源并拆解风格",
      "复核素材匹配",
      "生成四版并查看执行报告",
    ];
    const positions = labels.map((label) => section.indexOf(label));

    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((left, right) => left - right));
    expect(section).toContain('accept="video/mp4,video/quicktime,.mp4,.mov"');
    expect(section).toContain("从我的历史成片选择");
    expect(section).toContain("确认风格并开始匹配素材");
    expect(section).toContain("相近替代");
    expect(section).toContain("无法实现");
    expect(section).toContain("尚未开始生成视频");
    expect(section).not.toContain("补拍");
    expect(section).toContain('<output id="reference-rights-source-kind"');
    expect(section).not.toContain('<select id="reference-rights-source-kind"');
  });

  it("reuses one exact operation identity after mapping-confirm response loss", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const bodies = [];
      let attempt = 0;
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          sample: { id: "sample-a", version: 1, sourceDigest: "sample-digest" },
          rights: { id: "rights-a", version: 2, contentDigest: "rights-digest" },
          analysis: { id: "analysis-a", version: 3, contentDigest: "analysis-digest" },
          selection: { id: "selection-a", version: 4, contentDigest: "selection-digest", status: "confirmed" },
          mappingPlan: { id: "mapping-a", version: 5, contentDigest: "mapping-digest", permissionSetDigest: "permission-digest", status: "draft" }
        }
      };
      studioApi = async (_path, options) => {
        bodies.push(options.body);
        attempt += 1;
        if (attempt === 1) throw new Error("response-lost");
        return { mappingPlan: { ...studioState.reference.mappingPlan, status: "confirmed" } };
      };
      await confirmReferenceMapping();
      await confirmReferenceMapping();
      return {
        keys: bodies.map((body) => body.operationKey),
        status: studioState.reference.mappingPlan.status,
        cachedKeyCount: referenceOperationKeyCache.size,
      };
    })()`, context) as any;

    expect(result.keys).toHaveLength(2);
    expect(result.keys[0]).toBe(result.keys[1]);
    expect(result.keys[0]).toMatch(/^reference-mapping-confirm-[a-f0-9]{64}$/u);
    expect(result.status).toBe("confirmed");
    expect(result.cachedKeyCount).toBe(0);
  });

  it("binds every reference mutation key to its exact canonical input", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: emptyReferenceState(3),
      };
      const inputs = {
        upload: { sourceKind: "self-owned", file: { name: "a.mov", size: 8, type: "video/quicktime", lastModified: 1, digest: "file-a" } },
        history: { assetId: "asset-a", sourceKind: "self-owned" },
        rights: { sample: { id: "sample-a", version: 1, sourceDigest: "sample-a" }, sourceKind: "self-owned", reusableElements: ["rhythm"] },
        analysis: { sample: { id: "sample-a", version: 1, sourceDigest: "sample-a" }, rights: { id: "rights-a", version: 1, contentDigest: "rights-a" } },
        selection: { analysis: { id: "analysis-a", version: 1, contentDigest: "analysis-a" }, selectedRuleIds: ["rule-a"] },
        mapping: { selection: { id: "selection-a", version: 1, contentDigest: "selection-a", status: "confirmed" } },
        "mapping-confirm": { mappingPlan: { id: "mapping-a", version: 1, contentDigest: "mapping-a", permissionSetDigest: "permission-a", status: "draft" } },
        render: { mappingPlan: { id: "mapping-a", version: 2, contentDigest: "mapping-b", permissionSetDigest: "permission-a", status: "confirmed" } },
      };
      const rows = [];
      for (const [action, input] of Object.entries(inputs)) {
        const first = await referenceMutationOperationKey(action, input);
        const retry = await referenceMutationOperationKey(action, JSON.parse(JSON.stringify(input)));
        const changed = await referenceMutationOperationKey(action, { ...input, exactInputRevision: 2 });
        rows.push({ action, first, retry, changed });
      }
      return rows;
    })()`, context) as Array<{ action: string; first: string; retry: string; changed: string }>;

    expect(result.map((row) => row.action)).toEqual([
      "upload", "history", "rights", "analysis", "selection", "mapping", "mapping-confirm", "render",
    ]);
    for (const row of result) {
      expect(row.retry).toBe(row.first);
      expect(row.changed).not.toBe(row.first);
      expect(row.first).toMatch(new RegExp(`^reference-${row.action}-[a-f0-9]{64}$`, "u"));
    }
  });

  it("reuses the render key when report loading fails after a successful render", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const postKeys = [];
      let getAttempt = 0;
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          sample: { id: "sample-a", version: 1, sourceDigest: "sample-digest" },
          rights: { id: "rights-a", version: 2, contentDigest: "rights-digest" },
          analysis: { id: "analysis-a", version: 3, contentDigest: "analysis-digest" },
          selection: { id: "selection-a", version: 4, contentDigest: "selection-digest", status: "confirmed" },
          mappingPlan: { id: "mapping-a", version: 5, contentDigest: "mapping-digest", permissionSetDigest: "permission-digest", status: "confirmed" }
        }
      };
      const report = { id: "report-a", version: 1, status: "reviewable", artifacts: [] };
      studioApi = async (_path, options) => {
        if (options.method === "POST") {
          postKeys.push(options.body.operationKey);
          return { report };
        }
        getAttempt += 1;
        if (getAttempt === 1) throw new Error("report-fetch-lost");
        return { report };
      };
      await renderReferenceDrafts();
      await renderReferenceDrafts();
      return {
        postKeys,
        reportId: studioState.reference.executionReport?.id,
        cachedKeyCount: referenceOperationKeyCache.size,
      };
    })()`, context) as any;

    expect(result.postKeys).toHaveLength(2);
    expect(result.postKeys[0]).toBe(result.postKeys[1]);
    expect(result.postKeys[0]).toMatch(/^reference-render-[a-f0-9]{64}$/u);
    expect(result.reportId).toBe("report-a");
    expect(result.cachedKeyCount).toBe(0);
  });

  it.each(["409", "non-reviewable-200"])(
    "clears existing players when report loading discovers a concurrent withdrawal via %s",
    async (mode) => {
      const context = await loadStudioUiContext();
      const result = await runInContext(`(async () => {
      const responseMode = ${JSON.stringify(mode)};
      const artifacts = [
        { kind: "editorial-master", draftId: "draft-master", draftVersion: 1, outputDigest: "digest-a" },
        { kind: "xiaohongshu", draftId: "draft-xhs", draftVersion: 1, outputDigest: "digest-b" },
        { kind: "douyin", draftId: "draft-douyin", draftVersion: 1, outputDigest: "digest-d" },
        { kind: "wechat-channels", draftId: "draft-wechat", draftVersion: 1, outputDigest: "digest-c" },
      ];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          mappingPlan: {
            id: "mapping-a", version: 2, contentDigest: "mapping-digest",
            permissionSetDigest: "permission-digest", status: "confirmed",
          },
          executionReport: { id: "report-old", version: 1, status: "reviewable", artifacts },
          drafts: referenceDraftsFromReport({ id: "report-old", version: 1, status: "reviewable", artifacts }),
        },
      };
      studioApi = async (path, options) => {
        if (options.method === "POST") return { report: { id: "report-new" } };
        if (responseMode === "non-reviewable-200") {
          return { report: { id: "report-new", version: 2, status: "invalidated", artifacts } };
        }
        const error = new Error("reference-execution-report-not-current");
        error.status = 409;
        throw error;
      };
      await renderReferenceDrafts();
      return {
        promptDrafts: studioState.reference.drafts,
        executionReport: studioState.reference.executionReport,
        markup: document.querySelector("#reference-drafts")?.innerHTML,
        reportMarkup: document.querySelector("#reference-report")?.innerHTML,
        renderDisabled: document.querySelector("#render-reference-drafts")?.disabled,
        renderGateText: document.querySelector("#reference-render-gate")?.textContent,
        renderGateClass: document.querySelector("#reference-render-gate")?.className,
        generationText: document.querySelector("#reference-generation-state")?.textContent,
        status: document.querySelector("#reference-status")?.textContent,
      };
    })()`, context) as any;

      expect(result.promptDrafts.every((draft: any) =>
        draft.status === "invalidated" && draft.mediaUrl === "",
      )).toBe(true);
      expect(result.markup).not.toContain("<video");
      expect(result.markup).toMatch(/data-reference-revise="draft-xhs"[^>]* disabled/u);
      expect(result.markup).toMatch(/data-reference-approve="draft-xhs"[^>]* disabled/u);
      expect(result.executionReport).toMatchObject({ id: "report-old", status: "invalidated" });
      expect(result.reportMarkup).not.toContain("reviewable");
      expect(result.reportMarkup).toContain("invalidated");
      expect(result.renderDisabled).toBe(true);
      expect(result.renderGateText).toBe("版本已失效");
      expect(result.renderGateClass).not.toContain("is-ready");
      expect(result.generationText).toBe("四版已失效，播放与审批已关闭");
      expect(result.status).toBe("素材权限或版本已失效；播放与审批已关闭。");
    },
  );

  it("rejects a render response when the same mapping id advances to a new version", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      let resolvePost;
      let postStartedResolve;
      const postStarted = new Promise((resolve) => { postStartedResolve = resolve; });
      const calls = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          mappingPlan: { id: "mapping-a", version: 1, contentDigest: "digest-v1", permissionSetDigest: "permissions-v1", status: "confirmed" }
        }
      };
      studioApi = async (path, options) => {
        calls.push([options.method, path]);
        if (options.method === "POST") {
          postStartedResolve();
          return new Promise((resolve) => { resolvePost = resolve; });
        }
        return { report: { id: "report-stale", version: 1, artifacts: [] } };
      };
      const pending = renderReferenceDrafts();
      await postStarted;
      studioState.reference.mappingPlan = {
        ...studioState.reference.mappingPlan,
        version: 2,
        contentDigest: "digest-v2",
        permissionSetDigest: "permissions-v2",
      };
      resolvePost({ report: { id: "report-stale", version: 1, artifacts: [] } });
      await pending;
      return { calls, executionReport: studioState.reference.executionReport };
    })()`, context) as any;

    expect(result.calls).toEqual([["POST", "/api/studio/reference-drafts"]]);
    expect(result.executionReport).toBeNull();
  });

  it("clears every reference object and advances its scope epoch when the customer changes", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = {
        ...emptyStudioState("customer-a"),
        reference: {
          ...emptyReferenceState(7),
          sample: { id: "sample-a" }, rights: { id: "rights-a" },
          analysis: { id: "analysis-a" }, selection: { id: "selection-a" },
          mappingPlan: { id: "mapping-a" }, drafts: [{ id: "draft-a" }],
          executionReport: { id: "report-a" }
        }
      };
      clearStudioScope("customer-b");
      return studioState;
    })()`, context) as any;

    expect(result.customerId).toBe("customer-b");
    expect(result.reference.scopeEpoch).toBe(8);
    expect(result.reference).toMatchObject({
      sample: null,
      rights: null,
      analysis: null,
      selection: null,
      mappingPlan: null,
      drafts: [],
      executionReport: null,
      error: null,
    });
  });

  it("ignores a late sample-list response after the session or upstream binding changes", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      let resolveRequest;
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" }
      };
      studioApi = async () => new Promise((resolve) => { resolveRequest = resolve; });
      const pending = refreshReferenceLibrary();
      const oldGuard = captureReferenceRequestGuard();
      studioState.session = { token: "token-b", customerId: "customer-a", sessionId: "session-b" };
      studioState.reference.sample = { id: "sample-new" };
      resolveRequest({ samples: [{ id: "sample-stale" }] });
      await pending;
      return {
        samples: studioState.reference.samples,
        sample: studioState.reference.sample,
        oldGuardCurrent: ownsReferenceRequestGuard(oldGuard),
      };
    })()`, context) as any;

    expect(result.samples).toEqual([]);
    expect(result.sample).toEqual({ id: "sample-new" });
    expect(result.oldGuardCurrent).toBe(false);
  });

  it("commits only the newest library and history refresh responses", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const libraryResolvers = [];
      const historyResolvers = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" }
      };
      studioApi = async (path) => new Promise((resolve) => {
        (path.endsWith("reference-history") ? historyResolvers : libraryResolvers).push(resolve);
      });
      const libraryOld = refreshReferenceLibrary();
      const libraryNew = refreshReferenceLibrary();
      libraryResolvers[1]({ samples: [{ id: "sample-new" }] });
      await libraryNew;
      libraryResolvers[0]({ samples: [{ id: "sample-old" }] });
      await libraryOld;
      const historyOld = refreshReferenceHistory();
      const historyNew = refreshReferenceHistory();
      historyResolvers[1]({ assets: [{ assetId: "asset-new" }] });
      await historyNew;
      historyResolvers[0]({ assets: [{ assetId: "asset-old" }] });
      await historyOld;
      return {
        samples: studioState.reference.samples,
        historyAssets: studioState.reference.historyAssets,
      };
    })()`, context) as any;

    expect(result).toEqual({
      samples: [{ id: "sample-new" }],
      historyAssets: [{ assetId: "asset-new" }],
    });
  });

  it("does not write upload or history success after a customer switch during refresh", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const refreshResolvers = [];
      const refreshStartedResolvers = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" }
      };
      referenceUploadApi = async () => ({ sample: { id: "sample-upload", version: 1 } });
      studioApi = async (path) => {
        if (path.endsWith("from-history")) return { sample: { id: "sample-history", version: 1 } };
        return new Promise((resolve) => {
          refreshResolvers.push(resolve);
          refreshStartedResolvers.shift()?.();
        });
      };
      const uploadRefreshStarted = new Promise((resolve) => refreshStartedResolvers.push(resolve));
      const upload = uploadReferenceSample({
        name: "fixture.mov", size: 4, type: "video/quicktime", lastModified: 1,
        arrayBuffer: async () => new Uint8Array([1, 2, 3, 4]).buffer,
      }, "external-style-only");
      await uploadRefreshStarted;
      clearStudioScope("customer-b");
      refreshResolvers[0]({ samples: [{ id: "stale-upload" }] });
      await upload;
      const uploadStatus = document.querySelector("#reference-status").textContent;

      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-c", customerId: "customer-a", sessionId: "session-c" }
      };
      const historicalRefreshStarted = new Promise((resolve) => refreshStartedResolvers.push(resolve));
      const historical = registerHistoricalReference("asset-a", "self-owned");
      await historicalRefreshStarted;
      clearStudioScope("customer-c");
      refreshResolvers[1]({ samples: [{ id: "stale-history" }] });
      await historical;
      return {
        uploadStatus,
        historyStatus: document.querySelector("#reference-status").textContent,
        customerId: studioState.customerId,
        sample: studioState.reference.sample,
        samples: studioState.reference.samples,
      };
    })()`, context) as any;

    expect(result.uploadStatus).not.toContain("样片已入库");
    expect(result.historyStatus).not.toContain("历史成片已登记");
    expect(result).toMatchObject({ customerId: "customer-c", sample: null, samples: [] });
  });

  it("keeps mapping and rendering actions locked until their confirmations exist", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          sample: { id: "sample-a" },
          analysis: { id: "analysis-a", rules: [{ id: "rule-a" }] },
          selectedRuleIds: ["rule-a"]
        }
      };
      renderStudioState();
      const beforeSelection = document.querySelector("#create-reference-mapping").disabled;
      studioState.reference.selection = { id: "selection-a", status: "confirmed" };
      renderStudioState();
      const afterSelection = document.querySelector("#create-reference-mapping").disabled;
      studioState.reference.mappingPlan = { id: "mapping-a", status: "draft" };
      renderStudioState();
      const beforeMappingConfirmation = document.querySelector("#render-reference-drafts").disabled;
      studioState.reference.mappingPlan = { id: "mapping-a", status: "confirmed" };
      renderStudioState();
      const afterMappingConfirmation = document.querySelector("#render-reference-drafts").disabled;
      studioState.reference.busyAction = "render";
      renderStudioState();
      const uploadDisabledWhileBusy = document.querySelector('#reference-upload-form button[type="submit"]').disabled;
      const historyRefreshDisabledWhileBusy = document.querySelector("#refresh-reference-history").disabled;
      return {
        beforeSelection, afterSelection, beforeMappingConfirmation, afterMappingConfirmation,
        uploadDisabledWhileBusy, historyRefreshDisabledWhileBusy,
      };
    })()`, context) as any;

    expect(result).toEqual({
      beforeSelection: true,
      afterSelection: false,
      beforeMappingConfirmation: true,
      afterMappingConfirmation: false,
      uploadDisabledWhileBusy: true,
      historyRefreshDisabledWhileBusy: true,
    });
  });

  it("shows server capability and warning codes verbatim while separating execution dimensions", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const analysisMarkup = renderReferenceAnalysis({
        id: "analysis-a", status: "degraded", confidence: 0.42,
        cuts: [1000, 2500], sections: [{ id: "s1", startMs: 0, endMs: 3000, summary: "开场" }],
        transcriptSpans: [], beatMarkers: [500, 1500],
        transitions: [{ id: "t1", atMs: 1000, kind: "cut", confidence: 0.9 }],
        rules: [{ id: "rule-a", kind: "rhythm", description: "短镜头开场", referenceStartMs: 0, referenceEndMs: 3000 }],
        warnings: ["reference.audio-evidence-missing", "synthetic-fixture-only"],
        capability: { mode: "offline-fallback", label: "离线/模拟分析", version: 2, provider: null }
      });
      const reportMarkup = renderReferenceExecutionReport({
        id: "report-a", version: 3, publishClaimed: false,
        rhythm: { status: "matched", summaryCode: "reference.rhythm.matched", evidenceRefs: [] },
        transitions: { status: "substituted", summaryCode: "reference.transition.substituted", evidenceRefs: [] },
        narrative: { status: "unachievable", summaryCode: "reference.narrative.unachievable", evidenceRefs: [] },
        audio: { status: "matched", summaryCode: "reference.audio.matched", evidenceRefs: [] },
        mappingCounts: { matched: 2, substituted: 1, unachievable: 1 },
        platformChanges: [
          { kind: "xiaohongshu", changeCodes: ["platform.xiaohongshu.reorder-opening"] },
          { kind: "wechat-channels", changeCodes: ["platform.wechat-channels.extend-context"] }
        ],
        artifacts: []
      });
      return { analysisMarkup, reportMarkup };
    })()`, context) as any;

    expect(result.analysisMarkup).toContain("离线/模拟分析");
    expect(result.analysisMarkup).toContain("reference.audio-evidence-missing");
    expect(result.analysisMarkup).toContain("synthetic-fixture-only");
    expect(result.reportMarkup).toContain("节奏");
    expect(result.reportMarkup).toContain("转场");
    expect(result.reportMarkup).toContain("叙事");
    expect(result.reportMarkup).toContain("音频");
    expect(result.reportMarkup).toContain("平台主动调整");
    expect(result.reportMarkup).not.toMatch(/总分|总相似度|totalSimilarity/u);
  });

  it("derives shot count and average duration from sections, not transition cuts", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderReferenceAnalysis({
      id: "analysis-sections", version: 1, status: "degraded", confidence: 0.8,
      cuts: [1000], beatMarkers: [], transcriptSpans: [],
      sections: [
        { id: "s1", startMs: 0, endMs: 1000, summary: "一" },
        { id: "s2", startMs: 1000, endMs: 3000, summary: "二" },
        { id: "s3", startMs: 3000, endMs: 6000, summary: "三" }
      ],
      transitions: [
        { id: "cut", atMs: 1000, kind: "cut", confidence: 0.9 },
        { id: "fade", atMs: 3000, kind: "fade", confidence: 0.9 },
        { id: "cross", atMs: 4500, kind: "crossfade", confidence: 0.9 }
      ],
      rules: [{ id: "r1", kind: "rhythm", description: "节奏", referenceStartMs: 0, referenceEndMs: 6000 }],
      warnings: ["synthetic-browser-pilot-evidence-only"],
      capability: { mode: "offline-fallback", label: "离线/模拟分析", provider: null, version: 1 }
    })`, context) as string;

    expect(markup).toContain("<strong>3</strong><small>镜头数</small>");
    expect(markup).toContain("<strong>2.00 秒</strong><small>平均镜头时长</small>");
    expect(markup).toContain("cut、fade、crossfade");
  });

  it("keeps keyed sample and draft media markup intact across unrelated renders", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = {
        ...emptyStudioState("customer-a"),
        reference: {
          ...emptyReferenceState(1),
          sample: { id: "sample-a", version: 1, sourceDigest: "digest-a", sourceKind: "external-style-only" },
          drafts: [{ draftId: "draft-a", version: 1, kind: "editorial-master", label: "原视频母版", outputDigest: "output-a", mediaUrl: "/draft-a" }]
        }
      };
      renderStudioState();
      const sample = document.querySelector("#reference-sample-card");
      const drafts = document.querySelector("#reference-drafts");
      sample.innerHTML = "sample-node-preserved";
      drafts.innerHTML = "draft-node-preserved";
      studioState.reference.error = "unrelated-error";
      renderStudioState();
      const same = { sample: sample.innerHTML, drafts: drafts.innerHTML };
      studioState.reference.sample = { ...studioState.reference.sample, id: "sample-b" };
      studioState.reference.drafts = [{ ...studioState.reference.drafts[0], draftId: "draft-b", mediaUrl: "/draft-b" }];
      renderStudioState();
      return { same, changed: { sample: sample.innerHTML, drafts: drafts.innerHTML } };
    })()`, context) as any;

    expect(result.same).toEqual({ sample: "sample-node-preserved", drafts: "draft-node-preserved" });
    expect(result.changed.sample).toContain("sample-b");
    expect(result.changed.drafts).toContain("draft-b");
  });

  it("posts the confirmed mapping to the render API and loads the exact report", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          sample: { id: "sample-a" }, rights: { id: "rights-a" },
          analysis: { id: "analysis-a" }, selection: { id: "selection-a", status: "confirmed" },
          mappingPlan: { id: "mapping-a", status: "confirmed" }
        }
      };
      const report = {
        id: "report-a", version: 1, status: "reviewable",
        artifacts: [
          { kind: "editorial-master", draftId: "draft-master", outputDigest: "a" },
          { kind: "xiaohongshu", draftId: "draft-xhs", outputDigest: "b" },
          { kind: "wechat-channels", draftId: "draft-wechat", outputDigest: "c" }
        ]
      };
      studioApi = async (path, options) => {
        calls.push({ path, method: options.method, body: options.body });
        return options.method === "GET" ? { report } : { report };
      };
      await renderReferenceDrafts();
      return { calls, state: studioState.reference };
    })()`, context) as any;

    expect(result.calls.map((call: any) => [call.method, call.path])).toEqual([
      ["POST", "/api/studio/reference-drafts"],
      ["GET", "/api/studio/reference-execution-reports/report-a"],
    ]);
    expect(result.calls[0].body.mappingPlanId).toBe("mapping-a");
    expect(result.state.executionReport.id).toBe("report-a");
    expect(result.state.drafts.map((draft: any) => draft.mediaUrl)).toEqual([
      "/api/studio/reference-drafts/draft-master/media",
      "/api/studio/reference-drafts/draft-xhs/media",
      "/api/studio/reference-drafts/draft-wechat/media",
    ]);
  });

  it("renders four independently reviewable cards without approve-all or publish actions", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = {
        ...emptyStudioState("customer-a"),
        reference: {
          ...emptyReferenceState(1),
          executionReport: { id: "report-a", version: 3 },
          drafts: [
            { draftId: "draft-master", draftVersion: 4, kind: "editorial-master", label: "原视频母版", outputDigest: "digest-a", mediaUrl: "/master", status: "reviewable" },
            { draftId: "draft-xhs", draftVersion: 5, kind: "xiaohongshu", label: "小红书版", outputDigest: "digest-b", mediaUrl: "/xhs", status: "approved" },
            { draftId: "draft-douyin", draftVersion: 5, kind: "douyin", label: "抖音版", outputDigest: "digest-d", mediaUrl: "/douyin", status: "reviewable" },
            { draftId: "draft-wechat", draftVersion: 6, kind: "wechat-channels", label: "视频号版", outputDigest: "digest-c", mediaUrl: "/wechat", status: "invalidated" },
          ],
        },
      };
      syncReferenceDraftControls();
      return renderReferenceDraftCards(studioState.reference.drafts);
    })()`, context) as string;

    expect((markup.match(/data-reference-revision-prompt=/gu) || [])).toHaveLength(4);
    expect((markup.match(/data-reference-approval-confirm=/gu) || [])).toHaveLength(4);
    expect((markup.match(/data-reference-revise=/gu) || [])).toHaveLength(4);
    expect((markup.match(/data-reference-approve=/gu) || [])).toHaveLength(4);
    expect(markup).toContain("待人工审片");
    expect(markup).toContain("已批准");
    expect(markup).toContain("已失效");
    expect(markup).toContain("我已确认正在审批当前精确版本");
    expect(markup).not.toMatch(/一键批准|批准全部|发布按钮/u);
    expect(markup).not.toContain('<video controls preload="metadata" src="/wechat"');
  });

  it("posts an exact revision binding and resets per-card controls when identity changes", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      const artifacts = [
        { kind: "editorial-master", draftId: "draft-master", draftVersion: 1, outputDigest: "digest-a" },
        { kind: "xiaohongshu", draftId: "draft-xhs", draftVersion: 2, outputDigest: "digest-b" },
        { kind: "wechat-channels", draftId: "draft-wechat", draftVersion: 1, outputDigest: "digest-c" },
      ];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          mappingPlan: { id: "mapping-a", version: 1, contentDigest: "mapping", permissionSetDigest: "permissions", status: "confirmed" },
          executionReport: { id: "report-a", version: 7, artifacts },
          drafts: referenceDraftsFromReport({ id: "report-a", version: 7, artifacts })
            .map((draft) => ({ ...draft, status: "approved" })),
        },
      };
      syncReferenceDraftControls();
      setReferenceDraftPrompt("draft-xhs", "开头缩短 1 秒");
      setReferenceDraftApprovalConfirmation("draft-xhs", true);
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        const nextArtifacts = artifacts.map((artifact) => artifact.kind === "xiaohongshu"
          ? { ...artifact, draftId: "draft-xhs-v3", draftVersion: 3, outputDigest: "digest-b-v3" }
          : artifact);
        return { report: { id: "report-b", version: 8, status: "reviewable", artifacts: nextArtifacts } };
      };
      await reviseReferenceDraft("draft-xhs");
      return {
        calls,
        controls: studioState.reference.draftControls,
        report: studioState.reference.executionReport,
        statuses: studioState.reference.drafts.map((draft) => draft.status),
      };
    })()`, context) as any;

    expect(result.calls).toHaveLength(1);
    expect(result.calls[0].path).toBe("/api/studio/reference-drafts/draft-xhs/revise");
    expect(result.calls[0].body).toEqual({
      reportId: "report-a",
      reportVersion: 7,
      draftVersion: 2,
      draftDigest: "digest-b",
      prompt: "开头缩短 1 秒",
      operationKey: result.calls[0].body.operationKey,
    });
    expect(result.calls[0].body.operationKey).toMatch(/^reference-revise-xiaohongshu-[a-f0-9]{64}$/u);
    expect(result.report).toMatchObject({ id: "report-b", version: 8 });
    expect(result.controls.xiaohongshu).toMatchObject({ prompt: "", confirmed: false });
    expect(result.statuses).toEqual(["reviewable", "reviewable", "reviewable"]);
  });

  it("retains a failed prompt but clears every player and disables review after a withdrawal conflict", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const artifacts = [
        { kind: "editorial-master", draftId: "draft-master", draftVersion: 1, outputDigest: "digest-a" },
        { kind: "xiaohongshu", draftId: "draft-xhs", draftVersion: 1, outputDigest: "digest-b" },
        { kind: "wechat-channels", draftId: "draft-wechat", draftVersion: 1, outputDigest: "digest-c" },
      ];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          mappingPlan: { id: "mapping-a", version: 1, contentDigest: "mapping", permissionSetDigest: "permissions", status: "confirmed" },
          executionReport: { id: "report-a", version: 1, artifacts },
          drafts: referenceDraftsFromReport({ id: "report-a", version: 1, artifacts }),
        },
      };
      syncReferenceDraftControls();
      setReferenceDraftPrompt("draft-xhs", "开头缩短 1 秒");
      studioApi = async () => {
        const error = new Error("reference-render-required");
        error.status = 409;
        throw error;
      };
      await reviseReferenceDraft("draft-xhs");
      return {
        prompt: studioState.reference.draftControls.xiaohongshu.prompt,
        drafts: studioState.reference.drafts,
        markup: renderReferenceDraftCards(studioState.reference.drafts),
        domMarkup: document.querySelector("#reference-drafts")?.innerHTML,
        status: document.querySelector("#reference-status")?.textContent,
      };
    })()`, context) as any;

    expect(result.prompt).toBe("开头缩短 1 秒");
    expect(result.drafts.every((draft: any) => draft.status === "invalidated" && !draft.mediaUrl)).toBe(true);
    expect(result.markup).not.toContain("<video");
    expect(result.markup).toMatch(/data-reference-revise="draft-xhs"[^>]* disabled/u);
    expect(result.markup).toMatch(/data-reference-approve="draft-xhs"[^>]* disabled/u);
    expect(result.domMarkup).not.toContain("<video");
    expect(result.domMarkup).toMatch(/data-reference-revise="draft-xhs"[^>]* disabled/u);
    expect(result.domMarkup).toMatch(/data-reference-approve="draft-xhs"[^>]* disabled/u);
    expect(result.status).toBe("素材权限或版本已失效；播放与审批已关闭。");
  });

  it("approves one exact version through its own endpoint and applies returned review statuses", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      const artifacts = [
        { kind: "editorial-master", draftId: "draft-master", draftVersion: 4, outputDigest: "digest-a" },
        { kind: "xiaohongshu", draftId: "draft-xhs", draftVersion: 5, outputDigest: "digest-b" },
        { kind: "wechat-channels", draftId: "draft-wechat", draftVersion: 6, outputDigest: "digest-c" },
      ];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        reference: {
          ...emptyReferenceState(1),
          mappingPlan: { id: "mapping-a", version: 1, contentDigest: "mapping", permissionSetDigest: "permissions", status: "confirmed" },
          executionReport: { id: "report-a", version: 9, status: "reviewable", artifacts },
          drafts: referenceDraftsFromReport({ id: "report-a", version: 9, status: "reviewable", artifacts }),
        },
      };
      syncReferenceDraftControls();
      setReferenceDraftApprovalConfirmation("draft-master", true);
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        return {
          approval: { id: "approval-a", status: "active", publishClaimed: false },
          review: {
            status: "reviewable",
            publishClaimed: false,
            bindings: { executionReport: { id: "report-a", version: 9 } },
            artifacts: artifacts.map((artifact) => ({
              ...artifact, status: artifact.kind === "editorial-master" ? "approved" : "reviewable",
            })),
          },
        };
      };
      await approveReferenceDraft("draft-master");
      return { calls, drafts: studioState.reference.drafts, controls: studioState.reference.draftControls };
    })()`, context) as any;

    expect(result.calls[0].path).toBe("/api/studio/reference-drafts/draft-master/approve");
    expect(result.calls[0].body).toEqual({
      reportId: "report-a",
      reportVersion: 9,
      draftVersion: 4,
      draftDigest: "digest-a",
      operationKey: result.calls[0].body.operationKey,
    });
    expect(result.calls[0].body.operationKey).toMatch(/^reference-approve-editorial-master-[a-f0-9]{64}$/u);
    expect(result.drafts.map((draft: any) => draft.status)).toEqual(["approved", "reviewable", "reviewable"]);
    expect(result.controls["editorial-master"].confirmed).toBe(false);
  });

  it.each(["non-reviewable-response", "file-digest-mismatch"])(
    "fails every approval control closed for %s",
    async (mode) => {
      const context = await loadStudioUiContext();
      const result = await runInContext(`(async () => {
        const responseMode = ${JSON.stringify(mode)};
        const artifacts = [
          { kind: "editorial-master", draftId: "draft-master", draftVersion: 4, outputDigest: "digest-a" },
          { kind: "xiaohongshu", draftId: "draft-xhs", draftVersion: 5, outputDigest: "digest-b" },
          { kind: "wechat-channels", draftId: "draft-wechat", draftVersion: 6, outputDigest: "digest-c" },
        ];
        const report = { id: "report-a", version: 9, status: "reviewable", artifacts };
        studioState = {
          ...emptyStudioState("customer-a"),
          session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
          reference: {
            ...emptyReferenceState(1),
            mappingPlan: { id: "mapping-a", version: 1, contentDigest: "mapping", permissionSetDigest: "permissions", status: "confirmed" },
            executionReport: report,
            drafts: referenceDraftsFromReport(report),
          },
        };
        syncReferenceDraftControls();
        setReferenceDraftApprovalConfirmation("draft-master", true);
        studioApi = async () => {
          if (responseMode === "file-digest-mismatch") {
            const error = new Error("reference-approval-file-digest-mismatch");
            error.status = 409;
            throw error;
          }
          return {
            approval: { id: "approval-a", status: "active", publishClaimed: false },
            review: {
              status: "invalidated",
              publishClaimed: false,
              bindings: { executionReport: { id: "report-a", version: 9 } },
              artifacts: artifacts.map((artifact) => ({
                ...artifact,
                status: artifact.kind === "editorial-master" ? "approved" : "reviewable",
              })),
            },
          };
        };
        await approveReferenceDraft("draft-master");
        return {
          report: studioState.reference.executionReport,
          review: studioState.reference.review,
          drafts: studioState.reference.drafts,
          renderDisabled: document.querySelector("#render-reference-drafts")?.disabled,
        };
      })()`, context) as any;

      expect(result.report.status).toBe("invalidated");
      expect(result.review).toBeNull();
      expect(result.drafts.every((draft: any) =>
        draft.status === "invalidated" && draft.mediaUrl === "",
      )).toBe(true);
      expect(result.renderDisabled).toBe(true);
    },
  );
});

function extractReferenceSection(html: string): string {
  const match = html.match(/<section[^>]+id="reference-workflow"[\s\S]*?<\/section>/u);
  return match?.[0] ?? "";
}

async function loadStudioUiContext() {
  const elements = new Map<string, ReturnType<typeof fakeElement>>();
  const document = {
    addEventListener() {},
    querySelector(selector: string) {
      if (!elements.has(selector)) elements.set(selector, fakeElement());
      return elements.get(selector)!;
    },
  };
  const context = createContext({
    document,
    window: { prompt: () => null },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    crypto: { randomUUID: () => "reference-ui-operation", subtle: webcrypto.subtle },
    AbortController,
    FormData,
    TextEncoder,
    console,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
  });
  const source = await readFile(join(publicRoot, "studio.js"), "utf8");
  runInContext(source, context);
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
