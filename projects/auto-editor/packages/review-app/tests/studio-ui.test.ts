import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { createContext, runInContext } from "node:vm";

import { describe, expect, it } from "vitest";

const publicRoot = join(process.cwd(), "packages/review-app/public");

describe("private content studio UI", () => {
  it("explains caption quality failures without exposing internal error codes", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`({
      firstDraft: studioErrorMessage(new Error("draft-quality-check-failed")),
      captionRevision: studioErrorMessage(new Error("caption-revision-quality-check-failed")),
      shareVersion: studioErrorMessage(new Error("share-optimization-quality-check-failed")),
      unknown: studioErrorMessage(new Error("unexpected-local-error"))
    })`, context) as { firstDraft: string; captionRevision: string; shareVersion: string; unknown: string };

    expect(result.firstDraft).toContain("已停止生成并清理失败文件");
    expect(result.firstDraft).not.toContain("draft-quality-check-failed");
    expect(result.captionRevision).toContain("失败版本已清理，不会出现在作品里");
    expect(result.captionRevision).not.toContain("caption-revision-quality-check-failed");
    expect(result.shareVersion).toContain("原作品没有修改");
    expect(result.shareVersion).not.toContain("share-optimization-quality-check-failed");
    expect(result.unknown).toContain("unexpected-local-error");
  });

  it("shows demand, evidence match, customer feedback, confirmation, and history in order", async () => {
    const html = await readFile(join(publicRoot, "studio.html"), "utf8");
    const stepMarkers = ["demand", "materials", "match", "brief", "drafts", "revision"];
    const positions = stepMarkers.map((step) => html.indexOf(`data-studio-step="${step}"`));

    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    for (const label of ["告诉我这次想讲什么", "看看现有素材合不合适", "确认要用的素材", "我的作品", "按你的意见修改"]) {
      expect(html).toContain(label);
    }
    expect(html).toContain("离线/模拟分析");
    expect(html).toContain("不会自动发布");
    expect(html).toContain('id="studio-release"');
    expect(html).toContain('id="studio-demand-form"');
    expect(html).toContain('id="match-assessment"');
    expect(html).toContain('id="revision-proposal"');
    expect(html).toContain('id="studio-history"');
    expect(html).toContain('aria-label="常用功能"');
    expect(html).toContain('aria-label="制作进度"');
    expect(html).toContain('window.location.protocol === "file:"');
    expect(html).toContain('window.location.replace("http://127.0.0.1:4273/studio")');
    expect(html).toContain("1</span><strong>开始整理");
    expect(html).toContain("2</span><strong>确认素材");
    expect(html).toContain("3</span><strong>审核作品");
    expect(html).toContain("说一句你的想法");
    expect(html).toContain(">开始</button>");
    expect(html).not.toContain('name="prompt" required');
    expect(html).toContain("查看作品");
    expect(html).toContain("添加素材");
    expect(html).toContain("连接硬件");
    expect(html).toContain("<summary>更多</summary>");
    expect(html).not.toContain('data-studio-step="analysis"');
    expect(html).toContain("补充更多要求（可选）");
    expect(html).toContain("先播放看看，再决定通过、让 AI 修改，或送到剪映精修");
    expect(html).toContain("先播放看看，再决定通过、让 AI 修改，或送到剪映精修");
    expect(html).toContain("看视频");
    expect(html).toContain("选下一步");
    expect(html).toContain('id="highlight-evaluations"');
    expect(html).toContain("人工高光基准");
    expect(html).toContain('<details class="advanced-review-drawer">');
    expect(html).toContain("高级验收");
    expect(html).toContain("模型选片测试与人工标注");
    expect(html).not.toContain('<details class="advanced-review-drawer" open>');
    expect(html).toContain("不会自动训练，也不会发布");
    expect(html).toContain(">用这些素材生成初稿</button>");
    expect(html).toContain('id="brief-adjust-demand"');
    expect(html).toContain("换素材");
    expect(html).toContain("去素材库补充");
    expect(html).toContain("调整想法");
    expect(html).not.toMatch(/<button[^>]*>\s*(?:立即)?发布\s*<\/button>/);
  });

  it("shows the honest highlight gate before a human supplies the answer", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`renderHighlightEvaluations([{
      sourceAssetId: "asset-a", sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 17933,
      status: "awaiting-human-label", threshold: { minimumIoU: 0.5 }, labels: [], modelCandidates: [], scores: [],
      publishable: false, trainingEligible: false, boundary: "human-label-required-model-evaluation-only"
    }], [{ id: "card-a", sourceAssetId: "asset-a", fileName: "黄昏飞行.mp4" }])`, context) as string;
    expect(html).toContain("待人工标注，不能评价模型选片");
    expect(html).toContain("系统不会替你编一个高光时间");
    expect(html).toContain("训练资格：否 · 自动发布：否");
    expect(html).toContain('data-highlight-form="card-a"');
  });

  it("shows every real video in the human highlight queue and excludes synthetic fixtures", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`renderHighlightEvaluations([
      { sourceAssetId: "real-a", sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 18000,
        status: "passed", labels: [{ sourceStartMs: 3000, sourceEndMs: 9000, reason: "晚霞" }], modelCandidates: [], scores: [] },
      { sourceAssetId: "real-b", sourceDigest: "${"b".repeat(64)}", sourceStartMs: 0, sourceEndMs: 22000,
        status: "awaiting-human-label", labels: [], modelCandidates: [], scores: [] },
      { sourceAssetId: "fixture-a", sourceDigest: "${"c".repeat(64)}", sourceStartMs: 0, sourceEndMs: 10500,
        status: "awaiting-human-label", labels: [], modelCandidates: [], scores: [] }
    ], [
      { id: "card-a", sourceAssetId: "real-a", displayName: "飞机.mp4", mediaUrl: "/plane.mp4" },
      { id: "card-b", sourceAssetId: "real-b", displayName: "机械手.mp4", mediaUrl: "/robot.mp4" },
      { id: "card-fixture", sourceAssetId: "fixture-a", fileName: "synthetic-color-tone-fixture-1.mp4", mediaUrl: "/fixture.mp4" }
    ], { selectedAssetIds: ["real-b"] }, { labeledVideos: 1, remainingVideos: 9, attemptedVideos: 1, failureStages: [], automaticEditingEligible: false })`, context) as string;

    expect(html).toContain("人工高光待确认队列");
    expect(html).toContain("待确认 1 条");
    expect(html).toContain("当前队列展示 2 条非演示视频：1 条已标注，1 条待标注");
    expect(html).toContain("本轮只处理现有素材，不要求客户新增拍摄");
    expect(html).toContain("另排除 1 条合成演示素材");
    expect(html).toContain("飞机.mp4");
    expect(html).toContain("机械手.mp4");
    expect(html).toContain('src="/robot.mp4"');
    expect(html).toContain("当前作品正在使用这条素材");
    expect(html).not.toContain("synthetic-color-tone-fixture-1.mp4");
  });

  it("lets a human bring in model times or mark the current frame without submitting a label", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const values = { startSeconds: { value: "", min: "0", max: "22.11" }, endSeconds: { value: "", min: "0", max: "22.11" }, reason: { value: "人工仍未填写" } };
      const status = { dataset: { highlightFormStatus: "card-b" }, textContent: "" };
      const card = { querySelectorAll: () => [status] };
      const form = {
        dataset: { highlightForm: "card-b" }, elements: { namedItem: (name) => values[name] },
        closest: () => card, scrollIntoView() {}
      };
      const candidateApplied = applyHighlightCandidateToForm(form, 3.126, 8.904);
      const afterCandidate = { start: values.startSeconds.value, end: values.endSeconds.value, reason: values.reason.value, status: status.textContent };
      const startMarked = markHighlightBoundaryFromVideo(form, { currentTime: 5.4321 }, "start");
      const endMarked = markHighlightBoundaryFromVideo(form, { currentTime: 10.6789 }, "end");
      return { candidateApplied, startMarked, endMarked, afterCandidate,
        final: { start: values.startSeconds.value, end: values.endSeconds.value, reason: values.reason.value, status: status.textContent } };
    })()`, context) as any;

    expect(result.candidateApplied).toBe(true);
    expect(result.afterCandidate).toMatchObject({ start: "3.13", end: "8.90", reason: "人工仍未填写" });
    expect(result.afterCandidate.status).toContain("系统不会自动提交");
    expect(result.startMarked).toBe(true);
    expect(result.endMarked).toBe(true);
    expect(result.final).toMatchObject({ start: "5.43", end: "10.68", reason: "人工仍未填写" });
    expect(result.final.status).toContain("仍需人工核对时间和理由");
  });

  it("renders current-frame controls and a non-submitting model candidate shortcut", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`renderHighlightEvaluations([{
      sourceAssetId: "asset-b", sourceDigest: "${"b".repeat(64)}", sourceStartMs: 0, sourceEndMs: 22000,
      status: "awaiting-human-label", labels: [], scores: [],
      modelCandidates: [{ startMs: 3100, endMs: 8900, text: "模型看到动作变化", receiptJobId: "vision-job-b" }]
    }], [{ id: "card-b", sourceAssetId: "asset-b", displayName: "机械手.mp4", mediaUrl: "/robot.mp4" }])`, context) as string;

    expect(html).toContain('data-highlight-video="card-b"');
    expect(html).toContain('data-highlight-mark-start="card-b"');
    expect(html).toContain('data-highlight-mark-end="card-b"');
    expect(html).toContain('data-use-highlight-candidate="card-b"');
    expect(html).toContain('data-highlight-start="3.1"');
    expect(html).toContain('data-highlight-end="8.9"');
    expect(html).toContain('data-accept-highlight-candidate="card-b"');
    expect(html).toContain('data-reject-highlight-candidate="card-b"');
    expect(html).toContain('data-highlight-receipt="vision-job-b"');
    expect(html).toContain('data-highlight-start-ms="3100"');
    expect(html).toContain("我已看过，确认这段");
    expect(html).toContain("这段不合适");
    expect(html).toContain("需要调整，带入下方");
    expect(html).toContain("为什么这是高光");
    expect(html).toContain("提交我的人工判断并评分");
    expect(html).toContain("重新分析模型候选");
    expect(html).toContain('name="humanConfirmed"');
    expect(html).toContain("我已亲自看过这段视频");
    expect(html).toContain("data-highlight-submit disabled");
  });

  it("submits one explicit candidate confirmation with its exact model receipt", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), busy: false,
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        return { evaluations: [], profiles: [], batch: { labeledVideos: 1 } };
      };
      renderStudioState = () => {};
      focusNextHighlightAction = () => true;
      const control = { dataset: {
        acceptHighlightCandidate: "card-b", highlightReceipt: "vision-job-b",
        highlightStartMs: "3100", highlightEndMs: "8900"
      }};
      await acceptStudioHighlightCandidate(control);
      return { calls, batch: studioState.highlightBatch };
    })()`, context) as any;

    expect(result.calls).toHaveLength(1);
    expect(result.calls[0]).toMatchObject({
      path: "/api/studio/highlights/accept-candidate",
      body: {
        materialCardId: "card-b", receiptJobId: "vision-job-b",
        sourceStartMs: 3100, sourceEndMs: 8900, humanConfirmed: true,
      },
    });
    expect(result.calls[0].body.operationKey).toMatch(/^highlight-candidate-accept-/);
    expect(result.batch).toEqual({ labeledVideos: 1 });
  });

  it("submits one receipt-bound rejection without turning it into a human highlight", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), busy: false,
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        return { evaluations: [], rejections: [{ id: "reject-a" }], profiles: [],
          batch: { labeledVideos: 0, automaticEditingEligible: false } };
      };
      renderStudioState = () => {};
      const control = { dataset: {
        rejectHighlightCandidate: "card-b", highlightReceipt: "vision-job-b",
        highlightStartMs: "3100", highlightEndMs: "8900"
      }};
      await rejectStudioHighlightCandidate(control);
      return { calls, rejections: studioState.highlightCandidateRejections,
        batch: studioState.highlightBatch };
    })()`, context) as any;

    expect(result.calls).toHaveLength(1);
    expect(result.calls[0]).toMatchObject({
      path: "/api/studio/highlights/reject-candidate",
      body: {
        materialCardId: "card-b", receiptJobId: "vision-job-b",
        sourceStartMs: 3100, sourceEndMs: 8900, humanConfirmed: true,
      },
    });
    expect(result.calls[0].body.operationKey).toMatch(/^highlight-candidate-reject-/);
    expect(result.rejections).toEqual([{ id: "reject-a" }]);
    expect(result.batch).toEqual({ labeledVideos: 0, automaticEditingEligible: false });
  });

  it("renders a rejected candidate as negative feedback and disables repeat decisions", async () => {
    const context = await loadStudioUiContext();
    const digest = "b".repeat(64);
    const html = runInContext(`renderHighlightEvaluations([{
      sourceAssetId: "asset-b", sourceDigest: "${digest}", sourceStartMs: 0, sourceEndMs: 22000,
      status: "awaiting-human-label", labels: [], scores: [],
      modelCandidates: [{ startMs: 3100, endMs: 8900, text: "模型看到动作变化", receiptJobId: "vision-job-b" }]
    }], [{ id: "card-b", sourceAssetId: "asset-b", sourceDigest: "${digest}",
      displayName: "机械手.mp4", mediaUrl: "/robot.mp4" }], null, null, [], [{
      id: "reject-a", sourceAssetId: "asset-b", sourceDigest: "${digest}",
      sourceStartMs: 3100, sourceEndMs: 8900, receiptJobId: "vision-job-b",
      reason: "not-a-highlight", provenance: "human-confirmed", createdAt: "2026-10-15T00:00:00.000Z"
    }])`, context) as string;

    expect(html).toContain("已记录 1 条模型候选否决");
    expect(html).toContain("人工已标记：不适合作为高光");
    expect(html).toMatch(/data-accept-highlight-candidate="card-b"[^>]* disabled/);
    expect(html).toMatch(/data-reject-highlight-candidate="card-b"[^>]* disabled/);
    expect(html).toContain('data-use-highlight-candidate="card-b"');
  });

  it("builds a batch draft plan only from existing real materials with model candidates", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const a = "a".repeat(64), b = "b".repeat(64), c = "c".repeat(64), d = "d".repeat(64);
      studioState = { ...emptyStudioState("customer-a"),
        materials: [
          { id: "card-a", sourceAssetId: "asset-a", sourceDigest: a, displayName: "待复核.mp4", observations: [] },
          { id: "card-b", sourceAssetId: "asset-b", sourceDigest: b, displayName: "无候选.mp4", observations: [] },
          { id: "card-c", sourceAssetId: "asset-c", sourceDigest: c, displayName: "已标注.mp4", observations: [] },
          { id: "card-d", sourceAssetId: "asset-d", sourceDigest: d, displayName: "合成.mp4", observations: ["synthetic-color-tone-fixture"] }
        ],
        highlightEvaluations: [
          { sourceAssetId: "asset-a", sourceDigest: a, labels: [], modelCandidates: [{ startMs: 3100, endMs: 8900 }] },
          { sourceAssetId: "asset-b", sourceDigest: b, labels: [], modelCandidates: [] },
          { sourceAssetId: "asset-c", sourceDigest: c, labels: [{ id: "human-c" }], modelCandidates: [{ startMs: 1000, endMs: 2000 }] },
          { sourceAssetId: "asset-d", sourceDigest: d, labels: [], modelCandidates: [{ startMs: 1000, endMs: 2000 }] }
        ]
      };
      return buildExistingHighlightDraftPlan();
    })()`, context) as any;

    expect(result.tasks).toEqual([{
      materialCardId: "card-a", sourceAssetId: "asset-a", startSeconds: 3.1, endSeconds: 8.9,
    }]);
    expect(result.boundary).toEqual({
      humanLabelsCreated: 0, trainingEligible: false, productionAccepted: false, automaticPublishing: false,
    });
  });

  it("opens the submit gate only after an explicit human confirmation", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const confirmation = { checked: false };
      const submit = { disabled: true };
      const form = {
        dataset: { highlightForm: "card-b" },
        elements: { namedItem: (name) => name === "humanConfirmed" ? confirmation : null },
        querySelector: () => submit
      };
      const before = updateHighlightSubmitGate(form);
      confirmation.checked = true;
      const after = updateHighlightSubmitGate(form);
      confirmation.checked = false;
      const cancelled = updateHighlightSubmitGate(form);
      return { before, after, cancelled, disabled: submit.disabled };
    })()`, context) as any;

    expect(result).toEqual({ before: false, after: true, cancelled: false, disabled: true });
  });

  it("moves the reviewer to the next real pending video after a successful label", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = {
        ...emptyStudioState("customer-a"),
        materials: [
          { id: "card-done", sourceAssetId: "asset-done", displayName: "完成.mp4" },
          { id: "card-next", sourceAssetId: "asset-next", displayName: "下一条.mp4" }
        ],
        highlightEvaluations: [
          { sourceAssetId: "asset-done", labels: [{ reason: "已完成" }] },
          { sourceAssetId: "asset-next", labels: [] }
        ]
      };
      const status = { dataset: { highlightFormStatus: "card-next" }, textContent: "" };
      const start = { focused: false, focus() { this.focused = true; } };
      const form = {
        dataset: { highlightForm: "card-next" },
        closest: () => ({ querySelectorAll: () => [status] }),
        elements: { namedItem: () => start },
        scrolled: false,
        scrollIntoView() { this.scrolled = true; }
      };
      document.querySelector = (selector) => selector === "#highlight-evaluations"
        ? { querySelectorAll: () => [form] }
        : null;
      const moved = focusNextPendingHighlight("card-done");
      return { moved, scrolled: form.scrolled, focused: start.focused, status: status.textContent };
    })()`, context) as any;

    expect(result).toMatchObject({ moved: true, scrolled: true, focused: true });
    expect(result.status).toContain("上一条已记录");
  });

  it("routes the operator to the server-selected next human task without submitting it", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"),
        materials: [{ id: "card-a", sourceAssetId: "asset-a" }],
        highlightBatch: { actionPlan: { nextAction: {
          kind: "profile-existing", sourceAssetId: "asset-a", captureSequence: null
        } } }
      };
      const status = { textContent: "" };
      const lighting = { focused: false, focus() { this.focused = true; } };
      const form = { dataset: { highlightCoverageForm: "card-a" }, scrolled: false,
        scrollIntoView() { this.scrolled = true; },
        querySelector: () => status,
        elements: { namedItem: () => lighting }
      };
      document.querySelector = (selector) => selector === "#highlight-evaluations"
        ? { querySelectorAll: () => [form] }
        : null;
      const focused = focusNextHighlightAction();
      return { focused, scrolled: form.scrolled, lightingFocused: lighting.focused, status: status.textContent };
    })()`, context) as any;

    expect(result).toMatchObject({ focused: true, scrolled: true, lightingFocused: true });
    expect(result.status).toContain("当前优先任务");
  });

  it("renders only a persisted human-confirmed shooting profile as coverage evidence", async () => {
    const context = await loadStudioUiContext();
    const digest = "c".repeat(64);
    const html = runInContext(`renderHighlightEvaluations([{
      sourceAssetId: "asset-c", sourceDigest: "${digest}", sourceStartMs: 0, sourceEndMs: 45000,
      status: "awaiting-human-label", labels: [], scores: [], modelCandidates: []
    }], [{ id: "card-c", sourceAssetId: "asset-c", sourceDigest: "${digest}", displayName: "制作.mp4" }], null, null, [{
      sourceAssetId: "asset-c", sourceDigest: "${digest}", lighting: "mixed",
      motion: "making-action", environment: "indoor-mixed", provenance: "human-confirmed",
      updatedAt: "2026-10-15T00:00:00.000Z"
    }])`, context) as string;

    expect(digest).toHaveLength(64);
    expect(html).toContain("人工拍摄画像 · 已保存");
    expect(html).toContain('<option value="mixed" selected>混合光线</option>');
    expect(html).toContain('<option value="making-action" selected>制作动作</option>');
    expect(html).toContain('<option value="indoor-mixed" selected>室内复杂环境</option>');
    expect(html).toContain("模型推测没有计入");
  });

  it("submits the shooting profile with explicit human confirmation and refreshes the gate", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a" } };
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        return { profiles: [{ sourceAssetId: "asset-a", provenance: "human-confirmed" }],
          evaluations: [], attempts: [], batch: { automaticEditingEligible: false } };
      };
      const values = { lighting: "stable", motion: "static-display", environment: "indoor-controlled" };
      const confirmation = { checked: true };
      const form = {
        dataset: { highlightCoverageForm: "card-a" },
        elements: { namedItem: (name) => name === "humanConfirmed" ? confirmation : { value: values[name] || "" } },
        querySelector: () => ({ textContent: "" })
      };
      await submitStudioHighlightCoverage(form);
      return { calls, profiles: studioState.highlightCoverageProfiles, gate: studioState.highlightBatch };
    })()`, context) as any;

    expect(result.calls[0]).toMatchObject({
      path: "/api/studio/highlights/coverage",
      body: {
        materialCardId: "card-a", lighting: "stable", motion: "static-display",
        environment: "indoor-controlled", humanConfirmed: true,
      },
    });
    expect(result.calls[0].body.operationKey).toContain("highlight-coverage");
    expect(result.profiles).toEqual([{ sourceAssetId: "asset-a", provenance: "human-confirmed" }]);
    expect(result.gate.automaticEditingEligible).toBe(false);
  });

  it("keeps an unsubmitted highlight draft locally, restores it, and clears it without creating a label", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const records = new Map();
      window.localStorage = {
        getItem: (key) => records.has(key) ? records.get(key) : null,
        setItem: (key, value) => records.set(key, value),
        removeItem: (key) => records.delete(key)
      };
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { sessionId: "session-a", customerId: "customer-a" },
        materials: [{ id: "card-b", sourceAssetId: "asset-b", sourceDigest: "${"b".repeat(64)}" }],
        highlightEvaluations: [{ sourceAssetId: "asset-b", sourceDigest: "${"b".repeat(64)}" }]
      };
      const values = {
        startSeconds: { value: "2.09", defaultValue: "2.09" },
        endSeconds: { value: "4.82", defaultValue: "4.82" },
        reason: { value: '人工判断：动作完成 & 镜头稳定 "未提交"', defaultValue: '人工判断：动作完成 & 镜头稳定 "未提交"' }
      };
      const indicator = { textContent: "" };
      const clearButton = { disabled: true };
      const status = { dataset: { highlightFormStatus: "card-b" }, textContent: "" };
      const card = {
        querySelector: (selector) => selector === "[data-highlight-draft-state]" ? indicator : clearButton,
        querySelectorAll: () => [status]
      };
      const form = {
        dataset: { highlightForm: "card-b" },
        elements: { namedItem: (name) => values[name] },
        closest: () => card
      };
      const saved = persistHighlightDraftFromForm(form);
      const restoredHtml = renderHighlightEvaluations([{
        sourceAssetId: "asset-b", sourceDigest: "${"b".repeat(64)}", sourceStartMs: 0, sourceEndMs: 22000,
        status: "awaiting-human-label", labels: [], scores: [], modelCandidates: []
      }], [{ id: "card-b", sourceAssetId: "asset-b", sourceDigest: "${"b".repeat(64)}", displayName: "机械手.mp4" }]);
      clearHighlightDraft("card-b", form);
      return { saved, restoredHtml, afterClear: readHighlightDraft("card-b"), values,
        indicator: indicator.textContent, clearDisabled: clearButton.disabled, storedCount: records.size };
    })()`, context) as any;

    expect(result.saved).toMatchObject({
      status: "unsubmitted-local-draft",
      boundary: { humanLabel: false, trainingEligible: false, productionAccepted: false, automaticPublishing: false },
    });
    expect(result.restoredHtml).toContain('value="2.09"');
    expect(result.restoredHtml).toContain('value="4.82"');
    expect(result.restoredHtml).toContain("人工判断：动作完成 &amp; 镜头稳定 &quot;未提交&quot;");
    expect(result.restoredHtml).toContain("未提交草稿已从本机恢复");
    expect(result.afterClear).toBe(null);
    expect(result.values).toMatchObject({ startSeconds: { value: "" }, endSeconds: { value: "" }, reason: { value: "" } });
    expect(result.values).toMatchObject({ startSeconds: { defaultValue: "" }, endSeconds: { defaultValue: "" }, reason: { defaultValue: "" } });
    expect(result.indicator).toContain("尚无未提交草稿");
    expect(result.clearDisabled).toBe(true);
    expect(result.storedCount).toBe(0);
  });

  it("shows the persisted worker stage instead of a generic highlight timeout", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), highlightRetestAttempts: [{
        sourceAssetId: "asset-a", status: "failed-retryable", attempt: 2,
        failureCode: "studio-analysis-worker-timeout", lastStage: "model-generation",
        activeStageElapsedMs: 12500
      }] };
      return renderHighlightEvaluations([{
        sourceAssetId: "asset-a", sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 17933,
        status: "failed", threshold: { minimumIoU: 0.5 },
        labels: [{ sourceStartMs: 3000, sourceEndMs: 15000, reason: "晚霞" }],
        modelCandidates: [], scores: [], publishable: false, trainingEligible: false,
        boundary: "human-label-required-model-evaluation-only"
      }], [{ id: "card-a", sourceAssetId: "asset-a", fileName: "黄昏飞行.mp4" }]);
    })()`, context) as string;

    expect(html).toContain("停在模型生成判断（至少 12.5 秒）");
    expect(html).toContain("第 2 次");
    expect(html).toContain("studio-analysis-worker-timeout");
  });

  it("shows successful full-timeline frame coverage from the model receipt", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), highlightRetestAttempts: [{
        sourceAssetId: "asset-a", status: "succeeded", attempt: 1,
        failureCode: null, lastStage: null, activeStageElapsedMs: null,
        analysisStrategy: "temporal-bucket-salience-v2", analyzedFrames: 5
      }] };
      return renderHighlightEvaluations([{
        sourceAssetId: "asset-a", sourceDigest: "${"a".repeat(64)}", sourceStartMs: 0, sourceEndMs: 169840,
        status: "awaiting-human-label", threshold: { minimumIoU: 0.5 },
        labels: [], modelCandidates: [], scores: [], publishable: false, trainingEligible: false,
        boundary: "human-label-required-model-evaluation-only"
      }], [{ id: "card-a", sourceAssetId: "asset-a", fileName: "长视频.mp4" }]);
    })()`, context) as string;

    expect(html).toContain("全片分布抽帧 5 帧");
    expect(html).toContain("重新分析模型候选");
  });

  it("shows the batch evidence gate without promoting one passing video to automation readiness", async () => {
    const context = await loadStudioUiContext();
    const script = await readFile(join(publicRoot, "studio.js"), "utf8");
    const html = runInContext(`renderHighlightBatchGate({
      status: "insufficient-samples",
      labeledVideos: 1, remainingVideos: 9, passedVideos: 1, failedVideos: 0,
      passRate: 1, attemptedVideos: 1, evidenceCoverageComplete: true,
      successfulRuntimeSamples: 1, p95RuntimeMs: 127198,
      failureStages: [], automaticEditingEligible: false,
      sampleCoverage: { status: "insufficient-variety", contentTypes: ["designer-viewpoint"], durationBands: ["short"],
        lightingTypes: [], motionTypes: [], environmentTypes: [], profiledVideos: 0,
        profileCoverageComplete: false, modelInferencesCounted: 0 },
      actionPlan: { existingEligibleVideos: 3, existingVideosAwaitingLabel: 2, existingVideosAwaitingProfile: 3,
        minimumCountShortfall: 7, additionalEligibleVideosNeeded: 0,
        nextAction: { kind: "profile-existing", sourceAssetId: "asset-a", captureSequence: null },
        captureTargets: [], customerCaptureRequired: false, futureEvidenceMode: "natural-upload-accumulation",
        evidenceBasis: "confirmed-gaps-only", modelInferencesCounted: 0 },
      productionAccepted: false, automaticPublishing: false,
      boundary: "local-pilot-evidence-only"
    }, { id: "brief-a" })`, context) as string;

    expect(html).toContain("证据采集中，暂不能自动进入成片");
    expect(html).toContain("1/10");
    expect(html).toContain("1/20");
    expect(html).toContain("100.0%");
    expect(html).toContain("127.2 秒");
    expect(html).toContain("1/1");
    expect(html).toContain("本轮不要求客户补拍；后续随日常上传自然积累");
    expect(html).toContain("复测全部已标注视频");
    expect(html).toContain("导出验收回执");
    expect(html).toContain("导出模型反馈候选包");
    expect(html).toContain("data-highlight-retest-batch");
    expect(html).toContain("data-highlight-export");
    expect(html).toContain("data-highlight-feedback-dataset-export");
    expect(html).toContain("data-highlight-automatic-drafts disabled");
    expect(html).toContain("自动样片入口保持锁定");
    expect(html).toContain("样本多样性：</strong>未通过");
    expect(html).toContain("人物／口播");
    expect(html).toContain("模型猜测计入 0 条");
    expect(html).toContain("人工拍摄画像：</strong>0/1 条");
    expect(html).toContain("现有 3 条可计入视频：还要标注 2 条，还要补人工画像 3 条。本轮不生成新增拍摄任务");
    expect(html).not.toContain("建议，尚未拍摄");
    expect(html).toContain("data-highlight-next-action>补下一张人工画像");
    expect(html).toContain("data-highlight-export-capture-plan>导出现有素材任务单 CSV");
    expect(script).toContain('timeZone: "Asia/Taipei"');
    expect(html).toContain("生产验收：否 · 自动发布：否");
    expect(html).toContain("交付证据还差 19 条");
  });

  it("exports a versioned feedback candidate without claiming training approval", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const downloads = [];
      Blob = class { constructor(parts, options) { this.parts = parts; this.options = options; } };
      URL = { createObjectURL: () => "blob:feedback", revokeObjectURL: () => {} };
      document.createElement = () => ({
        href: "", download: "", click() { downloads.push(this.download); }
      });
      studioState = { ...emptyStudioState("customer-a"), busy: false,
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
      studioApi = async (path, options) => ({ dataset: {
        schemaVersion: 1, datasetId: "highlight-feedback-v1-123456789abc",
        datasetDigest: "a".repeat(64), generatedAt: "2026-10-15T00:00:00.000Z",
        counts: { total: 2 }, entries: [],
        readiness: { trainingEligible: false, automaticTraining: false },
        boundary: "versioned-candidate-dataset-not-training-approval"
      }});
      renderStudioState = () => {};
      const dataset = await exportStudioHighlightFeedbackDataset();
      return { dataset, downloads, busy: studioState.busy };
    })()`, context) as any;

    expect(result.dataset).toMatchObject({
      datasetId: "highlight-feedback-v1-123456789abc",
      readiness: { trainingEligible: false, automaticTraining: false },
    });
    expect(result.downloads).toEqual(["highlight-feedback-v1-123456789abc.json"]);
    expect(result.busy).toBe(false);
  });

  it("exports a versioned caption correction candidate without starting training", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const downloads = [];
      Blob = class { constructor(parts, options) { this.parts = parts; this.options = options; } };
      URL = { createObjectURL: () => "blob:caption-feedback", revokeObjectURL: () => {} };
      document.createElement = () => ({
        href: "", download: "", click() { downloads.push(this.download); }
      });
      studioState = { ...emptyStudioState("customer-a"), busy: false,
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
      studioApi = async () => ({ dataset: {
        schemaVersion: 1, datasetId: "caption-feedback-v1-123456789abc",
        datasetDigest: "b".repeat(64), generatedAt: "2026-10-24T00:00:00.000Z",
        counts: { total: 3 }, entries: [],
        readiness: { trainingEligible: false, automaticTraining: false, releaseEligible: false },
        boundary: "versioned-caption-candidate-dataset-not-training-approval"
      }});
      renderStudioState = () => {};
      const dataset = await exportStudioCaptionFeedbackDataset();
      return { dataset, downloads, busy: studioState.busy };
    })()`, context) as any;

    expect(result.dataset).toMatchObject({
      datasetId: "caption-feedback-v1-123456789abc",
      readiness: { trainingEligible: false, automaticTraining: false, releaseEligible: false },
    });
    expect(result.downloads).toEqual(["caption-feedback-v1-123456789abc.json"]);
    expect(result.busy).toBe(false);
  });

  it("loads caption corrections into a readable local review state", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), busy: false,
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
      studioApi = async () => ({ dataset: {
        schemaVersion: 1, datasetId: "caption-feedback-v1-123456789abc",
        datasetDigest: "b".repeat(64), generatedAt: "2026-10-24T00:00:00.000Z",
        counts: { total: 1 }, entries: [{ revisedDraftId: "draft-v2" }],
        readiness: { trainingEligible: false, automaticTraining: false, releaseEligible: false },
        boundary: "versioned-caption-candidate-dataset-not-training-approval"
      }});
      renderStudioState = () => {};
      const dataset = await loadStudioCaptionFeedbackDataset("draft-v2");
      return { dataset, reviewDraftId: studioState.captionFeedbackReviewDraftId,
        storedId: studioState.captionFeedbackDataset?.datasetId, busy: studioState.busy };
    })()`, context) as any;

    expect(result).toMatchObject({
      reviewDraftId: "draft-v2",
      storedId: "caption-feedback-v1-123456789abc",
      busy: false,
    });
    expect(result.dataset.readiness).toMatchObject({
      trainingEligible: false,
      automaticTraining: false,
      releaseEligible: false,
    });
  });

  it("exports only existing-material review tasks and ignores stale capture suggestions", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      const a = "a".repeat(64), b = "b".repeat(64), c = "c".repeat(64);
      studioState = { ...emptyStudioState("customer-a"),
        materials: [
          { id: "card-a", sourceAssetId: "asset-a", sourceDigest: a, displayName: "=危险.mp4", contentType: "single-product", observations: [] },
          { id: "card-b", sourceAssetId: "asset-b", sourceDigest: b, displayName: "待标注.mp4", contentType: "designer-viewpoint", observations: [] },
          { id: "card-c", sourceAssetId: "asset-c", sourceDigest: c, displayName: "合成.mp4", contentType: "single-product", observations: ["synthetic-color-tone-fixture"] }
        ],
        highlightEvaluations: [
          { sourceAssetId: "asset-a", sourceDigest: a, sourceStartMs: 0, sourceEndMs: 20000, labels: [{ id: "label-a" }] },
          { sourceAssetId: "asset-b", sourceDigest: b, sourceStartMs: 0, sourceEndMs: 50000, labels: [] },
          { sourceAssetId: "asset-c", sourceDigest: c, sourceStartMs: 0, sourceEndMs: 10000, labels: [] }
        ],
        highlightCoverageProfiles: [],
        highlightBatch: { actionPlan: { captureTargets: [{ sequence: 1, contentType: "jewelry-making-process",
          durationBand: "medium", lighting: "mixed", motion: "making-action", environment: "outdoor",
          status: "recommended-not-captured" }] } }
      };
      return buildStudioCaptureTaskCsv();
    })()`, context) as any;

    expect(result.rows).toHaveLength(3);
    expect(result.rows.map((row: any) => row.status)).toEqual([
      "待人工画像", "待人工标注", "待人工画像",
    ]);
    expect(result.csv).toContain("'=危险.mp4");
    expect(result.csv).not.toContain("新增补拍");
    expect(result.csv).not.toContain("建议，尚未拍摄");
    expect(result.csv).not.toContain("制作过程");
    expect(result.csv).not.toContain("合成.mp4");
    expect(result.boundary).toEqual({ completedTasksClaimed: 0, modelInferencesCounted: 0, automaticPublishing: false });
  });

  it("opens the local automatic sample button only when the evidence gate and brief are both ready", async () => {
    const context = await loadStudioUiContext();
    const gate = `{
      status: "pilot-threshold-met", labeledVideos: 10, remainingVideos: 0,
      passedVideos: 8, failedVideos: 2, passRate: 0.8, attemptedVideos: 10,
      evidenceCoverageComplete: true, successfulRuntimeSamples: 9, p95RuntimeMs: 140000,
      failureStages: [], automaticEditingEligible: true,
      sampleCoverage: { status: "pilot-variety-met", contentTypes: ["single-product", "jewelry-making-process"], durationBands: ["short", "long"],
        lightingTypes: ["stable", "mixed"], motionTypes: ["static-display", "making-action"],
        environmentTypes: ["indoor-controlled", "outdoor"], profiledVideos: 10,
        profileCoverageComplete: true, modelInferencesCounted: 0 },
      productionAccepted: false, automaticPublishing: false,
      boundary: "local-pilot-evidence-only"
    }`;
    const withBrief = runInContext(`renderHighlightBatchGate(${gate}, { id: "brief-a" })`, context) as string;
    const withoutBrief = runInContext(`renderHighlightBatchGate(${gate}, null)`, context) as string;

    expect(withBrief).toContain("data-highlight-automatic-drafts>用通过门槛的模型生成本地样片");
    expect(withBrief).toContain("自动样片入口已开放");
    expect(withoutBrief).toContain("data-highlight-automatic-drafts disabled");
    expect(withoutBrief).toContain("要先确认本次创作方案");
    expect(withBrief).toContain("生产验收：否 · 自动发布：否");
  });

  it("uses the gated automatic-draft endpoint and keeps the result in human review", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      const gate = { automaticEditingEligible: true, labeledVideos: 10, status: "pilot-threshold-met" };
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        brief: { id: "brief-a" }, highlightBatch: gate
      };
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/highlights/automatic-drafts") return {
          drafts: [{ id: "auto-master", status: "pending-review" }], gate,
          reviewRequired: true, productionAccepted: false, automaticPublishing: false
        };
        return { history: { demands: [], assessments: [], opportunities: [], briefs: [], drafts: [], customerFeedback: [], revisionProposals: [] } };
      };
      await createAutomaticHighlightDrafts();
      const eligible = { calls: [...calls], drafts: studioState.drafts, tab: studioActiveTab };
      calls.length = 0;
      studioState.highlightBatch = { ...gate, automaticEditingEligible: false };
      await createAutomaticHighlightDrafts();
      return { eligible, blockedCalls: calls };
    })()`, context) as any;

    expect(result.eligible.calls[0]).toMatchObject({
      path: "/api/studio/highlights/automatic-drafts",
      body: { briefId: "brief-a" },
    });
    expect(result.eligible.calls[0].body.operationKey).toContain("highlight-automatic-drafts");
    expect(result.eligible.calls[0].body).not.toHaveProperty("publish");
    expect(result.eligible.drafts).toEqual([{ id: "auto-master", status: "pending-review" }]);
    expect(result.eligible.tab).toBe("works");
    expect(result.blockedCalls).toEqual([]);
  });

  it("submits user demand instead of a hard-coded goal", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" } };
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        return path === "/api/studio/demands"
          ? { demand: { id: "demand-a", ...options.body, version: 1, status: "current" } }
          : { history: { demands: [], assessments: [], opportunities: [], briefs: [], drafts: [], customerFeedback: [], revisionProposals: [] } };
      };
      await submitStudioDemand({
        contentType: "designer-viewpoint",
        goal: "让顾客理解设计灵感",
        targetAudience: "准备到店的顾客",
        desiredEffect: "愿意预约到店",
        mustInclude: [], mustAvoid: [], submittedBy: "operator-on-behalf",
      });
      return { calls, state: studioState };
    })()`, context) as any;

    expect(result.calls[0]).toMatchObject({
      path: "/api/studio/demands",
      body: { goal: "让顾客理解设计灵感", desiredEffect: "愿意预约到店" },
    });
    expect(result.calls.map((call: any) => call.path)).toEqual([
      "/api/studio/demands",
      "/api/studio/history",
    ]);
    expect(result.state.demand.id).toBe("demand-a");
  });

  it("turns one plain-language request into the existing demand contract", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { id: "inventory-a", items: [{ materialCardId: "card-a", sourceAssetId: "asset-a" }] },
      };
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/demands") return {
          demand: { id: "demand-quick", ...options.body, version: 1, status: "current" }
        };
        if (path === "/api/studio/matches") return { assessment: {
          id: "match-quick", demandId: "demand-quick", verdict: "ready", matchedEvidence: [],
          unmetNeeds: [], recommendedCaptures: [], safeScope: "只用已授权素材",
          capability: { label: "离线/模拟分析" }
        } };
        if (path === "/api/studio/opportunities") return { opportunities: [], gaps: [], gapList: null };
        return { history: seededHistory() };
      };
      await submitQuickStudioPrompt("讲清楚这枚戒指的设计灵感");
      return { calls, state: studioState };
    })()`, context) as any;

    expect(result.calls[0]).toMatchObject({
      path: "/api/studio/demands",
      body: {
        contentType: "designer-viewpoint",
        goal: "讲清楚这枚戒指的设计灵感",
        targetAudience: "待 Agent 继续确认",
        desiredEffect: "形成可审的内容初稿",
        submittedBy: "operator-on-behalf",
      },
    });
    expect(result.state.demand.id).toBe("demand-quick");
    expect(result.state.matchAssessment.id).toBe("match-quick");
    expect(result.calls.map((call: any) => call.path)).toEqual([
      "/api/studio/demands",
      "/api/studio/history",
      "/api/studio/matches",
      "/api/studio/opportunities",
      "/api/studio/history",
    ]);
  });

  it("derives one current creation step while keeping works and library as separate tabs", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => ({
      empty: studioAppView(emptyStudioState("customer-a"), "create"),
      matching: studioAppView({ ...emptyStudioState("customer-a"), demand: { id: "demand-a" } }, "create"),
      freshStart: studioAppView({
        ...emptyStudioState("customer-a"), demand: { id: "demand-a" },
        matchAssessment: { id: "match-a", verdict: "ready" }, brief: { id: "brief-a" },
        drafts: [{ id: "draft-a" }]
      }, "create"),
      works: studioAppView({ ...emptyStudioState("customer-a"), drafts: [{ id: "draft-a" }] }, "works"),
      library: studioAppView(emptyStudioState("customer-a"), "library"),
      revision: studioAppView({
        ...emptyStudioState("customer-a"), demand: { id: "demand-a" },
        matchAssessment: { id: "match-a", verdict: "ready" }, brief: { id: "brief-a" },
        revisionProposal: { id: "proposal-a", status: "proposed" }
      }, "create"),
    }))()`, context) as any;

    expect(result.empty).toEqual({ tab: "create", step: "demand" });
    expect(result.matching).toEqual({ tab: "create", step: "match" });
    expect(result.freshStart).toEqual({ tab: "create", step: "demand" });
    expect(result.works).toEqual({ tab: "works", step: "drafts" });
    expect(result.library).toEqual({ tab: "library", step: "materials" });
    expect(result.revision).toEqual({ tab: "create", step: "revision" });
  });

  it("sends an empty library to upload before creating a demand", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a" },
        inventory: { items: [] } };
      const calls = [];
      studioApi = async (path) => { calls.push(path); throw new Error("unexpected-call"); };
      await submitQuickStudioPrompt("讲设计灵感");
      return { calls, tab: studioActiveTab, demand: studioState.demand };
    })()`, context) as any;
    expect(result.calls).toEqual([]);
    expect(result.tab).toBe("library");
    expect(result.demand).toBeNull();
  });

  it("resumes matching after upload without replacing the customer's demand", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a" },
        inventory: { items: [{ materialCardId: "card-upload" }] }, demand: { id: "demand-original" } };
      studioActiveTab = "library";
      const matchedIds = [];
      assessStudioDemand = async () => { matchedIds.push(studioState.demand.id); };
      await continueFromStudioLibrary();
      return { matchedIds, tab: studioActiveTab, demandId: studioState.demand.id };
    })()`, context) as any;
    expect(result).toEqual({ matchedIds: ["demand-original"], tab: "create", demandId: "demand-original" });
  });

  it("displays video duration in minutes rather than treating milliseconds as seconds", async () => {
    const context = await loadStudioUiContext();
    const html = runInContext(`renderMaterials([{ id: "long-video", sourceAssetId: "asset-long",
      sourceStartMs: 0, sourceEndMs: 100000, contentType: "single-product", technicalUsable: true,
      displayName: "制作过程.mp4" }])`, context) as string;
    expect(html).toContain("1分40秒");
    expect(html).not.toContain("1666分");
  });

  it.each([["ready", false], ["partial", true], ["blocked", true]] as const)(
    "maps %s to the brief gate and a textual verdict",
    async (verdict, disabled) => {
      const context = await loadStudioUiContext();
      const result = runInContext(`(() => {
        studioState = {
          ...emptyStudioState("customer-a"),
          matchAssessment: {
            id: "match-a", verdict: "${verdict}", safeScope: "只用已授权素材",
            matchedEvidence: [{ requirement: "成品近景", materialCardIds: ["material-a"], explanation: "现有近景可用" }],
            unmetNeeds: ["还缺设计师出镜"], recommendedCaptures: ["从历史素材库找设计师观点；找不到就缩小范围或暂停，不补拍"],
            capability: { label: "离线/模拟分析" }
          },
          opportunities: ${verdict === "ready" ? '[{ id: "opportunity-a" }]' : "[]"}
        };
        renderStudioState();
        return {
          disabled: document.querySelector("#confirm-brief").disabled,
          markup: document.querySelector("#match-assessment").innerHTML,
        };
      })()`, context) as any;

      expect(result.disabled).toBe(disabled);
      expect(result.markup).toContain(verdict === "ready" ? "适合继续" : verdict === "partial" ? "部分匹配" : "暂不适合");
      if (verdict === "ready") expect(result.markup).toContain("绿灯 · 适合继续");
      expect(result.markup).toContain(`verdict-${verdict}`);
      expect(result.markup).toContain("适合的原因");
      expect(result.markup).toContain("不适合或还缺的内容");
      expect(result.markup).toContain("可以怎么继续");
      expect(result.markup).not.toMatch(/补拍建议|补拍 10 秒/);
    },
  );

  it("assesses the current demand and discovers opportunities only when ready", async () => {
    const context = await loadStudioUiContext();
    const calls = await runInContext(`(async () => {
      async function exercise(verdict) {
        studioState = {
          ...emptyStudioState("customer-a"),
          session: { token: "token-a", customerId: "customer-a" },
          inventory: { id: "inventory-a", items: [] },
          demand: { id: "demand-a" }
        };
        const seen = [];
        studioApi = async (path, options) => {
          seen.push({ path, body: options.body });
          if (path === "/api/studio/matches") return { assessment: {
            id: "match-a", demandId: "demand-a", verdict, matchedEvidence: [],
            unmetNeeds: [], recommendedCaptures: [], safeScope: "只用已授权素材",
            capability: { label: "离线/模拟分析" }
          } };
          if (path === "/api/studio/opportunities") return {
            opportunities: [{ id: "opportunity-a" }], gaps: [], gapList: null
          };
          return { history: seededHistory() };
        };
        await assessStudioDemand();
        return seen;
      }
      return { ready: await exercise("ready"), partial: await exercise("partial") };
    })()`, context) as any;

    expect(calls.ready.map((call: any) => call.path)).toEqual([
      "/api/studio/matches",
      "/api/studio/opportunities",
      "/api/studio/history",
    ]);
    expect(calls.ready[1].body).toMatchObject({ demandId: "demand-a", matchAssessmentId: "match-a" });
    expect(calls.partial.map((call: any) => call.path)).toEqual([
      "/api/studio/matches",
      "/api/studio/history",
    ]);
  });

  it("explains a visual product mismatch before the brief step", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { id: "inventory-a", items: [] },
        demand: { id: "demand-a" }
      };
      studioApi = async (path) => path === "/api/studio/matches"
        ? { assessment: {
            id: "match-a", demandId: "demand-a", verdict: "blocked", matchedEvidence: [],
            unmetNeeds: ["画面内容与商品介绍目标不一致：当前素材主要是飞机、机翼、天空或黄昏风景"],
            recommendedCaptures: ["改为风景内容需求，或重新选择能清楚看到商品主体的历史素材"],
            safeScope: "不会进入任务书或生成成片", capability: { label: "本地视觉模型" }
          } }
        : { history: seededHistory() };
      await assessStudioDemand();
      renderStudioState();
      return {
        status: document.querySelector("#studio-status").textContent,
        markup: document.querySelector("#match-assessment").innerHTML,
        briefDisabled: document.querySelector("#confirm-brief").disabled,
      };
    })()`, context) as any;

    expect(result.status).toContain("画面内容与商品目标不一致");
    expect(result.markup).toContain("飞机、机翼、天空或黄昏风景");
    expect(result.markup).toContain("改为风景内容需求");
    expect(result.briefDisabled).toBe(true);
  });

  it("confirms the current opportunity inventory after analysis replaces material cards", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { id: "old-inventory", items: [{ materialCardId: "old-card", sourceAssetId: "asset-a" }] },
        materials: [{ id: "old-card" }], demand: { id: "demand-a" }
      };
      let confirmedBody;
      studioApi = async (path, options) => {
        if (path === "/api/studio/matches") return { assessment: { id: "match-a", verdict: "ready" } };
        if (path === "/api/studio/opportunities") return {
          inventory: { id: "new-inventory", items: [
            { materialCardId: "new-card-a", sourceAssetId: "asset-a" },
            { materialCardId: "new-card-b", sourceAssetId: "asset-b" },
            { materialCardId: "new-card-c", sourceAssetId: "asset-c" },
            { materialCardId: "new-card-d", sourceAssetId: "asset-d" }
          ] },
          materials: [{ id: "new-card-a", displayName: "更新后的素材" }],
          opportunities: [{ id: "opportunity-a", materialCardIds: ["new-card-a", "new-card-b", "new-card-c", "new-card-d"] }]
        };
        if (path === "/api/studio/briefs") {
          confirmedBody = options.body;
          return { brief: { id: "brief-a" } };
        }
        return { history: seededHistory() };
      };
      await assessStudioDemand();
      await confirmStudioBrief();
      return { confirmedBody, inventory: studioState.inventory, materials: studioState.materials };
    })()`, context) as any;
    expect(result.inventory.id).toBe("new-inventory");
    expect(result.materials).toEqual([{ id: "new-card-a", displayName: "更新后的素材" }]);
    expect(result.confirmedBody.selectedAssetIds).toEqual(["asset-a", "asset-b", "asset-c", "asset-d"]);
  });

  it("invalidates only the cached match after a customer records material suitability", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { id: "inventory-a", items: [] },
        demand: { id: "demand-a" },
        matchAssessment: { id: "match-a", verdict: "ready" },
        opportunities: [{ id: "opportunity-a" }],
        brief: { id: "brief-v1", version: 1 },
        drafts: [{ id: "draft-v1", version: 1 }],
      };
      studioApi = async (path) => path === "/api/studio/analysis/history"
        ? { history: { entries: [], reviews: [], duplicateGroups: [], suitabilityReviews: [] } }
        : {};
      await submitStudioMaterialSuitability(
        { jobId: "job-a", sourceAssetId: "asset-a", sourceDigest: "d".repeat(64), sourceStartMs: 0, sourceEndMs: 1000 },
        { demandId: "demand-a", matchAssessmentId: "match-a" },
        "not-suitable", "不符合当前故事方向",
      );
      return { state: studioState, view: studioAppView(studioState, "create") };
    })()`, context) as any;

    expect(result.view).toEqual({ tab: "create", step: "match" });
    expect(result.state.matchAssessment).toBeNull();
    expect(result.state.opportunities).toEqual([]);
    expect(result.state.brief).toEqual({ id: "brief-v1", version: 1 });
    expect(result.state.drafts).toEqual([{ id: "draft-v1", version: 1 }]);
  });

  it("shows a proposal before any V2 render request", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = seededV1StudioState();
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/customer-feedback") return { feedback: {
          id: "feedback-a", draftId: "draft-a", satisfaction: "partially-satisfied",
          comment: "请让设计师观点更突出", issueCategories: ["style"], mustKeep: [], mustRemove: [], needsReshoot: []
        } };
        if (path === "/api/studio/revision-proposals") return { proposal: {
          id: "proposal-a", feedbackId: "feedback-a", status: "proposed",
          understoodFeedback: "需要突出设计师观点", plannedChanges: ["调整开场"],
          retainedElements: [], unsupportedRequests: [], captureGaps: [], capability: { label: "离线/模拟分析" }
        } };
        return { history: seededHistory() };
      };
      await submitCustomerDraftFeedback({
        draftId: "draft-a", satisfaction: "partially-satisfied", issueCategories: ["style"],
        comment: "请让设计师观点更突出", mustKeep: [], mustRemove: [], needsReshoot: [],
        submittedBy: "customer-self-declared",
      });
      return { calls, state: studioState, proposal: renderRevisionProposal(studioState.revisionProposal) };
    })()`, context) as any;

    expect(result.calls.map((call: any) => call.path)).toEqual([
      "/api/studio/customer-feedback",
      "/api/studio/revision-proposals",
      "/api/studio/history",
    ]);
    expect(result.calls.some((call: any) => call.path === "/api/studio/drafts")).toBe(false);
    expect(result.state.revisionProposal.status).toBe("proposed");
    expect(result.proposal).toContain("需要突出设计师观点");
  });

  it("confirms only a V2 brief and renders drafts only after the separate action", async () => {
    const context = await loadStudioUiContext();
    const calls = await runInContext(`(async () => {
      studioState = {
        ...seededV1StudioState(),
        customerFeedback: { id: "feedback-a", draftId: "draft-a", satisfaction: "partially-satisfied" },
        revisionProposal: { id: "proposal-a", status: "proposed", captureGaps: [] }
      };
      const seen = [];
      studioApi = async (path, options = {}) => {
        seen.push({ path, body: options.body });
        if (path === "/api/studio/revision-proposals/confirm") return { brief: {
          id: "brief-v2", version: 2, parentBriefId: "brief-a", feedbackId: "feedback-a",
          revisionProposalId: "proposal-a", goal: "更突出设计师观点",
          selectedAssetIds: ["asset-a"], confirmedFactIds: [], reviewRequirements: ["采用前人工复核"],
          capability: { label: "离线/模拟分析" }
        } };
        if (path === "/api/studio/drafts") return { drafts: [{ id: "draft-v2-a", version: 2 }] };
        return { history: seededHistory() };
      };
      await confirmRevisionProposal();
      const afterConfirm = {
        calls: seen.map((row) => row.path),
        brief: studioState.brief,
        drafts: studioState.drafts,
        proposalMarkup: renderRevisionProposal(studioState.revisionProposal),
      };
      await createStudioDrafts();
      return {
        afterConfirm,
        finalCalls: seen.map((row) => row.path),
        drafts: studioState.drafts,
        renderedProposalText: renderRevisionProposal(
          studioState.revisionProposal,
          studioState.drafts.length > 0,
        ),
      };
    })()`, context) as any;

    expect(calls.afterConfirm.calls).toEqual([
      "/api/studio/revision-proposals/confirm",
      "/api/studio/history",
      "/api/studio/music/list",
    ]);
    expect(calls.afterConfirm.brief.version).toBe(2);
    expect(calls.afterConfirm.drafts).toEqual([]);
    expect(calls.afterConfirm.proposalMarkup).toContain("提案已确认，等待生成新版初稿");
    expect(calls.afterConfirm.proposalMarkup).not.toContain("提案仍有未解决项，不能确认");
    expect(calls.finalCalls).toEqual([
      "/api/studio/revision-proposals/confirm",
      "/api/studio/history",
      "/api/studio/music/list",
      "/api/studio/drafts",
      "/api/studio/history",
    ]);
    expect(calls.drafts[0].id).toBe("draft-v2-a");
    expect(calls.renderedProposalText).toContain("提案已确认，新版初稿已生成");
    expect(calls.renderedProposalText).not.toContain("等待生成新版初稿");
  });

  it("unlocks adopt only for satisfied feedback on the exact draft", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = {
        ...seededV1StudioState(),
        customerFeedback: { id: "feedback-a", draftId: "draft-a", satisfaction: "satisfied" }
      };
      return renderDraftCards([
        { id: "draft-a", version: 1, mediaUrl: "/a.mp4", durationSeconds: 31,
          sourceMap: [], capability: { label: "离线/模拟分析" } },
        { id: "draft-b", version: 1, mediaUrl: "/b.mp4", durationSeconds: 31,
          sourceMap: [], capability: { label: "离线/模拟分析" } }
      ]);
    })()`, context) as string;

    expect(markup).toMatch(/data-adopt-draft="draft-a"(?![^>]*disabled)/);
    expect(markup).toContain('data-review-before-adopt="draft-b"');
    expect(markup).toContain("先评价再通过");
    expect(markup).toContain("选择“满意”后，才会开放最终确认");
    expect(markup).toContain("采用仍不是发布");
  });

  it("uses the latest exact-draft feedback to gate adopt deterministically", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const draft = { id: "draft-a", version: 1, mediaUrl: "/a.mp4", durationSeconds: 31,
        sourceMap: [], capability: { label: "离线/模拟分析" } };
      const sibling = { ...draft, id: "draft-b" };
      const history = seededHistory();
      history.customerFeedback = [{
        id: "feedback-old-satisfied", draftId: "draft-a", version: 1,
        satisfaction: "satisfied", createdAt: "2026-09-11T08:00:00.000Z"
      }];
      studioState = {
        ...seededV1StudioState(), drafts: [draft, sibling], history,
        customerFeedback: {
          id: "feedback-new-unsatisfied", draftId: "draft-a", version: 2,
          satisfaction: "unsatisfied", createdAt: "2026-09-11T09:00:00.000Z"
        }
      };
      const calls = [];
      studioApi = async (path) => { calls.push(path); return {}; };
      const blockedMarkup = renderDraftCards(studioState.drafts);
      await submitStudioFeedback("draft-a", "adopt");

      studioState.customerFeedback = {
        id: "feedback-latest-satisfied", draftId: "draft-a", version: 3,
        satisfaction: "satisfied", createdAt: "2026-09-11T10:00:00.000Z"
      };
      const enabledMarkup = renderDraftCards(studioState.drafts);
      return { blockedMarkup, enabledMarkup, calls };
    })()`, context) as any;

    expect(result.blockedMarkup).not.toContain('data-adopt-draft="draft-a"');
    expect(result.blockedMarkup).toContain('data-review-before-adopt="draft-a"');
    expect(result.calls).toEqual([]);
    expect(result.enabledMarkup).toMatch(/data-adopt-draft="draft-a"(?![^>]*disabled)/);
    expect(result.enabledMarkup).toContain('data-review-before-adopt="draft-b"');
  });

  it("accepts adoption only from an exact non-publishing response and refreshes persistent draft states", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...seededV1StudioState(),
        customerFeedback: { id: "satisfied", draftId: "draft-a", version: 1, satisfaction: "satisfied" },
        drafts: [{ id: "draft-a", briefId: "brief-a", version: 1, status: "reviewable" },
          { id: "draft-b", briefId: "brief-a", version: 1, status: "reviewable" }]
      };
      const calls = [];
      studioApi = async (path, options) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/feedback") return { feedback: {
          id: "adoption-event", draftId: "draft-a", action: "adopt", publishClaimed: false
        } };
        return { history: { ...seededHistory(), drafts: [
          { id: "draft-a", briefId: "brief-a", version: 1, status: "adopted" },
          { id: "draft-b", briefId: "brief-a", version: 1, status: "rejected" }
        ] } };
      };
      await submitStudioFeedback("draft-a", "adopt");
      return { calls, state: studioState, markup: renderDraftCards(studioState.drafts) };
    })()`, context) as any;

    expect(result.calls.map((call: any) => call.path)).toEqual([
      "/api/studio/feedback", "/api/studio/history",
    ]);
    expect(result.state.feedback).toMatchObject({
      action: "adopt", draftId: "draft-a", publishClaimed: false,
    });
    expect(result.state.drafts.map((draft: any) => draft.status)).toEqual(["adopted", "rejected"]);
    expect(result.markup).toContain("已通过");
    expect(result.markup).toContain("历史版本");
  });

  it.each([
    ["missing", "{}"],
    ["wrong draft", '{ feedback: { draftId: "draft-b", action: "adopt", publishClaimed: false } }'],
    ["wrong action", '{ feedback: { draftId: "draft-a", action: "reject", publishClaimed: false } }'],
    ["publish claimed", '{ feedback: { draftId: "draft-a", action: "adopt", publishClaimed: true } }'],
  ])("fails closed on %s adoption evidence", async (_label, responseSource) => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...seededV1StudioState(),
        feedback: null,
        customerFeedback: { id: "satisfied", draftId: "draft-a", version: 1, satisfaction: "satisfied" }
      };
      const calls = [];
      studioApi = async (path) => { calls.push(path); return ${responseSource}; };
      await submitStudioFeedback("draft-a", "adopt");
      return { calls, state: studioState, status: document.querySelector("#studio-status").textContent };
    })()`, context) as any;
    expect(result.calls).toEqual(["/api/studio/feedback"]);
    expect(result.state.feedback).toBeNull();
    expect(result.status).toContain("studio-adoption-response-invalid");
  });

  it.each([
    ["unsupported-only", ["需要外部版权素材"], [], true, 0],
    ["capture-only", [], ["补拍设计师口述"], true, 0],
    ["no blockers", [], [], false, 1],
  ] as const)("uses one proposal confirmation gate for %s", async (_label, unsupported, gaps, disabled, expectedCalls) => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...seededV1StudioState(),
        revisionProposal: {
          id: "proposal-a", version: 1, status: "proposed", understoodFeedback: "调整观点",
          plannedChanges: ["调整开场"], retainedElements: [],
          unsupportedRequests: ${JSON.stringify(unsupported)}, captureGaps: ${JSON.stringify(gaps)},
          capability: { label: "离线/模拟分析" }
        }
      };
      const calls = [];
      studioApi = async (path) => {
        calls.push(path);
        if (path === "/api/studio/revision-proposals/confirm") return { brief: {
          id: "brief-v2", version: 2, goal: "调整观点", selectedAssetIds: ["asset-a"],
          confirmedFactIds: [], reviewRequirements: ["人工复核"], capability: { label: "离线/模拟分析" }
        } };
        return { history: seededHistory() };
      };
      renderStudioState();
      const before = {
        disabled: document.querySelector("#confirm-revision-proposal").disabled,
        markup: renderRevisionProposal(studioState.revisionProposal),
      };
      await confirmRevisionProposal();
      return { before, calls };
    })()`, context) as any;

    expect(result.before.disabled).toBe(disabled);
    expect(result.calls.filter((path: string) => path === "/api/studio/revision-proposals/confirm")).toHaveLength(expectedCalls);
    expect(result.before.markup).toContain(disabled ? "提案仍有未解决项，不能确认" : "提案可以内部确认");
  });

  it("refreshes visible history after successful match, brief, and draft writes", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const history = { ...seededHistory(), demands: [{
        id: "demand-history", version: 1, goal: "历史中的需求", targetAudience: "顾客",
        desiredEffect: "理解", mustInclude: [], mustAvoid: [], status: "current"
      }] };
      async function run(kind) {
        studioState = seededV1StudioState();
        if (kind === "match") {
          studioState.brief = null;
          studioState.drafts = [];
          studioState.opportunities = [];
        }
        const calls = [];
        studioApi = async (path) => {
          calls.push(path);
          if (path === "/api/studio/matches") return { assessment: {
            id: "match-a", verdict: "ready", matchedEvidence: [], unmetNeeds: [], recommendedCaptures: [], safeScope: "安全",
            capability: { label: "离线/模拟分析" }
          } };
          if (path === "/api/studio/opportunities") return { opportunities: [{ id: "opportunity-a" }], gaps: [], gapList: null };
          if (path === "/api/studio/briefs") return { brief: {
            id: "brief-a", version: 1, goal: "讲清楚", selectedAssetIds: ["asset-a"], confirmedFactIds: [],
            reviewRequirements: ["人工复核"], capability: { label: "离线/模拟分析" }
          } };
          if (path === "/api/studio/drafts") return { drafts: [{ id: "draft-a", version: 1 }] };
          return { history };
        };
        if (kind === "match") await assessStudioDemand();
        if (kind === "brief") await confirmStudioBrief();
        if (kind === "draft") await createStudioDrafts();
        return { calls, history: studioState.history, markup: document.querySelector("#studio-history").innerHTML };
      }
      return { match: await run("match"), brief: await run("brief"), draft: await run("draft") };
    })()`, context) as any;

    expect(result.match.calls).toEqual(["/api/studio/matches", "/api/studio/opportunities", "/api/studio/history"]);
    expect(result.brief.calls).toEqual([
      "/api/studio/briefs", "/api/studio/history", "/api/studio/music/list",
    ]);
    expect(result.draft.calls).toEqual(["/api/studio/drafts", "/api/studio/history"]);
    for (const state of [result.match, result.brief, result.draft]) {
      expect(state.history.demands[0].id).toBe("demand-history");
      expect(state.markup).toContain("历史中的需求");
    }
  });

  it("editing during the first demand submit aborts it and prevents stale demand recovery", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
      };
      let aborted = false;
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/demands") {
          options.signal.addEventListener("abort", () => { aborted = true; }, { once: true });
          await delay(25);
          return { demand: { id: "stale-demand", version: 1, status: "current" } };
        }
        return { history: seededHistory() };
      };
      const pending = submitStudioDemand({
        contentType: "single-product", goal: "第一次需求", targetAudience: "顾客",
        desiredEffect: "理解", mustInclude: [], mustAvoid: [], submittedBy: "operator-on-behalf",
      });
      await delay(1);
      const epochBeforeEdit = studioRequestEpoch;
      const edited = beginStudioDemandEdit();
      const during = {
        epoch: studioRequestEpoch,
        pendingDemandEpoch: pendingStudioDemandEpoch,
        busy: studioState.busy,
        demand: studioState.demand,
        assessDisabled: document.querySelector("#assess-demand").disabled,
      };
      await pending;
      return { aborted, edited, epochBeforeEdit, during, final: studioState };
    })()`, context) as any;

    expect(result.aborted).toBe(true);
    expect(result.edited).toBe(true);
    expect(result.during).toMatchObject({
      epoch: result.epochBeforeEdit + 1,
      pendingDemandEpoch: null,
      busy: false,
      demand: null,
      assessDisabled: true,
    });
    expect(result.final).toMatchObject({ busy: false, demand: null });
  });

  it("typing during inventory initialization does not abort the customer initialization", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      let inventoryAborted = false;
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/session") {
          return { session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" } };
        }
        if (path === "/api/studio/inventory") {
          options.signal.addEventListener("abort", () => { inventoryAborted = true; }, { once: true });
          await delay(25);
          return {
            inventory: { id: "inventory-a", items: [{ sourceAssetId: "asset-a" }] },
            materials: [{ id: "material-a", sourceAssetId: "asset-a" }],
          };
        }
        throw new Error("unexpected-path:" + path);
      };
      const pending = switchStudioCustomer("customer-a");
      await delay(1);
      const epochBeforeTyping = studioRequestEpoch;
      const edited = beginStudioDemandEdit();
      await pending;
      return {
        inventoryAborted,
        edited,
        epochBeforeTyping,
        epochAfterTyping: studioRequestEpoch,
        state: studioState,
      };
    })()`, context) as any;

    expect(result).toMatchObject({
      inventoryAborted: false,
      edited: false,
      epochAfterTyping: result.epochBeforeTyping,
      state: {
        busy: false,
        inventory: { id: "inventory-a" },
        materials: [{ id: "material-a", sourceAssetId: "asset-a" }],
      },
    });
  });

  it("rebuilds the visible workflow from the current server history after a reload", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = emptyStudioState("customer-a");
      restoreStudioWorkflowFromHistory({
        demands: [
          { id: "demand-old", version: 1, status: "superseded" },
          { id: "demand-current", version: 2, status: "current" },
        ],
        assessments: [
          { id: "match-old", version: 1, demandId: "demand-old", verdict: "blocked" },
          { id: "match-current", version: 1, demandId: "demand-current", verdict: "ready" },
        ],
        opportunities: [
          { id: "opportunity-old", demandId: "demand-old", matchAssessmentId: "match-old" },
          { id: "opportunity-current", demandId: "demand-current", matchAssessmentId: "match-current" },
        ],
        briefs: [
          { id: "brief-v1", version: 1, demandId: "demand-current" },
          { id: "brief-v2", version: 2, demandId: "demand-current" },
        ],
        drafts: [
          { id: "draft-v1", version: 1, briefId: "brief-v1" },
          { id: "draft-v2-a", version: 2, briefId: "brief-v2" },
          { id: "draft-v2-b", version: 2, briefId: "brief-v2" },
        ],
        customerFeedback: [
          { id: "feedback-old", version: 1, draftId: "draft-v1" },
          { id: "feedback-current", version: 1, draftId: "draft-v2-a" },
        ],
        revisionProposals: [
          { id: "proposal-current", version: 1, feedbackId: "feedback-current", status: "proposed" },
        ],
      });
      return studioState;
    })()`, context) as any;

    expect(result).toMatchObject({
      demand: { id: "demand-current" },
      matchAssessment: { id: "match-current" },
      opportunities: [{ id: "opportunity-current" }],
      brief: { id: "brief-v2", version: 2 },
      drafts: [{ id: "draft-v2-a" }, { id: "draft-v2-b" }],
      customerFeedback: { id: "feedback-current" },
      feedbackDraftId: "draft-v2-a",
      revisionProposal: { id: "proposal-current" },
    });
  });

  it.each(["match", "feedback"] as const)("editing demand aborts delayed %s and restores idle gates", async (kind) => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      studioState = seededV1StudioState();
      let aborted = false;
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/matches" || path === "/api/studio/customer-feedback") {
          options.signal.addEventListener("abort", () => { aborted = true; }, { once: true });
          await delay(25);
          return path === "/api/studio/matches"
            ? { assessment: { id: "old-match", verdict: "blocked" } }
            : { feedback: { id: "old-feedback", draftId: "draft-a", satisfaction: "unsatisfied" } };
        }
        return { history: seededHistory() };
      };
      const oldWork = "${kind}" === "match"
        ? assessStudioDemand()
        : submitCustomerDraftFeedback({ draftId: "draft-a", satisfaction: "satisfied", issueCategories: [],
            comment: null, mustKeep: [], mustRemove: [], needsReshoot: [], submittedBy: "customer-self-declared" });
      await delay(1);
      beginStudioDemandEdit();
      const during = {
        busy: studioState.busy,
        demand: studioState.demand,
        matchAssessment: studioState.matchAssessment,
        customerFeedback: studioState.customerFeedback,
        assessDisabled: document.querySelector("#assess-demand").disabled,
        briefDisabled: document.querySelector("#confirm-brief").disabled,
        draftDisabled: document.querySelector("#make-drafts").disabled,
      };
      await oldWork;
      return { aborted, during, final: studioState };
    })()`, context) as any;

    expect(result.aborted).toBe(true);
    expect(result.during).toMatchObject({
      busy: false, demand: null, matchAssessment: null, customerFeedback: null,
      assessDisabled: true, briefDisabled: true, draftDisabled: true,
    });
    expect(result.final).toMatchObject({ busy: false, demand: null, matchAssessment: null, customerFeedback: null });
  });

  it("escapes every new dynamic text field", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      const xss = "<img src=x onerror=alert(1)>";
      studioState = { ...emptyStudioState("customer-a"), customerFeedback: {
        id: "feedback-a", draftId: "draft-a", satisfaction: "partially-satisfied",
        comment: xss, issueCategories: [xss], mustKeep: [xss], mustRemove: [xss], needsReshoot: [xss]
      } };
      return [
        renderMatchAssessment({ verdict: "partial", safeScope: xss,
          matchedEvidence: [{ requirement: xss, explanation: xss, materialCardIds: [xss] }],
          unmetNeeds: [xss], recommendedCaptures: [xss], capability: { label: xss } }),
        renderCustomerFeedbackForm({ id: xss, version: 1 }, studioState.customerFeedback),
        renderRevisionProposal({ id: xss, status: "proposed", understoodFeedback: xss,
          plannedChanges: [xss], retainedElements: [xss], unsupportedRequests: [xss], captureGaps: [xss],
          capability: { label: xss } }),
        renderStudioHistory({
          demands: [{ id: xss, version: 1, goal: xss, targetAudience: xss, desiredEffect: xss,
            mustInclude: [xss], mustAvoid: [xss], status: "superseded" }],
          assessments: [{ id: xss, version: 1, verdict: "blocked", safeScope: xss,
            matchedEvidence: [], unmetNeeds: [xss], recommendedCaptures: [xss] }],
          opportunities: [{ id: xss, version: 1, goal: xss, rationale: xss }],
          briefs: [{ id: xss, version: 1, goal: xss, reviewRequirements: [xss] }],
          drafts: [{ id: xss, version: 1, status: "invalidated" }],
          customerFeedback: [{ id: xss, version: 1, satisfaction: "unsatisfied", comment: xss }],
          revisionProposals: [{ id: xss, version: 1, status: "proposed", understoodFeedback: xss }]
        })
      ].join("");
    })()`, context) as string;

    expect(markup).not.toContain("<img src=x onerror=alert(1)>");
    expect(markup).not.toContain('id="<img');
    expect(markup.match(/&lt;img src=x onerror=alert\(1\)&gt;/g)?.length).toBeGreaterThan(20);
  });

  it("clears new workflow state on customer switch and preserves history when editing demand", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const oldHistory = seededHistory();
      studioState = {
        ...seededV1StudioState(), matchAssessment: { id: "match-a", verdict: "ready" },
        customerFeedback: { id: "feedback-a" }, revisionProposal: { id: "proposal-a" },
        history: oldHistory, feedbackDraftId: "draft-a", gaps: [{ id: "gap-a" }]
      };
      clearStudioDemandDownstream();
      const afterEdit = JSON.parse(JSON.stringify(studioState));
      studioApi = async (path) => {
        if (path === "/api/studio/session") {
          return { session: { token: "token-b", customerId: "customer-b", sessionId: "session-b" } };
        }
        if (path === "/api/studio/history") return { history: seededHistory() };
        return { inventory: { id: "inventory-b", items: [] }, materials: [] };
      };
      await switchStudioCustomer("customer-b");
      return { afterEdit, afterSwitch: studioState };
    })()`, context) as any;

    expect(result.afterEdit).toMatchObject({
      demand: null, matchAssessment: null, opportunities: [], gaps: [], brief: null, drafts: [],
      customerFeedback: null, revisionProposal: null, feedbackDraftId: null,
    });
    expect(result.afterEdit.history).toEqual(seededHistoryExpected());
    expect(result.afterSwitch).toMatchObject({
      customerId: "customer-b", demand: null, matchAssessment: null, opportunities: [], gaps: [],
      brief: null, drafts: [], customerFeedback: null, revisionProposal: null,
      history: seededHistoryExpected(),
      feedbackDraftId: null,
    });
  });

  it("ignores delayed demand, match, feedback, and proposal responses after a newer scope starts", async () => {
    const context = await loadStudioUiContext();
    const states = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      async function runCase(kind) {
        studioState = seededV1StudioState();
        studioApi = async (path, options = {}) => {
          if (path === "/api/studio/session") return { session: { token: "token-b", customerId: "customer-b", sessionId: "session-b" } };
          if (path === "/api/studio/inventory") return { inventory: { id: "inventory-b", items: [] }, materials: [] };
          await delay(25);
          if (path === "/api/studio/demands") return { demand: { id: "old-demand", status: "current" } };
          if (path === "/api/studio/matches") return { assessment: { id: "old-match", verdict: "blocked" } };
          if (path === "/api/studio/customer-feedback") return { feedback: { id: "old-feedback", draftId: "draft-a", satisfaction: "unsatisfied" } };
          if (path === "/api/studio/revision-proposals") return { proposal: { id: "old-proposal", status: "proposed" } };
          return { history: seededHistory() };
        };
        const oldWork = kind === "demand"
          ? submitStudioDemand({ contentType: "single-product", goal: "旧需求", targetAudience: "顾客",
              desiredEffect: "理解", mustInclude: [], mustAvoid: [], submittedBy: "operator-on-behalf" })
          : kind === "match"
            ? assessStudioDemand()
            : kind === "feedback"
              ? submitCustomerDraftFeedback({ draftId: "draft-a", satisfaction: "satisfied", issueCategories: [],
                  comment: null, mustKeep: [], mustRemove: [], needsReshoot: [], submittedBy: "customer-self-declared" })
              : createRevisionProposal("feedback-a");
        await delay(1);
        await switchStudioCustomer("customer-b");
        await oldWork;
        return JSON.parse(JSON.stringify(studioState));
      }
      return [await runCase("demand"), await runCase("match"), await runCase("feedback"), await runCase("proposal")];
    })()`, context) as any[];

    for (const state of states) {
      expect(state).toMatchObject({
        customerId: "customer-b", session: { token: "token-b", customerId: "customer-b" },
        inventory: { id: "inventory-b" }, demand: null, matchAssessment: null,
        customerFeedback: null, revisionProposal: null,
      });
    }
  });

  it("lets a newer demand supersede an in-flight demand for the same customer", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" } };
      const seen = [];
      studioApi = async (path, options = {}) => {
        seen.push({ path, goal: options.body?.goal });
        if (path === "/api/studio/history") return { history: seededHistory() };
        await delay(options.body.goal === "旧需求" ? 25 : 0);
        return { demand: { id: options.body.goal === "旧需求" ? "demand-old" : "demand-new",
          ...options.body, version: options.body.goal === "旧需求" ? 1 : 2, status: "current" } };
      };
      const older = submitStudioDemand({ contentType: "single-product", goal: "旧需求", targetAudience: "顾客",
        desiredEffect: "理解", mustInclude: [], mustAvoid: [], submittedBy: "operator-on-behalf" });
      await delay(1);
      const newer = submitStudioDemand({ contentType: "designer-viewpoint", goal: "新需求", targetAudience: "准备到店的顾客",
        desiredEffect: "预约到店", mustInclude: [], mustAvoid: [], submittedBy: "customer-self-declared" });
      await Promise.all([older, newer]);
      return { state: studioState, seen };
    })()`, context) as any;

    expect(result.state.demand).toMatchObject({ id: "demand-new", goal: "新需求", version: 2 });
    expect(result.seen.filter((row: any) => row.path === "/api/studio/demands")).toHaveLength(2);
    expect(result.seen.filter((row: any) => row.path === "/api/studio/history")).toHaveLength(1);
  });

  it("renders two playable drafts with lineage and review-only decisions", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([
      {
        id: "draft-a", mediaUrl: "/api/studio/drafts/draft-a/media",
        durationSeconds: 31.5, capability: { label: "离线/模拟分析" },
        sourceMap: [{ sourceAssetId: "asset-a", sourceDigest: "digest-a", reason: "工序证据开场" }]
      },
      {
        id: "draft-b", mediaUrl: "/api/studio/drafts/draft-b/media",
        durationSeconds: 31.5, capability: { label: "离线/模拟分析" },
        sourceMap: [{ sourceAssetId: "asset-b", sourceDigest: "digest-b", reason: "设计师观点开场" }]
      }
    ])`, context);

    expect(markup.match(/<video\b/g)).toHaveLength(2);
    expect(markup.match(/\bcontrols\b/g)).toHaveLength(2);
    expect(markup.match(/class="source-drawer"/g)).toHaveLength(2);
    expect(markup.match(/class="customer-feedback-drawer"/g)).toHaveLength(2);
    expect(markup).toContain("来源与技术信息");
    expect(markup).not.toContain("data-adopt-draft");
    expect(markup.match(/data-review-before-adopt/g)).toHaveLength(2);
    expect(markup).not.toContain("data-change-draft");
    expect(markup).not.toContain("内部记录修改");
    expect(markup).not.toContain("request-changes");
    expect(markup).not.toContain("data-publish");
    expect(markup).not.toContain("/publish");
  });

  it("shows the latest active work first and folds the remaining works without losing them", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([
      { id: "draft-old", version: 1, createdAt: "2026-10-23T10:00:00.000Z", status: "reviewable",
        mediaUrl: "/api/studio/drafts/draft-old/media", durationSeconds: 31, sourceMap: [] },
      { id: "draft-current", version: 2, createdAt: "2026-10-23T11:00:00.000Z", status: "reviewable",
        mediaUrl: "/api/studio/drafts/draft-current/media", durationSeconds: 32, sourceMap: [] },
      { id: "draft-rejected", version: 3, createdAt: "2026-10-23T12:00:00.000Z", status: "rejected",
        mediaUrl: "/api/studio/drafts/draft-rejected/media", durationSeconds: 33, sourceMap: [] }
    ])`, context) as string;

    expect(markup).toContain('<details class="other-drafts-drawer">');
    expect(markup).not.toContain('<details class="other-drafts-drawer" open>');
    expect(markup).toContain("其他作品与历史版本");
    expect(markup).toContain("2 个，需要时再展开");
    expect(markup.match(/<video\b/g)).toHaveLength(3);
    expect(markup.indexOf('data-review-before-adopt="draft-current"')).toBeLessThan(
      markup.indexOf('<details class="other-drafts-drawer">'),
    );
    expect(markup.indexOf('data-review-before-adopt="draft-old"')).toBeGreaterThan(
      markup.indexOf('<details class="other-drafts-drawer">'),
    );
    expect(markup.indexOf('src="/api/studio/drafts/draft-rejected/media"')).toBeGreaterThan(
      markup.indexOf('<details class="other-drafts-drawer">'),
    );
  });

  it("shows local download and system share only after the exact work is adopted", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`({
      adopted: (() => { studioState.history = { distributionEvents: [{
        id: "distribution-1", draftId: "draft-adopted", action: "download-started",
        createdAt: "2026-10-25T08:30:00.000Z", publishClaimed: false, externalDeliveryClaimed: false
      }] }; return renderDraftCards([{
        id: "draft-adopted", version: 2, platformVariant: "xiaohongshu", status: "adopted",
        mediaUrl: "/api/studio/drafts/draft-adopted/media", durationSeconds: 35.4, sourceMap: []
      }]); })(),
      reviewable: renderDraftCards([{
        id: "draft-reviewable", version: 2, platformVariant: "douyin", status: "reviewable",
        mediaUrl: "/api/studio/drafts/draft-reviewable/media", durationSeconds: 35.4, sourceMap: []
      }])
    })`, context) as { adopted: string; reviewable: string };

    expect(result.adopted).toContain("导出与分发");
    expect(result.adopted).toContain("生成任务标记为小红书版");
    expect(result.adopted).toContain("播放后读取实际规格");
    expect(result.adopted).toContain("分享前自检");
    expect(result.adopted).toContain("分享资料");
    expect(result.adopted).toContain('data-copy-distribution="title"');
    expect(result.adopted).toContain('data-copy-distribution="caption"');
    expect(result.adopted).toContain('data-download-draft-cover="draft-adopted"');
    expect(result.adopted).toContain('data-download-distribution-note="draft-adopted"');
    expect(result.adopted).toContain("资料准备好不等于平台已接收或发布");
    expect(result.adopted).toContain("竖屏 9:16，至少 720×1280");
    expect(result.adopted).toContain('data-create-share-version="draft-adopted"');
    expect(result.adopted).toContain("不裁掉原画面、不覆盖原作品");
    expect(result.adopted).toContain("不是平台官方验收，也不保证平台上传成功");
    expect(result.adopted).toContain('href="/api/studio/drafts/draft-adopted/media"');
    expect(result.adopted).toContain('download="content-xiaohongshu-v2.mp4"');
    expect(result.adopted).toContain('data-share-draft="draft-adopted"');
    expect(result.adopted).toContain("不代表平台已验收或已经发布");
    expect(result.adopted).toContain("开始下载文件");
    expect(result.adopted).toContain("没有平台接收或发布回执");
    expect(result.reviewable).not.toContain("导出与分发");
    expect(result.reviewable).not.toContain("data-share-draft");
    expect(result.reviewable).not.toContain("data-download-draft-cover");
  });

  it("builds a platform-labelled sharing kit from confirmed task text without inventing product facts", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`({
      xhs: buildDraftDistributionKit({
        id: "draft-xhs", version: 3, platformVariant: "xiaohongshu", durationSeconds: 35
      }, { goal: "讲清楚锦红五毫米珠子的设计思路", contentType: "designer-viewpoint" }),
      channels: buildDraftDistributionKit({
        id: "draft-channels", version: 1, platformVariant: "wechat-channels", durationSeconds: 42
      }, { goal: "记录珠宝制作过程", contentType: "jewelry-making-process" })
    })`, context) as any;

    expect(result.xhs).toMatchObject({
      platformLabel: "小红书",
      videoFileName: "content-xiaohongshu-v3.mp4",
      coverFileName: "content-xiaohongshu-v3-cover.jpg",
      publishClaimed: false,
      platformAcceptedClaimed: false,
    });
    expect(result.xhs.title).toContain("锦红五毫米珠子");
    expect(result.xhs.caption).toContain("#珠宝 #设计灵感");
    expect(result.channels.title).toContain("完整记录");
    expect(result.channels.caption).toContain("#珠宝 #制作过程");
    expect(JSON.stringify(result)).not.toContain("已发布");
    expect(JSON.stringify(result)).not.toContain("平台已验收");
  });

  it("uses the renderer's local delivery baseline without claiming platform acceptance", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`({
      ready: assessLocalDraftDelivery(720, 1280, 35),
      landscape: assessLocalDraftDelivery(1920, 1080, 35),
      short: assessLocalDraftDelivery(720, 1280, 5),
      pending: assessLocalDraftDelivery(0, 0, 35),
      canFixLandscape: canCreateShareVersion(assessLocalDraftDelivery(1920, 1080, 35)),
      canFixSmall: canCreateShareVersion(assessLocalDraftDelivery(360, 640, 35)),
      cannotFixDuration: canCreateShareVersion(assessLocalDraftDelivery(720, 1280, 5)),
      doesNotFixReady: canCreateShareVersion(assessLocalDraftDelivery(720, 1280, 35))
    })`, context) as any;

    expect(result.ready).toEqual({
      status: "ready", durationReady: true, portraitReady: true, resolutionReady: true,
    });
    expect(result.landscape).toMatchObject({ status: "needs-adjustment", portraitReady: false });
    expect(result.short).toMatchObject({ status: "needs-adjustment", durationReady: false });
    expect(result.pending).toEqual({
      status: "pending", durationReady: true, portraitReady: null, resolutionReady: null,
    });
    expect(result.canFixLandscape).toBe(true);
    expect(result.canFixSmall).toBe(true);
    expect(result.cannotFixDuration).toBe(false);
    expect(result.doesNotFixReady).toBe(false);
  });

  it("labels a vertical share copy as a separate version that needs fresh review", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-share", version: 2, platformVariant: "wechat-channels", status: "reviewable",
      mediaUrl: "/api/studio/drafts/draft-share/media", durationSeconds: 35, sourceMap: [],
      shareOptimization: { parentDraftId: "draft-parent", targetWidth: 720, targetHeight: 1280,
        mode: "fit-pad-no-crop" }
    }])`, context) as string;

    expect(markup).toContain("竖屏分享副本");
    expect(markup).toContain("另存分享版");
    expect(markup).toContain("原作品未修改");
    expect(markup).toContain("需要重新播放、评价和确认");
    expect(markup).not.toContain("导出与分发");
  });

  it("focuses the newly generated share copy instead of leaving it hidden behind the adopted parent", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const parent = { id: "draft-parent", briefId: "brief-a", version: 2, status: "adopted",
        mediaUrl: "/parent.mp4", durationSeconds: 35, sourceMap: [] };
      const shared = { id: "draft-share", briefId: "brief-a", version: 2, status: "reviewable",
        mediaUrl: "/share.mp4", durationSeconds: 35, sourceMap: [],
        shareOptimization: { parentDraftId: "draft-parent", targetWidth: 720, targetHeight: 1280,
          mode: "fit-pad-no-crop" } };
      studioState = { ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        brief: { id: "brief-a", version: 2 }, drafts: [parent],
        history: { ...seededHistory(), drafts: [parent] } };
      studioApi = async (path) => path.includes("share-version")
        ? { draft: shared }
        : { history: { ...seededHistory(), drafts: [parent, shared] } };
      const trigger = { disabled: false };
      await createShareVersion("draft-parent", trigger);
      const markup = renderDraftCards(studioState.drafts);
      return { focused: studioFocusedDraftId, tab: studioActiveTab, markup, disabled: trigger.disabled };
    })()`, context) as any;

    expect(result.focused).toBe("draft-share");
    expect(result.tab).toBe("works");
    expect(result.disabled).toBe(false);
    expect(result.markup.indexOf('src="/share.mp4"')).toBeLessThan(
      result.markup.indexOf('<details class="other-drafts-drawer">'),
    );
    expect(result.markup.indexOf('src="/parent.mp4"')).toBeGreaterThan(
      result.markup.indexOf('<details class="other-drafts-drawer">'),
    );
  });

  it("adds the server-confirmed distribution receipt to the shared studio history", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), history: seededHistory() };
      studioApi = async (path, request) => {
        calls.push({ path, body: request.body });
        return { event: {
          id: "distribution-1", draftId: "draft-a", action: "download-started",
          platformVariant: "xiaohongshu", publishClaimed: false,
          externalDeliveryClaimed: false, createdAt: "2026-10-25T08:30:00.000Z"
        } };
      };
      const event = await recordDraftDistributionEvent("draft-a", "download-started", null);
      return { calls, event, history: studioState.history.distributionEvents };
    })()`, context) as any;

    expect(result.calls).toHaveLength(1);
    expect(result.calls[0].path).toBe("/api/studio/distribution-events");
    expect(result.calls[0].body).toMatchObject({ draftId: "draft-a", action: "download-started" });
    expect(result.event).toMatchObject({ publishClaimed: false, externalDeliveryClaimed: false });
    expect(result.history).toEqual([result.event]);
  });

  it("renders an inline caption editor with text, color, size, and position controls", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-caption", version: 1, mediaUrl: "/api/studio/drafts/draft-caption/media",
      durationSeconds: 35, status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      captionCues: [{ id: "cue-1", text: "自动生成字幕", reviewStatus: "model-suggestion" }],
      captionStyle: { template: "clean-outline", fontSizePx: 48, primaryColor: "#FFFFFF",
        outlineColor: "#000000", outlineWidthPx: 4, bottomMarginPx: 150, maxCharsPerLine: 14 }
    }])`, context) as string;

    expect(markup).toContain("直接修改字幕");
    expect(markup).toContain('data-caption-editor="draft-caption"');
    expect(markup).toContain('data-caption-cue-id="cue-1"');
    expect(markup).toContain("自动生成字幕");
    expect(markup).toContain('name="fontSizePx"');
    expect(markup).toContain('name="primaryColor"');
    expect(markup).toContain('name="outlineColor"');
    expect(markup).toContain('name="bottomMarginPx"');
    expect(markup).toContain("生成字幕新版");
    expect(markup).toContain("旧版不会被覆盖");
    expect(markup).not.toContain("data-caption-feedback-dataset-export");
  });

  it("offers the caption learning candidate export only after a real revision exists", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-caption-v2", version: 1, mediaUrl: "/api/studio/drafts/draft-caption-v2/media",
      durationSeconds: 35, status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      captionCues: [{ id: "cue-1", text: "人工修正字幕", reviewStatus: "model-suggestion" }],
      captionStyle: { template: "clean-outline", fontSizePx: 48, primaryColor: "#FFFFFF",
        outlineColor: "#000000", outlineWidthPx: 4, bottomMarginPx: 150, maxCharsPerLine: 14 },
      captionRevision: { parentDraftId: "draft-caption-v1", changedCueIds: ["cue-1"], styleChanged: false }
    }])`, context) as string;

    expect(markup).toContain("本版由字幕修改生成：改字 1 条");
    expect(markup).toContain("查看修正记录");
    expect(markup).toContain("导出候选包");
    expect(markup).toContain("data-caption-feedback-dataset-export");
    expect(markup).toContain("不会自动训练");
  });

  it("renders before and after caption evidence with a plain-language review filter", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioCaptionFeedbackFilter = "all";
      studioState = { ...emptyStudioState("customer-a"),
        materials: [{ sourceAssetId: "asset-a", displayName: "保山南红原料视频.mp4" }],
        captionFeedbackReviewDraftId: "draft-v2",
        captionFeedbackDataset: {
          counts: { total: 2 },
          entries: [{ revisedDraftId: "draft-v2", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64),
            sourceStartMs: 1200, sourceEndMs: 2800, timelineStartMs: 0, timelineEndMs: 1600,
            beforeText: "保珊老虹", afterText: "保山南红", workflowDecision: "unreviewed",
            workflowDecisionReason: null }]
        }
      };
      return renderDraftCards([{
        id: "draft-v2", version: 2, mediaUrl: "/api/studio/drafts/draft-v2/media", durationSeconds: 10,
        status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
        captionCues: [{ id: "cue-1", text: "保山南红", reviewStatus: "model-suggestion" }],
        captionStyle: { template: "clean-outline", fontSizePx: 48, primaryColor: "#FFFFFF",
          outlineColor: "#000000", outlineWidthPx: 4, bottomMarginPx: 150, maxCharsPerLine: 14 },
        captionRevision: { parentDraftId: "draft-v1", changedCueIds: ["cue-1"], styleChanged: false }
      }]);
    })()`, context) as string;

    expect(markup).toContain("保山南红原料视频.mp4");
    expect(markup).toContain("保珊老虹");
    expect(markup).toContain("保山南红");
    expect(markup).toContain("1.2–2.8 秒");
    expect(markup).toContain("data-caption-feedback-filter");
    expect(markup).toContain("未经人工审数据，不进入训练");
  });

  it("shows exact visual-model receipts and explains when no real receipt was used", async () => {
    const context = await loadStudioUiContext();
    const used = runInContext(`renderDraftCards([{
      id: "draft-vision", version: 1, mediaUrl: "/draft-vision.mp4", durationSeconds: 35,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      visualEvidence: { status: "used", degradedReason: null, items: [{
        shotId: "shot-1", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64),
        sourceStartMs: 0, sourceEndMs: 5000, receiptJobId: "vision-job-1",
        provider: { layer: "vision", adapterName: "mlx-vlm-worker",
          modelName: "qwen3-vl-2b-instruct-4bit", modelVersion: "fixture-v1",
          checkpointDigest: "b".repeat(64) },
        observations: ["商品主体清晰", "珠宝制作场景"],
        selectionReason: "视觉模型识别到商品主体，与镜头用途一致"
      }] }
    }])`, context) as string;
    expect(used).toContain("视觉模型仅做内容核对 · 1 个镜头");
    expect(used).toContain("商品主体清晰 · 珠宝制作场景");
    expect(used).toContain("视觉模型识别到商品主体，与镜头用途一致");
    expect(used).toContain("qwen3-vl-2b-instruct-4bit fixture-v1 · 回执 vision-job-1");

    const selected = runInContext(`renderDraftCards([{
      id: "draft-vision-selected", version: 1, mediaUrl: "/draft-vision-selected.mp4", durationSeconds: 35,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      visualEvidence: { status: "used", degradedReason: null, items: [{
        shotId: "shot-1", sourceAssetId: "asset-a", sourceDigest: "a".repeat(64),
        sourceStartMs: 0, sourceEndMs: 5000, receiptJobId: "vision-job-1",
        provider: { layer: "vision", adapterName: "mlx-vlm-worker",
          modelName: "qwen3-vl-2b-instruct-4bit", modelVersion: "fixture-v1",
          checkpointDigest: "b".repeat(64) },
        observations: ["机翼与落日颜色清晰"],
        selectionReason: "根据本地视觉时间证据定位"
      }] }
    }])`, context) as string;
    expect(selected).toContain("视觉模型参与选片 · 1 个镜头");

    const unused = runInContext(`renderDraftCards([{
      id: "draft-no-vision", version: 1, mediaUrl: "/draft-no-vision.mp4", durationSeconds: 35,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      visualEvidence: { status: "not-used", items: [],
        degradedReason: "没有与当前素材严格匹配的本地真实视觉模型回执" }
    }])`, context) as string;
    expect(unused).toContain("本版未使用视觉模型");
    expect(unused).toContain("没有与当前素材严格匹配的本地真实视觉模型回执");
  });

  it("shows whether a draft used reliable music beats or the fixed-rhythm fallback", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-beat", version: 1, mediaUrl: "/draft-beat.mp4", durationSeconds: 18,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      beatSyncPlan: { mode: "beat-synced", detector: "audio-energy-onset-v1",
        detectedBeatCount: 35, alignedCutCount: 2, cutTimesMs: [4510, 13510],
        toleranceMs: 360, confidence: 1, reason: "已将 2 个镜头切点对齐到可靠重拍" }
    }, {
      id: "draft-fallback", version: 1, mediaUrl: "/draft-fallback.mp4", durationSeconds: 18,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      beatSyncPlan: { mode: "rule-fallback", detector: "audio-energy-onset-v1",
        detectedBeatCount: 0, alignedCutCount: 0, cutTimesMs: [],
        toleranceMs: 360, confidence: 0, reason: "未检测到可靠重拍，沿用平台固定节奏" }
    }])`, context) as string;

    expect(markup).toContain("音乐卡点 2 处");
    expect(markup).toContain("已将 2 个镜头切点对齐到可靠重拍");
    expect(markup).toContain("固定节奏回退");
  });

  it("shows the selected music rights boundary on the rendered draft", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-music", version: 1, mediaUrl: "/draft-music.mp4", durationSeconds: 18,
      status: "reviewable", sourceMap: [], capability: { label: "本地平台自动剪辑 v1" },
      musicMixPlan: { mode: "mixed", reason: "保留原声并自动压低配乐",
        rights: { previewUseAllowed: true, publishUseAllowed: false } }
    }])`, context) as string;
    expect(markup).toContain("配乐混音 · 仅预览");
    expect(markup).toContain("保留原声并自动压低配乐");
  });

  it("renders music preview, selection, and per-track mix controls", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(studioState.brief = { id: "brief-a" }, renderMusicSelection({ selectedTrackId: "music-a", recommendations: [{
      trackId: "music-a", rank: 1, score: 92, evidenceMode: "visual-model-supported",
      preferenceSignal: { selectedCount: 3, clearedCount: 0, mixAdjustedCount: 1, preferenceScore: 10 },
      reasons: ["内容需要有力的音乐"], modelReceipts: [{ receiptJobId: "vision-job-1",
        modelName: "qwen3-vl-2b-instruct-4bit", modelVersion: "2026-02" }]
    }], tracks: [{
      assetId: "music-a", fileName: "客户原创.m4a", durationSeconds: 12,
      mediaUrl: "/api/studio/music/music-a/media",
      rights: { licenseNote: "客户原创", publishUseAllowed: false },
      mixPreference: { trackStartMs: 1500, originalGainDb: -2, musicGainDb: -18 },
      audioProfile: { energy: "strong", tempoBpm: 120, highlightStartMs: 1500,
        waveform: [0.2, 0.7, 1, 0.5], reason: "本地分析建议从 1.5 秒附近进入" }
    }, {
      assetId: "music-b", fileName: "商业授权.mp3", durationSeconds: 20,
      mediaUrl: "/api/studio/music/music-b/media",
      rights: { licenseNote: "商业授权编号 A-1", publishUseAllowed: true },
      mixPreference: { trackStartMs: 0, originalGainDb: 0, musicGainDb: -16 }
    }] }))`, context) as string;
    expect(markup).toContain('src="/api/studio/music/music-a/media"');
    expect(markup).toContain('value="1.5"');
    expect(markup).toContain('value="-2"');
    expect(markup).toContain('value="-18"');
    expect(markup).toContain('data-select-music="music-b"');
    expect(markup).toContain("商业授权编号 A-1");
    expect(markup).toContain("约 120 BPM");
    expect(markup).toContain("推荐起点 1.5 秒");
    expect(markup).toContain('class="music-waveform"');
    expect(markup).toContain("推荐第 1 名 · 92 分");
    expect(markup).toContain("视觉模型回执辅助");
    expect(markup).toContain("同类视频偏好 +10 分");
    expect(markup).toContain("qwen3-vl-2b-instruct-4bit 2026-02 · 回执 vision-job-1");
  });

  it("keeps the review card simple and tucks AI and Jianying refinement behind one edit choice", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([{
      id: "draft-v1", version: 1, mediaUrl: "/api/studio/drafts/draft-v1/media",
      durationSeconds: 35, status: "reviewable", sourceMap: [], capability: { label: "离线/模拟分析" }
    }])`, context);

    expect(markup).toContain(">先评价再通过</button>");
    expect(markup).toContain('data-review-before-adopt="draft-v1"');
    expect(markup).not.toContain('data-adopt-draft="draft-v1"');
    expect(markup).not.toContain("<summary>修改这版</summary>");
    expect(markup).toContain(">让 AI 修改</button>");
    expect(markup).toContain(">去剪映精修</button>");
    expect(markup).toContain("<summary>来源与技术信息</summary>");
    expect(markup).toContain("data-external-edit-form=\"draft-v1\"");
    expect(markup).toContain("修改说明");
    expect(markup).toContain("必保留");
    expect(markup).toContain("附带原素材");
    expect(markup).toContain("后台准备剪映交接");
    expect(markup).toContain("不打开剪映、不抢占桌面");
    expect(markup).not.toContain("已自动导入剪映");
    const feedback = runInContext(`renderCustomerFeedbackForm({ id: "draft-v1" })`, context);
    expect(feedback).toContain("<summary>评价这版</summary>");
    expect(feedback).toContain("满意会解锁“确认通过”");
    expect(feedback).toContain("看完觉得怎么样？");
    expect(feedback).toContain("一句话意见（可选）");
    expect(feedback).toContain('<details class="customer-feedback-more">');
    expect(feedback).not.toContain('<details class="customer-feedback-more" open>');
    expect(feedback).toContain("详细修改要求（可选）");
    expect(feedback).toContain(">保存评价</button>");
    expect(feedback).toContain("只用现有素材调整，不安排补拍");
    expect(feedback).not.toContain('name="needsReshoot"');
  });

  it("explains why a phone cannot use a loopback-only hardware gateway", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), hardwarePairingError:
        "本机处理没有完成（hardware-lan-disabled），可以保留当前页面后重试。" };
      return renderHardwarePairing();
    })()`, context) as string;

    expect(markup).toContain("需要切换到手机连接模式");
    expect(markup).toContain("手机扫任何二维码都连不上");
    expect(markup).toContain("pnpm studio:demo:lan");
    expect(markup).not.toContain("重新生成二维码");
  });

  it("shows the private LAN address beside an active hardware QR code", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), hardwarePairing: {
        id: "pairing-a", status: "waiting", pairingUrl: "http://192.168.5.55:4273/hardware/pair?code=secret",
        qrDataUrl: "data:image/png;base64,fixture", expiresAt: "2026-10-10T12:00:00.000Z", deviceName: null
      } };
      return renderHardwarePairing();
    })()`, context) as string;

    expect(markup).toContain("连接地址：192.168.5.55:4273");
    expect(markup).not.toContain("code=secret");
  });

  it("uses no-retake customer language while retaining legacy contract fields", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      const feedback = renderCustomerFeedbackForm({ id: "draft-a" }, {
        satisfaction: "unsatisfied", comment: "当前素材缺少成品画面", issueCategories: [],
        mustKeep: [], mustRemove: [], needsReshoot: ["成品结尾"]
      });
      const proposal = renderRevisionProposal({ id: "proposal-a", version: 1, status: "proposed",
        understoodFeedback: "现有素材无法完成：成品结尾", plannedChanges: [], retainedElements: [],
        unsupportedRequests: [], captureGaps: ["成品结尾"], capability: {} });
      return feedback + proposal;
    })()`, context) as string;

    expect(markup).toContain("现有素材无法完成");
    expect(markup).toContain("现有素材限制");
    expect(markup).not.toContain("需要补拍");
    expect(markup).not.toContain("补拍缺口");
  });

  it("puts existing-video upload in the material library instead of the works area", async () => {
    const html = await readFile(join(publicRoot, "studio.html"), "utf8");
    const materialStart = html.indexOf('data-studio-step="materials"');
    const matchStart = html.indexOf('data-studio-step="match"');
    const worksStart = html.indexOf('data-studio-step="drafts"');
    const upload = html.indexOf('id="material-upload-form"');

    expect(upload).toBeGreaterThan(materialStart);
    expect(upload).toBeLessThan(matchStart);
    expect(upload).toBeLessThan(worksStart);
    expect(html).toContain("添加素材");
    expect(html).toContain("上传后放进素材库");
    expect(html).toContain("内容类型（可选）");
    expect(html).toContain("可一次选择多条视频");
    expect(html).toContain('accept="video/mp4,video/quicktime,.mp4,.mov"');
    expect(html).toContain("multiple required");
    expect(html).toContain("批量检查并入库");
    expect(html).toContain("每批最多 20 条");
    expect(html).toContain('id="material-upload-results"');
    expect(html).not.toContain("上传为 V1 初稿");
  });

  it("renders an auditable batch intake receipt without creating human labels", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderMaterialUploadReport({
      schemaVersion: 1, generatedAt: "2026-10-15T03:00:00.000Z",
      summary: { selected: 3, imported: 1, duplicate: 1, rejected: 1, failed: 0 },
      items: [
        { fileName: "合格.mp4", status: "imported", reason: "媒体检查通过并已进入人工高光队列", sourceDigest: "${"a".repeat(64)}" },
        { fileName: "重复.mp4", status: "duplicate", reason: "与素材库或本批次已有视频完全相同", sourceDigest: "${"a".repeat(64)}" },
        { fileName: "错误.txt", status: "rejected", reason: "只接受 MP4 或 MOV 视频", sourceDigest: null }
      ]
    })`, context) as string;

    expect(markup).toContain("本次批量入库回执");
    expect(markup).toContain("选择 3 条 · 入库 1 条 · 重复 1 条 · 未入库 1 条");
    expect(markup).toContain("重复已跳过");
    expect(markup).toContain("人工高光标签新增 0 条");
    expect(markup).toContain("合成素材计入真实样本 0 条");
    expect(markup).toContain("自动发布：否");
    expect(markup).toContain("data-export-material-upload-report");
  });

  it("imports unique videos sequentially and skips existing and same-batch duplicates", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      FormData = class { append() {} };
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", sessionId: "session-a" },
        materials: [{ id: "existing", sourceDigest: "${"a".repeat(64)}" }]
      };
      const files = [
        { name: "已有.mp4", type: "video/mp4", size: 10 },
        { name: "新视频.mp4", type: "video/mp4", size: 20 },
        { name: "同批重复.mov", type: "video/quicktime", size: 20 },
        { name: "错误.txt", type: "text/plain", size: 5 }
      ];
      studioFileSha256 = async (file) => file.name === "已有.mp4" ? "${"a".repeat(64)}" : "${"b".repeat(64)}";
      let fetchCount = 0;
      fetch = async () => {
        fetchCount += 1;
        return { ok: true, json: async () => ({
          inventory: { items: [{ materialCardId: "card-new" }] },
          materialTags: { version: 0, tags: [], assignments: [] },
          material: { id: "card-new", sourceAssetId: "asset-new", sourceDigest: "${"b".repeat(64)}" },
          materials: []
        }) };
      };
      refreshStudioHighlights = async () => [];
      const fileName = { textContent: "" };
      const form = {
        elements: { namedItem: (name) => name === "file" ? { files } : { value: "single-product" } },
        querySelector: () => fileName,
        reset() {}
      };
      await uploadStudioMaterial(form);
      return { fetchCount, report: studioState.materialUploadReport, fileName: fileName.textContent };
    })()`, context) as any;

    expect(result.fetchCount).toBe(1);
    expect(result.report.summary).toEqual({ selected: 4, imported: 1, duplicate: 2, rejected: 1, failed: 0 });
    expect(result.report.items.map((item: any) => item.status)).toEqual([
      "duplicate", "imported", "duplicate", "rejected",
    ]);
    expect(result.report.sessionId).toBe("session-a");
    expect(result.report.boundary).toEqual({ humanLabelsCreated: 0, syntheticCountedAsReal: 0, automaticPublishing: false });
    expect(result.fileName).toBe("可一次选择多条视频");
  });

  it("shows external-editor V2 lineage beside the original version", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderDraftCards([
      { id: "draft-v1", version: 1, mediaUrl: "/v1.mp4", durationSeconds: 35, sourceMap: [], capability: {} },
      { id: "draft-v2", version: 2, mediaUrl: "/v2.mp4", durationSeconds: 34, sourceMap: [], capability: {},
        externalEdit: { origin: "external-editor-return", externalEditor: "unverified-external-file", declaredEditor: "jianying-desktop", editorProvenance: "return-folder-only", parentDraftId: "draft-v1", handoffId: "handoff-1", lineageConfidence: "parent-only" } }
    ])`, context);

    expect(markup).toContain("V1");
    expect(markup).toContain("外部回传待确认");
    expect(markup).toContain("来源待确认");
    expect(markup).toContain("只确认父版本，不证明导出软件");
  });

  it("retries package preparation without duplicating the handoff or erasing the entered instruction", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" },
        drafts: [{ id: "draft-a", version: 1, outputDigest: "a".repeat(64), sourceMap: [] }] };
      const calls = [];
      let prepareCount = 0;
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/external-edit-handoffs") return { handoff: { id: "handoff-a", status: "preparing" } };
        if (path.endsWith("/prepare") && prepareCount++ === 0) throw new Error("temporary-prepare-error");
        if (path.endsWith("/open")) return { opened: true, jianyingOpened: false, packageFolderOpened: true, returnFolderOpened: true };
        return { handoff: { id: "handoff-a", status: "degraded" }, mode: "file-package", degradedReason: "jianying-not-installed" };
      };
      const values = { revisionInstruction: "节奏加快，保留设计师原话", mustKeep: "设计师原话", includeSources: { checked: false }, platformVariant: "editorial-master" };
      const form = { dataset: { externalEditForm: "draft-a" }, elements: { namedItem: (name) => ({ value: values[name] ?? "", checked: Boolean(values[name]?.checked) }) }, querySelector: () => ({ textContent: "" }) };
      await submitExternalEditForm(form);
      await submitExternalEditForm(form);
      return { calls, flow: studioState.externalEdits["draft-a"] };
    })()`, context) as any;

    expect(result.calls.filter((call: any) => call.path === "/api/studio/external-edit-handoffs")).toHaveLength(1);
    expect(result.calls.filter((call: any) => call.path.endsWith("/prepare"))).toHaveLength(2);
    expect(result.calls.filter((call: any) => call.path.endsWith("/open"))).toHaveLength(0);
    expect(result.calls[0].body).toMatchObject({ revisionInstruction: "节奏加快，保留设计师原话", sourceAssetIds: [] });
    expect(result.flow.handoff.status).toBe("degraded");
    expect(result.flow.openResult).toBeUndefined();
  });

  it("reuses the persisted active handoff after a browser or server restart", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" },
        drafts: [{ id: "draft-a", version: 1, outputDigest: "a".repeat(64), sourceMap: [] }] };
      const calls = [];
      studioApi = async (path, options = {}) => {
        calls.push({ path, method: options.method || "POST" });
        if (path === "/api/studio/external-edit-handoffs" && options.method !== "GET") {
          throw new Error("external-edit-active-handoff-exists");
        }
        if (path === "/api/studio/external-edit-handoffs") return { handoffs: [{
          id: "handoff-a", parentDraftId: "draft-a", status: "degraded", platformVariant: "editorial-master"
        }] };
        if (path.endsWith("/prepare")) return { handoff: {
          id: "handoff-a", parentDraftId: "draft-a", status: "degraded", platformVariant: "editorial-master"
        }, mode: "file-package", health: "unsupported-version" };
        return { opened: true, jianyingOpened: true, packageFolderOpened: false, returnFolderOpened: true,
          automation: { attempted: true, projectCreated: true, materialImported: true, timelineImported: false,
            error: "jianying-timeline-step-required" } };
      };
      const values = { revisionInstruction: "继续精修", mustKeep: "", includeSources: { checked: false }, platformVariant: "editorial-master" };
      const form = { dataset: { externalEditForm: "draft-a" }, elements: { namedItem: (name) => ({ value: values[name] ?? "", checked: Boolean(values[name]?.checked) }) }, querySelector: () => ({ textContent: "" }) };
      await submitExternalEditForm(form);
      return { calls, flow: studioState.externalEdits["draft-a"] };
    })()`, context) as any;

    expect(result.calls.filter((call: any) => call.path === "/api/studio/external-edit-handoffs")).toHaveLength(2);
    expect(result.calls.filter((call: any) => call.path.endsWith("/prepare"))).toHaveLength(1);
    expect(result.calls.filter((call: any) => call.path.endsWith("/open"))).toHaveLength(0);
    expect(result.flow.handoff.id).toBe("handoff-a");
    expect(result.flow.prepared.health).toBe("unsupported-version");
  });

  it("labels the selected platform through the Jianying handoff and returned candidate", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), externalEdits: { "draft-a": {
        handoff: { id: "handoff-a", status: "candidate-found", platformVariant: "xiaohongshu" },
        prepared: { mode: "file-package", health: "unsupported-version", degradedReason: "jianying-version-not-verified" },
        openResult: { opened: true, jianyingOpened: true, packageFolderOpened: true, returnFolderOpened: true },
        candidates: [{ id: "candidate-a", mediaUrl: "/candidate.mp4", durationSeconds: 30,
          mediaFacts: { width: 720, height: 1280 }, status: "pending-review" }]
      } } };
      return renderExternalEditPanel({ id: "draft-a" });
    })()`, context);

    expect(markup.match(/>小红书版</g)).toHaveLength(2);
    expect(markup).toContain("平台身份已跟随本次交接任务保存");
    expect(markup).toContain("打开剪映和回传文件夹（会切到前台）");
    expect(markup).toContain("已经导出？直接选择成片");
    expect(markup).toContain("上传并预览");
    expect(markup).toContain("不证明文件来自剪映");
    expect(markup).toContain("请先完整播放检查，再决定是否确认为 V2");
    expect(markup).toContain('data-external-edit-panel="draft-a" open');
    expect(markup).toContain('data-external-accept="draft-a" data-candidate-id="candidate-a" disabled');
    expect(markup).toContain("720×1280");
    expect(markup).not.toContain("已自动导入剪映");
  });

  it("uploads one manually selected export into preview without auto-accepting it", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const appended = [];
      FormData = class { append(name, value) { appended.push({ name, value: value?.name || value }); } };
      const calls = [];
      const candidate = { id: "candidate-uploaded", status: "pending-review", mediaUrl: "/candidate.mp4" };
      fetch = async (path, options) => {
        calls.push({ path, method: options.method, authorization: options.headers.authorization });
        return { ok: true, json: async () => ({ candidate }) };
      };
      renderStudioState = () => {};
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" },
        externalEdits: { "draft-a": { handoff: { id: "handoff-a", status: "editing" }, candidates: [] } } };
      const status = { textContent: "" };
      const submit = { disabled: false };
      const file = { name: "剪映导出.mp4", size: 1024 };
      const form = {
        dataset: { externalReturnUpload: "draft-a" },
        elements: { namedItem: (name) => name === "file" ? { files: [file] } : null },
        closest: () => ({ querySelector: () => status }),
        querySelector: () => submit,
      };
      await uploadExternalEditCandidate(form);
      return { calls, appended, flow: studioState.externalEdits["draft-a"], status: status.textContent, submit };
    })()`, context) as any;

    expect(result.calls).toEqual([{
      path: "/api/studio/external-edit-handoffs/handoff-a/candidates/import",
      method: "POST",
      authorization: "Bearer token-a",
    }]);
    expect(result.appended.map((item: any) => item.name)).toEqual(["contentType", "operationKey", "file"]);
    expect(result.flow.handoff.status).toBe("candidate-found");
    expect(result.flow.candidates).toEqual([{ id: "candidate-uploaded", status: "pending-review", mediaUrl: "/candidate.mp4" }]);
    expect(result.flow.previewedCandidates).toEqual([]);
    expect(result.submit.disabled).toBe(false);
  });

  it("explains external return failures and leaves the same upload ready to retry", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      FormData = class { append() {} };
      renderStudioState = () => {};
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" },
        externalEdits: { "draft-a": { handoff: { id: "handoff-a", status: "editing" }, candidates: [] } } };
      const status = { textContent: "" };
      const submit = { disabled: false };
      const file = { name: "剪映导出.mp4", size: 1024 };
      const form = {
        dataset: { externalReturnUpload: "draft-a" },
        elements: { namedItem: () => ({ files: [file] }) },
        closest: () => ({ querySelector: () => status }),
        querySelector: () => submit,
      };
      const messages = {};
      for (const code of [
        "external-edit-upload-duration-invalid", "external-edit-upload-media-invalid",
        "external-edit-upload-too-large", "external-edit-upload-output-invalid",
        "external-edit-handoff-not-awaiting-return", "network-disconnected"
      ]) {
        fetch = async () => ({ ok: false, json: async () => ({ error: code }) });
        await uploadExternalEditCandidate(form);
        messages[code] = status.textContent;
      }
      return { messages, submit, candidates: studioState.externalEdits["draft-a"].candidates };
    })()`, context) as any;

    expect(result.messages["external-edit-upload-duration-invalid"]).toContain("30–60 秒");
    expect(result.messages["external-edit-upload-media-invalid"]).toContain("视频无法读取");
    expect(result.messages["external-edit-upload-too-large"]).toContain("256 MB");
    expect(result.messages["external-edit-upload-output-invalid"]).toContain("原文件没有被采用");
    expect(result.messages["external-edit-handoff-not-awaiting-return"]).toContain("重新发起");
    expect(result.messages["network-disconnected"]).toContain("不会重复生成，可直接重试");
    expect(result.submit.disabled).toBe(false);
    expect(result.candidates).toEqual([]);
  });

  it("rebuilds the local Jianying bridge before reopening after a server restart", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      let openCount = 0;
      studioState = { ...emptyStudioState("customer-a"), externalEdits: { "draft-a": {
        handoff: { id: "handoff-a", status: "degraded", platformVariant: "editorial-master" },
        prepared: { mode: "file-package", health: "unsupported-version" }
      } } };
      studioApi = async (path) => {
        calls.push(path);
        if (path.endsWith("/open") && openCount++ === 0) throw new Error("external-edit-prepare-required");
        if (path.endsWith("/prepare")) return {
          handoff: { id: "handoff-a", status: "degraded", platformVariant: "editorial-master" },
          mode: "file-package", health: "unsupported-version"
        };
        return { opened: true, jianyingOpened: true, packageFolderOpened: false, returnFolderOpened: true,
          automation: { attempted: true, projectCreated: true, materialImported: true, timelineImported: false,
            error: "jianying-timeline-step-required" } };
      };
      await openExternalEditPackage("draft-a");
      return { calls, flow: studioState.externalEdits["draft-a"] };
    })()`, context) as any;

    expect(result.calls.filter((path: string) => path.endsWith("/open"))).toHaveLength(2);
    expect(result.calls.filter((path: string) => path.endsWith("/prepare"))).toHaveLength(1);
    expect(result.flow.error).toBeNull();
    expect(result.flow.openResult.automation.materialImported).toBe(true);
    expect(result.flow.autoReturnWatch).toBe(true);
  });

  it("quietly detects a returned Jianying video and stops the background watcher", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = { ...emptyStudioState("customer-a"), externalEdits: { "draft-a": {
        handoff: { id: "handoff-a", status: "editing", platformVariant: "editorial-master" },
        autoReturnWatch: true, candidates: []
      } } };
      externalEditCandidatePollDraftId = "draft-a";
      externalEditCandidatePoll = 1;
      studioApi = async () => ({ candidates: [{ id: "candidate-a", status: "pending-review", mediaUrl: "/candidate-a.mp4" }] });
      await pollExternalEditCandidates("draft-a");
      return studioState.externalEdits["draft-a"];
    })()`, context) as any;

    expect(result.handoff.status).toBe("candidate-found");
    expect(result.autoReturnWatch).toBe(false);
    expect(result.candidates).toHaveLength(1);
  });

  it("shows verified Jianying automation progress without claiming a native API", async () => {
    const context = await loadStudioUiContext();
    const timelineReady = runInContext(`externalEditStatusLabel("degraded", {
      handoff: { platformVariant: "douyin" },
      openResult: { opened: true, jianyingOpened: true, automation: {
        attempted: true, projectCreated: true, materialImported: true, timelineImported: true, error: null
      } }
    })`, context);
    const manualTimeline = runInContext(`externalEditStatusLabel("degraded", {
      handoff: { platformVariant: "wechat-channels" },
      openResult: { opened: true, jianyingOpened: true, automation: {
        attempted: true, projectCreated: true, materialImported: true, timelineImported: false,
        error: "jianying-timeline-step-required"
      } }
    })`, context);

    expect(timelineReady).toContain("已自动进入剪映时间线");
    expect(manualTimeline).toContain("已自动导入剪映");
    expect(manualTimeline).toContain("拖到下方时间线");
  });

  it("does not accept an external candidate until that exact candidate has been previewed", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = { ...emptyStudioState("customer-a"), session: { token: "token-a", customerId: "customer-a" },
        drafts: [{ id: "draft-v1", version: 1, briefId: "brief-a" }],
        externalEdits: { "draft-v1": { handoff: { id: "handoff-a", status: "candidate-found" }, candidates: [{ id: "candidate-a" }] } } };
      studioApi = async (path) => {
        calls.push(path);
        if (path.endsWith("/accept")) return { draft: { id: "draft-v2", version: 2, briefId: "brief-a", externalEdit: { lineageConfidence: "parent-only" } } };
        return { history: { drafts: [{ id: "draft-v1", version: 1, briefId: "brief-a" }, { id: "draft-v2", version: 2, briefId: "brief-a", externalEdit: { lineageConfidence: "parent-only" } }] } };
      };
      await acceptExternalEditCandidate("draft-v1", "candidate-a");
      const beforePreview = [...calls];
      studioState.externalEdits["draft-v1"].previewedCandidates = ["candidate-a"];
      await acceptExternalEditCandidate("draft-v1", "candidate-a");
      return { beforePreview, calls, drafts: studioState.drafts };
    })()`, context) as any;

    expect(result.beforePreview).toEqual([]);
    expect(result.calls.filter((path: string) => path.endsWith("/accept"))).toHaveLength(1);
    expect(result.drafts.map((draft: any) => draft.id)).toEqual(["draft-v1", "draft-v2"]);
  });

  it("refuses the legacy request-changes UI path without making a network request", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        drafts: [{ id: "draft-a", mediaUrl: "/api/studio/drafts/draft-a/media",
          durationSeconds: 31.5, sourceMap: [{ sourceAssetId: "asset-a", sourceDigest: "digest-a",
            reason: "工序证据" }], capability: { label: "离线/模拟分析" } }]
      };
      const markup = renderDraftCards(studioState.drafts);
      const requests = [];
      studioApi = async (path, options) => {
        requests.push({ path, body: options.body });
        throw new Error("legacy-request-must-not-reach-network");
      };
      await submitStudioFeedback("draft-a", "request-changes", "请让设计师观点更突出");
      return { markup, requests, state: studioState };
    })()`, context) as any;

    expect(result.markup).not.toContain("data-change-reason");
    expect(result.markup).not.toContain("data-submit-change");
    expect(result.requests).toEqual([]);
    expect(result.state.feedback).toBeNull();
  });

  it("keeps merchandise facts unselected until the user explicitly checks them", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const material = {
        id: "material-a", observations: ["合成戒指近景"], sourceStartMs: 0, sourceEndMs: 10500,
        capability: { label: "离线/模拟分析" },
        pendingFacts: ["fact-a: 示例属性字段待人工确认", "fact-b: 示例尺寸字段待人工确认"]
      };
      studioState = {
        customerId: "customer-a", session: { token: "token-a", customerId: "customer-a" },
        inventory: { items: [{ sourceAssetId: "asset-a" }] }, materials: [material],
        opportunities: [{ id: "opportunity-a", goal: "讲戒指", rationale: "素材可用",
          capability: { label: "离线/模拟分析" } }], brief: null, drafts: [], feedback: null,
        selectedFactIds: [], busy: false
      };
      const defaultMarkup = renderMaterials(studioState.materials);
      studioState.selectedFactIds = ["fact-b: 尺寸为 12 号"];
      let submitted = null;
      studioApi = async (path, options) => {
        if (path === "/api/studio/history") return { history: seededHistory() };
        if (path === "/api/studio/music/list") return { tracks: [], selectedTrackId: null };
        if (path === "/api/studio/briefs") submitted = options.body;
        return { brief: { id: "brief-a", version: 1, goal: "讲戒指",
          selectedAssetIds: ["asset-a"], confirmedFactIds: ["fact-b: 尺寸为 12 号"],
          reviewRequirements: ["采用前人工复核"], capability: { label: "离线/模拟分析" } } };
      };
      await confirmStudioBrief();
      return { defaultMarkup, submitted };
    })()`, context) as any;

    expect(result.defaultMarkup.match(/data-studio-fact-id=/g)).toHaveLength(2);
    expect(result.defaultMarkup).not.toMatch(/data-studio-fact-id=[^>]+checked/);
    expect(result.submitted.confirmedFactIds).toEqual(["fact-b: 尺寸为 12 号"]);
  });

  it("shows the exact material tray before generating a draft", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderBrief(null, {
      id: "opportunity-a", goal: "讲清楚设计灵感", rationale: "产品和口播可以组成完整故事",
      materialCardIds: ["card-a", "card-b"]
    }, [
      { id: "card-a", displayName: "微信视频2026-10-02.mp4", observations: ["å¾®ä¿¡è§é¢2026-10-02.mp4"], contentType: "single-product", sourceStartMs: 0, sourceEndMs: 10000 },
      { id: "card-b", observations: ["设计师口播"], contentType: "designer-viewpoint", sourceStartMs: 2000, sourceEndMs: 14000 }
    ])`, context);

    expect(markup).toContain("Agent 准备用这 2 段素材");
    expect(markup).toContain("素材够用，不需要再上传");
    expect(markup).toContain("微信视频2026-10-02.mp4");
    expect(markup).not.toContain("å¾®ä¿¡è§é¢2026-10-02.mp4");
    expect(markup).toContain("设计师口播");
  });

  it("submits only the source assets selected for the current opportunity", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      let submitted = null;
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { items: [
          { materialCardId: "card-a", sourceAssetId: "asset-a" },
          { materialCardId: "card-b", sourceAssetId: "asset-b" },
          { materialCardId: "card-c", sourceAssetId: "asset-c" }
        ] },
        matchAssessment: { id: "match-a", verdict: "ready" },
        opportunities: [{
          id: "opportunity-a", goal: "讲设计灵感", rationale: "素材可用",
          materialCardIds: ["card-c", "card-a"]
        }],
      };
      studioApi = async (path, options) => {
        if (path === "/api/studio/history") return { history: seededHistory() };
        if (path === "/api/studio/music/list") return { tracks: [], selectedTrackId: null };
        if (path === "/api/studio/briefs") submitted = options.body;
        return { brief: { id: "brief-a", version: 1, goal: "讲设计灵感", selectedAssetIds: options.body.selectedAssetIds,
          confirmedFactIds: [], reviewRequirements: ["人工复核"] } };
      };
      await confirmStudioBrief();
      return submitted;
    })()`, context) as any;

    expect(result.selectedAssetIds).toEqual(["asset-c", "asset-a"]);
  });

  it("carries staged music and its mix into the first confirmed brief", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { items: [{ materialCardId: "card-a", sourceAssetId: "asset-a" }] },
        matchAssessment: { id: "match-a", verdict: "ready" },
        opportunities: [{ id: "opportunity-a", materialCardIds: ["card-a"] }],
        music: {
          selectedTrackId: "music-a",
          recommendations: [],
          tracks: [{ assetId: "music-a", fileName: "客户配乐.m4a",
            mixPreference: { trackStartMs: 2600, originalGainDb: -2, musicGainDb: -18 } }],
        },
      };
      studioApi = async (path, options = {}) => {
        calls.push({ path, body: options.body });
        if (path === "/api/studio/briefs") return { brief: {
          id: "brief-a", version: 1, selectedAssetIds: ["asset-a"], confirmedFactIds: [],
          reviewRequirements: ["人工复核"]
        } };
        if (path === "/api/studio/history") return { history: seededHistory() };
        if (path === "/api/studio/music/list") return {
          selectedTrackId: "music-a", recommendations: [], tracks: [{ assetId: "music-a" }]
        };
        if (path === "/api/studio/music/recommend") return { recommendations: [] };
        return { selectedTrackId: "music-a", tracks: [] };
      };
      await confirmStudioBrief();
      return calls;
    })()`, context) as any[];

    expect(result.map((call) => call.path)).toEqual([
      "/api/studio/briefs",
      "/api/studio/history",
      "/api/studio/music/select",
      "/api/studio/music/mix",
      "/api/studio/music/list",
      "/api/studio/music/recommend",
    ]);
    expect(result[2].body).toEqual({ briefId: "brief-a", trackId: "music-a" });
    expect(result[3].body).toEqual({
      briefId: "brief-a",
      trackId: "music-a",
      trackStartMs: 2600,
      originalGainDb: -2,
      musicGainDb: -18,
    });
  });

  it("confirms the suggested approach, creates drafts, and opens the works step in one action", async () => {
    const context = await loadStudioUiContext();
    const result = await runInContext(`(async () => {
      const calls = [];
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { items: [{ sourceAssetId: "asset-a" }] },
        matchAssessment: { id: "match-a", verdict: "ready" },
        opportunities: [{ id: "opportunity-a", goal: "讲清楚设计灵感", rationale: "现有素材可用" }],
      };
      studioApi = async (path) => {
        calls.push(path);
        if (path === "/api/studio/briefs") return { brief: {
          id: "brief-a", version: 1, goal: "讲清楚设计灵感", selectedAssetIds: ["asset-a"],
          confirmedFactIds: [], reviewRequirements: ["人工复核"]
        } };
        if (path === "/api/studio/drafts") return { drafts: [{ id: "draft-a", version: 1, sourceMap: [] }] };
        return { history: seededHistory() };
      };
      await confirmBriefAndCreateDrafts();
      return { calls, activeTab: studioActiveTab, view: studioAppView(studioState, studioActiveTab), drafts: studioState.drafts };
    })()`, context) as any;

    expect(result.calls).toEqual([
      "/api/studio/briefs", "/api/studio/history", "/api/studio/music/list",
      "/api/studio/drafts", "/api/studio/history",
    ]);
    expect(result.activeTab).toBe("works");
    expect(result.view).toEqual({ tab: "works", step: "drafts" });
    expect(result.drafts).toHaveLength(1);
  });

  it("shows a plain-language material list and keeps optional details collapsed", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`(() => {
      studioState = { selectedFactIds: [] };
      return renderMaterials([{
        id: "material-a", sourceAssetId: "asset-a", sourceStartMs: 0, sourceEndMs: 12500,
        mediaUrl: "/api/studio/materials/asset-a/media",
        displayName: "微信视频2026-10-02.mp4", uploadedAt: "2026-10-02T08:00:00.000Z",
        uploadSource: "手机／本机上传", libraryPath: "素材库 / 手机／本机上传 / 2026-10-02 / 微信视频2026-10-02.mp4",
        contentType: "single-product", technicalUsable: true,
        observations: ["戒指近景"], pendingFacts: ["材质待人工确认"],
        capability: { mode: "offline-fallback", label: "离线/模拟分析" }
      }]);
    })()`, context);

    expect(markup).toContain("微信视频2026-10-02.mp4");
    expect(markup).toContain("2026/10/02");
    expect(markup).toContain("手机／本机上传");
    expect(markup).toContain("路径：素材库 / 手机／本机上传 / 2026-10-02 / 微信视频2026-10-02.mp4");
    expect(markup).toContain('aria-label="按标签筛选素材"');
    expect(markup).toContain('data-material-filter="all" aria-pressed="true">全部 <span>1</span>');
    expect(markup).toContain('<span class="material-type-tag">产品</span><span>13秒</span>');
    expect(markup).toContain("材质待人工确认");
    expect(markup).toContain('<video class="material-preview" controls playsinline');
    expect(markup).toContain('aria-label="播放 微信视频2026-10-02.mp4"');
    expect(markup).toContain('src="/api/studio/materials/asset-a/media#t=0.1"');
    expect(markup).toContain("<summary>整理</summary>");
    expect(markup).not.toContain("点击播放，确认是不是你要用的视频");
    expect(markup).not.toContain("可正常播放");
    expect(markup).toContain("检查这段素材");
    expect(markup).toMatch(/<details class="material-details material-card-more">[\s\S]*data-start-analysis=/);
    expect(markup).not.toContain("共 1 条素材");
    expect(markup).not.toContain("本地模拟结果</small>");
    expect(markup).not.toContain("开始本地五层分析");
    expect(markup).not.toContain("查看技术与事实状态");
    expect(markup).not.toMatch(/data-studio-fact-id=[^>]+checked/);
  });

  it("filters a growing material library with simple counted tags", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = { selectedFactIds: [] };
      const materials = [
        { id: "product", sourceAssetId: "asset-product", sourceStartMs: 0, sourceEndMs: 5000,
          contentType: "single-product", technicalUsable: true, observations: ["戒指近景"] },
        { id: "process", sourceAssetId: "asset-process", sourceStartMs: 0, sourceEndMs: 7000,
          contentType: "jewelry-making-process", technicalUsable: true, observations: ["镶嵌过程"] },
        { id: "person", sourceAssetId: "asset-person", sourceStartMs: 0, sourceEndMs: 9000,
          contentType: "designer-viewpoint", technicalUsable: true, observations: ["设计师口播"] },
      ];
      studioMaterialFilter = "designer-viewpoint";
      return renderMaterials(materials);
    })()`, context) as string;

    expect(result).toContain('data-material-filter="all" aria-pressed="false">全部 <span>3</span>');
    expect(result).toContain('data-material-filter="designer-viewpoint" aria-pressed="true">人物／口播 <span>1</span>');
    expect(result).toContain("设计师口播");
    expect(result).not.toContain("戒指近景");
    expect(result).not.toContain("镶嵌过程");
  });

  it("lets a customer create multiple management tags and place several on one material", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = {
        selectedFactIds: [],
        materialTags: {
          version: 3,
          tags: [
            { id: "tag-october", label: "十月拍摄" },
            { id: "tag-edit", label: "待剪辑" },
          ],
          assignments: [{ materialCardId: "product", tagIds: ["tag-october", "tag-edit"] }],
        },
      };
      const materials = [
        { id: "product", sourceAssetId: "asset-product", sourceStartMs: 0, sourceEndMs: 5000,
          contentType: "single-product", technicalUsable: true, observations: ["戒指近景"] },
        { id: "person", sourceAssetId: "asset-person", sourceStartMs: 0, sourceEndMs: 9000,
          contentType: "designer-viewpoint", technicalUsable: true, observations: ["设计师口播"] },
      ];
      const all = renderMaterials(materials);
      studioMaterialFilter = customMaterialFilterValue("tag-edit");
      const filtered = renderMaterials(materials);
      return { all, filtered };
    })()`, context) as { all: string; filtered: string };

    expect(result.all).toContain('data-material-tag-form');
    expect(result.all).toContain('placeholder="例如：十月拍摄"');
    expect(result.all).toContain('待剪辑 <span>1</span>');
    expect(result.all).toContain('<span class="material-type-tag is-custom">十月拍摄</span>');
    expect(result.all).toContain('data-material-tag-id="tag-october"');
    expect(result.all).toContain('data-material-tag-id="tag-edit"');
    expect(result.filtered).toContain("戒指近景");
    expect(result.filtered).not.toContain("设计师口播");
  });

  it("explains empty and loading states in plain language and offers a next click", async () => {
    const context = await loadStudioUiContext();
    const result = runInContext(`(() => {
      studioState = { ...emptyStudioState("customer-a"), busy: true };
      const loading = renderMaterials([]);
      studioState.busy = false;
      const empty = renderMaterials([]);
      const works = renderDraftCards([]);
      return { loading, empty, works };
    })()`, context) as any;

    expect(result.loading).toContain("演示内容正在准备");
    expect(result.loading).toContain("不用上传真实素材");
    expect(result.empty).toContain("先添加你的第一段视频");
    expect(result.empty).toContain("在上方选择视频并上传");
    expect(result.works).toContain("这里会显示你的内容初稿");
    expect(result.works).toContain('data-studio-tab="create"');
  });

  it("labels gap capture advice as offline simulated analysis", async () => {
    const context = await loadStudioUiContext();
    const markup = runInContext(`renderOpportunities([], [{
      missingEvidence: "还缺一段成品镜头",
      impactWithoutCapture: "无法做成完整初稿",
      recommendedCapture: "补一段 10 秒成品近景"
    }], { label: "离线/模拟分析" })`, context);

    expect(markup).toContain("补一段 10 秒成品近景");
    expect(markup).toContain("离线/模拟分析");
  });

  it("clears every old-customer selection before opening the next customer", async () => {
    const context = await loadStudioUiContext();
    const snapshots = await runInContext(`(async () => {
      studioState = {
        customerId: "customer-a", session: { token: "old-token" },
        inventory: { id: "inventory-a" }, materials: [{ id: "material-a" }],
        opportunities: [{ id: "opportunity-a" }], brief: { id: "brief-a" },
        drafts: [{ id: "draft-a" }], feedback: { id: "feedback-a" }, busy: false
      };
      const seen = [];
      studioApi = async (path, options = {}) => {
        seen.push({ path, state: JSON.parse(JSON.stringify(studioState)) });
        if (path === "/api/studio/session") return {
          session: { token: "new-token", customerId: "customer-b", sessionId: "session-b" },
          capability: { mode: "offline-fallback", label: "离线/模拟分析" }
        };
        return { inventory: { id: "inventory-b", items: [] }, materials: [] };
      };
      await switchStudioCustomer("customer-b");
      return { seen, finalState: studioState };
    })()`, context) as any;

    expect(snapshots.seen[0]).toMatchObject({
      path: "/api/studio/session",
      state: {
        customerId: "customer-b",
        session: null,
        inventory: null,
        materials: [],
        opportunities: [],
        brief: null,
        drafts: [],
        feedback: null,
      },
    });
    expect(snapshots.finalState).toMatchObject({
      customerId: "customer-b",
      session: { token: "new-token", customerId: "customer-b" },
      inventory: { id: "inventory-b" },
      materials: [],
      opportunities: [],
      brief: null,
      drafts: [],
      feedback: null,
    });
  });

  it("keeps delayed session and inventory responses from reclaiming a newer customer", async () => {
    const context = await loadStudioUiContext();
    const finalState = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/session") {
          const customerId = options.body.customerId;
          await delay(customerId === "customer-a" ? 30 : 0);
          return { session: { token: "token-" + customerId, customerId, sessionId: "session-" + customerId } };
        }
        const customerId = studioState.session.customerId;
        await delay(customerId === "customer-a" ? 15 : 0);
        return { inventory: { id: "inventory-" + customerId, items: [] }, materials: [] };
      };
      const older = switchStudioCustomer("customer-a");
      await delay(1);
      const newer = switchStudioCustomer("customer-b");
      await Promise.all([older, newer]);
      return studioState;
    })()`, context) as any;

    expect(finalState).toMatchObject({
      customerId: "customer-b",
      session: { token: "token-customer-b", customerId: "customer-b" },
      inventory: { id: "inventory-customer-b" },
      opportunities: [],
      brief: null,
      drafts: [],
    });
  });

  it("serializes customer session requests so the newest HttpOnly cookie lands last", async () => {
    const context = await loadStudioUiContext();
    const events = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const order = [];
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/session") {
          const customerId = options.body.customerId;
          order.push("start-" + customerId);
          await delay(customerId === "customer-a" ? 25 : 0);
          order.push("finish-" + customerId);
          return { session: { token: "token-" + customerId, customerId, sessionId: "session-" + customerId } };
        }
        return { inventory: { id: "inventory-b", items: [] }, materials: [] };
      };
      const older = switchStudioCustomer("customer-a");
      await delay(1);
      const newer = switchStudioCustomer("customer-b");
      await Promise.all([older, newer]);
      return order;
    })()`, context);

    expect(events).toEqual([
      "start-customer-a",
      "finish-customer-a",
      "start-customer-b",
      "finish-customer-b",
    ]);
  });

  it("aborts an old non-session workflow when the customer changes", async () => {
    const context = await loadStudioUiContext();
    const oldSignalWasAborted = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      studioState = {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a" },
        inventory: { id: "inventory-a", items: [] }
      };
      let aborted = false;
      studioApi = async (path, options = {}) => {
        if (path === "/api/studio/opportunities") {
          options.signal.addEventListener("abort", () => { aborted = true; }, { once: true });
          await delay(25);
          return { opportunities: [], gaps: [], gapList: null };
        }
        if (path === "/api/studio/session") return {
          session: { token: "token-b", customerId: "customer-b", sessionId: "session-b" }
        };
        return { inventory: { id: "inventory-b", items: [] }, materials: [] };
      };
      const oldWork = findStudioOpportunity();
      await delay(1);
      await switchStudioCustomer("customer-b");
      await oldWork;
      return aborted;
    })()`, context);

    expect(oldSignalWasAborted).toBe(true);
  });

  it("ignores delayed opportunity, brief, and draft responses after a customer switch", async () => {
    const context = await loadStudioUiContext();
    const states = await runInContext(`(async () => {
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      async function runStaleCase(kind) {
        studioState = {
          customerId: "customer-a", session: { token: "token-a", customerId: "customer-a" },
          inventory: { items: [{ sourceAssetId: "asset-a" }] },
          materials: [{ id: "material-a", observations: ["合成素材"], sourceStartMs: 0,
            sourceEndMs: 10500, pendingFacts: ["fact-a"],
            capability: { label: "离线/模拟分析" } }], selectedFactIds: [],
          opportunities: [{ id: "opportunity-a", goal: "讲戒指", rationale: "素材可用",
            capability: { label: "离线/模拟分析" } }],
          brief: { id: "brief-a", version: 1, goal: "讲戒指", selectedAssetIds: ["asset-a"],
            confirmedFactIds: [], reviewRequirements: ["采用前人工复核"],
            capability: { label: "离线/模拟分析" } },
          drafts: [{ id: "draft-a", mediaUrl: "/api/studio/drafts/draft-a/media",
            durationSeconds: 31.5, sourceMap: [{ sourceAssetId: "asset-a", sourceDigest: "digest-a",
              reason: "工序证据" }], capability: { label: "离线/模拟分析" } }],
          feedback: null, busy: false
        };
        studioApi = async (path, options = {}) => {
          if (path === "/api/studio/session") return {
            session: { token: "token-b", customerId: "customer-b", sessionId: "session-b" }
          };
          if (path === "/api/studio/inventory") return {
            inventory: { id: "inventory-b", items: [] }, materials: []
          };
          await delay(25);
          if (path === "/api/studio/opportunities") return {
            opportunities: [{ id: "old-opportunity", goal: "旧故事", rationale: "旧判断",
              capability: { label: "离线/模拟分析" } }], gaps: [], gapList: null
          };
          if (path === "/api/studio/briefs") return {
            brief: { id: "old-brief", version: 1, goal: "旧任务书", selectedAssetIds: ["asset-a"],
              confirmedFactIds: [], reviewRequirements: ["人工复核"],
              capability: { label: "离线/模拟分析" } }
          };
          if (path === "/api/studio/feedback") return {
            feedback: { id: "old-feedback", action: "adopt", publishClaimed: false }
          };
          return { drafts: [{ id: "old-draft", mediaUrl: "/api/studio/drafts/old-draft/media",
            durationSeconds: 31.5, sourceMap: [{ sourceAssetId: "asset-a", sourceDigest: "digest-a",
              reason: "旧镜头" }], capability: { label: "离线/模拟分析" } }] };
        };
        const oldWork = kind === "opportunity"
          ? findStudioOpportunity()
          : kind === "brief"
            ? confirmStudioBrief()
            : kind === "drafts"
              ? createStudioDrafts()
              : submitStudioFeedback("draft-a", "adopt");
        await delay(1);
        const customerSwitch = switchStudioCustomer("customer-b");
        await Promise.all([oldWork, customerSwitch]);
        return JSON.parse(JSON.stringify(studioState));
      }
      return [
        await runStaleCase("opportunity"),
        await runStaleCase("brief"),
        await runStaleCase("drafts"),
        await runStaleCase("feedback")
      ];
    })()`, context) as any[];

    for (const state of states) {
      expect(state).toMatchObject({
        customerId: "customer-b",
        session: { token: "token-b", customerId: "customer-b" },
        inventory: { id: "inventory-b" },
        opportunities: [],
        brief: null,
        drafts: [],
        feedback: null,
      });
    }
  });

  it("contains no browser request that can publish", async () => {
    const javascript = await readFile(join(publicRoot, "studio.js"), "utf8");

    expect(javascript).not.toMatch(/["'`]\/api\/studio\/publish/);
    expect(javascript).not.toContain("data-publish");
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
    ["#assess-demand", fakeElement()],
    ["#confirm-brief", fakeElement()],
    ["#make-drafts", fakeElement()],
    ["#confirm-revision-proposal", fakeElement()],
    ["#make-revision-drafts", fakeElement()],
  ]);
  const document = {
    addEventListener() {},
    querySelector: (selector: string) => elements.get(selector) ?? fakeElement(),
  };
  const context = createContext({
    document,
    window: { prompt: () => "请把观点放前面" },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    crypto: { randomUUID: () => "ui-operation-1" },
    AbortController,
    console,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  const source = await readFile(join(publicRoot, "studio.js"), "utf8");
  runInContext(source, context);
  runInContext(`
    function seededHistory() {
      return { demands: [], assessments: [], opportunities: [], briefs: [], drafts: [],
        customerFeedback: [], revisionProposals: [] };
    }
    function seededV1StudioState() {
      return {
        ...emptyStudioState("customer-a"),
        session: { token: "token-a", customerId: "customer-a", sessionId: "session-a" },
        inventory: { id: "inventory-a", items: [{ sourceAssetId: "asset-a" }] },
        demand: { id: "demand-a", version: 1, status: "current" },
        matchAssessment: { id: "match-a", verdict: "ready" },
        opportunities: [{ id: "opportunity-a" }],
        brief: { id: "brief-a", version: 1 },
        drafts: [{ id: "draft-a", version: 1 }],
      };
    }
  `, context);
  return context;
}

function seededHistoryExpected() {
  return {
    demands: [], assessments: [], opportunities: [], briefs: [], drafts: [],
    customerFeedback: [], revisionProposals: [],
  };
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
