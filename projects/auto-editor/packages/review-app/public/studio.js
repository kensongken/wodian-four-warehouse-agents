let studioState = emptyStudioState("");
let studioRequestEpoch = 0;
let activeStudioRequestController = null;
let pendingStudioDemandEpoch = null;
let studioSessionQueue = Promise.resolve();
let referenceRequestEpoch = 0;
let activeReferenceRequestController = null;
const referenceOperationKeyCache = new Map();
let referenceLibraryRefreshSequence = 0;
let referenceHistoryRefreshSequence = 0;
let referenceLibraryRefreshController = null;
let referenceHistoryRefreshController = null;
let studioActiveTab = "create";
let studioLibraryMode = "materials";
let studioMaterialFilter = "all";
let studioCaptionFeedbackFilter = "all";
let studioFocusedDraftId = null;
let hardwarePairingPoll = null;
let externalEditCandidatePoll = null;
let externalEditCandidatePollDraftId = null;
let externalEditCandidatePollAttempts = 0;
let externalEditCandidatePollBusy = false;
const HIGHLIGHT_DRAFT_PREFIX = "wearable-studio-highlight-draft-v1";

const studioMaterialCategories = [
  { value: "all", label: "全部" },
  { value: "single-product", label: "产品" },
  { value: "jewelry-making-process", label: "制作过程" },
  { value: "designer-viewpoint", label: "人物／口播" },
];

function emptyReferenceState(scopeEpoch = 0) {
  return {
    scopeEpoch,
    samples: [], historyAssets: [], sample: null, rights: null, analysis: null,
    selectedRuleIds: [], selection: null, mappingPlan: null, drafts: [],
    executionReport: null, draftControls: {}, busyAction: null, error: null,
  };
}

function emptyStudioState(customerId) {
  return {
    customerId, release: null, session: null, inventory: null, materials: [],
    materialTags: { version: 0, tags: [], assignments: [] },
    materialUploadReport: null,
    hardwarePairing: null, hardwarePairingError: null,
    demand: null, matchAssessment: null, opportunities: [], gaps: [], gapCapability: null,
    brief: null, drafts: [], customerFeedback: null, revisionProposal: null, history: null,
    analysisHistory: { entries: [], reviews: [], duplicateGroups: [] }, modelStatus: null,
    selectedFactIds: [], feedbackDraftId: null, feedback: null, busy: false,
    externalEdits: {}, externalEditOperations: {},
    music: { tracks: [], selectedTrackId: null, recommendations: [] },
    highlightEvaluations: [],
    highlightRetestAttempts: [],
    highlightCoverageProfiles: [],
    highlightCandidateRejections: [],
    highlightBatch: null,
    captionFeedbackDataset: null,
    captionFeedbackReviewDraftId: null,
    reference: emptyReferenceState(),
  };
}

function studioAppView(state, requestedTab = "create") {
  const tab = ["create", "works", "library"].includes(requestedTab) ? requestedTab : "create";
  if (tab === "library") return { tab, step: "materials" };
  if (tab === "works") {
    const needsRevisionAction = state.revisionProposal?.status === "proposed" ||
      (state.revisionProposal?.status === "confirmed" && state.drafts.length === 0);
    return { tab, step: needsRevisionAction ? "revision" : "drafts" };
  }
  // "创作"永远是新建入口；旧初稿只放在"作品"，不在首页重复铺开。
  if (state.matchAssessment?.verdict === "ready" && state.brief &&
      state.revisionProposal?.status !== "proposed") {
    return { tab, step: "demand" };
  }
  if (!state.demand) return { tab, step: "demand" };
  if (!state.matchAssessment || state.matchAssessment.verdict !== "ready") return { tab, step: "match" };
  if (!state.brief) return { tab, step: "brief" };
  if (state.revisionProposal?.status === "proposed" ||
      (state.revisionProposal?.status === "confirmed" && state.drafts.length === 0)) {
    return { tab, step: "revision" };
  }
  return { tab, step: "drafts" };
}

