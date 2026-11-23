const EDITABLE_HERE = new Set([
  "main_image_missing",
  "main_image_mismatch",
  "specification_missing",
  "quality_defect_review_missing",
  "price_range_missing",
]);

const PRODUCT_TYPE_LABELS = {
  first_sale_bead: "首售身份珠",
  legacy_material: "旧料",
  redesign_output: "改款成品",
};

const INVENTORY_STATUS_LABELS = {
  in_stock: "现货可售",
  reserved: "已预留",
  out_of_stock: "暂无库存",
};

export function buildProductPassportDraftView(draft) {
  if (!draft) {
    return {
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
    };
  }

  const items = (draft.blockers || []).map(blocker => {
    const editable = EDITABLE_HERE.has(blocker.code);
    return {
      ...blocker,
      group: editable ? "editable_here" : "system_integration",
      groupLabel: editable ? "本页可补" : "待系统接入",
    };
  });
  const editableCount = items.filter(item => item.group === "editable_here").length;

  return {
    state: draft.publication_status || "blocked",
    statusLabel: "不可发布",
    totalCount: items.length,
    editableCount,
    integrationCount: items.length - editableCount,
    items,
    productTypeLabel: PRODUCT_TYPE_LABELS[draft.product_type] || "待选择",
    inventoryStatusLabel:
      INVENTORY_STATUS_LABELS[draft.inventory_status] || "待商家确认",
    mallLabel: "禁止商城导入",
    salesLabel:
      draft.sales_journey_usage === "preview_only"
        ? "销售之旅仅预览"
        : "尚不可交接",
  };
}
