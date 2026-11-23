import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { createContext, runInContext } from "node:vm";

import { describe, expect, it } from "vitest";

describe("mobile creative production UI", () => {
  it("starts with one clear intent action", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({ stage: "draft_intent", title: "新建创作" })`, context);

    expect(workspace.innerHTML).toContain("今天想拍什么？");
    expect(workspace.innerHTML).toContain("说出或写下拍摄想法");
    expect((workspace.innerHTML.match(/creative-primary/g) ?? [])).toHaveLength(1);
  });

  it("continues an incomplete intent in the same session instead of showing a new-session form", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_draft", stage: "draft_intent",
      title: "返工故事", questions: ["platforms", "audience"] })`, context);
    expect(workspace.innerHTML).toContain("继续补充这次创作");
    expect(workspace.innerHTML).toContain("data-intent-answer");
    expect(workspace.innerHTML).not.toContain('class="intent-form"');
  });

  it("offers create-new and explicit session switching without mixing identities", async () => {
    const { context } = await loadCreativeUiContext();
    const markup = runInContext(`creativeSessionSwitcherMarkup([
      { id: "session_a", title: "A", stage: "paused" },
      { id: "session_b", title: "B", stage: "completed" }
    ], "session_b")`, context);
    expect(markup).toContain('data-creative-session="session_a"');
    expect(markup).toContain('data-creative-session="session_b"');
    expect(markup).toContain('value="session_b" selected');
    expect(markup).toContain("data-new-creative-session");
    const switched = await runInContext(`(async () => {
      let reconciledId = null;
      reconcileCreativeSession = async (id) => { reconciledId = id; return id; };
      await switchCreativeSessionFromUi({ currentTarget: { value: "session_a" } });
      return { selectedCreativeSessionId, reconciledId };
    })()`, context);
    expect(switched).toEqual({ selectedCreativeSessionId: "session_a", reconciledId: "session_a" });
  });

  it("does not reveal suggestion cards before accept-view", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01",
      stage: "awaiting_material_consent",
      title: "绿松石返工故事",
      materialSuggestion: { id: "suggestions_01", suggestions: [{ segmentId: "hidden_01" }] }
    })`, context);

    expect(workspace.innerHTML).toContain("看看素材");
    expect(workspace.innerHTML).not.toContain("material-card");
    expect(workspace.innerHTML).not.toContain("hidden_01");
  });

  it("shows complete evidence and separate preview and selection controls after consent", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01",
      stage: "reviewing_materials",
      title: "绿松石返工故事",
      suggestionSet: { id: "suggestions_01", suggestions: [{
        segmentId: "segment_01",
        previewUrl: "/api/creative-sessions/session_01/material-suggestions/segment_01/preview",
        previewKind: "image",
        capturedAt: "2026-09-01T08:00:00.000Z",
        productId: "绿松石戒指",
        processStage: "第三次返工",
        reason: "同一件作品的上一道工序",
        source: "本地素材库",
        permissionState: "allowed",
        technicallyAvailable: true
      }] }
    })`, context);

    expect(workspace.innerHTML).toContain("material-card");
    expect(workspace.innerHTML).toContain("素材预览");
    expect(workspace.innerHTML).toContain("<img");
    expect(workspace.innerHTML).toContain('data-preview-url="/api/creative-sessions/session_01/material-suggestions/segment_01/preview"');
    expect(workspace.innerHTML).not.toContain('src="/api/creative-sessions/');
    expect(workspace.innerHTML).toContain("素材预览暂时无法加载");
    expect(workspace.innerHTML).not.toContain("thumbnailPath");
    expect(workspace.innerHTML).toContain("2026年9月1日");
    expect(workspace.innerHTML).toContain("绿松石戒指");
    expect(workspace.innerHTML).toContain("第三次返工");
    expect(workspace.innerHTML).toContain("同一件作品的上一道工序");
    expect(workspace.innerHTML).toContain("本地素材库");
    expect(workspace.innerHTML).toContain("权限已确认");
    expect(workspace.innerHTML).toContain("看看素材");
    expect(workspace.innerHTML).toContain("加入本次创作");
  });

  it("labels all four script sources and withholds dispatch before confirmation", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01",
      stage: "awaiting_script_confirmation",
      title: "绿松石返工故事",
      currentScriptVersion: 1,
      script: { scriptVersion: 1, title: "返工三次，为什么还要继续", opening: "这枚戒指返工了三次。", sections: [
        { id: "fact", source: "customer-input", expression: "客户确认返工三次", referencedSegmentIds: [] },
        { id: "history", source: "historical-material", expression: "展示上次返工", referencedSegmentIds: ["segment_01"] },
        { id: "agent", source: "agent-suggestion", expression: "从结果倒叙", referencedSegmentIds: [] },
        { id: "shoot", source: "shoot-suggestion", expression: "可补拍一个近景", referencedSegmentIds: [] }
      ] }
    })`, context);

    for (const label of ["客户事实", "历史素材", "Agent 建议", "可选拍摄建议"]) {
      expect(workspace.innerHTML).toContain(label);
    }
    for (const decision of ["继续修改", "暂存", "确认脚本，准备拍摄"]) {
      expect(workspace.innerHTML).toContain(decision);
    }
    expect(workspace.innerHTML).not.toContain("data-dispatch");
  });

  it("shows the V1 to V2 script revision, designer prompt, explanation, and changed content", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_diff", stage: "awaiting_script_confirmation",
      currentScriptVersion: 2, script: { scriptVersion: 2, title: "V2", opening: "新开头", designerPrompt: "开头更直接",
        changeExplanation: "已缩短铺垫", sections: [{ id: "fact", source: "customer-input", expression: "新内容", referencedSegmentIds: [] }] },
      scriptHistory: [
        { scriptVersion: 1, title: "V1", opening: "旧开头", sections: [{ id: "fact", source: "customer-input", expression: "旧内容", referencedSegmentIds: [] }] },
        { scriptVersion: 2, title: "V2", opening: "新开头", designerPrompt: "开头更直接", changeExplanation: "已缩短铺垫", sections: [{ id: "fact", source: "customer-input", expression: "新内容", referencedSegmentIds: [] }] }
      ] })`, context);
    for (const copy of ["V1", "V2", "开头更直接", "已缩短铺垫", "旧开头", "新开头", "旧内容", "新内容"]) {
      expect(workspace.innerHTML).toContain(copy);
    }
  });

  it("shows all three simulated devices on the dispatch confirmation page", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01", stage: "ready_to_dispatch", title: "绿松石返工故事",
      currentScriptVersion: 2, currentShotPlanVersion: 1, privacyMode: "creator",
      shotPlan: { steps: [{ id: "step_01", purpose: "拍返工细节" }] }, receipts: [],
      devices: [
        { deviceId: "watch", deviceRole: "watch-director", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 82 } },
        { deviceId: "stone-a", deviceRole: "main-stone-a", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 76 } },
        { deviceId: "stone-b", deviceRole: "main-stone-b", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 71 } }
      ]
    })`, context);

    expect(workspace.innerHTML).toContain("手表（模拟）");
    expect(workspace.innerHTML).toContain("主石 A（模拟）");
    expect(workspace.innerHTML).toContain("主石 B（模拟）");
    expect(workspace.innerHTML).toContain("data-dispatch");
    expect(workspace.innerHTML).not.toContain("已接收拍摄任务");
    expect((workspace.innerHTML.match(/模拟电量/g) ?? [])).toHaveLength(3);
  });

  it("provides the complete field action set", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01", stage: "shooting", title: "绿松石返工故事",
      currentBriefVersion: 1, currentScriptVersion: 1, currentShotPlanVersion: 1,
      shotPlan: { steps: [{ id: "step_01", order: 1, purpose: "拍返工细节", expressionSuggestion: "讲清为什么重做", visualDirection: "拍手工近景", durationSeconds: 20, mustCapture: ["戒面"], prohibitedCapture: ["客户脸部"] }] },
      receipts: [{ status: "received" }, { status: "received" }, { status: "received" }],
      devices: [
        { deviceId: "watch", deviceRole: "watch-director", status: "received", battery: { label: "模拟电量", percent: 82 } },
        { deviceId: "stone-a", deviceRole: "main-stone-a", status: "received", battery: { label: "模拟电量", percent: 76 } },
        { deviceId: "stone-b", deviceRole: "main-stone-b", status: "received", battery: { label: "模拟电量", percent: 71 } }
      ]
    })`, context);

    for (const action of ["完成", "重拍", "跳过", "语音改写", "备注", "隐私暂停"]) {
      expect(workspace.innerHTML).toContain(action);
    }
    expect(workspace.innerHTML).toContain("已接收拍摄任务");
    expect(workspace.innerHTML).toContain("结束并开始内容处理");
    expect(workspace.innerHTML).not.toContain("导入手机返回素材（A-0 模拟）");
  });

  it("lets the customer explicitly resume after confirming the environment is safe", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_paused", stage: "paused", privacyMode: "paused" })`, context);
    expect(workspace.innerHTML).toContain("我已确认环境安全，继续拍摄");
    expect(workspace.innerHTML).toContain("data-privacy-resume");
  });

  it("shows derived take two and processing without claiming editorial ownership", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_processing", stage: "editorial_review",
      title: "返工故事", currentScriptVersion: 1, currentShotPlanVersion: 1,
      stepResolutions: [{ stepId: "step_1", state: "completed-returned", acceptedTake: 2 }],
      processingTask: { id: "task_1", status: "processing", phase: "render", publishedClaimed: false },
      stories: [], revisions: [], artifacts: [] })`, context);
    expect(workspace.innerHTML).toContain("第 2 次完成");
    expect(workspace.innerHTML).toContain("正在生成四个视频版本");
    expect(workspace.innerHTML).not.toContain("内容总编已接手");
    expect(workspace.innerHTML).not.toContain("去审片");
  });

  it("shows exactly four persisted review videos only when the task is reviewable", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_reviewable", stage: "editorial_review",
      title: "返工故事", processingTask: { id: "task_1", status: "reviewable", phase: "complete", publishedClaimed: false },
      stories: [{ id: "story_1", type: "product-journey" }],
      revisions: [{ id: "revision_1", storyId: "story_1", revision: 1 }],
      artifacts: [
        { id: "artifact_master", revisionId: "revision_1", kind: "editorial-master", mediaUrl: "/media/master" },
        { id: "artifact_red", revisionId: "revision_1", kind: "xiaohongshu", mediaUrl: "/media/red" },
        { id: "artifact_douyin", revisionId: "revision_1", kind: "douyin", mediaUrl: "/media/douyin" },
        { id: "artifact_channels", revisionId: "revision_1", kind: "wechat-channels", mediaUrl: "/media/channels" }
      ] })`, context);
    for (const label of ["原视频", "小红书", "抖音", "视频号", "去审片"]) {
      expect(workspace.innerHTML).toContain(label);
    }
    expect(workspace.innerHTML.match(/<video data-artifact-id=/g)).toHaveLength(4);
    expect(workspace.innerHTML).not.toContain("发布");
  });

  it("renders no-assets, retryable, and manual-review handoff recovery as one-action cards", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const cases = [
      ["no-assets", "本次已结束，没有强制补拍"],
      ["failed-retryable", "使用原操作编号安全重试"],
      ["failed-manual-review", "人工核对后重置"],
    ];
    for (const [status, copy] of cases) {
      runInContext(`renderCreativeSession({ id: "session_${status}", stage: "editorial_review",
        title: "返工故事", processingTask: { id: "task_${status}", status: "${status}", phase: "complete", publishedClaimed: false },
        stories: [], revisions: [], artifacts: [] })`, context);
      expect(workspace.innerHTML).toContain(copy);
      expect(workspace.innerHTML).not.toContain("去审片");
      if (status === "failed-manual-review") {
        expect(workspace.innerHTML).toContain("data-reset-manual-editorial");
        expect(workspace.innerHTML).not.toContain("data-retry-editorial");
      }
    }
  });

  it("starts a fresh editorial operation only after explicit human confirmation of a manual failure", async () => {
    const { context } = await loadCreativeUiContext();
    const calls: Array<{ sessionId: string; path: string; body: unknown; operationKey?: string }> = [];
    Object.assign(context, { testButton: { ...fakeElement(), closest: () => fakeElement() }, testCalls: calls });
    const result = await runInContext(`(async () => {
      reviewSession = { designerId: "designer_01" };
      currentCreativeView = { id: "session_manual", processingTask: { id: "task_manual", status: "failed-manual-review" } };
      makeOperationId = () => "process_restart_key_01";
      mutateCreativeSession = async (input) => {
        testCalls.push({ sessionId: input.sessionId, path: input.path, body: input.body, operationKey: input.operationKey });
        if (input.path.endsWith("/editorial-manual-reset")) return { authorization: { id: "manual_auth_01" } };
        return input;
      };
      return resetManualEditorialFromUi({ currentTarget: testButton });
    })()`, context);
    expect(result).toBeTruthy();
    expect(calls).toEqual([
      { sessionId: "session_manual", path: "/api/creative-sessions/session_manual/editorial-manual-reset",
        body: { failedTaskId: "task_manual", confirmedBy: "designer_01",
          confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
          confirmationText: "人工核对完成，确认重新开始剪辑",
          confirmedAt: expect.any(String), processOperationKey: "process_restart_key_01" }, operationKey: undefined },
      { sessionId: "session_manual", path: "/api/creative-sessions/session_manual/editorial-process",
        body: { manualRestartAuthorizationId: "manual_auth_01" }, operationKey: "process_restart_key_01" },
    ]);
  });

  it("keeps post-shoot inventory out of the cut until the customer explicitly reviews it", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_edit", stage: "editorial_review",
      postShootMaterialSuggestion: { id: "post_set", count: 2 },
      postShootMaterialDecision: null })`, context);
    expect(workspace.innerHTML).toContain("是否查看拍后建议的库存素材");
    expect(workspace.innerHTML).toContain("data-post-material-accept");
    expect(workspace.innerHTML).toContain("data-post-material-decline");
    expect(workspace.innerHTML).not.toContain("已加入成片");
  });

  it("keeps the post-shoot decision reachable and starts editing only after an exact choice", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_edit", stage: "editorial_review",
      processingTask: { id: "capture_task", status: "ready-for-editorial", phase: "complete" },
      postShootMaterialSuggestion: null, postShootMaterialDecision: null })`, context);
    expect(workspace.innerHTML).toContain("先确认是否补用库存");
    expect(workspace.innerHTML).toContain("data-post-material-propose");
    expect(workspace.innerHTML).not.toContain("data-start-editorial");

    runInContext(`renderCreativeSession({ id: "session_edit", stage: "editorial_review",
      processingTask: { id: "capture_task", status: "ready-for-editorial", phase: "complete" },
      postShootMaterialSuggestion: { id: "post_set", count: 2 },
      postShootMaterialDecision: { action: "decline-view", selectedSegmentIds: [] } })`, context);
    expect(workspace.innerHTML).toContain("本次不使用历史素材");
    expect(workspace.innerHTML).toContain("确认后开始剪辑");
    expect(workspace.innerHTML).toContain("data-start-editorial");
    expect(workspace.innerHTML).not.toContain("去审片");

    runInContext(`renderCreativeSession({ id: "session_edit", stage: "editorial_review",
      processingTask: { id: "capture_task", status: "ready-for-editorial", phase: "complete" },
      postShootMaterialSuggestion: { id: "post_set", count: 2 },
      postShootMaterialDecision: { action: "confirm-selection", selectedSegmentIds: ["history_2"] } })`, context);
    expect(workspace.innerHTML).toContain("已精确选择 1 段历史素材");
    expect(workspace.innerHTML).toContain("data-start-editorial");
  });

  it("clears post-shoot selection access when the latest decision declines a previously visible set", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const result = runInContext(`(() => {
      const view = { id: "session_edit", stage: "editorial_review",
        postShootMaterialSuggestion: { id: "post_set", count: 1 },
        postShootMaterialDecision: { action: "decline-view", selectedSegmentIds: [] },
        postShootSuggestionSet: { id: "post_set", suggestions: [{ segmentId: "private_history" }] } };
      renderCreativeSession(view);
      return materialSelectionView(view);
    })()`, context);
    expect(result).toBeNull();
    expect(workspace.innerHTML).not.toContain("private_history");
    expect(workspace.innerHTML).not.toContain("data-post-material-confirm");
  });

  it("removes current videos and approval controls after a persisted output-integrity failure", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    runInContext(`renderCreativeSession({ id: "session_edit", stage: "editorial_review",
      processingTask: { id: "task_1", status: "failed-manual-review", phase: "complete",
        errorCode: "creative-editorial-current-output-invalid", publishedClaimed: false },
      revisions: [], artifacts: [], stories: [] })`, context);
    expect(workspace.innerHTML).toContain("data-reset-manual-editorial");
    expect(workspace.innerHTML).not.toContain("<video");
    expect(workspace.innerHTML).not.toContain("去审片");
    expect(workspace.innerHTML).not.toContain("data-approve");
  });

  it("does not offer an old completed take while the server says retake-pending", async () => {
    const { context } = await loadCreativeUiContext();
    const result = runInContext(`nextCreativeReturn({
      shotPlan: { steps: [{ id: "step_1" }] },
      stepResolutions: [{ stepId: "step_1", state: "retake-pending", acceptedTake: null }],
      events: [
        { stepId: "step_1", action: "completed", take: 1 },
        { stepId: "step_1", action: "retake", take: 1 }
      ]
    })`, context);
    expect(result).toBeNull();
  });

  it("uses the server returnable take instead of inferring it from old event history", async () => {
    const { context } = await loadCreativeUiContext();
    const result = runInContext(`nextCreativeReturn({
      shotPlan: { steps: [{ id: "step_1" }] },
      stepResolutions: [{ stepId: "step_1", state: "pending", acceptedTake: null, returnableTake: 2 }],
      events: [{ stepId: "step_1", action: "completed", take: 1 }]
    })`, context);
    expect(result).toEqual({ step: { id: "step_1" }, take: 2 });
  });

  it("binds a failed manual-derived editorial retry to its exact durable request", async () => {
    const operation = pendingOperationRecord({
      path: "/api/creative-sessions/session_retry/editorial-process",
      operationKey: "editorial-process-key-0001",
      sessionId: "session_retry",
      body: { manualRestartAuthorizationId: "manual_auth_retry_0001" },
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "安全重试" };
    const feedback = { textContent: "" };
    button.closest = () => ({ querySelector: () => feedback });
    const calls: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });
    const result = await runInContext(`(async () => {
      currentCreativeView = { id: "session_retry", processingTask: {
        status: "failed-retryable", operationKey: "editorial-process-key-0001"
      } };
      api = async (path, options = {}) => {
        testCalls.push({ path, options });
        return { processingTask: { id: "task_retry", status: "reviewable" } };
      };
      reconcileCreativeSession = async () => currentCreativeView;
      completeReconciledCreativeOperation = async () => undefined;
      return retryEditorialFromUi({ currentTarget: testButton });
    })()`, context);
    expect(result).toMatchObject({ processingTask: { status: "reviewable" } });
    expect(calls).toEqual([{
      path: "/api/creative-sessions/session_retry/editorial-process",
      options: {
        method: "POST",
        body: JSON.stringify({ manualRestartAuthorizationId: "manual_auth_retry_0001" }),
        headers: {
          "x-client-instance": "client_instance_ui_0001",
          "x-operation-key": "editorial-process-key-0001",
        },
      },
    }]);
  });

  it("keeps ordinary failed editorial retry compatible with its saved empty body", async () => {
    const operation = pendingOperationRecord({
      path: "/api/creative-sessions/session_retry/editorial-process",
      operationKey: "ordinary-editorial-key-0001",
      sessionId: "session_retry",
      body: {},
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), closest: () => fakeElement() };
    const retried: unknown[] = [];
    Object.assign(context, { testButton: button, testRetried: retried });
    await runInContext(`(async () => {
      currentCreativeView = { id: "session_retry", processingTask: {
        status: "failed-retryable", operationKey: "ordinary-editorial-key-0001"
      } };
      retryPendingCreativeOperation = async (operation) => { testRetried.push(operation); return operation; };
      return retryEditorialFromUi({ currentTarget: testButton });
    })()`, context);
    expect(retried).toEqual([expect.objectContaining({
      operationKey: "ordinary-editorial-key-0001",
      request: {
        method: "POST",
        path: "/api/creative-sessions/session_retry/editorial-process",
        body: {},
      },
    })]);
  });

  it("shows manual recovery and sends nothing when failed retry identity is missing or copied", async () => {
    const copied = pendingOperationRecord({
      path: "/api/creative-sessions/session_retry/editorial-process",
      operationKey: "editorial-process-key-0001",
      clientInstanceId: "client_instance_copied_0001",
      sessionId: "session_retry",
      body: { manualRestartAuthorizationId: "manual_auth_retry_0001" },
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([copied]));
    const { context } = await loadCreativeUiContext(storage);
    const feedback = { textContent: "" };
    const button = { ...fakeElement(), closest: () => ({ querySelector: () => feedback }) };
    const calls: string[] = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });
    const result = await runInContext(`(async () => {
      currentCreativeView = { id: "session_retry", processingTask: {
        status: "failed-retryable", operationKey: "editorial-process-key-0001"
      } };
      api = async (path) => { testCalls.push(path); return {}; };
      mutateCreativeSession = async () => { testCalls.push("mutate"); return {}; };
      retryPendingCreativeOperation = async () => { testCalls.push("retry"); return {}; };
      return retryEditorialFromUi({ currentTarget: testButton });
    })()`, context);
    expect(result).toBeNull();
    expect(calls).toEqual([]);
    expect(feedback.textContent).toContain("人工恢复");
  });

  it("uses session-scoped creative revision and approval URLs", async () => {
    const { context } = await loadCreativeUiContext();
    const inputs: Array<{ path: string; body: unknown }> = [];
    const form = {
      ...fakeElement(), dataset: { creativeReviseStory: "story_1" },
      elements: { namedItem: () => ({ value: "剪短开头" }) },
      querySelector: () => ({ ...fakeElement(), disabled: false }),
    };
    const approve = { ...fakeElement(), disabled: false,
      dataset: { approveCreativeArtifact: "artifact_1" } };
    Object.assign(context, { testInputs: inputs, testForm: form, testApprove: approve });
    await runInContext(`(async () => {
      currentCreativeView = { id: "session_1" };
      mutateCreativeSession = async (input) => { testInputs.push({ path: input.path, body: input.body }); return input; };
      await reviseCreativeStoryFromUi({ preventDefault() {}, currentTarget: testForm });
      await approveCreativeArtifactFromUi({ currentTarget: testApprove });
    })()`, context);
    expect(inputs).toEqual([
      { path: "/api/creative-sessions/session_1/stories/story_1/revisions", body: { prompt: "剪短开头" } },
      { path: "/api/creative-sessions/session_1/artifacts/artifact_1/approve", body: {} },
    ]);
  });

  it("uses session-scoped creative export and phone-delivery URLs", async () => {
    const { context } = await loadCreativeUiContext();
    const inputs: Array<{ path: string; body: unknown }> = [];
    const exportButton = { ...fakeElement(), disabled: false,
      dataset: { exportCreativeApproval: "approval_1" } };
    const deliveryButton = { ...fakeElement(), disabled: false,
      dataset: { deliverCreativePackage: "package_1" } };
    Object.assign(context, { testInputs: inputs, testExport: exportButton, testDelivery: deliveryButton });
    await runInContext(`(async () => {
      currentCreativeView = { id: "session_1" };
      mutateCreativeSession = async (input) => { testInputs.push({ path: input.path, body: input.body }); return input; };
      await exportCreativeApprovalFromUi({ currentTarget: testExport });
      await deliverCreativePackageFromUi({ currentTarget: testDelivery });
    })()`, context);
    expect(inputs).toEqual([
      { path: "/api/creative-sessions/session_1/approvals/approval_1/export", body: {} },
      { path: "/api/creative-sessions/session_1/export-packages/package_1/deliver", body: {} },
    ]);
  });

  it("uses per-device server status even when several receipts belong to one device", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext(`renderCreativeSession({
      id: "session_01", stage: "ready_to_dispatch", currentScriptVersion: 1,
      currentShotPlanVersion: 1, privacyMode: "creator", shotPlan: { steps: [{ id: "step_01" }] },
      receipts: [
        { deviceRole: "watch-director", status: "received" },
        { deviceRole: "watch-director", status: "received" },
        { deviceRole: "watch-director", status: "received" }
      ],
      devices: [
        { deviceRole: "watch-director", status: "received", battery: { label: "模拟电量", percent: 82 } },
        { deviceRole: "main-stone-a", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 76 } },
        { deviceRole: "main-stone-b", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 71 } }
      ]
    })`, context);

    expect((workspace.innerHTML.match(/已接收拍摄任务/g) ?? [])).toHaveLength(1);
    expect((workspace.innerHTML.match(/等待确认下发/g) ?? [])).toHaveLength(2);
  });

  it("puts creative mutation failures beside the trigger and restores its button", async () => {
    const { context } = await loadCreativeUiContext();
    const feedback = { textContent: "" };
    const button = { disabled: false, textContent: "确认修改" };
    Object.assign(context, { testButton: button, testFeedback: feedback });

    await runInContext(`api = async () => { throw new Error("创作服务暂时不可用") };
      mutateCreativeSession({
        button: testButton, feedback: testFeedback, busyText: "正在修改…",
        path: "/api/creative-sessions/session_01/script-revisions", body: { prompt: "缩短开头" }
      })`, context);

    expect(feedback.textContent).toContain("创作服务暂时不可用");
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe("确认修改");
  });

  it("reloads the server view after a successful mutation", async () => {
    const { context } = await loadCreativeUiContext();
    const calls: Array<{ call: string; headers?: Record<string, string> }> = [];
    const button = { disabled: false, textContent: "生成脚本" };
    const feedback = { textContent: "" };
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });

    await runInContext(`api = async (path, options) => {
        testCalls.push({ call: (options?.method || "GET") + " " + path, headers: options?.headers });
        if (options?.method === "POST") return { script: { scriptVersion: 1, sections: [] } };
        return { session: { id: "session_01", stage: "awaiting_script_confirmation", currentScriptVersion: 1 } };
      };
      mutateCreativeSession({
        sessionId: "session_01", button: testButton, feedback: testFeedback,
        busyText: "正在生成…", path: "/api/creative-sessions/session_01/scripts", body: {},
        remember(response, draft) { draft.script = response.script }
      })`, context);

    expect(calls.map((item) => item.call)).toEqual([
      "POST /api/creative-sessions/session_01/scripts",
      "GET /api/creative-sessions/session_01",
      "DELETE /api/creative-operation-receipts/operation_01",
    ]);
  });

  it("restores a material stage from one server detail response without client cache", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const calls: string[] = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`api = async (path) => {
        testCalls.push(path);
        return { session: {
          id: "session_reload", stage: "reviewing_materials", title: "刷新后的创作",
          suggestionSet: { id: "set_reload", suggestions: [{
            segmentId: "segment_reload", capturedAt: "2026-09-01T08:00:00.000Z",
            productId: "绿松石戒指", processStage: "打磨", reason: "同一件作品",
            source: "本地素材库", permissionState: "allowed", technicallyAvailable: true,
            previewUrl: "/api/creative-sessions/session_reload/material-suggestions/segment_reload/preview",
            previewKind: "video"
          }] }
        } };
      };
      reloadCreativeSession("session_reload")`, context);

    expect(calls).toEqual(["/api/creative-sessions/session_reload"]);
    expect(workspace.innerHTML).toContain("segment_reload");
    expect(workspace.innerHTML).toContain("<video");
  });

  it("does not let an older tab response overwrite the current tab", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    let resolveCreative!: (value: unknown) => void;
    const creativeResponse = new Promise((resolve) => { resolveCreative = resolve; });
    Object.assign(context, { testCreativeResponse: creativeResponse });

    const pending = runInContext(`api = async (path) => {
        if (path === "/api/creative-sessions") return testCreativeResponse;
        if (path.startsWith("/api/batches/")) return { assets: [], stories: [], failures: [] };
        if (path === "/api/review") return { stories: [] };
        if (path === "/api/approved") return { approvals: [] };
        return { segments: [] };
      };
      activeView = "creative";
      const oldLoad = loadView();
      activeView = "profile";
      const currentLoad = loadView();
      Promise.resolve(currentLoad).then(() => oldLoad)`, context);
    await Promise.resolve();
    await Promise.resolve();
    expect(workspace.innerHTML).toContain("内容生产总编");
    resolveCreative({ sessions: [] });
    await pending;
    expect(workspace.innerHTML).toContain("内容生产总编");
    expect(workspace.innerHTML).not.toContain("今天想拍什么");
  });

  it("locks a whole decision group so accept and decline cannot double-submit", async () => {
    const { context } = await loadCreativeUiContext();
    let resolveRequest!: (value: unknown) => void;
    const request = new Promise((resolve) => { resolveRequest = resolve; });
    const accept = { ...fakeElement(), disabled: false, textContent: "看看素材" };
    const decline = { ...fakeElement(), disabled: false, textContent: "这次不使用" };
    const group = { ...fakeElement(), dataset: {}, querySelectorAll: () => [accept, decline] };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    Object.assign(context, { testAccept: accept, testDecline: decline, testGroup: group,
      testFeedback: feedback, testRequest: request, testCalls: calls });

    const pending = runInContext(`api = async (path) => { testCalls.push(path); return testRequest };
      Promise.all([
        mutateCreativeSession({ button: testAccept, group: testGroup, feedback: testFeedback,
          busyText: "正在记录…", path: "/decision", body: {} }),
        mutateCreativeSession({ button: testDecline, group: testGroup, feedback: testFeedback,
          busyText: "正在记录…", path: "/decision", body: {} })
      ])`, context);
    await Promise.resolve();
    expect(calls).toHaveLength(1);
    expect(accept.disabled).toBe(true);
    expect(decline.disabled).toBe(true);
    resolveRequest({});
    await pending;
    expect(accept.disabled).toBe(false);
    expect(decline.disabled).toBe(false);
  });

  it("uses one global creative mutation lock across different cards and forms", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    let resolveRequest!: (value: unknown) => void;
    const request = new Promise((resolve) => { resolveRequest = resolve; });
    const first = { ...fakeElement(), disabled: false, textContent: "重拍" };
    const second = { ...fakeElement(), disabled: false, textContent: "隐私暂停" };
    const firstGroup = { ...fakeElement(), dataset: {}, querySelectorAll: () => [first] };
    const secondGroup = { ...fakeElement(), dataset: {}, querySelectorAll: () => [second] };
    const calls: string[] = [];
    const aborts: string[] = [];
    class TrackingAbortController {
      signal = {};
      abort() { aborts.push("aborted"); }
    }
    workspace.querySelectorAll = () => [first, second];
    Object.assign(context, { testFirst: first, testSecond: second, testFirstGroup: firstGroup,
      testSecondGroup: secondGroup, testRequest: request, testCalls: calls,
      AbortController: TrackingAbortController });

    const pending = runInContext(`api = async (path) => { testCalls.push(path); return testRequest };
      Promise.all([
        mutateCreativeSession({ button: testFirst, group: testFirstGroup, busyText: "正在记录…", path: "/first", body: {} }),
        mutateCreativeSession({ button: testSecond, group: testSecondGroup, busyText: "正在记录…", path: "/second", body: {} })
      ])`, context);
    await Promise.resolve();

    expect(calls).toEqual(["/first"]);
    expect(aborts).toEqual([]);
    expect(first.disabled).toBe(true);
    expect(second.disabled).toBe(true);
    resolveRequest({});
    await pending;
    expect(first.disabled).toBe(false);
    expect(second.disabled).toBe(false);
  });

  it("lets only the mutation detail response commit after an older creative GET", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    let resolveOldList!: (value: unknown) => void;
    const oldList = new Promise((resolve) => { resolveOldList = resolve; });
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const detailCalls = { count: 0 };
    Object.assign(context, { testOldList: oldList, testButton: button, testDetailCalls: detailCalls });

    const oldLoad = runInContext(`api = async (path, options) => {
        if (path === "/api/creative-sessions") return testOldList;
        if (options?.method === "POST") return {};
        if (path === "/api/creative-sessions/session_epoch") {
          testDetailCalls.count += 1;
          return { session: testDetailCalls.count === 1
            ? { id: "session_epoch", stage: "materials_declined", title: "新阶段" }
            : { id: "session_epoch", stage: "awaiting_material_consent", title: "旧阶段", materialSuggestion: { count: 1 } } };
        }
        return {};
      };
      activeView = "creative";
      loadView()`, context);
    await runInContext(`mutateCreativeSession({ sessionId: "session_epoch", button: testButton,
      busyText: "正在确认…", path: "/mutation", body: {} })`, context);
    expect(workspace.innerHTML).toContain("已按你的决定准备好");

    resolveOldList({ sessions: [{ id: "session_epoch", updatedAt: "2026-09-05T00:00:00.000Z" }] });
    await oldLoad;
    expect(workspace.innerHTML).toContain("已按你的决定准备好");
    expect(workspace.innerHTML).not.toContain("要看看以前拍过的素材吗");
  });

  it("does not start a creative refresh while a mutation is in flight", async () => {
    const { context } = await loadCreativeUiContext();
    let resolveMutation!: (value: unknown) => void;
    const mutation = new Promise((resolve) => { resolveMutation = resolve; });
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const calls: string[] = [];
    Object.assign(context, { testMutation: mutation, testButton: button, testCalls: calls });

    const pending = runInContext(`api = async (path, options) => {
        testCalls.push((options?.method || "GET") + " " + path);
        if (options?.method === "POST") return testMutation;
        return { session: { id: "session_01", stage: "materials_declined" } };
      };
      const mutationWork = mutateCreativeSession({ sessionId: "session_01", button: testButton,
        busyText: "正在确认…", path: "/mutation", body: {} });
      const refreshWork = loadView();
      Promise.all([mutationWork, refreshWork])`, context);
    await Promise.resolve();
    expect(calls).toEqual(["POST /mutation"]);
    resolveMutation({});
    await pending;
    expect(calls).toEqual([
      "POST /mutation",
      "GET /api/creative-sessions/session_01",
      "DELETE /api/creative-operation-receipts/operation_01",
    ]);
  });

  it("reconciles the creative page after switching to review and back during a mutation", async () => {
    const { context, workspace, elements, navButtons } = await loadCreativeUiContext();
    let resolveMutation!: (value: unknown) => void;
    const mutation = new Promise((resolve) => { resolveMutation = resolve; });
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const calls: string[] = [];
    Object.assign(context, { testMutation: mutation, testButton: button, testCalls: calls });

    const pending = runInContext(`api = async (path, options) => {
        testCalls.push((options?.method || "GET") + " " + path);
        if (options?.method === "POST") return testMutation;
        if (path === "/api/creative-sessions/session_switch") {
          return { session: { id: "session_switch", stage: "materials_declined", title: "往返后的新阶段" } };
        }
        if (path.startsWith("/api/batches/")) return { assets: [], stories: [], failures: [] };
        if (path === "/api/review") return { stories: [] };
        if (path === "/api/approved") return { approvals: [] };
        return { segments: [] };
      };
      activeView = "creative";
      mutateCreativeSession({ sessionId: "session_switch", button: testButton,
        busyText: "正在确认…", path: "/mutation", body: {} })`, context);
    await Promise.resolve();

    await runInContext(`activeView = "review"; loadView()`, context);
    await runInContext(`activeView = "creative"; loadView()`, context);

    expect(elements.get("#view-title")?.textContent).toBe("今天的创作");
    expect(elements.get("#creative-session-header")?.hidden).toBe(false);
    expect(elements.get("#production-toolbar")?.hidden).toBe(true);
    expect(navButtons.find((item) => item.dataset.view === "creative")?.classList.contains("active")).toBe(true);
    expect(calls).not.toContain("GET /api/creative-sessions/session_switch");

    resolveMutation({});
    await pending;

    expect(calls).toContain("GET /api/creative-sessions/session_switch");
    expect(elements.get("#view-title")?.textContent).toBe("今天的创作");
    expect(elements.get("#creative-session-header")?.innerHTML).toContain("往返后的新阶段");
    expect(workspace.innerHTML).toContain("已按你的决定准备好");
    expect(navButtons.find((item) => item.dataset.view === "creative")?.classList.contains("active")).toBe(true);
  });

  it("times out a creative mutation, unlocks controls, and reconciles server state", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const second = { ...fakeElement(), disabled: false, textContent: "另一操作" };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    const aborts: string[] = [];
    class TrackingAbortController {
      signal = { aborted: false };
      abort() { this.signal.aborted = true; aborts.push("aborted"); }
    }
    workspace.querySelectorAll = () => [button, second];
    Object.assign(context, { testButton: button, testSecond: second, testFeedback: feedback,
      testCalls: calls, AbortController: TrackingAbortController });

    await runInContext(`creativeRequestTimeoutMs = 5;
      api = async (path, options) => {
        testCalls.push((options?.method || "GET") + " " + path);
        if (options?.method === "POST") return new Promise(() => {});
        return { session: { id: "session_timeout", stage: "materials_declined", title: "超时后状态" } };
      };
      mutateCreativeSession({ sessionId: "session_timeout", button: testButton, feedback: testFeedback,
        busyText: "正在确认…", path: "/mutation", body: {} })`, context);

    expect(calls).toEqual([
      "POST /mutation",
      "GET /api/creative-sessions/session_timeout",
    ]);
    expect(aborts.length).toBeGreaterThan(0);
    expect(button.disabled).toBe(false);
    expect(second.disabled).toBe(false);
    expect(feedback.textContent).toBe("请求超时，已自动对账；可安全重试，不会重复执行");
    expect(workspace.innerHTML).toContain("已按你的决定准备好");
  });

  it("reconciles through the session list when a timed-out create has no response id", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const button = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });

    await runInContext(`creativeRequestTimeoutMs = 5;
      api = async (path, options) => {
        testCalls.push((options?.method || "GET") + " " + path);
        if (options?.method === "POST") return new Promise(() => {});
        if (path.includes("creative-operation-receipts")) {
          return { receipt: { status: "inflight", clientInstanceId: "client_instance_ui_0001",
            request: { method: "POST", path: "/api/creative-sessions", body: {} } } };
        }
        if (path === "/api/creative-sessions") {
          return { sessions: [{ id: "session_created_after_timeout", updatedAt: "2026-09-05T00:00:00.000Z" }] };
        }
        return { session: { id: "session_created_after_timeout", stage: "awaiting_material_consent",
          title: "服务端已创建", materialSuggestion: { count: 1 } } };
      };
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions", body: {} })`, context);

    expect(calls).toEqual([
      "POST /api/creative-sessions",
      "GET /api/creative-operation-receipts/operation_01",
      "GET /api/creative-sessions",
      "GET /api/creative-sessions/session_created_after_timeout",
    ]);
    expect(button.disabled).toBe(false);
    expect(workspace.innerHTML).toContain("要看看以前拍过的素材吗");
    expect(feedback.textContent).toBe("请求超时，已自动对账；可安全重试，不会重复执行");
  });

  it("restores a timed-out logical operation key across reload without reusing it for edited input", async () => {
    const storage = memoryStorage();
    const first = await loadCreativeUiContext(storage);
    const firstButton = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const firstFeedback = { textContent: "" };
    const firstRequests: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(first.context, {
      testButton: firstButton,
      testFeedback: firstFeedback,
      testRequests: firstRequests,
      crypto: { randomUUID: () => "logical-operation-original" },
    });

    await runInContext(`creativeRequestTimeoutMs = 5;
      api = async (path, options = {}) => {
        testRequests.push({ path, options });
        if (options.method === "POST") return new Promise(() => {});
        return { session: { id: "session_old", stage: "draft_intent", title: "旧状态" } };
      };
      mutateCreativeSession({ sessionId: "session_old", button: testButton, feedback: testFeedback,
        busyText: "正在修改…", path: "/api/creative-sessions/session_old/script-revisions",
        body: { prompt: "保留原开头" } })`, first.context);

    const originalKey = firstRequests[0]?.options.headers?.["x-operation-key"];
    expect(originalKey).toBe("logical-operation-original");
    expect(firstFeedback.textContent).toContain("可安全重试");

    const reloaded = await loadCreativeUiContext(storage);
    const retryButton = { ...fakeElement(), disabled: false, textContent: "继续修改" };
    const retryFeedback = { textContent: "" };
    const retriedRequests: Array<{ path: string; options: Record<string, any> }> = [];
    let generated = 0;
    Object.assign(reloaded.context, {
      testButton: retryButton,
      testFeedback: retryFeedback,
      testRequests: retriedRequests,
      crypto: { randomUUID: () => `logical-operation-new-${++generated}` },
    });
    await runInContext(`api = async (path, options = {}) => {
        testRequests.push({ path, options });
        if (options.method === "POST") return { script: { scriptVersion: 2 } };
        return { session: { id: "session_old", stage: "awaiting_script_confirmation", currentScriptVersion: 2 } };
      };
      mutateCreativeSession({ sessionId: "session_old", button: testButton, feedback: testFeedback,
        busyText: "正在修改…", path: "/api/creative-sessions/session_old/script-revisions",
        body: { prompt: "换成新的开头" } })`, reloaded.context);
    await runInContext(`mutateCreativeSession({ sessionId: "session_old", button: testButton, feedback: testFeedback,
        busyText: "正在修改…", path: "/api/creative-sessions/session_old/script-revisions",
        body: { prompt: "保留原开头" } })`, reloaded.context);

    const mutationKeys = retriedRequests
      .filter((item) => item.options.method === "POST")
      .map((item) => item.options.headers?.["x-operation-key"]);
    expect(mutationKeys).toEqual(["logical-operation-new-1", originalKey]);
  });

  it("clears a deterministic 4xx key only after a successful state reconciliation", async () => {
    const { context } = await loadCreativeUiContext();
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const feedback = { textContent: "" };
    const calls: Array<{ path: string; options: Record<string, any> }> = [];
    let generated = 0;
    Object.assign(context, {
      testButton: button,
      testFeedback: feedback,
      testCalls: calls,
      crypto: { randomUUID: () => `logical-4xx-${++generated}` },
    });

    await runInContext(`api = async (path, options = {}) => {
        testCalls.push({ path, options });
        if (options.method === "POST") {
          const error = new Error("material-view-decision-not-allowed");
          error.status = 400;
          throw error;
        }
        return { session: { id: "session_01", stage: "materials_declined" } };
      };
      mutateCreativeSession({ sessionId: "session_01", button: testButton, feedback: testFeedback,
        busyText: "正在确认…", path: "/api/creative-sessions/session_01/material-viewing",
        body: { decision: "accept", suggestionSetId: "set_01" } })`, context);
    await runInContext(`mutateCreativeSession({ sessionId: "session_01", button: testButton, feedback: testFeedback,
        busyText: "正在确认…", path: "/api/creative-sessions/session_01/material-viewing",
        body: { decision: "accept", suggestionSetId: "set_01" } })`, context);

    expect(calls.map((item) => `${item.options.method || "GET"} ${item.path}`)).toEqual([
      "POST /api/creative-sessions/session_01/material-viewing",
      "GET /api/creative-sessions/session_01",
      "DELETE /api/creative-operation-receipts/logical-4xx-1",
      "POST /api/creative-sessions/session_01/material-viewing",
      "GET /api/creative-sessions/session_01",
      "DELETE /api/creative-operation-receipts/logical-4xx-2",
    ]);
    expect(calls.filter((item) => item.options.method === "POST")
      .map((item) => item.options.headers["x-operation-key"]))
      .toEqual(["logical-4xx-1", "logical-4xx-2"]);
  });

  it("uses the request operation key as the durable field-event operation id", async () => {
    const storage = memoryStorage();
    const { context, elements } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "记录" };
    const requests: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(context, {
      testButton: button,
      testRequests: requests,
      crypto: { randomUUID: () => "logical-field-event-0001" },
    });

    await runInContext(`activeView = "review";
      api = async (path, options = {}) => {
        testRequests.push({ path, options });
        return { event: { id: "event_01" }, resultingScriptVersion: 1 };
      };
      mutateCreativeSession({ sessionId: "session_01", button: testButton,
        busyText: "正在记录…", path: "/api/creative-sessions/session_01/events",
        body: { action: "note", stepId: "step_01", reason: "保留这个光线",
          briefVersion: 1, scriptVersion: 1, shotPlanVersion: 1 } })`, context);

    const request = requests[0];
    const body = JSON.parse(request.options.body);
    expect(request.path).not.toContain("logical-field-event-0001");
    expect(request.options.headers["x-operation-key"]).toBe("logical-field-event-0001");
    expect(body.operationId).toBe("logical-field-event-0001");
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("keeps an ambiguous browser key past ten minutes until reconciliation succeeds", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([
      pendingOperationRecord({
        path: "/api/creative-sessions/session_01/script-revisions",
        body: { prompt: "过期的改写" },
        operationKey: "logical-operation-expired",
        sessionId: "session_01",
      }),
    ]));
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "继续修改" };
    const keys: string[] = [];
    Object.assign(context, {
      testButton: button,
      testKeys: keys,
      crypto: { randomUUID: () => "logical-operation-fresh" },
    });

    await runInContext(`activeView = "review";
      api = async (_path, options = {}) => {
        if (options.method === "POST") testKeys.push(options.headers["x-operation-key"]);
        return { script: { scriptVersion: 2 } };
      };
      mutateCreativeSession({ sessionId: "session_01", button: testButton,
        busyText: "正在修改…", path: "/api/creative-sessions/session_01/script-revisions",
        body: { prompt: "过期的改写" } })`, context);

    expect(keys).toEqual(["logical-operation-expired"]);
  });

  it("retains a successful POST key when detail reconciliation fails, then replays one create", async () => {
    const storage = memoryStorage();
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const feedback = { textContent: "" };
    const requests: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(context, {
      testButton: button,
      testFeedback: feedback,
      testRequests: requests,
      crypto: { randomUUID: () => "logical-create-detail-retry" },
    });

    await runInContext(`let detailShouldFail = true;
      api = async (path, options = {}) => {
        testRequests.push({ path, options });
        if (options.method === "POST") {
          return { session: { id: "session_created_once" } };
        }
        if (detailShouldFail) throw new Error("creative-detail-unavailable");
        return { session: { id: "session_created_once", stage: "awaiting_material_consent",
          title: "只创建一次", materialSuggestion: { count: 1 } } };
      };
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "只创建一次", text: "对账失败后重试" } })`, context);

    expect(storage.getItem("wearable.creative.pending-operations.v1")).toContain("logical-create-detail-retry");

    await runInContext(`detailShouldFail = false;
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "只创建一次", text: "对账失败后重试" } })`, context);

    const creates = requests.filter((item) => item.options.method === "POST");
    expect(creates).toHaveLength(2);
    expect(creates.map((item) => item.options.headers["x-operation-key"]))
      .toEqual(["logical-create-detail-retry", "logical-create-detail-retry"]);
    expect(requests.at(-1)).toMatchObject({
      path: "/api/creative-operation-receipts/logical-create-detail-retry",
      options: { method: "DELETE" },
    });
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("retries only acknowledgement after its response is lost, never the reconciled mutation", async () => {
    const storage = memoryStorage();
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "生成脚本" };
    const feedback = { textContent: "" };
    const methods: string[] = [];
    Object.assign(context, {
      testButton: button,
      testFeedback: feedback,
      testMethods: methods,
      crypto: { randomUUID: () => "logical-ack-response-lost" },
    });

    await runInContext(`let acknowledgementFails = true;
      api = async (path, options = {}) => {
        testMethods.push((options.method || "GET") + " " + path);
        if (options.method === "POST") return { script: { scriptVersion: 1 } };
        if (options.method === "DELETE") {
          if (acknowledgementFails) throw new Error("ack-response-lost");
          return { acknowledged: true };
        }
        return { session: { id: "session_ack", stage: "awaiting_script_confirmation",
          currentScriptVersion: 1 } };
      };
      mutateCreativeSession({ sessionId: "session_ack", button: testButton,
        feedback: testFeedback, busyText: "正在生成…",
        path: "/api/creative-sessions/session_ack/scripts", body: {} })`, context);

    expect(storage.getItem("wearable.creative.pending-operations.v1")).toContain('"state":"reconciled"');

    await runInContext(`acknowledgementFails = false;
      mutateCreativeSession({ sessionId: "session_ack", button: testButton,
        feedback: testFeedback, busyText: "正在生成…",
        path: "/api/creative-sessions/session_ack/scripts", body: {} })`, context);

    expect(methods).toEqual([
      "POST /api/creative-sessions/session_ack/scripts",
      "GET /api/creative-sessions/session_ack",
      "DELETE /api/creative-operation-receipts/logical-ack-response-lost",
      "DELETE /api/creative-operation-receipts/logical-ack-response-lost",
    ]);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("retries a reconciled acknowledgement on refresh without replaying the mutation", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([
      pendingOperationRecord({
        path: "/api/creative-sessions/session_refresh_ack/scripts",
        operationKey: "logical-refresh-ack-retry",
        state: "reconciled",
        sessionId: "session_refresh_ack",
      }),
    ]));
    const { context } = await loadCreativeUiContext(storage);
    const calls: string[] = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`api = async (path, options = {}) => {
        testCalls.push((options.method || "GET") + " " + path);
        if (options.method === "POST") throw new Error("mutation-must-not-replay");
        if (options.method === "DELETE") return { acknowledged: true };
        return { sessions: [] };
      };
      activeView = "creative";
      loadView()`, context);

    expect(calls).toEqual([
      "DELETE /api/creative-operation-receipts/logical-refresh-ack-retry",
      "GET /api/creative-sessions",
    ]);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("reuses one operation key after a transient 500 and clears it after successful reconciliation", async () => {
    const storage = memoryStorage();
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const feedback = { textContent: "" };
    const keys: string[] = [];
    Object.assign(context, {
      testButton: button,
      testFeedback: feedback,
      testKeys: keys,
      crypto: { randomUUID: () => "logical-transient-500" },
    });

    await runInContext(`let postAttempts = 0;
      api = async (_path, options = {}) => {
        if (options.method === "POST") {
          testKeys.push(options.headers["x-operation-key"]);
          postAttempts += 1;
          if (postAttempts === 1) {
            const error = new Error("internal-error");
            error.status = 500;
            throw error;
          }
          return { session: { id: "session_after_retry" } };
        }
        return { session: { id: "session_after_retry", stage: "awaiting_material_consent",
          title: "重试成功", materialSuggestion: { count: 1 } } };
      };
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "重试成功", text: "第一次 500" } })`, context);
    await runInContext(`mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "重试成功", text: "第一次 500" } })`, context);

    expect(keys).toEqual(["logical-transient-500", "logical-transient-500"]);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("blocks a new mutation when all 32 browser operation slots are still ambiguous", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify(
      Array.from({ length: 32 }, (_, index) => pendingOperationRecord({
        path: `/api/creative-sessions/session_capacity/script-revisions/${index}`,
        operationKey: `pending-operation-${index}`,
      })),
    ));
    const { context } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "继续修改" };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });

    await runInContext(`api = async (path) => { testCalls.push(path); return {} };
      mutateCreativeSession({ sessionId: "session_capacity", button: testButton,
        feedback: testFeedback, busyText: "正在修改…",
        path: "/api/creative-sessions/session_capacity/script-revisions",
        body: { prompt: "这是一个新操作" } })`, context);

    expect(calls).toEqual([]);
    expect(feedback.textContent).toContain("32 个待确认操作");
    expect(feedback.textContent).toContain("重试原操作");
    expect(button.disabled).toBe(false);
    expect(JSON.parse(storage.getItem("wearable.creative.pending-operations.v1") ?? "[]"))
      .toHaveLength(32);
  });

  it.each([
    ["malformed JSON", "{"],
    ["non-array JSON", JSON.stringify({ operationKey: "not-an-array" })],
    ["invalid entry", JSON.stringify([{ identity: "missing-request-fields" }])],
    ["duplicate identity", JSON.stringify([
      pendingOperationRecord({ operationKey: "duplicate-operation-0001" }),
      pendingOperationRecord({ operationKey: "duplicate-operation-0002" }),
    ])],
    ["more than 32 entries", JSON.stringify(Array.from({ length: 33 }, (_, index) =>
      pendingOperationRecord({
        path: `/api/creative-sessions/session_storage/script-revisions/${index}`,
        operationKey: `storage-overflow-operation-${index}`,
      })))],
  ])("fails closed without deleting %s pending-operation storage", async (_label, raw) => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", raw);
    const { context, workspace } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "继续修改" };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async (path) => { testCalls.push(path); return {} };
      renderCreativeSession({ id: "session_storage", stage: "awaiting_script_confirmation",
        currentScriptVersion: 1, script: { scriptVersion: 1, title: "Storage gate", opening: "Open",
          sections: [], narrativeOrder: ["open"] } });
      mutateCreativeSession({ sessionId: "session_storage", button: testButton,
        feedback: testFeedback, busyText: "正在修改…",
        path: "/api/creative-sessions/session_storage/script-revisions",
        body: { prompt: "keep this operation" } })`, context);

    expect(calls).toEqual([]);
    expect(feedback.textContent).toContain("本机操作记录异常");
    expect(workspace.innerHTML).toContain("人工重置本机操作记录");
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe(raw);
  });

  it("fails closed when sessionStorage reads or writes throw and never posts first", async () => {
    const readFailure = {
      getItem() { throw new Error("SecurityError"); },
      setItem() { throw new Error("SecurityError"); },
      removeItem() { throw new Error("SecurityError"); },
    };
    const readContext = await loadCreativeUiContext(readFailure);
    const readButton = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const readFeedback = { textContent: "" };
    const readCalls: string[] = [];
    Object.assign(readContext.context, {
      testButton: readButton,
      testFeedback: readFeedback,
      testCalls: readCalls,
    });
    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async (path) => { testCalls.push(path); return {} };
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "read failure", text: "must not post" } })`, readContext.context);
    expect(readCalls).toEqual([]);
    expect(readFeedback.textContent).toContain("本机操作记录异常");

    const writable = memoryStorage();
    const writeContext = await loadCreativeUiContext(writable);
    const writeButton = { ...fakeElement(), disabled: false, textContent: "开始创作" };
    const writeFeedback = { textContent: "" };
    const order: string[] = [];
    writable.setItem = () => { order.push("persist"); throw new Error("QuotaExceededError"); };
    Object.assign(writeContext.context, {
      testButton: writeButton,
      testFeedback: writeFeedback,
      testOrder: order,
    });
    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async () => { testOrder.push("post"); return {} };
      mutateCreativeSession({ button: testButton, feedback: testFeedback,
        busyText: "正在开始…", path: "/api/creative-sessions",
        body: { customerId: "designer_01", title: "write failure", text: "persist first" } })`, writeContext.context);
    expect(order).toEqual(["persist"]);
    expect(writeFeedback.textContent).toContain("无法安全保存");
  });

  it("clears corrupt storage only after an explicit confirmed manual reset", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", "not-json");
    const { context } = await loadCreativeUiContext(storage);
    const calls: string[] = [];
    Object.assign(context, { testCalls: calls });

    const reset = runInContext(`api = async (path) => { testCalls.push(path); return {} };
      window.confirm = () => true;
      resetCreativePendingOperationsFromUi()`, context);

    expect(reset).toBe(true);
    expect(calls).toEqual([]);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBeNull();
    expect(runInContext("creativePendingStorageIssue", context)).toBeNull();
  });

  it("queries every ambiguous receipt on reload and recovers completed work after its stage advanced", async () => {
    const operation = pendingOperationRecord({
      path: "/api/creative-sessions/session_recovery/scripts",
      operationKey: "reload-completed-operation-0001",
      sessionId: "session_recovery",
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    const { context } = await loadCreativeUiContext(storage);
    const calls: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async (path, options = {}) => {
        testCalls.push({ path, options });
        if (path.includes("creative-operation-receipts") && options.method !== "DELETE") {
          return { receipt: { status: "completed", clientInstanceId: "client_instance_ui_0001",
            request: ${JSON.stringify(operation.request)},
            response: { status: 201, body: { script: { id: "script_recovered", scriptVersion: 1 } } } } };
        }
        if (options.method === "DELETE") return { acknowledged: true };
        return { session: { id: "session_recovery", stage: "shooting", currentScriptVersion: 1 } };
      };
      recoverPendingCreativeOperations()`, context);

    expect(calls.map(({ path, options }) => `${options.method || "GET"} ${path}`)).toEqual([
      "GET /api/creative-operation-receipts/reload-completed-operation-0001",
      "GET /api/creative-sessions/session_recovery",
      "DELETE /api/creative-operation-receipts/reload-completed-operation-0001",
    ]);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBe("[]");
  });

  it("keeps inflight and retryable reload receipts visible and safely retries the exact saved request", async () => {
    const inflight = pendingOperationRecord({
      path: "/api/creative-sessions/session_recovery/events",
      operationKey: "reload-inflight-operation-0001",
      sessionId: "session_recovery",
      body: { action: "note", reason: "light changed" },
    });
    const retryable = pendingOperationRecord({
      path: "/api/creative-sessions/session_recovery/script-revisions",
      operationKey: "reload-retryable-operation-0001",
      sessionId: "session_recovery",
      body: { prompt: "make the opening calmer" },
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([inflight, retryable]));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const calls: Array<{ path: string; options: Record<string, any> }> = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async (path, options = {}) => {
        testCalls.push({ path, options });
        if (path.includes("reload-inflight-operation-0001")) {
          return { receipt: { status: "inflight", clientInstanceId: "client_instance_ui_0001",
            request: ${JSON.stringify(inflight.request)} } };
        }
        if (path.includes("reload-retryable-operation-0001") && options.method !== "DELETE") {
          return { receipt: { status: "retryable", clientInstanceId: "client_instance_ui_0001",
            request: ${JSON.stringify(retryable.request)},
            response: { status: 500, body: { error: "internal-error" } } } };
        }
        if (options.method === "POST") return { script: { id: "script_retry", scriptVersion: 2 } };
        if (options.method === "DELETE") return { acknowledged: true };
        return { session: { id: "session_recovery", stage: "awaiting_script_confirmation",
          currentScriptVersion: 2 } };
      };
      recoverPendingCreativeOperations().then(() =>
        renderCreativeSession({ id: "session_recovery", stage: "editorial_review" }))`, context);

    expect(workspace.innerHTML).toContain("仍在服务端处理");
    expect(workspace.innerHTML).toContain("可使用同一操作键安全重试");
    expect(calls.filter(({ options }) => options.method === "POST")).toEqual([]);

    await runInContext(`retryPendingCreativeOperation(
      creativePendingOperations.get(${JSON.stringify(retryable.identity)}))`, context);
    const postCall = calls.find(({ options }) => options.method === "POST");
    expect(postCall).toMatchObject({
      path: retryable.request.path,
      options: {
        headers: {
          "x-client-instance": "client_instance_ui_0001",
          "x-operation-key": retryable.operationKey,
        },
        body: JSON.stringify(retryable.request.body),
      },
    });
  });

  it("refuses to query or acknowledge a recovered receipt owned by another client", async () => {
    const operation = pendingOperationRecord({
      operationKey: "foreign-client-operation-0001",
      clientInstanceId: "client_instance_foreign_0001",
    });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    const { context, elements } = await loadCreativeUiContext(storage);
    const calls: string[] = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001" };
      api = async (path) => { testCalls.push(path); return {} };
      recoverPendingCreativeOperations()`, context);

    expect(calls).toEqual([]);
    expect(storage.getItem("wearable.creative.pending-operations.v1"))
      .toContain("foreign-client-operation-0001");
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-pending-client-mismatch");
    expect(elements.get("#status")?.textContent).toContain("客户端实例");
  });

  it("keeps a restarted-server mutation ambiguous and blocks creative POSTs until reset", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: "client_instance_ui_0001",
      clientClaim: "creative_claim_before_restart_0001",
    }));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const feedback = { textContent: "" };
    const calls: Array<{ path: string; method: string }> = [];
    Object.assign(context, {
      navigator: { locks: memoryWebLocks() },
      testButton: button,
      testFeedback: feedback,
      fetch: async (path: string, options: Record<string, any> = {}) => {
        calls.push({ path, method: options.method ?? "GET" });
        return { ok: false, status: 409, json: async () => ({
          error: "creative-client-instance-invalid",
        }) };
      },
    });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001",
        clientClaim: "creative_claim_before_restart_0001" };
      mutateCreativeSession({ sessionId: "session_restart_mutation", button: testButton,
        feedback: testFeedback, busyText: "正在确认…",
        path: "/api/creative-sessions/session_restart_mutation/script-revisions",
        body: { prompt: "keep this ambiguous" } })`, context);

    expect(calls).toEqual([{
      path: "/api/creative-sessions/session_restart_mutation/script-revisions",
      method: "POST",
    }]);
    const pending = JSON.parse(
      storage.getItem("wearable.creative.pending-operations.v1") ?? "[]",
    );
    expect(pending).toEqual([expect.objectContaining({
      operationKey: "operation_01",
      state: "ambiguous",
      receiptStatus: "unknown",
    })]);
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-client-recovery-required");
    expect(workspace.innerHTML).toContain("服务已重启，待处理操作无法自动确认");
    expect(workspace.innerHTML).toContain("data-reset-pending-operations");
    expect(feedback.textContent).toContain("服务已重启");

    const blocked = await runInContext(
      `api("/api/creative-sessions/session_restart_mutation/scripts", {
        method: "POST", body: "{}" }).catch((error) => error.message)`,
      context,
    );
    expect(blocked).toBe("creative-client-recovery-required");
    expect(calls).toHaveLength(1);
  });

  it("enters restart recovery when an ambiguous receipt query loses its server owner", async () => {
    const operation = pendingOperationRecord({ operationKey: "restart-query-operation-0001" });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: operation.clientInstanceId,
      clientClaim: "creative_claim_before_restart_0001",
    }));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const calls: Array<{ path: string; method: string }> = [];
    Object.assign(context, {
      fetch: async (path: string, options: Record<string, any> = {}) => {
        calls.push({ path, method: options.method ?? "GET" });
        return { ok: false, status: 409, json: async () => ({
          error: "creative-client-instance-invalid",
        }) };
      },
    });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001",
        clientClaim: "creative_claim_before_restart_0001" };
      recoverPendingCreativeOperations()`, context);

    expect(calls).toEqual([{
      path: "/api/creative-operation-receipts/restart-query-operation-0001",
      method: "GET",
    }]);
    expect(JSON.parse(storage.getItem("wearable.creative.pending-operations.v1") ?? "[]"))
      .toEqual([expect.objectContaining({ state: "ambiguous", receiptStatus: "unknown" })]);
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-client-recovery-required");
    expect(workspace.innerHTML).toContain("服务已重启，待处理操作无法自动确认");
  });

  it("demotes a pending acknowledgement to ambiguous when the server owner is lost", async () => {
    const operation = {
      ...pendingOperationRecord({
        operationKey: "restart-ack-operation-0001",
        state: "reconciled",
      }),
      receiptStatus: "completed",
    };
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: operation.clientInstanceId,
      clientClaim: "creative_claim_before_restart_0001",
    }));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const calls: Array<{ path: string; method: string }> = [];
    Object.assign(context, {
      fetch: async (path: string, options: Record<string, any> = {}) => {
        calls.push({ path, method: options.method ?? "GET" });
        return { ok: false, status: 409, json: async () => ({
          error: "creative-client-instance-invalid",
        }) };
      },
    });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001",
        clientClaim: "creative_claim_before_restart_0001" };
      retryReconciledCreativeAcknowledgements()`, context);

    expect(calls).toEqual([{
      path: "/api/creative-operation-receipts/restart-ack-operation-0001",
      method: "DELETE",
    }]);
    expect(JSON.parse(storage.getItem("wearable.creative.pending-operations.v1") ?? "[]"))
      .toEqual([expect.objectContaining({ state: "ambiguous", receiptStatus: "unknown" })]);
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-client-recovery-required");
    expect(workspace.innerHTML).toContain("服务已重启，待处理操作无法自动确认");
  });

  it("shares one concurrent session bootstrap for the same stored claim", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: "client_instance_ui_0001",
      clientClaim: "creative_claim_before_bootstrap_0001",
    }));
    const { context } = await loadCreativeUiContext(storage);
    let releaseBootstrap!: () => void;
    const bootstrapGate = new Promise<void>((resolve) => { releaseBootstrap = resolve; });
    const calls: Array<{ path: string; headers: Record<string, string> }> = [];
    let sessionAttempts = 0;
    Object.assign(context, {
      navigator: { locks: memoryWebLocks() },
      testBootstrapGate: bootstrapGate,
      fetch: async (path: string, options: Record<string, any> = {}) => {
        calls.push({ path, headers: options.headers ?? {} });
        if (path === "/api/session") {
          sessionAttempts += 1;
          const attempt = sessionAttempts;
          await bootstrapGate;
          if (attempt > 1) {
            return { ok: false, status: 409, json: async () => ({
              error: "creative-client-claim-invalid",
            }) };
          }
          return { ok: true, status: 200, json: async () => ({
            designerId: "designer_01",
            reviewToken: "safe-review-token",
            clientInstanceId: "client_instance_ui_0001",
            clientClaim: "creative_claim_after_bootstrap_0002",
          }) };
        }
        return { ok: true, status: 200, json: async () => ({ sessions: [] }) };
      },
    });

    const concurrent = runInContext(`reviewSession = null; Promise.allSettled([
      api("/api/creative-sessions"),
      api("/api/creative-sessions"),
    ])`, context) as Promise<Array<{ status: string }>>;
    await new Promise((resolve) => setTimeout(resolve, 0));
    releaseBootstrap();
    const results = await concurrent;

    expect(results.map((result) => result.status)).toEqual(["fulfilled", "fulfilled"]);
    expect(calls.filter((call) => call.path === "/api/session")).toHaveLength(1);
    expect(calls.filter((call) => call.path === "/api/creative-sessions")).toHaveLength(2);
    expect(calls[0]?.headers).toMatchObject({
      "x-client-instance": "client_instance_ui_0001",
      "x-client-claim": "creative_claim_before_bootstrap_0001",
    });
    expect(storage.getItem("wearable.creative.client-owner.v1"))
      .toContain("creative_claim_after_bootstrap_0002");
    expect(runInContext("creativePendingStorageIssue", context)).toBeNull();
  });

  it("keeps a stale copied-tab claim distinct from server-restart recovery", async () => {
    const storage = memoryStorage();
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: "client_instance_ui_0001",
      clientClaim: "creative_claim_stale_copy_0001",
    }));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const calls: string[] = [];
    Object.assign(context, {
      navigator: { locks: memoryWebLocks() },
      fetch: async (path: string) => {
        calls.push(path);
        return { ok: false, status: 409, json: async () => ({
          error: "creative-client-claim-invalid",
        }) };
      },
    });

    const error = await runInContext(
      `reviewSession = null; api("/api/session").catch((failure) => failure.message)`,
      context,
    );
    const blocked = await runInContext(
      `api("/api/creative-sessions", { method: "POST", body: "{}" })
        .catch((failure) => failure.message)`,
      context,
    );

    expect(error).toBe("creative-client-claim-invalid");
    expect(blocked).toBe("creative-client-claim-invalid");
    expect(calls).toEqual(["/api/session"]);
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-client-claim-invalid");
    expect(workspace.innerHTML).toContain("客户端租约已变更");
    expect(workspace.innerHTML).not.toContain("服务已重启");
  });

  it("leases a copied owner to one tab, blocks the duplicate, and lets a reload reclaim it", async () => {
    const operation = pendingOperationRecord();
    const firstStorage = memoryStorage();
    const copiedStorage = memoryStorage();
    for (const storage of [firstStorage, copiedStorage]) {
      storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
      storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
        clientInstanceId: operation.clientInstanceId,
        clientClaim: "creative_claim_original_0001",
      }));
    }
    const locks = memoryWebLocks();
    const first = await loadCreativeUiContext(firstStorage);
    const copied = await loadCreativeUiContext(copiedStorage);
    Object.assign(first.context, { navigator: { locks } });
    Object.assign(copied.context, { navigator: { locks } });
    const firstCalls: Array<Record<string, any>> = [];
    const copiedCalls: Array<Record<string, any>> = [];
    Object.assign(first.context, {
      fetch: async (_path: string, options: Record<string, any>) => {
        firstCalls.push(options);
        return { ok: true, status: 200, json: async () => ({
          reviewToken: "safe-review-token",
          clientInstanceId: operation.clientInstanceId,
          clientClaim: "creative_claim_rotated_0002",
        }) };
      },
    });
    Object.assign(copied.context, {
      fetch: async (_path: string, options: Record<string, any>) => {
        copiedCalls.push(options);
        return { ok: true, status: 200, json: async () => ({
          reviewToken: "safe-review-token",
          clientInstanceId: operation.clientInstanceId,
          clientClaim: "creative_claim_should_not_be_issued",
        }) };
      },
    });

    await runInContext(`reviewSession = null; api("/api/session")`, first.context);
    const copiedError = await runInContext(
      `reviewSession = null; api("/api/session").catch((error) => error.message)`,
      copied.context,
    );

    expect(firstCalls[0]?.headers).toMatchObject({
      "x-client-instance": operation.clientInstanceId,
      "x-client-claim": "creative_claim_original_0001",
    });
    expect(copiedCalls).toEqual([]);
    expect(copiedError).toBe("creative-tab-owner-active");
    expect(runInContext("creativePendingStorageIssue", copied.context))
      .toBe("creative-tab-owner-active");

    runInContext("releaseCreativeTabLease()", first.context);
    await Promise.resolve();
    await Promise.resolve();
    const reloadStorage = memoryStorage();
    reloadStorage.setItem(
      "wearable.creative.pending-operations.v1",
      firstStorage.getItem("wearable.creative.pending-operations.v1")!,
    );
    reloadStorage.setItem(
      "wearable.creative.client-owner.v1",
      firstStorage.getItem("wearable.creative.client-owner.v1")!,
    );
    const reload = await loadCreativeUiContext(reloadStorage);
    Object.assign(reload.context, { navigator: { locks } });
    const reloadCalls: Array<Record<string, any>> = [];
    Object.assign(reload.context, {
      fetch: async (_path: string, options: Record<string, any>) => {
        reloadCalls.push(options);
        return { ok: true, status: 200, json: async () => ({
          reviewToken: "safe-review-token",
          clientInstanceId: operation.clientInstanceId,
          clientClaim: "creative_claim_rotated_0003",
        }) };
      },
    });
    await runInContext(`reviewSession = null; api("/api/session")`, reload.context);
    expect(reloadCalls[0]?.headers["x-client-claim"]).toBe("creative_claim_rotated_0002");
  });

  it("keeps ambiguous keys and blocks mutations when the service lost the old owner on restart", async () => {
    const operation = pendingOperationRecord();
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    storage.setItem("wearable.creative.client-owner.v1", JSON.stringify({
      clientInstanceId: operation.clientInstanceId,
      clientClaim: "creative_claim_before_restart_0001",
    }));
    const { context, workspace } = await loadCreativeUiContext(storage);
    Object.assign(context, { navigator: { locks: memoryWebLocks() } });
    const calls: string[] = [];
    Object.assign(context, {
      fetch: async (path: string) => {
        calls.push(path);
        return { ok: false, status: 409, json: async () => ({
          error: "creative-client-recovery-required",
          recovery: { reason: "server-owner-state-unavailable", automaticRetryAllowed: false },
        }) };
      },
    });

    const error = await runInContext(
      `reviewSession = null; api("/api/session").catch((failure) => failure.message)`,
      context,
    );
    await runInContext("loadView()", context);

    expect(error).toBe("creative-client-recovery-required");
    expect(calls).toEqual(["/api/session", "/api/session"]);
    expect(workspace.innerHTML).toContain("服务已重启，待处理操作无法自动确认");
    expect(workspace.innerHTML).toContain("data-reset-pending-operations");
    expect(storage.getItem("wearable.creative.pending-operations.v1"))
      .toContain(operation.operationKey);
    expect(runInContext(
      `(() => { try { pendingCreativeOperation("/api/creative-sessions", {}); return "sent" }
        catch (failure) { return failure.message } })()`,
      context,
    )).toBe("creative-client-recovery-required");
    expect(runInContext("resetCreativePendingOperationsFromUi()", context)).toBe(true);
    expect(storage.getItem("wearable.creative.pending-operations.v1")).toBeNull();
    expect(storage.getItem("wearable.creative.client-owner.v1")).toBeNull();
  });

  it("treats a missing receipt as manual-review-only and never offers same-key retry", async () => {
    const operation = pendingOperationRecord({ operationKey: "missing-receipt-operation-0001" });
    const storage = memoryStorage();
    storage.setItem("wearable.creative.pending-operations.v1", JSON.stringify([operation]));
    const { context, workspace } = await loadCreativeUiContext(storage);
    const calls: string[] = [];
    Object.assign(context, { testCalls: calls });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token",
        clientInstanceId: "client_instance_ui_0001", clientClaim: "creative_claim_ui_0001" };
      api = async (path) => {
        testCalls.push(path);
        const error = new Error("creative-operation-receipt-not-found");
        error.status = 404;
        throw error;
      };
      recoverPendingCreativeOperations().then(() =>
        renderCreativeSession({ id: "session_storage", stage: "editorial_review" }))`, context);

    expect(calls).toEqual([
      "/api/creative-operation-receipts/missing-receipt-operation-0001",
    ]);
    expect(workspace.innerHTML).toContain("服务端已无此回执，请人工核对后重置");
    expect(workspace.innerHTML).not.toContain("data-retry-pending-operation");
    expect(runInContext("creativePendingStorageIssue", context))
      .toBe("creative-pending-receipt-missing");
  });

  it("times out a mutation detail reload and retries one clean reconciliation GET", async () => {
    const { context } = await loadCreativeUiContext();
    const button = { ...fakeElement(), disabled: false, textContent: "确认" };
    const feedback = { textContent: "" };
    const calls: string[] = [];
    Object.assign(context, { testButton: button, testFeedback: feedback, testCalls: calls });

    await runInContext(`creativeRequestTimeoutMs = 5;
      let detailCount = 0;
      api = async (path, options) => {
        testCalls.push((options?.method || "GET") + " " + path);
        if (options?.method === "POST") return {};
        detailCount += 1;
        if (detailCount === 1) return new Promise(() => {});
        return { session: { id: "session_detail_timeout", stage: "materials_declined" } };
      };
      mutateCreativeSession({ sessionId: "session_detail_timeout", button: testButton, feedback: testFeedback,
        busyText: "正在确认…", path: "/mutation", body: {} })`, context);

    expect(calls).toEqual([
      "POST /mutation",
      "GET /api/creative-sessions/session_detail_timeout",
      "GET /api/creative-sessions/session_detail_timeout",
      "DELETE /api/creative-operation-receipts/operation_01",
    ]);
    expect(button.disabled).toBe(false);
    expect(feedback.textContent).toBe("请求超时，已自动对账；可安全重试，不会重复执行");
  });

  it("loads material preview bytes with the review token and revokes object URLs", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const calls: Array<{ path: string; options: Record<string, unknown> }> = [];
    const revoked: string[] = [];
    const media = {
      ...fakeElement(),
      dataset: { previewUrl: "/api/creative-sessions/session_01/material-suggestions/segment_01/preview" },
      isConnected: true,
      src: "",
    };
    workspace.querySelectorAll = (selector?: string) => selector === "[data-material-preview]" ? [media] : [];
    Object.assign(context, { testCalls: calls, testRevoked: revoked, testMedia: media });

    await runInContext(`reviewSession = { reviewToken: "safe-review-token" };
      fetch = async (path, options) => {
        testCalls.push({ path, options });
        return { ok: true, blob: async () => ({ synthetic: true }) };
      };
      URL = {
        createObjectURL: () => "blob:preview-01",
        revokeObjectURL: (value) => testRevoked.push(value)
      };
      bindMaterialPreviewFallbacks()`, context);

    expect(calls).toHaveLength(1);
    expect(calls[0]?.path).toContain("/preview");
    expect(calls[0]?.options.headers).toEqual({ "x-review-token": "safe-review-token" });
    expect(media.src).toBe("blob:preview-01");
    expect(JSON.stringify(calls)).not.toContain("?token=");

    runInContext(`renderCreativeSession({ id: "session_01", stage: "materials_declined" })`, context);
    expect(revoked).toEqual(["blob:preview-01"]);
  });

  it("aborts a slow material preview before rerender and never creates a stale object URL", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    let resolvePreview!: (value: unknown) => void;
    const preview = new Promise((resolve) => { resolvePreview = resolve; });
    const media = {
      ...fakeElement(),
      dataset: { previewUrl: "/api/creative-sessions/session_01/material-suggestions/segment_01/preview" },
      isConnected: true,
      src: "",
    };
    const signals: Array<{ aborted: boolean }> = [];
    const created: string[] = [];
    class TrackingAbortController {
      signal = { aborted: false };
      abort() { this.signal.aborted = true; }
    }
    workspace.querySelectorAll = (selector?: string) => selector === "[data-material-preview]" ? [media] : [];
    Object.assign(context, { testPreview: preview, testSignals: signals, testCreated: created,
      AbortController: TrackingAbortController });

    const pending = runInContext(`reviewSession = { reviewToken: "safe-review-token" };
      fetch = async (_path, options) => { testSignals.push(options.signal); return testPreview };
      URL = {
        createObjectURL: () => { testCreated.push("blob:stale"); return "blob:stale" },
        revokeObjectURL() {}
      };
      bindMaterialPreviewFallbacks()`, context);
    await Promise.resolve();
    expect(signals).toHaveLength(1);

    workspace.querySelectorAll = () => [];
    runInContext(`renderCreativeSession({ id: "session_01", stage: "materials_declined" })`, context);
    expect(signals[0]?.aborted).toBe(true);
    resolvePreview({ ok: true, blob: async () => ({ synthetic: true }) });
    await pending;

    expect(created).toEqual([]);
    expect(media.src).toBe("");
  });

  it("uses friendly Chinese copy for creative GET failures", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    await runInContext(`api = async () => { throw new Error("script-version-not-current") };
      activeView = "creative";
      loadView()`, context);
    expect(workspace.innerHTML).toContain("最新脚本");
    expect(workspace.innerHTML).not.toContain("script-version-not-current");

    await runInContext(`api = async () => { throw new Error("opaque-load-code") };
      loadView()`, context);
    expect(workspace.innerHTML).toContain("操作未完成");
    expect(workspace.innerHTML).toContain("错误代码：opaque-load-code");

    await runInContext(`creativeRequestTimeoutMs = 5;
      api = async (path) => path === "/api/creative-sessions"
        ? { sessions: [{ id: "session_slow", updatedAt: "2026-09-05T00:00:00.000Z" }] }
        : new Promise(() => {});
      loadView()`, context);
    expect(workspace.innerHTML).toContain("请求超时，请重试或刷新查看最新状态");
    expect(workspace.innerHTML).not.toContain("creative-request-timeout");
  });

  it("keys material selection to the creative session and suggestion set", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    const view = (session: string, set: string) => `({ id: "${session}", stage: "reviewing_materials",
      suggestionSet: { id: "${set}", suggestions: [{ segmentId: "same_segment",
        capturedAt: "2026-09-01T08:00:00.000Z", reason: "理由", source: "本地素材库",
        permissionState: "allowed", technicallyAvailable: true }] } })`;

    runInContext(`currentCreativeView = ${view("session_a", "set_a")}; renderCreativeSession(currentCreativeView);
      toggleMaterialSelection("same_segment")`, context);
    expect(workspace.innerHTML).toContain("已加入");
    runInContext(`currentCreativeView = ${view("session_b", "set_b")}; renderCreativeSession(currentCreativeView)`, context);
    expect(workspace.innerHTML).not.toContain("已加入");
    expect(workspace.innerHTML).toContain("data-selection-count>0<");
  });

  it("moves focus to the newly rendered stage heading", async () => {
    const { context, workspace } = await loadCreativeUiContext();
    let focused = false;
    const heading = { ...fakeElement(), focus: () => { focused = true; } };
    workspace.querySelector = (selector?: string) => selector === "[data-stage-heading]" ? heading : fakeElement();

    runInContext(`renderCreativeSession({ id: "session_01", stage: "awaiting_material_consent",
      title: "绿松石返工", materialSuggestion: { id: "set_01", count: 1 } })`, context);

    expect(focused).toBe(true);
    expect(workspace.innerHTML).toContain('data-stage-heading');
  });

  it("asks for a retake reason and sends it to the field event API", async () => {
    const { context } = await loadCreativeUiContext();
    const card = { ...fakeElement(), dataset: { step: "step_01" } };
    const button = { ...fakeElement(), dataset: { fieldAction: "retake" }, closest: () => card };
    Object.assign(context, { testButton: button });

    const result = await runInContext(`window.prompt = () => "光线变了";
      let capturedConfig;
      mutateCreativeSession = async (config) => { capturedConfig = config; return {} };
      recordFieldActionFromUi(testButton, { id: "session_01", currentBriefVersion: 1,
        currentScriptVersion: 2, currentShotPlanVersion: 3 }).then(() => capturedConfig.body)`, context);

    expect(result.reason).toBe("光线变了");
  });

  it("maps business errors to clear Chinese and keeps unknown codes secondary", async () => {
    const { context } = await loadCreativeUiContext();
    const known = runInContext(`friendlyCreativeError("script-version-not-current")`, context);
    const unknown = runInContext(`friendlyCreativeError("opaque-internal-code")`, context);

    expect(known).toContain("最新脚本");
    expect(unknown).toContain("操作未完成");
    expect(unknown).toContain("opaque-internal-code");
    expect(unknown).not.toBe("opaque-internal-code");
  });

  it("reads creative intent values from named form controls", async () => {
    const { context } = await loadCreativeUiContext();
    const submitted: Array<Record<string, unknown>> = [];
    const feedback = { textContent: "" };
    const button = { disabled: false, textContent: "提交" };
    const controls = {
      title: { value: "绿松石返工故事" },
      text: { value: "拍一条返工三次的视频号内容" },
    };
    const form = {
      title: "DOM built-in title",
      elements: { namedItem: (name: "title" | "text") => controls[name] },
      querySelector: (selector: string) => selector === ".inline-feedback" ? feedback : button,
    };
    Object.assign(context, { testForm: form, testSubmitted: submitted });

    await runInContext(`api = async (_path, options) => {
        testSubmitted.push(JSON.parse(options.body));
        throw new Error("stop-after-capture");
      };
      submitCreativeIntent({ preventDefault() {}, currentTarget: testForm })`, context);

    expect(submitted[0]).toEqual({
      customerId: "designer_01",
      title: "绿松石返工故事",
      text: "拍一条返工三次的视频号内容",
    });
  });

  it("labels device capability as local simulation in the profile entry", async () => {
    const { context, workspace } = await loadCreativeUiContext();

    runInContext("renderProfile()", context);

    expect(workspace.innerHTML).toContain("三端均为本地模拟");
    expect(workspace.innerHTML).not.toContain("真机已连接");
  });

  it("clearly separates story lines from the four platform video versions", async () => {
    const { context } = await loadCreativeUiContext();
    const markup = runInContext(`renderCreativeReview({
      stories: [{ id: "story_01", type: "inner-journey" }],
      revisions: [{ id: "revision_01", storyId: "story_01", revision: 1 }],
      artifacts: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"].map((kind, index) => ({
        id: "artifact_" + index, revisionId: "revision_01", kind, mediaUrl: "/media/" + index
      })),
      approvals: [], exportPackages: [], deliveries: []
    }, "本次素材已处理")`, context) as string;

    expect(markup).toContain("1 条内容主线已拆好");
    expect(markup).toContain("原视频、小红书、抖音、视频号四个版本");
    expect(markup).toContain("4/4");
    expect(markup).not.toContain("4/3");
  });

  it("keeps touch targets and desktop layout narrow", async () => {
    const css = await readFile(join(process.cwd(), "packages/review-app/public/styles.css"), "utf8");

    expect(css).toContain("min-height: 44px");
    expect(css).toContain("env(safe-area-inset-bottom)");
    expect(css).toContain("max-width: 480px");
    expect(css).toContain("[hidden] { display: none !important; }");
    expect(css).toContain(".creative-action-dock {\n  position: fixed;");
    expect(css).toContain("#status.global-status");
    expect(css).not.toMatch(/#status\s*\{[\s\S]*?position:\s*absolute/);
  });
});

