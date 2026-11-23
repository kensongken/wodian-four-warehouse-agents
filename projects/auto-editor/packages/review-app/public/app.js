const workspace = document.querySelector("#workspace");
const status = document.querySelector("#status");
const batchInput = document.querySelector("#batch-id");
const pipeline = document.querySelector("#pipeline");
const pendingCount = document.querySelector("#pending-count");
const viewTitle = document.querySelector("#view-title");
const viewDescription = document.querySelector("#view-description");
const viewKicker = document.querySelector("#view-kicker");
const appTitle = document.querySelector("#app-title");
const creativeSessionHeader = document.querySelector("#creative-session-header");
const productionToolbar = document.querySelector("#production-toolbar");
const pipelineCard = document.querySelector("#pipeline-card");
let activeView = "creative";
let selectedStoryId;
let selectedVariantKind = "editorial-master";
let editorTrigger;
let reviewSession;
let creativeSessionBootstrap;
let currentReviewData;
let currentApprovedData = { approvals: [] };
let currentCreativeView;
let creativeSessionList = [];
let selectedCreativeSessionId = null;
let materialSelectionState = { key: null, ids: new Set() };
let lastCreativeFocusKey;
let creativeRequestTimeoutMs = 30_000;
const creativePendingStorageKey = "wearable.creative.pending-operations.v1";
const creativeClientOwnerStorageKey = "wearable.creative.client-owner.v1";
const creativePendingMaxEntries = 32;
const restoredCreativePendingState = restoreCreativePendingOperations();
const creativePendingOperations = restoredCreativePendingState.operations;
let creativePendingStorageIssue = restoredCreativePendingState.issue;
let creativeTabLease;
const creativeRequestCoordinator = {
  epoch: 0,
  loadController: null,
  detailController: null,
  mutationController: null,
  mutationInFlight: false,
  pendingCreativeReload: false,
  previewObjectUrls: new Set(),
  previewControllers: new Set(),
};

const viewCopy = {
  creative: ["今天的创作", "从一句想法开始，一步一步完成拍摄。"],
  library: ["素材库", "安全查看新素材、历史库存、权限和使用记录。"],
  review: ["今日待审", "一次看一个版本，决定通过或要求改剪。"],
  approved: ["发布中心", "查看已经批准的内容和手机接收状态。"],
  profile: ["我的", "总编身份、生产规则与系统状态。"],
};

document.querySelector("#today-label").textContent = new Intl.DateTimeFormat("zh-CN", {
  month: "long", day: "numeric", weekday: "short",
}).format(new Date());

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    activeView = button.dataset.view;
    loadView();
  });
});
document.querySelector("#refresh").addEventListener("click", () => {
  loadView();
});

async function loadView() {
  const requestedView = activeView;
  const epoch = advanceCreativeEpoch();
  syncViewChrome(requestedView);
  if (requestedView === "creative" && creativeRequestCoordinator.mutationInFlight) {
    creativeRequestCoordinator.pendingCreativeReload = true;
    status.textContent = "正在完成当前操作…";
    workspace.innerHTML = '<div class="empty"><strong>正在完成刚才的操作</strong><p>完成后会自动读取最新创作阶段。</p></div>';
    return;
  }
  const controller = createRequestController();
  creativeRequestCoordinator.loadController = controller;
  const signal = controller?.signal;
  status.textContent = "同步中…";
  try {
    if (requestedView === "creative") {
      creativeRequestCoordinator.pendingCreativeReload = false;
      await loadCreativeView({ epoch, controller, signal, requestedView });
      if (!isCurrentViewLoad(epoch, requestedView)) return;
      status.textContent = "刚刚更新";
      return;
    }
    const batchPath = `/api/batches/${encodeURIComponent(batchInput.value)}`;
    const [batch, review, approved] = await Promise.all([
      api(batchPath, { signal }), api("/api/review", { signal }), api("/api/approved", { signal }),
    ]);
    if (!isCurrentViewLoad(epoch, requestedView)) return;
    const scoped = scopeProductionData(batch, review, approved);
    currentReviewData = scoped.review;
    currentApprovedData = scoped.approved;
    if (pendingCount) pendingCount.textContent = String(countPendingArtifacts(scoped.review, scoped.approved));
    renderPipeline(batch, scoped.review, scoped.approved);

    if (requestedView === "review") renderReview(scoped.review);
    else if (requestedView === "approved") renderApproved(scoped.approved);
    else if (requestedView === "profile") renderProfile();
    else {
      const library = await api("/api/library", { signal });
      if (!isCurrentViewLoad(epoch, requestedView)) return;
      renderLibrary(library);
    }
    status.textContent = "刚刚更新";
  } catch (error) {
    if (error?.name === "AbortError" || !isCurrentViewLoad(epoch, requestedView)) return;
    status.textContent = "需要处理";
    if (creativePendingStorageIssue) {
      renderCreativeOperationRecovery();
    } else {
      workspace.innerHTML = `<div class="empty"><strong>暂时无法读取生产数据</strong><p>${escapeHtml(friendlyCreativeError(error.message))}</p></div>`;
    }
  } finally {
    if (creativeRequestCoordinator.loadController === controller) {
      creativeRequestCoordinator.loadController = null;
    }
  }
}

function syncViewChrome(requestedView) {
  const [title, description] = viewCopy[requestedView];
  viewTitle.textContent = title;
  viewDescription.textContent = description;
  appTitle.textContent = requestedView === "creative" ? "创作任务" : title;
  viewKicker.textContent = requestedView === "creative" ? "CREATIVE FLOW" : "EDITORIAL DESK";
  productionToolbar.hidden = requestedView === "creative";
  pipelineCard.hidden = requestedView === "creative";
  creativeSessionHeader.hidden = requestedView !== "creative";
  document.querySelectorAll("[data-view]").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === requestedView);
  });
}

function isCurrentViewLoad(epoch, requestedView) {
  return epoch === creativeRequestCoordinator.epoch && requestedView === activeView;
}

async function loadCreativeView({ epoch, controller, signal, requestedView = "creative" } = {}) {
  await recoverPendingCreativeOperations();
  const list = await withCreativeRequestTimeout(
    () => api("/api/creative-sessions", { signal }),
    controller,
  );
  if (epoch !== undefined && !isCurrentViewLoad(epoch, requestedView)) return;
  if (!list.sessions?.length) {
    creativeSessionList = [];
    currentCreativeView = { stage: "draft_intent", title: "新建创作" };
    renderCreativeSession(currentCreativeView);
    return;
  }
  creativeSessionList = [...list.sessions].sort((left, right) =>
    right.updatedAt.localeCompare(left.updatedAt));
  const session = creativeSessionList.find((item) => item.id === selectedCreativeSessionId) ??
    creativeSessionList[0];
  selectedCreativeSessionId = session.id;
  await reloadCreativeSession(session.id, {
    epoch,
    controller,
    signal,
    isCurrent: () => epoch === undefined || isCurrentViewLoad(epoch, requestedView),
  });
}

async function reloadCreativeSession(sessionId, {
  epoch = creativeRequestCoordinator.epoch,
  controller = createRequestController(),
  signal,
  isCurrent = () => epoch === creativeRequestCoordinator.epoch,
  render = true,
} = {}) {
  const requestSignal = controller?.signal ?? signal;
  const response = await withCreativeRequestTimeout(
    () => api(`/api/creative-sessions/${encodeURIComponent(sessionId)}`, { signal: requestSignal }),
    controller,
  );
  if (!isCurrent()) return null;
  if (!render) return response.session;
  currentCreativeView = response.session;
  renderCreativeSession(currentCreativeView);
  return currentCreativeView;
}

function renderCreativeSession(sessionView) {
  const renderers = {
    draft_intent: renderIntentComposer,
    awaiting_material_consent: renderMaterialConsent,
    reviewing_materials: renderMaterialSelection,
    materials_declined: renderScriptDraftAction,
    awaiting_script_confirmation: renderScriptReview,
    ready_to_dispatch: renderDispatchConfirmation,
    shooting: renderShootingConsole,
    ingesting: renderIngestProgress,
    editorial_review: renderEditorialHandoff,
    completed: renderCompletedSession,
    paused: renderPausedSession,
    cancelled: renderClosedSession,
    invalidated: renderClosedSession,
  };
  const renderer = renderers[sessionView.stage] ?? renderClosedSession;
  resetMaterialPreviews();
  const focusKey = `${sessionView.id ?? "new"}:${sessionView.stage}:${sessionView.currentScriptVersion ?? 0}:${sessionView.currentShotPlanVersion ?? 0}`;
  const selectionView = materialSelectionView(sessionView);
  if (!selectionView) clearMaterialSelection();
  else materialSelectionFor(selectionView);
  updateCreativeHeader(sessionView);
  workspace.innerHTML = creativeSessionSwitcherMarkup(creativeSessionList, sessionView.id) +
    renderer(sessionView).replace("<h3", '<h3 data-stage-heading tabindex="-1"') +
    creativeOperationRecoveryMarkup();
  bindCreativeActions(sessionView);
  bindCreativeRecoveryActions();
  bindMaterialPreviewFallbacks();
  if (focusKey !== lastCreativeFocusKey) {
    lastCreativeFocusKey = focusKey;
    workspace.querySelector("[data-stage-heading]")?.focus({ preventScroll: true });
  }
  return workspace.innerHTML;
}

function creativeSessionSwitcherMarkup(sessions, selectedId) {
  if (!sessions?.length) return "";
  return `<div class="creative-session-switcher"><label>创作会话<select data-creative-session-select>${sessions.map((session) =>
    `<option data-creative-session="${escapeHtml(session.id)}" value="${escapeHtml(session.id)}"${session.id === selectedId ? " selected" : ""}>${escapeHtml(session.title)} · ${escapeHtml(session.stage)}</option>`).join("")}</select></label><button type="button" class="quiet-action" data-new-creative-session>+新建创作</button></div>`;
}

function updateCreativeHeader(view) {
  if (view.stage === "draft_intent" && !view.id) {
    creativeSessionHeader.innerHTML = `<div class="session-title-row"><div><span class="session-eyebrow">新创作</span><strong>先说你想拍的内容</strong></div><span class="mode-chip">本地模拟</span></div><div class="session-progress" aria-label="创作进度"><i style="width:8%"></i></div>`;
    return;
  }
  const meta = creativeStageInfo(view.stage);
  creativeSessionHeader.innerHTML = `<div class="session-title-row"><div><span class="session-eyebrow">当前会话</span><strong>${escapeHtml(view.title ?? "未命名创作")}</strong></div><span class="mode-chip">${view.privacyMode === "paused" ? "隐私已暂停" : "隐私保护 · 模拟"}</span></div><div class="session-stage"><span>${meta.label}</span><b>${meta.progress}%</b></div><div class="session-progress" aria-label="创作进度"><i style="width:${meta.progress}%"></i></div>`;
}

function renderIntentComposer(view) {
  if (view?.id) {
    const labels = { platforms: "发布平台", audience: "给谁看", shootConditions: "今天可拍条件", prohibitedAreas: "禁拍内容", theme: "主题" };
    return `<section class="creative-card intent-card"><span class="section-kicker">CONTINUE</span><h3>继续补充这次创作</h3><p>还需要确认：${(view.questions ?? []).map((item) => labels[item] ?? item).join("、")}。回答后会继续使用同一个创作会话。</p><form data-intent-answer><label>补充答案<textarea name="text" required></textarea></label><button class="creative-primary" type="submit">继续整理</button><span class="inline-feedback" role="status" aria-live="polite"></span></form></section>`;
  }
  return `<section class="creative-card intent-card"><span class="section-kicker">START HERE</span><h3>今天想拍什么？</h3><p>说作品、过程或你想表达的观点。Agent 会整理成可确认的创作任务。</p><form class="intent-form"><label>拍摄想法<textarea name="text" required aria-label="今天的拍摄想法" placeholder="例如：拍一条绿松石戒指返工三次的视频号内容"></textarea></label><label>给这次创作起个名字<input name="title" required value="绿松石戒指返工故事" /></label><button class="creative-primary" type="submit">说出或写下拍摄想法</button><span class="inline-feedback" role="status" aria-live="polite"></span></form></section>`;
}

