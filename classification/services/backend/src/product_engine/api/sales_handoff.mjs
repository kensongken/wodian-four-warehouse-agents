export function salesHandoffView(product) {
  const status = product?.sales_handoff_status;
  if (status === 'accepted') {
    if (!product?.sales_receiver_receipt_id) {
      return { statusText: '回执异常，请停止使用', isError: true };
    }
    return {
      statusText: `销售仓已签收 · 回执 ${product.sales_receiver_receipt_id}`,
      isError: false,
    };
  }
  if (status === 'rejected') {
    const code = product?.sales_handoff_rejection_code;
    return {
      statusText: code ? `销售仓拒收 · ${code}` : '销售仓拒收',
      isError: true,
    };
  }
  if (status === 'pending') {
    return { statusText: '正在等待销售仓回执', isError: false };
  }
  if (status === 'prepared') {
    return { statusText: '已准备，待发送销售仓', isError: false };
  }
  if (product?.classification_source === 'human_confirmed' && product?.classification_id) {
    return { statusText: '可准备销售预览', isError: false };
  }
  return { statusText: '需先完成人工分类确认', isError: false };
}

export function salesHandoffAvailability(product) {
  const view = salesHandoffView(product);
  const hasClassification = Boolean(
    product?.classification_source === 'human_confirmed' && product?.classification_id,
  );
  return {
    canPrepare: hasClassification && !product?.sales_handoff_id,
    canDeliver: Boolean(
      product?.sales_handoff_id && product?.sales_handoff_status === 'prepared',
    ),
    canVerify: Boolean(
      product?.sales_handoff_status === 'accepted'
        && product?.sales_receiver_receipt_id,
    ),
    statusText: view.statusText,
  };
}

const formLabels = {
  unknown: '未知',
  bangle: '手镯',
  bead: '单珠',
  bracelet: '手串',
  brooch: '胸针',
  earring: '耳饰',
  necklace: '项链',
  pendant: '吊坠',
  ring: '戒指',
  strand: '串珠',
  round_bead_bracelet: '方圆珠手串',
};

export function salesPreviewDisplay(preview) {
  const images = preview?.images || [];
  const mainCount = images.filter(image => image.role === 'main_candidate').length;
  const detailCount = images.length - mainCount;
  const form = formLabels[preview?.form?.value] || preview?.form?.value || '待确认';
  const observations = preview?.visual_observations?.length
    ? preview.visual_observations
    : ['待训练员确认图片可见特征'];
  const sellingPoints = preview?.selling_point_candidates?.length
    ? preview.selling_point_candidates
    : ['待训练员补充可用销售卖点'];
  return {
    summary: `商品编码 ${preview?.product_code || '待确认'} · ${images.length} 张可用图片（主图 ${mainCount}、附图 ${detailCount}）· 等待销售之旅验收`,
    facts: `材质：${preview?.material?.value || '待确认'} · 形态：${form} · 价格：${preview?.price_candidate ? '已人工确认' : '待录入'}`,
    observations,
    sellingPoints,
  };
}

export function createSalesHandoffController({ fetchImpl }) {
  async function request(url, options) {
    const response = await fetchImpl(url, options);
    const payload = await response.json();
    if (!response.ok) throw payload;
    return payload;
  }

  function post(url) {
    return request(url, { method: 'POST' });
  }

  return {
    prepare(classificationId) {
      return post(
        `/local/api/classification-versions/${encodeURIComponent(classificationId)}`
          + '/prepare-sales-preview',
      );
    },
    deliver(handoffId) {
      return post(
        `/local/api/warehouse-handoffs/${encodeURIComponent(handoffId)}/deliver`,
      );
    },
    read(handoffId) {
      return request(`/local/api/warehouse-handoffs/${encodeURIComponent(handoffId)}`);
    },
  };
}
