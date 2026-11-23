const PROMOTED_STATUS = '正式图片交接包已生成，尚未发送销售仓或风格仓';

export function formalHandoffAvailability(batch, receipt) {
  if (receipt?.state === 'promoting') {
    return { state: 'promoting', enabled: false, statusText: '正在生成正式交接包' };
  }
  if (receipt?.products?.length) {
    return { state: 'promoted', enabled: false, statusText: PROMOTED_STATUS };
  }
  const items = batch?.items || [];
  const successful = batch?.counts?.success || 0;
  const reviewed = batch?.counts?.reviewed || 0;
  const ready = items.length > 0
    && successful === items.length
    && reviewed === successful
    && items.every(item => item.status === 'reviewed');
  if (ready) {
    return { state: 'promotion_ready', enabled: true, statusText: '可生成正式交接包' };
  }
  return { state: 'review_pending', enabled: false, statusText: '待全部图片人工审核完成' };
}

function receiptView(receipt) {
  return { ...receipt, statusText: PROMOTED_STATUS };
}

export function createEmployeeFormalHandoffController({ fetchImpl, requestIdFactory }) {
  const requestIds = new Map();
  const inFlight = new Map();

  function requestId(batchId) {
    if (!requestIds.has(batchId)) requestIds.set(batchId, requestIdFactory());
    return requestIds.get(batchId);
  }

  async function loadLatestOrNull(batchId) {
    const response = await fetchImpl(
      `/local/api/upload-batches/${encodeURIComponent(batchId)}/formal-handoffs/latest`,
      { method: 'GET' },
    );
    const payload = await response.json();
    if (response.status === 404 && payload.error_code === 'employee_handoff_not_found') return null;
    if (!response.ok) throw payload;
    return receiptView(payload);
  }

  function submit(batch) {
    const batchId = batch?.batch_id;
    if (!batchId) return Promise.reject({ error_code: 'employee_batch_not_found' });
    if (inFlight.has(batchId)) return inFlight.get(batchId);

    const submission = (async () => {
      const response = await fetchImpl(
        `/local/api/upload-batches/${encodeURIComponent(batchId)}/formal-handoffs`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request_id: requestId(batchId) }),
        },
      );
      const payload = await response.json();
      if (!response.ok) throw payload;
      return receiptView(payload);
    })();
    inFlight.set(batchId, submission);
    submission.finally(() => inFlight.delete(batchId)).catch(() => {});
    return submission;
  }

  return { loadLatestOrNull, submit };
}
