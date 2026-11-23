const DECISIONS = new Set(['approved', 'not_my_style', 'product_detail_inaccurate']);
const SESSION_STATUSES = new Set(['awaiting_review', 'regenerating', 'approved', 'customer_approved', 'manual_attention_required']);
const outputLabels = {scene: '场景图', multi_product: '多产品混合图', real_photo: '实拍图'};
const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));
const segment = value => encodeURIComponent(String(value));
const statusName = value => value === 'approved' ? 'customer_approved' : value;
const errorMessage = error => error?.message_zh || '暂时没有收到结果，请重试；不会重复计次。';

function emptyState() {
  return {jobId: '', status: 'idle', jobStatus: '', outputType: '', regenerationCount: 0,
    candidates: [], sourceAssetIds: [], sourceProductIds: [], productAssociations: [],
    realVisible: new Set(), failedMedia: new Set(), busy: false, loading: false,
    error: '', pendingIntent: null, retryJobId: null, approvedCandidateId: null};
}

export function candidateReviewView(state) {
  const validCards = state.candidates.length === 3 && state.sourceAssetIds.length > 0;
  return {
    jobId: state.jobId, status: state.status, outputLabel: outputLabels[state.outputType] || '风格图',
    roundNumber: state.regenerationCount + 1, busy: state.busy, loading: state.loading,
    error: state.error, canRetry: Boolean(state.error && (state.pendingIntent || state.retryJobId)),
    canReview: validCards && state.status === 'awaiting_review'
      && state.jobStatus === 'awaiting_customer_review' && !state.loading
      && !state.failedMedia.size && !state.retryJobId && (!state.pendingIntent || state.busy),
    productAssociations: [...state.productAssociations], sourceProductIds: [...state.sourceProductIds],
    candidates: validCards ? state.candidates.map(candidate => ({
      candidateId: candidate.candidate_id, number: candidate.sequence_number,
      visibleKind: state.realVisible.has(candidate.candidate_id) ? 'real_photo' : 'style_image',
      styleUrl: `/local/api/style-jobs/${segment(state.jobId)}/candidates/${segment(candidate.candidate_id)}/image`,
      realUrls: state.sourceAssetIds.map(id => `/local/api/style-jobs/${segment(state.jobId)}/assets/${segment(id)}/image`),
      mediaFailed: state.failedMedia.has(candidate.candidate_id),
      adopted: state.approvedCandidateId === candidate.candidate_id,
    })) : [],
  };
}