async function loadCreativeUiContext(sessionStorage = memoryStorage()) {
  const workspace = fakeElement();
  const elements = new Map<string, ReturnType<typeof fakeElement>>([
    ["#workspace", workspace],
    ["#status", fakeElement()],
    ["#batch-id", Object.assign(fakeElement(), { value: "batch_new" })],
    ["#pipeline", fakeElement()],
    ["#pending-count", fakeElement()],
    ["#view-title", fakeElement()],
    ["#view-description", fakeElement()],
    ["#today-label", fakeElement()],
    ["#refresh", fakeElement()],
    ["#creative-session-header", fakeElement()],
    ["#production-toolbar", fakeElement()],
    ["#pipeline-card", fakeElement()],
  ]);
  const navButtons = ["creative", "library", "review", "approved", "profile"].map((view) =>
    Object.assign(fakeElement(), { dataset: { view } }));
  elements.set('[data-view="creative"]', navButtons[0]);
  const document = {
    activeElement: null,
    querySelector: (selector: string) => elements.get(selector) ?? fakeElement(),
    querySelectorAll: (selector: string) => selector === "[data-view]" ? navButtons : [],
  };
  const context = createContext({
    document,
    window: { confirm: () => true },
    Intl,
    Date,
    sessionStorage,
    encodeURIComponent,
    crypto: { randomUUID: () => "operation_01" },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    setTimeout,
    clearTimeout,
  });
  const source = await readFile(join(process.cwd(), "packages/review-app/public/app.js"), "utf8");
  runInContext(source, context);
  runInContext(`reviewSession = { reviewToken: "safe-review-token",
    clientInstanceId: "client_instance_ui_0001" }`, context);
  return { context, workspace, elements, navButtons };
}

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem(key: string) { return values.get(key) ?? null; },
    setItem(key: string, value: string) { values.set(key, value); },
    removeItem(key: string) { values.delete(key); },
  };
}