function setStudioAppTab(tab) {
  studioActiveTab = ["create", "works", "library"].includes(tab) ? tab : "create";
  if (studioActiveTab === "library") studioLibraryMode = "materials";
  renderStudioAppChrome();
  if (typeof window?.scrollTo === "function") window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderStudioAppChrome() {
  const shell = document.querySelector(".studio-shell");
  const view = studioAppView(studioState, studioActiveTab);
  const phase = view.tab === "works" || view.step === "drafts" || view.step === "revision"
    ? "review"
    : view.step === "brief"
      ? "confirm"
      : "start";
  if (shell?.dataset) {
    shell.dataset.activeTab = view.tab;
    shell.dataset.currentStep = view.step;
    shell.dataset.libraryMode = studioLibraryMode;
    shell.dataset.hasAnalysis = String((studioState.analysisHistory?.entries || []).length > 0);
    shell.dataset.hasDrafts = String(studioState.drafts.length > 0);
    shell.dataset.currentPhase = phase;
  }
  if (typeof document.querySelectorAll !== "function") return;
  document.querySelectorAll("[data-studio-tab]").forEach((control) => {
    const selected = control.dataset.studioTab === view.tab;
    control.setAttribute("aria-selected", String(selected));
  });
  document.querySelectorAll("[data-journey-phase]").forEach((item) => {
    if (item.dataset.journeyPhase === phase) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
}

function inferQuickContentType(prompt) {
  if (/(\u5236\u4f5c|\u5de5\u827a|\u6253\u78e8|\u9576\u5d4c|\u8fc7\u7a0b)/u.test(prompt)) return "jewelry-making-process";
  if (/(\u8bbe\u8ba1\u5e08|\u89c2\u70b9|\u7075\u611f|\u5fc3\u8def|\u6545\u4e8b|\u53e3\u64ad)/u.test(prompt)) return "designer-viewpoint";
  return "single-product";
}

async function submitQuickStudioPrompt(prompt) {
  const goal = String(prompt || "").trim();
  if (!goal) return studioState;
  if (studioState.inventory && studioState.inventory.items.length === 0) {
    setStudioAppTab("library");
    setStudioBusy(false, "先上传一段视频，再回来填写想法。你的想法会留在输入框里。");
    return studioState;
  }
  await submitStudioDemand({
    contentType: inferQuickContentType(goal),
    goal,
    targetAudience: "待 Agent 继续确认",
    desiredEffect: "形成可审的内容初稿",
    mustInclude: [],
    mustAvoid: [],
    submittedBy: "operator-on-behalf",
  });
  if (studioState.demand && studioState.inventory && !studioState.busy) {
    await assessStudioDemand();
  }
  return studioState;
}

function clearStudioScope(customerId) {
  stopHardwarePairingPoll();
  stopExternalEditCandidatePoll();
  activeReferenceRequestController?.abort();
  referenceLibraryRefreshController?.abort();
  referenceHistoryRefreshController?.abort();
  referenceRequestEpoch += 1;
  referenceLibraryRefreshSequence += 1;
  referenceHistoryRefreshSequence += 1;
  referenceOperationKeyCache.clear();
  studioMaterialFilter = "all";
  studioCaptionFeedbackFilter = "all";
  const nextReferenceEpoch = Number(studioState.reference?.scopeEpoch || 0) + 1;
  studioState = {
    ...emptyStudioState(customerId),
    reference: emptyReferenceState(nextReferenceEpoch),
  };
  renderStudioState();
}

function stopHardwarePairingPoll() {
  if (hardwarePairingPoll !== null) clearInterval(hardwarePairingPoll);
  hardwarePairingPoll = null;
}

function stopExternalEditCandidatePoll() {
  const activeFlow = externalEditCandidatePollDraftId
    ? studioState.externalEdits?.[externalEditCandidatePollDraftId]
    : null;
  if (activeFlow) activeFlow.autoReturnWatch = false;
  if (externalEditCandidatePoll !== null) clearInterval(externalEditCandidatePoll);
  externalEditCandidatePoll = null;
  externalEditCandidatePollDraftId = null;
  externalEditCandidatePollAttempts = 0;
  externalEditCandidatePollBusy = false;
}

function startExternalEditCandidatePoll(draftId) {
  stopExternalEditCandidatePoll();
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff) return false;
  externalEditCandidatePollDraftId = draftId;
  externalEditCandidatePollAttempts = 0;
  flow.autoReturnWatch = true;
  externalEditCandidatePoll = setInterval(() => { void pollExternalEditCandidates(draftId); }, 5_000);
  return true;
}

async function pollExternalEditCandidates(draftId) {
  if (externalEditCandidatePollBusy || externalEditCandidatePollDraftId !== draftId) return studioState;
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff || ["returned", "failed", "cancelled"].includes(flow.handoff.status)) {
    stopExternalEditCandidatePoll();
    if (flow) flow.autoReturnWatch = false;
    return studioState;
  }
  externalEditCandidatePollBusy = true;
  externalEditCandidatePollAttempts += 1;
  try {
    const result = await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/candidates`);
    flow.candidates = result.candidates || [];
    if (flow.candidates.some((candidate) => candidate.status === "pending-review")) {
      flow.handoff.status = "candidate-found";
      flow.autoReturnWatch = false;
      stopExternalEditCandidatePoll();
      renderStudioState();
    } else if (externalEditCandidatePollAttempts >= 120) {
      flow.autoReturnWatch = false;
      stopExternalEditCandidatePoll();
      renderStudioState();
    }
  } catch {
    // Keep the manual check available and stop quiet background retries after
    // one read failure instead of producing repeated requests or UI noise.
    flow.autoReturnWatch = false;
    stopExternalEditCandidatePoll();
    renderStudioState();
  } finally {
    externalEditCandidatePollBusy = false;
  }
  return studioState;
}

function clearStudioDemandDownstream() {
  studioState.demand = null;
  studioState.matchAssessment = null;
  studioState.opportunities = [];
  studioState.gaps = [];
  studioState.gapCapability = null;
  studioState.brief = null;
  studioState.drafts = [];
  studioState.customerFeedback = null;
  studioState.revisionProposal = null;
  studioState.feedbackDraftId = null;
  studioState.feedback = null;
  studioFocusedDraftId = null;
  invalidateStudioCaptionFeedbackDataset();
  renderStudioState();
}

function invalidateStudioCaptionFeedbackDataset() {
  studioState.captionFeedbackDataset = null;
  studioState.captionFeedbackReviewDraftId = null;
  studioCaptionFeedbackFilter = "all";
}

function latestStudioRecord(values, predicate = () => true) {
  return [...(values || [])]
    .filter(predicate)
    .sort((left, right) => {
      const version = Number(left?.version || 0) - Number(right?.version || 0);
      if (version !== 0) return version;
      return String(left?.createdAt || "").localeCompare(String(right?.createdAt || ""));
    })
    .at(-1) || null;
}

function restoreStudioWorkflowFromHistory(history) {
  const safeHistory = history || {
    demands: [], assessments: [], opportunities: [], briefs: [], drafts: [],
    customerFeedback: [], revisionProposals: [],
  };
  const demand = latestStudioRecord(
    safeHistory.demands,
    (item) => item?.status === "current",
  ) || latestStudioRecord(safeHistory.demands);
  const assessment = demand
    ? latestStudioRecord(safeHistory.assessments, (item) => item?.demandId === demand.id)
    : null;
  const opportunities = demand
    ? (safeHistory.opportunities || []).filter((item) =>
      item?.demandId === demand.id && (!assessment || item?.matchAssessmentId === assessment.id))
    : [];
  const brief = demand
    ? latestStudioRecord(safeHistory.briefs, (item) => item?.demandId === demand.id)
    : null;
  const drafts = brief
    ? (safeHistory.drafts || []).filter((item) =>
      item?.briefId === brief.id && Number(item?.version || 0) === Number(brief.version || 0))
    : [];
  const draftIds = new Set(drafts.map((item) => item.id));
  const customerFeedback = latestStudioRecord(
    safeHistory.customerFeedback,
    (item) => draftIds.has(item?.draftId),
  );
  const revisionProposal = customerFeedback
    ? latestStudioRecord(
      safeHistory.revisionProposals,
      (item) => item?.feedbackId === customerFeedback.id,
    )
    : null;
  studioState.history = safeHistory;
  studioState.demand = demand;
  studioState.matchAssessment = assessment;
  studioState.opportunities = opportunities;
  studioState.brief = brief;
  studioState.drafts = drafts;
  studioState.customerFeedback = customerFeedback;
  studioState.feedbackDraftId = customerFeedback?.draftId || null;
  studioState.revisionProposal = revisionProposal;
  return studioState;
}

function beginStudioDemandEdit() {
  const hasDemandToEdit = Boolean(studioState.demand);
  const hasPendingDemandToInterrupt = pendingStudioDemandEpoch !== null;
  if (!hasDemandToEdit && !hasPendingDemandToInterrupt) {
    return false;
  }
  beginStudioWorkflow();
  studioState.busy = false;
  clearStudioDemandDownstream();
  const status = document.querySelector("#studio-status");
  if (status) status.textContent = "需求已编辑，当前下游结果已清空，历史版本仍保留";
  return true;
}

async function switchStudioCustomer(customerId) {
  const workflow = beginStudioWorkflow(customerId, null);
  clearStudioScope(customerId);
  setStudioBusy(true, "正在准备演示内容，第一次打开请稍等；不用上传真实素材。");
  try {
    const opened = await openStudioSessionSerially(workflow);
    if (!opened || !ownsStudioWorkflow(workflow)) return studioState;
    if (!opened.session || opened.session.customerId !== customerId || !opened.session.token) {
      throw new Error("studio-session-scope-mismatch");
    }
    studioState.session = opened.session;
    studioState.release = opened.release || null;
    workflow.sessionToken = opened.session.token;
    const result = await studioApi("/api/studio/inventory", {
      method: "POST", body: {}, signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.inventory = result.inventory;
    studioState.materials = result.materials;
    studioState.materialTags = result.materialTags || { version: 0, tags: [], assignments: [] };
    await refreshStudioHighlights(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    const historyResult = await studioApi("/api/studio/history", {
      method: "POST", body: {}, signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    restoreStudioWorkflowFromHistory(historyResult.history);
    await refreshStudioHighlights(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioMusicForCurrentBrief(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshReferenceLibrary();
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshReferenceHistory();
    if (!ownsStudioWorkflow(workflow)) return studioState;
    const restoredLabel = opened.resumed && studioState.drafts.length > 0
      ? `已恢复 ${studioState.materials.length} 段素材和 ${studioState.drafts.length} 个初稿`
      : opened.resumed && studioState.demand
        ? `已恢复上次的创作进度，共 ${studioState.materials.length} 段素材`
        : opened.resumed
          ? `已恢复 ${studioState.materials.length} 段素材，可以继续创作`
        : `已准备 ${result.inventory.items.length} 段演示素材，可以开始体验`;
    setStudioBusy(false, restoredLabel);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitStudioDemand(input) {
  if (!studioState.session) return studioState;
  const workflow = beginStudioWorkflow();
  pendingStudioDemandEpoch = workflow.epoch;
  clearStudioDemandDownstream();
  setStudioBusy(true, "正在记录这次的内容需求…");
  try {
    const result = await studioApi("/api/studio/demands", {
      method: "POST",
      body: {
        contentType: input.contentType,
        goal: String(input.goal || "").trim(),
        targetAudience: String(input.targetAudience || "").trim(),
        desiredEffect: String(input.desiredEffect || "").trim(),
        mustInclude: splitLines(input.mustInclude),
        mustAvoid: splitLines(input.mustAvoid),
        submittedBy: input.submittedBy,
        operationKey: operationKey("demand"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.demand = result.demand;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, "这次需求已记录，请检查现有素材是否匹配");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  } finally {
    if (pendingStudioDemandEpoch === workflow.epoch) pendingStudioDemandEpoch = null;
  }
  renderStudioState();
  return studioState;
}

async function assessStudioDemand() {
  if (!studioState.session || !studioState.demand || !studioState.inventory || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  const demandId = studioState.demand.id;
  studioState.matchAssessment = null;
  studioState.opportunities = [];
  studioState.gaps = [];
  studioState.gapCapability = null;
  studioState.brief = null;
  studioState.drafts = [];
  studioState.customerFeedback = null;
  studioState.revisionProposal = null;
  studioState.feedbackDraftId = null;
  setStudioBusy(true, "正在对照这次需求检查现有证据…");
  try {
    const matched = await studioApi("/api/studio/matches", {
      method: "POST",
      body: { demandId, operationKey: operationKey("match") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow) || studioState.demand?.id !== demandId) return studioState;
    studioState.matchAssessment = matched.assessment;
    if (matched.assessment.verdict === "ready") {
      const result = await studioApi("/api/studio/opportunities", {
        method: "POST",
        body: { demandId, matchAssessmentId: matched.assessment.id },
        signal: workflow.controller.signal,
      });
      if (!ownsStudioWorkflow(workflow) || studioState.demand?.id !== demandId) return studioState;
      if (result.inventory) studioState.inventory = result.inventory;
      if (result.materials) studioState.materials = result.materials;
      studioState.opportunities = result.opportunities || [];
      studioState.gaps = result.gaps || [];
      studioState.gapCapability = result.gapList?.capability || null;
    }
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow) || studioState.demand?.id !== demandId) return studioState;
    const hasVisualMismatch = (matched.assessment.unmetNeeds || []).some((item) =>
      String(item).includes("画面内容与商品介绍目标不一致"));
    const message = hasVisualMismatch
      ? "画面内容与商品目标不一致，请改成风景需求或更换商品素材"
      : matched.assessment.verdict === "ready"
      ? "现有素材适合继续，请核对任务书"
      : matched.assessment.verdict === "partial"
        ? "现有素材部分匹配，请查看缺口和历史素材建议"
        : "现有素材暂不适合，不会进入任务书";
    setStudioBusy(false, message);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

// Kept for the earlier local journey while the visible workbench uses demand-bound matching.
async function findStudioOpportunity() {
  if (!studioState.session || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在根据来源和权限找可讲的故事…");
  try {
    const body = studioState.demand && studioState.matchAssessment
      ? { demandId: studioState.demand.id, matchAssessmentId: studioState.matchAssessment.id }
      : { contentType: "single-product", goal: "用旧素材讲清楚纯色合成商品夹具" };
    const result = await studioApi("/api/studio/opportunities", {
      method: "POST", body, signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    if (result.inventory) studioState.inventory = result.inventory;
    if (result.materials) studioState.materials = result.materials;
    studioState.opportunities = result.opportunities || [];
    studioState.gaps = result.gaps || [];
    studioState.gapCapability = result.gapList?.capability || null;
    setStudioBusy(false, studioState.opportunities.length
      ? "找到 1 个可讲的故事，请确认任务书"
      : "现有证据还不足，请展开查看缺口");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function confirmStudioBrief() {
  const opportunity = studioState.opportunities[0];
  const matchAssessment = studioState.matchAssessment;
  if (!opportunity || !studioState.inventory || studioState.busy) return studioState;
  if (matchAssessment && matchAssessment.verdict !== "ready") return studioState;
  const stagedMusic = studioState.music?.tracks?.find(
    (track) => track.assetId === studioState.music.selectedTrackId,
  ) || null;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在记录这份不可覆盖的任务书…");
  try {
    const plannedCardIds = Array.isArray(opportunity.materialCardIds)
      ? opportunity.materialCardIds
      : [];
    const inventoryByCardId = new Map(studioState.inventory.items
      .map((item) => [item.materialCardId, item]));
    const selectedAssetIds = plannedCardIds.length
      ? plannedCardIds.flatMap((cardId) => {
        const item = inventoryByCardId.get(cardId);
        return item ? [item.sourceAssetId] : [];
      })
      : studioState.inventory.items.map((item) => item.sourceAssetId);
    const body = {
      opportunityId: opportunity.id,
      selectedAssetIds,
      confirmedFactIds: [...studioState.selectedFactIds],
      reviewRequirements: ["采用前人工复核"],
      operationKey: operationKey("brief"),
    };
    if (matchAssessment) body.matchAssessmentId = matchAssessment.id;
    const result = await studioApi("/api/studio/briefs", {
      method: "POST", body, signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.brief = result.brief;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await bindStagedStudioMusicToBrief(stagedMusic, result.brief.id, workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioMusicForCurrentBrief(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
  setStudioBusy(false, "确认已记录，可以生成四个平台初稿");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function bindStagedStudioMusicToBrief(track, briefId, workflow = null) {
  if (!track || !briefId) return studioState;
  const signal = workflow?.controller?.signal;
  await studioApi("/api/studio/music/select", {
    method: "POST", body: { briefId, trackId: track.assetId }, signal,
  });
  if (workflow && !ownsStudioWorkflow(workflow)) return studioState;
  const mix = track.mixPreference || { trackStartMs: 0, originalGainDb: 0, musicGainDb: -16 };
  await studioApi("/api/studio/music/mix", {
    method: "POST",
    body: {
      briefId,
      trackId: track.assetId,
      trackStartMs: mix.trackStartMs,
      originalGainDb: mix.originalGainDb,
      musicGainDb: mix.musicGainDb,
    },
    signal,
  });
  return studioState;
}

async function confirmBriefAndCreateDrafts() {
  await confirmStudioBrief();
  if (studioState.brief && !studioState.busy && studioState.drafts.length === 0) {
    await createStudioDrafts();
  }
  return studioState;
}

async function createStudioDrafts() {
  if (!studioState.brief || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  const briefId = studioState.brief.id;
  setStudioBusy(true, "正在本机剪出母版、小红书、抖音和视频号四个平台版本…");
  try {
    const result = await studioApi("/api/studio/drafts", {
      method: "POST",
      body: { briefId, operationKey: operationKey("drafts") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow) || studioState.brief?.id !== briefId) return studioState;
    studioState.drafts = result.drafts;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow) || studioState.brief?.id !== briefId) return studioState;
    studioActiveTab = "works";
    setStudioBusy(false, `${studioState.drafts.length} 个平台版本已准备好，请播放后再做决定`);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitCaptionRevision(form) {
  if (studioState.busy) return studioState;
  const draftId = form.dataset.captionEditor;
  const captionTexts = [...form.querySelectorAll("[data-caption-cue-id]")].map((input) => ({
    id: input.dataset.captionCueId,
    text: String(input.value || "").trim(),
  }));
  if (!draftId || captionTexts.some((item) => !item.text)) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在重新烧录字幕并进行成片质检…");
  try {
    const result = await studioApi(`/api/studio/drafts/${encodeURIComponent(draftId)}/captions`, {
      method: "POST",
      body: {
        captionTexts,
        style: {
          template: formValue(form, "template"),
          fontSizePx: Number(formValue(form, "fontSizePx")),
          primaryColor: formValue(form, "primaryColor").toUpperCase(),
          outlineColor: formValue(form, "outlineColor").toUpperCase(),
          outlineWidthPx: Number(formValue(form, "outlineWidthPx")),
          bottomMarginPx: Number(formValue(form, "bottomMarginPx")),
          maxCharsPerLine: Number(formValue(form, "maxCharsPerLine")),
        },
        operationKey: operationKey("caption-revision"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.drafts = (studioState.history?.drafts || []).filter((draft) =>
      draft.briefId === result.draft.briefId && draft.version === result.draft.version);
    invalidateStudioCaptionFeedbackDataset();
    setStudioBusy(false, "字幕新版已生成；旧版仍保留，可以直接对比播放");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitCustomerDraftFeedback(input) {
  if (!studioState.session || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  const draftId = input.draftId;
  setStudioBusy(true, "正在记录客户对这一版的反馈…");
  try {
    const result = await studioApi("/api/studio/customer-feedback", {
      method: "POST",
      body: {
        draftId,
        satisfaction: input.satisfaction,
        issueCategories: [...new Set(input.issueCategories || [])],
        comment: String(input.comment || "").trim() || null,
        mustKeep: splitLines(input.mustKeep),
        mustRemove: splitLines(input.mustRemove),
        needsReshoot: splitLines(input.needsReshoot),
        submittedBy: input.submittedBy,
        operationKey: operationKey("customer-feedback"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.customerFeedback = result.feedback;
    studioState.feedbackDraftId = result.feedback.draftId || draftId;
    studioState.revisionProposal = null;
    if (result.feedback.satisfaction !== "satisfied") {
      await createRevisionProposal(result.feedback.id, workflow);
      if (!ownsStudioWorkflow(workflow)) return studioState;
    }
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, result.feedback.satisfaction === "satisfied"
      ? "客户已标记满意，只解锁这一份精确初稿的采用"
      : "修改提案已生成，请先内部确认；尚未生成 V2 初稿");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function createRevisionProposal(feedbackId, existingWorkflow = null) {
  const workflow = existingWorkflow || beginStudioWorkflow();
  const ownsBusyState = existingWorkflow === null;
  if (ownsBusyState) setStudioBusy(true, "正在把客户反馈整理成可确认的修改提案…");
  try {
    const result = await studioApi("/api/studio/revision-proposals", {
      method: "POST",
      body: { feedbackId, operationKey: operationKey("revision-proposal") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.revisionProposal = result.proposal;
    if (ownsBusyState) setStudioBusy(false, "修改提案已准备好，请内部确认");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    if (ownsBusyState) setStudioBusy(false, studioErrorMessage(error));
    else throw error;
  }
  renderStudioState();
  return studioState;
}

async function confirmRevisionProposal() {
  const proposal = studioState.revisionProposal;
  if (!canConfirmRevisionProposal(proposal) || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在内部确认修改提案并创建新版任务书…");
  try {
    const result = await studioApi("/api/studio/revision-proposals/confirm", {
      method: "POST",
      body: { proposalId: proposal.id, operationKey: operationKey("confirm-revision") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.brief = result.brief;
    studioState.drafts = [];
    studioState.revisionProposal = { ...proposal, status: "confirmed" };
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioMusicForCurrentBrief(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, `任务书 V${result.brief.version} 已创建；请另外生成新版剪辑方向`);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function refreshStudioHistory(workflow) {
  const result = await studioApi("/api/studio/history", {
    method: "POST", body: {}, signal: workflow.controller.signal,
  });
  if (ownsStudioWorkflow(workflow)) studioState.history = result.history;
  return studioState.history;
}

async function submitStudioFeedback(draftId, action) {
  if (studioState.busy) return studioState;
  if (action !== "adopt" && action !== "reject") return studioState;
  if (action === "adopt" && !hasSatisfiedFeedbackForDraft(draftId)) return studioState;
  const reason = null;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在记录你的审核决定…");
  try {
    const result = await studioApi("/api/studio/feedback", {
      method: "POST",
      body: { draftId, action, reason: reason?.trim() || null, operationKey: operationKey("feedback") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    if (action === "adopt") assertStudioAdoptionResponse(result, draftId);
    studioState.feedback = result.feedback;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    if (studioState.brief && Array.isArray(studioState.history?.drafts)) {
      studioState.drafts = studioState.history.drafts.filter((draft) =>
        draft.briefId === studioState.brief.id && draft.version === studioState.brief.version);
    }
    invalidateStudioCaptionFeedbackDataset();
    setStudioBusy(false, action === "adopt"
      ? "已记录“采用这版”，采用仍不是发布"
      : "修改意见已记录，原初稿保留不被覆盖");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function refreshStudioAnalysis(workflow = null) {
  if (!studioState.session) return studioState.analysisHistory;
  const active = workflow || beginStudioWorkflow();
  const result = await studioApi("/api/studio/analysis/history", {
    method: "POST", body: {}, signal: active.controller.signal,
  });
  if (ownsStudioWorkflow(active)) studioState.analysisHistory = result.history;
  renderStudioState();
  return studioState.analysisHistory;
}

async function refreshStudioModels(workflow = null) {
  if (!studioState.session) return studioState.modelStatus;
  const active = workflow || beginStudioWorkflow();
  const result = await studioApi("/api/studio/models/status", {
    method: "POST", body: {}, signal: active.controller.signal,
  });
  if (ownsStudioWorkflow(active)) studioState.modelStatus = result.models;
  renderStudioState();
  return studioState.modelStatus;
}

async function refreshStudioModelWorkbench() {
  if (!studioState.session || studioState.busy) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在刷新本地分析证据与模型状态…");
  try {
    await refreshStudioAnalysis(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioModels(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, "分析证据与本地模型状态已刷新");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function startStudioAnalysis(assetId, layers) {
  if (!studioState.session || studioState.busy) return studioState;
  const requestedLayers = Array.isArray(layers) ? [...layers] : [];
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在本机分层检查这段素材…");
  try {
    await studioApi("/api/studio/analysis/jobs", {
      method: "POST",
      body: { assetId, layers: requestedLayers, operationKey: operationKey("analysis") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioAnalysis(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioModels(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, "本地分析已完成；模型结果仍需人工复核");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitStudioAnalysisReview(receipt, action, target, correction = null) {
  const allowed = ["accept-suggestion", "correct-suggestion", "reject-suggestion", "keep-from-duplicate-group"];
  if (!studioState.session || studioState.busy || !allowed.includes(action)) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在记录精确绑定的人工复核决定…");
  try {
    await studioApi("/api/studio/analysis/review", {
      method: "POST",
      body: {
        action,
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        target,
        correction,
        operationKey: operationKey("analysis-review"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioAnalysis(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    setStudioBusy(false, "人工复核已记录；模型建议没有被当作自动事实");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitStudioMaterialSuitability(receipt, binding, decision, reason) {
  if (!studioState.session || studioState.busy || !["suitable", "not-suitable"].includes(decision)) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在记录素材对当前需求的人工结论…");
  try {
    await studioApi("/api/studio/analysis/suitability", {
      method: "POST",
      body: {
        decision,
        demandId: binding.demandId,
        matchAssessmentId: binding.matchAssessmentId,
        receiptJobId: receipt.jobId,
        sourceAssetId: receipt.sourceAssetId,
        sourceDigest: receipt.sourceDigest,
        sourceStartMs: receipt.sourceStartMs,
        sourceEndMs: receipt.sourceEndMs,
        reason,
        operationKey: operationKey("material-suitability"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioAnalysis(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    // A new suitability decision changes the evidence basis; require a fresh
    // match before the customer continues from the old ready result.
    studioState.matchAssessment = null;
    studioState.opportunities = [];
    studioState.gaps = [];
    studioState.gapCapability = null;
    setStudioBusy(false, "已记录素材是否适合当前需求的人工结论");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

function captureReferenceRequestGuard(controller = null) {
  const reference = studioState.reference || emptyReferenceState();
  return {
    requestEpoch: referenceRequestEpoch,
    scopeEpoch: reference.scopeEpoch,
    customerId: studioState.customerId,
    sessionToken: studioState.session?.token || null,
    sessionId: studioState.session?.sessionId || null,
    sampleIdentity: referenceGuardIdentity(reference.sample, ["sourceDigest", "sourceKind", "collectionMode", "status"]),
    rightsIdentity: referenceGuardIdentity(reference.rights, ["contentDigest", "sourceKind", "status"]),
    analysisIdentity: referenceGuardIdentity(reference.analysis, ["contentDigest", "status"]),
    selectionIdentity: referenceGuardIdentity(reference.selection, ["contentDigest", "status"]),
    mappingPlanIdentity: referenceGuardIdentity(reference.mappingPlan, ["contentDigest", "permissionSetDigest", "status"]),
    executionReportIdentity: referenceGuardIdentity(reference.executionReport, ["status"]),
    draftIdentities: canonicalReferenceJson((reference.drafts || []).map(referenceDraftBinding)),
    controller,
  };
}

function referenceGuardIdentity(value, extraFields) {
  return canonicalReferenceJson(referenceIdentity(value, extraFields));
}

function ownsReferenceRequestGuard(guard) {
  const reference = studioState.reference || emptyReferenceState();
  if (guard.controller?.signal.aborted || guard.requestEpoch !== referenceRequestEpoch) return false;
  return guard.scopeEpoch === reference.scopeEpoch &&
    guard.customerId === studioState.customerId &&
    guard.sessionToken === (studioState.session?.token || null) &&
    guard.sessionId === (studioState.session?.sessionId || null) &&
    guard.sampleIdentity === referenceGuardIdentity(reference.sample, ["sourceDigest", "sourceKind", "collectionMode", "status"]) &&
    guard.rightsIdentity === referenceGuardIdentity(reference.rights, ["contentDigest", "sourceKind", "status"]) &&
    guard.analysisIdentity === referenceGuardIdentity(reference.analysis, ["contentDigest", "status"]) &&
    guard.selectionIdentity === referenceGuardIdentity(reference.selection, ["contentDigest", "status"]) &&
    guard.mappingPlanIdentity === referenceGuardIdentity(reference.mappingPlan, ["contentDigest", "permissionSetDigest", "status"]) &&
    guard.executionReportIdentity === referenceGuardIdentity(reference.executionReport, ["status"]) &&
    guard.draftIdentities === canonicalReferenceJson((reference.drafts || []).map(referenceDraftBinding));
}

function beginReferenceAction(action, message) {
  activeReferenceRequestController?.abort();
  const controller = new AbortController();
  activeReferenceRequestController = controller;
  referenceRequestEpoch += 1;
  studioState.reference.busyAction = action;
  studioState.reference.error = null;
  const status = document.querySelector("#reference-status");
  if (status) status.textContent = message;
  renderStudioState();
  return captureReferenceRequestGuard(controller);
}

function finishReferenceAction(guard, message) {
  if (!ownsReferenceRequestGuard(guard)) return false;
  studioState.reference.busyAction = null;
  studioState.reference.error = null;
  const status = document.querySelector("#reference-status");
  if (status) status.textContent = message;
  renderStudioState();
  return true;
}

function failReferenceAction(guard, error) {
  if (!ownsReferenceRequestGuard(guard) || isAbortError(error)) return false;
  studioState.reference.busyAction = null;
  studioState.reference.error = error instanceof Error ? error.message : String(error);
  const status = document.querySelector("#reference-status");
  if (status) status.textContent = "本次参考样片操作未完成，已保留上一个有效阶段。";
  renderStudioState();
  return true;
}

function referenceSignal(guard) {
  return guard.controller?.signal;
}

function selectReferenceSample(sample) {
  activeReferenceRequestController?.abort();
  referenceRequestEpoch += 1;
  referenceOperationKeyCache.clear();
  studioState.reference = {
    ...emptyReferenceState(studioState.reference.scopeEpoch),
    samples: studioState.reference.samples,
    historyAssets: studioState.reference.historyAssets,
    sample,
  };
  renderStudioState();
  return sample;
}

async function refreshReferenceLibrary() {
  if (!studioState.session) return studioState.reference.samples;
  referenceLibraryRefreshController?.abort();
  const controller = new AbortController();
  referenceLibraryRefreshController = controller;
  const sequence = ++referenceLibraryRefreshSequence;
  const guard = captureReferenceRequestGuard(controller);
  try {
    const result = await studioApi("/api/studio/reference-samples", { method: "GET", signal: controller.signal });
    if (!ownsReferenceRefresh("library", sequence, guard)) return studioState.reference.samples;
    studioState.reference.samples = Array.isArray(result.samples) ? result.samples : [];
    renderStudioState();
  } catch (error) {
    if (ownsReferenceRefresh("library", sequence, guard) && !isAbortError(error)) {
      studioState.reference.error = error instanceof Error ? error.message : String(error);
      renderStudioState();
    }
  }
  return studioState.reference.samples;
}

async function refreshReferenceHistory() {
  if (!studioState.session) return studioState.reference.historyAssets;
  referenceHistoryRefreshController?.abort();
  const controller = new AbortController();
  referenceHistoryRefreshController = controller;
  const sequence = ++referenceHistoryRefreshSequence;
  const guard = captureReferenceRequestGuard(controller);
  try {
    const result = await studioApi("/api/studio/reference-history", { method: "GET", signal: controller.signal });
    if (!ownsReferenceRefresh("history", sequence, guard)) return studioState.reference.historyAssets;
    studioState.reference.historyAssets = Array.isArray(result.assets) ? result.assets : [];
    renderStudioState();
  } catch (error) {
    if (ownsReferenceRefresh("history", sequence, guard) && !isAbortError(error)) {
      studioState.reference.error = error instanceof Error ? error.message : String(error);
      renderStudioState();
    }
  }
  return studioState.reference.historyAssets;
}

function ownsReferenceRefresh(kind, sequence, guard) {
  const current = kind === "library" ? referenceLibraryRefreshSequence : referenceHistoryRefreshSequence;
  return sequence === current && ownsReferenceRequestGuard(guard);
}

async function uploadReferenceSample(file, sourceKind) {
  if (!studioState.session || !file || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("upload", "正在本机校验并导入参考样片…");
  try {
    const fileDigest = await referenceFileDigest(file);
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const operationKey = await referenceMutationOperationKey("upload", {
      sourceKind,
      file: {
        name: String(file.name || ""), size: Number(file.size || 0), type: String(file.type || ""),
        lastModified: Number(file.lastModified || 0), digest: fileDigest,
      },
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await referenceUploadApi(file, sourceKind, operationKey, referenceSignal(guard));
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    clearReferenceOperationKey("upload", operationKey);
    studioState.reference.busyAction = null;
    selectReferenceSample(result.sample);
    const selectedGuard = captureReferenceRequestGuard();
    await refreshReferenceLibrary();
    if (!ownsReferenceRequestGuard(selectedGuard)) return studioState;
    const status = document.querySelector("#reference-status");
    if (status) status.textContent = "样片已入库；请播放预览并确认来源范围。";
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function registerHistoricalReference(assetId, sourceKind) {
  if (!studioState.session || !assetId || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("history", "正在登记授权历史成片…");
  try {
    const operationKey = await referenceMutationOperationKey("history", { assetId, sourceKind });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-samples/from-history", {
      method: "POST",
      body: { assetId, sourceKind, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    clearReferenceOperationKey("history", operationKey);
    studioState.reference.busyAction = null;
    selectReferenceSample(result.sample);
    const selectedGuard = captureReferenceRequestGuard();
    await refreshReferenceLibrary();
    if (!ownsReferenceRequestGuard(selectedGuard)) return studioState;
    const status = document.querySelector("#reference-status");
    if (status) status.textContent = "历史成片已登记为参考样片；请确认来源范围。";
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function declareReferenceRights(sourceKind, reusableElements) {
  const sample = studioState.reference.sample;
  if (!sample || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("rights", "正在记录来源与可复用范围…");
  try {
    const normalizedReusableElements = sourceKind === "external-style-only"
      ? []
      : [...new Set(reusableElements)];
    const operationKey = await referenceMutationOperationKey("rights", {
      sample: referenceIdentity(sample, ["sourceDigest", "sourceKind", "collectionMode"]),
      sourceKind,
      reusableElements: normalizedReusableElements,
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-rights", {
      method: "POST",
      body: {
        referenceSampleId: sample.id,
        sourceKind,
        reusableElements: normalizedReusableElements,
        operationKey,
      },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    studioState.reference = {
      ...studioState.reference,
      rights: result.rights, analysis: null, selectedRuleIds: [], selection: null,
      mappingPlan: null, drafts: [], executionReport: null, busyAction: null, error: null,
    };
    clearReferenceOperationKey("rights", operationKey);
    referenceOperationKeyCache.clear();
    finishReferenceAction(captureReferenceRequestGuard(), "来源范围已确认；现在可以分析样片。");
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function analyzeReferenceSample() {
  const sample = studioState.reference.sample;
  if (!sample || !studioState.reference.rights || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("analysis", "正在拆解镜头、节奏、叙事、音频和基础转场…");
  try {
    const operationKey = await referenceMutationOperationKey("analysis", {
      sample: referenceIdentity(sample, ["sourceDigest", "sourceKind"]),
      rights: referenceIdentity(studioState.reference.rights, ["contentDigest"]),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-analyses", {
      method: "POST",
      body: { referenceSampleId: sample.id, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    studioState.reference = {
      ...studioState.reference,
      analysis: result.analysis,
      selectedRuleIds: (result.analysis?.rules || []).map((rule) => rule.id),
      selection: null, mappingPlan: null, drafts: [], executionReport: null,
      busyAction: null, error: null,
    };
    clearReferenceOperationKey("analysis", operationKey);
    referenceOperationKeyCache.clear();
    finishReferenceAction(captureReferenceRequestGuard(), "风格拆解卡已准备；请勾选这次真正要学的规则。");
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

function updateReferenceRuleSelection(ruleId, checked) {
  const selected = new Set(studioState.reference.selectedRuleIds);
  if (checked) selected.add(ruleId);
  else selected.delete(ruleId);
  activeReferenceRequestController?.abort();
  referenceRequestEpoch += 1;
  referenceOperationKeyCache.clear();
  studioState.reference = {
    ...studioState.reference,
    selectedRuleIds: [...selected],
    selection: null, mappingPlan: null, drafts: [], executionReport: null,
    busyAction: null, error: null,
  };
  renderStudioState();
}

async function confirmReferenceStyle() {
  const analysis = studioState.reference.analysis;
  if (!analysis || studioState.reference.selectedRuleIds.length === 0 || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("selection", "正在确认风格规则版本…");
  try {
    const operationKey = await referenceMutationOperationKey("selection", {
      analysis: referenceIdentity(analysis, ["contentDigest"]),
      selectedRuleIds: [...studioState.reference.selectedRuleIds],
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-style-selections", {
      method: "POST",
      body: {
        analysisId: analysis.id,
        selectedRuleIds: [...studioState.reference.selectedRuleIds],
        operationKey,
      },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    studioState.reference = {
      ...studioState.reference,
      selection: result.selection, mappingPlan: null, drafts: [], executionReport: null,
      busyAction: null, error: null,
    };
    clearReferenceOperationKey("selection", operationKey);
    referenceOperationKeyCache.clear();
    finishReferenceAction(captureReferenceRequestGuard(), "风格规则已确认；可以建立素材匹配计划。");
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function createReferenceMapping() {
  const reference = studioState.reference;
  if (!reference.sample || reference.selection?.status !== "confirmed" || reference.busyAction) return studioState;
  const guard = beginReferenceAction("mapping", "正在用已授权的客户素材对位参考规则…");
  try {
    const operationKey = await referenceMutationOperationKey("mapping", {
      sample: referenceIdentity(reference.sample, ["sourceDigest"]),
      rights: referenceIdentity(reference.rights, ["contentDigest"]),
      analysis: referenceIdentity(reference.analysis, ["contentDigest"]),
      selection: referenceIdentity(reference.selection, ["contentDigest", "status"]),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-mapping-plans", {
      method: "POST",
      body: { referenceSampleId: reference.sample.id, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    studioState.reference = {
      ...studioState.reference, mappingPlan: result.mappingPlan,
      drafts: [], executionReport: null, busyAction: null, error: null,
    };
    clearReferenceOperationKey("mapping", operationKey);
    referenceOperationKeyCache.clear();
    finishReferenceAction(captureReferenceRequestGuard(), "匹配计划已生成；请逐条复核后人工确认。");
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function confirmReferenceMapping() {
  const mappingPlan = studioState.reference.mappingPlan;
  if (!mappingPlan || mappingPlan.status !== "draft" || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("mapping-confirm", "正在固定这份素材匹配版本…");
  try {
    const operationKey = await referenceMutationOperationKey("mapping-confirm", {
      mappingPlan: referenceIdentity(mappingPlan, ["contentDigest", "permissionSetDigest", "status"]),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi("/api/studio/reference-mapping-plans/confirm", {
      method: "POST",
      body: { mappingPlanId: mappingPlan.id, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    studioState.reference = {
      ...studioState.reference, mappingPlan: result.mappingPlan,
      drafts: [], executionReport: null, busyAction: null, error: null,
    };
    clearReferenceOperationKey("mapping-confirm", operationKey);
    referenceOperationKeyCache.clear();
    finishReferenceAction(captureReferenceRequestGuard(), "匹配计划已人工确认；四版渲染闸门已解锁。");
  } catch (error) {
    failReferenceAction(guard, error);
  }
  renderStudioState();
  return studioState;
}

async function renderReferenceDrafts() {
  const mappingPlan = studioState.reference.mappingPlan;
  if (!mappingPlan || mappingPlan.status !== "confirmed" || studioState.reference.busyAction) return studioState;
  const guard = beginReferenceAction("render", "正在渲染原视频母版与两个平台派生版…");
  try {
    const operationKey = await referenceMutationOperationKey("render", {
      mappingPlan: referenceIdentity(mappingPlan, ["contentDigest", "permissionSetDigest", "status"]),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const rendered = await studioApi("/api/studio/reference-drafts", {
      method: "POST",
      body: { mappingPlanId: mappingPlan.id, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const fetched = await studioApi(`/api/studio/reference-execution-reports/${encodeURIComponent(rendered.report.id)}`, {
      method: "GET", signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const report = requireReviewableReferenceReport(fetched.report);
    studioState.reference = {
      ...studioState.reference,
      executionReport: report,
      drafts: referenceDraftsFromReport(report),
      busyAction: null, error: null,
    };
    clearReferenceOperationKey("render", operationKey);
    finishReferenceAction(captureReferenceRequestGuard(), "四个待审版本已生成；请逐片播放并查看执行报告。");
  } catch (error) {
    const failed = failReferenceAction(guard, error);
    if (failed && isReferenceDraftUnavailable(error)) invalidateReferenceDraftReview(error);
  }
  renderStudioState();
  return studioState;
}

function referenceDraftsFromReport(report) {
  return (report?.artifacts || []).map((artifact) => {
    const draft = {
      ...artifact,
      draftVersion: artifact.draftVersion ?? artifact.version,
      label: studioPlatformLabel(artifact.kind),
      mediaUrl: `/api/studio/reference-drafts/${encodeURIComponent(artifact.draftId)}/media`,
    };
    return {
      ...draft,
      status: artifact.status || "reviewable",
    };
  });
}

function studioPlatformLabel(variant) {
  return ({
    "editorial-master": "原视频母版",
    xiaohongshu: "小红书版",
    douyin: "抖音版",
    "wechat-channels": "视频号版",
  })[variant] || "未标注平台版本";
}

function requireReviewableReferenceReport(report) {
  if (report?.status === "reviewable") return report;
  const error = new Error("reference-execution-report-not-current");
  error.status = 409;
  throw error;
}

function referenceDraftBinding(draft) {
  return {
    kind: draft?.kind || null,
    draftId: draft?.draftId || null,
    draftVersion: draft?.draftVersion ?? draft?.version ?? null,
    draftDigest: draft?.outputDigest || null,
  };
}

function referenceDraftControlIdentity(report, draft) {
  return canonicalReferenceJson({
    report: referenceIdentity(report),
    draft: referenceDraftBinding(draft),
  });
}

function syncReferenceDraftControls() {
  const reference = studioState.reference;
  const previous = reference.draftControls || {};
  const next = {};
  for (const draft of reference.drafts || []) {
    const identity = referenceDraftControlIdentity(reference.executionReport, draft);
    const current = previous[draft.kind];
    next[draft.kind] = current?.identity === identity
      ? current
      : { identity, prompt: "", confirmed: false };
  }
  reference.draftControls = next;
  return next;
}

function findReferenceDraft(draftId) {
  return (studioState.reference.drafts || []).find((draft) => draft.draftId === draftId) || null;
}

function setReferenceDraftPrompt(draftId, prompt) {
  const draft = findReferenceDraft(draftId);
  if (!draft) return false;
  syncReferenceDraftControls();
  studioState.reference.draftControls[draft.kind].prompt = String(prompt ?? "");
  return true;
}

function setReferenceDraftApprovalConfirmation(draftId, confirmed) {
  const draft = findReferenceDraft(draftId);
  if (!draft) return false;
  syncReferenceDraftControls();
  studioState.reference.draftControls[draft.kind].confirmed = confirmed === true;
  return true;
}

async function reviseReferenceDraft(draftId) {
  const draft = findReferenceDraft(draftId);
  const report = studioState.reference.executionReport;
  if (!draft || !report || draft.status === "invalidated" || studioState.reference.busyAction) return studioState;
  syncReferenceDraftControls();
  const prompt = String(studioState.reference.draftControls[draft.kind]?.prompt || "").trim();
  if (!prompt) return studioState;
  const exactInput = {
    reportId: report.id,
    reportVersion: report.version,
    draftVersion: draft.draftVersion ?? draft.version,
    draftDigest: draft.outputDigest,
    prompt,
  };
  const action = `revise-${draft.kind}`;
  const guard = beginReferenceAction(action, `正在改剪${draft.label || draft.kind}…`);
  try {
    const operationKey = await referenceMutationOperationKey(action, exactInput);
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi(`/api/studio/reference-drafts/${encodeURIComponent(draft.draftId)}/revise`, {
      method: "POST",
      body: { ...exactInput, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const revisedReport = requireReviewableReferenceReport(result?.report);
    studioState.reference = {
      ...studioState.reference,
      executionReport: revisedReport,
      drafts: referenceDraftsFromReport(revisedReport),
      busyAction: null,
      error: null,
    };
    clearReferenceOperationKey(action, operationKey);
    finishReferenceAction(captureReferenceRequestGuard(), "新版本已生成；请重新审片并按精确版本批准。");
  } catch (error) {
    const failed = failReferenceAction(guard, error);
    if (failed && isReferenceDraftUnavailable(error)) invalidateReferenceDraftReview(error);
  }
  renderStudioState();
  return studioState;
}

async function approveReferenceDraft(draftId) {
  const draft = findReferenceDraft(draftId);
  const report = studioState.reference.executionReport;
  if (!draft || !report || draft.status !== "reviewable" || studioState.reference.busyAction) return studioState;
  syncReferenceDraftControls();
  const control = studioState.reference.draftControls[draft.kind];
  if (control?.confirmed !== true) return studioState;
  const exactInput = {
    reportId: report.id,
    reportVersion: report.version,
    draftVersion: draft.draftVersion ?? draft.version,
    draftDigest: draft.outputDigest,
  };
  const action = `approve-${draft.kind}`;
  const guard = beginReferenceAction(action, `正在记录${draft.label || draft.kind}的人工审片结论…`);
  try {
    const operationKey = await referenceMutationOperationKey(action, exactInput);
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    const result = await studioApi(`/api/studio/reference-drafts/${encodeURIComponent(draft.draftId)}/approve`, {
      method: "POST",
      body: { ...exactInput, operationKey },
      signal: referenceSignal(guard),
    });
    if (!ownsReferenceRequestGuard(guard)) return studioState;
    applyReferenceApprovalReview(result, draft);
    studioState.reference.draftControls[draft.kind].confirmed = false;
    studioState.reference.busyAction = null;
    studioState.reference.error = null;
    clearReferenceOperationKey(action, operationKey);
    finishReferenceAction(captureReferenceRequestGuard(), `${draft.label || draft.kind}已记录人工批准；这不是发布。`);
  } catch (error) {
    const failed = failReferenceAction(guard, error);
    if (failed && isReferenceDraftUnavailable(error)) invalidateReferenceDraftReview(error);
  }
  renderStudioState();
  return studioState;
}

function applyReferenceApprovalReview(result, requestedDraft) {
  const currentReport = studioState.reference.executionReport;
  if (!result?.approval || result.approval.publishClaimed !== false ||
      !result.review || result.review.publishClaimed !== false ||
      !(["reviewable", "approved"]).includes(result.review.status) ||
      result.review.bindings?.executionReport?.id !== currentReport?.id ||
      Number(result.review.bindings?.executionReport?.version) !== Number(currentReport?.version) ||
      !Array.isArray(result.review.artifacts)) {
    throw referenceConflictError("reference-approval-review-not-current");
  }
  const statuses = new Map(result.review.artifacts.map((artifact) => [artifact.kind, artifact]));
  const nextDrafts = studioState.reference.drafts.map((draft) => {
    const artifact = statuses.get(draft.kind);
    if (!artifact || artifact.draftId !== draft.draftId ||
        Number(artifact.draftVersion) !== Number(draft.draftVersion ?? draft.version) ||
        artifact.outputDigest !== draft.outputDigest) {
      throw referenceConflictError("reference-approval-response-binding-mismatch");
    }
    return { ...draft, status: artifact.status };
  });
  const approved = nextDrafts.find((draft) => draft.kind === requestedDraft.kind);
  if (approved?.status !== "approved") {
    throw referenceConflictError("reference-approval-response-not-current");
  }
  studioState.reference.drafts = nextDrafts;
  studioState.reference.review = result.review;
}

function isReferenceDraftUnavailable(error) {
  const code = error instanceof Error ? error.message : String(error);
  return error?.status === 409 &&
    /(withdraw|invalidated|not-authorized|permission|stale|not-current|render-required|digest-mismatch|binding-mismatch)/u.test(code);
}

function referenceConflictError(code) {
  const error = new Error(code);
  error.status = 409;
  return error;
}

function invalidateReferenceDraftReview(error) {
  if (studioState.reference.executionReport) {
    studioState.reference.executionReport = {
      ...studioState.reference.executionReport,
      status: "invalidated",
    };
  }
  studioState.reference.review = null;
  studioState.reference.drafts = studioState.reference.drafts.map((draft) => ({
    ...draft,
    mediaUrl: "",
    status: "invalidated",
  }));
  for (const control of Object.values(studioState.reference.draftControls || {})) {
    control.confirmed = false;
  }
  studioState.reference.error = error instanceof Error ? error.message : String(error);
  const status = document.querySelector("#reference-status");
  if (status) status.textContent = "素材权限或版本已失效；播放与审批已关闭。";
}

function findReferenceCardControl(container, selector, datasetKey, draftId) {
  return [...container.querySelectorAll(selector)]
    .find((control) => control.dataset?.[datasetKey] === draftId) || null;
}

async function referenceUploadApi(file, sourceKind, operationKey, signal) {
  const body = new FormData();
  body.append("sourceKind", sourceKind);
  body.append("operationKey", operationKey);
  body.append("file", file, file.name);
  const headers = {};
  if (studioState.session?.token) headers.authorization = `Bearer ${studioState.session.token}`;
  const response = await fetch("/api/studio/reference-samples/import", {
    method: "POST", headers, credentials: "same-origin", body, signal,
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "reference-upload-failed");
    error.status = response.status;
    throw error;
  }
  return result;
}

async function uploadStudioMaterial(form) {
  if (!studioState.session || studioState.busy) return studioState;
  const files = [...(form.elements?.namedItem("file")?.files || [])];
  if (files.length === 0) return studioState;
  if (files.length > 20) {
    setStudioBusy(false, "每批最多选择 20 条视频，请分批入库");
    renderStudioState();
    return studioState;
  }
  const workflow = beginStudioWorkflow();
  const items = [];
  const knownDigests = new Set((studioState.materials || []).map((material) => material.sourceDigest).filter(Boolean));
  setStudioBusy(true, `正在逐条检查 ${files.length} 条视频；重复或损坏文件不会入库…`);
  for (const file of files) {
    if (!ownsStudioWorkflow(workflow)) return studioState;
    const base = { fileName: file.name, byteLength: file.size };
    const inputError = studioMaterialFileInputError(file);
    if (inputError) {
      items.push({ ...base, status: "rejected", reason: inputError, sourceDigest: null });
      continue;
    }
    let sourceDigest;
    try {
      sourceDigest = await studioFileSha256(file);
    } catch {
      items.push({ ...base, status: "failed", reason: "无法计算文件校验值，已停止入库", sourceDigest: null });
      continue;
    }
    if (knownDigests.has(sourceDigest)) {
      items.push({ ...base, status: "duplicate", reason: "与素材库或本批次已有视频完全相同", sourceDigest });
      continue;
    }
    try {
      const body = new FormData();
      body.append("contentType", formValue(form, "materialContentType"));
      body.append("operationKey", operationKey("material-upload"));
      body.append("file", file, file.name);
      const headers = { authorization: `Bearer ${studioState.session.token}` };
      const response = await fetch("/api/studio/materials/import", {
        method: "POST", headers, credentials: "same-origin", body, signal: workflow.controller.signal,
      });
      const result = await response.json();
      if (!response.ok) {
        const error = new Error(result.error || "material-upload-failed");
        error.status = response.status;
        throw error;
      }
      if (!ownsStudioWorkflow(workflow)) return studioState;
      if (result.material?.sourceDigest !== sourceDigest) throw new Error("material-upload-digest-mismatch");
      knownDigests.add(sourceDigest);
      studioState.inventory = result.inventory;
      studioState.materialTags = result.materialTags || studioState.materialTags;
      studioState.materials = [result.material,
        ...(result.materials || studioState.materials || []).filter((material) => material.id !== result.material.id)];
      items.push({
        ...base, status: "imported", reason: "媒体检查通过并已进入人工高光队列",
        sourceDigest, materialCardId: result.material.id, sourceAssetId: result.material.sourceAssetId,
      });
    } catch (error) {
      if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
      const messages = {
        "material-upload-duration-too-long": "超过 60 分钟，请先拆成较短片段",
        "material-upload-media-invalid": "视频无法完整读取，请重新导出 MP4",
        "material-upload-too-large": "超过 256 MB，请先压缩",
        "material-upload-digest-mismatch": "服务端校验值不一致，未作为成功入库",
      };
      const code = error instanceof Error ? error.message : "";
      items.push({ ...base, status: "failed", reason: messages[code] || studioErrorMessage(error), sourceDigest });
    }
  }
  if (!ownsStudioWorkflow(workflow)) return studioState;
  studioState.materialUploadReport = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    customerId: studioState.customerId,
    sessionId: studioState.session.sessionId || null,
    items,
    summary: {
      selected: items.length,
      imported: items.filter((item) => item.status === "imported").length,
      duplicate: items.filter((item) => item.status === "duplicate").length,
      rejected: items.filter((item) => item.status === "rejected").length,
      failed: items.filter((item) => item.status === "failed").length,
    },
    boundary: { humanLabelsCreated: 0, syntheticCountedAsReal: 0, automaticPublishing: false },
  };
  studioMaterialFilter = "all";
  form.reset();
  const fileName = form.querySelector("[data-material-file-name]");
  if (fileName) fileName.textContent = "可一次选择多条视频";
  studioActiveTab = "library";
  studioLibraryMode = "materials";
  if (items.some((item) => item.status === "imported")) await refreshStudioHighlights(workflow);
  const summary = studioState.materialUploadReport.summary;
  setStudioBusy(false, `批量检查完成：入库 ${summary.imported} 条，重复 ${summary.duplicate} 条，未入库 ${summary.rejected + summary.failed} 条。`);
  renderStudioState();
  return studioState;
}

function studioMaterialFileInputError(file) {
  if (!/\.(mp4|mov)$/i.test(file.name) ||
      (file.type && file.type !== "video/mp4" && file.type !== "video/quicktime")) {
    return "只接受 MP4 或 MOV 视频";
  }
  if (file.size > 268_435_456) return "超过 256 MB，请先压缩";
  if (file.size < 1) return "文件为空";
  return null;
}

async function studioFileSha256(file) {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function renderMaterialUploadReport(report) {
  if (!report?.items?.length) return "";
  const labels = { imported: "已入库", duplicate: "重复已跳过", rejected: "格式不合格", failed: "检查失败" };
  const rows = report.items.map((item) => `<li class="upload-result-${escapeAttribute(item.status)}">
    <strong>${escapeHtml(item.fileName)}</strong><span>${escapeHtml(labels[item.status] || item.status)} · ${escapeHtml(item.reason)}</span>${item.sourceDigest ? `<small>SHA-256 ${escapeHtml(item.sourceDigest)}</small>` : ""}
  </li>`).join("");
  return `<section class="material-upload-report"><div class="card-heading"><h4>本次批量入库回执</h4><button class="secondary-action" type="button" data-export-material-upload-report>导出 JSON</button></div>
    <p>选择 ${report.summary.selected} 条 · 入库 ${report.summary.imported} 条 · 重复 ${report.summary.duplicate} 条 · 未入库 ${report.summary.rejected + report.summary.failed} 条</p>
    <ol>${rows}</ol><p class="micro-boundary">人工高光标签新增 0 条 · 合成素材计入真实样本 0 条 · 自动发布：否。</p></section>`;
}

function exportMaterialUploadReport() {
  const report = studioState.materialUploadReport;
  if (!report) return null;
  const blob = new Blob([`${JSON.stringify(report, null, 2)}\n`], { type: "application/json" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = `真实视频批量入库回执-${report.generatedAt.slice(0, 10)}.json`;
  anchor.click();
  URL.revokeObjectURL(href);
  return report;
}

async function continueFromStudioLibrary() {
  if (!studioState.session || studioState.busy || !studioState.inventory?.items?.length) return studioState;
  setStudioAppTab("create");
  if (studioState.demand) await assessStudioDemand();
  else document.querySelector('#studio-quick-demand-form [name="prompt"]')?.focus();
  return studioState;
}

async function refreshStudioInventory() {
  if (!studioState.session) return studioState;
  try {
    const result = await studioApi("/api/studio/inventory", { body: {} });
    studioState.inventory = result.inventory;
    studioState.materials = result.materials || [];
    studioState.materialTags = result.materialTags || studioState.materialTags;
    await refreshStudioHighlights();
    setStudioBusy(false, `素材库已更新，共 ${studioState.materials.length} 段视频`);
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function refreshStudioHighlights(workflow = null) {
  if (!studioState.session) return [];
  const result = await studioApi("/api/studio/highlights/list", {
    method: "POST",
    body: {},
    ...(workflow?.controller ? { signal: workflow.controller.signal } : {}),
  });
  if (!workflow || ownsStudioWorkflow(workflow)) {
    studioState.highlightEvaluations = result.evaluations || [];
    studioState.highlightRetestAttempts = result.attempts || [];
    studioState.highlightCoverageProfiles = result.profiles || [];
    studioState.highlightCandidateRejections = result.rejections || [];
    studioState.highlightBatch = result.batch || null;
  }
  return studioState.highlightEvaluations;
}

async function submitStudioHighlight(form) {
  if (!studioState.session || studioState.busy) return studioState;
  const materialCardId = String(form.dataset.highlightForm || "");
  const humanConfirmation = form.elements?.namedItem?.("humanConfirmed");
  if (!humanConfirmation?.checked) {
    setHighlightFormStatus(form, "请先确认这是你亲自看过视频后作出的判断；模型建议不能代替这一步。");
    return studioState;
  }
  const startSeconds = Number(formValue(form, "startSeconds"));
  const endSeconds = Number(formValue(form, "endSeconds"));
  const reason = String(formValue(form, "reason")).trim();
  const workflow = beginStudioWorkflow();
  let submitted = false;
  setStudioBusy(true, "正在记录你确认的高光时间，并与模型候选对比…");
  try {
    const result = await studioApi("/api/studio/highlights", {
      method: "POST",
      body: {
        materialCardId,
        sourceStartMs: Math.round(startSeconds * 1_000),
        sourceEndMs: Math.round(endSeconds * 1_000),
        reason,
        humanConfirmed: true,
        operationKey: operationKey("highlight"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || [];
    studioState.highlightCoverageProfiles = result.profiles || studioState.highlightCoverageProfiles;
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || studioState.highlightBatch;
    clearHighlightDraft(materialCardId);
    form.reset();
    submitted = true;
    setStudioBusy(false, "人工高光已记录；页面显示的是模型对比结果，不代表已发布或已训练");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  if (submitted) focusNextHighlightAction();
  return studioState;
}

async function acceptStudioHighlightCandidate(control) {
  if (!studioState.session || studioState.busy || !control) return studioState;
  const materialCardId = String(control.dataset.acceptHighlightCandidate || "");
  const receiptJobId = String(control.dataset.highlightReceipt || "");
  const sourceStartMs = Number(control.dataset.highlightStartMs);
  const sourceEndMs = Number(control.dataset.highlightEndMs);
  if (!materialCardId || !receiptJobId || !Number.isSafeInteger(sourceStartMs)
    || !Number.isSafeInteger(sourceEndMs) || sourceEndMs <= sourceStartMs) return studioState;
  const workflow = beginStudioWorkflow();
  let submitted = false;
  setStudioBusy(true, "正在核对模型回执并记录你的确认…");
  try {
    const result = await studioApi("/api/studio/highlights/accept-candidate", {
      method: "POST",
      body: {
        materialCardId,
        receiptJobId,
        sourceStartMs,
        sourceEndMs,
        humanConfirmed: true,
        operationKey: operationKey("highlight-candidate-accept"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || [];
    studioState.highlightCoverageProfiles = result.profiles || studioState.highlightCoverageProfiles;
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || studioState.highlightBatch;
    submitted = true;
    setStudioBusy(false, "已记录你对这段模型候选的人工确认；没有进入训练或发布");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  if (submitted) focusNextHighlightAction();
  return studioState;
}

async function rejectStudioHighlightCandidate(control) {
  if (!studioState.session || studioState.busy || !control) return studioState;
  const materialCardId = String(control.dataset.rejectHighlightCandidate || "");
  const receiptJobId = String(control.dataset.highlightReceipt || "");
  const sourceStartMs = Number(control.dataset.highlightStartMs);
  const sourceEndMs = Number(control.dataset.highlightEndMs);
  if (!materialCardId || !receiptJobId || !Number.isSafeInteger(sourceStartMs)
    || !Number.isSafeInteger(sourceEndMs) || sourceEndMs <= sourceStartMs) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在核对模型回执并记录这次否决…");
  try {
    const result = await studioApi("/api/studio/highlights/reject-candidate", {
      method: "POST",
      body: {
        materialCardId,
        receiptJobId,
        sourceStartMs,
        sourceEndMs,
        humanConfirmed: true,
        operationKey: operationKey("highlight-candidate-reject"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || studioState.highlightEvaluations;
    studioState.highlightRetestAttempts = result.attempts || studioState.highlightRetestAttempts;
    studioState.highlightCoverageProfiles = result.profiles || studioState.highlightCoverageProfiles;
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || studioState.highlightBatch;
    setStudioBusy(false, "已记录负反馈；没有计入人工高光、训练或发布");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function submitStudioHighlightCoverage(form) {
  if (!studioState.session || studioState.busy) return studioState;
  const materialCardId = String(form.dataset.highlightCoverageForm || "");
  if (!form.elements?.namedItem?.("humanConfirmed")?.checked) {
    const status = form.querySelector?.("[data-highlight-coverage-status]");
    if (status) status.textContent = "请先确认这些选项来自你亲自观看，而不是照抄模型。";
    return studioState;
  }
  const workflow = beginStudioWorkflow();
  let submitted = false;
  setStudioBusy(true, "正在保存人工确认的拍摄覆盖画像…");
  try {
    const result = await studioApi("/api/studio/highlights/coverage", {
      method: "POST",
      body: {
        materialCardId,
        lighting: formValue(form, "lighting"),
        motion: formValue(form, "motion"),
        environment: formValue(form, "environment"),
        humanConfirmed: true,
        operationKey: operationKey("highlight-coverage"),
      },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || studioState.highlightEvaluations;
    studioState.highlightRetestAttempts = result.attempts || studioState.highlightRetestAttempts;
    studioState.highlightCoverageProfiles = result.profiles || [];
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || studioState.highlightBatch;
    submitted = true;
    setStudioBusy(false, "人工拍摄画像已保存；它只用于本地样本覆盖门槛");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  if (submitted) focusNextHighlightAction();
  return studioState;
}

async function retestStudioHighlight(materialCardId) {
  if (!studioState.session || studioState.busy || !materialCardId) return studioState;
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "本地视觉模型正在重新比较各时间窗，可能需要几分钟…");
  try {
    const result = await studioApi("/api/studio/highlights/retest", {
      method: "POST",
      body: { materialCardId },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || [];
    studioState.highlightRetestAttempts = result.attempts || [];
    studioState.highlightCoverageProfiles = result.profiles || studioState.highlightCoverageProfiles;
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || studioState.highlightBatch;
    const failed = studioState.highlightRetestAttempts.find((item) => item.status !== "succeeded");
    setStudioBusy(false, failed
      ? `分段模型复测未完成（${failed.failureCode || failed.status}），没有采用旧结果冒充`
      : "分段模型复测完成，请核对模型推荐时间窗与人工高光的重合度");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function retestStudioHighlightBatch() {
  if (!studioState.session || studioState.busy || Number(studioState.highlightBatch?.labeledVideos || 0) === 0) {
    return studioState;
  }
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在逐条复测所有已人工标注的视频；系统会保留每条真实结果…");
  try {
    const result = await studioApi("/api/studio/highlights/retest-batch", {
      method: "POST",
      body: {},
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.highlightEvaluations = result.evaluations || [];
    studioState.highlightRetestAttempts = result.attempts || [];
    studioState.highlightCoverageProfiles = result.profiles || studioState.highlightCoverageProfiles;
    studioState.highlightCandidateRejections = result.rejections || studioState.highlightCandidateRejections;
    studioState.highlightBatch = result.batch || null;
    setStudioBusy(false, "批量复测完成；请按批量门禁查看真实通过率、耗时和证据覆盖");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function createAutomaticHighlightDrafts() {
  if (!studioState.session || !studioState.brief || studioState.busy ||
      !studioState.highlightBatch?.automaticEditingEligible) return studioState;
  const workflow = beginStudioWorkflow();
  const briefId = studioState.brief.id;
  setStudioBusy(true, "真实证据门槛已通过，正在用模型选片结果生成本地样片…");
  try {
    const result = await studioApi("/api/studio/highlights/automatic-drafts", {
      method: "POST",
      body: { briefId, operationKey: operationKey("highlight-automatic-drafts") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow) || studioState.brief?.id !== briefId) return studioState;
    studioState.drafts = result.drafts || [];
    studioState.highlightBatch = result.gate || studioState.highlightBatch;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow) || studioState.brief?.id !== briefId) return studioState;
    studioActiveTab = "works";
    setStudioBusy(false, `${studioState.drafts.length} 个本地样片已生成；仍需人工播放审核，不会自动发布`);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function exportStudioHighlightReport() {
  if (!studioState.session || studioState.busy) return null;
  setStudioBusy(true, "正在生成本地批量验收回执…");
  try {
    const result = await studioApi("/api/studio/highlights/report", { method: "POST", body: {} });
    const report = result.report;
    if (!report || report.schemaVersion !== 1) throw new Error("studio-highlight-report-invalid");
    const blob = new Blob([`${JSON.stringify(report, null, 2)}\n`], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    const generatedDate = report.generatedAt
      ? new Intl.DateTimeFormat("en-CA", {
          timeZone: "Asia/Taipei",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
        }).format(new Date(report.generatedAt))
      : "local";
    anchor.download = `视频模型批量验收-${generatedDate}.json`;
    anchor.click();
    URL.revokeObjectURL(href);
    setStudioBusy(false, "批量验收回执已导出；文件不代表生产验收或发布许可");
    return report;
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
    return null;
  } finally {
    renderStudioState();
  }
}

async function exportStudioHighlightFeedbackDataset() {
  if (!studioState.session || studioState.busy) return null;
  setStudioBusy(true, "正在整理可追溯的模型反馈候选包…");
  try {
    const result = await studioApi("/api/studio/highlights/feedback-dataset", { method: "POST", body: {} });
    const dataset = result.dataset;
    if (!dataset || dataset.schemaVersion !== 1
      || dataset.boundary !== "versioned-candidate-dataset-not-training-approval"
      || !/^[a-f0-9]{64}$/.test(String(dataset.datasetDigest || ""))
      || dataset.readiness?.trainingEligible !== false
      || dataset.readiness?.automaticTraining !== false) {
      throw new Error("studio-highlight-feedback-dataset-invalid");
    }
    const blob = new Blob([`${JSON.stringify(dataset, null, 2)}\n`], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `${dataset.datasetId}.json`;
    anchor.click();
    URL.revokeObjectURL(href);
    setStudioBusy(false, `模型反馈候选包已导出（${Number(dataset.counts?.total || 0)} 条）；仍需人工审数据，未启动训练`);
    return dataset;
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
    return null;
  } finally {
    renderStudioState();
  }
}

async function exportStudioCaptionFeedbackDataset() {
  if (!studioState.session || studioState.busy) return null;
  setStudioBusy(true, "正在整理字幕修正错题本…");
  try {
    const result = await studioApi("/api/studio/captions/feedback-dataset", { method: "POST", body: {} });
    const dataset = result.dataset;
    if (!dataset || dataset.schemaVersion !== 1
      || dataset.boundary !== "versioned-caption-candidate-dataset-not-training-approval"
      || !/^[a-f0-9]{64}$/.test(String(dataset.datasetDigest || ""))
      || dataset.readiness?.trainingEligible !== false
      || dataset.readiness?.automaticTraining !== false
      || dataset.readiness?.releaseEligible !== false) {
      throw new Error("studio-caption-feedback-dataset-invalid");
    }
    const blob = new Blob([`${JSON.stringify(dataset, null, 2)}\n`], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `${dataset.datasetId}.json`;
    anchor.click();
    URL.revokeObjectURL(href);
    setStudioBusy(false, `字幕错题候选包已导出（${Number(dataset.counts?.total || 0)} 条）；未确认内容不会进入训练`);
    return dataset;
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
    return null;
  } finally {
    renderStudioState();
  }
}

async function loadStudioCaptionFeedbackDataset(draftId) {
  if (!studioState.session || studioState.busy) return null;
  setStudioBusy(true, "正在整理字幕修正记录…");
  try {
    const result = await studioApi("/api/studio/captions/feedback-dataset", { method: "POST", body: {} });
    const dataset = result.dataset;
    if (!dataset || dataset.schemaVersion !== 1
      || dataset.boundary !== "versioned-caption-candidate-dataset-not-training-approval"
      || !Array.isArray(dataset.entries)
      || !/^[a-f0-9]{64}$/.test(String(dataset.datasetDigest || ""))
      || dataset.readiness?.trainingEligible !== false
      || dataset.readiness?.automaticTraining !== false
      || dataset.readiness?.releaseEligible !== false) {
      throw new Error("studio-caption-feedback-dataset-invalid");
    }
    studioState.captionFeedbackDataset = dataset;
    studioState.captionFeedbackReviewDraftId = draftId;
    setStudioBusy(false, dataset.counts?.total > 0
      ? `已读取 ${dataset.counts.total} 条字幕修正记录`
      : "暂无可审核的字幕修正记录");
    return dataset;
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
    return null;
  } finally {
    renderStudioState();
  }
}

function buildStudioCaptureTaskCsv() {
  const batch = studioState.highlightBatch;
  if (!batch?.actionPlan) return null;
  const contentTypeLabels = { "single-product": "产品特写", "jewelry-making-process": "制作过程", "designer-viewpoint": "人物／口播" };
  const evaluationBySource = new Map((studioState.highlightEvaluations || []).map((item) => [item.sourceAssetId, item]));
  const profileKeys = new Set((studioState.highlightCoverageProfiles || []).map((item) =>
    `${item.sourceAssetId}\0${item.sourceDigest}`));
  const reviewableMaterials = (studioState.materials || []).filter((material) =>
    !isSyntheticHighlightFixture(material) && evaluationBySource.has(material.sourceAssetId));
  const rows = [];
  for (const material of reviewableMaterials) {
    const evaluation = evaluationBySource.get(material.sourceAssetId);
    if (!(evaluation?.labels || []).length) rows.push({
      taskType: "现有素材人工标注",
      materialName: material.displayName || material.fileName || material.id,
      status: "待人工标注",
      contentType: contentTypeLabels[material.contentType] || material.contentType || "",
      duration: `${Math.max(0, Number(evaluation?.sourceEndMs || 0) - Number(evaluation?.sourceStartMs || 0)) / 1_000} 秒`,
      lighting: "", motion: "", environment: "",
      boundary: "必须人工观看后提交",
    });
    const profileKey = `${material.sourceAssetId}\0${material.sourceDigest}`;
    if (!profileKeys.has(profileKey)) rows.push({
      taskType: "现有素材人工画像",
      materialName: material.displayName || material.fileName || material.id,
      status: "待人工画像",
      contentType: contentTypeLabels[material.contentType] || material.contentType || "",
      duration: `${Math.max(0, Number(evaluation?.sourceEndMs || 0) - Number(evaluation?.sourceStartMs || 0)) / 1_000} 秒`,
      lighting: "待人工确认", motion: "待人工确认", environment: "待人工确认",
      boundary: "模型推测不计入",
    });
  }
  const headers = ["任务编号", "任务类型", "素材或任务", "状态", "内容类型", "时长", "光线", "主体动作", "环境", "证据边界"];
  const lines = [headers, ...rows.map((row, index) => [
    index + 1, row.taskType, row.materialName, row.status, row.contentType, row.duration,
    row.lighting, row.motion, row.environment, row.boundary,
  ])];
  return {
    rows,
    csv: `${lines.map((line) => line.map(studioCsvCell).join(",")).join("\r\n")}\r\n`,
    boundary: { completedTasksClaimed: 0, modelInferencesCounted: 0, automaticPublishing: false },
  };
}

function studioCsvCell(value) {
  let text = String(value ?? "").normalize("NFC").replace(/[\r\n]+/gu, " ");
  if (/^[=+\-@]/u.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function exportStudioCaptureTaskList() {
  const taskList = buildStudioCaptureTaskCsv();
  if (!taskList) return null;
  const blob = new Blob([`\uFEFF${taskList.csv}`], { type: "text/csv;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = `现有素材补验任务单-${new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date())}.csv`;
  anchor.click();
  URL.revokeObjectURL(href);
  return taskList;
}

async function startStudioHardwarePairing() {
  if (!studioState.session) return studioState;
  stopHardwarePairingPoll();
  studioState.hardwarePairingError = null;
  try {
    const result = await studioApi("/api/studio/hardware/pairings", { body: {} });
    studioState.hardwarePairing = result.pairing;
    renderStudioState();
    hardwarePairingPoll = setInterval(() => { void refreshStudioHardwarePairing(); }, 2_000);
  } catch (error) {
    studioState.hardwarePairingError = studioErrorMessage(error);
    renderStudioState();
  }
  return studioState;
}

async function refreshStudioHardwarePairing() {
  const pairingId = studioState.hardwarePairing?.id;
  if (!studioState.session || !pairingId) return studioState;
  try {
    const result = await studioApi("/api/studio/hardware/pairings/status", { body: { pairingId } });
    if (studioState.hardwarePairing?.id !== pairingId) return studioState;
    studioState.hardwarePairing = result.pairing;
    studioState.hardwarePairingError = null;
    if (result.pairing.status !== "waiting") stopHardwarePairingPoll();
  } catch (error) {
    stopHardwarePairingPoll();
    studioState.hardwarePairingError = studioErrorMessage(error);
  }
  renderStudioState();
  return studioState;
}

function renderHardwarePairing() {
  const pairing = studioState.hardwarePairing;
  if (!pairing && !studioState.hardwarePairingError) return "";
  if (studioState.hardwarePairingError) {
    const lanDisabled = studioState.hardwarePairingError.includes("hardware-lan-disabled");
    return `<div class="hardware-pairing-copy"><h4>${lanDisabled ? "需要切换到手机连接模式" : "连接没有完成"}</h4><p>${lanDisabled ? "当前只允许这台 Mac 访问，所以手机扫任何二维码都连不上。请用“手机连接模式”重新启动本地工作台，再生成二维码。" : escapeHtml(studioState.hardwarePairingError)}</p>${lanDisabled ? '<p class="micro-boundary">本地启动命令：pnpm studio:demo:lan</p>' : '<div class="hardware-pairing-actions"><button class="secondary-action" type="button" data-hardware-pairing-retry>重新生成二维码</button></div>'}</div>`;
  }
  const waiting = pairing.status === "waiting";
  const connected = pairing.status === "connected";
  const connectionHost = /^http:\/\/([^/?#]+)/u.exec(String(pairing.pairingUrl || ""))?.[1]
    || "本机局域网地址";
  return `${waiting ? `<img src="${escapeAttribute(pairing.qrDataUrl)}" alt="iPhone 连接二维码">` : ""}<div class="hardware-pairing-copy"><h4>${waiting ? "用 iPhone 扫码" : connected ? `已连接：${escapeHtml(pairing.deviceName || "iPhone")}` : "二维码已过期"}</h4><p>${waiting ? `手机和 Mac 请连接同一个 Wi-Fi。连接地址：${escapeHtml(connectionHost)}。扫码后进入硬件收件箱，用一段视频模拟设备送入。` : connected ? "手机收件箱确认后，视频会进入当前客户素材库；完成后点下方刷新。" : "请重新生成一个二维码。"}</p><div class="hardware-pairing-actions">${connected ? '<button class="secondary-action" type="button" data-hardware-refresh>刷新素材库</button>' : ''}${waiting ? '' : '<button class="secondary-action" type="button" data-hardware-pairing-retry>重新连接</button>'}</div></div>`;
}

async function createStudioMaterialTag(labelValue) {
  const label = String(labelValue || "").trim();
  if (!studioState.session || studioState.busy || !label) return studioState;
  setStudioBusy(true, "正在添加管理标签…");
  try {
    const result = await studioApi("/api/studio/material-tags", {
      body: { label, operationKey: operationKey("material-tag-create") },
    });
    studioState.materialTags = result.materialTags;
    setStudioBusy(false, `已添加标签“${label}”`);
  } catch (error) {
    const messages = {
      "material-tag-label-exists": "已经有同名标签",
      "material-tag-limit-reached": "最多可以建立 30 个管理标签",
      "material-tag-label-invalid": "标签请控制在 20 个字以内",
    };
    setStudioBusy(false, messages[error instanceof Error ? error.message : ""] || studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function assignStudioMaterialTag(materialCardId, tagId, checked) {
  if (!studioState.session || studioState.busy) return studioState;
  const selected = new Set(materialTagIdsForCard(materialCardId));
  if (checked) selected.add(tagId);
  else selected.delete(tagId);
  setStudioBusy(true, "正在保存素材标签…");
  try {
    const result = await studioApi("/api/studio/material-tags/assign", {
      body: {
        materialCardId,
        tagIds: [...selected],
        operationKey: operationKey("material-tag-assign"),
      },
    });
    studioState.materialTags = result.materialTags;
    setStudioBusy(false, "素材标签已保存");
  } catch (error) {
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function referenceMutationOperationKey(action, input) {
  const identity = canonicalReferenceJson({
    action,
    scope: {
      scopeEpoch: studioState.reference.scopeEpoch,
      customerId: studioState.customerId,
      sessionId: studioState.session?.sessionId || null,
    },
    input,
  });
  const cached = referenceOperationKeyCache.get(action);
  if (cached?.identity === identity) return cached.key;
  const digest = await referenceSha256(identity);
  const key = `reference-${action}-${digest}`;
  referenceOperationKeyCache.set(action, { identity, key });
  return key;
}

function clearReferenceOperationKey(action, key) {
  if (referenceOperationKeyCache.get(action)?.key === key) referenceOperationKeyCache.delete(action);
}

function referenceIdentity(value, extraFields = []) {
  if (!value) return null;
  const identity = { id: value.id || null, version: value.version || null };
  for (const field of extraFields) identity[field] = value[field] ?? null;
  return identity;
}

async function referenceFileDigest(file) {
  const bytes = await file.arrayBuffer();
  return hexReferenceDigest(await crypto.subtle.digest("SHA-256", bytes));
}

async function referenceSha256(value) {
  const bytes = new TextEncoder().encode(value);
  return hexReferenceDigest(await crypto.subtle.digest("SHA-256", bytes));
}

function hexReferenceDigest(buffer) {
  return [...new Uint8Array(buffer)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function canonicalReferenceJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalReferenceJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalReferenceJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function assertStudioAdoptionResponse(result, draftId) {
  const feedback = result?.feedback;
  if (!feedback || feedback.action !== "adopt" || feedback.draftId !== draftId ||
      feedback.publishClaimed !== false) {
    throw new Error("studio-adoption-response-invalid");
  }
  return feedback;
}

let studioApi = async function studioApiRequest(path, options = {}) {
  const method = options.method || "POST";
  const headers = {};
  if (method !== "GET") headers["content-type"] = "application/json";
  if (options.authenticated !== false && studioState.session?.token) headers.authorization = `Bearer ${studioState.session.token}`;
  const response = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: method === "GET" ? undefined : JSON.stringify(options.body || {}), signal: options.signal,
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "studio-request-failed");
    error.status = response.status;
    throw error;
  }
  return result;
};

function beginStudioWorkflow(customerId = studioState.customerId, sessionToken = studioState.session?.token || null) {
  activeStudioRequestController?.abort();
  pendingStudioDemandEpoch = null;
  const controller = new AbortController();
  activeStudioRequestController = controller;
  studioRequestEpoch += 1;
  return { epoch: studioRequestEpoch, customerId, sessionToken, controller };
}

function ownsStudioWorkflow(workflow) {
  if (workflow.controller.signal.aborted || workflow.epoch !== studioRequestEpoch) return false;
  if (studioState.customerId !== workflow.customerId) return false;
  return workflow.sessionToken === null || studioState.session?.token === workflow.sessionToken;
}

async function resumeStudioSession(customerId, signal) {
  const response = await fetch("/api/studio/session/resume", {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "same-origin",
    body: "{}",
    signal,
  });
  const result = await response.json();
  if (response.status === 401) return null;
  if (!response.ok) {
    const error = new Error(result.error || "studio-session-resume-failed");
    error.status = response.status;
    throw error;
  }
  return result?.session?.customerId === customerId ? result : null;
}

function openStudioSessionSerially(workflow) {
  const opened = studioSessionQueue.catch(() => undefined).then(async () => {
    if (!ownsStudioWorkflow(workflow)) return null;
    const resumed = await resumeStudioSession(workflow.customerId, workflow.controller.signal);
    if (resumed) return resumed;
    if (!ownsStudioWorkflow(workflow)) return null;
    return studioApi("/api/studio/session", {
      method: "POST", body: { customerId: workflow.customerId }, authenticated: false,
    });
  });
  studioSessionQueue = opened.then(() => undefined, () => undefined);
  return opened;
}

function isAbortError(error) {
  return error instanceof Error && error.name === "AbortError";
}

function renderStudioState() {
  studioState.reference ||= emptyReferenceState();
  const release = document.querySelector("#studio-release");
  const currentDemand = document.querySelector("#current-demand");
  const materials = document.querySelector("#materials");
  const materialUploadResults = document.querySelector("#material-upload-results");
  const hardwarePairingCard = document.querySelector("#hardware-pairing-card");
  const librarySummary = document.querySelector("#library-summary");
  const createMaterialSummary = document.querySelector("#create-material-summary");
  const continueFromLibrary = document.querySelector("#continue-from-library");
  const matchAssessment = document.querySelector("#match-assessment");
  const opportunities = document.querySelector("#opportunities");
  const brief = document.querySelector("#brief");
  const musicSelection = document.querySelector("#music-selection");
  const drafts = document.querySelector("#drafts");
  const highlightEvaluations = document.querySelector("#highlight-evaluations");
  const proposal = document.querySelector("#revision-proposal");
  const history = document.querySelector("#studio-history");
  const analysisQueue = document.querySelector("#analysis-queue");
  const analysisHistory = document.querySelector("#analysis-history");
  const modelStatus = document.querySelector("#model-status");
  const referenceError = document.querySelector("#reference-error");
  const referenceHistory = document.querySelector("#reference-history-list");
  const referenceSample = document.querySelector("#reference-sample-card");
  const referenceAnalysis = document.querySelector("#reference-analysis");
  const referenceRules = document.querySelector("#reference-style-rules");
  const referenceMapping = document.querySelector("#reference-mapping");
  const referenceDrafts = document.querySelector("#reference-drafts");
  const referenceReport = document.querySelector("#reference-report");
  if (release) {
    const metadata = studioState.release;
    release.textContent = metadata
      ? `Release ${metadata.version} · ${metadata.label} · ${metadata.boundary || "local-synthetic-candidate"} · ${metadata.production ? "生产" : "非生产"}/${metadata.deployed ? "已部署" : "未部署"}`
      : "正在读取本地候选版本…";
  }
  if (currentDemand) currentDemand.innerHTML = renderStudioDemand(studioState.demand);
  const materialCount = studioState.inventory?.items?.length || 0;
  if (createMaterialSummary) createMaterialSummary.textContent = !studioState.inventory
    ? "正在读取素材…"
    : materialCount > 0 ? `已有 ${materialCount} 段素材可供挑选` : "先上传视频，再开始创作";
  if (continueFromLibrary) {
    continueFromLibrary.disabled = !studioState.session || studioState.busy || materialCount === 0;
    continueFromLibrary.textContent = studioState.demand ? "继续匹配素材" : "用这些素材开始";
  }
  if (materials) materials.innerHTML = renderMaterials(studioState.materials);
  if (materialUploadResults) materialUploadResults.innerHTML = renderMaterialUploadReport(studioState.materialUploadReport);
  if (hardwarePairingCard) {
    hardwarePairingCard.innerHTML = renderHardwarePairing();
    hardwarePairingCard.hidden = !studioState.hardwarePairing && !studioState.hardwarePairingError;
  }
  if (librarySummary) {
    const visibleCount = filteredStudioMaterials(studioState.materials).length;
    const activeCategory = studioMaterialCategories.find((category) => category.value === studioMaterialFilter);
    const activeCustomTag = studioMaterialFilter.startsWith("custom:")
      ? studioState.materialTags?.tags?.find((tag) => customMaterialFilterValue(tag.id) === studioMaterialFilter)
      : null;
    librarySummary.textContent = studioMaterialFilter === "all"
      ? `已有 ${studioState.materials.length} 段素材`
      : `${activeCategory?.label || activeCustomTag?.label || "当前分类"} ${visibleCount} 段 · 全部 ${studioState.materials.length} 段`;
  }
  if (matchAssessment) matchAssessment.innerHTML = renderMatchAssessment(studioState.matchAssessment);
  if (opportunities) opportunities.innerHTML = renderOpportunities(studioState.opportunities, studioState.gaps || [], studioState.gapCapability);
  if (brief) brief.innerHTML = renderBrief(
    studioState.brief,
    studioState.opportunities[0],
    studioState.materials,
  );
  if (musicSelection) musicSelection.innerHTML = renderMusicSelection(studioState.music);
  if (drafts) drafts.innerHTML = renderDraftCards(studioState.drafts);
  if (highlightEvaluations) highlightEvaluations.innerHTML = renderHighlightEvaluations(
    studioState.highlightEvaluations,
    studioState.materials,
    studioState.brief,
    studioState.highlightBatch,
    studioState.highlightCoverageProfiles,
    studioState.highlightCandidateRejections,
  );
  if (proposal) proposal.innerHTML = renderRevisionProposal(
    studioState.revisionProposal,
    studioState.drafts.length > 0,
  );
  if (history) history.innerHTML = renderStudioHistory(studioState.history);
  if (analysisQueue) analysisQueue.innerHTML = renderAnalysisQueue(studioState.analysisHistory);
  if (analysisHistory) analysisHistory.innerHTML = renderAnalysisHistory(studioState.analysisHistory);
  if (modelStatus) modelStatus.innerHTML = renderModelStatus(studioState.modelStatus);
  if (referenceError) {
    referenceError.hidden = !studioState.reference.error;
    referenceError.textContent = studioState.reference.error || "";
  }
  if (referenceHistory) referenceHistory.innerHTML = renderReferenceHistory(studioState.reference.historyAssets);
  if (referenceSample) reconcileReferenceSample(referenceSample, studioState.reference.sample);
  if (referenceAnalysis) referenceAnalysis.innerHTML = renderReferenceAnalysis(studioState.reference.analysis);
  if (referenceRules) {
    referenceRules.disabled = !studioState.reference.analysis || Boolean(studioState.reference.busyAction);
    referenceRules.innerHTML = renderReferenceRuleChoices(
      studioState.reference.analysis,
      studioState.reference.selectedRuleIds,
    );
  }
  if (referenceMapping) referenceMapping.innerHTML = renderReferenceMapping(studioState.reference.mappingPlan);
  syncReferenceDraftControls();
  if (referenceDrafts) reconcileReferenceDrafts(referenceDrafts, studioState.reference.drafts);
  if (referenceReport) referenceReport.innerHTML = renderReferenceExecutionReport(studioState.reference.executionReport);

  syncReferenceRightsControls();
  renderReferenceGateState();

  setDisabled("#assess-demand", !studioState.demand || !studioState.inventory || studioState.busy);
  setDisabled("#find-story", !studioState.inventory || studioState.busy);
  setDisabled("#connect-hardware", !studioState.session || studioState.busy);
  setDisabled("#confirm-brief", studioState.matchAssessment?.verdict !== "ready" || studioState.opportunities.length === 0 || studioState.busy);
  setDisabled("#make-drafts", !studioState.brief || Number(studioState.brief.version || 1) > 1 || studioState.busy);
  setDisabled("#confirm-revision-proposal", !canConfirmRevisionProposal(studioState.revisionProposal) || studioState.busy);
  setDisabled("#make-revision-drafts", !studioState.brief || Number(studioState.brief.version || 0) < 2 || studioState.drafts.length > 0 || studioState.busy);
  const referenceBusy = Boolean(studioState.reference.busyAction);
  setDisabled('#reference-upload-form button[type="submit"]', referenceBusy);
  setDisabled("#reference-file", referenceBusy);
  setDisabled('#reference-upload-form [name="sourceKind"]', referenceBusy);
  setDisabled("#refresh-reference-history", referenceBusy);
  setDisabled("#reference-history-source-kind", referenceBusy);
  setDisabled("#declare-reference-rights", !studioState.reference.sample || referenceBusy);
  setDisabled("#analyze-reference", !studioState.reference.rights || referenceBusy);
  setDisabled("#confirm-reference-style", !studioState.reference.analysis || studioState.reference.selectedRuleIds.length === 0 || referenceBusy);
  setDisabled("#create-reference-mapping", studioState.reference.selection?.status !== "confirmed" || referenceBusy);
  setDisabled("#confirm-reference-mapping", studioState.reference.mappingPlan?.status !== "draft" || referenceBusy);
  setDisabled(
    "#render-reference-drafts",
    studioState.reference.mappingPlan?.status !== "confirmed" || referenceBusy ||
      referenceReviewInvalidated(studioState.reference),
  );
  renderStudioAppChrome();
}

function renderReferenceHistory(assets) {
  if (!Array.isArray(assets) || assets.length === 0) {
    return '<p class="empty-state">尚无可选的授权历史成片。只显示当前客户过去会话中可查看的视频。</p>';
  }
  return assets.map((asset) => `<article class="reference-history-item">
    <div><p><strong>${escapeHtml(asset.fileName)}</strong></p><small>${escapeHtml(formatReferenceBytes(asset.byteLength))} · ${escapeHtml(asset.capturedAt || asset.importedAt || "时间未记录")} · 查看权限 ${escapeHtml(asset.viewPermission)}</small></div>
    <button class="secondary-action" type="button" data-select-reference-history="${escapeAttribute(asset.assetId)}">选这条</button>
  </article>`).join("");
}

function reconcileReferenceSample(container, sample) {
  const identity = sample
    ? canonicalReferenceJson(referenceIdentity(sample, ["sourceDigest", "sourceKind", "collectionMode"]))
    : "reference-sample:none";
  if (container.dataset.mediaIdentity === identity) return;
  container.innerHTML = renderReferenceSample(sample);
  container.dataset.mediaIdentity = identity;
}

function renderReferenceSample(sample) {
  if (!sample) return '<p class="empty-state">样片入库后，这里会显示安全预览和范围标识。</p>';
  const sourceLabel = sample.sourceKind === "self-owned" ? "客户自有" : "外部样片 · 只学风格";
  const collectionLabel = sample.collectionMode === "historical-pointer" ? "历史成片指针" : "参考专用上传";
  const mediaUrl = `/api/studio/reference-samples/${encodeURIComponent(sample.id)}/media`;
  return `<video controls preload="metadata" src="${escapeAttribute(mediaUrl)}">你的浏览器暂不能播放这条样片。</video>
    <div class="reference-sample-meta">
      <div class="card-heading"><strong>${escapeHtml(sourceLabel)}</strong><span class="gate-chip is-ready">已入库</span></div>
      <p>${escapeHtml(collectionLabel)} · V${escapeHtml(sample.version)}</p>
      <p>查看 ${escapeHtml(sample.viewPermission)} · 使用 ${escapeHtml(sample.usePermission)} · ${escapeHtml(sample.candidateScope)}</p>
      <details><summary>查看样片版本绑定</summary><code>${escapeHtml(sample.id)}</code><br><small>${escapeHtml(sample.sourceDigest)}</small></details>
    </div>`;
}

function renderReferenceAnalysis(analysis) {
  if (!analysis) return '<p class="empty-state">尚未分析样片。</p>';
  const durationMs = referenceAnalysisDuration(analysis);
  const sections = Array.isArray(analysis.sections) ? analysis.sections : [];
  const shotCount = sections.length;
  const sectionDurationMs = sections.reduce((total, section) =>
    total + Math.max(0, Number(section.endMs || 0) - Number(section.startMs || 0)), 0);
  const averageShotMs = shotCount > 0 ? Math.round(sectionDurationMs / shotCount) : 0;
  const supportedTransitions = [...new Set((analysis.transitions || [])
    .filter((transition) => transition.kind !== "unsupported")
    .map((transition) => transition.kind))];
  const warnings = Array.isArray(analysis.warnings) ? analysis.warnings : [];
  const capability = analysis.capability || {};
  return `<article class="reference-analysis-card">
    <div class="card-heading"><strong>风格拆解卡 · V${escapeHtml(analysis.version || 1)}</strong><span class="analysis-status status-${analysis.status === "complete" ? "succeeded" : "failed-manual-review"}">${escapeHtml(analysis.status)}</span></div>
    <div class="reference-capability"><span class="offline-badge">${escapeHtml(capability.label || "capability-unavailable")}</span><code>${escapeHtml(capability.mode || "mode-unavailable")}</code><code>${escapeHtml(capability.provider ?? "provider:null")}</code><code>v${escapeHtml(capability.version ?? "?")}</code></div>
    <div class="reference-metric-grid">
      <div class="reference-metric"><strong>${escapeHtml(formatReferenceDuration(durationMs))}</strong><small>样片时间范围</small></div>
      <div class="reference-metric"><strong>${escapeHtml(shotCount)}</strong><small>镜头数</small></div>
      <div class="reference-metric"><strong>${escapeHtml(formatReferenceDuration(averageShotMs))}</strong><small>平均镜头时长</small></div>
      <div class="reference-metric"><strong>${escapeHtml(Math.round(Number(analysis.confidence || 0) * 100))}%</strong><small>分析把握程度</small></div>
    </div>
    ${renderReferenceTimeline(analysis, durationMs)}
    <div class="reference-report-grid">
      <div class="reference-report-section"><strong>叙事段落</strong><p>${escapeHtml((analysis.sections || []).length)} 段</p></div>
      <div class="reference-report-section"><strong>基础转场</strong><p>${renderInlineValues(supportedTransitions, "未证实可执行转场")}</p></div>
      <div class="reference-report-section"><strong>音频证据</strong><p>${escapeHtml((analysis.beatMarkers || []).length)} 个节拍位置 · ${escapeHtml((analysis.transcriptSpans || []).length)} 段绑定转写</p></div>
      <div class="reference-report-section"><strong>可选规则</strong><p>${escapeHtml((analysis.rules || []).length)} 条</p></div>
    </div>
    ${warnings.length ? `<div><strong>服务端能力警告（原文）</strong><ul class="reference-warning-list">${warnings.map((warning) => `<li><code>${escapeHtml(warning)}</code></li>`).join("")}</ul></div>` : '<p class="empty-state">服务端未返回能力警告。</p>'}
  </article>`;
}

function renderReferenceTimeline(analysis, durationMs) {
  const sections = Array.isArray(analysis.sections) ? analysis.sections : [];
  const segments = sections.length ? sections : [{ id: "full", startMs: 0, endMs: Math.max(1, durationMs), summary: "整体" }];
  const total = Math.max(1, durationMs);
  return `<div class="reference-timeline" role="img" aria-label="按时长归一化的样片段落">${segments.map((segment) => {
    const width = Math.max(2, Math.min(100, (Number(segment.endMs - segment.startMs) / total) * 100));
    return `<span style="flex-basis:${width.toFixed(2)}%" title="${escapeAttribute(`${segment.summary} ${formatStudioInterval(segment.startMs, segment.endMs)}`)}"></span>`;
  }).join("")}</div><small>时间条已按样片总时长归一化；每段宽度代表相对时长。</small>`;
}

function renderReferenceRuleChoices(analysis, selectedRuleIds) {
  if (!analysis) return '<legend>选择这次要学习的规则</legend><p class="empty-state">分析后才能选择。</p>';
  const selected = new Set(selectedRuleIds || []);
  const kindLabels = { rhythm: "节奏", transition: "转场", narrative: "叙事", audio: "音频" };
  return `<legend>选择这次要学习的规则</legend>${(analysis.rules || []).map((rule) => `<label>
    <input type="checkbox" data-reference-rule-id="${escapeAttribute(rule.id)}"${selected.has(rule.id) ? " checked" : ""}>
    <span><strong>${escapeHtml(kindLabels[rule.kind] || rule.kind)}</strong> · ${escapeHtml(rule.description)}<br><small>${formatStudioInterval(rule.referenceStartMs, rule.referenceEndMs)}</small></span>
  </label>`).join("")}`;
}

function renderReferenceMapping(mappingPlan) {
  if (!mappingPlan) return '<p class="empty-state">先确认风格规则。</p>';
  const labels = { matched: "已匹配", substituted: "相近替代", unachievable: "无法实现" };
  return `<div class="card-heading"><strong>匹配计划 V${escapeHtml(mappingPlan.version)} · ${escapeHtml(mappingPlan.status)}</strong><span>${escapeHtml(mappingPlan.unresolvedCount)} 条无法实现</span></div>
    <div class="reference-mapping-list">${(mappingPlan.mappings || []).map((mapping) => `<article class="reference-mapping-row">
      <span class="mapping-status mapping-${escapeAttribute(mapping.status)}">${escapeHtml(labels[mapping.status] || mapping.status)}</span>
      <div><strong>${escapeHtml(mapping.targetRule)}</strong><p>参考段 ${formatStudioInterval(mapping.referenceStartMs, mapping.referenceEndMs)}</p>
      ${mapping.sourceAssetId ? `<p>客户素材 <code>${escapeHtml(mapping.sourceAssetId)}</code> · ${formatStudioInterval(mapping.sourceStartMs, mapping.sourceEndMs)}</p><small>来源 ${escapeHtml(mapping.sourceProvenance)} · 查看 ${escapeHtml(mapping.permission?.view)} · 使用 ${escapeHtml(mapping.permission?.use)}</small>` : '<p class="empty-state">没有素材被冒充为匹配结果。</p>'}
      ${mapping.differenceReason ? `<p><strong>差异说明：</strong>${escapeHtml(mapping.differenceReason)}</p>` : ""}</div>
    </article>`).join("")}</div>`;
}

function renderReferenceDraftCards(drafts) {
  if (!Array.isArray(drafts) || drafts.length === 0) return '<p class="empty-state">匹配计划经人工确认后才能生成。</p>';
  const statusLabels = { reviewable: "待人工审片", approved: "已批准", invalidated: "已失效" };
  const busy = Boolean(studioState.reference.busyAction);
  return `<div class="reference-drafts-grid">${drafts.map((draft) => {
    const status = draft.status || "reviewable";
    const invalidated = status === "invalidated" || !draft.mediaUrl;
    const control = studioState.reference.draftControls?.[draft.kind] || { prompt: "", confirmed: false };
    const promptDisabled = invalidated || busy;
    const approvalDisabled = invalidated || status === "approved" || busy || control.confirmed !== true;
    return `<article class="reference-variant-card status-${escapeAttribute(status)}">
      <div class="card-heading"><h4>${escapeHtml(draft.label)}</h4><span class="gate-chip ${status === "approved" ? "is-ready" : status === "invalidated" ? "is-blocked" : ""}">${escapeHtml(statusLabels[status] || status)}</span></div>
      ${invalidated ? '<div class="reference-media-unavailable" role="status">当前版本已不可播放。</div>' : `<video controls preload="metadata" src="${escapeAttribute(draft.mediaUrl)}">你的浏览器暂不能播放这个版本。</video>`}
      <label class="reference-revision-field">针对这一版的修改指令
        <textarea rows="2" maxlength="120" data-reference-revision-prompt="${escapeAttribute(draft.draftId)}"${promptDisabled ? " disabled" : ""}>${escapeHtml(control.prompt)}</textarea>
      </label>
      <button class="secondary-action" type="button" data-reference-revise="${escapeAttribute(draft.draftId)}"${promptDisabled || !String(control.prompt || "").trim() ? " disabled" : ""}>仅改剪这一版</button>
      <label class="reference-version-confirm"><input type="checkbox" data-reference-approval-confirm="${escapeAttribute(draft.draftId)}"${control.confirmed ? " checked" : ""}${invalidated || status === "approved" || busy ? " disabled" : ""}> 我已确认正在审批当前精确版本</label>
      <button class="primary-action" type="button" data-reference-approve="${escapeAttribute(draft.draftId)}"${approvalDisabled ? " disabled" : ""}>批准这一版</button>
      <p class="micro-boundary">${escapeHtml(statusLabels[status] || status)} · publishClaimed=false</p>
      <code>${escapeHtml(draft.draftId)}</code><small>V${escapeHtml(draft.draftVersion ?? draft.version)} · 输出摘要 ${escapeHtml(draft.outputDigest)}</small>
    </article>`;
  }).join("")}</div>`;
}

function reconcileReferenceDrafts(container, drafts) {
  const identity = canonicalReferenceJson({
    report: referenceIdentity(studioState.reference.executionReport),
    busyAction: studioState.reference.busyAction,
    drafts: (drafts || []).map((draft) => ({
      ...referenceDraftBinding(draft),
      mediaUrl: draft.mediaUrl || null,
      status: draft.status || "reviewable",
      control: studioState.reference.draftControls?.[draft.kind] || null,
    })),
  });
  if (container.dataset.mediaIdentity === identity) return;
  container.innerHTML = renderReferenceDraftCards(drafts);
  container.dataset.mediaIdentity = identity;
}

function renderReferenceExecutionReport(report) {
  if (!report) return '<p class="empty-state">还没有执行报告。</p>';
  const dimensions = [
    ["节奏", report.rhythm], ["转场", report.transitions],
    ["叙事", report.narrative], ["音频", report.audio],
  ];
  const statusLabels = { matched: "已执行", substituted: "部分执行 / 相近替代", unachievable: "未执行 / 无法实现" };
  const bindingRows = ["sample", "rights", "analysis", "styleSelection", "mappingPlan"]
    .map((key) => report.bindings?.[key] ? `${key}:${report.bindings[key].id}@${report.bindings[key].version}` : null)
    .filter(Boolean);
  return `<div class="card-heading"><strong>执行报告 V${escapeHtml(report.version)}</strong><span class="gate-chip ${report.status === "reviewable" ? "is-ready" : "is-blocked"}">${escapeHtml(report.status)}</span></div>
    <div class="reference-report-grid">${dimensions.map(([label, value]) => `<article class="reference-report-section"><strong>${label}</strong><p>${escapeHtml(statusLabels[value?.status] || value?.status || "unknown")}</p><code>${escapeHtml(value?.summaryCode || "summary-unavailable")}</code>${renderReferenceEvidenceRefs(value?.evidenceRefs)}</article>`).join("")}</div>
    <div class="reference-report-section"><strong>匹配结果</strong><p>已匹配 ${escapeHtml(report.mappingCounts?.matched || 0)} · 相近替代 ${escapeHtml(report.mappingCounts?.substituted || 0)} · 无法实现 ${escapeHtml(report.mappingCounts?.unachievable || 0)}</p></div>
    <h4>平台主动调整</h4><div class="reference-platform-changes">${(report.platformChanges || []).map((change) => `<article class="reference-report-section"><strong>${escapeHtml(({ xiaohongshu: "小红书版", douyin: "抖音版", "wechat-channels": "视频号版" })[change.kind] || change.kind)}</strong>${renderValueList(change.changeCodes, "服务端未返回调整项")}</article>`).join("")}</div>
    <details class="reference-version-bindings"><summary>查看本次生成的版本绑定</summary><p>${bindingRows.map(escapeHtml).join(" · ")}</p><code>${escapeHtml(report.id)}</code><p>publishClaimed=${escapeHtml(report.publishClaimed)}</p></details>`;
}

function renderReferenceEvidenceRefs(refs) {
  const values = Array.isArray(refs) ? refs : [];
  return values.length ? `<small>${values.map(escapeHtml).join(" · ")}</small>` : "";
}

function renderReferenceGateState() {
  const reference = studioState.reference;
  const invalidated = referenceReviewInvalidated(reference);
  const renderReady = reference.executionReport?.status === "reviewable" &&
    reference.drafts.length === 4 &&
    reference.drafts.every((draft) => draft.status !== "invalidated");
  setReferenceGate("#reference-sample-gate", reference.sample ? "已选样片" : "待选择", Boolean(reference.sample));
  setReferenceGate("#reference-analysis-gate", reference.analysis ? "已得风格卡" : reference.rights ? "待分析" : "待声明", Boolean(reference.analysis));
  setReferenceGate("#reference-mapping-gate", reference.mappingPlan?.status === "confirmed" ? "已确认匹配" : reference.selection ? "待复核" : "待风格确认", reference.mappingPlan?.status === "confirmed");
  setReferenceGate(
    "#reference-render-gate",
    invalidated ? "版本已失效" : renderReady ? "四版已生成" : "待匹配确认",
    renderReady,
  );
  const generation = document.querySelector("#reference-generation-state");
  if (generation) generation.textContent = invalidated
    ? "四版已失效，播放与审批已关闭"
    : renderReady
      ? "四个待审视频已生成，尚未对外发布"
      : "尚未开始生成视频";
}

function referenceReviewInvalidated(reference) {
  return reference.executionReport?.status === "invalidated" ||
    reference.drafts.some((draft) => draft.status === "invalidated");
}

function setReferenceGate(selector, text, ready) {
  const gate = document.querySelector(selector);
  if (!gate) return;
  gate.textContent = text;
  gate.className = `gate-chip${ready ? " is-ready" : ""}`;
}

function syncReferenceRightsControls() {
  const sample = studioState.reference.sample;
  const sourceKind = document.querySelector("#reference-rights-source-kind");
  const reuse = document.querySelector("#reference-reuse-elements");
  if (sourceKind) {
    sourceKind.textContent = sample
      ? sample.sourceKind === "self-owned" ? "客户自有样片" : "外部样片，只学风格"
      : "选择样片后显示";
  }
  if (reuse) {
    reuse.disabled = !sample || sample.sourceKind !== "self-owned" || Boolean(studioState.reference.busyAction);
    if (sample?.sourceKind !== "self-owned") {
      for (const input of reuse.querySelectorAll('input[name="reusableElements"]')) input.checked = false;
    }
  }
}

function referenceAnalysisDuration(analysis) {
  return Math.max(0,
    ...(analysis.cuts || []),
    ...(analysis.beatMarkers || []),
    ...(analysis.sections || []).map((item) => Number(item.endMs || 0)),
    ...(analysis.transcriptSpans || []).map((item) => Number(item.endMs || 0)),
    ...(analysis.transitions || []).map((item) => Number(item.atMs || 0)),
    ...(analysis.rules || []).map((item) => Number(item.referenceEndMs || 0)));
}

function formatReferenceDuration(milliseconds) {
  const value = Math.max(0, Number(milliseconds || 0));
  return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)} 秒`;
}

function formatReferenceBytes(bytes) {
  const value = Math.max(0, Number(bytes || 0));
  return value >= 1_048_576 ? `${(value / 1_048_576).toFixed(1)} MB` : `${Math.round(value / 1024)} KB`;
}

function renderStudioDemand(demand) {
  if (!demand) return '<p class="empty-state">还没有提交这次需求。</p>';
  return `<article class="decision-card">
    <div class="card-heading"><strong>需求 V${escapeHtml(demand.version)}</strong><span>${escapeHtml(demand.status)}</span></div>
    <p><strong>想讲清楚：</strong>${escapeHtml(demand.goal)}</p>
    <p><strong>希望谁看：</strong>${escapeHtml(demand.targetAudience)}</p>
    <p><strong>期待效果：</strong>${escapeHtml(demand.desiredEffect)}</p>
    <details><summary>查看必须出现与避免内容</summary><p>必须出现：${renderInlineValues(demand.mustInclude, "无")}</p><p>不能出现：${renderInlineValues(demand.mustAvoid, "无")}</p></details>
  </article>`;
}

function studioMaterialCategoryLabel(contentType) {
  return studioMaterialCategories.find((category) => category.value === contentType)?.label || "其他";
}

function formatStudioUploadDate(value) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return "日期待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    timeZone: "Asia/Taipei",
  }).format(new Date(timestamp));
}

