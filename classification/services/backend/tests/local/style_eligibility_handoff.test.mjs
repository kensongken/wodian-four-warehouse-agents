import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createStyleEligibilityController,
  styleEligibilityAvailability,
  styleEligibilityView,
} from '../../src/product_engine/api/style_eligibility_handoff.mjs';

const classificationId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const handoffId = 'ffffffff-ffff-4fff-8fff-ffffffffffff';
const receiptId = '11111111-1111-4111-8111-111111111111';

const rejectedProduct = {
  classification_source: 'human_confirmed',
  classification_id: classificationId,
  style_handoff_id: handoffId,
  style_handoff_status: 'rejected',
  style_decision_receipt_id: receiptId,
  style_rejection_code: 'classification_material_unconfirmed',
};

test('material rejection remains complete and verifiable after reload', () => {
  assert.deepEqual(styleEligibilityAvailability(rejectedProduct), {
    canPrepare: false,
    canEvaluate: false,
    canVerify: true,
    statusText: `风格仓已拒绝 · 材质未确认 · 回执 ${receiptId}`,
  });
});

test('a rejected product never exposes generation', () => {
  assert.equal(styleEligibilityView(rejectedProduct).canGenerate, false);
});

test('only the three bounded actions are exposed', async () => {
  const requests = [];
  const controller = createStyleEligibilityController({
    fetchImpl: async (url, options = {}) => {
      requests.push([url, options.method]);
      return { ok: true, json: async () => ({ ok: true }) };
    },
  });

  await controller.prepare(classificationId);
  await controller.evaluate(handoffId);
  await controller.verify(handoffId);

  assert.deepEqual(requests, [
    [`/local/api/classification-versions/${classificationId}/prepare-style-eligibility`, 'POST'],
    [`/local/api/style-warehouse-handoffs/${handoffId}/evaluate`, 'POST'],
    [`/local/api/style-warehouse-handoffs/${handoffId}`, 'GET'],
  ]);
  assert.deepEqual(Object.keys(controller).sort(), ['evaluate', 'prepare', 'verify']);
});