function renderMaterialConsent(view) {
  const count = view.materialSuggestion?.count ?? view.materialSuggestion?.suggestions?.length ?? 0;
  return `<section class="creative-card"><span class="step-label">第 2 步 · 历史素材</span><h3>要看看以前拍过的素材吗？</h3><p>素材库找到 ${count} 段可能有帮助的旧素材。现在只是询问是否查看，不会自动加入本次创作。</p><div class="consent-note"><b>你来决定</b><span>查看不等于使用，下一步还会逐段确认。</span></div><div class="creative-actions"><button type="button" class="quiet-action" data-material-decline>这次不使用</button><button type="button" class="creative-primary" data-material-accept>看看素材</button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function renderMaterialSelection(view) {
  const suggestions = view.suggestionSet?.suggestions ?? [];
  const selection = materialSelectionFor(view);
  return `<section class="creative-stack"><div class="creative-intro"><span class="step-label">第 3 步 · 选素材</span><h3>哪些能帮你讲清这个故事？</h3><p>只有你点“加入本次创作”的素材才会被脚本使用。</p></div>${suggestions.map((item) => materialSuggestionCard(item, selection)).join("") || '<div class="empty">没有可查看的历史素材。</div>'}<div class="creative-action-dock"><button type="button" class="creative-primary" data-confirm-materials>确认素材组合 <span data-selection-count>${selection.size}</span></button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function materialSuggestionCard(item, selection = materialSelectionFor(currentCreativeView)) {
  const selected = selection.has(item.segmentId);
  const capturedAt = new Date(item.capturedAt);
  const date = `${capturedAt.getFullYear()}年${capturedAt.getMonth() + 1}月${capturedAt.getDate()}日`;
  return `<article class="material-card ${selected ? "selected" : ""}" data-material-card="${escapeHtml(item.segmentId)}">${materialPreview(item)}<div class="material-body"><div class="material-topline"><span>${date}</span><span class="permission-chip ${escapeHtml(item.permissionState)}">${materialPermissionName(item.permissionState)}</span></div><h4>${escapeHtml(item.productId ?? "未绑定产品")} · ${escapeHtml(item.processStage ?? "待识别工序")}</h4><dl><div><dt>推荐理由</dt><dd>${escapeHtml(item.reason)}</dd></div><div><dt>来源</dt><dd>${escapeHtml(item.source)}</dd></div></dl><div class="material-actions"><button type="button" class="quiet-action" data-preview-material="${escapeHtml(item.segmentId)}">看看素材</button><button type="button" class="select-action" aria-pressed="${selected}" data-select-material="${escapeHtml(item.segmentId)}">${selected ? "已加入" : "加入本次创作"}</button></div></div></article>`;
}

function materialPreview(item) {
  const url = item.previewUrl ? escapeHtml(item.previewUrl) : "";
  let media = "";
  if (url && item.previewKind === "image") {
    media = `<img alt="${escapeHtml(item.productId ?? "历史素材")}预览" data-material-preview data-preview-url="${url}" />`;
  } else if (url && item.previewKind === "video") {
    media = `<video controls preload="metadata" aria-label="历史素材视频预览" data-material-preview data-preview-url="${url}"></video>`;
  } else if (url && item.previewKind === "audio") {
    media = `<audio controls preload="metadata" aria-label="历史素材音频预览" data-material-preview data-preview-url="${url}"></audio>`;
  }
  return `<div class="material-preview ${media ? "has-media" : ""}">${media}<div class="material-preview-fallback" ${media ? "hidden" : ""}><span>素材预览暂时无法加载</span><b>${escapeHtml(item.processStage ?? "待识别工序")}</b></div></div>`;
}

async function bindMaterialPreviewFallbacks() {
  const epoch = creativeRequestCoordinator.epoch;
  const controller = createRequestController();
  if (controller) creativeRequestCoordinator.previewControllers.add(controller);
  const signal = controller?.signal;
  try {
    await Promise.all([...workspace.querySelectorAll("[data-material-preview]")].map(async (media) => {
      media.addEventListener("error", () => {
        releaseMaterialPreviewUrl(media.src);
        media.hidden = true;
        const fallback = media.closest(".material-preview")?.querySelector(".material-preview-fallback");
        if (fallback) fallback.hidden = false;
      }, { once: true });
      const previewUrl = media.dataset.previewUrl;
      if (!previewUrl || !reviewSession?.reviewToken) return showMaterialPreviewFallback(media);
      try {
        const response = await fetch(previewUrl, {
          headers: { "x-review-token": reviewSession.reviewToken },
          signal,
        });
        if (!response.ok) throw new Error("material-preview-unavailable");
        if (signal?.aborted || epoch !== creativeRequestCoordinator.epoch || !media.isConnected) return;
        const blob = await response.blob();
        if (signal?.aborted || epoch !== creativeRequestCoordinator.epoch || !media.isConnected) return;
        const objectUrl = URL.createObjectURL(blob);
        creativeRequestCoordinator.previewObjectUrls.add(objectUrl);
        media.src = objectUrl;
      } catch (error) {
        if (signal?.aborted || epoch !== creativeRequestCoordinator.epoch || !media.isConnected) return;
        showMaterialPreviewFallback(media);
      }
    }));
  } finally {
    if (controller) creativeRequestCoordinator.previewControllers.delete(controller);
  }
}

function showMaterialPreviewFallback(media) {
  media.hidden = true;
  const fallback = media.closest?.(".material-preview")?.querySelector(".material-preview-fallback");
  if (fallback) fallback.hidden = false;
}

function releaseMaterialPreviewUrl(objectUrl) {
  if (!objectUrl || !creativeRequestCoordinator.previewObjectUrls.has(objectUrl)) return;
  URL.revokeObjectURL(objectUrl);
  creativeRequestCoordinator.previewObjectUrls.delete(objectUrl);
}

function revokeMaterialPreviewUrls() {
  creativeRequestCoordinator.previewObjectUrls.forEach((objectUrl) => URL.revokeObjectURL(objectUrl));
  creativeRequestCoordinator.previewObjectUrls.clear();
}

function abortMaterialPreviewRequests() {
  creativeRequestCoordinator.previewControllers.forEach((controller) => controller.abort());
  creativeRequestCoordinator.previewControllers.clear();
}

function resetMaterialPreviews() {
  abortMaterialPreviewRequests();
  revokeMaterialPreviewUrls();
}

function materialSelectionKey(view) {
  return view?.id && view?.suggestionSet?.id ? `${view.id}:${view.suggestionSet.id}` : null;
}

function materialSelectionFor(view) {
  const key = materialSelectionKey(view);
  if (materialSelectionState.key !== key) materialSelectionState = { key, ids: new Set() };
  return materialSelectionState.ids;
}

function materialSelectionView(view) {
  if (view?.stage === "reviewing_materials") return view;
  if (view?.stage === "editorial_review" && view.postShootMaterialDecision?.action === "accept-view" && view.postShootSuggestionSet) {
    return { ...view, suggestionSet: view.postShootSuggestionSet };
  }
  return null;
}

function clearMaterialSelection() {
  materialSelectionState = { key: null, ids: new Set() };
}

function renderScriptDraftAction(view) {
  return `<section class="creative-card"><span class="step-label">第 3 步 · 创作脚本</span><h3>已按你的决定准备好</h3><p>${view.stage === "materials_declined" ? "脚本只使用你刚才说的事实和今天将要新拍的画面。" : "已按你确认的素材组合准备脚本。"}</p><div class="creative-actions single"><button type="button" class="creative-primary" data-generate-script>生成本次脚本</button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function renderScriptReview(view) {
  if (!view.script || view.currentScriptVersion === 0) return renderScriptDraftAction(view);
  const groups = [
    ["customer-input", "客户事实", "只保留你明确说过的事实和观点"],
    ["historical-material", "历史素材", "只使用你确认加入的片段"],
    ["agent-suggestion", "Agent 建议", "表达与结构建议，不冒充客户事实"],
    ["shoot-suggestion", "可选拍摄建议", "有时间就拍，不是强制要求"],
  ];
  return `<section class="creative-stack"><div class="script-hero creative-card"><span class="step-label">第 4 步 · 脚本确认</span><span class="version-badge">V${view.script.scriptVersion}</span><h3>${escapeHtml(view.script.title)}</h3><p class="script-opening">${escapeHtml(view.script.opening)}</p></div>${scriptRevisionDiff(view)}<div class="script-source-grid">${groups.map(([source, label, helper]) => scriptSourceSection(view.script, source, label, helper)).join("")}</div><form class="script-revision-form"><label>想怎么改？<textarea name="prompt" required placeholder="例如：开头再直接一些，保留返工的细节"></textarea></label><button type="submit" class="quiet-action">继续修改</button><span class="inline-feedback" role="status" aria-live="polite"></span></form><div class="creative-action-dock script-decisions"><button type="button" class="quiet-action" data-save-script>暂存</button><button type="button" class="creative-primary" data-confirm-script>确认脚本，准备拍摄</button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function scriptRevisionDiff(view) {
  const history = [...(view.scriptHistory ?? [])].sort((a, b) => a.scriptVersion - b.scriptVersion);
  if (history.length < 2) return "";
  const previous = history.at(-2);
  const current = history.at(-1);
  const expressions = (script) => (script.sections ?? []).map((item) => item.expression).join("｜");
  return `<section class="creative-card script-diff" aria-label="脚本修改对比"><h4>V${previous.scriptVersion} → V${current.scriptVersion}</h4><p><b>设计师提示：</b>${escapeHtml(current.designerPrompt ?? "未记录")}</p><p><b>修改说明：</b>${escapeHtml(current.changeExplanation ?? "未记录")}</p><div class="diff-columns"><del>V${previous.scriptVersion}：${escapeHtml(previous.opening)}｜${escapeHtml(expressions(previous))}</del><ins>V${current.scriptVersion}：${escapeHtml(current.opening)}｜${escapeHtml(expressions(current))}</ins></div></section>`;
}

function scriptSourceSection(script, source, label, helper) {
  const sections = script.sections.filter((item) => item.source === source);
  return `<section class="script-source"><div><span>${escapeHtml(label)}</span><small>${escapeHtml(helper)}</small></div>${sections.length ? sections.map((item) => `<p>${escapeHtml(item.expression)}</p>`).join("") : '<p class="source-empty">本版暂无，确认时不会自动补写。</p>'}</section>`;
}

function renderDispatchConfirmation(view) {
  const planReady = view.currentShotPlanVersion > 0 && view.shotPlan;
  return `<section class="creative-stack"><div class="creative-card"><span class="step-label">第 5 步 · 同步前确认</span><h3>${planReady ? "最后检查，确认后再开拍" : "脚本已确认，先准备模拟设备"}</h3><p>脚本 V${view.currentScriptVersion} · ${view.shotPlan?.steps?.length ?? 0} 个拍摄步骤。A-0 只验证本地软件与模拟设备流程。</p></div><div class="device-list">${creativeDevices(view).map(deviceRow).join("")}</div><div class="safety-card"><b>开拍前请再看一眼</b><p>隐私模式：${view.privacyMode === "paused" ? "已暂停" : "创作者模式"}</p><p>禁拍提醒：客户脸部、订单信息和敏感区域</p></div><div class="creative-action-dock">${planReady ? '<button type="button" class="creative-primary" data-dispatch>确认下发并开始拍摄</button>' : '<button type="button" class="creative-primary" data-create-package>准备拍摄步骤与模拟设备</button>'}<span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function creativeDevices(view) {
  const defaults = [
    { deviceRole: "watch-director", deviceId: "watch_sim_01", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 82 } },
    { deviceRole: "main-stone-a", deviceId: "stone_a_sim_01", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 76 } },
    { deviceRole: "main-stone-b", deviceId: "stone_b_sim_01", status: "waiting-dispatch", battery: { label: "模拟电量", percent: 71 } },
  ];
  const byRole = new Map((view.devices ?? []).map((device) => [device.deviceRole, device]));
  return defaults.map((fallback) => ({ ...fallback, ...(byRole.get(fallback.deviceRole) ?? {}) }));
}

function deviceRow(device) {
  const copy = {
    "watch-director": ["手表（模拟）", "导演提示卡"],
    "main-stone-a": ["主石 A（模拟）", "近景录制任务"],
    "main-stone-b": ["主石 B（模拟）", "辅助角度录制"],
  }[device.deviceRole] ?? ["模拟设备", "拍摄任务"];
  const state = ({ received: "已接收拍摄任务", pending: "正在同步", failed: "同步失败，可重试", "privacy-paused": "隐私已暂停", "waiting-dispatch": "等待确认下发" })[device.status] ?? "等待确认下发";
  return `<article class="device-row"><span class="device-mark">◇</span><div><strong>${copy[0]}</strong><small>${copy[1]} · ${escapeHtml(device.battery?.label ?? "模拟电量")} ${device.battery?.percent ?? "--"}%</small></div><span class="device-state ${escapeHtml(device.status)}">${state}</span></article>`;
}

function renderShootingConsole(view) {
  const steps = view.shotPlan?.steps ?? [];
  const devices = creativeDevices(view);
  const allReceived = devices.every((device) => device.status === "received");
  const nextReturn = nextCreativeReturn(view);
  const unresolved = (view.stepResolutions ?? []).filter((item) =>
    ["pending", "retake-pending"].includes(item.state)).length;
  return `<section class="creative-stack"><div class="shooting-banner"><span class="live-dot"></span><div><b>现场拍摄中</b><small>${allReceived ? "三台模拟设备已接收拍摄任务" : "请查看各模拟设备的回执状态"}</small></div></div><div class="device-list compact">${devices.map(deviceRow).join("")}</div>${steps.length ? steps.map((step, index) => shootingStepCard(step, index, view)).join("") : '<div class="creative-card"><h3>拍摄任务已开始</h3><p>本页缺少当前步骤明细，请回到创作流程重新同步。</p></div>'}<div class="creative-card capture-handoff"><span class="step-label">手机素材回流</span><h3>${nextReturn ? `回传第 ${nextReturn.take} 次完成素材` : unresolved ? `还有 ${unresolved} 个镜头未结束` : "所有镜头已留下结果"}</h3><p>仅用于 A-0 本地模拟，不表示真实硬件已连接。</p>${nextReturn ? '<button type="button" class="creative-primary" data-capture-return>回传下一段素材</button>' : '<button type="button" class="creative-primary" data-end-shoot>结束并开始内容处理</button>'}<span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
}

function shootingStepCard(step, index, view) {
  const resolution = (view.stepResolutions ?? []).find((item) => item.stepId === step.id);
  const state = ({ pending: "待拍摄", "retake-pending": "等待新的完成 take", "completed-returned": `第 ${resolution?.acceptedTake} 次完成已回传`, skipped: "已跳过，不强制补拍" })[resolution?.state] ?? "待拍摄";
  return `<article class="shooting-card" data-step="${escapeHtml(step.id)}"><div class="shot-order"><span>导演卡 ${index + 1} · ${state}</span><b>${escapeHtml(step.purpose)}</b></div><p class="spoken-prompt">${escapeHtml(step.expressionSuggestion ?? "按脚本完成这一段")}</p><dl><div><dt>怎么拍</dt><dd>${escapeHtml(step.visualDirection ?? "按现场条件完成画面")}</dd></div><div><dt>建议时长</dt><dd>${step.durationSeconds ?? 0} 秒</dd></div><div><dt>必须拍到</dt><dd>${(step.mustCapture ?? []).map(escapeHtml).join("、") || "无"}</dd></div><div><dt>不能拍</dt><dd>${(step.prohibitedCapture ?? []).map(escapeHtml).join("、") || "敏感信息"}</dd></div></dl><div class="field-actions"><button type="button" class="complete-action" data-field-action="completed">完成</button><button type="button" data-field-action="retake">重拍</button><button type="button" data-field-action="skipped">跳过</button><button type="button" data-field-action="voice-rewrite">语音改写</button><button type="button" data-field-action="note">备注</button><button type="button" class="privacy-action" data-field-action="privacy-paused">隐私暂停</button></div><span class="inline-feedback" role="status" aria-live="polite"></span></article>`;
}

function renderIngestProgress(view) {
  return renderShootingConsole(view).replace("现场拍摄中", "素材回传中，可继续剩余步骤");
}
function renderEditorialHandoff(view) {
  const task = view.processingTask;
  const completedTakes = (view.stepResolutions ?? [])
    .filter((item) => item.state === "completed-returned")
    .map((item) => `第 ${item.acceptedTake} 次完成`).join("、");
  if (task?.status === "processing") {
    const phase = ({ segment: "正在切分素材", tag: "正在识别人物、产品与场景", story: "正在拆解故事", revision: "正在生成初剪", render: "正在生成四个视频版本" })[task.phase] ?? "正在整理素材";
    return `<section class="creative-card handoff-state processing"><span class="step-label">CONTENT ENGINE</span><h3>${phase}</h3><p>${completedTakes || "已接收本次有效素材"}，处理状态来自服务端持久化任务。</p><button type="button" class="creative-primary" data-refresh-creative>查看最新进度</button></section>`;
  }
  if (task?.status === "reviewable") return renderCreativeReview(view, completedTakes);
  if (task?.status === "no-assets") return `<section class="creative-card handoff-state"><h3>本次已结束，没有强制补拍</h3><p>所有镜头都由客户明确跳过，系统不会伪造可审片。</p><button type="button" class="quiet-action" data-new-creative-session>发起新创作</button></section>`;
  if (task?.status === "failed-retryable") return `<section class="creative-card handoff-state" role="alert"><h3>内容处理暂时中断</h3><p>${escapeHtml(task.errorCode ?? "本地处理服务暂时不可用")}</p><button type="button" class="creative-primary" data-retry-editorial>使用原操作编号安全重试</button><span class="inline-feedback" role="status" aria-live="polite"></span></section>`;
  if (task?.status === "failed-manual-review") return `<section class="creative-card handoff-state" role="alert"><h3>需要人工核对</h3><p>保留原始证据，不会自动重复提交。</p><button type="button" class="quiet-action" data-reset-manual-editorial>人工核对后重置</button></section>`;
  const suggestion = view.postShootMaterialSuggestion;
  const decision = view.postShootMaterialDecision;
  if (!suggestion) return `<section class="creative-card state-message"><h3>素材已齐，先确认是否补用库存</h3><p>${completedTakes || "已记录跳过结果"}。只有本次再次精确确认的历史素材才能进入剪辑。</p><button type="button" class="creative-primary" data-post-material-propose>检查拍后库存素材建议</button><span class="inline-feedback" role="status" aria-live="polite"></span></section>`;
  if (!decision) return `<section class="creative-card"><h3>是否查看拍后建议的库存素材？</h3><p>找到 ${suggestion.count} 段。这是本次成片的新授权，不沿用拍前决定。</p><div class="creative-actions"><button type="button" data-post-material-decline>不使用</button><button type="button" class="creative-primary" data-post-material-accept>看看素材</button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
  if (decision.action === "accept-view" && view.postShootSuggestionSet) {
    const selection = materialSelectionFor({ ...view, suggestionSet: view.postShootSuggestionSet });
    return `<section class="creative-stack"><div class="creative-card"><h3>为本次成片精确选择</h3><p>未选的库存素材不会进入故事或时间线。</p></div>${view.postShootSuggestionSet.suggestions.map((item) => materialSuggestionCard(item, selection)).join("")}<div class="creative-action-dock"><button type="button" class="creative-primary" data-post-material-confirm>确认本次成片素材</button><span class="inline-feedback" role="status" aria-live="polite"></span></div></section>`;
  }
  const decisionCopy = decision.action === "decline-view"
    ? "本次不使用历史素材，将只用本次新素材形成内容。"
    : `已精确选择 ${decision.selectedSegmentIds?.length ?? 0} 段历史素材，未选内容不会进入成片。`;
  return `<section class="creative-card state-message"><span class="step-label">授权已确认</span><h3>${decisionCopy}</h3><p>确认后开始剪辑，生成原视频、小红书、抖音、视频号四个版本。</p><button type="button" class="creative-primary" data-start-editorial>确认后开始剪辑</button><span class="inline-feedback" role="status" aria-live="polite"></span></section>`;
}

function renderCreativeReview(view, completedTakes) {
  const stories = view.stories ?? [];
  const revisions = view.revisions ?? [];
  const artifacts = view.artifacts ?? [];
  return `<section class="creative-stack creative-review-terminal"><div class="creative-card review-ready"><span class="step-label">READY TO REVIEW</span><h3>${stories.length} 条内容主线已拆好，可以去审片</h3><p>每条主线分别生成原视频、小红书、抖音、视频号四个版本。${completedTakes || "本次有效素材已处理"}；审批与改剪是两个独立动作，未批准不会对外。</p><button type="button" class="creative-primary" data-open-review>去审片</button></div>${stories.map((story) => {
    const revision = revisions.filter((item) => item.storyId === story.id)
      .sort((left, right) => right.revision - left.revision)[0];
    const videos = revision ? artifacts.filter((item) => item.revisionId === revision.id) : [];
    return `<article class="creative-card story-review" data-story-id="${escapeHtml(story.id)}"><div class="story-review-heading"><div><span class="step-label">${escapeHtml(storyTypeName(story.type))}</span><h3>剪辑 V${revision?.revision ?? "-"}</h3></div><span class="review-count">${videos.length}/4</span></div><div class="variant-video-grid">${videos.map((artifact) => `<section class="variant-video-card"><div><b>${escapeHtml(platformInfo(artifact.kind).name)}</b><span>本地可审版</span></div><video data-artifact-id="${escapeHtml(artifact.id)}" data-material-preview data-preview-url="${escapeHtml(artifact.mediaUrl)}" controls preload="metadata"></video><div class="material-preview-fallback" hidden>视频暂时无法加载</div>${creativeArtifactAction(view, artifact)}<span class="inline-feedback" role="status" aria-live="polite"></span></section>`).join("")}</div><form class="creative-revision-form" data-creative-revise-story="${escapeHtml(story.id)}"><label>给内容总编的改剪提示<textarea name="prompt" required placeholder="例如：开头缩短到3秒，产品完成镜头多留两秒"></textarea></label><button type="submit" class="quiet-action">生成新版本</button><span class="inline-feedback" role="status" aria-live="polite"></span></form></article>`;
  }).join("")}</section>`;
}

function creativeArtifactAction(view, artifact) {
  const approval = (view.approvals ?? []).find((item) => item.artifactId === artifact.id);
  if (!approval) return `<button type="button" class="quiet-action" data-approve-creative-artifact="${escapeHtml(artifact.id)}">批准这个精确版本</button>`;
  const value = (view.exportPackages ?? []).find((item) => item.approvalId === approval.id);
  if (!value) return `<button type="button" class="quiet-action" data-export-creative-approval="${escapeHtml(approval.id)}">生成手机发布包</button>`;
  const delivery = (view.deliveries ?? []).find((item) => item.packageId === value.id && item.status === "received");
  if (delivery) return '<button type="button" class="quiet-action" disabled>手机已接收 · 未发布</button>';
  return `<button type="button" class="quiet-action" data-deliver-creative-package="${escapeHtml(value.id)}">发送到手机</button>`;
}

function storyTypeName(type) {
  return ({ "persona-monologue": "人设 IP", "product-journey": "产品历程", "inner-journey": "心路历程" })[type] ?? "其他内容";
}

function nextCreativeReturn(view) {
  for (const step of view.shotPlan?.steps ?? []) {
    const resolution = (view.stepResolutions ?? []).find((item) => item.stepId === step.id);
    if (resolution?.state === "pending" && Number.isInteger(resolution.returnableTake)) {
      return { step, take: resolution.returnableTake };
    }
  }
  return null;
}
function renderCompletedSession() {
  return stateMessage("本次创作已完成", "素材、脚本、现场记录和审批都保留在同一会话中。");
}
function renderPausedSession() {
  return `<section class="creative-card state-message"><span class="state-symbol">◇</span><h3>创作已暂停</h3><p>隐私保护正在生效，模拟设备不会继续记录。</p><button type="button" class="creative-primary" data-privacy-resume>我已确认环境安全，继续拍摄</button><span class="inline-feedback" role="status" aria-live="polite"></span></section>`;
}
function renderClosedSession(view) {
  return stateMessage("这个创作任务已关闭", view.stage === "invalidated" ? "旧版本已失效，请使用最新创作会话。" : "可以返回创作首页建立新任务。");
}
function stateMessage(title, copy) {
  return `<section class="creative-card state-message"><span class="state-symbol">◇</span><h3>${title}</h3><p>${copy}</p></section>`;
}

function bindCreativeActions(view) {
  workspace.querySelector("[data-creative-session-select]")?.addEventListener("change", switchCreativeSessionFromUi);
  workspace.querySelector("[data-new-creative-session]")?.addEventListener("click", startNewCreativeSessionFromUi);
  workspace.querySelector(".intent-form")?.addEventListener("submit", submitCreativeIntent);
  workspace.querySelector("[data-intent-answer]")?.addEventListener("submit", answerCreativeIntentFromUi);
  workspace.querySelector("[data-material-accept]")?.addEventListener("click", (event) => decideMaterialViewingFromUi(event, "accept"));
  workspace.querySelector("[data-material-decline]")?.addEventListener("click", (event) => decideMaterialViewingFromUi(event, "decline"));
  workspace.querySelectorAll("[data-select-material]").forEach((button) =>
    button.addEventListener("click", () => toggleMaterialSelection(button.dataset.selectMaterial)));
  workspace.querySelectorAll("[data-preview-material]").forEach((button) =>
    button.addEventListener("click", () => showMaterialPreview(button)));
  workspace.querySelector("[data-confirm-materials]")?.addEventListener("click", confirmMaterialsFromUi);
  workspace.querySelector("[data-generate-script]")?.addEventListener("click", generateScriptFromUi);
  workspace.querySelector(".script-revision-form")?.addEventListener("submit", reviseScriptFromUi);
  workspace.querySelector("[data-save-script]")?.addEventListener("click", saveScriptDraftFromUi);
  workspace.querySelector("[data-confirm-script]")?.addEventListener("click", confirmScriptFromUi);
  workspace.querySelector("[data-create-package]")?.addEventListener("click", createShootingPackageFromUi);
  workspace.querySelector("[data-dispatch]")?.addEventListener("click", dispatchFromUi);
  workspace.querySelectorAll("[data-field-action]").forEach((button) =>
    button.addEventListener("click", () => recordFieldActionFromUi(button, view)));
  workspace.querySelector("[data-privacy-resume]")?.addEventListener("click", resumePrivacyFromUi);
  workspace.querySelector("[data-capture-return]")?.addEventListener("click", returnCaptureFromUi);
  workspace.querySelector("[data-end-shoot]")?.addEventListener("click", endShootFromUi);
  workspace.querySelector("[data-post-material-propose]")?.addEventListener("click", proposePostShootMaterialsFromUi);
  workspace.querySelector("[data-post-material-accept]")?.addEventListener("click", (event) => decidePostShootMaterialsFromUi(event, "accept"));
  workspace.querySelector("[data-post-material-decline]")?.addEventListener("click", (event) => decidePostShootMaterialsFromUi(event, "decline"));
  workspace.querySelector("[data-post-material-confirm]")?.addEventListener("click", confirmPostShootMaterialsFromUi);
  workspace.querySelector("[data-start-editorial]")?.addEventListener("click", startEditorialFromUi);
  workspace.querySelector("[data-retry-editorial]")?.addEventListener("click", retryEditorialFromUi);
  workspace.querySelector("[data-reset-manual-editorial]")?.addEventListener("click", resetManualEditorialFromUi);
  workspace.querySelector("[data-refresh-creative]")?.addEventListener("click", () => reconcileCreativeSession(view.id));
  workspace.querySelector("[data-open-review]")?.addEventListener("click", () =>
    workspace.querySelector(".story-review")?.scrollIntoView?.({ behavior: "smooth" }));
  workspace.querySelectorAll("[data-creative-revise-story]").forEach((form) =>
    form.addEventListener("submit", reviseCreativeStoryFromUi));
  workspace.querySelectorAll("[data-approve-creative-artifact]").forEach((button) =>
    button.addEventListener("click", approveCreativeArtifactFromUi));
  workspace.querySelectorAll("[data-export-creative-approval]").forEach((button) =>
    button.addEventListener("click", exportCreativeApprovalFromUi));
  workspace.querySelectorAll("[data-deliver-creative-package]").forEach((button) =>
    button.addEventListener("click", deliverCreativePackageFromUi));
}

function switchCreativeSessionFromUi(event) {
  selectedCreativeSessionId = event.currentTarget.value;
  return reconcileCreativeSession(selectedCreativeSessionId);
}

function startNewCreativeSessionFromUi() {
  selectedCreativeSessionId = null;
  currentCreativeView = { stage: "draft_intent", title: "新建创作" };
  renderCreativeSession(currentCreativeView);
}

async function answerCreativeIntentFromUi(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const text = form.elements.namedItem("text").value;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: form.querySelector(".inline-feedback"), busyText: "正在继续整理…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/intent-answers`,
    body: { text, answers: { response: text } } });
}