function materialTagIdsForCard(materialCardId) {
  return studioState.materialTags?.assignments?.find((assignment) =>
    assignment.materialCardId === materialCardId)?.tagIds || [];
}

function customMaterialFilterValue(tagId) {
  return `custom:${tagId}`;
}

function filteredStudioMaterials(materials) {
  if (studioMaterialFilter.startsWith("custom:")) {
    const tagId = studioMaterialFilter.slice("custom:".length);
    return materials.filter((item) => materialTagIdsForCard(item.id).includes(tagId));
  }
  return studioMaterialFilter === "all"
    ? materials
    : materials.filter((item) => item.contentType === studioMaterialFilter);
}

function renderMaterialFilters(materials) {
  const customTags = studioState.materialTags?.tags || [];
  return `<nav class="material-filter-bar" aria-label="按标签筛选素材">
    <div class="material-filter-chips">${studioMaterialCategories.map((category) => {
      const count = category.value === "all"
        ? materials.length
        : materials.filter((item) => item.contentType === category.value).length;
      const selected = studioMaterialFilter === category.value;
      return `<button class="material-filter-chip${selected ? " is-selected" : ""}" type="button" data-material-filter="${escapeAttribute(category.value)}" aria-pressed="${selected}">${escapeHtml(category.label)} <span>${count}</span></button>`;
    }).join("")}</div>
    <div class="custom-tag-manager">
      ${customTags.length
        ? `<div class="material-filter-chips custom-filter-chips">${customTags.map((tag) => {
            const filterValue = customMaterialFilterValue(tag.id);
            const count = materials.filter((item) => materialTagIdsForCard(item.id).includes(tag.id)).length;
            const selected = studioMaterialFilter === filterValue;
            return `<button class="material-filter-chip custom-filter-chip${selected ? " is-selected" : ""}" type="button" data-material-filter="${escapeAttribute(filterValue)}" aria-pressed="${selected}">${escapeHtml(tag.label)} <span>${count}</span></button>`;
          }).join("")}</div>`
        : ""}
      <details class="custom-tag-create">
        <summary>＋ 新建标签</summary>
        <form class="custom-tag-form" data-material-tag-form>
          <label><span class="sr-only">新标签名称</span><input name="label" maxlength="20" placeholder="例如：十月拍摄" required></label>
          <button type="submit">添加</button>
        </form>
      </details>
    </div>
  </nav>`;
}

