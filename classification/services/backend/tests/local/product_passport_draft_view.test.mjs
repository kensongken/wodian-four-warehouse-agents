import assert from "node:assert/strict";

import { buildProductPassportDraftView } from "../../src/product_engine/api/product_passport_draft_view.mjs";

const blocker = (code, message) => ({
  code,
  message_zh: message,
  required_action_zh: `处理 ${message}`,
});

{
  const view = buildProductPassportDraftView({
    publication_status: "blocked",
    mall_import_allowed: false,
    sales_journey_usage: "preview_only",
    product_type: "redesign_output",
    inventory_status: "reserved",
    blockers: [
      blocker("main_image_missing", "主图缺失"),
      blocker("product_type_missing", "商品类型缺失"),
      blocker("specification_missing", "规格缺失"),
      blocker("quality_defect_review_missing", "品质瑕疵缺失"),
      blocker("price_range_missing", "价格缺失"),
      blocker("inventory_missing", "库存缺失"),
      blocker("service_rules_missing", "服务规则缺失"),
    ],
  });

  assert.equal(view.state, "blocked");
  assert.equal(view.statusLabel, "不可发布");
  assert.equal(view.totalCount, 7);
  assert.equal(view.editableCount, 4);
  assert.equal(view.integrationCount, 3);
  assert.deepEqual(
    view.items.map(item => [item.code, item.group, item.groupLabel]),
    [
      ["main_image_missing", "editable_here", "本页可补"],
      ["product_type_missing", "system_integration", "待系统接入"],
      ["specification_missing", "editable_here", "本页可补"],
      ["quality_defect_review_missing", "editable_here", "本页可补"],
      ["price_range_missing", "editable_here", "本页可补"],
      ["inventory_missing", "system_integration", "待系统接入"],
      ["service_rules_missing", "system_integration", "待系统接入"],
    ],
  );
  assert.equal(view.mallLabel, "禁止商城导入");
  assert.equal(view.salesLabel, "销售之旅仅预览");
  assert.equal(view.productTypeLabel, "改款成品");
  assert.equal(view.inventoryStatusLabel, "已预留");
}

{
  const view = buildProductPassportDraftView(null);

  assert.deepEqual(view, {
    state: "human_confirmation_required",
    statusLabel: "等待人工确认",
    totalCount: 0,
    editableCount: 0,
    integrationCount: 0,
    items: [],
    productTypeLabel: "待选择",
    inventoryStatusLabel: "待商家确认",
    mallLabel: "禁止商城导入",
    salesLabel: "尚不可交接",
  });
}

console.log("product passport draft view tests passed");
