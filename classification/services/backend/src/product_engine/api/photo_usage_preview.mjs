export function visibleState({ selectedCount = 0, organizing = false, submitting = false }) {
  if (submitting) return 'generating';
  if (organizing) return 'organizing';
  if (selectedCount === 0) return 'awaiting_selection';
  return 'needs_confirmation';
}

const READINESS_MESSAGES = Object.freeze({
  product_confirmation_required: '请选择脱敏测试商品。',
  photo_classification_required: '请先处理无法打开或待确认的图片。',
  real_photo_required: '至少保留一张可用的实拍商品图。',
  main_photo_required: '请选择一张实拍商品图作为主图。',
  transform_permission_required: '请确认整批图片仅用于脱敏本地测试转换。',
  photo_conflict_present: '请先处理重复图片或商品归组冲突。',
  desensitized_test_boundary_required: '当前批次不符合脱敏本地测试边界。',
  style_service_unavailable: '本地风格测试服务尚未准备好。',
  style_readiness_unavailable: '暂时无法读取本批次状态，请稍后重试。',
});

const ERROR_MESSAGES = Object.freeze({
  position_conflict: '目标位置已有图片。',
  target_batch_not_found: '找不到目标批次，请重新选择。',
  photo_decode_failed: '图片文件无法打开，请重新上传可用图片。',
  style_handoff_not_found: '找不到这个本地风格图任务。',
  style_handoff_fingerprint_conflict: '确认事实已变化，请建立新的测试批次。',
  style_service_unavailable: '本地风格测试服务暂时不可用。',
});

export function bridgeErrorMessage(payload, fallback = '操作没有完成，请稍后重试。') {
  const supplied = payload?.message_zh;
  if (typeof supplied === 'string' && supplied.trim()) return supplied.trim();
  const code = payload?.error_code || payload?.error?.code;
  return ERROR_MESSAGES[code] || fallback;
}

export function readinessStatusMessage(
  missingCodes = [],
  { canConfirm = false, selectionComplete = false } = {},
) {
  if (canConfirm && selectionComplete) return '已准备好，请确认商品并生成风格图。';
  if (!Array.isArray(missingCodes) || missingCodes.length === 0) return '等待人工确认。';
  const messages = [];
  for (const code of missingCodes) {
    const message = READINESS_MESSAGES[code] || '仍有一项需要处理，请查看详情。';
    if (!messages.includes(message)) messages.push(message);
  }
  return `还不能生成：${messages.join('；')}`;
}

export function recoveryActionNames(view) {
  if (Array.isArray(view?.recovery_actions)) {
    const allowed = new Set(['reupload', 'delete', 'move']);
    return [...new Set(view.recovery_actions.filter(action => allowed.has(action)))];
  }
  return ['failed', 'duplicate'].includes(view?.state) ? ['reupload', 'delete', 'move'] : [];
}

export function eligibleMainPhotoItems(items = []) {
  return items
    .filter(view => (
      ['accepted', 'exception'].includes(view?.state)
      && (view.final_category || view.automatic_category) === 'real_photo'
    ))
    .sort((left, right) => (
      (left?.item?.position ?? Number.MAX_SAFE_INTEGER)
      - (right?.item?.position ?? Number.MAX_SAFE_INTEGER)
      || String(left?.item?.item_id || '').localeCompare(String(right?.item?.item_id || ''))
    ))
    .map((view, index) => ({ ...view, recommended: index === 0 }));
}

export function selectMainPhotoId(choices = [], selectedId = '') {
  if (choices.some(view => view?.item?.item_id === selectedId)) return selectedId;
  return choices[0]?.item?.item_id || '';
}

export function technicalDetails(payload) {
  const base = payload?.technical_details && typeof payload.technical_details === 'object'
    ? payload.technical_details
    : {};
  return {
    ...base,
    missing_codes: Array.isArray(payload?.missing_codes) ? payload.missing_codes : [],
    item_states: (payload?.items || []).map(view => ({
      item_id: view?.item?.item_id,
      state: view?.state,
      reason_code: view?.reason_code,
    })),
  };
}

export function selectionFingerprint({ productId = '', mainPhotoId = '', permission = false } = {}) {
  return JSON.stringify([String(productId), String(mainPhotoId), permission === true]);
}

export function newBatchState(batchId, generation = 1) {
  return {
    batchId,
    permission: false,
    submitting: false,
    generation,
    selectionRevision: 0,
    selectionFingerprint: selectionFingerprint(),
  };
}

export function statusState(currentStatus) {
  if (currentStatus === 'processing') return 'generating';
  if (currentStatus === 'needs_confirmation' || currentStatus === 'pending_real_model') return 'needs_confirmation';
  if (currentStatus === 'awaiting_review') return 'awaiting_selection';
  return 'needs_confirmation';
}

export function createTargetBatchDirectory(fetchTargets) {
  let revision = 0;
  let options = [];

  return {
    async refresh(currentBatchId, signal) {
      const requestRevision = ++revision;
      const payload = await fetchTargets(signal);
      if (requestRevision !== revision) return options.map(option => ({ ...option }));
      if (payload?.ok === false) throw payload;
      options = (Array.isArray(payload?.targets) ? payload.targets : [])
        .filter(target => target?.batch_id && target.batch_id !== currentBatchId)
        .map(target => ({
          batchId: target.batch_id,
          productId: target.product_id,
          label: target.display_label,
        }));
      return options.map(option => ({ ...option }));
    },
    snapshot: () => options.map(option => ({ ...option })),
  };
}