function renderMaterials(materials) {
  if (!materials.length) return studioState.busy && !studioState.inventory
    ? '<div class="simple-empty-state" role="status"><strong>演示内容正在准备</strong><p>准备好后会显示在这里，第一次打开请稍等；现在不用上传真实素材。</p></div>'
    : '<div class="simple-empty-state"><strong>先添加你的第一段视频</strong><p>在上方选择视频并上传，成功后就能在这里播放。</p></div>';
  const visibleMaterials = filteredStudioMaterials(materials);
  const filterBar = renderMaterialFilters(materials);
  if (!visibleMaterials.length) {
    return `${filterBar}<div class="simple-empty-state"><strong>这个标签下还没有素材</strong><p>可以选择其他标签，或继续上传新视频。</p><button type="button" data-material-filter="all">查看全部素材</button></div>`;
  }
  return `${filterBar}<div class="material-grid">${visibleMaterials.map((item, index) => {
      const observations = item.observations || [];
      const pendingFacts = item.pendingFacts || [];
      const duration = Math.max(0, Number(item.sourceEndMs || 0) - Number(item.sourceStartMs || 0));
      const durationLabel = duration >= 60_000
        ? `${Math.floor(duration / 60_000)}分${Math.round((duration % 60_000) / 1_000)}秒`
        : `${Math.max(1, Math.round(duration / 1_000))}秒`;
      const contentType = studioMaterialCategoryLabel(item.contentType);
      const assignedTagIds = materialTagIdsForCard(item.id);
      const customTags = (studioState.materialTags?.tags || []).filter((tag) => assignedTagIds.includes(tag.id));
      const status = item.technicalUsable ? "可正常播放" : "播放待确认";
      const title = item.displayName || observations[0] || `素材 ${index + 1}`;
      const uploadedAt = formatStudioUploadDate(item.uploadedAt || item.createdAt);
      const uploadSource = item.uploadSource || "已有素材";
      const libraryPath = item.libraryPath || `素材库 / ${uploadSource}`;
      const mediaUrl = item.mediaUrl || `/api/studio/materials/${encodeURIComponent(item.sourceAssetId)}/media`;
      return `<article class="material-card material-card-simple">
        <div class="material-preview-wrap">
          <video class="material-preview" controls playsinline preload="metadata" aria-label="播放 ${escapeAttribute(title)}" src="${escapeAttribute(`${mediaUrl}#t=0.1`)}"></video>
        </div>
        <div class="material-card-body">
          <div class="material-card-copy">
            <div class="material-title-row"><h3>${escapeHtml(title)}</h3>${item.technicalUsable ? "" : `<span class="material-status">${status}</span>`}</div>
            <p class="material-origin"><span>${escapeHtml(uploadedAt)}</span><span>${escapeHtml(uploadSource)}</span></p>
            <p class="material-card-meta"><span class="material-type-tag">${escapeHtml(contentType)}</span>${customTags.map((tag) => `<span class="material-type-tag is-custom">${escapeHtml(tag.label)}</span>`).join("")}<span>${durationLabel}</span></p>
          </div>
          <div class="material-card-actions">
            <details class="material-details material-card-more">
              <summary>整理</summary>
              <p class="material-library-path">路径：${escapeHtml(libraryPath)}</p>
              ${(studioState.materialTags?.tags || []).length
                ? `<fieldset><legend>这条素材属于哪些标签？</legend>${studioState.materialTags.tags.map((tag) => `<label><input type="checkbox" data-material-tag-id="${escapeAttribute(tag.id)}" data-material-card-id="${escapeAttribute(item.id)}"${assignedTagIds.includes(tag.id) ? " checked" : ""}> ${escapeHtml(tag.label)}</label>`).join("")}</fieldset>`
                : '<p>需要分类时，先在上方新建标签。</p>'}
              ${pendingFacts.length ? `<fieldset class="fact-checklist"><legend>这些细节要放进内容里吗？（可选）</legend>${pendingFacts.map((fact) => `<label><input type="checkbox" data-studio-fact-id="${escapeAttribute(fact)}"${studioState.selectedFactIds.includes(fact) ? " checked" : ""}> ${escapeHtml(fact)}</label>`).join("")}</fieldset>` : ""}
              <p>素材区间：${Number(item.sourceStartMs || 0) / 1000}—${Number(item.sourceEndMs || 0) / 1000} 秒</p>
              ${item.technicalFlags?.length ? `<p>需要留意：${escapeHtml(item.technicalFlags.join("、"))}</p>` : ""}
              ${observations.length > 1 ? `<p>识别到的内容：${escapeHtml(observations.slice(1).join("、"))}</p>` : ""}
              ${item.capability?.mode === "offline-fallback" ? '<p>当前是离线模拟分析，不代表正式模型结论。</p>' : ""}
              <button class="primary-action material-analyze" type="button" data-start-analysis="${escapeAttribute(item.sourceAssetId)}">检查这段素材</button>
            </details>
          </div>
        </div>
      </article>`;
    }).join("")}</div>`;
}

function renderAnalysisQueue(history) {
  const entries = history?.entries || [];
  const counts = entries.reduce((value, entry) => {
    const status = entry.job?.status || "unknown";
    value[status] = (value[status] || 0) + 1;
    return value;
  }, {});
  if (!entries.length) return '<p class="empty-state">分析队列为空。请从某条已授权素材开始。</p>';
  return `<article class="decision-card"><strong>分析队列与状态</strong><p>共 ${escapeHtml(entries.length)} 个：${Object.entries(counts).map(([status, count]) => `${escapeHtml(analysisStatusLabel(status))} ${escapeHtml(count)}`).join("、")}</p></article>`;
}

function renderAnalysisHistory(history) {
  const entries = history?.entries || [];
  if (!entries.length) return '<p class="empty-state">尚无可复核的分析记录。</p>';
  return `${renderTopLevelDuplicateGroups(history?.duplicateGroups || [], entries)}${entries.map((entry) => renderAnalysisCard(entry, history?.suitabilityContext || null)).join("")}${renderAnalysisReviewHistory(history?.reviews || [])}${renderMaterialSuitabilityHistory(history?.suitabilityReviews || [])}`;
}

function renderTopLevelDuplicateGroups(groups, entries) {
  if (!groups.length) return "";
  return `<section class="history-group"><h3>重复组概览</h3><ol>${groups.map((group) => {
    const receipt = entries.flatMap((entry) => entry.receipts || []).find((candidate) => candidate.jobId === group.receiptJobId);
    const action = receipt ? `<button type="button" class="secondary-action" data-analysis-review-action="keep-from-duplicate-group" data-receipt-job-id="${escapeAttribute(receipt.jobId)}" data-review-target="${escapeAttribute(group.id)}">人工选择保留</button>` : "";
    return `<li>${escapeHtml(group.kind || "疑似重复")} · ${renderInlineValues(group.sourceAssetIds, "无素材")}${action}</li>`;
  }).join("")}</ol></section>`;
}

function renderAnalysisCard(entry, suitabilityContext = null) {
  const job = entry?.job || {};
  const receipts = Array.isArray(entry?.receipts) ? entry.receipts : [];
  const successful = receipts.filter((receipt) => receipt.status === "succeeded");
  const failedReceipts = receipts.filter((receipt) => receipt.status !== "succeeded");
  const technical = successful.find((receipt) => receipt.layer === "technical");
  const transcript = successful.find((receipt) => receipt.layer === "transcript");
  const transcriptSpans = Array.isArray(transcript?.qualitySignals?.spans) ? transcript.qualitySignals.spans : [];
  const suggestions = successful.flatMap((receipt) => (receipt.suggestedTags || []).map((target) => ({ receipt, target })));
  const observations = successful.flatMap((receipt) => receipt.observations || []);
  const uncertainties = successful.flatMap((receipt) => arraySignal(receipt, "uncertainty"));
  const matches = successful.flatMap((receipt) => arraySignal(receipt, "demandEvidenceCandidates"));
  const mismatches = successful.flatMap((receipt) => arraySignal(receipt, "forbiddenContentWarnings"));
  const groups = successful.flatMap((receipt) => arraySignal(receipt, "duplicateGroups").map((group) => ({ receipt, group })));
  const identities = successful.map((receipt) => `${receipt.modelName || receipt.adapterName || "未知本地模型"} · ${receipt.modelVersion || "未知版本"}`);
  const interval = formatStudioInterval(job.sourceStartMs, job.sourceEndMs);
  const degraded = entry?.evidence?.degradedReasons || [];
  const suitabilityReceipt = successful.find((receipt) => receipt.layer === "vision") || successful[0] || null;
  const failureCode = job.failure?.code || null;
  return `<article class="analysis-card">
    <div class="card-heading"><strong>分析 ${escapeHtml(job.sourceAssetId || "未知素材")}</strong><span class="analysis-status status-${escapeAttribute(job.status || "unknown")}">${escapeHtml(analysisStatusLabel(job.status))}</span></div>
    <p><strong>来源时间：</strong>${interval}</p>
    <section><h3>技术质量</h3>${technical ? `<p>画面 ${escapeHtml(technical.qualitySignals?.hasVideo === true ? "可见" : "未确认")} · 声音 ${escapeHtml(technical.qualitySignals?.hasAudio === true ? "可见" : "未确认")} · ${escapeHtml(technical.qualitySignals?.width || "?")}×${escapeHtml(technical.qualitySignals?.height || "?")}</p>` : '<p class="empty-state">技术分析未完成。</p>'}</section>
    <section><h3>转写</h3>${transcriptSpans.length ? `<ol>${transcriptSpans.map((span) => `<li>${formatStudioInterval(span.startMs, span.endMs)} · ${escapeHtml(span.text)}</li>`).join("")}</ol>` : '<p class="empty-state">没有可展示的绑定转写。</p>'}</section>
    <section><h3>模型建议 · 待人工确认</h3>${suggestions.length ? `<ul>${suggestions.map(({ receipt, target }) => `<li>${escapeHtml(target)}${renderSuggestionReviewActions(receipt, target)}</li>`).join("")}</ul>` : '<p class="empty-state">暂无建议标签。</p>'}${renderValueList(observations, "暂无其他模型观察。")}</section>
    <section><h3>不确定性</h3>${renderSignalValues(uncertainties, "暂无已记录的不确定项。")}</section>
    <section><h3>重复组</h3>${groups.length ? `<ul>${groups.map(({ receipt, group }) => `<li>${escapeHtml(signalText(group))}<button type="button" class="secondary-action" data-analysis-review-action="keep-from-duplicate-group" data-receipt-job-id="${escapeAttribute(receipt.jobId)}" data-review-target="${escapeAttribute(group.id || signalText(group))}">保留这份原片</button></li>`).join("")}</ul>` : '<p class="empty-state">未发现已绑定的疑似重复组。</p>'}</section>
    <section><h3>需求匹配与原因</h3><h4>匹配</h4>${renderSignalValues(matches, "暂无模型匹配证据；以上方需求评估为准。")}<h4>不匹配或避免内容</h4>${renderSignalValues(mismatches, "暂无模型警示。")}</section>
    <section><h3>素材是否合适</h3>${suitabilityReceipt && suitabilityContext ? `<p>这是针对当前需求的独立人工结论，不会冒充标签复核。</p><div class="suggestion-actions"><button type="button" class="secondary-action" data-material-suitability="suitable" data-receipt-job-id="${escapeAttribute(suitabilityReceipt.jobId)}" data-demand-id="${escapeAttribute(suitabilityContext.demandId)}" data-match-assessment-id="${escapeAttribute(suitabilityContext.matchAssessmentId)}">适合当前需求</button><button type="button" class="secondary-action" data-material-suitability="not-suitable" data-receipt-job-id="${escapeAttribute(suitabilityReceipt.jobId)}" data-demand-id="${escapeAttribute(suitabilityContext.demandId)}" data-match-assessment-id="${escapeAttribute(suitabilityContext.matchAssessmentId)}">不适合当前需求</button></div>` : '<p class="empty-state">需要当前需求、匹配评估与成功回执三者精确绑定后才能反馈。</p>'}</section>
    <details><summary>查看本地模型身份与降级原因</summary><p><strong>本地模型：</strong>${renderInlineValues(identities, "尚无成功的本地模型回执")}</p>${failureCode ? `<p><strong>任务降级原因：</strong>${escapeHtml(failureCode)}</p>` : ""}${failedReceipts.map((receipt) => `<p><strong>失败回执：</strong>${escapeHtml(receipt.adapterName || "unknown-adapter")} · ${escapeHtml(receipt.modelName || "unknown-model")} · ${escapeHtml(receipt.modelVersion || "unknown-version")} · ${escapeHtml(receipt.failure?.code || "unknown-failure")}</p>`).join("")}${renderSignalValues(degraded, "未记录其他降级原因。")}</details>
  </article>`;
}

function renderAnalysisReviewHistory(reviews) {
  if (!reviews.length) return '<p class="empty-state">尚无人工复核记录。</p>';
  return `<section class="history-group"><h3>人工复核历史</h3><ol>${reviews.map((review) => `<li>${escapeHtml(review.action)} · ${escapeHtml(review.target)}${review.correction ? ` → ${escapeHtml(review.correction)}` : ""}<br><small>${formatStudioInterval(review.sourceStartMs, review.sourceEndMs)} · ${escapeHtml(review.receiptJobId)}</small></li>`).join("")}</ol></section>`;
}

function renderMaterialSuitabilityHistory(reviews) {
  if (!reviews.length) return "";
  return `<section class="history-group"><h3>素材对当前需求的人工结论</h3><ol>${reviews.map((review) => `<li>${escapeHtml(review.decision === "suitable" ? "适合" : "不适合")} · ${escapeHtml(review.reason)}<br><small>${formatStudioInterval(review.sourceStartMs, review.sourceEndMs)} · ${escapeHtml(review.demandId)} · ${escapeHtml(review.matchAssessmentId)}</small></li>`).join("")}</ol></section>`;
}

function renderSuggestionReviewActions(receipt, target) {
  const binding = ` data-receipt-job-id="${escapeAttribute(receipt.jobId)}" data-review-target="${escapeAttribute(target)}"`;
  return `<span class="suggestion-actions"><button type="button" class="secondary-action" data-analysis-review-action="accept-suggestion"${binding}>标签正确</button><button type="button" class="secondary-action" data-analysis-review-action="correct-suggestion"${binding}>修正标签</button><button type="button" class="secondary-action" data-analysis-review-action="reject-suggestion"${binding}>标签不正确</button></span>`;
}

function renderModelStatus(status) {
  if (!status) return '<p class="empty-state">点击刷新后查看本地模型身份与降级状态。</p>';
  const artifacts = status.artifacts || [];
  const ready = artifacts.filter((artifact) => artifact.status === "ready");
  const unavailable = artifacts.filter((artifact) => artifact.status !== "ready");
  const label = ready.length
    ? unavailable.length ? "基础能力可用，部分辅助能力未启用" : "本地基础能力可用"
    : "本地模型暂不可用";
  const probeText = String(status.visionProbe || "").includes("succeeded")
    ? "已完成合成样例验证；真实素材效果仍需人工验收。"
    : "还没有完成可用的合成样例验证；AI 结果必须人工检查。";
  return `<article class="decision-card"><div class="card-heading"><strong>AI 能力状态</strong><span class="offline-badge">${escapeHtml(label)}</span></div><p>${escapeHtml(probeText)}</p><details><summary>查看模型明细</summary>${ready.length ? `<p><strong>可用：</strong>${ready.map((artifact) => escapeHtml(artifact.id)).join("、")}</p>` : '<p>目前没有已确认可用的模型。</p>'}${unavailable.length ? `<p><strong>未启用：</strong>${unavailable.map((artifact) => `${escapeHtml(artifact.id)}（${escapeHtml(artifact.status)}）`).join("、")}</p>` : ""}</details></article>`;
}

function arraySignal(receipt, key) {
  const value = receipt?.qualitySignals?.[key];
  return Array.isArray(value) ? value : [];
}

function renderSignalValues(values, emptyLabel) {
  return values.length ? `<ul>${values.map((value) => `<li>${escapeHtml(signalText(value))}</li>`).join("")}</ul>` : `<p class="empty-state">${escapeHtml(emptyLabel)}</p>`;
}

function signalText(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return String(value || "");
  return [value.requirement, value.reason, value.id, Array.isArray(value.sourceAssetIds) ? value.sourceAssetIds.join("、") : null].filter(Boolean).join("：");
}

function analysisStatusLabel(value) {
  return ({ queued: "等待", running: "分析中", succeeded: "可复核", "failed-retryable": "失败可重试", "failed-manual-review": "需人工处理", cancelled: "已取消" })[value] || "未知状态";
}

function formatStudioInterval(startMs, endMs) {
  const format = (value) => {
    const safe = Number.isFinite(Number(value)) && Number(value) >= 0 ? Math.floor(Number(value)) : 0;
    const minutes = Math.floor(safe / 60000);
    const seconds = Math.floor((safe % 60000) / 1000);
    const milliseconds = safe % 1000;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
  };
  return `${format(startMs)}—${format(endMs)}`;
}

function renderMatchAssessment(assessment) {
  if (!assessment) return '<p class="empty-state">还没有评估这次需求与现有素材。</p>';
  const labels = { ready: "绿灯 · 适合继续", partial: "部分匹配", blocked: "暂不适合" };
  const verdict = ["ready", "partial", "blocked"].includes(assessment.verdict) ? assessment.verdict : "blocked";
  const matches = assessment.matchedEvidence || [];
  const unmet = assessment.unmetNeeds || [];
  const captures = assessment.recommendedCaptures || [];
  return `<article class="verdict-card verdict-${escapeAttribute(verdict)}">
    <div class="card-heading"><strong>匹配结果：${labels[verdict]}</strong><span class="offline-badge">${escapeHtml(assessment.capability?.label || "离线/模拟分析")}</span></div>
    <section><h3>适合的原因</h3>${matches.length ? `<ul>${matches.map((row) => `<li><strong>${escapeHtml(row.requirement)}</strong>：${escapeHtml(row.explanation)}<br><small>证据 ${renderInlineValues(row.materialCardIds, "无")}</small></li>`).join("")}</ul>` : '<p class="empty-state">尚无可以支撑这次需求的明确证据。</p>'}</section>
    <section><h3>不适合或还缺的内容</h3>${renderValueList(unmet, verdict === "ready" ? "没有阻止继续的缺口。" : "尚未说明具体缺口。")}</section>
    <section><h3>可以怎么继续</h3>${renderValueList(captures, verdict === "ready" ? "现有素材够用，不需要补拍。" : "先从历史素材库找；仍不够就缩小范围或暂停，不补拍。")}</section>
    <details><summary>查看安全范围和技术细节</summary><p>${escapeHtml(assessment.safeScope)}</p><p><code>${escapeHtml(assessment.id)}</code></p></details>
  </article>`;
}

