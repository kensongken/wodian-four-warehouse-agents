export function salesDeliveryStatusText(product) {
  if (product.sales_strategy_status !== 'preview_only') return '销售之旅输入尚未准备';
  const prefix = `销售之旅输入 v${product.sales_strategy_version}`;
  if (product.sales_delivery_status === 'delivered') {
    return `${prefix} 已送达 · 仅供预览，待人工确认`;
  }
  if (product.sales_delivery_status === 'failed') {
    return `${prefix} 送达失败 · 系统将保留记录`;
  }
  if (product.sales_delivery_status === 'pending') {
    return `${prefix} 已准备 · 正在送达`;
  }
  return `${prefix} 已准备 · 等待送达`;
}

export function salesJourneyReviewUrl(product) {
  if (product.sales_strategy_status !== 'preview_only' || product.sales_delivery_status !== 'delivered') return null;
  return 'http://127.0.0.1:3001/product-strategy-approvals';
}
