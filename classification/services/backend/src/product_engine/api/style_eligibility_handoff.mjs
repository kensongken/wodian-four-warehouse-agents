const REJECTION_LABELS = {
  classification_material_unconfirmed: '材质未确认',
  classification_not_latest: '分类版本已过期',
  style_transform_authorization_required: '缺少转换授权',
  style_transform_authorization_expired: '转换授权已过期',
  style_transform_authorization_revoked: '转换授权已撤回',
  style_image_set_mismatch: '图片集不一致',
  style_permission_boundary_violation: '自动权限越界',
};

export function styleEligibilityView(product) {
  const status = product?.style_handoff_status;
  if (status === 'rejected') {
    const label = REJECTION_LABELS[product?.style_rejection_code]
      || product?.style_rejection_code
      || '原因未记录';
    const receipt = product?.style_decision_receipt_id;
    return {
      statusText: receipt
        ? `风格仓已拒绝 · ${label} · 回执 ${receipt}`
        : `风格仓已拒绝 · ${label} · 回执异常`,
      isError: true,
      canGenerate: false,
    };
  }
  if (status === 'accepted') {
    const receipt = product?.style_decision_receipt_id;
    return {
      statusText: receipt
        ? `风格仓资格已通过 · 回执 ${receipt}`
        : '风格仓回执异常，请停止使用',
      isError: !receipt,
      canGenerate: false,
    };
  }
  if (status === 'pending') {
    return { statusText: '正在等待风格仓决策', isError: false, canGenerate: false };
  }
  if (status === 'prepared') {
    return { statusText: '风格资格已准备，待发送校验', isError: false, canGenerate: false };
  }
  if (product?.classification_source === 'human_confirmed' && product?.classification_id) {
    return { statusText: '可准备风格资格校验', isError: false, canGenerate: false };
  }
  return { statusText: '需先完成人工分类确认', isError: false, canGenerate: false };
}

export function styleEligibilityAvailability(product) {
  const view = styleEligibilityView(product);
  const decided = ['accepted', 'rejected'].includes(product?.style_handoff_status);
  return {
    canPrepare: Boolean(
      product?.classification_source === 'human_confirmed'
        && product?.classification_id
        && !product?.style_handoff_id,
    ),
    canEvaluate: Boolean(
      product?.style_handoff_id && product?.style_handoff_status === 'prepared',
    ),
    canVerify: Boolean(decided && product?.style_decision_receipt_id),
    statusText: view.statusText,
  };
}

export function createStyleEligibilityController({ fetchImpl }) {
  async function request(url, method) {
    const response = await fetchImpl(url, { method });
    const payload = await response.json();
    if (!response.ok) throw payload;
    return payload;
  }

  return {
    prepare(classificationId) {
      return request(
        `/local/api/classification-versions/${encodeURIComponent(classificationId)}`
          + '/prepare-style-eligibility',
        'POST',
      );
    },
    evaluate(handoffId) {
      return request(
        `/local/api/style-warehouse-handoffs/${encodeURIComponent(handoffId)}/evaluate`,
        'POST',
      );
    },
    verify(handoffId) {
      return request(
        `/local/api/style-warehouse-handoffs/${encodeURIComponent(handoffId)}`,
        'GET',
      );
    },
  };
}