function renderOpportunities(opportunities, gaps, gapCapability = null) {
  if (opportunities.length) return opportunities.map((item) => `<article class="decision-card"><div class="card-heading"><strong>可讲：${escapeHtml(item.goal)}</strong><span class="offline-badge">${escapeHtml(item.capability?.label || "离线/模拟分析")}</span></div><p>${escapeHtml(item.rationale)}</p></article>`).join("");
  if (gaps.length) return `<details class="gap-drawer" open><summary>查看还缺什么 ${gapCapability ? `<span class="offline-badge">${escapeHtml(gapCapability.label)}</span>` : ""}</summary>${gaps.map((gap) => `<p><strong>${escapeHtml(gap.missingEvidence)}</strong><br>${escapeHtml(gap.impactWithoutCapture)}<br>${escapeHtml(gap.recommendedCapture)}</p>`).join("")}</details>`;
  return '<p class="empty-state">还没有可讲的内容方向。</p>';
}

function selectedOpportunityMaterials(opportunity, materials) {
  const materialCardIds = new Set(Array.isArray(opportunity?.materialCardIds)
    ? opportunity.materialCardIds
    : []);
  if (!materialCardIds.size) return [];
  return (materials || []).filter((material) => materialCardIds.has(material.id));
}

function renderBrief(brief, opportunity = null, materials = []) {
  if (!brief && opportunity) {
    const selectedMaterials = selectedOpportunityMaterials(opportunity, materials);
    const plannedCount = selectedMaterials.length || opportunity.materialCardIds?.length || 0;
    const rows = selectedMaterials.map((item, index) => {
      const durationMs = Math.max(0, Number(item.sourceEndMs || 0) - Number(item.sourceStartMs || 0));
      const duration = `${Math.max(1, Math.round(durationMs / 1_000))}秒`;
      const contentType = ({
        "single-product": "产品镜头",
        "jewelry-making-process": "制作过程",
        "designer-viewpoint": "设计师表达",
      })[item.contentType] || "视频素材";
      const title = item.displayName || item.observations?.[0] || `素材 ${index + 1}`;
      const mediaUrl = item.mediaUrl || `/api/studio/materials/${encodeURIComponent(item.sourceAssetId)}/media`;
      return `<li><video class="plan-video" controls playsinline preload="metadata" src="${escapeAttribute(mediaUrl)}" aria-label="确认素材 ${escapeAttribute(title)}"></video><strong>${escapeHtml(title)}</strong><small>${escapeHtml(contentType)} · ${duration}</small></li>`;
    }).join("");
    const materialList = rows || `<li><strong>已选好 ${plannedCount} 段素材</strong><small>具体来源将在初稿中保留</small></li>`;
    return `<article class="decision-card material-plan">
      <div class="material-plan-header"><strong>Agent 准备用这 ${plannedCount} 段素材</strong><span class="material-plan-ready">素材够用，不需要再上传</span></div>
      <p><strong>准备做：</strong>${escapeHtml(opportunity.goal || "根据现有素材整理一版内容")}</p>
      <details class="material-plan-details"><summary>为什么选这些素材</summary><p>${escapeHtml(opportunity.rationale || "现有素材可以支持这个方向")}</p></details>
      <ul class="material-plan-list">${materialList}</ul>
      <small>确认后会生成母版、小红书、抖音和视频号初稿，并自动带你进入作品页。</small>
    </article>`;
  }
  if (!brief) return '<p class="empty-state">先点“开始整理”，这里会出现建议做法。</p>';
  return `<article class="decision-card"><div class="card-heading"><strong>任务书 V${escapeHtml(brief.version)}</strong><span class="offline-badge">${escapeHtml(brief.capability?.label || "离线/模拟分析")}</span></div><p>${escapeHtml(brief.goal)}</p><p>选用 ${(brief.selectedAssetIds || []).length} 段素材 · 已确认 ${(brief.confirmedFactIds || []).length} 条事实</p><details><summary>查看人工复核要求</summary><p>${renderInlineValues(brief.reviewRequirements, "无")}</p></details></article>`;
}

function renderMusicSelection(music) {
  const recommendations = music?.recommendations || [];
  const recommendationByTrack = new Map(recommendations.map((item) => [item.trackId, item]));
  const tracks = [...(music?.tracks || [])].sort((left, right) =>
    (recommendationByTrack.get(left.assetId)?.rank ?? 999) - (recommendationByTrack.get(right.assetId)?.rank ?? 999));
  if (!tracks.length) return '<p class="empty-state">未选配乐，将保留原视频声音。</p>';
  return `<div class="music-recommendation-tools"><button type="button" class="quiet-action" data-refresh-music-recommendations ${studioState.brief ? "" : "disabled"}>根据当前内容重新排序</button><small>${studioState.brief ? "排序不会自动替你选歌" : "确认任务书后才能结合内容排序"}</small></div><div class="music-track-list">${tracks.map((track) => {
    const selected = track.assetId === music.selectedTrackId;
    const recommendation = recommendationByTrack.get(track.assetId);
    const publishLabel = track.rights?.publishUseAllowed ? "已声明可发布" : "仅本地预览，发布前需补授权";
    const preference = track.mixPreference || { trackStartMs: 0, originalGainDb: 0, musicGainDb: -16 };
    const profile = track.audioProfile || { energy: "balanced", tempoBpm: null, highlightStartMs: 0, waveform: [], reason: "尚未分析" };
    const energyLabel = ({ soft: "柔和", balanced: "均衡", strong: "有力" })[profile.energy] || "均衡";
    const waveform = (profile.waveform || []).length
      ? `<div class="music-waveform" aria-label="音乐波形">${profile.waveform.map((value) => `<i style="--wave:${Math.max(.08, Number(value || 0)).toFixed(3)}"></i>`).join("")}</div>`
      : '<div class="music-waveform-empty">旧配乐尚未生成波形，重新上传即可分析</div>';
    return `<article class="decision-card music-track-card${selected ? " is-selected" : ""}">
      <div class="card-heading"><strong>${selected ? "已选：" : ""}${escapeHtml(track.fileName)}</strong><span>${Number(track.durationSeconds || 0).toFixed(1)} 秒</span></div>
      ${recommendation ? `<div class="music-recommendation ${recommendation.evidenceMode === "visual-model-supported" ? "model-supported" : "technical-only"}"><strong>推荐第 ${Number(recommendation.rank)} 名 · ${Number(recommendation.score)} 分</strong><span>${recommendation.evidenceMode === "visual-model-supported" ? "视觉模型回执辅助" : "仅技术排序"}</span>${recommendation.preferenceSignal ? `<span>同类视频偏好 ${Number(recommendation.preferenceSignal.preferenceScore) >= 0 ? "+" : ""}${Number(recommendation.preferenceSignal.preferenceScore)} 分</span>` : ""}<ul>${(recommendation.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>${(recommendation.modelReceipts || []).length ? `<small>${recommendation.modelReceipts.map((receipt) => `${escapeHtml(receipt.modelName)} ${escapeHtml(receipt.modelVersion)} · 回执 ${escapeHtml(receipt.receiptJobId)}`).join("；")}</small>` : ""}</div>` : ""}
      <audio controls preload="metadata" src="${escapeAttribute(track.mediaUrl || `/api/studio/music/${encodeURIComponent(track.assetId)}/media`)}">当前浏览器无法试听音频。</audio>
      ${waveform}<div class="music-analysis-summary"><span>${escapeHtml(energyLabel)}</span>${profile.tempoBpm ? `<span>约 ${Number(profile.tempoBpm)} BPM</span>` : ""}<span>推荐起点 ${(Number(profile.highlightStartMs || 0) / 1000).toFixed(1)} 秒</span></div>
      <small>${escapeHtml(profile.reason || "本地音频分析")}</small>
      <p>${escapeHtml(track.rights?.licenseNote || "无授权说明")}</p><small>${escapeHtml(publishLabel)} · 生成人声自动避让混音</small>
      ${selected ? `<form data-music-mix-form data-track-id="${escapeAttribute(track.assetId)}" class="music-mix-form">
        <label>从第几秒开始<input name="trackStartSeconds" type="number" min="0" max="${Math.max(0, Number(track.durationSeconds || 0) - 0.1).toFixed(1)}" step="0.1" value="${(Number(preference.trackStartMs || 0) / 1000).toFixed(1)}"></label>
        <label>原声大小（dB）<input name="originalGainDb" type="number" min="-24" max="6" step="1" value="${Number(preference.originalGainDb ?? 0)}"></label>
        <label>配乐大小（dB）<input name="musicGainDb" type="number" min="-36" max="0" step="1" value="${Number(preference.musicGainDb ?? -16)}"></label>
        <button class="secondary-action" type="submit">保存混音设置</button>
      </form><button type="button" class="quiet-action" data-clear-music>不使用配乐</button>`
        : `<button type="button" class="secondary-action" data-select-music="${escapeAttribute(track.assetId)}">选用这首</button>`}
    </article>`;
  }).join("")}</div>`;
}

async function uploadStudioMusic(form) {
  if (!studioState.session || studioState.busy) return studioState;
  const file = form.elements?.namedItem("file")?.files?.[0];
  if (!file || !/\.(mp3|m4a|wav|aac)$/i.test(file.name)) {
    setStudioBusy(false, "请选择 MP3、M4A、WAV 或 AAC 音频文件");
    return studioState;
  }
  const workflow = beginStudioWorkflow();
  setStudioBusy(true, "正在检查配乐和授权说明…");
  try {
    const body = new FormData();
    body.append("operationKey", operationKey("music-upload"));
    body.append("sourceKind", formValue(form, "sourceKind"));
    body.append("licenseNote", formValue(form, "licenseNote"));
    body.append("publishUseAllowed", String(Boolean(form.elements?.namedItem("publishUseAllowed")?.checked)));
    body.append("briefId", studioState.brief?.id || "");
    body.append("file", file, file.name);
    const response = await fetch("/api/studio/music/import", {
      method: "POST", headers: { authorization: `Bearer ${studioState.session.token}` },
      credentials: "same-origin", body, signal: workflow.controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "studio-music-upload-failed");
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.music = result;
    if (studioState.brief) await refreshStudioMusicRecommendations(workflow);
    form.reset();
    setStudioBusy(false, `已选用 ${result.track.fileName}；生成时会保留原声并自动压低背景音乐`);
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  }
  renderStudioState();
  return studioState;
}

async function refreshStudioMusicRecommendations(workflow = null) {
  if (!studioState.session || !studioState.brief || !(studioState.music?.tracks || []).length) return studioState;
  const result = await studioApi("/api/studio/music/recommend", {
    method: "POST", body: { briefId: studioState.brief.id }, signal: workflow?.controller?.signal,
  });
  if (workflow && !ownsStudioWorkflow(workflow)) return studioState;
  studioState.music = { ...studioState.music, recommendations: result.recommendations || [] };
  return studioState;
}

async function refreshStudioMusicForCurrentBrief(workflow = null) {
  if (!studioState.session) return studioState;
  const result = await studioApi("/api/studio/music/list", {
    method: "POST",
    body: { briefId: studioState.brief?.id || null },
    signal: workflow?.controller?.signal,
  });
  if (workflow && !ownsStudioWorkflow(workflow)) return studioState;
  studioState.music = { ...result, recommendations: [] };
  if (studioState.brief && result.tracks?.length) await refreshStudioMusicRecommendations(workflow);
  return studioState;
}

async function clearStudioMusic() {
  if (!studioState.session || studioState.busy) return;
  const result = await studioApi("/api/studio/music/select", {
    method: "POST", body: { briefId: studioState.brief?.id || null, trackId: null },
  });
  studioState.music = { ...result, recommendations: studioState.music?.recommendations || [] };
  renderStudioState();
}

async function selectStudioMusic(trackId) {
  if (!studioState.session || studioState.busy) return;
  const result = await studioApi("/api/studio/music/select", {
    method: "POST", body: { briefId: studioState.brief?.id || null, trackId },
  });
  studioState.music = { ...result, recommendations: studioState.music?.recommendations || [] };
  renderStudioState();
}

async function updateStudioMusicMix(form) {
  if (!studioState.session || studioState.busy) return;
  const result = await studioApi("/api/studio/music/mix", {
    method: "POST",
    body: {
      briefId: studioState.brief?.id || null,
      trackId: form.dataset.trackId,
      trackStartMs: Math.round(Number(formValue(form, "trackStartSeconds")) * 1000),
      originalGainDb: Number(formValue(form, "originalGainDb")),
      musicGainDb: Number(formValue(form, "musicGainDb")),
    },
  });
  studioState.music = { ...result, recommendations: studioState.music?.recommendations || [] };
  setStudioBusy(false, "混音设置已保存；下次生成初稿会使用这些数值");
  renderStudioState();
}

function renderDraftCards(drafts) {
  if (!drafts.length) return '<div class="simple-empty-state"><strong>这里会显示你的内容初稿</strong><p>先说说想讲什么，再确认素材。生成后可以在这里播放、提出修改，或选择进入剪映精修。</p><button type="button" data-studio-tab="create">开始创作</button></div>';
  const indexedDrafts = drafts.map((draft, index) => ({ draft, index }));
  let primaryIndex = studioFocusedDraftId
    ? indexedDrafts.findIndex((entry) => entry.draft.id === studioFocusedDraftId)
    : -1;
  if (primaryIndex < 0) indexedDrafts.forEach((entry, index) => {
    if (entry.draft.status === "adopted") primaryIndex = index;
  });
  if (primaryIndex < 0) {
    indexedDrafts.forEach((entry, index) => {
      if (entry.draft.status !== "rejected" && entry.draft.status !== "invalidated") primaryIndex = index;
    });
  }
  if (primaryIndex < 0) primaryIndex = indexedDrafts.length - 1;
  const [primaryDraft] = indexedDrafts.splice(primaryIndex, 1);
  const orderedDrafts = [primaryDraft, ...indexedDrafts];
  const cards = orderedDrafts.map(({ draft, index }) => {
    const canAdopt = hasSatisfiedFeedbackForDraft(draft.id) &&
      draft.status !== "adopted" && draft.status !== "rejected";
    const statusLabel = draft.status === "adopted"
      ? "已通过"
      : draft.status === "rejected"
        ? "历史版本"
        : "待审核";
    const versionNumber = `V${escapeHtml(draft.version || studioState.brief?.version || 1)}`;
    const platformLabels = {
      "editorial-master": "原视频母版",
      xiaohongshu: "小红书版",
      douyin: "抖音版",
      "wechat-channels": "视频号版",
    };
    const platformMarks = {
      "editorial-master": "母版",
      xiaohongshu: "小红书",
      douyin: "抖音",
      "wechat-channels": "视频号",
    };
    const approvedCaptionImport = draft.approvedCaptionImport || null;
    const isReturnedDraft = draft.externalEdit?.lineageConfidence === "parent-only";
    const isUnverifiedExternalReturn = isReturnedDraft
      && draft.externalEdit?.externalEditor === "unverified-external-file";
    const versionName = draft.externalEdit?.lineageConfidence === "parent-only"
      ? isUnverifiedExternalReturn ? "外部回传待确认" : "历史剪映回传记录"
      : draft.shareOptimization
        ? "竖屏分享副本"
      : approvedCaptionImport
        ? `已验收字幕章节 ${index + 1}`
        : platformLabels[draft.platformVariant] || (index === 0 ? "制作过程版" : "设计师讲述版");
    const versionMark = draft.externalEdit?.lineageConfidence === "parent-only"
      ? isUnverifiedExternalReturn ? "来源待确认" : "历史记录"
      : draft.shareOptimization
        ? "另存分享版"
      : approvedCaptionImport
        ? "字幕已验收"
        : platformMarks[draft.platformVariant] || `方案 ${index === 0 ? "A" : "B"}`;
    const captionCount = Array.isArray(draft.captionCues) ? draft.captionCues.length : 0;
    const captionTemplateLabels = {
      "clean-outline": "清晰描边",
      "social-highlight": "社交平台高亮",
      "classic-bottom": "经典底部",
    };
    const captionTemplate = draft.captionStyle?.template
      ? captionTemplateLabels[draft.captionStyle.template] || draft.captionStyle.template
      : null;
    const transitionLabel = draft.transitionPlan?.style === "fade-through-black"
      ? `柔和转场 ${Number(draft.transitionPlan.durationMs || 0)}ms`
      : draft.transitionPlan?.style === "cut" ? "快速硬切" : null;
    const beatSyncLabel = draft.beatSyncPlan?.mode === "beat-synced"
      ? `音乐卡点 ${Number(draft.beatSyncPlan.alignedCutCount || 0)} 处`
      : draft.beatSyncPlan?.mode === "rule-fallback" ? "固定节奏回退" : null;
    const musicMixLabel = draft.musicMixPlan?.mode === "mixed"
      ? `配乐混音 · ${draft.musicMixPlan.rights?.publishUseAllowed ? "可发布授权" : "仅预览"}`
      : null;
    const qualityStatus = draft.qualityReport?.status || "not-checked";
    const qualityLabels = {
      passed: "自动质检通过",
      "needs-review": "自动质检有提醒",
      failed: "自动质检未通过",
      "not-checked": "尚无自动质检",
    };
    const qualityItems = (draft.qualityReport?.checks || [])
      .filter((check) => check.status !== "passed")
      .map((check) => `<li><strong>${escapeHtml(check.message)}</strong><small>${escapeHtml(check.threshold)}</small></li>`)
      .join("");
    const adoptionClosed = ["adopted", "rejected", "invalidated"].includes(draft.status);
    const approvalLabel = qualityStatus === "failed"
      ? "质检未通过"
      : draft.status === "adopted"
        ? "已通过"
        : draft.status === "rejected"
          ? "历史版本"
          : draft.status === "invalidated"
            ? "版本已失效"
            : canAdopt ? "确认通过" : "先评价再通过";
    const approvalAttribute = qualityStatus === "failed" || adoptionClosed
      ? " disabled"
      : canAdopt
        ? ` data-adopt-draft="${escapeAttribute(draft.id)}"`
        : ` data-review-before-adopt="${escapeAttribute(draft.id)}"`;
    const approvalHint = draft.status === "adopted"
      ? "这版已经通过，但还没有发布。"
      : canAdopt
        ? "已经记录“满意”，再确认一次即可采用；采用仍不会发布。"
        : qualityStatus === "failed" || adoptionClosed
          ? "这版不能继续采用，可以查看其他版本。"
          : "先播放视频并评价；选择“满意”后，才会开放最终确认。";
    const sourceItems = (draft.sourceMap || []).map((source) => `<li><strong>${escapeHtml(source.reason)}</strong><br><code>${escapeHtml(source.sourceAssetId)}</code><br><small>摘要 ${escapeHtml(source.sourceDigest)}</small></li>`).join("");
    const captionEditor = captionCount > 0 ? renderCaptionEditor(draft) : "";
    return `<article class="draft-card">
      <div class="draft-card-heading"><div><span class="draft-version-mark">${versionMark}</span><h3>${versionName}</h3></div><span class="draft-status draft-status-${escapeAttribute(draft.status || "reviewable")}">${statusLabel}</span></div>
      <video controls preload="metadata" data-draft-video="${escapeAttribute(draft.id)}" src="${escapeAttribute(draft.mediaUrl || "")}">你的浏览器暂不能播放这个视频。</video>
      <div class="draft-meta"><span>${Number(draft.durationSeconds || 0).toFixed(1)} 秒</span><span>${versionNumber}</span>${transitionLabel ? `<span>${escapeHtml(transitionLabel)}</span>` : ""}${beatSyncLabel ? `<span title="${escapeAttribute(draft.beatSyncPlan?.reason || "")}">${escapeHtml(beatSyncLabel)}</span>` : ""}${musicMixLabel ? `<span title="${escapeAttribute(draft.musicMixPlan?.reason || "")}">${escapeHtml(musicMixLabel)}</span>` : ""}${captionCount > 0 ? `<span>已烧录 ${captionCount} 条字幕</span>` : ""}${captionTemplate ? `<span>${escapeHtml(captionTemplate)}模板</span>` : ""}</div>
      <div class="quality-summary quality-${escapeAttribute(qualityStatus)}"><strong>${escapeHtml(qualityLabels[qualityStatus] || qualityStatus)}</strong>${qualityItems ? `<ul>${qualityItems}</ul>` : ""}</div>
      ${renderDraftVisualEvidence(draft.visualEvidence)}
      ${captionCount > 0 ? approvedCaptionImport
        ? '<p class="micro-boundary">这一版使用你已验收的字幕候选，已烧录到独立的本地草稿；原片没有被覆盖，也没有发布。</p>'
        : '<p class="micro-boundary">当前视频已烧录本地语音模型生成的预览字幕；剪映交接包同时保留可修改 SRT，发布前请人工核对文字。</p>' : ""}
      ${draft.shareOptimization ? `<p class="draft-lineage-note">由已通过作品另存为 720×1280 竖屏版；原作品未修改。本版需要重新播放、评价和确认。</p>` : ""}
      ${captionEditor}
      <div class="draft-decision-actions" aria-label="选择这版作品的下一步">
        <button class="draft-approve-action" type="button"${approvalAttribute}>${approvalLabel}</button>
        <button type="button" data-open-ai-edit="${escapeAttribute(draft.id)}">让 AI 修改</button>
        <button type="button" data-open-external-edit="${escapeAttribute(draft.id)}">去剪映精修</button>
      </div>
      <p class="draft-approval-hint">${approvalHint}</p>
      ${renderDraftDistribution(draft, platformLabels[draft.platformVariant] || "当前作品")}
      ${renderCustomerFeedbackForm(draft, latestCustomerFeedbackForDraft(draft.id))}
      ${renderExternalEditPanel(draft)}
      <details class="source-drawer"><summary>来源与技术信息</summary><p><span class="offline-badge">${escapeHtml(draft.capability?.label || "离线/模拟分析")}</span>${isReturnedDraft ? isUnverifiedExternalReturn ? " · 回传文件夹检测 · 只确认父版本，不证明导出软件" : " · 历史剪映回传记录 · 来源未重新验证" : ""}</p><ol>${sourceItems}</ol><p class="micro-boundary">采用仍不是发布；这里只记录审核决定。</p></details>
    </article>`;
  });
  if (cards.length === 1) return cards[0];
  return `${cards[0]}<details class="other-drafts-drawer">
    <summary><span>其他作品与历史版本</span><small>${cards.length - 1} 个，需要时再展开</small></summary>
    <div class="other-drafts-list">${cards.slice(1).join("")}</div>
  </details>`;
}

function draftExportFileName(draft) {
  const suffix = ({
    "editorial-master": "master",
    xiaohongshu: "xiaohongshu",
    douyin: "douyin",
    "wechat-channels": "wechat-channels",
  })[draft.platformVariant] || "reviewed";
  return `content-${suffix}-v${Number(draft.version || 1)}.mp4`;
}

function draftDistributionBrief(draft) {
  return (studioState.history?.briefs || []).find((brief) => brief.id === draft.briefId)
    || (studioState.brief?.id === draft.briefId ? studioState.brief : null);
}

function buildDraftDistributionKit(draft, brief = null) {
  const platformLabels = {
    "editorial-master": "原视频母版",
    xiaohongshu: "小红书",
    douyin: "抖音",
    "wechat-channels": "视频号",
  };
  const typeTags = {
    "single-product": ["珠宝", "产品展示"],
    "jewelry-making-process": ["珠宝", "制作过程"],
    "designer-viewpoint": ["珠宝", "设计灵感"],
  };
  const platformLabel = platformLabels[draft.platformVariant] || "当前平台";
  const goal = String(brief?.goal || "这条珠宝内容").trim().replace(/[\r\n]+/gu, " ").slice(0, 80);
  const tags = typeTags[brief?.contentType] || ["珠宝", "内容记录"];
  const hashtags = tags.map((tag) => `#${tag}`).join(" ");
  const titleSuffix = draft.platformVariant === "xiaohongshu" ? "｜设计记录"
    : draft.platformVariant === "wechat-channels" ? "｜完整记录"
      : draft.platformVariant === "douyin" ? "" : "｜母版";
  const title = `${goal}${titleSuffix}`.slice(0, 60);
  const caption = `${goal}\n\n这是已在本地审核通过的内容作品，发布前请再次核对画面、字幕和平台规则。\n\n${hashtags}`;
  const baseName = draftExportFileName(draft).replace(/\.mp4$/u, "");
  return {
    platformLabel,
    title,
    caption,
    hashtags,
    videoFileName: `${baseName}.mp4`,
    coverFileName: `${baseName}-cover.jpg`,
    noteFileName: `${baseName}-发布说明.txt`,
    durationSeconds: Number(draft.durationSeconds || 0),
    publishClaimed: false,
    platformAcceptedClaimed: false,
  };
}

function renderDraftDistributionKit(draft) {
  const kit = buildDraftDistributionKit(draft, draftDistributionBrief(draft));
  return `<details class="draft-distribution-kit">
    <summary>分享资料</summary>
    <div class="draft-kit-copy">
      <span>标题</span><strong>${escapeHtml(kit.title)}</strong>
      <button type="button" data-copy-distribution="title" data-kit-draft="${escapeAttribute(draft.id)}">复制标题</button>
    </div>
    <div class="draft-kit-copy">
      <span>发布文案</span><p>${escapeHtml(kit.caption).replaceAll("\n", "<br>")}</p>
      <button type="button" data-copy-distribution="caption" data-kit-draft="${escapeAttribute(draft.id)}">复制文案</button>
    </div>
    <div class="draft-kit-actions">
      <button type="button" data-download-draft-cover="${escapeAttribute(draft.id)}">保存当前画面为封面</button>
      <button type="button" data-download-distribution-note="${escapeAttribute(draft.id)}">下载发布说明</button>
    </div>
    <p data-distribution-kit-status="${escapeAttribute(draft.id)}">封面取自你暂停的视频画面；发布说明会带上文件名、时长和边界。</p>
    <p class="micro-boundary">文案只使用当前任务主题，不补写未经确认的产品事实。资料准备好不等于平台已接收或发布。</p>
  </details>`;
}

function renderDraftDistribution(draft, platformLabel) {
  if (draft.status !== "adopted") return "";
  const fileName = draftExportFileName(draft);
  const targetNote = draft.platformVariant === "editorial-master"
    ? "母版适合继续剪辑或本地保存"
    : `生成任务标记为${platformLabel}`;
  return `<section class="draft-distribution" aria-label="导出与分发">
    <div><span class="draft-version-mark">已通过</span><h4>导出与分发</h4></div>
    <p><strong>${escapeHtml(targetNote)}</strong></p>
    <dl><div><dt>文件</dt><dd>MP4</dd></div><div><dt>时长</dt><dd>${Number(draft.durationSeconds || 0).toFixed(1)} 秒</dd></div><div><dt>画面</dt><dd data-draft-resolution="${escapeAttribute(draft.id)}">播放后读取实际规格</dd></div></dl>
    ${renderDraftDeliveryCheck(draft, platformLabel)}
    ${renderDraftDistributionKit(draft)}
    <div class="draft-distribution-actions">
      <a class="primary-action" data-download-draft="${escapeAttribute(draft.id)}" href="${escapeAttribute(draft.mediaUrl || "")}" download="${escapeAttribute(fileName)}">下载文件</a>
      <button class="secondary-action" type="button" data-share-draft="${escapeAttribute(draft.id)}">打开系统分享</button>
    </div>
    <p class="micro-boundary" data-share-status="${escapeAttribute(draft.id)}">平台标记来自生成任务，不代表平台已验收或已经发布；分享时由你在系统菜单里选择目标应用。</p>
    ${renderDraftDistributionHistory(draft.id)}
  </section>`;
}

function assessLocalDraftDelivery(width, height, durationSeconds) {
  const duration = Number(durationSeconds || 0);
  const durationReady = duration >= 6 && duration <= 60;
  const hasFrame = Number(width) > 0 && Number(height) > 0;
  if (!hasFrame) {
    return { status: "pending", durationReady, portraitReady: null, resolutionReady: null };
  }
  const ratio = Number(width) / Number(height);
  const portraitReady = Math.abs(ratio - 9 / 16) <= 0.01;
  const resolutionReady = Number(width) >= 720 && Number(height) >= 1280;
  return {
    status: durationReady && portraitReady && resolutionReady ? "ready" : "needs-adjustment",
    durationReady,
    portraitReady,
    resolutionReady,
  };
}

function renderDraftDeliveryCheck(draft, platformLabel) {
  const check = assessLocalDraftDelivery(0, 0, draft.durationSeconds);
  return `<div class="draft-delivery-check" data-delivery-check="${escapeAttribute(draft.id)}">
    <div><strong>分享前自检</strong><span class="is-pending" data-delivery-verdict>播放后检查画面</span></div>
    <p>目标版本：${escapeHtml(platformLabel)} · 本地交付基线</p>
    <ul>
      <li class="is-ready" data-delivery-file>MP4 文件</li>
      <li class="${check.durationReady ? "is-ready" : "is-failed"}" data-delivery-duration>时长 6–60 秒</li>
      <li class="is-pending" data-delivery-frame>竖屏 9:16，至少 720×1280</li>
    </ul>
    <div class="draft-delivery-fix" data-delivery-fix hidden>
      <button type="button" data-create-share-version="${escapeAttribute(draft.id)}">生成竖屏分享版</button>
      <p>会另存一份 720×1280 新版本，不裁掉原画面、不覆盖原作品；生成后需要重新审核。</p>
    </div>
    <p class="micro-boundary">这里只检查本项目的本地交付基线，不是平台官方验收，也不保证平台上传成功。</p>
  </div>`;
}

function canCreateShareVersion(check) {
  return check.durationReady === true &&
    check.status === "needs-adjustment" &&
    (check.portraitReady === false || check.resolutionReady === false);
}

function updateDraftDeliveryCheck(draftId, width, height, durationSeconds) {
  const panel = document.querySelector?.(`[data-delivery-check="${draftId}"]`);
  if (!panel) return null;
  const check = assessLocalDraftDelivery(width, height, durationSeconds);
  const verdict = panel.querySelector?.("[data-delivery-verdict]");
  const frame = panel.querySelector?.("[data-delivery-frame]");
  const fix = panel.querySelector?.("[data-delivery-fix]");
  if (verdict) {
    verdict.className = check.status === "ready" ? "is-ready" : "is-failed";
    verdict.textContent = check.status === "ready" ? "本地规格通过" : "需要调整后再分享";
  }
  if (frame) {
    frame.className = check.portraitReady && check.resolutionReady ? "is-ready" : "is-failed";
    frame.textContent = `${width}×${height} · ${check.portraitReady ? "竖屏比例符合" : "不是 9:16"} · ${check.resolutionReady ? "达到本地清晰度基线" : "低于本地清晰度基线"}`;
  }
  if (fix) fix.hidden = !canCreateShareVersion(check);
  return check;
}

async function createShareVersion(draftId, trigger) {
  if (!studioState.session || studioState.busy || !draftId) return studioState;
  const workflow = beginStudioWorkflow();
  trigger.disabled = true;
  setStudioBusy(true, "正在把已通过作品另存为竖屏分享版；原作品不会修改…");
  try {
    const result = await studioApi(`/api/studio/drafts/${encodeURIComponent(draftId)}/share-version`, {
      method: "POST",
      body: { operationKey: operationKey("share-version") },
      signal: workflow.controller.signal,
    });
    if (!ownsStudioWorkflow(workflow)) return studioState;
    await refreshStudioHistory(workflow);
    if (!ownsStudioWorkflow(workflow)) return studioState;
    studioState.drafts = (studioState.history?.drafts || []).filter((draft) =>
      draft.briefId === result.draft.briefId && draft.version === result.draft.version);
    studioFocusedDraftId = result.draft.id;
    studioActiveTab = "works";
    invalidateStudioCaptionFeedbackDataset();
    setStudioBusy(false, "竖屏分享版已另存；原作品未修改。请重新播放、评价并确认这一版");
  } catch (error) {
    if (!ownsStudioWorkflow(workflow) || isAbortError(error)) return studioState;
    setStudioBusy(false, studioErrorMessage(error));
  } finally {
    trigger.disabled = false;
  }
  renderStudioState();
  return studioState;
}

function renderDraftDistributionHistory(draftId) {
  const labels = {
    "download-started": "开始下载文件",
    "system-share-invoked": "调用系统分享",
    "share-fallback-download": "分享不可用，改为下载",
  };
  const events = (studioState.history?.distributionEvents || [])
    .filter((event) => event.draftId === draftId)
    .sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
  if (!events.length) {
    return `<div class="draft-distribution-history" data-distribution-history="${escapeAttribute(draftId)}"><strong>操作记录</strong><p>还没有下载或分享记录。</p></div>`;
  }
  return `<div class="draft-distribution-history" data-distribution-history="${escapeAttribute(draftId)}"><strong>操作记录</strong><ul>${events.slice(0, 5).map((event) => `<li><span>${escapeHtml(labels[event.action] || "本地操作")}</span><time datetime="${escapeAttribute(event.createdAt)}">${escapeHtml(formatStudioEventDateTime(event.createdAt))}</time></li>`).join("")}</ul><p>这里只证明本机操作发生过，没有平台接收或发布回执。</p></div>`;
}

function formatStudioEventDateTime(value) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false, timeZone: "Asia/Taipei",
  }).format(new Date(timestamp));
}