async function resumePrivacyFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".state-message").querySelector(".inline-feedback"),
    busyText: "正在恢复…", path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/privacy-resume`,
    body: { actorRole: "customer", confirmSafe: true } });
}

async function returnCaptureFromUi(event) {
  const button = event.currentTarget;
  const pendingReturn = nextCreativeReturn(currentCreativeView);
  const step = pendingReturn?.step;
  const device = creativeDevices(currentCreativeView).find((item) => item.deviceRole === "watch-director");
  if (!step || !device) return;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".capture-handoff").querySelector(".inline-feedback"),
    busyText: "正在回流…", path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/capture-handoff`,
    body: { mode: "a0-simulated-phone-return", stepId: step.id, deviceId: device.deviceId,
      deviceRole: "watch-director", carrier: "watch", take: pendingReturn.take, stepOutcome: "completed" } });
}

async function reviseCreativeStoryFromUi(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  return mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: form.querySelector(".inline-feedback"),
    busyText: "正在生成新版本…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/stories/${encodeURIComponent(form.dataset.creativeReviseStory)}/revisions`,
    body: { prompt: form.elements.namedItem("prompt").value },
  });
}

async function approveCreativeArtifactFromUi(event) {
  const button = event.currentTarget;
  return mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest?.(".variant-video-card")?.querySelector?.(".inline-feedback"),
    busyText: "正在批准…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/artifacts/${encodeURIComponent(button.dataset.approveCreativeArtifact)}/approve`,
    body: {},
  });
}

