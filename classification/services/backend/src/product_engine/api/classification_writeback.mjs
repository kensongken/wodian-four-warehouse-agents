export function classificationWritebackAvailability(context) {
  const items = context?.items || [];
  const enabled = Boolean(
    context?.productCode
      && Number.isInteger(context?.packageVersion)
      && context?.localBatchId
      && items.length > 0
      && items.every(item => item.status === 'reviewed'),
  );
  return {
    enabled,
    statusText: enabled ? '可提交分类仓' : '待本机审核完成',
  };
}

export function classificationInboxReceipt(product) {
  if (
    product?.classification_source !== 'human_confirmed'
      || !Number.isInteger(product?.classification_version)
      || !product?.classification_id
  ) return null;
  return {
    sourceText: `人工确认 v${product.classification_version}`,
    statusText: `分类版本 v${product.classification_version} 已记账，尚未发送其他仓`,
    receiptText: `回执 ${product.classification_id}`,
  };
}

export function classificationInboxReceiptForPackage(product, productCode, packageVersion) {
  if (
    product?.product_code !== productCode
      || product?.package_version !== packageVersion
  ) return null;
  return classificationInboxReceipt(product);
}

export function createClassificationWritebackController({ fetchImpl, requestIdFactory }) {
  const requestIds = new Map();

  function contextKey(context) {
    return `${context.localBatchId}\u0000${context.productCode}\u0000${context.packageVersion}`;
  }

  function requestId(context) {
    const key = contextKey(context);
    if (!requestIds.has(key)) requestIds.set(key, requestIdFactory());
    return requestIds.get(key);
  }

  async function submit(context) {
    const availability = classificationWritebackAvailability(context);
    if (!availability.enabled) {
      throw { error_code: 'product_review_incomplete', message_zh: availability.statusText };
    }
    const response = await fetchImpl(
      `/local/api/formal-product-packages/${encodeURIComponent(context.productCode)}`
        + `/versions/${context.packageVersion}/classification-confirmations`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: requestId(context),
          local_batch_id: context.localBatchId,
        }),
      },
    );
    const payload = await response.json();
    if (!response.ok) throw payload;
    return {
      ...payload,
      statusText: `分类版本 v${payload.classification_version} 已记账，尚未发送其他仓`,
      receiptText: `回执 ${payload.classification_id}`,
    };
  }

  return { submit };
}
