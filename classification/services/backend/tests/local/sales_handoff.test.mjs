import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createSalesHandoffController,
  salesPreviewDisplay,
  salesHandoffAvailability,
  salesHandoffView,
} from '../../src/product_engine/api/sales_handoff.mjs';

const classificationId = '66666666-6666-4666-8666-666666666666';
const handoffId = '77777777-7777-4777-8777-777777777777';
const receiptId = '88888888-8888-4888-8888-888888888888';

test('human-confirmed product without a handoff can only prepare', () => {
  assert.deepEqual(salesHandoffAvailability({
    classification_source: 'human_confirmed',
    classification_id: classificationId,
  }), {
    canPrepare: true,
    canDeliver: false,
    canVerify: false,
    statusText: '可准备销售预览',
  });
});

test('prepared handoff can only deliver', () => {
  assert.deepEqual(salesHandoffAvailability({
    classification_source: 'human_confirmed',
    classification_id: classificationId,
    sales_handoff_id: handoffId,
    sales_handoff_status: 'prepared',
  }), {
    canPrepare: false,
    canDeliver: true,
    canVerify: false,
    statusText: '已准备，待发送销售仓',
  });
});

test('accepted handoff can verify the original receipt without preparing again', () => {
  assert.deepEqual(salesHandoffAvailability({
    classification_source: 'human_confirmed',
    classification_id: classificationId,
    sales_handoff_id: handoffId,
    sales_handoff_status: 'accepted',
    sales_receiver_receipt_id: receiptId,
  }), {
    canPrepare: false,
    canDeliver: false,
    canVerify: true,
    statusText: `销售仓已签收 · 回执 ${receiptId}`,
  });
});

test('accepted status requires a durable receiver receipt', () => {
  assert.equal(
    salesHandoffView({ sales_handoff_status: 'accepted' }).statusText,
    '回执异常，请停止使用',
  );
  assert.deepEqual(salesHandoffView({
    sales_handoff_status: 'accepted',
    sales_receiver_receipt_id: receiptId,
  }), {
    statusText: `销售仓已签收 · 回执 ${receiptId}`,
    isError: false,
  });
});

test('controller calls narrow prepare, read, and deliver endpoints', async () => {
  const calls = [];
  const controller = createSalesHandoffController({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          handoff_id: handoffId,
          status: url.endsWith('/deliver') ? 'accepted' : 'prepared',
          receiver_receipt_id: url.endsWith('/deliver') ? receiptId : null,
        }),
      };
    },
  });

  await controller.prepare(classificationId);
  await controller.read(handoffId);
  await controller.deliver(handoffId);

  assert.deepEqual(calls.map(call => [call.url, call.options?.method || 'GET']), [
    [`/local/api/classification-versions/${classificationId}/prepare-sales-preview`, 'POST'],
    [`/local/api/warehouse-handoffs/${handoffId}`, 'GET'],
    [`/local/api/warehouse-handoffs/${handoffId}/deliver`, 'POST'],
  ]);
});

test('formats the authoritative sales preview instead of the original spreadsheet', () => {
  assert.deepEqual(salesPreviewDisplay({
    product_code: 'BCNHDIY914',
    images: [{ role: 'main_candidate' }, { role: 'gallery' }],
    material: { value: '信息不足' },
    form: { value: 'earring' },
    price_candidate: null,
    visual_observations: ['中性混合色调'],
    selling_point_candidates: [],
  }), {
    summary: '商品编码 BCNHDIY914 · 2 张可用图片（主图 1、附图 1）· 等待销售之旅验收',
    facts: '材质：信息不足 · 形态：耳饰 · 价格：待录入',
    observations: ['中性混合色调'],
    sellingPoints: ['待训练员补充可用销售卖点'],
  });
});

test('controller exposes safe backend errors', async () => {
  const controller = createSalesHandoffController({
    fetchImpl: async () => ({
      ok: false,
      json: async () => ({
        error_code: 'classification_not_latest',
        message_zh: '分类版本已更新，请重新核对。',
      }),
    }),
  });

  await assert.rejects(controller.prepare(classificationId), error => {
    assert.equal(error.error_code, 'classification_not_latest');
    assert.equal(error.message_zh, '分类版本已更新，请重新核对。');
    return true;
  });
});