function memoryWebLocks() {
  const held = new Set<string>();
  return {
    async request(
      name: string,
      options: { ifAvailable?: boolean },
      callback: (lock: { name: string } | null) => unknown,
    ) {
      if (options.ifAvailable && held.has(name)) return callback(null);
      held.add(name);
      try {
        return await callback({ name });
      } finally {
        held.delete(name);
      }
    },
  };
}

function pendingOperationRecord(input: {
  path?: string;
  body?: Record<string, unknown>;
  operationKey?: string;
  clientInstanceId?: string;
  sessionId?: string | null;
  state?: "ambiguous" | "reconciled";
} = {}) {
  const request = {
    method: "POST" as const,
    path: input.path ?? "/api/creative-sessions/session_storage/script-revisions",
    body: input.body ?? { prompt: "keep this operation" },
  };
  if (request.path.endsWith("/events")) {
    request.body = {
      ...request.body,
      operationId: input.operationKey ?? "pending-operation-valid-0001",
    };
  }
  const logicalRequest = request.path.endsWith("/events")
    ? {
        ...request,
        body: Object.fromEntries(Object.entries(request.body).filter(([key]) => key !== "operationId")),
      }
    : request;
  return {
    identity: canonicalTestValue(logicalRequest),
    operationKey: input.operationKey ?? "pending-operation-valid-0001",
    clientInstanceId: input.clientInstanceId ?? "client_instance_ui_0001",
    createdAt: Date.now() - 1_000,
    state: input.state ?? "ambiguous",
    sessionId: input.sessionId === undefined ? "session_storage" : input.sessionId,
    request,
  };
}

function canonicalTestValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalTestValue).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalTestValue(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function fakeElement(): any {
  const classes = new Set<string>();
  return {
    textContent: "",
    innerHTML: "",
    value: "",
    hidden: false,
    dataset: {} as Record<string, string>,
    classList: {
      add(...values: string[]) { values.forEach((value) => classes.add(value)); },
      remove(...values: string[]) { values.forEach((value) => classes.delete(value)); },
      toggle(value: string, force?: boolean) {
        if (force === true || (force === undefined && !classes.has(value))) classes.add(value);
        else classes.delete(value);
      },
      contains(value: string) { return classes.has(value); },
    },
    addEventListener() {},
    click() {},
    querySelectorAll: () => [],
    querySelector: (_selector?: string) => fakeElement(),
    closest: () => fakeElement(),
    focus() {},
    setAttribute() {},
  };
}