/** Keep asynchronous answers in the batch that requested them. */
export function createBatchBridgeController({ fetchReadiness, render }) {
  let controller = null;
  let state = newBatchState('', 0);
  let pollController = null;
  let selectionController = null;
  const actionControllers = new Set();

  function isCurrent(batchId, generation) {
    return state.batchId === batchId && state.generation === generation;
  }

  async function switchBatch(batchId) {
    controller?.abort();
    pollController?.abort();
    selectionController?.abort();
    selectionController = null;
    for (const actionController of actionControllers) actionController.abort();
    const nextController = new AbortController();
    controller = nextController;
    state = newBatchState(batchId, state.generation + 1);
    const generation = state.generation;
    render(null, visibleState({ selectedCount: 0 }));
    try {
      const payload = await fetchReadiness(batchId, nextController.signal);
      if (nextController.signal.aborted || state.batchId !== batchId || state.generation !== generation) return;
      render(payload, visibleState({ selectedCount: payload.batch?.items?.length || 0 }));
    } catch (_) {
      if (nextController.signal.aborted || state.batchId !== batchId || state.generation !== generation) return;
      render({ missing_codes: ['style_readiness_unavailable'] }, 'needs_confirmation');
    }
  }

  async function runBatchAction({
    batchId,
    request,
    refresh = false,
    onSuccess = () => {},
    onError = () => {},
    selectionSensitive = false,
  }) {
    const generation = state.generation;
    const selectionRevision = state.selectionRevision;
    if (!batchId || !isCurrent(batchId, generation)) return { status: 'stale' };
    const actionController = new AbortController();
    actionControllers.add(actionController);
    if (selectionSensitive) {
      selectionController?.abort();
      selectionController = actionController;
      state = { ...state, submitting: true };
    }
    const actionIsCurrent = () => (
      isCurrent(batchId, generation)
      && (!selectionSensitive || state.selectionRevision === selectionRevision)
    );
    try {
      let payload;
      try {
        payload = await request(actionController.signal);
      } catch (error) {
        if (actionController.signal.aborted || !actionIsCurrent()) {
          return { status: 'stale' };
        }
        onError(bridgeErrorMessage(error));
        return { status: 'error' };
      }
      if (actionController.signal.aborted || !actionIsCurrent()) {
        return { status: 'stale' };
      }
      if (payload?.ok === false) {
        onError(bridgeErrorMessage(payload));
        return { status: 'error' };
      }
      await onSuccess(payload);
      if (refresh && actionIsCurrent()) await switchBatch(batchId);
      return { status: 'success' };
    } finally {
      actionControllers.delete(actionController);
      if (selectionController === actionController) selectionController = null;
      if (selectionSensitive && actionIsCurrent()) state = { ...state, submitting: false };
    }
  }

  function updateSelection(selection) {
    const fingerprint = selectionFingerprint(selection);
    if (fingerprint === state.selectionFingerprint) return { ...state };
    selectionController?.abort();
    selectionController = null;
    pollController?.abort();
    state = {
      ...state,
      permission: selection?.permission === true,
      submitting: false,
      selectionRevision: state.selectionRevision + 1,
      selectionFingerprint: fingerprint,
    };
    return { ...state };
  }

  async function pollHandoff(
    handoffId,
    fetchHandoff,
    renderStatus,
    renderError = () => {},
    renderRetry = () => {},
  ) {
    pollController?.abort();
    const poll = new AbortController();
    pollController = poll;
    const batchId = state.batchId;
    const generation = state.generation;
    const selectionRevision = state.selectionRevision;
    const pollIsCurrent = () => (
      !poll.signal.aborted
      && state.batchId === batchId
      && state.generation === generation
      && state.selectionRevision === selectionRevision
    );
    let delay = 250;
    while (pollIsCurrent()) {
      let payload;
      try {
        payload = await fetchHandoff(handoffId, poll.signal);
      } catch (error) {
        if (!pollIsCurrent()) return { status: 'stale' };
        renderError(bridgeErrorMessage(error, '暂时无法读取风格图任务，请稍后重试。'));
        return { status: 'error' };
      }
      if (!pollIsCurrent()) return { status: 'stale' };
      if (payload?.ok === false) {
        renderError(bridgeErrorMessage(payload, '暂时无法读取风格图任务，请稍后重试。'));
        if (payload?.retryable === true) {
          const retry = () => {
            if (!pollIsCurrent()) return Promise.resolve({ status: 'stale' });
            return pollHandoff(
              handoffId,
              fetchHandoff,
              renderStatus,
              renderError,
              renderRetry,
            );
          };
          renderRetry(retry);
          return { status: 'retryable_error' };
        }
        return { status: 'error' };
      }
      const currentStatus = payload.handoff?.current_status || payload.current_status;
      renderStatus(payload, statusState(currentStatus));
      if (currentStatus !== 'processing') return { status: 'complete' };
      await new Promise(resolve => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 4000);
    }
    return { status: 'stale' };
  }

  return {
    switchBatch,
    refresh: () => state.batchId ? switchBatch(state.batchId) : Promise.resolve(),
    snapshot: () => ({ ...state }),
    updateSelection,
    visibleState: selectedCount => visibleState({ selectedCount, submitting: state.submitting }),
    runBatchAction,
    pollHandoff,
  };
}