async function exportCreativeApprovalFromUi(event) {
  const button = event.currentTarget;
  return mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest?.(".variant-video-card")?.querySelector?.(".inline-feedback"),
    busyText: "正在生成发布包…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/approvals/${encodeURIComponent(button.dataset.exportCreativeApproval)}/export`,
    body: {},
  });
}

async function deliverCreativePackageFromUi(event) {
  const button = event.currentTarget;
  return mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest?.(".variant-video-card")?.querySelector?.(".inline-feedback"),
    busyText: "正在发送到手机…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/export-packages/${encodeURIComponent(button.dataset.deliverCreativePackage)}/deliver`,
    body: {},
  });
}

async function endShootFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".capture-handoff").querySelector(".inline-feedback"),
    busyText: "正在交接…", path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/end-handoff`,
    body: { mode: "a0-simulated-phone-return" } });
}

async function proposePostShootMaterialsFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".state-message").querySelector(".inline-feedback"), busyText: "正在查找…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/post-shoot-material-suggestions`, body: {} });
}

async function decidePostShootMaterialsFromUi(event, decision) {
  const button = event.currentTarget;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".creative-actions").querySelector(".inline-feedback"), busyText: "正在记录…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/post-shoot-material-viewing`,
    body: { suggestionSetId: currentCreativeView.postShootMaterialSuggestion.id, decision } });
}

async function confirmPostShootMaterialsFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({ sessionId: currentCreativeView.id, button,
    feedback: button.closest(".creative-action-dock").querySelector(".inline-feedback"), busyText: "正在确认…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/post-shoot-material-selection`,
    body: { suggestionSetId: currentCreativeView.postShootMaterialSuggestion.id,
      selectedSegmentIds: [...materialSelectionFor({ ...currentCreativeView, suggestionSet: currentCreativeView.postShootSuggestionSet })] } });
}

async function startEditorialFromUi(event, restart = null) {
  const button = event.currentTarget;
  return mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest?.(".state-message")?.querySelector?.(".inline-feedback"),
    busyText: "正在开始剪辑…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/editorial-process`,
    body: restart ? { manualRestartAuthorizationId: restart.authorizationId } : {},
    operationKey: restart?.processOperationKey,
  });
}

async function retryEditorialFromUi(event) {
  const operation = retryableEditorialOperation(currentCreativeView);
  if (!operation) {
    const feedback = event.currentTarget.closest?.(".handoff-state")?.querySelector?.(".inline-feedback");
    if (feedback) feedback.textContent = "未找到这次剪辑的完整操作记录，请人工恢复，不会自动发起新任务。";
    return null;
  }
  return retryPendingCreativeOperation(operation);
}

function retryableEditorialOperation(view) {
  const task = view?.processingTask;
  if (!task || task.status !== "failed-retryable" ||
    !validOpaqueIdentifier(task.operationKey) || !reviewSession?.clientInstanceId) return null;
  const path = `/api/creative-sessions/${encodeURIComponent(view.id)}/editorial-process`;
  const matches = [...creativePendingOperations.values()].filter((operation) => {
    if (operation.operationKey !== task.operationKey ||
      operation.clientInstanceId !== reviewSession.clientInstanceId ||
      operation.state !== "ambiguous" || operation.request?.method !== "POST" ||
      operation.request.path !== path) return false;
    const logicalBody = creativeLogicalBody(operation.request.path, operation.request.body);
    return operation.identity === canonicalCreativeValue({
      method: operation.request.method,
      path: operation.request.path,
      body: logicalBody,
    });
  });
  return matches.length === 1 ? matches[0] : null;
}

async function resetManualEditorialFromUi(event) {
  if (!window.confirm("已完成人工核对并确认可以重新开始剪辑吗？")) return false;
  const button = event.currentTarget;
  const task = currentCreativeView.processingTask;
  if (!task || task.status !== "failed-manual-review") return false;
  const processOperationKey = makeOperationId();
  const reset = await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest?.(".handoff-state")?.querySelector?.(".inline-feedback"),
    busyText: "正在记录人工确认…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/editorial-manual-reset`,
    body: {
      failedTaskId: task.id,
      confirmedBy: reviewSession.designerId,
      confirmationCode: "CONFIRM_MANUAL_EDITORIAL_RESTART",
      confirmationText: "人工核对完成，确认重新开始剪辑",
      confirmedAt: new Date().toISOString(),
      processOperationKey,
    },
  });
  if (!reset?.authorization?.id) return false;
  return startEditorialFromUi(event, {
    authorizationId: reset.authorization.id,
    processOperationKey,
  });
}

async function submitCreativeIntent(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const title = form.elements.namedItem("title").value;
  const text = form.elements.namedItem("text").value;
  await mutateCreativeSession({
    button,
    feedback: form.querySelector(".inline-feedback"),
    busyText: "正在整理创作意图…",
    path: "/api/creative-sessions",
    body: {
      customerId: "designer_01",
      title,
      text,
    },
  });
}

async function decideMaterialViewingFromUi(event, decision) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-actions").querySelector(".inline-feedback"),
    busyText: "正在记录你的决定…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/material-viewing`,
    body: { decision, suggestionSetId: currentCreativeView.materialSuggestion.id },
  });
}

function toggleMaterialSelection(segmentId) {
  const selectionView = materialSelectionView(currentCreativeView);
  if (!selectionView) return;
  const selection = materialSelectionFor(selectionView);
  if (selection.has(segmentId)) selection.delete(segmentId);
  else selection.add(segmentId);
  renderCreativeSession(currentCreativeView);
  workspace.querySelector(`[data-select-material="${cssEscape(segmentId)}"]`)?.focus();
}

function showMaterialPreview(button) {
  const card = button.closest(".material-card");
  const opened = card?.classList.toggle("preview-open");
  button.textContent = opened ? "收起素材" : "看看素材";
}

async function confirmMaterialsFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-action-dock").querySelector(".inline-feedback"),
    busyText: "正在确认素材…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/material-selection`,
    body: {
      suggestionSetId: currentCreativeView.suggestionSet.id,
      selectedSegmentIds: [...materialSelectionFor(currentCreativeView)],
    },
    clearSelection: true,
  });
}

async function generateScriptFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-actions").querySelector(".inline-feedback"),
    busyText: "正在生成脚本…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/scripts`,
    body: {},
    remember(response, draft) { draft.script = response.script; },
  });
}

function saveScriptDraftFromUi(event) {
  const button = event.currentTarget;
  const feedback = button.closest(".creative-action-dock").querySelector(".inline-feedback");
  feedback.textContent = `脚本 V${currentCreativeView.currentScriptVersion} 已在服务端暂存，尚未确认，可安全离开后再继续。`;
}

async function reviseScriptFromUi(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const prompt = form.elements.namedItem("prompt").value;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: form.querySelector(".inline-feedback"),
    busyText: "正在修改…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/script-revisions`,
    body: { prompt },
    remember(response, draft) { draft.script = response.script; },
  });
}

async function confirmScriptFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-action-dock").querySelector(".inline-feedback"),
    busyText: "正在确认脚本…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/script-confirmation`,
    body: { scriptVersion: currentCreativeView.currentScriptVersion },
    remember(response, draft) { draft.script = response.script; },
  });
}

async function createShootingPackageFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-action-dock").querySelector(".inline-feedback"),
    busyText: "正在准备模拟设备…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/shooting-package`,
    body: {},
    remember(response, draft) {
      draft.shotPlan = response.shotPlan;
      draft.directorCards = response.directorCards;
      draft.deviceBriefs = response.deviceBriefs;
    },
  });
}

async function dispatchFromUi(event) {
  const button = event.currentTarget;
  await mutateCreativeSession({
    sessionId: currentCreativeView.id,
    button,
    feedback: button.closest(".creative-action-dock").querySelector(".inline-feedback"),
    busyText: "正在确认模拟设备回执…",
    path: `/api/creative-sessions/${encodeURIComponent(currentCreativeView.id)}/dispatch`,
    body: { confirmStart: true },
    remember(response, draft) { draft.receipts = response.receipts; },
  });
}

async function recordFieldActionFromUi(button, view) {
  const action = button.dataset.fieldAction;
  const card = button.closest(".shooting-card");
  let transcript;
  let reason;
  if (action === "voice-rewrite") {
    transcript = window.prompt("请说出或输入改写要求");
    if (!transcript) return;
  }
  if (["note", "skipped", "retake"].includes(action)) {
    const prompts = { note: "请写下现场备注", skipped: "为什么跳过这一步？", retake: "这次为什么需要重拍？" };
    reason = window.prompt(prompts[action]);
    if (!reason) return;
  }
  await mutateCreativeSession({
    sessionId: view.id,
    button,
    feedback: card.querySelector(".inline-feedback"),
    busyText: "正在记录…",
    path: `/api/creative-sessions/${encodeURIComponent(view.id)}/events`,
    body: {
      action,
      stepId: card.dataset.step,
      briefVersion: view.currentBriefVersion,
      scriptVersion: view.currentScriptVersion,
      shotPlanVersion: view.currentShotPlanVersion,
      deviceId: creativeDevices(view).find((item) => item.deviceRole === "watch-director")?.deviceId,
      deviceRole: "watch-director",
      ...(transcript ? { transcript } : {}),
      ...(reason ? { reason } : {}),
    },
    remember(response, draft) {
      draft.events = [...(draft.events ?? []), response.event];
      if (action === "voice-rewrite") delete draft.script;
    },
  });
}

