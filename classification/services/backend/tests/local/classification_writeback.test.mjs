import assert from 'node:assert/strict';
import test from 'node:test';

import {
  classificationInboxReceipt,
  classificationInboxReceiptForPackage,
  classificationWritebackAvailability,
  createClassificationWritebackController,
} from '../../src/product_engine/api/classification_writeback.mjs';

const context = {
  productCode: 'BCNHDIY914',
  packageVersion: 1,
  localBatchId: 'BATCH-20260920033509-8C21C203',
  items: [{ status: 'reviewed' }, { status: 'reviewed' }],
};

test('button remains unavailable until every product image is reviewed', () => {
  assert.deepEqual(
    classificationWritebackAvailability({
      ...context,
      items: [{ status: 'reviewed' }, { status: 'pending_review' }],
    }),
    { enabled: false, statusText: '待本机审核完成' },
  );
  assert.deepEqual(classificationWritebackAvailability(context), {
    enabled: true,
    statusText: '可提交分类仓',
  });
});

test('one click sends one narrow request and keeps its request id for retry', async () => {
  const calls = [];
  let attempt = 0;
  const controller = createClassificationWritebackController({
    requestIdFactory: () => '11111111-1111-4111-8111-111111111111',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      attempt += 1;
      if (attempt === 1) {
        return {
          ok: false,
          status: 409,
          json: async () => ({
            error_code: 'stale_material_package_version',
            message_zh: '素材版本已更新，请重新核对。',
          }),
        };
      }
      return {
        ok: true,
        status: 201,
        json: async () => ({
          classification_version: 1,
          classification_id: '66666666-6666-4666-8666-666666666666',
          disposition: 'created',
          downstream_status: 'not_sent',
        }),
      };
    },
  });

  await assert.rejects(controller.submit(context), error => {
    assert.equal(error.message_zh, '素材版本已更新，请重新核对。');
    return true;
  });
  const receipt = await controller.submit(context);

  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, calls[1].url);
  assert.equal(
    calls[0].url,
    '/local/api/formal-product-packages/BCNHDIY914/versions/1/classification-confirmations',
  );
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    request_id: '11111111-1111-4111-8111-111111111111',
    local_batch_id: context.localBatchId,
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), JSON.parse(calls[0].options.body));
  assert.equal(receipt.classification_version, 1);
});

test('created and duplicate receipts remain explicitly not sent downstream', async () => {
  for (const disposition of ['created', 'duplicate']) {
    const controller = createClassificationWritebackController({
      requestIdFactory: () => `request-${disposition}`,
      fetchImpl: async () => ({
        ok: true,
        status: disposition === 'created' ? 201 : 200,
        json: async () => ({
          classification_version: 3,
          classification_id: 'receipt-id',
          disposition,
          downstream_status: 'not_sent',
        }),
      }),
    });
    const receipt = await controller.submit(context);
    assert.equal(receipt.statusText, '分类版本 v3 已记账，尚未发送其他仓');
    assert.equal(receipt.receiptText, '回执 receipt-id');
    assert.doesNotMatch(receipt.statusText, /已发送销售仓|已签收|已生成风格图/);
  }
});

test('authoritative inbox row restores the persisted receipt after reload', () => {
  assert.deepEqual(classificationInboxReceipt({
    classification_source: 'human_confirmed',
    classification_version: 2,
    classification_id: '66666666-6666-4666-8666-666666666666',
  }), {
    sourceText: '人工确认 v2',
    statusText: '分类版本 v2 已记账，尚未发送其他仓',
    receiptText: '回执 66666666-6666-4666-8666-666666666666',
  });
  assert.equal(classificationInboxReceipt({ classification_source: 'album_candidate' }), null);
});

test('formal handoff restores only the classification receipt for the exact package', () => {
  const product = {
    product_code: 'BCNHDIY914',
    package_version: 1,
    classification_source: 'human_confirmed',
    classification_version: 2,
    classification_id: '11111111-1111-4111-8111-111111111111',
  };

  assert.deepEqual(
    classificationInboxReceiptForPackage(product, 'BCNHDIY914', 1),
    {
      sourceText: '人工确认 v2',
      statusText: '分类版本 v2 已记账，尚未发送其他仓',
      receiptText: '回执 11111111-1111-4111-8111-111111111111',
    },
  );
  assert.equal(classificationInboxReceiptForPackage(product, 'BCNHDIY914', 2), null);
  assert.equal(classificationInboxReceiptForPackage(product, 'BCNHDIY933', 1), null);
});