async function recordDraftDistributionEvent(draftId, action, panel) {
  const result = await studioApi("/api/studio/distribution-events", {
    method: "POST",
    body: { draftId, action, operationKey: operationKey("distribution") },
  });
  const history = studioState.history || {};
  const events = [...(history.distributionEvents || []), result.event];
  studioState.history = { ...history, distributionEvents: events };
  const current = panel?.querySelector?.("[data-distribution-history]");
  if (current) current.outerHTML = renderDraftDistributionHistory(draftId);
  return result.event;
}

function startDraftDownload(draft, sourceLink) {
  const link = document.createElement("a");
  link.href = draft.mediaUrl || sourceLink?.href || "";
  link.download = draftExportFileName(draft);
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
}

function distributionKitForDraft(draftId) {
  const draft = (studioState.drafts || []).find((item) => item.id === draftId);
  if (!draft || draft.status !== "adopted") return null;
  return { draft, kit: buildDraftDistributionKit(draft, draftDistributionBrief(draft)) };
}

function distributionKitStatus(draftId, message) {
  const status = document.querySelector?.(`[data-distribution-kit-status="${draftId}"]`);
  if (status) status.textContent = message;
}

async function copyDistributionKitText(draftId, field, trigger) {
  const value = distributionKitForDraft(draftId);
  if (!value || !["title", "caption"].includes(field)) return false;
  const text = value.kit[field];
  try {
    if (typeof globalThis.navigator?.clipboard?.writeText === "function") {
      await globalThis.navigator.clipboard.writeText(text);
    } else {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      let copied = false;
      try {
        input.select();
        copied = document.execCommand?.("copy") === true;
      } finally {
        input.remove();
      }
      if (!copied) throw new Error("clipboard-unavailable");
    }
    const label = field === "title" ? "标题" : "文案";
    distributionKitStatus(draftId, `${label}已复制；请到目标平台粘贴并人工确认。`);
    if (trigger) trigger.textContent = "已复制";
    return true;
  } catch {
    distributionKitStatus(draftId, "当前浏览器不能自动复制，请手动选择上方文字。没有自动发送到平台。");
    return false;
  }
}

