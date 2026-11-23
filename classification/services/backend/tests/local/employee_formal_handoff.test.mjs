import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createEmployeeFormalHandoffController,
  formalHandoffAvailability,
} from '../../src/product_engine/api/employee_formal_handoff.mjs';

const BATCH_ID = 'BATCH-20260921035620-3A284AA4';

function reviewedBatch() {
  return {
    batch_id: BATCH_ID,
    counts: { success: 12, reviewed: 12 },
    items: Array.from({ length: 12 }, (_, index) => ({
      status: 'reviewed',
      product_code: `PRODUCT-${Math.floor(index / 2) + 1}`,
    })),
  };
}

function createdReceipt() {
  return {
    batch_id: BATCH_ID,
    request_id: '11111111-1111-4111-8111-111111111111',
    content_sha256: 'a'.repeat(64),
    disposition: 'created',
    downstream_status: 'not_sent',
    products: Array.from({ length: 6 }, (_, index) => ({
      product_code: `PRODUCT-${index + 1}`,
      package_version: index + 1,
      content_sha256: String(index + 1).repeat(64),
    })),
  };
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

test('12 reviewed images become ready but never submit automatically', () => {
  assert.deepEqual(formalHandoffAvailability(reviewedBatch(), null), {
    state: 'promotion_ready',
    enabled: true,
    statusText: '可生成正式交接包',
  });
});

test('double click shares one in-flight request and one request id', async () => {
  const calls = [];
  const controller = createEmployeeFormalHandoffController({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return jsonResponse(201, createdReceipt());
    },
    requestIdFactory: () => '11111111-1111-4111-8111-111111111111',
  });

  const first = controller.submit(reviewedBatch());
  const second = controller.submit(reviewedBatch());

  assert.strictEqual(first, second);
  await first;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `/local/api/upload-batches/${BATCH_ID}/formal-handoffs`);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    request_id: '11111111-1111-4111-8111-111111111111',
  });
});

test('load latest restores six products and not-sent boundary without posting', async () => {
  const calls = [];
  const controller = createEmployeeFormalHandoffController({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return jsonResponse(200, createdReceipt());
    },
    requestIdFactory: () => 'unused',
  });

  const view = await controller.loadLatestOrNull(BATCH_ID);

  assert.equal(view.products.length, 6);
  assert.equal(view.statusText, '正式图片交接包已生成，尚未发送销售仓或风格仓');
  assert.deepEqual(calls, [{
    url: `/local/api/upload-batches/${BATCH_ID}/formal-handoffs/latest`,
    options: { method: 'GET' },
  }]);
});

test('missing latest receipt remains promotion-ready and retry reuses request id', async () => {
  const calls = [];
  let responseIndex = 0;
  const responses = [
    jsonResponse(404, { error_code: 'employee_handoff_not_found' }),
    jsonResponse(503, { error_code: 'employee_handoff_storage_unavailable' }),
    jsonResponse(201, createdReceipt()),
  ];
  const controller = createEmployeeFormalHandoffController({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return responses[responseIndex++];
    },
    requestIdFactory: () => '11111111-1111-4111-8111-111111111111',
  });

  assert.equal(await controller.loadLatestOrNull(BATCH_ID), null);
  await assert.rejects(controller.submit(reviewedBatch()));
  await controller.submit(reviewedBatch());

  assert.equal(JSON.parse(calls[1].options.body).request_id, JSON.parse(calls[2].options.body).request_id);
});

test('promoting and promoted states disable the explicit action', () => {
  assert.deepEqual(formalHandoffAvailability(reviewedBatch(), { state: 'promoting' }), {
    state: 'promoting',
    enabled: false,
    statusText: '正在生成正式交接包',
  });
  assert.deepEqual(formalHandoffAvailability(reviewedBatch(), createdReceipt()), {
    state: 'promoted',
    enabled: false,
    statusText: '正式图片交接包已生成，尚未发送销售仓或风格仓',
  });
});