async function mutateCreativeSession({
  sessionId, button, group, feedback, busyText, path, body, clearSelection, operationKey,
}) {
  if (creativeRequestCoordinator.mutationInFlight) return null;
  creativeRequestCoordinator.mutationInFlight = true;
  creativeRequestCoordinator.pendingCreativeReload = false;
  advanceCreativeEpoch();
  const actionGroup = group ?? button.closest?.(".creative-actions, .creative-action-dock, .script-revision-form, .shooting-card, .intent-form");
  const controls = [...new Set([
    ...workspace.querySelectorAll("button, input, textarea, select"),
    ...(actionGroup?.querySelectorAll?.("button, input, textarea, select") ?? []),
  ])];
  if (!controls.includes(button)) controls.push(button);
  const controlStates = controls.map((item) => ({ item, disabled: item.disabled, text: item.textContent }));
  controls.forEach((item) => { item.disabled = true; });
  button.textContent = busyText;
  if (feedback) feedback.textContent = "";
  let reloadId = sessionId;
  let reconciled = false;
  let pendingOperation = null;
  let postCompleted = false;
  try {
    const logicalBody = creativeLogicalBody(path, body);
    pendingOperation = pendingCreativeOperation(path, logicalBody, operationKey);
    if (pendingOperation.state === "reconciled") {
      postCompleted = true;
      await acknowledgePendingCreativeOperation(pendingOperation);
      clearPendingCreativeOperation(pendingOperation);
      if (clearSelection) clearMaterialSelection();
      return null;
    }
    const controller = createRequestController();
    creativeRequestCoordinator.mutationController = controller;
    const response = await withCreativeRequestTimeout(() => api(path, {
      method: "POST",
      body: JSON.stringify(pendingOperation.request.body),
      headers: {
        "x-client-instance": pendingOperation.clientInstanceId,
        "x-operation-key": pendingOperation.operationKey,
      },
      signal: controller?.signal,
    }), controller);
    postCompleted = true;
    reloadId = sessionId ?? response.session?.id;
    if (reloadId) {
      const renderReconciliation = activeView === "creative";
      await reconcileCreativeSession(reloadId);
      await completeReconciledCreativeOperation(pendingOperation, reloadId);
      if (clearSelection) clearMaterialSelection();
      reconciled = renderReconciliation;
      creativeRequestCoordinator.pendingCreativeReload = !renderReconciliation;
    }
    return response;
  } catch (error) {
    if (isCreativeTimeout(error)) {
      if (feedback) feedback.textContent = "请求超时，已自动对账；可安全重试，不会重复执行";
      if (postCompleted && pendingOperation && reloadId) {
        try {
          const renderReconciliation = activeView === "creative";
          await reconcileCreativeSession(reloadId);
          await completeReconciledCreativeOperation(pendingOperation, reloadId);
          if (clearSelection) clearMaterialSelection();
          reconciled = renderReconciliation;
          creativeRequestCoordinator.pendingCreativeReload = !renderReconciliation;
        } catch {
          creativeRequestCoordinator.pendingCreativeReload = true;
        }
      } else if (activeView === "creative") {
        try {
          await reconcileCreativeState(reloadId);
          reconciled = true;
          creativeRequestCoordinator.pendingCreativeReload = false;
        } catch {
          creativeRequestCoordinator.pendingCreativeReload = true;
        }
      } else {
        creativeRequestCoordinator.pendingCreativeReload = true;
      }
    } else if (error?.name !== "AbortError") {
      if (!creativePendingStorageIssue && !postCompleted &&
        isDeterministicCreativeFailure(error) && pendingOperation) {
        try {
          if (reloadId) {
            const renderReconciliation = activeView === "creative";
            await reconcileCreativeSession(reloadId);
            reconciled = renderReconciliation;
            creativeRequestCoordinator.pendingCreativeReload = !renderReconciliation;
          } else {
            await reconcileCreativeList();
          }
          if (error.message === "creative-operation-key-conflict") {
            clearPendingCreativeOperation(pendingOperation);
          } else {
            await completeReconciledCreativeOperation(pendingOperation, reloadId);
          }
        } catch {
          creativeRequestCoordinator.pendingCreativeReload = true;
        }
      }
      if (feedback) feedback.textContent = `未完成：${friendlyCreativeError(error.message)}`;
    }
    return null;
  } finally {
    creativeRequestCoordinator.mutationController = null;
    controlStates.forEach(({ item, disabled, text }) => {
      item.disabled = disabled;
      item.textContent = text;
    });
    creativeRequestCoordinator.mutationInFlight = false;
    if (!reconciled && creativeRequestCoordinator.pendingCreativeReload && activeView === "creative") {
      try {
        await reconcileCreativeState(reloadId);
        creativeRequestCoordinator.pendingCreativeReload = false;
      } catch (error) {
        if (error?.name !== "AbortError") status.textContent = friendlyCreativeError(error.message);
      }
    }
  }
}

async function reconcileCreativeState(sessionId) {
  if (sessionId) return reconcileCreativeSession(sessionId);
  if (activeView !== "creative") return null;
  const epoch = advanceCreativeEpoch();
  syncViewChrome("creative");
  const controller = createRequestController();
  creativeRequestCoordinator.loadController = controller;
  try {
    await loadCreativeView({
      epoch,
      controller,
      signal: controller?.signal,
      requestedView: "creative",
    });
    return currentCreativeView;
  } finally {
    if (creativeRequestCoordinator.loadController === controller) {
      creativeRequestCoordinator.loadController = null;
    }
  }
}

async function reconcileCreativeSession(sessionId) {
  const render = activeView === "creative";
  const epoch = render ? advanceCreativeEpoch() : creativeRequestCoordinator.epoch;
  if (render) syncViewChrome("creative");
  const controller = createRequestController();
  creativeRequestCoordinator.detailController = controller;
  try {
    return await reloadCreativeSession(sessionId, {
      epoch,
      controller,
      signal: controller?.signal,
      render,
      isCurrent: () => !render ||
        (epoch === creativeRequestCoordinator.epoch && activeView === "creative"),
    });
  } finally {
    if (creativeRequestCoordinator.detailController === controller) {
      creativeRequestCoordinator.detailController = null;
    }
  }
}

async function reconcileCreativeList() {
  const controller = createRequestController();
  creativeRequestCoordinator.detailController = controller;
  try {
    return await withCreativeRequestTimeout(() => api("/api/creative-sessions", {
      signal: controller?.signal,
    }), controller);
  } finally {
    if (creativeRequestCoordinator.detailController === controller) {
      creativeRequestCoordinator.detailController = null;
    }
  }
}

async function completeReconciledCreativeOperation(operation, sessionId) {
  markPendingCreativeOperationReconciled(operation, sessionId);
  await acknowledgePendingCreativeOperation(operation);
  clearPendingCreativeOperation(operation);
}

function markPendingCreativeOperationReconciled(operation, sessionId) {
  const current = creativePendingOperations.get(operation.identity);
  if (current?.operationKey !== operation.operationKey) return;
  const next = new Map(creativePendingOperations);
  next.set(operation.identity, {
    ...current,
    state: "reconciled",
    sessionId: sessionId ?? null,
    receiptStatus: "completed",
  });
  replaceCreativePendingOperations(next);
}

async function acknowledgePendingCreativeOperation(operation) {
  const controller = createRequestController();
  creativeRequestCoordinator.detailController = controller;
  try {
    return await withCreativeRequestTimeout(() => api(
      `/api/creative-operation-receipts/${encodeURIComponent(operation.operationKey)}`,
      {
        method: "DELETE",
        body: "{}",
        headers: { "x-client-instance": operation.clientInstanceId },
        signal: controller?.signal,
      },
    ), controller);
  } finally {
    if (creativeRequestCoordinator.detailController === controller) {
      creativeRequestCoordinator.detailController = null;
    }
  }
}

async function retryReconciledCreativeAcknowledgements() {
  for (const operation of creativePendingOperations.values()) {
    if (operation.state !== "reconciled") continue;
    try {
      await acknowledgePendingCreativeOperation(operation);
      clearPendingCreativeOperation(operation);
    } catch {
      // Keep the reconciled key. Refresh or the original control can retry only this acknowledgement.
    }
  }
}

async function recoverPendingCreativeOperations() {
  if (creativePendingStorageIssue || creativePendingOperations.size === 0) return;
  const owner = pendingCreativeOwner();
  if (!owner) return failCreativePendingStorage("creative-pending-storage-invalid");
  if (reviewSession?.clientInstanceId && reviewSession.clientInstanceId !== owner) {
    return failCreativePendingStorage("creative-pending-client-mismatch");
  }
  for (const operation of [...creativePendingOperations.values()]) {
    if (creativePendingStorageIssue) return;
    if (operation.clientInstanceId !== owner) {
      failCreativePendingStorage("creative-pending-client-mismatch");
      return;
    }
    if (operation.state === "reconciled") {
      try {
        await acknowledgePendingCreativeOperation(operation);
        clearPendingCreativeOperation(operation);
      } catch {
        // A reconciled operation is kept until the owning client receives a definite ACK.
      }
      continue;
    }
    try {
      const response = await api(
        `/api/creative-operation-receipts/${encodeURIComponent(operation.operationKey)}`,
        { headers: { "x-client-instance": operation.clientInstanceId } },
      );
      const receipt = response.receipt;
      assertRecoveredReceiptIdentity(operation, receipt);
      if (receipt.status === "acknowledged") {
        clearPendingCreativeOperation(operation);
        continue;
      }
      if (receipt.status === "completed") {
        const sessionId = recoveredSessionId(operation, receipt.response?.body);
        if (sessionId) await reconcileCreativeSession(sessionId);
        else await reconcileCreativeList();
        await completeReconciledCreativeOperation(operation, sessionId);
        continue;
      }
      setPendingReceiptStatus(operation, receipt.status);
    } catch (error) {
      if (error?.status === 404) {
        setPendingReceiptStatus(operation, "missing");
        failCreativePendingStorage("creative-pending-receipt-missing");
      }
    }
  }
}

async function retryPendingCreativeOperation(operation) {
  if (!operation || creativePendingStorageIssue) return null;
  if (!reviewSession?.clientInstanceId ||
    reviewSession.clientInstanceId !== operation.clientInstanceId) {
    failCreativePendingStorage("creative-pending-client-mismatch");
    return null;
  }
  persistCreativePendingOperations(creativePendingOperations);
  try {
    const response = await api(operation.request.path, {
      method: operation.request.method,
      body: JSON.stringify(operation.request.body),
      headers: {
        "x-client-instance": operation.clientInstanceId,
        "x-operation-key": operation.operationKey,
      },
    });
    const sessionId = recoveredSessionId(operation, response);
    if (sessionId) await reconcileCreativeSession(sessionId);
    else await reconcileCreativeList();
    await completeReconciledCreativeOperation(operation, sessionId);
    return response;
  } catch (error) {
    if (Number.isInteger(error?.status) && error.status >= 500) {
      setPendingReceiptStatus(operation, "retryable");
    }
    return null;
  }
}

function assertRecoveredReceiptIdentity(operation, receipt) {
  const requestIdentity = receipt?.request
    ? canonicalCreativeValue({
        method: receipt.request.method,
        path: receipt.request.path,
        body: creativeLogicalBody(receipt.request.path, receipt.request.body),
      })
    : null;
  if (!receipt || receipt.clientInstanceId !== operation.clientInstanceId ||
    requestIdentity !== operation.identity) {
    failCreativePendingStorage("creative-pending-receipt-identity-mismatch");
    throw new Error("creative-pending-receipt-identity-mismatch");
  }
}

