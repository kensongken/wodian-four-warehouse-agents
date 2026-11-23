const BASIS = new Set(['document_supported', 'merchant_declared', 'information_insufficient']);

export function evidenceState(input = {}) {
  return Object.freeze({
    claimBasis: BASIS.has(input.claimBasis) ? input.claimBasis : 'information_insufficient',
    materialLabel: String(input.materialLabel || '').trim(),
    acknowledged: input.acknowledged === true,
    fileCount: Number(input.fileCount || 0),
    inFlight: input.inFlight === true,
  });
}

export function validationMessage(value) {
  const state = evidenceState(value);
  if (state.inFlight) return '正在提交，请稍候。';
  if (state.claimBasis !== 'information_insufficient' && !state.materialLabel) {
    return '请填写材质名称。';
  }
  if (state.claimBasis === 'document_supported' && state.fileCount !== 1) {
    return state.fileCount > 1
      ? '凭证支持每次只能选择 1 个文件。'
      : '请选择 1 个凭证文件。';
  }
  if (state.claimBasis === 'merchant_declared' && !state.acknowledged) {
    return '请确认“商家声明不代表鉴定结论”。';
  }
  return '';
}

export function canSubmitEvidence(value) {
  return validationMessage(value) === '';
}

export function warehouseImpact(claimBasis) {
  if (claimBasis === 'document_supported') {
    return {
      sales: '销售预览将标注“凭证支持”。',
      style: '风格仓将继续校验授权和图片版本。',
    };
  }
  if (claimBasis === 'merchant_declared') {
    return {
      sales: '销售预览将标注“商家声明，非鉴定结论”。',
      style: '风格仓仍需独立授权，并会再次核验材料依据。',
    };
  }
  return {
    sales: '销售预览将保留“材质待人工确认”。',
    style: '风格仓将保持拦截，不会开始生图。',
  };
}

