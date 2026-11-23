import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canSubmitEvidence,
  evidenceState,
  validationMessage,
  warehouseImpact,
} from '../../src/product_engine/api/material_evidence.mjs';

test('merchant declaration requires the warning acknowledgement', () => {
  const state = evidenceState({
    claimBasis: 'merchant_declared',
    materialLabel: '南红',
    acknowledged: false,
  });
  assert.equal(canSubmitEvidence(state), false);
  assert.equal(validationMessage(state), '请确认“商家声明不代表鉴定结论”。');
});

test('document support requires exactly one staged file', () => {
  assert.equal(canSubmitEvidence(evidenceState({
    claimBasis: 'document_supported', materialLabel: '绿松石', fileCount: 0,
  })), false);
  assert.equal(validationMessage(evidenceState({
    claimBasis: 'document_supported', materialLabel: '绿松石', fileCount: 2,
  })), '凭证支持每次只能选择 1 个文件。');
});

test('impact copy is deterministic for every basis', () => {
  assert.deepEqual(warehouseImpact('document_supported'), {
    sales: '销售预览将标注“凭证支持”。',
    style: '风格仓将继续校验授权和图片版本。',
  });
  assert.equal(
    warehouseImpact('information_insufficient').style,
    '风格仓将保持拦截，不会开始生图。',
  );
});

