import assert from 'node:assert/strict';
import test from 'node:test';

import { salesDeliveryStatusText, salesJourneyReviewUrl } from '../../src/product_engine/api/sales_delivery_status.mjs';

test('shows delivered sales input without claiming it is sales ready', () => {
  assert.equal(
    salesDeliveryStatusText({
      sales_strategy_status: 'preview_only',
      sales_strategy_version: 3,
      sales_delivery_status: 'delivered',
    }),
    '销售之旅输入 v3 已送达 · 仅供预览，待人工确认',
  );
});

test('distinguishes queued, failed, and missing sales input', () => {
  assert.equal(
    salesDeliveryStatusText({
      sales_strategy_status: 'preview_only',
      sales_strategy_version: 2,
      sales_delivery_status: 'pending',
    }),
    '销售之旅输入 v2 已准备 · 正在送达',
  );
  assert.equal(
    salesDeliveryStatusText({
      sales_strategy_status: 'preview_only',
      sales_strategy_version: 2,
      sales_delivery_status: 'failed',
    }),
    '销售之旅输入 v2 送达失败 · 系统将保留记录',
  );
  assert.equal(salesDeliveryStatusText({}), '销售之旅输入尚未准备');
});

test('offers the sales review workbench only after durable delivery', () => {
  assert.equal(salesJourneyReviewUrl({
    sales_strategy_status: 'preview_only', sales_delivery_status: 'delivered',
  }), 'http://127.0.0.1:3001/product-strategy-approvals');
  assert.equal(salesJourneyReviewUrl({
    sales_strategy_status: 'preview_only', sales_delivery_status: 'pending',
  }), null);
});