function recoveredSessionId(operation, responseBody) {
  if (operation.sessionId) return operation.sessionId;
  if (typeof responseBody?.session?.id === "string") return responseBody.session.id;
  const match = operation.request.path.match(/^\/api\/creative-sessions\/([^/]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function setPendingReceiptStatus(operation, receiptStatus) {
  const current = creativePendingOperations.get(operation.identity);
  if (current?.operationKey !== operation.operationKey) return;
  const next = new Map(creativePendingOperations);
  next.set(operation.identity, { ...current, receiptStatus });
  replaceCreativePendingOperations(next);
}

async function withCreativeRequestTimeout(request, controller, timeoutMs = creativeRequestTimeoutMs) {
  let timeoutHandle;
  const timeout = new Promise((_, reject) => {
    timeoutHandle = setTimeout(() => {
      controller?.abort();
      const error = new Error("creative-request-timeout");
      error.name = "TimeoutError";
      reject(error);
    }, timeoutMs);
  });
  try {
    return await Promise.race([Promise.resolve().then(request), timeout]);
  } finally {
    clearTimeout(timeoutHandle);
  }
}

function isCreativeTimeout(error) {
  return error?.name === "TimeoutError" || error?.message === "creative-request-timeout";
}

function createRequestController() {
  return typeof AbortController === "function" ? new AbortController() : null;
}

function creativeLogicalBody(path, body) {
  if (!path.endsWith("/events") || !Object.prototype.hasOwnProperty.call(body, "operationId")) {
    return body;
  }
  const { operationId: _operationId, ...logicalBody } = body;
  return logicalBody;
}

function pendingCreativeOperation(path, body, requestedOperationKey) {
  if (creativePendingStorageIssue) throw new Error(creativePendingStorageIssue);
  const clientInstanceId = reviewSession?.clientInstanceId;
  if (!validOpaqueIdentifier(clientInstanceId)) {
    throw new Error("creative-client-instance-required");
  }
  const identity = canonicalCreativeValue({ method: "POST", path, body });
  const existing = creativePendingOperations.get(identity);
  if (existing) {
    if (existing.clientInstanceId !== clientInstanceId) {
      throw new Error("creative-pending-client-mismatch");
    }
    if (requestedOperationKey && existing.operationKey !== requestedOperationKey) {
      throw new Error("creative-pending-operation-key-conflict");
    }
    return existing;
  }
  if (creativePendingOperations.size >= creativePendingMaxEntries) {
    throw new Error("creative-pending-operation-capacity");
  }
  const operationKey = requestedOperationKey ?? makeOperationId();
  if (!validOpaqueIdentifier(operationKey)) throw new Error("creative-operation-key-invalid");
  const operation = {
    identity,
    operationKey,
    clientInstanceId,
    createdAt: Date.now(),
    state: "ambiguous",
    sessionId: null,
    receiptStatus: "unknown",
    request: {
      method: "POST",
      path,
      body: path.endsWith("/events") ? { ...body, operationId: operationKey } : body,
    },
  };
  const next = new Map(creativePendingOperations);
  next.set(identity, operation);
  replaceCreativePendingOperations(next);
  return operation;
}

function clearPendingCreativeOperation(operation) {
  if (creativePendingOperations.get(operation.identity)?.operationKey !== operation.operationKey) return;
  const next = new Map(creativePendingOperations);
  next.delete(operation.identity);
  replaceCreativePendingOperations(next);
}

function restoreCreativePendingOperations() {
  const empty = () => ({ operations: new Map(), issue: null });
  try {
    if (typeof sessionStorage === "undefined") {
      return { operations: new Map(), issue: "creative-pending-storage-unavailable" };
    }
    const parsed = JSON.parse(sessionStorage.getItem(creativePendingStorageKey) ?? "[]");
    if (!Array.isArray(parsed) || parsed.length > creativePendingMaxEntries) {
      return { operations: new Map(), issue: "creative-pending-storage-invalid" };
    }
    const restored = new Map();
    const operationKeys = new Set();
    for (const entry of parsed) {
      if (!validPendingCreativeOperation(entry) || restored.has(entry.identity) ||
        operationKeys.has(entry.operationKey)) {
        return { operations: new Map(), issue: "creative-pending-storage-invalid" };
      }
      restored.set(entry.identity, entry);
      operationKeys.add(entry.operationKey);
    }
    return { operations: restored, issue: null };
  } catch {
    return { operations: new Map(), issue: "creative-pending-storage-read-failed" };
  }
  return empty();
}

function validPendingCreativeOperation(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry) ||
    typeof entry.identity !== "string" || !validOpaqueIdentifier(entry.operationKey) ||
    !validOpaqueIdentifier(entry.clientInstanceId) || !Number.isFinite(entry.createdAt) ||
    !["ambiguous", "reconciled"].includes(entry.state) ||
    !(entry.sessionId === null || typeof entry.sessionId === "string") ||
    !["unknown", "inflight", "retryable", "completed", "missing"].includes(
      entry.receiptStatus ?? "unknown",
    ) || !entry.request || entry.request.method !== "POST" ||
    typeof entry.request.path !== "string" ||
    !entry.request.path.startsWith("/api/creative-sessions") ||
    !entry.request.body || typeof entry.request.body !== "object" || Array.isArray(entry.request.body)) {
    return false;
  }
  if (entry.request.path.endsWith("/events") &&
    entry.request.body.operationId !== entry.operationKey) return false;
  const logicalBody = creativeLogicalBody(entry.request.path, entry.request.body);
  return entry.identity === canonicalCreativeValue({
    method: "POST",
    path: entry.request.path,
    body: logicalBody,
  });
}

function validOpaqueIdentifier(value) {
  return typeof value === "string" && value.length >= 8 && value.length <= 200 &&
    value.trim() === value && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value);
}

function pendingCreativeOwner() {
  const owners = new Set([...creativePendingOperations.values()].map(
    (operation) => operation.clientInstanceId,
  ));
  return owners.size === 1 ? [...owners][0] : null;
}

function persistCreativePendingOperations(operations = creativePendingOperations) {
  try {
    if (typeof sessionStorage === "undefined") {
      throw new Error("creative-pending-storage-unavailable");
    }
    sessionStorage.setItem(
      creativePendingStorageKey,
      JSON.stringify([...operations.values()]),
    );
  } catch {
    failCreativePendingStorage("creative-pending-storage-write-failed");
    throw new Error("creative-pending-storage-write-failed");
  }
}

function replaceCreativePendingOperations(next) {
  persistCreativePendingOperations(next);
  creativePendingOperations.clear();
  for (const [identity, operation] of next) {
    creativePendingOperations.set(identity, operation);
  }
}

function failCreativePendingStorage(code) {
  creativePendingStorageIssue = code;
  status.textContent = friendlyCreativeError(code);
}

function enterCreativeServerRestartRecovery() {
  if (creativePendingOperations.size > 0) {
    const ambiguous = new Map([...creativePendingOperations].map(([identity, operation]) => [
      identity,
      { ...operation, state: "ambiguous", receiptStatus: "unknown" },
    ]));
    replaceCreativePendingOperations(ambiguous);
  }
  reviewSession = null;
  failCreativePendingStorage("creative-client-recovery-required");
  renderCreativeOperationRecovery();
}

function enterCreativeOwnershipFailure(code) {
  reviewSession = null;
  failCreativePendingStorage(code);
  renderCreativeOperationRecovery();
}

function renderCreativeOperationRecovery() {
  workspace.innerHTML = creativeOperationRecoveryMarkup();
  bindCreativeRecoveryActions();
}

function resetCreativePendingOperationsFromUi() {
  if (!window.confirm("确认人工重置本机操作记录吗？这可能需要你手工核对服务端状态。")) {
    return false;
  }
  let pendingBackup;
  let ownerBackup;
  try {
    if (typeof sessionStorage === "undefined") throw new Error("unavailable");
    pendingBackup = sessionStorage.getItem(creativePendingStorageKey);
    ownerBackup = sessionStorage.getItem(creativeClientOwnerStorageKey);
    sessionStorage.removeItem(creativePendingStorageKey);
    sessionStorage.removeItem(creativeClientOwnerStorageKey);
    if (sessionStorage.getItem(creativePendingStorageKey) !== null ||
      sessionStorage.getItem(creativeClientOwnerStorageKey) !== null) {
      throw new Error("reset-incomplete");
    }
  } catch {
    try {
      if (typeof pendingBackup === "string") {
        sessionStorage.setItem(creativePendingStorageKey, pendingBackup);
      }
      if (typeof ownerBackup === "string") {
        sessionStorage.setItem(creativeClientOwnerStorageKey, ownerBackup);
      }
    } catch {
      // The storage failure remains visible and mutations stay blocked.
    }
    failCreativePendingStorage("creative-pending-storage-reset-failed");
    return false;
  }
  releaseCreativeTabLease();
  creativePendingOperations.clear();
  creativePendingStorageIssue = null;
  reviewSession = null;
  if (currentCreativeView) renderCreativeSession(currentCreativeView);
  return true;
}

function creativeOperationRecoveryMarkup() {
  if (creativePendingStorageIssue) {
    return `<section class="creative-card operation-recovery" role="alert"><h3>本机操作记录异常</h3><p>${escapeHtml(friendlyCreativeError(creativePendingStorageIssue))}</p><button type="button" class="quiet-action" data-reset-pending-operations>人工重置本机操作记录</button></section>`;
  }
  if (creativePendingOperations.size === 0) return "";
  const summaries = [...creativePendingOperations.values()].map((operation) => {
    const copy = {
      inflight: "仍在服务端处理",
      retryable: "上次服务失败，可使用同一操作键安全重试",
      missing: "服务端已无此回执，请人工核对后重置",
      completed: "操作已完成，正在等待对账",
      unknown: "待查询服务端回执",
    }[operation.receiptStatus ?? "unknown"];
    const retry = ["inflight", "retryable"].includes(operation.receiptStatus)
      ? `<button type="button" class="quiet-action" data-retry-pending-operation="${escapeHtml(operation.operationKey)}">使用同一操作键安全重试</button>`
      : "";
    return `<li><span>${escapeHtml(copy)}</span>${retry}</li>`;
  }).join("");
  return `<section class="creative-card operation-recovery"><h3>有 ${creativePendingOperations.size} 个待确认操作</h3><ul>${summaries}</ul><button type="button" class="quiet-action" data-reset-pending-operations>人工重置本机操作记录</button></section>`;
}

function bindCreativeRecoveryActions() {
  workspace.querySelector("[data-reset-pending-operations]")?.addEventListener(
    "click",
    resetCreativePendingOperationsFromUi,
  );
  workspace.querySelectorAll("[data-retry-pending-operation]").forEach((button) => {
    button.addEventListener("click", async () => {
      const operation = [...creativePendingOperations.values()].find(
        (item) => item.operationKey === button.dataset.retryPendingOperation,
      );
      button.disabled = true;
      await retryPendingCreativeOperation(operation);
      button.disabled = false;
      if (currentCreativeView) renderCreativeSession(currentCreativeView);
    });
  });
}