/** Own only the displayed state; the consumer owns decisions, rounds and associations. */
export function createCandidateReviewController({fetchJob, review, onRender,
  newKey = () => globalThis.crypto.randomUUID(),
  wait = () => new Promise(resolve => setTimeout(resolve, 1000)),
}) {
  let state = emptyState();
  let revision = 0;
  const emit = () => onRender(candidateReviewView(state));

  async function readJob(jobId, version) {
    const followed = new Set();
    state.loading = true;
    state.error = '';
    state.retryJobId = null;
    emit();
    try {
      while (version === revision) {
        const payload = await fetchJob(jobId);
        if (version !== revision) return;
        if (payload?.ok === false) throw payload;
        if (payload?.job_id !== jobId || payload.publishable !== false
          || payload.synthetic_test_only !== true || !SESSION_STATUSES.has(payload.status)
          || !Number.isInteger(payload.regeneration_count) || payload.regeneration_count < 0
          || payload.regeneration_count > 3 || !Array.isArray(payload.candidates)
          || !Array.isArray(payload.source_asset_ids) || !Array.isArray(payload.source_product_ids)) {
          throw {message_zh: '任务结果无法确认，请重试读取。'};
        }
        if (payload.current_job_id !== jobId) {
          if (!payload.current_job_id || followed.has(payload.current_job_id) || followed.size >= 8) {
            throw {message_zh: '任务轮次无法确认，请重试读取。'};
          }
          followed.add(jobId);
          jobId = payload.current_job_id;
          continue;
        }
        const cards = payload.candidates;
        const approved = payload.status === 'approved';
        if (approved && (!cards.some(card => card.candidate_id === payload.approved_candidate_id)
          || !Array.isArray(payload.approved_associations) || !payload.approved_associations.length
          || payload.approved_associations.some(item => item.candidate_id !== payload.approved_candidate_id
            || !payload.source_asset_ids.includes(item.asset_id) || !payload.source_product_ids.includes(item.product_id)))) {
          throw {message_zh: '暂时无法读取已采用图片的关联，请重试读取。'};
        }
        const qualified = cards.length === 3
          && new Set(cards.map(card => card.candidate_id)).size === 3
          && cards.every(card => card.candidate_id && card.publishable === false
            && card.synthetic_test_only === true)
          && (['awaiting_customer_review', 'customer_approved'].includes(payload.job_status)
            || (payload.status === 'manual_attention_required' && payload.job_status === 'customer_rejected'));
        state = {...state, jobId, status: statusName(payload.status), jobStatus: payload.job_status,
          outputType: payload.output_type, regenerationCount: payload.regeneration_count,
          candidates: qualified ? cards : [], sourceAssetIds: payload.source_asset_ids,
          sourceProductIds: payload.source_product_ids, failedMedia: new Set(),
          approvedCandidateId: approved ? payload.approved_candidate_id : null,
          productAssociations: approved ? payload.approved_associations.map(item => ({
            product_id: item.product_id, approved_image: {candidate_id: item.candidate_id},
            real_photo: {asset_id: item.asset_id},
          })) : [],
          realVisible: state.jobId === jobId ? state.realVisible : new Set()};
        if (state.status !== 'regenerating' && !['queued', 'generating', 'machine_check'].includes(state.jobStatus)) break;
        emit();
        await wait();
      }
    } catch (error) {
      if (version !== revision) return;
      state.error = errorMessage(error);
      state.retryJobId = jobId;
    } finally {
      if (version === revision) {state.loading = false; emit();}
    }
  }

  async function submit(intent, version) {
    state.busy = true;
    state.error = '';
    emit();
    try {
      const result = await review(intent);
      if (version !== revision) return;
      if (result?.ok === false) throw result;
      if (result?.publishable !== false || result.synthetic_test_only !== true
        || !SESSION_STATUSES.has(result.status) || !Number.isInteger(result.regeneration_count)
        || result.regeneration_count < 0 || result.regeneration_count > 3) {
        throw {message_zh: '审核结果无法确认，请重试原操作。'};
      }
      state.pendingIntent = null;
      state.busy = false;
      // An acknowledgement describes the original click; another tab may have
      // already advanced the session. Restore the authoritative live job before
      // rendering adoption or retry controls.
      if (result.current_job_id) {
        await readJob(result.current_job_id, version);
        return;
      }
      state.status = statusName(result.status);
      state.regenerationCount = result.regeneration_count;
      state.productAssociations = result.product_associations || [];
      if (state.status === 'customer_approved') {
        state.approvedCandidateId = intent.candidateId;
        state.realVisible = new Set();
      }
      emit();
      if (result.next_job_id) await readJob(result.next_job_id, version);
      else if (state.status === 'regenerating') await readJob(intent.jobId, version);
    } catch (error) {
      if (version === revision) {
        state.error = errorMessage(error);
        // An explicit refusal is safe to refresh. An ambiguous response retains
        // the original write intent, so retry can recover its recorded result.
        if (error?.retryable === false) {
          state.pendingIntent = null;
          state.retryJobId = intent.jobId;
        }
      }
    } finally {
      if (version === revision) {state.busy = false; emit();}
    }
  }

  async function decide(candidateId, decision) {
    if (state.busy || state.pendingIntent || !candidateReviewView(state).canReview
      || !DECISIONS.has(decision) || !state.candidates.some(card => card.candidate_id === candidateId)) return;
    state.pendingIntent = {jobId: state.jobId, candidateId, decision, idempotencyKey: newKey()};
    await submit(state.pendingIntent, revision);
  }

  return {
    load(jobId) {
      const version = ++revision;
      state = emptyState();
      return readJob(jobId, version);
    },
    reset() {revision++; state = emptyState(); emit();},
    toggleReal(candidateId) {
      if (!state.candidates.some(card => card.candidate_id === candidateId)) return;
      if (state.realVisible.has(candidateId)) state.realVisible.delete(candidateId);
      else state.realVisible.add(candidateId);
      emit();
    },
    approve: candidateId => decide(candidateId, 'approved'),
    reject: (candidateId, decision) => decision !== 'approved' ? decide(candidateId, decision) : Promise.resolve(),
    retry() {
      if (state.busy || state.loading) return Promise.resolve();
      if (state.pendingIntent) return submit(state.pendingIntent, revision);
      if (state.retryJobId) return readJob(state.retryJobId, revision);
      return Promise.resolve();
    },
    mediaFailed(candidateId) {
      if (state.failedMedia.has(candidateId) || !state.candidates.some(card => card.candidate_id === candidateId)) return;
      state.failedMedia.add(candidateId);
      state.error = '图片无法安全读取，请重试查看后再采用。';
      state.retryJobId = state.jobId;
      emit();
    },
  };
}

