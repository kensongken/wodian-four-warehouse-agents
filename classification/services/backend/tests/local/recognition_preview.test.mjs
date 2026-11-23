import assert from "node:assert/strict";

import {
  candidateToDraft,
  formToDraft,
  reviewItemStatusText,
} from "../../src/product_engine/api/recognition_preview.mjs";

const candidate = {
  template_code: "LOCAL-AI-FIRST",
  attempt: 1,
  status: "succeeded",
  material_candidates: [
    {
      label: "绿松",
      confidence_level: "medium",
      visible_evidence: "绿色调珠体，仅供训练员复核",
    },
  ],
  product_form_candidate: "圆珠饰品",
  visual_observations: ["绿色调珠体", "珠体排列成环"],
  selling_point_drafts: ["近景实拍展示珠体轮廓"],
  specification_candidates: ["珠径待测量"],
};

{
  const draft = candidateToDraft(candidate);

  assert.deepEqual(draft, {
    template_code: "LOCAL-AI-FIRST",
    main_image_template_code: "LOCAL-AI-FIRST",
    version: "AI候选 1",
    material_label: "绿松",
    product_form: "圆珠饰品",
    visual_observations: ["绿色调珠体", "珠体排列成环"],
    selling_points: ["近景实拍展示珠体轮廓"],
    specification_note: "珠径待测量",
    quality_note: "",
    defect_note: "",
    price_range: null,
    price_reason: "待录入该品类的价格标准",
    preview_source: "ai_candidate",
  });
}

{
  const original = candidateToDraft(candidate);
  const changed = formToDraft(original, {
    main_image_selected: false,
    material_label: "绿松",
    product_form: "手串",
    visual_observations: "绿色调珠体\n珠体排列成环",
    selling_points: "近景实拍展示珠体轮廓\n适合上手展示",
    specification_note: "珠径待测量",
    quality_note: "待人工确认",
    defect_note: "",
    price_minimum: "",
    price_maximum: "",
    price_basis: "",
  });

  assert.equal(changed.product_form, "手串");
  assert.equal(changed.main_image_template_code, null);
  assert.deepEqual(changed.selling_points, [
    "近景实拍展示珠体轮廓",
    "适合上手展示",
  ]);
  assert.equal(changed.preview_source, "draft_unsaved");
  assert.equal(changed.version, "未保存草稿");

  const restored = candidateToDraft(candidate);
  assert.equal(restored.product_form, "圆珠饰品");
  assert.equal(restored.preview_source, "ai_candidate");
}

{
  const draft = formToDraft(candidateToDraft(candidate), {
    main_image_selected: true,
    material_label: "绿松",
    product_form: "手串",
    visual_observations: "绿色调珠体",
    selling_points: "近景实拍展示珠体轮廓",
    specification_note: "",
    quality_note: "",
    defect_note: "",
    suggested_price: "241",
  });

  assert.deepEqual(draft.price_range, {
    currency: "CNY",
    minimum: "241",
    maximum: "241",
    basis: "训练员输入的内部建议价",
  });
}

console.log("recognition preview tests passed");

{
  assert.equal(reviewItemStatusText({ status: "pending_review" }), "待审核");
  assert.equal(reviewItemStatusText({ status: "reviewed" }), "已审核");
  assert.equal(reviewItemStatusText({ status: "failed" }), "不可审核");
}