function canonicalCreativeValue(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalCreativeValue).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalCreativeValue(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function isDeterministicCreativeFailure(error) {
  return Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
}

function advanceCreativeEpoch() {
  creativeRequestCoordinator.epoch += 1;
  creativeRequestCoordinator.loadController?.abort();
  creativeRequestCoordinator.detailController?.abort();
  creativeRequestCoordinator.loadController = null;
  creativeRequestCoordinator.detailController = null;
  resetMaterialPreviews();
  return creativeRequestCoordinator.epoch;
}

function creativeStageInfo(stage) {
  return ({
    draft_intent: { label: "说清拍摄想法", progress: 8 },
    awaiting_material_consent: { label: "等待历史素材决定", progress: 22 },
    reviewing_materials: { label: "选择本次使用的素材", progress: 36 },
    materials_declined: { label: "准备创作脚本", progress: 43 },
    awaiting_script_confirmation: { label: "等待脚本确认", progress: 58 },
    ready_to_dispatch: { label: "同步前最后确认", progress: 72 },
    shooting: { label: "现场拍摄中", progress: 84 },
    ingesting: { label: "素材回传中", progress: 90 },
    editorial_review: { label: "等待审片", progress: 95 },
    completed: { label: "已完成", progress: 100 },
    paused: { label: "隐私暂停", progress: 84 },
    cancelled: { label: "已取消", progress: 0 },
    invalidated: { label: "已失效", progress: 0 },
  })[stage] ?? { label: "等待处理", progress: 0 };
}

function materialPermissionName(value) {
  return ({ allowed: "权限已确认", "review-required": "权限待确认", blocked: "不可使用", withdrawn: "授权已撤回" })[value] ?? value;
}
function friendlyCreativeError(value) {
  const known = {
    "creative-dependencies-required": "创作服务未启动",
    "creative-title-required": "请先给这次创作起个名字",
    "creative-text-required": "请先说明今天想拍什么",
    "creative-session-not-found": "这个创作任务已不存在，请返回创作首页",
    "material-view-acceptance-required": "请先确认是否查看历史素材",
    "material-suggestion-set-not-current": "素材建议已更新，请按最新列表重新选择",
    "material-selection-invalid": "所选素材中有一段已不可用，请重新选择",
    "confirmed-script-required": "请先确认当前脚本",
    "script-version-not-current": "脚本已更新，请先查看并确认最新脚本",
    "script-revision-prompt-required": "请先写下想修改的地方",
    "shooting-package-not-current": "拍摄步骤已更新，请重新准备模拟设备",
    "dispatch-confirmation-required": "请明确确认后再下发拍摄任务",
    "device-offline": "模拟设备暂时离线，可稍后重试",
    "privacy-paused": "当前处于隐私暂停，请确认安全后再继续",
    "request-failed": "网络暂时不稳定，请稍后重试",
    "creative-request-timeout": "请求超时，请重试或刷新查看最新状态",
    "creative-operation-key-conflict": "操作内容已经变化，请重新确认后再试",
    "creative-operation-registry-capacity": "当前操作较多，请稍后安全重试",
    "creative-pending-operation-capacity": "仍有 32 个待确认操作，请先重试原操作或刷新对账后再继续",
    "creative-client-instance-required": "尚未取得安全的客户端实例，请刷新后重试",
    "creative-client-instance-invalid": "客户端身份已失效，已停止自动操作",
    "creative-client-claim-required": "客户端租约信息不完整，已停止自动操作",
    "creative-client-claim-invalid": "客户端租约已变更，已停止自动操作",
    "creative-client-recovery-required": "服务已重启，待处理操作无法自动确认，请人工核对后重置",
    "creative-tab-owner-active": "同一客户端正在另一个标签页中使用，本页已停止自动操作",
    "creative-tab-lock-unavailable": "浏览器无法安全区分当前标签页，未发送任何操作",
    "creative-pending-client-mismatch": "本机待确认操作属于另一个客户端实例，不能读取或确认",
    "creative-pending-receipt-missing": "服务端已无此回执，请人工核对后重置。不能使用原操作键自动重试",
    "creative-pending-receipt-identity-mismatch": "服务端回执与本机请求身份不一致，已停止自动处理",
    "creative-pending-storage-invalid": "本机操作记录异常，请先人工核对并重置",
    "creative-pending-storage-read-failed": "本机操作记录异常，浏览器拒绝读取，未发送任何操作",
    "creative-pending-storage-unavailable": "本机操作记录异常，当前环境无法安全保存",
    "creative-pending-storage-write-failed": "无法安全保存本机操作记录，未发送任何操作",
    "creative-pending-storage-reset-failed": "无法重置本机操作记录，原记录已保留",
  };
  return known[value] ?? `操作未完成，请稍后重试（错误代码：${value || "unknown"}）`;
}
function makeOperationId() {
  return globalThis.crypto?.randomUUID?.() ?? `operation_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}
function cssEscape(value) {
  return globalThis.CSS?.escape ? globalThis.CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");
}

function renderPipeline(batch, review, approved) {
  const rendered = review.stories.reduce((sum, story) => {
    const latest = latestRevision(story);
    if (!latest) return sum;
    return sum + story.artifacts.filter((item) => item.status === "active" && item.revisionId === latest.id).length;
  }, 0);
  const packages = approved.approvals.flatMap((item) => item.packages ?? []);
  const delivered = packages.filter((item) => item.status === "delivered").length;
  const activeIndex = { batch: 0, library: 1, review: 2, approved: 4 }[activeView];
  const steps = [
    ["素材进入", batch.assets.length, "原始文件"],
    ["故事拆分", batch.stories.length, "条内容主线"],
    ["四版剪辑", rendered, "个版本已成片"],
    ["总编审批", approved.approvals.length, "个版本已通过"],
    ["手机接收", delivered, "个发布包已送达"],
  ];
  pipeline.innerHTML = steps.map((step, index) => `<article class="pipeline-step ${index === activeIndex ? "current" : ""}">
    <span class="step-dot">${index + 1}</span><strong>${step[1]}</strong><b>${step[0]}</b><small>${step[2]}</small>
  </article>`).join("");
}

function countPendingArtifacts(review, approved) {
  const approvedIds = new Set(approved.approvals.map((item) => item.artifactId));
  return review.stories.reduce((sum, story) => {
    const latest = latestRevision(story);
    if (!latest) return sum;
    return sum + story.artifacts.filter((item) => item.revisionId === latest.id && item.status === "active" && !approvedIds.has(item.id)).length;
  }, 0);
}

function scopeProductionData(batch, review, approved) {
  const storyIds = new Set(batch.stories.map((story) => story.id));
  return {
    review: { ...review, stories: review.stories.filter((story) => storyIds.has(story.id)) },
    approved: { ...approved, approvals: approved.approvals.filter((approval) => storyIds.has(approval.storyId)) },
  };
}

function renderReview(data) {
  if (!data.stories.length) {
    workspace.innerHTML = '<div class="empty">新素材还没有形成可生产的故事。</div>';
    return;
  }
  if (!selectedStoryId || !data.stories.some((story) => story.id === selectedStoryId)) {
    selectedStoryId = data.stories.find((story) => story.revisions.length)?.id ?? data.stories[0].id;
  }
  const selected = data.stories.find((story) => story.id === selectedStoryId);
  workspace.innerHTML = `<div class="production-layout">
    <aside class="story-rail"><div class="rail-label">本批内容主线 · ${data.stories.length}</div>${data.stories.map(storyChoice).join("")}</aside>
    <article class="story-detail">${storyDetail(selected)}</article>
  </div>`;
  bindReviewActions();
}

function storyChoice(story) {
  const latest = latestRevision(story);
  const info = storyInfo(story.type);
  return `<button class="story-choice ${story.id === selectedStoryId ? "active" : ""}" data-story-choice="${escapeHtml(story.id)}">
    <span class="story-icon">${info.icon}</span><span><b>${info.name}</b><small>${storySummary(story)}</small></span><em>${latest ? `V${latest.revision}` : "待剪"}</em>
  </button>`;
}

function storyDetail(story) {
  const latest = latestRevision(story);
  const info = storyInfo(story.type);
  if (!latest) return `<div class="story-brief"><div><span class="section-kicker">${info.english}</span><h3>${info.name}</h3><p>故事已经拆出，正在等待生成四个视频版本。</p></div><div class="story-status"><span class="version-badge">待剪辑</span></div></div>${technicalDetails(story)}`;
  if (!latest.variants.some((variant) => variant.kind === selectedVariantKind)) selectedVariantKind = latest.variants[0].kind;
  const revisionArtifacts = story.artifacts.filter((item) => item.revisionId === latest.id);
  const activeArtifacts = revisionArtifacts.filter((item) => item.status === "active");
  const selectedVariant = latest.variants.find((variant) => variant.kind === selectedVariantKind) ?? latest.variants[0];
  const selectedArtifact = revisionArtifacts.find((item) => item.kind === selectedVariant.kind);
  const isApproved = selectedArtifact?.status === "active" && currentApprovedData.approvals.some((item) => item.artifactId === selectedArtifact.id);
  return `<div class="story-brief"><div><span class="story-type">${info.icon} · ${info.name}</span><h3>${displayStoryTitle(latest, info.name)}</h3><p>${info.description}</p></div><div class="story-status"><span class="version-badge">V${latest.revision}</span><small>${activeArtifacts.length}/4 可审批</small></div></div>
    <div class="source-summary">
      <div class="source-group new"><strong>● 本批新素材 · ${story.lineage.new.length} 段</strong><p>决定这条内容讲什么，是故事的主干。</p></div>
      <div class="source-group history"><strong>● 历史素材补充 · ${story.lineage.history.length} 段</strong><p>${historyReasonSummary(story)}</p></div>
    </div>
    <div class="variant-tabs" role="tablist" aria-label="视频版本">${latest.variants.map((variant) => variantTab(variant)).join("")}</div>
    <div class="active-variant" role="tabpanel" id="variant-panel-${escapeHtml(selectedVariant.kind)}" aria-labelledby="variant-tab-${escapeHtml(selectedVariant.kind)}">${variantCard(selectedVariant, revisionArtifacts)}</div>
    ${reviewActionDock(story.id, selectedArtifact, isApproved)}
    ${promptPanel(story.id)}${technicalDetails(story)}`;
}

function variantTab(variant) {
  const platform = platformInfo(variant.kind);
  const selected = variant.kind === selectedVariantKind;
  return `<button type="button" role="tab" id="variant-tab-${escapeHtml(variant.kind)}" aria-controls="variant-panel-${escapeHtml(variant.kind)}" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}" class="variant-tab ${selected ? "active" : ""}" data-variant-tab="${escapeHtml(variant.kind)}"><span>${platform.icon}</span>${platform.name}</button>`;
}

function variantCard(variant, artifacts) {
  const artifact = artifacts.find((item) => item.kind === variant.kind);
  const platform = platformInfo(variant.kind);
  const isActive = artifact?.status === "active";
  const approved = isActive && currentApprovedData.approvals.some((item) => item.artifactId === artifact.id);
  const state = approved ? "已批准" : isActive ? "待审批" : artifact ? "已失效" : "生成中";
  const seconds = Math.round(variant.timeline.reduce((sum, item) => sum + item.outMs - item.inMs, 0) / 1000);
  return `<section class="variant">
    <div class="variant-top"><span class="platform-name"><i class="platform-icon">${platform.icon}</i>${platform.name}</span><span class="state-chip">${state}</span></div>
    <h4>${escapeHtml(variant.title)}</h4><p class="caption">${escapeHtml(variant.caption)}</p>
    ${artifact ? `<video controls preload="metadata" src="/api/artifacts/${encodeURIComponent(artifact.id)}/media"></video>` : '<div class="pending">等待生成视频</div>'}
    <div class="variant-meta"><span>约 ${seconds || 1} 秒</span><span>${variant.timeline.length} 个内容镜头</span></div>
  </section>`;
}

function reviewActionDock(storyId, artifact, approved) {
  const isActive = artifact?.status === "active";
  const approveAction = isActive && !approved
    ? `<button type="button" class="primary-action" data-approve="${escapeHtml(artifact.id)}">通过此版本</button>`
    : `<button type="button" class="primary-action" disabled>${approved ? "此版本已通过" : artifact ? "此版本已失效" : "正在生成"}</button>`;
  return `<div class="review-actions"><button type="button" class="secondary-action" data-open-editor="${escapeHtml(storyId)}">要求改剪</button>${approveAction}<span class="action-feedback" role="status" aria-live="polite"></span></div>`;
}

function promptPanel(storyId) {
  return `<section class="editor-panel" role="dialog" aria-modal="true" aria-hidden="true" aria-label="改剪指令"><div class="sheet-handle"></div><div class="editor-panel-head"><div><span class="section-kicker">NEW REVISION</span><h4>要求改剪</h4><p>说清楚怎么改，系统会生成新版本，当前版本仍会保留。</p></div><button type="button" class="sheet-close" data-close-editor aria-label="关闭改剪面板">×</button></div>
    <div class="prompt-shortcuts"><button type="button" data-prompt="把开头压缩到3秒内，尽快进入主题">缩短开头</button><button type="button" data-prompt="加强产品镜头，突出材质、工艺和完成效果">加强产品镜头</button><button type="button" data-prompt="多保留人物口播，让情绪和观点更完整">多保留人物口播</button><button type="button" data-prompt="视频号版本节奏更稳，结尾保留完整观点">优化视频号节奏</button></div>
    <form class="prompt-box" data-story="${escapeHtml(storyId)}"><textarea name="prompt" required aria-label="给总编下指令" placeholder="例如：视频号开头换成去年返工的口播，产品完成镜头多留两秒……"></textarea><button type="submit">生成新版本</button><span class="action-feedback" role="status" aria-live="polite"></span></form>
  </section>`;
}

function technicalDetails(story) {
  const revisions = [...story.revisions].sort((a, b) => b.revision - a.revision);
  return `<details class="technical-details"><summary>查看版本与素材技术记录</summary><p>故事编号：${escapeHtml(story.id)}</p><p>新素材：${story.lineage.new.map(escapeHtml).join(" · ")}</p><p>历史素材：${story.lineage.history.map(escapeHtml).join(" · ") || "无"}</p>${story.diffs.map((diff) => `<p>V${diff.revision}：${escapeHtml(diff.explanation ?? "按总编要求调整")}</p>`).join("")}${revisions.length ? "" : "<p>尚无剪辑版本</p>"}</details>`;
}

function bindReviewActions() {
  workspace.querySelectorAll("[data-story-choice]").forEach((button) => button.addEventListener("click", () => {
    selectedStoryId = button.dataset.storyChoice;
    selectedVariantKind = "editorial-master";
    renderReview(currentReviewData);
  }));
  const variantTabs = [...workspace.querySelectorAll("[data-variant-tab]")];
  variantTabs.forEach((button, index) => {
    button.addEventListener("click", () => selectVariant(button.dataset.variantTab, true));
    button.addEventListener("keydown", (event) => {
      const movement = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
      if (movement === undefined && !["Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? variantTabs.length - 1 : (index + movement + variantTabs.length) % variantTabs.length;
      selectVariant(variantTabs[nextIndex].dataset.variantTab, true);
    });
  });
  workspace.querySelectorAll("[data-open-editor]").forEach((button) => button.addEventListener("click", () => {
    const panel = workspace.querySelector(".editor-panel");
    editorTrigger = button;
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    panel.querySelector("textarea").focus();
  }));
  workspace.querySelectorAll("[data-close-editor]").forEach((button) => button.addEventListener("click", () => {
    closeEditor(workspace.querySelector(".editor-panel"));
  }));
  const editorPanel = workspace.querySelector(".editor-panel");
  if (editorPanel) editorPanel.addEventListener("keydown", handleEditorKeydown);
  workspace.querySelectorAll(".prompt-box").forEach((form) => form.addEventListener("submit", submitPrompt));
  workspace.querySelectorAll("[data-approve]").forEach((button) => button.addEventListener("click", approveArtifact));
  workspace.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
    const textarea = workspace.querySelector(".prompt-box textarea");
    textarea.value = button.dataset.prompt;
    textarea.focus();
  }));
}

function selectVariant(kind, restoreFocus = false) {
  selectedVariantKind = kind;
  renderReview(currentReviewData);
  if (restoreFocus) workspace.querySelector(`[data-variant-tab="${kind}"]`)?.focus();
}

function closeEditor(panel) {
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  editorTrigger?.focus();
}