function downloadDistributionNote(draftId) {
  const value = distributionKitForDraft(draftId);
  if (!value) return null;
  const { draft, kit } = value;
  const note = [
    `目标平台：${kit.platformLabel}`,
    `视频文件：${kit.videoFileName}`,
    `封面文件：${kit.coverFileName}`,
    `时长：${kit.durationSeconds.toFixed(1)} 秒`,
    "",
    `标题：${kit.title}`,
    "",
    "发布文案：",
    kit.caption,
    "",
    "边界：本资料由本地已通过作品生成；未声明平台已验收、已接收或已发布。发布前请人工核对平台规则。",
  ].join("\n");
  const blob = new Blob([`\uFEFF${note}\n`], { type: "text/plain;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = kit.noteFileName;
  anchor.click();
  URL.revokeObjectURL(href);
  distributionKitStatus(draftId, "发布说明已下载；视频和封面仍由你确认后分享。");
  return kit;
}

async function downloadCurrentDraftCover(draftId, trigger) {
  const value = distributionKitForDraft(draftId);
  const video = document.querySelector?.(`[data-draft-video="${draftId}"]`);
  if (!value || !video || Number(video.readyState || 0) < 2 || !Number(video.videoWidth) || !Number(video.videoHeight)) {
    distributionKitStatus(draftId, "请先播放视频并暂停在想作为封面的画面，再点保存封面。");
    return false;
  }
  trigger.disabled = true;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 720;
    canvas.height = 1280;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("cover-canvas-unavailable");
    context.fillStyle = "#000000";
    context.fillRect(0, 0, canvas.width, canvas.height);
    const scale = Math.min(canvas.width / video.videoWidth, canvas.height / video.videoHeight);
    const width = video.videoWidth * scale;
    const height = video.videoHeight * scale;
    context.drawImage(video, (canvas.width - width) / 2, (canvas.height - height) / 2, width, height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    if (!blob) throw new Error("cover-encode-failed");
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = value.kit.coverFileName;
    anchor.click();
    URL.revokeObjectURL(href);
    distributionKitStatus(draftId, "封面已保存为 720×1280；没有修改视频，也没有自动发布。");
    return true;
  } catch {
    distributionKitStatus(draftId, "封面保存失败，视频和原作品没有修改；可换一个画面后重试。");
    return false;
  } finally {
    trigger.disabled = false;
  }
}

async function downloadDraftMedia(draftId, trigger) {
  const draft = (studioState.drafts || []).find((item) => item.id === draftId);
  if (!draft?.mediaUrl) return;
  const panel = trigger?.closest?.(".draft-distribution");
  const status = panel?.querySelector?.("[data-share-status]");
  trigger.setAttribute?.("aria-disabled", "true");
  try {
    await recordDraftDistributionEvent(draftId, "download-started", panel);
    if (status) status.textContent = "已记录开始下载；这不是平台接收或发布回执。";
  } catch (error) {
    if (status) status.textContent = `文件仍会下载，但本次操作记录失败（${studioErrorMessage(error)}）。`;
  } finally {
    trigger.removeAttribute?.("aria-disabled");
    startDraftDownload(draft, trigger);
  }
}

async function shareDraftMedia(draftId, trigger) {
  const draft = (studioState.drafts || []).find((item) => item.id === draftId);
  const panel = trigger?.closest?.(".draft-distribution");
  const status = panel?.querySelector?.("[data-share-status]");
  const download = panel?.querySelector?.("[data-download-draft]");
  const share = globalThis.navigator?.share;
  if (!draft?.mediaUrl || typeof share !== "function" || typeof File !== "function") {
    try {
      if (draft?.mediaUrl) await recordDraftDistributionEvent(draftId, "share-fallback-download", panel);
      if (status) status.textContent = "当前设备不支持文件分享，已改为本地下载；没有自动发布。";
    } catch (error) {
      if (status) status.textContent = `已改为本地下载，但操作记录失败（${studioErrorMessage(error)}）。`;
    }
    if (draft) startDraftDownload(draft, download);
    return;
  }
  trigger.disabled = true;
  if (status) status.textContent = "正在准备系统分享菜单…";
  try {
    const response = await fetch(draft.mediaUrl, { credentials: "same-origin" });
    if (!response.ok) throw new Error("draft-share-media-unavailable");
    const file = new File([await response.blob()], draftExportFileName(draft), { type: "video/mp4" });
    if (typeof globalThis.navigator.canShare === "function" && !globalThis.navigator.canShare({ files: [file] })) {
      let recorded = true;
      try {
        await recordDraftDistributionEvent(draftId, "share-fallback-download", panel);
      } catch {
        recorded = false;
      }
      startDraftDownload(draft, download);
      if (status) status.textContent = recorded
        ? "当前设备不能分享这个视频文件，已改为本地下载；没有自动发布。"
        : "已改为本地下载，但操作记录失败；没有自动发布。";
      return;
    }
    await share.call(globalThis.navigator, { title: "已通过的视频", files: [file] });
    try {
      await recordDraftDistributionEvent(draftId, "system-share-invoked", panel);
      if (status) status.textContent = "已记录调用系统分享；平台是否接收成功请以目标应用提示为准。";
    } catch {
      if (status) status.textContent = "系统分享已完成，但本地操作记录失败；平台是否接收成功请以目标应用提示为准。";
    }
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      if (status) status.textContent = "已取消系统分享，没有发布任何内容。";
    } else if (status) {
      status.textContent = "系统分享没有完成，可以改用“下载文件”；没有自动发布。";
    }
  } finally {
    trigger.disabled = false;
  }
}

function isSyntheticHighlightFixture(material) {
  if (!material) return false;
  const names = [material.displayName, material.fileName, ...(material.observations || [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return names.includes("synthetic-color-tone-fixture") || names.includes("纯色合成商品夹具");
}

function isEligibleHighlightMaterial(material) {
  if (!material || isSyntheticHighlightFixture(material)) return false;
  const observations = material.observations || [];
  const evidence = [...observations, ...(material.technicalFlags || []), ...(material.pendingFacts || [])]
    .filter(Boolean).join(" ").toLowerCase();
  if (evidence.includes("接入方式：模拟硬件") || evidence.includes("设备真实性：本地模拟")) return false;
  return observations.some((value) =>
    String(value).startsWith("客户上传素材：") || String(value).startsWith("硬件接入素材："));
}

function highlightDraftStorage() {
  try {
    return typeof window !== "undefined" ? window.localStorage || null : null;
  } catch {
    return null;
  }
}

function highlightDraftIdentity(materialCardId, evaluationOverride = null, materialOverride = null) {
  const material = materialOverride || (studioState.materials || []).find((item) => item.id === materialCardId);
  const evaluation = evaluationOverride || (studioState.highlightEvaluations || []).find((item) =>
    item.sourceAssetId === material?.sourceAssetId);
  const customerId = String(studioState.customerId || studioState.session?.customerId || "");
  const sessionId = String(studioState.session?.sessionId || "");
  const sourceAssetId = String(evaluation?.sourceAssetId || material?.sourceAssetId || "");
  const sourceDigest = String(evaluation?.sourceDigest || material?.sourceDigest || "");
  if (!materialCardId || !customerId || !sessionId || !sourceAssetId || !sourceDigest) return null;
  return { customerId, sessionId, materialCardId, sourceAssetId, sourceDigest };
}

function highlightDraftStorageKey(materialCardId, evaluationOverride = null, materialOverride = null) {
  const identity = highlightDraftIdentity(materialCardId, evaluationOverride, materialOverride);
  if (!identity) return null;
  return [HIGHLIGHT_DRAFT_PREFIX, identity.customerId, identity.sessionId,
    identity.materialCardId, identity.sourceAssetId, identity.sourceDigest]
    .map((part) => encodeURIComponent(part)).join(":");
}

function readHighlightDraft(materialCardId, evaluationOverride = null, materialOverride = null) {
  const storage = highlightDraftStorage();
  const key = highlightDraftStorageKey(materialCardId, evaluationOverride, materialOverride);
  if (!storage || !key) return null;
  try {
    const parsed = JSON.parse(storage.getItem(key) || "null");
    const identity = highlightDraftIdentity(materialCardId, evaluationOverride, materialOverride);
    if (!parsed || parsed.schemaVersion !== 1 || parsed.status !== "unsubmitted-local-draft" ||
      parsed.customerId !== identity?.customerId || parsed.sessionId !== identity?.sessionId ||
      parsed.materialCardId !== identity?.materialCardId || parsed.sourceAssetId !== identity?.sourceAssetId ||
      parsed.sourceDigest !== identity?.sourceDigest) return null;
    return parsed;
  } catch {
    return null;
  }
}

function setHighlightDraftState(form, draft) {
  const card = form?.closest?.(".highlight-evaluation-card");
  const indicator = card?.querySelector?.("[data-highlight-draft-state]");
  const clearButton = card?.querySelector?.("[data-highlight-draft-clear]");
  if (indicator) indicator.textContent = draft
    ? "未提交草稿已保存在本机；不计入人工标签、训练或发布。"
    : "尚无未提交草稿。";
  if (clearButton) clearButton.disabled = !draft;
}

function persistHighlightDraftFromForm(form) {
  const materialCardId = String(form?.dataset?.highlightForm || "");
  const storage = highlightDraftStorage();
  const key = highlightDraftStorageKey(materialCardId);
  const identity = highlightDraftIdentity(materialCardId);
  if (!storage || !key || !identity) return null;
  const startSeconds = String(formValue(form, "startSeconds") || "");
  const endSeconds = String(formValue(form, "endSeconds") || "");
  const reason = String(formValue(form, "reason") || "");
  if (!startSeconds && !endSeconds && !reason) {
    storage.removeItem(key);
    setHighlightDraftState(form, null);
    return null;
  }
  const draft = {
    schemaVersion: 1,
    ...identity,
    startSeconds,
    endSeconds,
    reason,
    updatedAt: new Date().toISOString(),
    status: "unsubmitted-local-draft",
    boundary: {
      humanLabel: false,
      trainingEligible: false,
      productionAccepted: false,
      automaticPublishing: false,
    },
  };
  storage.setItem(key, JSON.stringify(draft));
  setHighlightDraftState(form, draft);
  setHighlightFormStatus(form, "未提交草稿已保存在本机；刷新后可恢复，系统不会自动提交。");
  return draft;
}

function clearHighlightDraft(materialCardId, form = null) {
  const storage = highlightDraftStorage();
  const key = highlightDraftStorageKey(materialCardId);
  if (storage && key) storage.removeItem(key);
  if (form) {
    for (const fieldName of ["startSeconds", "endSeconds", "reason"]) {
      const field = form.elements?.namedItem?.(fieldName);
      if (!field) continue;
      field.value = "";
      if ("defaultValue" in field) field.defaultValue = "";
    }
    setHighlightDraftState(form, null);
    setHighlightFormStatus(form, "本机草稿已清除；没有产生人工标签，也没有进入训练或发布。");
  }
  return true;
}

function renderHighlightEvaluations(
  evaluations, materials, brief = null, batch = null, coverageProfiles = [], candidateRejections = [],
) {
  if (!Array.isArray(evaluations) || evaluations.length === 0) {
    return '<div class="simple-empty-state"><strong>还没有可验收的视频</strong><p>上传视频后，才能圈出人工确认的高光时间。</p></div>';
  }
  const materialBySource = new Map((materials || []).map((item) => [item.sourceAssetId, item]));
  const eligibleEvaluations = evaluations.filter((evaluation) =>
    isEligibleHighlightMaterial(materialBySource.get(evaluation.sourceAssetId)));
  const visibleEvaluations = evaluations.filter((evaluation) =>
    !isSyntheticHighlightFixture(materialBySource.get(evaluation.sourceAssetId)));
  const pendingCount = visibleEvaluations.filter((evaluation) => (evaluation.labels || []).length === 0).length;
  const labeledCount = visibleEvaluations.length - pendingCount;
  const selectedAssetIds = new Set(brief?.selectedAssetIds || []);
  const statusLabels = {
    "awaiting-human-label": "待人工标注，不能评价模型选片",
    "no-discriminative-model-candidate": "模型未通过：没有可区分的时间窗口",
    passed: "模型通过人工高光对比",
    failed: "模型未通过人工高光对比",
  };
  const workerStageLabels = {
    "runtime-preaccept": "运行环境核验",
    "frontstage-extraction": "提取候选画面",
    "model-load": "加载视觉模型",
    "processor-prepare": "处理候选画面",
    "model-generation": "模型生成判断",
    "output-validation": "校验模型输出",
  };
  const batchSummary = renderHighlightBatchGate(batch, brief);
  const queueSummary = `<section class="highlight-queue-summary" aria-label="人工高光待确认队列">
    <div class="card-heading"><h4>人工高光待确认队列</h4><span class="gate-chip ${pendingCount === 0 ? "is-ready" : "is-blocked"}">待确认 ${pendingCount} 条</span></div>
    <p>当前队列展示 ${visibleEvaluations.length} 条非演示视频：${labeledCount} 条已标注，${pendingCount} 条待标注。本轮只处理现有素材，不要求客户新增拍摄；后续证据随日常上传自然积累。</p>
    <p class="micro-boundary">其中 ${eligibleEvaluations.length} 条有明确上传来源，可计入本地试运行；${Math.max(0, visibleEvaluations.length - eligibleEvaluations.length)} 条来源未确认，只可查看和复核。另排除 ${Math.max(0, evaluations.length - visibleEvaluations.length)} 条合成演示素材。上传来源本身不等于生产真实性验收。</p>
    <p class="micro-boundary">已记录 ${(candidateRejections || []).length} 条模型候选否决；它们只作负反馈，不增加人工高光数量，也不进入训练或发布。</p>
  </section>`;
  return batchSummary + queueSummary + visibleEvaluations.map((evaluation, index) => {
    const material = materialBySource.get(evaluation.sourceAssetId);
    const coverageProfile = (coverageProfiles || []).find((item) =>
      item.sourceAssetId === evaluation.sourceAssetId && item.sourceDigest === evaluation.sourceDigest);
    const localDraft = material?.id ? readHighlightDraft(material.id, evaluation, material) : null;
    const retest = (studioState.highlightRetestAttempts || []).find((item) =>
      item.sourceAssetId === evaluation.sourceAssetId);
    const durationSeconds = Math.max(0, (evaluation.sourceEndMs - evaluation.sourceStartMs) / 1_000);
    const retestFailed = retest && retest.status !== "succeeded";
    const statusClass = !retestFailed && evaluation.status === "passed" ? "is-ready" : "is-blocked";
    const statusLabel = retestFailed
      ? `复测未完成：${retest.failureCode || retest.status}`
      : statusLabels[evaluation.status] || evaluation.status;
    const labels = (evaluation.labels || []).map((label) =>
      `<li><strong>${(label.sourceStartMs / 1_000).toFixed(2)}–${(label.sourceEndMs / 1_000).toFixed(2)} 秒</strong> · ${escapeHtml(label.reason)} <small>${label.confirmationMode === "accepted-model-candidate" ? "人工确认模型候选 · 回执已绑定" : "人工填写"}</small></li>`).join("");
    const scores = (evaluation.scores || []).map((score) =>
      `<li>模型所选时间窗整体重合度 ${(Number(score.bestIoU || 0) * 100).toFixed(1)}%</li>`).join("");
    const modelCandidates = (evaluation.modelCandidates || []).map((candidate) => {
      const rejected = (candidateRejections || []).some((item) =>
        item.sourceAssetId === evaluation.sourceAssetId
        && item.sourceDigest === evaluation.sourceDigest
        && item.receiptJobId === candidate.receiptJobId
        && item.sourceStartMs === candidate.startMs
        && item.sourceEndMs === candidate.endMs);
      return `<li><strong>${(candidate.startMs / 1_000).toFixed(2)}–${(candidate.endMs / 1_000).toFixed(2)} 秒</strong> · ${escapeHtml(candidate.text)} <small>${rejected ? "人工已标记：不适合作为高光" : "模型建议，待人工复核"}</small><button class="primary-action" type="button" data-accept-highlight-candidate="${escapeAttribute(material?.id || "")}" data-highlight-receipt="${escapeAttribute(candidate.receiptJobId || "")}" data-highlight-start-ms="${candidate.startMs}" data-highlight-end-ms="${candidate.endMs}"${material?.id && candidate.receiptJobId && !rejected ? "" : " disabled"}>我已看过，确认这段</button><button class="secondary-action" type="button" data-reject-highlight-candidate="${escapeAttribute(material?.id || "")}" data-highlight-receipt="${escapeAttribute(candidate.receiptJobId || "")}" data-highlight-start-ms="${candidate.startMs}" data-highlight-end-ms="${candidate.endMs}"${material?.id && candidate.receiptJobId && !rejected ? "" : " disabled"}>这段不合适</button><button class="quiet-action highlight-candidate-use" type="button" data-use-highlight-candidate="${escapeAttribute(material?.id || "")}" data-highlight-start="${candidate.startMs / 1_000}" data-highlight-end="${candidate.endMs / 1_000}"${material?.id ? "" : " disabled"}>需要调整，带入下方</button></li>`;
    }).join("");
    return `<article class="highlight-evaluation-card">
      <div class="card-heading"><h4>${escapeHtml(material?.displayName || material?.fileName || `素材 ${index + 1}`)}</h4><span class="gate-chip ${statusClass}">${escapeHtml(statusLabel)}</span></div>
      ${selectedAssetIds.has(evaluation.sourceAssetId) ? '<p class="micro-boundary">当前作品正在使用这条素材。</p>' : ""}
      ${material?.mediaUrl ? `<video class="highlight-source-video" data-highlight-video="${escapeAttribute(material?.id || "")}" src="${escapeAttribute(material.mediaUrl)}" controls preload="metadata" playsinline></video>
      <div class="highlight-time-actions"><button class="secondary-action" type="button" data-highlight-mark-start="${escapeAttribute(material?.id || "")}"${material?.id ? "" : " disabled"}>当前画面设为开始</button><button class="secondary-action" type="button" data-highlight-mark-end="${escapeAttribute(material?.id || "")}"${material?.id ? "" : " disabled"}>当前画面设为结束</button><output data-highlight-form-status="${escapeAttribute(material?.id || "")}">先播放视频，到合适画面时点击按钮。</output></div>` : ""}
      <p>原视频范围 ${(evaluation.sourceStartMs / 1_000).toFixed(2)}–${(evaluation.sourceEndMs / 1_000).toFixed(2)} 秒 · 判定线：时间重合度至少 50%</p>
      ${labels ? `<ol class="highlight-label-list">${labels}</ol>` : '<p class="micro-boundary">目前没有人工答案；系统不会替你编一个高光时间。</p>'}
      ${retestFailed
        ? '<p class="micro-boundary">此次复测未产生符合数据边界的高光候选，系统没有计算命中率。</p>'
        : modelCandidates
          ? `<details><summary>查看模型推荐的 ${evaluation.modelCandidates.length} 个时间窗</summary><ol class="highlight-label-list">${modelCandidates}</ol></details>`
          : '<p class="micro-boundary">模型尚未给出可区分的高光时间窗。</p>'}
      ${!retestFailed && scores ? `<ul class="highlight-score-list">${scores}</ul>` : ""}
      ${retest ? `<p class="micro-boundary">最近复测：${escapeHtml(retest.status)} · 第 ${Number(retest.attempt || 0)} 次${retest.analysisStrategy === "temporal-bucket-salience-v2" && Number.isSafeInteger(retest.analyzedFrames) ? ` · 全片分布抽帧 ${Number(retest.analyzedFrames)} 帧` : ""}${retest.failureCode ? ` · ${escapeHtml(retest.failureCode)}` : ""}${retest.lastStage ? ` · 停在${escapeHtml(workerStageLabels[retest.lastStage] || retest.lastStage)}${Number.isFinite(retest.activeStageElapsedMs) ? `（至少 ${Math.max(0, Number(retest.activeStageElapsedMs) / 1_000).toFixed(1)} 秒）` : ""}` : ""}</p>` : ""}
      <button class="secondary-action" type="button" data-highlight-retest="${escapeAttribute(material?.id || "")}"${material?.id ? "" : " disabled"}>${(evaluation.labels || []).length > 0 ? "重新运行分段模型复测" : "重新分析模型候选"}</button>
      <form class="studio-form-grid highlight-coverage-form" data-highlight-coverage-form="${escapeAttribute(material?.id || "")}">
        <strong>人工拍摄画像${coverageProfile ? " · 已保存" : ""}</strong>
        <label>光线<select name="lighting" required>${renderCoverageOptions([
          ["stable", "稳定光线"], ["mixed", "混合光线"], ["low", "低光环境"],
        ], coverageProfile?.lighting)}</select></label>
        <label>主体动作<select name="motion" required>${renderCoverageOptions([
          ["static-display", "静态展示"], ["handheld-product", "手持展示"], ["making-action", "制作动作"],
          ["person-speaking", "人物口播"], ["device-motion", "设备运动"],
        ], coverageProfile?.motion)}</select></label>
        <label>拍摄环境<select name="environment" required>${renderCoverageOptions([
          ["indoor-controlled", "室内可控环境"], ["indoor-mixed", "室内复杂环境"], ["outdoor", "户外环境"],
        ], coverageProfile?.environment)}</select></label>
        <label class="highlight-human-confirmation"><input name="humanConfirmed" type="checkbox" required>我已亲自看过视频，并确认以上拍摄条件。</label>
        <button class="secondary-action" type="submit"${material?.id ? "" : " disabled"}>${coverageProfile ? "更新人工画像" : "保存人工画像"}</button>
        <output data-highlight-coverage-status>${coverageProfile ? "已计入人工覆盖证据；模型推测没有计入。" : "尚未计入覆盖证据。"}</output>
      </form>
      <form class="studio-form-grid highlight-form" data-highlight-form="${escapeAttribute(material?.id || "")}">
        <label>开始秒数<input name="startSeconds" type="number" min="${evaluation.sourceStartMs / 1_000}" max="${evaluation.sourceEndMs / 1_000}" step="0.01" value="${escapeAttribute(localDraft?.startSeconds || "")}" required></label>
        <label>结束秒数<input name="endSeconds" type="number" min="${evaluation.sourceStartMs / 1_000}" max="${evaluation.sourceEndMs / 1_000}" step="0.01" value="${escapeAttribute(localDraft?.endSeconds || "")}" required></label>
        <label class="highlight-reason">为什么这是高光<input name="reason" maxlength="500" value="${escapeAttribute(localDraft?.reason || "")}" placeholder="例如：飞机穿过晚霞，画面最有冲击力" required></label>
        <label class="highlight-human-confirmation"><input name="humanConfirmed" type="checkbox" required>我已亲自看过这段视频，时间和理由是我的判断，不是直接照抄模型。</label>
        <button class="secondary-action" type="submit" data-highlight-submit disabled>提交我的人工判断并评分</button>
      </form>
      <div class="highlight-draft-state"><span data-highlight-draft-state>${localDraft ? "未提交草稿已从本机恢复；不计入人工标签、训练或发布。" : "尚无未提交草稿。"}</span><button class="quiet-action" type="button" data-highlight-draft-clear="${escapeAttribute(material?.id || "")}"${localDraft ? "" : " disabled"}>清除草稿</button></div>
      <p class="micro-boundary">人工标注只用于本次本地评价；训练资格：否 · 自动发布：否。</p>
    </article>`;
  }).join("");
}

function renderCoverageOptions(options, current) {
  return `<option value=""${current ? "" : " selected"} disabled>请选择</option>` + options.map(([value, label]) =>
    `<option value="${escapeAttribute(value)}"${current === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function highlightFormForMaterial(container, materialCardId) {
  return [...container.querySelectorAll("[data-highlight-form]")]
    .find((form) => form.dataset.highlightForm === materialCardId) || null;
}

function highlightCoverageFormForMaterial(container, materialCardId) {
  return [...container.querySelectorAll("[data-highlight-coverage-form]")]
    .find((form) => form.dataset.highlightCoverageForm === materialCardId) || null;
}

function focusNextHighlightAction() {
  const action = studioState.highlightBatch?.actionPlan?.nextAction;
  if (!action) return false;
  if (action.kind === "retest-batch") {
    void retestStudioHighlightBatch();
    return true;
  }
  const material = (studioState.materials || []).find((item) => item.sourceAssetId === action.sourceAssetId);
  const container = document.querySelector?.("#highlight-evaluations");
  if (!material?.id || !container) return false;
  const form = action.kind === "profile-existing"
    ? highlightCoverageFormForMaterial(container, material.id)
    : highlightFormForMaterial(container, material.id);
  if (!form) return false;
  form.scrollIntoView?.({ behavior: "smooth", block: "center" });
  if (action.kind === "profile-existing") {
    const status = form.querySelector?.("[data-highlight-coverage-status]");
    if (status) status.textContent = "这是当前优先任务：请播放视频后，确认光线、动作和环境。";
    form.elements?.namedItem?.("lighting")?.focus?.();
  } else {
    setHighlightFormStatus(form, "这是当前优先任务：请播放视频，再填写你自己的高光时间和理由。");
    form.elements?.namedItem?.("startSeconds")?.focus?.();
  }
  return true;
}

function highlightVideoForMaterial(container, materialCardId) {
  return [...container.querySelectorAll("[data-highlight-video]")]
    .find((video) => video.dataset.highlightVideo === materialCardId) || null;
}

function setHighlightFormStatus(form, message) {
  const materialCardId = form?.dataset?.highlightForm || "";
  const card = form?.closest?.(".highlight-evaluation-card");
  const output = [...(card?.querySelectorAll?.("[data-highlight-form-status]") || [])]
    .find((item) => item.dataset.highlightFormStatus === materialCardId);
  if (output) output.textContent = message;
}

function updateHighlightSubmitGate(form) {
  const confirmation = form?.elements?.namedItem?.("humanConfirmed");
  const submit = form?.querySelector?.("[data-highlight-submit]");
  if (!submit) return false;
  submit.disabled = !form?.dataset?.highlightForm || !confirmation?.checked;
  return !submit.disabled;
}

function focusNextPendingHighlight(completedMaterialCardId) {
  const materialsBySource = new Map((studioState.materials || []).map((item) => [item.sourceAssetId, item]));
  const next = (studioState.highlightEvaluations || []).find((evaluation) => {
    const material = materialsBySource.get(evaluation.sourceAssetId);
    return material?.id !== completedMaterialCardId && !isSyntheticHighlightFixture(material) &&
      (evaluation.labels || []).length === 0;
  });
  const materialCardId = materialsBySource.get(next?.sourceAssetId)?.id;
  const container = document.querySelector?.("#highlight-evaluations");
  const form = materialCardId && container ? highlightFormForMaterial(container, materialCardId) : null;
  if (!form) return false;
  setHighlightFormStatus(form, "上一条已记录。请播放这条视频，再填写你自己的时间和理由。");
  form.scrollIntoView?.({ behavior: "smooth", block: "center" });
  form.elements?.namedItem?.("startSeconds")?.focus?.();
  return true;
}

function applyHighlightCandidateToForm(form, startSeconds, endSeconds) {
  const start = Number(startSeconds);
  const end = Number(endSeconds);
  const startInput = form?.elements?.namedItem("startSeconds");
  const endInput = form?.elements?.namedItem("endSeconds");
  if (!startInput || !endInput || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return false;
  startInput.value = start.toFixed(2);
  endInput.value = end.toFixed(2);
  persistHighlightDraftFromForm(form);
  setHighlightFormStatus(form, `已带入模型建议的 ${start.toFixed(2)}–${end.toFixed(2)} 秒并保存为未提交草稿；请播放核对并填写你自己的理由，系统不会自动提交。`);
  form.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  return true;
}

function buildExistingHighlightDraftPlan() {
  const materialBySource = new Map((studioState.materials || []).map((item) => [item.sourceAssetId, item]));
  const tasks = (studioState.highlightEvaluations || []).flatMap((evaluation) => {
    const material = materialBySource.get(evaluation.sourceAssetId);
    const candidate = (evaluation.modelCandidates || [])[0];
    if (!material?.id || isSyntheticHighlightFixture(material) || (evaluation.labels || []).length > 0 || !candidate) return [];
    return [{
      materialCardId: material.id,
      sourceAssetId: evaluation.sourceAssetId,
      startSeconds: Number(candidate.startMs) / 1_000,
      endSeconds: Number(candidate.endMs) / 1_000,
    }];
  });
  return {
    tasks,
    boundary: { humanLabelsCreated: 0, trainingEligible: false, productionAccepted: false, automaticPublishing: false },
  };
}

function prepareExistingHighlightDrafts() {
  const plan = buildExistingHighlightDraftPlan();
  const container = document.querySelector?.("#highlight-evaluations");
  const materialBySource = new Map((studioState.materials || []).map((item) => [item.sourceAssetId, item]));
  let prepared = 0;
  let existingDraftsPreserved = 0;
  for (const task of plan.tasks) {
    if (readHighlightDraft(task.materialCardId)) {
      existingDraftsPreserved += 1;
      continue;
    }
    const form = container ? highlightFormForMaterial(container, task.materialCardId) : null;
    if (form && applyHighlightCandidateToForm(form, task.startSeconds, task.endSeconds)) prepared += 1;
  }
  const pendingWithoutCandidate = (studioState.highlightEvaluations || []).filter((evaluation) =>
    !isSyntheticHighlightFixture(materialBySource.get(evaluation.sourceAssetId)) &&
    (evaluation.labels || []).length === 0 && !(evaluation.modelCandidates || []).length).length;
  const status = container?.querySelector?.("[data-highlight-prepare-status]");
  if (status) status.textContent = prepared > 0
    ? `系统已整理 ${prepared} 条高光时间草稿；${existingDraftsPreserved > 0 ? `保留 ${existingDraftsPreserved} 条已有草稿；` : ""}${pendingWithoutCandidate > 0 ? `另有 ${pendingWithoutCandidate} 条没有可靠候选，` : ""}请内部运营播放复核。没有生成任何人工标签。`
    : plan.tasks.length > 0
      ? "已有草稿已保留，没有覆盖人工编辑；仍需内部运营播放复核。"
      : "当前没有可安全预填的模型候选；没有生成任何人工标签。";
  return { prepared, existingDraftsPreserved, pendingWithoutCandidate, ...plan.boundary };
}

function markHighlightBoundaryFromVideo(form, video, boundary) {
  const currentSeconds = Number(video?.currentTime);
  const inputName = boundary === "start" ? "startSeconds" : boundary === "end" ? "endSeconds" : "";
  const input = inputName ? form?.elements?.namedItem(inputName) : null;
  if (!input || !Number.isFinite(currentSeconds)) return false;
  const minimum = Number(input.min);
  const maximum = Number(input.max);
  const clamped = Math.min(Number.isFinite(maximum) ? maximum : currentSeconds,
    Math.max(Number.isFinite(minimum) ? minimum : 0, currentSeconds));
  input.value = clamped.toFixed(2);
  persistHighlightDraftFromForm(form);
  setHighlightFormStatus(form, `已把当前播放位置 ${clamped.toFixed(2)} 秒设为${boundary === "start" ? "开始" : "结束"}并保存草稿；提交前仍需人工核对时间和理由。`);
  return true;
}

function renderHighlightBatchGate(batch, brief = null) {
  if (!batch) return "";
  const statusLabels = {
    "insufficient-samples": "证据采集中，暂不能自动进入成片",
    "below-pilot-threshold": "批量门槛未通过",
    "pilot-threshold-met": "本地试运行门槛已通过",
  };
  const passRate = batch.passRate === null
    ? "暂无"
    : `${(Number(batch.passRate) * 100).toFixed(1)}%`;
  const p95Runtime = batch.p95RuntimeMs === null
    ? "暂无"
    : `${(Number(batch.p95RuntimeMs) / 1_000).toFixed(1)} 秒`;
  const failureStages = (batch.failureStages || []).map((item) =>
    `<li>${escapeHtml(item.stage)}：${Number(item.count || 0)} 次</li>`).join("");
  const coverage = batch.sampleCoverage || {};
  const actionPlan = batch.actionPlan || {};
  const deliveryEvidence = batch.deliveryEvidence || {
    labeledVideos: Number(batch.labeledVideos || 0), remainingVideos: Math.max(0, 20 - Number(batch.labeledVideos || 0)),
    readyForHumanAcceptance: false, productionAccepted: false,
  };
  const contentTypeLabels = { "single-product": "产品特写", "jewelry-making-process": "制作过程", "designer-viewpoint": "人物／口播" };
  const durationBandLabels = { short: "短片（≤30秒）", medium: "中片（30–90秒）", long: "长片（>90秒）" };
  const lightingLabels = { stable: "稳定光线", mixed: "混合光线", low: "低光环境" };
  const motionLabels = { "static-display": "静态展示", "handheld-product": "手持展示", "making-action": "制作动作", "person-speaking": "人物口播", "device-motion": "设备运动" };
  const environmentLabels = { "indoor-controlled": "室内可控", "indoor-mixed": "室内复杂", outdoor: "户外" };
  const coveredContentTypes = (coverage.contentTypes || []).map((item) => contentTypeLabels[item] || item);
  const coveredDurationBands = (coverage.durationBands || []).map((item) => durationBandLabels[item] || item);
  const coveredLighting = (coverage.lightingTypes || []).map((item) => lightingLabels[item] || item);
  const coveredMotion = (coverage.motionTypes || []).map((item) => motionLabels[item] || item);
  const coveredEnvironments = (coverage.environmentTypes || []).map((item) => environmentLabels[item] || item);
  const draftPlan = buildExistingHighlightDraftPlan();
  const nextActionLabels = {
    "profile-existing": "补下一张人工画像",
    "label-existing": "标注下一条现有视频",
    "retest-batch": "复测全部已标注视频",
  };
  return `<section class="highlight-batch-gate ${batch.automaticEditingEligible ? "is-ready" : "is-blocked"}" aria-label="批量验收门禁">
    <div class="card-heading"><h4>批量验收门禁</h4><span class="gate-chip ${batch.automaticEditingEligible ? "is-ready" : "is-blocked"}">${escapeHtml(statusLabels[batch.status] || batch.status)}</span></div>
    <div class="highlight-batch-metrics">
      <span><strong>${Number(batch.labeledVideos || 0)}/10</strong><small>本机试运行证据</small></span>
      <span><strong>${Number(deliveryEvidence.labeledVideos || 0)}/20</strong><small>交付验收证据</small></span>
      <span><strong>${passRate}</strong><small>当前通过率，目标 ≥80%</small></span>
      <span><strong>${p95Runtime}</strong><small>成功任务 P95，目标 ≤180 秒</small></span>
      <span><strong>${Number(batch.attemptedVideos || 0)}/${Number(batch.labeledVideos || 0)}</strong><small>运行回执覆盖</small></span>
    </div>
    ${Number(batch.remainingVideos || 0) > 0 ? `<p>当前证据未达到 10 条试运行参考线，但本轮不要求客户补拍；后续随日常上传自然积累。样本不足时不会开放自动成片资格。</p>` : ""}
    <p><strong>样本多样性：</strong>${coverage.status === "pilot-variety-met" ? "通过" : "未通过"}。已覆盖 ${coveredContentTypes.length}/2 类内容（${escapeHtml(coveredContentTypes.join("、") || "暂无")}），${coveredDurationBands.length}/2 类时长（${escapeHtml(coveredDurationBands.join("、") || "暂无")}）。</p>
    <p><strong>人工拍摄画像：</strong>${Number(coverage.profiledVideos || 0)}/${Number(batch.labeledVideos || 0)} 条。光线 ${coveredLighting.length}/2 类（${escapeHtml(coveredLighting.join("、") || "暂无")}），动作 ${coveredMotion.length}/2 类（${escapeHtml(coveredMotion.join("、") || "暂无")}），环境 ${coveredEnvironments.length}/2 类（${escapeHtml(coveredEnvironments.join("、") || "暂无")}）。</p>
    <p class="micro-boundary">内容类型来自上传时的选择，时长来自视频本身，光线、动作和环境必须由人看完后确认；模型猜测计入 0 条。</p>
    <details class="highlight-action-plan" open><summary>现有素材补验清单</summary>
      <p>现有 ${Number(actionPlan.existingEligibleVideos || 0)} 条可计入视频：还要标注 ${Number(actionPlan.existingVideosAwaitingLabel || 0)} 条，还要补人工画像 ${Number(actionPlan.existingVideosAwaitingProfile || 0)} 条。本轮不生成新增拍摄任务。</p>
      <button class="secondary-action" type="button" data-highlight-prepare-drafts${draftPlan.tasks.length > 0 ? "" : " disabled"}>系统整理高光草稿（${draftPlan.tasks.length}）</button>
      <output class="micro-boundary" data-highlight-prepare-status>系统只预填模型已有的时间建议，人工理由保持空白；由内部运营复核，客户无需操作。</output>
      <button class="primary-action" type="button" data-highlight-next-action${actionPlan.nextAction ? "" : " disabled"}>${escapeHtml(nextActionLabels[actionPlan.nextAction?.kind] || "现有素材已处理完")}</button>
      <button class="secondary-action" type="button" data-highlight-export-capture-plan>导出现有素材任务单 CSV</button>
      <p class="micro-boundary">清单只包含已有视频的人工标注和画像工作，不要求客户额外拍摄。</p>
    </details>
    ${failureStages ? `<details><summary>查看失败阶段统计</summary><ul>${failureStages}</ul></details>` : ""}
    <div class="highlight-batch-actions">
      <button class="secondary-action" type="button" data-highlight-retest-batch${Number(batch.labeledVideos || 0) > 0 ? "" : " disabled"}>复测全部已标注视频</button>
      <button class="secondary-action" type="button" data-highlight-export>导出验收回执</button>
      <button class="secondary-action" type="button" data-highlight-feedback-dataset-export>导出模型反馈候选包</button>
      <button class="primary-action" type="button" data-highlight-automatic-drafts${batch.automaticEditingEligible && brief?.id ? "" : " disabled"}>用通过门槛的模型生成本地样片</button>
    </div>
    <p class="micro-boundary">${batch.automaticEditingEligible ? (brief?.id ? "自动样片入口已开放；生成后仍需人工审片。" : "证据已达标，但要先确认本次创作方案。") : "自动样片入口保持锁定；当前只处理已有素材，其余样本随日常上传自然积累。达到可计入视频、样本多样性、人工画像、通过率和运行时间门槛后再开放。"}</p>
    <p class="micro-boundary">${deliveryEvidence.readyForHumanAcceptance ? "20 条交付证据已齐，可进入人工终验；这仍不等于生产通过。" : `交付证据还差 ${Number(deliveryEvidence.remainingVideos || 0)} 条，随日常上传自然积累。`} 生产验收：否 · 自动发布：否。</p>
  </section>`;
}

function renderCaptionEditor(draft) {
  const style = draft.captionStyle || {};
  const rows = (draft.captionCues || []).map((cue, index) => `<label class="caption-cue-row"><span>${index + 1}</span><textarea rows="2" maxlength="500" data-caption-cue-id="${escapeAttribute(cue.id)}" required>${escapeHtml(cue.text)}</textarea></label>`).join("");
  const revisionNote = draft.captionRevision
    ? `<p class="micro-boundary">本版由字幕修改生成：改字 ${draft.captionRevision.changedCueIds?.length || 0} 条${draft.captionRevision.styleChanged ? "，样式已调整" : ""}。</p>`
    : "";
  const datasetExport = draft.captionRevision
    ? `<div class="caption-feedback-actions"><button type="button" class="secondary-action" data-caption-feedback-review="${escapeAttribute(draft.id)}">查看修正记录</button><button type="button" class="secondary-action" data-caption-feedback-dataset-export>导出候选包</button></div><p class="micro-boundary">这是带原文、改文、时间和审核状态的错题本；不会自动训练。</p>${renderCaptionFeedbackReviewPanel(draft)}`
    : "";
  const open = studioState.captionFeedbackReviewDraftId === draft.id ? " open" : "";
  return `<details class="caption-editor-drawer"${open}><summary>直接修改字幕</summary>${revisionNote}
    <form class="caption-editor-form" data-caption-editor="${escapeAttribute(draft.id)}">
      <div class="caption-cue-list">${rows}</div>
      <div class="caption-style-grid">
        <label>字号<input type="number" name="fontSizePx" min="36" max="64" step="1" value="${escapeAttribute(style.fontSizePx || 48)}" required></label>
        <label>文字颜色<input type="color" name="primaryColor" value="${escapeAttribute(style.primaryColor || "#FFFFFF")}"></label>
        <label>描边颜色<input type="color" name="outlineColor" value="${escapeAttribute(style.outlineColor || "#000000")}"></label>
        <label>底部位置<input type="number" name="bottomMarginPx" min="100" max="280" step="5" value="${escapeAttribute(style.bottomMarginPx || 150)}" required><small>数字越大，字幕越往上</small></label>
      </div>
      <input type="hidden" name="template" value="${escapeAttribute(style.template || "clean-outline")}">
      <input type="hidden" name="outlineWidthPx" value="${escapeAttribute(style.outlineWidthPx || 4)}">
      <input type="hidden" name="maxCharsPerLine" value="${escapeAttribute(style.maxCharsPerLine || 14)}">
      <button type="submit" class="primary-action">生成字幕新版</button>
      <p class="micro-boundary">会生成新视频，旧版不会被覆盖。</p>
      ${datasetExport}
    </form>
  </details>`;
}

function renderCaptionFeedbackReviewPanel(draft) {
  const dataset = studioState.captionFeedbackDataset;
  if (!dataset || studioState.captionFeedbackReviewDraftId !== draft.id) return "";
  const allEntries = (dataset.entries || []).filter((entry) => entry.revisedDraftId === draft.id);
  const entries = studioCaptionFeedbackFilter === "all"
    ? allEntries
    : allEntries.filter((entry) => entry.workflowDecision === studioCaptionFeedbackFilter);
  const decisionLabels = {
    unreviewed: "待审核",
    adopted: "已通过",
    "changes-requested": "待修改",
    rejected: "不采用",
  };
  const materialBySource = new Map((studioState.materials || []).map((material) => [material.sourceAssetId, material]));
  const rows = entries.map((entry) => {
    const material = materialBySource.get(entry.sourceAssetId);
    const materialName = material?.displayName || material?.fileName || entry.sourceAssetId;
    const sourceTime = `${(Number(entry.sourceStartMs) / 1_000).toFixed(1)}–${(Number(entry.sourceEndMs) / 1_000).toFixed(1)} 秒`;
    return `<article class="caption-feedback-entry">
      <div class="caption-feedback-entry-heading"><strong>${escapeHtml(materialName)}</strong><span class="caption-feedback-status caption-feedback-${escapeAttribute(entry.workflowDecision)}">${escapeHtml(decisionLabels[entry.workflowDecision] || entry.workflowDecision)}</span></div>
      <dl><div><dt>原文</dt><dd>${escapeHtml(entry.beforeText)}</dd></div><div><dt>修正后</dt><dd>${escapeHtml(entry.afterText)}</dd></div></dl>
      <p>${sourceTime}${entry.workflowDecisionReason ? ` · ${escapeHtml(entry.workflowDecisionReason)}` : ""}</p>
      <details><summary>查看证据绑定</summary><small>来源 ${escapeHtml(entry.sourceAssetId)} · 摘要 ${escapeHtml(entry.sourceDigest)} · 时间线 ${(Number(entry.timelineStartMs) / 1_000).toFixed(1)}–${(Number(entry.timelineEndMs) / 1_000).toFixed(1)} 秒</small></details>
    </article>`;
  }).join("");
  return `<section class="caption-feedback-review" aria-label="字幕修正记录">
    <div class="caption-feedback-review-heading"><div><strong>修正记录</strong><small>本版 ${allEntries.length} 条 · 全部 ${Number(dataset.counts?.total || 0)} 条</small></div>
      <label>状态<select data-caption-feedback-filter><option value="all"${studioCaptionFeedbackFilter === "all" ? " selected" : ""}>全部</option><option value="unreviewed"${studioCaptionFeedbackFilter === "unreviewed" ? " selected" : ""}>待审核</option><option value="adopted"${studioCaptionFeedbackFilter === "adopted" ? " selected" : ""}>已通过</option><option value="changes-requested"${studioCaptionFeedbackFilter === "changes-requested" ? " selected" : ""}>待修改</option><option value="rejected"${studioCaptionFeedbackFilter === "rejected" ? " selected" : ""}>不采用</option></select></label>
    </div>
    ${rows || '<p class="caption-feedback-empty">这个状态下暂无修正记录。</p>'}
    <p class="micro-boundary">修正记录只用于本地评测候选；未经人工审数据，不进入训练，也不代表模型已学会。</p>
  </section>`;
}

function renderDraftVisualEvidence(evidence) {
  if (!evidence || evidence.status === "not-used") {
    return `<details class="visual-evidence visual-evidence-unused"><summary>本版未使用视觉模型</summary><p>${escapeHtml(evidence?.degradedReason || "没有与当前素材严格匹配的真实视觉模型回执；镜头依据剪辑规则和语音转写选择。")}</p></details>`;
  }
  const rows = (evidence.items || []).map((item, index) => `<li>
    <strong>镜头 ${index + 1}：${escapeHtml((item.observations || []).join(" · "))}</strong>
    <span>保留原因：${escapeHtml(item.selectionReason)}</span>
    <small>${escapeHtml(item.provider?.modelName || "本地视觉模型")} ${escapeHtml(item.provider?.modelVersion || "")} · 回执 ${escapeHtml(item.receiptJobId)}</small>
  </li>`).join("");
  const visuallySelectedCount = (evidence.items || []).filter((item) =>
    item.selectionReason?.includes("根据本地视觉时间证据定位")).length;
  const title = visuallySelectedCount > 0
    ? `视觉模型参与选片 · ${visuallySelectedCount} 个镜头`
    : evidence.status === "partial"
      ? `视觉模型仅做内容核对（证据不完整） · ${evidence.items?.length || 0} 个镜头`
      : `视觉模型仅做内容核对 · ${evidence.items?.length || 0} 个镜头`;
  return `<details class="visual-evidence visual-evidence-${escapeAttribute(evidence.status)}"><summary>${escapeHtml(title)}</summary>${evidence.degradedReason ? `<p>${escapeHtml(evidence.degradedReason)}</p>` : ""}<ol>${rows}</ol></details>`;
}

function externalEditStatusLabel(status, flow = {}) {
  const platformLabel = studioPlatformLabel(flow.handoff?.platformVariant);
  if (flow.error) return "上一步没完成，可保留填写内容后重试";
  if (status === "candidate-found" || (flow.candidates || []).some((candidate) => candidate.status === "pending-review")) {
    return `发现${platformLabel}剪映回传视频；请先完整播放检查，再决定是否确认为 V2`;
  }
  if (flow.prepared && !flow.openResult) {
    return `${platformLabel}剪映交接包已在后台准备好；当前没有打开剪映，需要精修时再点“打开剪映和回传文件夹”`;
  }
  if (flow.openResult && !flow.openResult.opened) return "剪映和交接文件夹都没能打开，请检查本机应用状态后重试";
  if (flow.openResult?.opened) {
    const opened = flow.openResult;
    const automation = opened.automation;
    if (automation?.timelineImported) {
      return `${platformLabel}视频已自动进入剪映时间线；字幕文件已随交接包准备，请导入字幕后再精修导出`;
    }
    if (automation?.materialImported) {
      return `${platformLabel}视频已自动导入剪映；请把 parent.mp4 拖到下方时间线`;
    }
    if (automation?.error === "jianying-accessibility-permission-required") {
      return "剪映已打开，但 Mac 未允许自动操作；请在系统设置中给当前应用开启辅助功能权限";
    }
    if (automation?.stage === "create-project" || automation?.stage === "confirm-project") {
      return "剪映已打开，但自动新建草稿没有完成；已打开交接包，可手动新建草稿后导入 parent.mp4";
    }
    if (automation?.stage === "open-import" || automation?.stage === "wait-import-panel"
      || automation?.stage === "open-documents" || automation?.stage === "open-bridge-folder"
      || automation?.stage === "choose-file") {
      return "剪映草稿已建立，但自动导入素材没有完成；已打开交接包，可手动导入 parent.mp4";
    }
    if (automation?.attempted) {
      return "自动导入没有完成，已打开交接包；可把 parent.mp4 拖进剪映继续";
    }
    if (flow.prepared?.mode === "jianying-draft" && opened.jianyingOpened) {
      return `${platformLabel}剪映工程已准备并打开；请检查时间线后继续精修`;
    }
    if (opened.jianyingOpened && opened.packageFolderOpened && opened.returnFolderOpened) {
      return `已打开剪映、${platformLabel}交接包和回传文件夹；请导入 parent.mp4，导出后放入回传文件夹`;
    }
    if (opened.packageFolderOpened && opened.returnFolderOpened) {
      return `${platformLabel}交接包和回传文件夹已打开；请在剪映手动导入 parent.mp4，再把导出视频放入回传文件夹`;
    }
    if (opened.jianyingOpened) return `剪映已打开；请手动导入${platformLabel}交接包中的 parent.mp4`;
    return "已打开部分交接窗口；请确认剪映、交接包和回传文件夹是否都可见";
  }
  return ({
    preparing: "准备交接包中",
    ready: "交接包已就绪",
    editing: "等待导出成片",
    "candidate-found": "发现待审核成片",
    returned: "已回传新版本",
    degraded: "交接包已就绪，等待手动精修",
    failed: "处理失败，可重新准备",
    cancelled: "已结束这次精修",
  })[status] || "填写修改要求后生成交接包";
}

function renderExternalEditPanel(draft) {
  const draftId = escapeAttribute(draft.id);
  const flow = studioState.externalEdits?.[draft.id] || null;
  const handoff = flow?.handoff || null;
  const candidates = flow?.candidates || [];
  const platformLabel = studioPlatformLabel(handoff?.platformVariant);
  const flowStatus = externalEditStatusLabel(handoff?.status, flow || {});
  const shouldOpen = Boolean(flow?.autoReturnWatch
    || handoff?.status === "candidate-found"
    || candidates.some((candidate) => candidate.status === "pending-review"));
  const candidateMarkup = candidates.map((candidate) => `<article class="external-edit-candidate">
    <span class="platform-version-chip">${escapeHtml(platformLabel)}</span>
    <video controls preload="metadata" data-external-preview="${escapeAttribute(candidate.id)}" src="${escapeAttribute(candidate.mediaUrl)}">无法播放候选视频，请下载后检查。</video>
    <p>${Number(candidate.durationSeconds || 0).toFixed(1)} 秒 · ${candidate.mediaFacts?.width || "?"}×${candidate.mediaFacts?.height || "?"} · ${candidate.status === "ignored" ? "已忽略" : "待你审核"}</p>
    <div class="review-actions"><button type="button" data-external-accept="${draftId}" data-candidate-id="${escapeAttribute(candidate.id)}" disabled>确认作为 V2</button><button type="button" data-external-ignore="${draftId}" data-candidate-id="${escapeAttribute(candidate.id)}"${candidate.status === "ignored" ? " disabled" : ""}>忽略</button></div>
  </article>`).join("");
  const detail = handoff ? `<div class="external-edit-progress" data-external-progress="${draftId}">
    <div class="external-edit-target"><span class="platform-version-chip">${escapeHtml(platformLabel)}</span><span>平台身份已跟随本次交接任务保存</span></div>
    <p data-external-status="${draftId}">${escapeHtml(flowStatus)}</p>
    ${flow?.prepared?.degradedReason ? `<p class="micro-boundary">${flow.prepared.health === "unsupported-version" ? "本机已识别到剪映" : "本机未识别到剪映"}；当前使用本机自动导入，不冒充剪映官方工程 API。</p>` : ""}
    <div class="review-actions"><button type="button" data-external-open="${draftId}">打开剪映和回传文件夹（会切到前台）</button><button type="button" data-external-refresh="${draftId}">检查回传视频</button></div>
    <form class="external-return-upload" data-external-return-upload="${draftId}">
      <label>已经导出？直接选择成片<input name="file" type="file" accept=".mp4,.mov,.m4v,video/mp4,video/quicktime" required></label>
      <button type="submit">上传并预览</button>
      <p class="micro-boundary">仅接收 30–60 秒、最大 256 MB 的视频；上传只进入待审核，不会自动通过，也不证明文件来自剪映。</p>
    </form>
    ${flow?.autoReturnWatch ? '<p class="micro-boundary">正在后台等待剪映回传；发现新 MP4 会自动显示，最长检查 10 分钟。</p>' : ""}
    ${candidateMarkup || "<p class=\"empty-state\">1. 主动打开剪映和回传文件夹 · 2. 在剪映精修并导出 MP4 到“回传”文件夹 · 3. 回到这里点“检查回传视频”。</p>"}
  </div>` : `<form class="external-edit-form studio-form-grid" data-external-edit-form="${draftId}">
    <label>修改说明<textarea name="revisionInstruction" maxlength="2000" required placeholder="例如：开头缩短，保留设计师讲返工原因的原话"></textarea></label>
    <label>必保留<textarea name="mustKeep" maxlength="4019" placeholder="一行一项，最多 20 项"></textarea></label>
    <label>这次精修哪个版本<select name="platformVariant"><option value="editorial-master">原视频母版</option><option value="xiaohongshu">小红书版</option><option value="douyin">抖音版</option><option value="wechat-channels">视频号版</option></select></label>
    <label class="external-source-consent">是否附带原素材：<input type="checkbox" name="includeSources"> 同意把这版用到的原素材也放进交接包</label>
    <button class="primary-action" type="submit">后台准备剪映交接</button>
    <p class="micro-boundary" data-external-status="${draftId}">只在后台生成视频、字幕和交接说明，不打开剪映、不抢占桌面。交接包完成后，你再决定什么时候进入剪映。</p>
  </form>`;
  return `<details class="external-edit-drawer" data-external-edit-panel="${draftId}"${shouldOpen ? " open" : ""}><summary>剪映精修</summary><p>保留当前版本不动；剪完导出后，预览并确认才会生成新版本。</p>${detail}</details>`;
}

function externalEditRequestOperationKey(draftId, body) {
  const signature = JSON.stringify(body);
  const prior = studioState.externalEditOperations?.[draftId];
  if (prior?.signature === signature) return prior.operationKey;
  const key = operationKey("external-edit");
  studioState.externalEditOperations = { ...(studioState.externalEditOperations || {}), [draftId]: { signature, operationKey: key } };
  return key;
}

async function submitExternalEditForm(form) {
  const draftId = form.dataset.externalEditForm;
  const draft = (studioState.drafts || []).find((item) => item.id === draftId);
  if (!draft || !studioState.session) return studioState;
  const revisionInstruction = formValue(form, "revisionInstruction").trim();
  if (!revisionInstruction) return studioState;
  const mustKeep = splitLines(formValue(form, "mustKeep"));
  const includeSources = Boolean(form.elements?.namedItem("includeSources")?.checked);
  const sourceAssetIds = includeSources
    ? [...new Set((draft.sourceMap || []).map((source) => source.sourceAssetId).filter(Boolean))]
    : [];
  const body = {
    parentDraftId: draft.id,
    parentDraftVersion: draft.version || 1,
    parentDraftDigest: draft.outputDigest,
    platformVariant: formValue(form, "platformVariant") || "editorial-master",
    revisionInstruction,
    mustKeep,
    includeSources,
    sourceAssetIds,
  };
  const flow = studioState.externalEdits?.[draftId] || {};
  flow.error = null;
  studioState.externalEdits = { ...(studioState.externalEdits || {}), [draftId]: flow };
  const statusNode = form.querySelector('[data-external-status]');
  if (statusNode) statusNode.textContent = "正在安全整理交接包…";
  try {
    if (!flow.handoff) {
      try {
        const created = await studioApi("/api/studio/external-edit-handoffs", {
          method: "POST",
          body: { ...body, operationKey: externalEditRequestOperationKey(draftId, body) },
        });
        flow.handoff = created.handoff;
      } catch (error) {
        if (!(error instanceof Error) || error.message !== "external-edit-active-handoff-exists") throw error;
        const existing = await studioApi("/api/studio/external-edit-handoffs", { method: "GET" });
        flow.handoff = (existing.handoffs || []).find((handoff) =>
          handoff.parentDraftId === draft.id && !["returned", "cancelled", "failed"].includes(handoff.status));
        if (!flow.handoff) throw error;
      }
      studioState.externalEdits[draftId] = flow;
    }
    const prepared = await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/prepare`, {
      method: "POST", body: {},
    });
    flow.handoff = prepared.handoff;
    flow.prepared = prepared;
    renderStudioState();
    const reopenedDrawer = document.querySelector(`[data-external-edit-panel="${draftId}"]`);
    if (reopenedDrawer) reopenedDrawer.open = true;
  } catch (error) {
    flow.error = error instanceof Error ? error.message : String(error);
    console.error("studio-external-edit-error", flow.error);
    if (flow.prepared) renderStudioState();
    else if (statusNode) statusNode.textContent = externalEditStatusLabel(flow.handoff?.status, flow);
  }
  return studioState;
}

async function openExternalEditPackage(draftId) {
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff) return studioState;
  flow.error = null;
  try {
    const openPath = `/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/open`;
    try {
      flow.openResult = await studioApi(openPath, { method: "POST", body: {} });
    } catch (error) {
      if (!(error instanceof Error) || error.message !== "external-edit-prepare-required") throw error;
      const prepared = await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/prepare`, {
        method: "POST", body: {},
      });
      flow.handoff = prepared.handoff;
      flow.prepared = prepared;
      flow.openResult = await studioApi(openPath, { method: "POST", body: {} });
    }
  } catch (error) {
    flow.error = error instanceof Error ? error.message : String(error);
  }
  if (flow.openResult?.opened && !flow.error) startExternalEditCandidatePoll(draftId);
  renderStudioState();
  return studioState;
}

async function refreshExternalEditCandidates(draftId) {
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff) return studioState;
  flow.error = null;
  try {
    const result = await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/candidates`);
    flow.candidates = result.candidates || [];
    if (flow.candidates.some((candidate) => candidate.status === "pending-review")) {
      flow.handoff.status = "candidate-found";
    }
  } catch (error) {
    flow.error = error instanceof Error ? error.message : String(error);
  }
  renderStudioState();
  return studioState;
}

async function uploadExternalEditCandidate(form) {
  const draftId = form.dataset.externalReturnUpload;
  const flow = studioState.externalEdits?.[draftId];
  const file = form.elements?.namedItem("file")?.files?.[0];
  if (!flow?.handoff || !file || !studioState.session) return studioState;
  const statusNode = form.closest("[data-external-progress]")?.querySelector("[data-external-status]");
  if (!/\.(mp4|mov|m4v)$/i.test(file.name)) {
    if (statusNode) statusNode.textContent = "请选择 MP4、MOV 或 M4V 视频";
    return studioState;
  }
  if (file.size > 268_435_456) {
    if (statusNode) statusNode.textContent = "文件超过 256 MB，请先重新导出较小版本";
    return studioState;
  }
  const sessionToken = studioState.session.token;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  if (statusNode) statusNode.textContent = "正在检查导出视频；完成后请先播放再确认…";
  try {
    const body = new FormData();
    body.append("contentType", "designer-viewpoint");
    body.append("operationKey", operationKey(`external-return-upload-${flow.handoff.id}`));
    body.append("file", file, file.name);
    const response = await fetch(
      `/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/candidates/import`,
      {
        method: "POST",
        headers: { authorization: `Bearer ${sessionToken}` },
        credentials: "same-origin",
        body,
      },
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "external-edit-upload-failed");
    if (studioState.session?.token !== sessionToken) return studioState;
    flow.candidates = [result.candidate, ...(flow.candidates || []).filter((item) => item.id !== result.candidate.id)];
    flow.handoff.status = "candidate-found";
    flow.previewedCandidates = (flow.previewedCandidates || []).filter((id) => id !== result.candidate.id);
    flow.error = null;
    stopExternalEditCandidatePoll();
    renderStudioState();
    const drawer = document.querySelector(`[data-external-edit-panel="${draftId}"]`);
    if (drawer) drawer.open = true;
  } catch (error) {
    const messages = {
      "external-edit-upload-duration-invalid": "成片需为 30–60 秒，请重新导出",
      "external-edit-upload-media-invalid": "视频无法读取，请重新导出 MP4",
      "external-edit-upload-file-name-invalid": "请选择 MP4、MOV 或 M4V 视频",
      "external-edit-upload-file-required": "请先选择一个导出视频",
      "external-edit-upload-too-large": "文件超过 256 MB，请先重新导出较小版本",
      "external-edit-upload-output-conflict": "本地已有冲突文件，请刷新页面后重试",
      "external-edit-upload-output-invalid": "本地保存检查失败，原文件没有被采用，请直接重试",
      "external-edit-upload-candidate-invalid": "上传已停止在审核前，原文件没有被采用，请直接重试",
      "external-edit-handoff-not-awaiting-return": "这个剪映交接已经结束，请从当前作品重新发起",
    };
    const code = error instanceof Error ? error.message : "";
    flow.error = code || "external-edit-upload-failed";
    if (statusNode) statusNode.textContent = messages[code] || "上传中断，原文件不会重复生成，可直接重试";
  } finally {
    if (submit) submit.disabled = false;
  }
  return studioState;
}

async function ignoreExternalEditCandidate(draftId, candidateId) {
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff) return studioState;
  try {
    await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/candidates/${encodeURIComponent(candidateId)}/ignore`, {
      method: "POST", body: { operationKey: operationKey(`ignore-${candidateId}`) },
    });
    await refreshExternalEditCandidates(draftId);
  } catch (error) {
    flow.error = error instanceof Error ? error.message : String(error);
    renderStudioState();
  }
  return studioState;
}

async function acceptExternalEditCandidate(draftId, candidateId) {
  const flow = studioState.externalEdits?.[draftId];
  if (!flow?.handoff || !flow.previewedCandidates?.includes(candidateId)) return studioState;
  flow.error = null;
  try {
    const result = await studioApi(`/api/studio/external-edit-handoffs/${encodeURIComponent(flow.handoff.id)}/candidates/${encodeURIComponent(candidateId)}/accept`, {
      method: "POST", body: { operationKey: flow.acceptOperationKey || (flow.acceptOperationKey = operationKey(`accept-${candidateId}`)) },
    });
    const returnedDraft = result.draft;
    const drafts = studioState.drafts || [];
    if (!drafts.some((draft) => draft.id === returnedDraft.id)) studioState.drafts = [...drafts, returnedDraft];
    flow.handoff.status = "returned";
    flow.acceptedDraftId = returnedDraft.id;
    const history = await studioApi("/api/studio/history");
    studioState.history = history.history;
    studioState.drafts = (history.history.drafts || []).filter((draft) =>
      draft.briefId === returnedDraft.briefId && draft.version >= returnedDraft.version - 1);
  } catch (error) {
    flow.error = error instanceof Error ? error.message : String(error);
  }
  renderStudioState();
  return studioState;
}

function renderCustomerFeedbackForm(draft, feedback = null) {
  const draftId = escapeAttribute(draft.id);
  const summary = feedback ? `<div class="feedback-summary"><p><strong>已收反馈：</strong>${escapeHtml(satisfactionLabel(feedback.satisfaction))}</p><p>${escapeHtml(feedback.comment || "未填写补充说明")}</p><p>问题类型：${renderInlineValues(feedback.issueCategories, "无")}</p><p>必须保留：${renderInlineValues(feedback.mustKeep, "无")}</p><p>必须删除：${renderInlineValues(feedback.mustRemove, "无")}</p><p>现有素材无法完成：${renderInlineValues(feedback.needsReshoot, "无")}</p></div>` : "";
  return `<details class="customer-feedback-drawer"><summary>评价这版</summary>${summary}<p class="micro-boundary">满意会解锁“确认通过”；不满意会整理修改建议，只用现有素材调整，不安排补拍。</p>
    <form class="customer-feedback-form studio-form-grid" data-customer-feedback="${draftId}">
      <label>看完觉得怎么样？<select name="satisfaction" required><option value="satisfied">满意</option><option value="partially-satisfied">部分满意</option><option value="unsatisfied">不满意</option></select></label>
      <label>一句话意见（可选）<textarea name="comment" maxlength="1000" placeholder="例如：节奏快一点，保留设计师讲选材的部分"></textarea></label>
      <details class="customer-feedback-more"><summary>详细修改要求（可选）</summary><div class="customer-feedback-more-fields">
        <fieldset><legend>问题类型（可多选）</legend>${[["content-direction", "内容方向"], ["shot-selection", "选镜"], ["pacing", "节奏"], ["copy", "文案"], ["style", "风格"], ["fact-error", "事实错误"], ["other", "其他"]].map(([value, label]) => `<label><input type="checkbox" name="issueCategories" value="${value}"> ${label}</label>`).join("")}</fieldset>
        <label>必须保留<textarea name="mustKeep" maxlength="4019" placeholder="一行一项，最多 20 项，每项 200 字"></textarea></label>
        <label>必须删除<textarea name="mustRemove" maxlength="4019" placeholder="一行一项，最多 20 项，每项 200 字"></textarea></label>
        <label>填写人<select name="submittedBy"><option value="customer-self-declared">客户本人填写</option><option value="operator-on-behalf">内部人员代录</option></select></label>
      </div></details>
      <button class="primary-action" type="submit">保存评价</button>
    </form></details>`;
}

function renderRevisionProposal(proposal, hasCurrentDrafts = false) {
  if (!proposal) return '<p class="empty-state">尚无修改提案。部分满意或不满意的反馈提交后，才会先生成提案。</p>';
  const gateText = proposal.status === "confirmed"
    ? hasCurrentDrafts
      ? "提案已确认，新版初稿已生成"
      : "提案已确认，等待生成新版初稿"
    : canConfirmRevisionProposal(proposal)
      ? "提案可以内部确认"
      : "提案仍有未解决项，不能确认";
  return `<article class="decision-card"><div class="card-heading"><strong>修改提案 V${escapeHtml(proposal.version || 1)}</strong><span class="offline-badge">${escapeHtml(proposal.capability?.label || "离线/模拟分析")}</span></div><p><strong>理解到的反馈：</strong>${escapeHtml(proposal.understoodFeedback)}</p><h3>计划修改</h3>${renderValueList(proposal.plannedChanges, "无")}<h3>保留内容</h3>${renderValueList(proposal.retainedElements, "无")}<h3>暂不能支持</h3>${renderValueList(proposal.unsupportedRequests, "无")}<h3>现有素材限制</h3>${renderValueList(proposal.captureGaps, "无；不会安排补拍")}<p class="proposal-gate"><strong>${gateText}</strong></p><details><summary>查看提案状态与编号</summary><p>${escapeHtml(proposal.status)}</p><code>${escapeHtml(proposal.id)}</code></details></article>`;
}

function renderStudioHistory(history) {
  if (!history) return '<p class="empty-state">尚无历史记录。</p>';
  const groups = [
    ["需求", history.demands || [], (item) => `${escapeHtml(item.goal)} · ${escapeHtml(item.targetAudience)} · ${escapeHtml(item.desiredEffect)} · 必须 ${renderInlineValues(item.mustInclude, "无")} · 避免 ${renderInlineValues(item.mustAvoid, "无")} · ${escapeHtml(item.status)}`],
    ["匹配评估", history.assessments || [], (item) => `${escapeHtml(satisfactionLabel(item.verdict))} · ${escapeHtml(item.safeScope)} · 缺口 ${renderInlineValues(item.unmetNeeds, "无")} · 可继续方式 ${renderInlineValues(item.recommendedCaptures, "无")}`],
    ["内容方向", history.opportunities || [], (item) => `${escapeHtml(item.goal)} · ${escapeHtml(item.rationale)}`],
    ["任务书", history.briefs || [], (item) => `${escapeHtml(item.goal)} · 复核 ${renderInlineValues(item.reviewRequirements, "无")}`],
    ["初稿", history.drafts || [], (item) => escapeHtml(item.status)],
    ["客户反馈", history.customerFeedback || [], (item) => `${escapeHtml(satisfactionLabel(item.satisfaction))} · ${escapeHtml(item.comment || "无补充说明")}`],
    ["修改提案", history.revisionProposals || [], (item) => `${escapeHtml(item.status)} · ${escapeHtml(item.understoodFeedback)}`],
  ];
  const populated = groups.filter(([, items]) => items.length);
  if (!populated.length) return '<p class="empty-state">尚无历史记录。</p>';
  return populated.map(([label, items, describe]) => `<section class="history-group"><h3>${label}</h3><ol>${items.map((item) => `<li><strong>V${escapeHtml(item.version || 1)}</strong> <code>${escapeHtml(item.id)}</code><br>${describe(item)}</li>`).join("")}</ol></section>`).join("");
}

function hasSatisfiedFeedbackForDraft(draftId) {
  return latestCustomerFeedbackForDraft(draftId)?.satisfaction === "satisfied";
}

function latestCustomerFeedbackForDraft(draftId) {
  const byId = new Map();
  for (const feedback of [...(studioState.history?.customerFeedback || []), studioState.customerFeedback]) {
    if (feedback?.draftId === draftId) byId.set(feedback.id, feedback);
  }
  return [...byId.values()].sort((left, right) => {
    const versionOrder = Number(right.version || 0) - Number(left.version || 0);
    if (versionOrder) return versionOrder;
    const timeOrder = String(right.createdAt || "").localeCompare(String(left.createdAt || ""));
    if (timeOrder) return timeOrder;
    return String(right.id || "").localeCompare(String(left.id || ""));
  })[0] || null;
}

function canConfirmRevisionProposal(proposal) {
  return Boolean(
    proposal &&
    proposal.status === "proposed" &&
    (proposal.unsupportedRequests || []).length === 0 &&
    (proposal.captureGaps || []).length === 0,
  );
}

function renderValueList(values, emptyLabel) {
  const rows = Array.isArray(values) ? values : [];
  return rows.length ? `<ul>${rows.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>` : `<p class="empty-state">${escapeHtml(emptyLabel)}</p>`;
}

function renderInlineValues(values, emptyLabel) {
  const rows = Array.isArray(values) ? values : [];
  return rows.length ? rows.map(escapeHtml).join("、") : escapeHtml(emptyLabel);
}

function satisfactionLabel(value) {
  return ({ satisfied: "满意", "partially-satisfied": "部分满意", unsatisfied: "不满意", ready: "适合继续", partial: "部分匹配", blocked: "暂不适合" })[value] || value || "未知";
}

function splitLines(value) {
  const rows = Array.isArray(value) ? value : String(value || "").split(/\r?\n/u);
  return [...new Set(rows.map((row) => String(row).trim()).filter(Boolean))];
}

function setStudioBusy(busy, message) {
  studioState.busy = busy;
  const status = document.querySelector("#studio-status");
  if (status) status.textContent = message;
  const quickSubmit = document.querySelector("#studio-quick-demand-form .quick-submit");
  if (quickSubmit) {
    quickSubmit.disabled = busy || !studioState.session;
    quickSubmit.textContent = busy ? "正在处理…" : "挑选素材";
  }
  const hero = document.querySelector(".studio-hero");
  if (hero && typeof hero.setAttribute === "function") {
    hero.setAttribute("aria-busy", String(busy && !studioState.inventory));
  }
  renderStudioState();
}

function setDisabled(selector, disabled) {
  const control = document.querySelector(selector);
  if (control) control.disabled = disabled;
}

function operationKey(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function studioErrorMessage(error) {
  const code = error instanceof Error ? error.message : String(error);
  const explanations = {
    "draft-quality-check-failed": "系统发现成片存在黑屏、静音、字幕重叠或时间不同步，已停止生成并清理失败文件。请检查素材或字幕后重试。",
    "caption-revision-quality-check-failed": "修改后的字幕没有通过时间轴或成片检查，失败版本已清理，不会出现在作品里。请缩短或调整字幕后重试。",
    "share-optimization-parent-not-adopted": "请先完成播放、评价并通过原作品，再生成竖屏分享版。",
    "share-optimization-quality-check-failed": "竖屏分享版没有通过本地成片检查，失败文件已清理，原作品没有修改。",
    "ffmpeg-studio-share-normalize-failed": "本机没有完成竖屏转码，失败文件已清理，原作品没有修改。可以保留页面后重试。",
  };
  if (explanations[code]) return explanations[code];
  return `本机处理没有完成（${code}），可以保留当前页面后重试。`;
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

function formValue(form, name) {
  return form.elements?.namedItem(name)?.value || "";
}

function bindStudioUi() {
  const customer = document.querySelector("#studio-customer");
  const quickDemandForm = document.querySelector("#studio-quick-demand-form");
  const voiceInput = document.querySelector("#studio-voice-input");
  const referenceShortcut = document.querySelector("#studio-add-reference");
  const demandForm = document.querySelector("#studio-demand-form");
  const assessDemand = document.querySelector("#assess-demand");
  const adjustDemand = document.querySelector("#adjust-demand");
  const briefAdjustDemand = document.querySelector("#brief-adjust-demand");
  const confirmBrief = document.querySelector("#confirm-brief");
  const makeDrafts = document.querySelector("#make-drafts");
  const confirmProposal = document.querySelector("#confirm-revision-proposal");
  const makeRevisionDrafts = document.querySelector("#make-revision-drafts");
  const refreshAnalysis = document.querySelector("#refresh-analysis");
  const analysisHistory = document.querySelector("#analysis-history");
  const materials = document.querySelector("#materials");
  const materialUploadForm = document.querySelector("#material-upload-form");
  const musicUploadForm = document.querySelector("#music-upload-form");
  const musicSelection = document.querySelector("#music-selection");
  const connectHardware = document.querySelector("#connect-hardware");
  const continueFromLibrary = document.querySelector("#continue-from-library");
  const hardwarePairingCard = document.querySelector("#hardware-pairing-card");
  const drafts = document.querySelector("#drafts");
  const highlightEvaluations = document.querySelector("#highlight-evaluations");
  const referenceUploadForm = document.querySelector("#reference-upload-form");
  const referenceHistoryList = document.querySelector("#reference-history-list");
  const refreshReferenceHistoryControl = document.querySelector("#refresh-reference-history");
  const referenceRightsForm = document.querySelector("#reference-rights-form");
  const analyzeReference = document.querySelector("#analyze-reference");
  const referenceStyleForm = document.querySelector("#reference-style-form");
  const referenceStyleRules = document.querySelector("#reference-style-rules");
  const createReferenceMappingControl = document.querySelector("#create-reference-mapping");
  const confirmReferenceMappingControl = document.querySelector("#confirm-reference-mapping");
  const renderReferenceDraftsControl = document.querySelector("#render-reference-drafts");
  const referenceDrafts = document.querySelector("#reference-drafts");
  document.querySelectorAll("[data-studio-tab]").forEach((control) => {
    control.addEventListener("click", () => {
      setStudioAppTab(control.dataset.studioTab);
      if (control.hasAttribute("data-focus-upload")) {
        document.querySelector('#material-upload-form input[name="file"]')?.focus();
      }
    });
  });
  continueFromLibrary?.addEventListener("click", () => { void continueFromStudioLibrary(); });
  customer?.addEventListener("change", (event) => switchStudioCustomer(event.currentTarget.value));
  materialUploadForm?.elements?.namedItem("file")?.addEventListener("change", (event) => {
    const selected = [...(event.currentTarget.files || [])];
    const fileName = materialUploadForm.querySelector("[data-material-file-name]");
    if (fileName) fileName.textContent = selected.length === 0
      ? "可一次选择多条视频"
      : selected.length === 1 ? selected[0].name : `已选择 ${selected.length} 条视频`;
  });
  materialUploadForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    void uploadStudioMaterial(event.currentTarget);
  });
  document.querySelector("#material-upload-results")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-export-material-upload-report]")) exportMaterialUploadReport();
  });
  musicUploadForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    void uploadStudioMusic(event.currentTarget);
  });
  musicSelection?.addEventListener("click", (event) => {
    if (event.target.closest("[data-clear-music]")) void clearStudioMusic();
    const selection = event.target.closest("[data-select-music]");
    if (selection) void selectStudioMusic(selection.dataset.selectMusic);
    if (event.target.closest("[data-refresh-music-recommendations]")) {
      void refreshStudioMusicRecommendations().then(() => renderStudioState());
    }
  });
  musicSelection?.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-music-mix-form]");
    if (!form) return;
    event.preventDefault();
    void updateStudioMusicMix(form);
  });
  connectHardware?.addEventListener("click", () => { void startStudioHardwarePairing(); });
  hardwarePairingCard?.addEventListener("click", (event) => {
    if (event.target.closest("[data-hardware-refresh]")) void refreshStudioInventory();
    if (event.target.closest("[data-hardware-pairing-retry]")) void startStudioHardwarePairing();
  });
  materials?.addEventListener("click", (event) => {
    const control = event.target.closest("[data-material-filter]");
    if (!control) return;
    const requested = control.dataset.materialFilter;
    studioMaterialFilter = studioMaterialCategories.some((category) => category.value === requested) ||
        studioState.materialTags?.tags?.some((tag) => customMaterialFilterValue(tag.id) === requested)
      ? requested
      : "all";
    renderStudioState();
  });
  materials?.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-material-tag-form]");
    if (!form) return;
    event.preventDefault();
    const input = form.elements?.namedItem("label");
    const label = String(input?.value || "").trim();
    if (label) void createStudioMaterialTag(label);
  });
  quickDemandForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = event.currentTarget.elements?.namedItem("prompt");
    const prompt = String(input?.value || "").trim() || "帮我看看现有素材能做什么内容";
    if (input && !input.value.trim()) input.value = prompt;
    void submitQuickStudioPrompt(prompt);
  });
  voiceInput?.addEventListener("click", () => startStudioVoiceInput(quickDemandForm));
  referenceShortcut?.addEventListener("click", () => {
    studioActiveTab = "library";
    studioLibraryMode = "reference";
    renderStudioAppChrome();
    const referenceAdvanced = document.querySelector(".reference-advanced");
    if (referenceAdvanced) referenceAdvanced.open = true;
    document.querySelector("#reference-workflow")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  demandForm?.addEventListener("input", () => beginStudioDemandEdit());
  demandForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitStudioDemand({
      contentType: formValue(event.currentTarget, "contentType"), goal: formValue(event.currentTarget, "goal"),
      targetAudience: formValue(event.currentTarget, "targetAudience"), desiredEffect: formValue(event.currentTarget, "desiredEffect"),
      mustInclude: formValue(event.currentTarget, "mustInclude"), mustAvoid: formValue(event.currentTarget, "mustAvoid"),
      submittedBy: formValue(event.currentTarget, "submittedBy"),
    });
  });
  assessDemand?.addEventListener("click", () => assessStudioDemand());
  const editDemand = () => {
    if (!beginStudioDemandEdit()) return;
    studioActiveTab = "create";
    renderStudioAppChrome();
    document.querySelector('#studio-quick-demand-form [name="prompt"]')?.focus();
  };
  adjustDemand?.addEventListener("click", editDemand);
  briefAdjustDemand?.addEventListener("click", editDemand);
  confirmBrief?.addEventListener("click", () => { void confirmBriefAndCreateDrafts(); });
  makeDrafts?.addEventListener("click", () => createStudioDrafts());
  confirmProposal?.addEventListener("click", () => confirmRevisionProposal());
  makeRevisionDrafts?.addEventListener("click", () => createStudioDrafts());
  refreshAnalysis?.addEventListener("click", () => refreshStudioModelWorkbench());
  referenceUploadForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const file = event.currentTarget.elements?.namedItem("file")?.files?.[0] || null;
    void uploadReferenceSample(file, formValue(event.currentTarget, "sourceKind"));
  });
  refreshReferenceHistoryControl?.addEventListener("click", () => refreshReferenceHistory());
  referenceHistoryList?.addEventListener("click", (event) => {
    const control = event.target.closest("[data-select-reference-history]");
    if (!control || control.disabled) return;
    const sourceKind = document.querySelector("#reference-history-source-kind")?.value || "self-owned";
    void registerHistoricalReference(control.dataset.selectReferenceHistory, sourceKind);
  });
  referenceRightsForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const reusableElements = [...event.currentTarget.querySelectorAll('[name="reusableElements"]:checked')]
      .map((input) => input.value);
    const sourceKind = studioState.reference.sample?.sourceKind;
    if (!sourceKind) return;
    void declareReferenceRights(sourceKind, reusableElements);
  });
  analyzeReference?.addEventListener("click", () => analyzeReferenceSample());
  referenceStyleRules?.addEventListener("change", (event) => {
    const input = event.target.closest("[data-reference-rule-id]");
    if (!input) return;
    updateReferenceRuleSelection(input.dataset.referenceRuleId, input.checked);
  });
  referenceStyleForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    void confirmReferenceStyle();
  });
  createReferenceMappingControl?.addEventListener("click", () => createReferenceMapping());
  confirmReferenceMappingControl?.addEventListener("click", () => confirmReferenceMapping());
  renderReferenceDraftsControl?.addEventListener("click", () => renderReferenceDrafts());
  referenceDrafts?.addEventListener("input", (event) => {
    const input = event.target.closest("[data-reference-revision-prompt]");
    if (!input) return;
    setReferenceDraftPrompt(input.dataset.referenceRevisionPrompt, input.value);
    const button = findReferenceCardControl(
      referenceDrafts, "[data-reference-revise]", "referenceRevise", input.dataset.referenceRevisionPrompt,
    );
    if (button) button.disabled = !String(input.value || "").trim() || Boolean(studioState.reference.busyAction);
  });
  referenceDrafts?.addEventListener("change", (event) => {
    const input = event.target.closest("[data-reference-approval-confirm]");
    if (!input) return;
    setReferenceDraftApprovalConfirmation(input.dataset.referenceApprovalConfirm, input.checked);
    const button = findReferenceCardControl(
      referenceDrafts, "[data-reference-approve]", "referenceApprove", input.dataset.referenceApprovalConfirm,
    );
    const draft = findReferenceDraft(input.dataset.referenceApprovalConfirm);
    if (button) button.disabled = !input.checked || draft?.status !== "reviewable" || Boolean(studioState.reference.busyAction);
  });
  referenceDrafts?.addEventListener("click", (event) => {
    const revise = event.target.closest("[data-reference-revise]");
    if (revise && !revise.disabled) {
      void reviseReferenceDraft(revise.dataset.referenceRevise);
      return;
    }
    const approve = event.target.closest("[data-reference-approve]");
    if (approve && !approve.disabled) void approveReferenceDraft(approve.dataset.referenceApprove);
  });
  materials?.addEventListener("change", (event) => {
    const tagInput = event.target.closest("[data-material-tag-id][data-material-card-id]");
    if (tagInput) {
      void assignStudioMaterialTag(
        tagInput.dataset.materialCardId,
        tagInput.dataset.materialTagId,
        tagInput.checked,
      );
      return;
    }
    const input = event.target.closest("[data-studio-fact-id]");
    if (!input) return;
    const selected = new Set(studioState.selectedFactIds);
    if (input.checked) selected.add(input.dataset.studioFactId);
    else selected.delete(input.dataset.studioFactId);
    studioState.selectedFactIds = [...selected];
  });
  for (const container of [materials, drafts]) {
    container?.addEventListener("click", (event) => {
      const navigation = event.target.closest("[data-studio-tab]");
      if (navigation) setStudioAppTab(navigation.dataset.studioTab);
    });
  }
  materials?.addEventListener("click", (event) => {
    const control = event.target.closest("[data-start-analysis]");
    if (!control || control.disabled) return;
    void startStudioAnalysis(control.dataset.startAnalysis, ["technical", "scene", "transcript", "embedding", "vision"]);
  });
  analysisHistory?.addEventListener("click", (event) => {
    const suitability = event.target.closest("[data-material-suitability]");
    if (suitability && !suitability.disabled) {
      const receipt = findStudioAnalysisReceipt(suitability.dataset.receiptJobId);
      if (!receipt) return;
      const reason = window.prompt("请说明这份素材为什么适合或不适合当前需求", "");
      if (!String(reason || "").trim()) return;
      void submitStudioMaterialSuitability(receipt, {
        demandId: suitability.dataset.demandId,
        matchAssessmentId: suitability.dataset.matchAssessmentId,
      }, suitability.dataset.materialSuitability, String(reason).trim());
      return;
    }
    const control = event.target.closest("[data-analysis-review-action]");
    if (!control || control.disabled) return;
    const receipt = findStudioAnalysisReceipt(control.dataset.receiptJobId);
    if (!receipt) return;
    const action = control.dataset.analysisReviewAction;
    const correction = action === "correct-suggestion"
      ? window.prompt("请写下修正后的人工结论", control.dataset.reviewTarget || "")
      : null;
    if (action === "correct-suggestion" && !String(correction || "").trim()) return;
    void submitStudioAnalysisReview(receipt, action, control.dataset.reviewTarget, correction);
  });
  drafts?.addEventListener("submit", (event) => {
    const captionForm = event.target.closest("[data-caption-editor]");
    if (captionForm) {
      event.preventDefault();
      void submitCaptionRevision(captionForm);
      return;
    }
    const externalReturnUpload = event.target.closest("[data-external-return-upload]");
    if (externalReturnUpload) {
      event.preventDefault();
      void uploadExternalEditCandidate(externalReturnUpload);
      return;
    }
    const externalForm = event.target.closest("[data-external-edit-form]");
    if (externalForm) {
      event.preventDefault();
      void submitExternalEditForm(externalForm);
      return;
    }
    const form = event.target.closest("[data-customer-feedback]");
    if (!form) return;
    event.preventDefault();
    const categories = [...form.querySelectorAll('[name="issueCategories"]:checked')].map((input) => input.value);
    void submitCustomerDraftFeedback({
      draftId: form.dataset.customerFeedback, satisfaction: formValue(form, "satisfaction"), issueCategories: categories,
      comment: formValue(form, "comment") || null, mustKeep: formValue(form, "mustKeep"), mustRemove: formValue(form, "mustRemove"),
      needsReshoot: "", submittedBy: formValue(form, "submittedBy"),
    });
  });
  drafts?.addEventListener("click", (event) => {
    const captionDatasetReview = event.target.closest("[data-caption-feedback-review]");
    if (captionDatasetReview && !captionDatasetReview.disabled) {
      void loadStudioCaptionFeedbackDataset(captionDatasetReview.dataset.captionFeedbackReview);
      return;
    }
    const captionDatasetExport = event.target.closest("[data-caption-feedback-dataset-export]");
    if (captionDatasetExport && !captionDatasetExport.disabled) {
      void exportStudioCaptionFeedbackDataset();
      return;
    }
    const aiEdit = event.target.closest("[data-open-ai-edit]");
    if (aiEdit) {
      const drawer = aiEdit.closest(".draft-card")?.querySelector(".customer-feedback-drawer");
      if (drawer) {
        drawer.open = true;
        drawer.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      }
      return;
    }
    const reviewBeforeAdopt = event.target.closest("[data-review-before-adopt]");
    if (reviewBeforeAdopt) {
      const drawer = reviewBeforeAdopt.closest(".draft-card")?.querySelector(".customer-feedback-drawer");
      if (drawer) {
        drawer.open = true;
        drawer.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      }
      return;
    }
    const externalEdit = event.target.closest("[data-open-external-edit]");
    if (externalEdit) {
      const drawer = externalEdit.closest(".draft-card")?.querySelector(".external-edit-drawer");
      if (drawer) {
        drawer.open = true;
        drawer.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
      }
      return;
    }
    const shareDraft = event.target.closest("[data-share-draft]");
    if (shareDraft && !shareDraft.disabled) {
      void shareDraftMedia(shareDraft.dataset.shareDraft, shareDraft);
      return;
    }
    const createShare = event.target.closest("[data-create-share-version]");
    if (createShare && !createShare.disabled) {
      void createShareVersion(createShare.dataset.createShareVersion, createShare);
      return;
    }
    const copyDistribution = event.target.closest("[data-copy-distribution]");
    if (copyDistribution) {
      void copyDistributionKitText(
        copyDistribution.dataset.kitDraft,
        copyDistribution.dataset.copyDistribution,
        copyDistribution,
      );
      return;
    }
    const downloadCover = event.target.closest("[data-download-draft-cover]");
    if (downloadCover && !downloadCover.disabled) {
      void downloadCurrentDraftCover(downloadCover.dataset.downloadDraftCover, downloadCover);
      return;
    }
    const downloadNote = event.target.closest("[data-download-distribution-note]");
    if (downloadNote) {
      downloadDistributionNote(downloadNote.dataset.downloadDistributionNote);
      return;
    }
    const downloadDraft = event.target.closest("[data-download-draft]");
    if (downloadDraft && downloadDraft.getAttribute("aria-disabled") !== "true") {
      event.preventDefault();
      void downloadDraftMedia(downloadDraft.dataset.downloadDraft, downloadDraft);
      return;
    }
    const externalOpen = event.target.closest("[data-external-open]");
    if (externalOpen) { void openExternalEditPackage(externalOpen.dataset.externalOpen); return; }
    const externalRefresh = event.target.closest("[data-external-refresh]");
    if (externalRefresh) { void refreshExternalEditCandidates(externalRefresh.dataset.externalRefresh); return; }
    const externalAccept = event.target.closest("[data-external-accept]");
    if (externalAccept && !externalAccept.disabled) {
      void acceptExternalEditCandidate(externalAccept.dataset.externalAccept, externalAccept.dataset.candidateId);
      return;
    }
    const externalIgnore = event.target.closest("[data-external-ignore]");
    if (externalIgnore && !externalIgnore.disabled) {
      void ignoreExternalEditCandidate(externalIgnore.dataset.externalIgnore, externalIgnore.dataset.candidateId);
      return;
    }
    const adopt = event.target.closest("[data-adopt-draft]");
    if (adopt && !adopt.disabled) void submitStudioFeedback(adopt.dataset.adoptDraft, "adopt");
  });
  drafts?.addEventListener("loadedmetadata", (event) => {
    const video = event.target.closest?.("[data-draft-video]");
    if (!video || !Number(video.videoWidth) || !Number(video.videoHeight)) return;
    const resolution = video.closest?.(".draft-card")?.querySelector?.("[data-draft-resolution]");
    if (resolution) resolution.textContent = `${video.videoWidth}×${video.videoHeight}`;
    const draftId = video.dataset?.draftVideo;
    const draft = (studioState.drafts || []).find((item) => item.id === draftId);
    if (draftId && draft) updateDraftDeliveryCheck(
      draftId, video.videoWidth, video.videoHeight, video.duration || draft.durationSeconds,
    );
  }, true);
  drafts?.addEventListener("change", (event) => {
    const filter = event.target.closest("[data-caption-feedback-filter]");
    if (!filter) return;
    studioCaptionFeedbackFilter = ["all", "unreviewed", "adopted", "changes-requested", "rejected"].includes(filter.value)
      ? filter.value
      : "all";
    renderStudioState();
  });
  highlightEvaluations?.addEventListener("submit", (event) => {
    const coverageForm = event.target.closest("[data-highlight-coverage-form]");
    if (coverageForm) {
      event.preventDefault();
      void submitStudioHighlightCoverage(coverageForm);
      return;
    }
    const form = event.target.closest("[data-highlight-form]");
    if (!form) return;
    event.preventDefault();
    void submitStudioHighlight(form);
  });
  highlightEvaluations?.addEventListener("input", (event) => {
    const form = event.target.closest?.("[data-highlight-form]");
    if (form && event.target?.name !== "humanConfirmed") persistHighlightDraftFromForm(form);
  });
  highlightEvaluations?.addEventListener("change", (event) => {
    if (event.target?.name !== "humanConfirmed") return;
    const form = event.target.closest?.("[data-highlight-form]");
    if (!form) return;
    const enabled = updateHighlightSubmitGate(form);
    setHighlightFormStatus(form, enabled
      ? "已确认这是你的人工判断；提交后才会计入人工标签。"
      : "人工确认已取消；当前内容仍只是未提交草稿。");
  });
  highlightEvaluations?.addEventListener("click", (event) => {
    const prepareDrafts = event.target.closest("[data-highlight-prepare-drafts]");
    if (prepareDrafts && !prepareDrafts.disabled) {
      prepareExistingHighlightDrafts();
      return;
    }
    const exportCapturePlan = event.target.closest("[data-highlight-export-capture-plan]");
    if (exportCapturePlan && !exportCapturePlan.disabled) {
      exportStudioCaptureTaskList();
      return;
    }
    const nextAction = event.target.closest("[data-highlight-next-action]");
    if (nextAction && !nextAction.disabled) {
      focusNextHighlightAction();
      return;
    }
    const draftClear = event.target.closest("[data-highlight-draft-clear]");
    if (draftClear && !draftClear.disabled) {
      const materialCardId = draftClear.dataset.highlightDraftClear;
      const form = highlightFormForMaterial(highlightEvaluations, materialCardId);
      clearHighlightDraft(materialCardId, form);
      return;
    }
    const candidate = event.target.closest("[data-use-highlight-candidate]");
    if (candidate && !candidate.disabled) {
      const form = highlightFormForMaterial(highlightEvaluations, candidate.dataset.useHighlightCandidate);
      if (form) applyHighlightCandidateToForm(form, candidate.dataset.highlightStart, candidate.dataset.highlightEnd);
      return;
    }
    const candidateAccept = event.target.closest("[data-accept-highlight-candidate]");
    if (candidateAccept && !candidateAccept.disabled) {
      void acceptStudioHighlightCandidate(candidateAccept);
      return;
    }
    const candidateReject = event.target.closest("[data-reject-highlight-candidate]");
    if (candidateReject && !candidateReject.disabled) {
      void rejectStudioHighlightCandidate(candidateReject);
      return;
    }
    const startMarker = event.target.closest("[data-highlight-mark-start]");
    const endMarker = event.target.closest("[data-highlight-mark-end]");
    const marker = startMarker || endMarker;
    if (marker && !marker.disabled) {
      const materialCardId = startMarker?.dataset.highlightMarkStart || endMarker?.dataset.highlightMarkEnd;
      const form = highlightFormForMaterial(highlightEvaluations, materialCardId);
      const video = highlightVideoForMaterial(highlightEvaluations, materialCardId);
      if (form && video) markHighlightBoundaryFromVideo(form, video, startMarker ? "start" : "end");
      return;
    }
    const batchRetest = event.target.closest("[data-highlight-retest-batch]");
    if (batchRetest && !batchRetest.disabled) {
      void retestStudioHighlightBatch();
      return;
    }
    const automaticDrafts = event.target.closest("[data-highlight-automatic-drafts]");
    if (automaticDrafts && !automaticDrafts.disabled) {
      void createAutomaticHighlightDrafts();
      return;
    }
    const reportExport = event.target.closest("[data-highlight-export]");
    if (reportExport && !reportExport.disabled) {
      void exportStudioHighlightReport();
      return;
    }
    const datasetExport = event.target.closest("[data-highlight-feedback-dataset-export]");
    if (datasetExport && !datasetExport.disabled) {
      void exportStudioHighlightFeedbackDataset();
      return;
    }
    const control = event.target.closest("[data-highlight-retest]");
    if (control && !control.disabled) void retestStudioHighlight(control.dataset.highlightRetest);
  });
  drafts?.addEventListener("play", (event) => {
    const preview = event.target.closest("[data-external-preview]");
    if (!preview) return;
    const candidateId = preview.dataset.externalPreview;
    const draftId = preview.closest("[data-external-edit-panel]")?.dataset.externalEditPanel;
    if (!draftId || !candidateId) return;
    const flow = studioState.externalEdits?.[draftId] || {};
    const candidate = (flow.candidates || []).find((item) => item.id === candidateId);
    if (candidate?.status !== "pending-review") return;
    flow.previewedCandidates = [...new Set([...(flow.previewedCandidates || []), candidateId])];
    studioState.externalEdits[draftId] = flow;
    const accept = preview.closest(".external-edit-candidate")?.querySelector("[data-external-accept]");
    if (accept) accept.disabled = false;
  }, true);
  void switchStudioCustomer(customer?.value || "synthetic-customer-a");
}

function startStudioVoiceInput(form) {
  const status = document.querySelector("#studio-status");
  const input = form?.elements?.namedItem("prompt");
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    if (status) status.textContent = "当前环境不支持语音输入，请继续使用文字";
    input?.focus();
    return false;
  }
  const recognition = new SpeechRecognition();
  recognition.lang = "zh-CN";
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => { if (status) status.textContent = "正在听，说完后会填入创作框…"; };
  recognition.onerror = () => { if (status) status.textContent = "这次没有听清，可以再试一次或直接输入文字"; };
  recognition.onresult = (event) => {
    const transcript = String(event.results?.[0]?.[0]?.transcript || "").trim();
    if (input && transcript) input.value = transcript;
    if (status) status.textContent = transcript ? "已填入语音内容，确认后点击「让 AI 帮我整理」" : "没有识别到内容";
  };
  recognition.start();
  return true;
}

function findStudioAnalysisReceipt(receiptJobId) {
  return (studioState.analysisHistory?.entries || [])
    .flatMap((entry) => entry.receipts || [])
    .find((receipt) => receipt.jobId === receiptJobId) || null;
}

document.addEventListener("DOMContentLoaded", bindStudioUi);