/** Text is escaped; media URLs are built exclusively from same-origin owned identifiers. */
export function candidateReviewMarkup(view) {
  if (view.status === 'idle' && !view.loading && !view.error) return '';
  const status = view.busy ? '正在记录你的选择…' : view.loading ? '正在读取下一轮候选图…'
    : view.status === 'customer_approved' ? '已采用，实拍图与风格图已关联。'
    : view.status === 'manual_attention_required' ? (view.candidates.length
      ? '已转人工处理，保留本轮图片供核对。' : '本轮未生成可用图片，已转人工处理。')
    : view.candidates.length === 3 ? '请选择一张喜欢的图，也可以先查看实物。'
    : '候选图仍在检查，暂时不能采用。';
  const button = (action, label, candidateId = '', write = false) => `<button type="button" data-action="${action}" data-candidate="${escape(candidateId)}"${write && view.busy ? ' disabled' : ''}>${label}</button>`;
  return `<h3>${escape(view.outputLabel)} · 第 ${view.roundNumber} 轮 / 最多 4 轮</h3>
    <p class="bridge-meta">仅限脱敏本地测试 · 不可发布</p>
    <p class="bridge-meta">商品：${view.sourceProductIds.map(escape).join('、')}</p>
    <p role="status" aria-live="polite">${status}</p>
    ${view.error ? `<p role="alert">${escape(view.error)}</p>` : ''}
    <div class="candidate-grid">${view.candidates.map(card => `<article class="candidate-card" aria-label="候选 ${card.number}">
      <h4>候选 ${card.number}${card.adopted ? ' · 已采用' : ''}</h4>
      ${card.mediaFailed ? '<p>图片暂时无法显示</p>' : (card.visibleKind === 'real_photo' ? card.realUrls : [card.styleUrl]).map((url, index) => `<img src="${escape(url)}" data-candidate="${escape(card.candidateId)}" alt="候选 ${card.number}${card.visibleKind === 'real_photo' ? ` 对应的实拍图 ${index + 1}` : ' 风格图'}">`).join('')}
      <div class="candidate-actions">${button('toggle', card.visibleKind === 'real_photo' ? '查看风格图' : '查看实物图', card.candidateId)}
      ${view.canReview ? button('approved', '采用这张', card.candidateId, true) : ''}</div>
    </article>`).join('')}</div>
    ${view.canReview ? `<div class="candidate-actions"><span>这一轮都不满意：</span>${button('not_my_style', '不像我的风格', view.candidates[0].candidateId, true)}${button('product_detail_inaccurate', '商品细节不准确', view.candidates[0].candidateId, true)}</div>` : ''}
    ${view.productAssociations.length ? `<p>已关联 ${view.productAssociations.length} 件商品；默认展示风格图，查看实物时展示原图。</p>` : ''}
    ${view.canRetry ? button('retry', '重试刚才的操作', '', true) : ''}`;
}

export function mountCandidateReview(root, {fetchImpl = globalThis.fetch} = {}) {
  async function request(url, options) {
    const response = await fetchImpl(url, options);
    const payload = await response.json();
    if (!response.ok) throw payload;
    return payload;
  }
  const controller = createCandidateReviewController({
    fetchJob: id => request(`/local/api/style-jobs/${segment(id)}`),
    review: intent => request(`/local/api/style-jobs/${segment(intent.jobId)}/candidates/${segment(intent.candidateId)}/review`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({decision: intent.decision, reason: null, idempotency_key: intent.idempotencyKey}),
    }),
    onRender(view) {
      const focused = root.ownerDocument.activeElement;
      const action = root.contains(focused) ? focused?.dataset?.action : null;
      const candidate = focused?.dataset?.candidate;
      root.innerHTML = candidateReviewMarkup(view);
      if (action) [...root.querySelectorAll('button')].find(button => button.dataset.action === action
        && button.dataset.candidate === candidate)?.focus();
    },
  });
  root.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (!button || !root.contains(button) || button.disabled) return;
    const {action, candidate} = button.dataset;
    if (action === 'toggle') controller.toggleReal(candidate);
    else if (action === 'retry') void controller.retry();
    else if (action === 'approved') void controller.approve(candidate);
    else void controller.reject(candidate, action);
  });
  root.addEventListener('error', event => {
    if (event.target.tagName === 'IMG') controller.mediaFailed(event.target.dataset.candidate);
  }, true);
  return controller;
}