function handleEditorKeydown(event) {
  const panel = event.currentTarget;
  if (event.key === "Escape") {
    event.preventDefault();
    closeEditor(panel);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...panel.querySelectorAll('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function renderBatch(data) {
  workspace.innerHTML = `<div class="metrics">${metric("原始文件", data.assets.length)}${metric("自然片段", data.segments.length)}${metric("内容主线", data.stories.length)}${metric("需要处理", data.failures.length)}</div>${cards(data.segments, segmentCard)}`;
}

function renderLibrary(data) {
  const reusable = data.segments.filter((item) => item.permissionState === "allowed");
  workspace.innerHTML = `<div class="metrics">${metric("可用素材", reusable.length)}${metric("人物口播", reusable.filter(hasPersona).length)}${metric("产品与工艺", reusable.filter(hasProduct).length)}${metric("待确认权限", data.segments.length - reusable.length)}</div>${cards(data.segments, segmentCard)}`;
}

function renderApproved(data) {
  workspace.innerHTML = data.approvals.length ? `<div class="metrics">${metric("已批准版本", data.approvals.length)}${metric("发布包", data.approvals.flatMap((item) => item.packages).length)}${metric("手机已接收", data.approvals.flatMap((item) => item.packages).filter((item) => item.status === "delivered").length)}</div>${cards(data.approvals, approvalCard)}` : '<div class="empty"><strong>还没有批准的内容</strong><p>请先在故事生产区选择一个平台版本进行审批。</p></div>';
}

function renderProfile() {
  workspace.innerHTML = `<section class="profile-card"><div class="profile-avatar">编</div><h3>内容生产总编</h3><p>负责故事选择、版本改剪与最终审批。</p><div class="profile-list"><div><span>生产模式</span><b>新素材为主 · 历史素材补充</b></div><div><span>设备方式</span><b>三端均为本地模拟</b></div><div><span>审批规则</span><b>逐版本确认 · 不自动发布</b></div><div><span>本地服务</span><b class="online">● 可用</b></div></div></section>`;
}

function segmentCard(segment, index) {
  const type = segment.tags.baseTypes.map(baseTypeName).join(" · ");
  return `<article class="card"><div class="card-head"><div><span class="badge">片段 ${String(index + 1).padStart(2, "0")}</span><h3>${escapeHtml(segment.tags.transcript || "无口播画面")}</h3></div><span class="badge ${segment.permissionState}">${permissionName(segment.permissionState)}</span></div><p>${escapeHtml(type)} · ${segment.tags.processStages.map(escapeHtml).join("、") || "待识别工序"}</p><p>${segment.tags.topics.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</p></article>`;
}

function approvalCard(approval) {
  const platform = platformInfo(approval.variant);
  const packageState = approval.packages[0]?.status;
  const story = currentReviewData?.stories.find((item) => item.id === approval.storyId);
  return `<article class="card"><span class="platform-name"><i class="platform-icon">${platform.icon}</i>${platform.name}</span><h3>${storyInfo(story?.type).name} · V${approval.revision}</h3><p>${packageState ? `发布包状态：${packageStateName(packageState)}` : "已经批准，等待生成手机发布包。"}</p><span class="badge allowed">设计师已批准</span></article>`;
}

async function submitPrompt(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const feedback = form.querySelector(".action-feedback");
  button.disabled = true;
  button.textContent = "正在生成…";
  feedback.textContent = "正在理解改剪要求";
  status.textContent = "总编正在生成三套新版本…";
  try {
    await api(`/api/stories/${encodeURIComponent(form.dataset.story)}/revisions`, { method: "POST", body: JSON.stringify({ prompt: form.prompt.value }) });
    await loadView();
  } catch (error) {
    status.textContent = error.message;
    feedback.textContent = `生成失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "生成新版本";
  }
}

async function approveArtifact(event) {
  if (!window.confirm("确认批准这个平台版本吗？批准只针对当前版本，不会连带批准其他平台。")) return;
  const button = event.currentTarget;
  const feedback = button.closest(".review-actions")?.querySelector(".action-feedback");
  button.disabled = true;
  button.textContent = "正在通过…";
  if (feedback) feedback.textContent = "正在记录本版本审批";
  status.textContent = "正在记录审批…";
  try {
    await api(`/api/artifacts/${encodeURIComponent(button.dataset.approve)}/approve`, { method: "POST", body: "{}" });
    await loadView();
  } catch (error) {
    status.textContent = error.message;
    if (feedback) feedback.textContent = `审批失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "通过此版本";
  }
}

async function api(path, options = {}) {
  if (path === "/api/session") return bootstrapCreativeSession();
  const method = String(options.method ?? "GET").toUpperCase();
  if (creativePendingStorageIssue && method === "POST" && isCreativeOwnerRoute(path)) {
    throw new Error(creativePendingStorageIssue);
  }
  if (!reviewSession) reviewSession = await bootstrapCreativeSession();
  const response = await fetch(path, { ...options, headers: {
    "content-type": "application/json",
    "x-review-token": reviewSession.reviewToken,
    "x-client-instance": reviewSession.clientInstanceId,
    "x-client-claim": reviewSession.clientClaim,
    ...(options.headers || {}),
  } });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "request-failed");
    error.status = response.status;
    error.recovery = data.recovery;
    if (isCreativeOwnerRoute(path) &&
      error.message === "creative-client-instance-invalid") {
      enterCreativeServerRestartRecovery();
      error.message = "creative-client-recovery-required";
    } else if (isCreativeOwnerRoute(path) &&
      error.message === "creative-client-claim-invalid") {
      enterCreativeOwnershipFailure("creative-client-claim-invalid");
    }
    throw error;
  }
  return data;
}

function bootstrapCreativeSession() {
  if (reviewSession) return Promise.resolve(reviewSession);
  const storedOwner = restoreCreativeClientOwner();
  const pendingOwner = pendingCreativeOwner();
  if (pendingOwner && !storedOwner) {
    enterCreativeServerRestartRecovery();
    return Promise.reject(new Error("creative-client-recovery-required"));
  }
  if (storedOwner && pendingOwner && storedOwner.clientInstanceId !== pendingOwner) {
    enterCreativeOwnershipFailure("creative-pending-client-mismatch");
    return Promise.reject(new Error("creative-pending-client-mismatch"));
  }
  const bootstrapKey = storedOwner
    ? `${storedOwner.clientInstanceId}\u0000${storedOwner.clientClaim}`
    : "new-client";
  if (creativeSessionBootstrap?.key === bootstrapKey) {
    return creativeSessionBootstrap.promise;
  }
  if (creativeSessionBootstrap) return creativeSessionBootstrap.promise;
  const promise = openCreativeSession(storedOwner);
  const bootstrap = { key: bootstrapKey, promise };
  creativeSessionBootstrap = bootstrap;
  const clear = () => {
    if (creativeSessionBootstrap === bootstrap) creativeSessionBootstrap = null;
  };
  promise.then(clear, clear);
  return promise;
}

async function openCreativeSession(storedOwner) {
  if (storedOwner) await claimCreativeTabLease(storedOwner.clientInstanceId);
  const response = await fetch("/api/session", {
    headers: {
      "content-type": "application/json",
      ...(storedOwner ? {
        "x-client-instance": storedOwner.clientInstanceId,
        "x-client-claim": storedOwner.clientClaim,
      } : {}),
    },
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "request-failed");
    error.status = response.status;
    error.recovery = data.recovery;
    if (error.message === "creative-client-recovery-required") {
      enterCreativeServerRestartRecovery();
    } else if ([
      "creative-client-claim-invalid",
      "creative-client-instance-invalid",
    ].includes(error.message)) {
      enterCreativeOwnershipFailure(error.message);
    }
    throw error;
  }
  if (!validOpaqueIdentifier(data.clientInstanceId) || !validOpaqueIdentifier(data.clientClaim)) {
    throw new Error("creative-client-instance-required");
  }
  if (storedOwner && data.clientInstanceId !== storedOwner.clientInstanceId) {
    enterCreativeOwnershipFailure("creative-pending-client-mismatch");
    throw new Error("creative-pending-client-mismatch");
  }
  await claimCreativeTabLease(data.clientInstanceId);
  persistCreativeClientOwner({
    clientInstanceId: data.clientInstanceId,
    clientClaim: data.clientClaim,
  });
  reviewSession = data;
  return data;
}

function isCreativeOwnerRoute(path) {
  return path.startsWith("/api/creative-sessions") ||
    path.startsWith("/api/creative-operation-receipts/");
}

function restoreCreativeClientOwner() {
  try {
    if (typeof sessionStorage === "undefined") throw new Error("unavailable");
    const raw = sessionStorage.getItem(creativeClientOwnerStorageKey);
    if (raw === null) return null;
    const owner = JSON.parse(raw);
    if (!owner || typeof owner !== "object" || Array.isArray(owner) ||
      !validOpaqueIdentifier(owner.clientInstanceId) ||
      !validOpaqueIdentifier(owner.clientClaim)) {
      throw new Error("invalid");
    }
    return {
      clientInstanceId: owner.clientInstanceId,
      clientClaim: owner.clientClaim,
    };
  } catch {
    failCreativePendingStorage("creative-pending-storage-read-failed");
    throw new Error("creative-pending-storage-read-failed");
  }
}

function persistCreativeClientOwner(owner) {
  try {
    if (typeof sessionStorage === "undefined") throw new Error("unavailable");
    sessionStorage.setItem(creativeClientOwnerStorageKey, JSON.stringify(owner));
  } catch {
    failCreativePendingStorage("creative-pending-storage-write-failed");
    throw new Error("creative-pending-storage-write-failed");
  }
}

async function claimCreativeTabLease(clientInstanceId) {
  if (creativeTabLease?.clientInstanceId === clientInstanceId) return;
  if (creativeTabLease) releaseCreativeTabLease();
  const locks = globalThis.navigator?.locks;
  if (!locks?.request) {
    failCreativePendingStorage("creative-tab-lock-unavailable");
    throw new Error("creative-tab-lock-unavailable");
  }
  let settleAcquisition;
  let releaseLease;
  const acquired = new Promise((resolve) => { settleAcquisition = resolve; });
  const held = new Promise((resolve) => { releaseLease = resolve; });
  Promise.resolve(locks.request(
    `wearable.creative.client-owner.${clientInstanceId}`,
    { mode: "exclusive", ifAvailable: true },
    async (lock) => {
      if (!lock) {
        settleAcquisition(false);
        return;
      }
      creativeTabLease = { clientInstanceId, release: releaseLease };
      settleAcquisition(true);
      await held;
    },
  )).catch(() => settleAcquisition(false));
  if (!await acquired) {
    failCreativePendingStorage("creative-tab-owner-active");
    throw new Error("creative-tab-owner-active");
  }
}

function releaseCreativeTabLease() {
  const lease = creativeTabLease;
  creativeTabLease = null;
  lease?.release();
}

globalThis.window?.addEventListener?.("pagehide", releaseCreativeTabLease);

function latestRevision(story) { return [...story.revisions].sort((a, b) => b.revision - a.revision)[0]; }
function displayStoryTitle(revision, fallback) { return revision.variants.find((item) => item.kind === "editorial-master")?.title || fallback; }
function storySummary(story) { return `${story.lineage.new.length} 段新素材${story.lineage.history.length ? ` + ${story.lineage.history.length} 段历史补充` : ""}`; }
function historyReasonSummary(story) { return story.lineage.history.length ? "补充同一人物、产品或此前工序，让故事更完整。" : "这条内容不需要调用历史库存。"; }
function storyInfo(type) { return ({ "product-journey": { name: "产品历程", icon: "品", english: "PRODUCT JOURNEY", description: "从选材、制作到完成，讲清楚这件产品为什么值得看。" }, "inner-journey": { name: "心路历程", icon: "心", english: "INNER JOURNEY", description: "保留犹豫、选择和感受，让内容产生真实的情绪连接。" }, "persona-monologue": { name: "人设口播", icon: "人", english: "PERSONA MONOLOGUE", description: "提炼设计师的观点与表达，持续建立可信的人设。" } })[type] ?? { name: "其他内容", icon: "辑", english: "EDITORIAL STORY", description: "从新素材中拆出的内容主线。" }; }
function platformInfo(kind) { return ({ "editorial-master": { name: "原视频", icon: "原" }, xiaohongshu: { name: "小红书", icon: "红" }, douyin: { name: "抖音", icon: "抖" }, "wechat-channels": { name: "视频号", icon: "视" } })[kind] ?? { name: kind, icon: "片" }; }
function baseTypeName(value) { return ({ "product-image": "产品画面", handmade: "制作过程", scene: "环境场景", "persona-copy": "人物口播", "customer-interaction": "客户互动" })[value] ?? value; }
function permissionName(value) { return ({ allowed: "可使用", "review-required": "待确认", blocked: "不可使用", withdrawn: "已撤回" })[value] ?? value; }
function packageStateName(value) { return ({ pending: "待发送", delivered: "手机已接收", "pending-retry": "等待重试", invalidated: "已失效" })[value] ?? value; }
function hasPersona(segment) { return segment.tags.baseTypes.includes("persona-copy"); }
function hasProduct(segment) { return segment.tags.baseTypes.some((type) => ["product-image", "handmade"].includes(type)); }
function cards(items, renderer) { return items.length ? `<div class="cards">${items.map(renderer).join("")}</div>` : '<div class="empty">暂无内容</div>'; }
function metric(label, value) { return `<div><strong>${value}</strong><span>${label}</span></div>`; }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]); }

document.querySelector('[data-view="creative"]').click();
