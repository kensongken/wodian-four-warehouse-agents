import assert from "node:assert/strict";

import {
  buildCandidateDetailView,
  buildRecognitionStatusView,
} from "../../src/product_engine/api/recognition_status_view.mjs";

{
  const view = buildRecognitionStatusView({
    status: "fallback_exact_match",
    error_code: "vision_not_configured",
    attempt: 1,
    material_candidates: [{ label: "绿松" }],
    product_form_candidate: "手串",
  });

  assert.deepEqual(view, {
    state: "not_started",
    stateLabel: "AI识别未启动",
    statusText: "当前未配置在线视觉服务，未生成 AI 识别候选。",
    detailText: "相同图片的历史人工标签只会显示在下方参考区。",
    retryEnabled: false,
    retryLabel: "视觉服务未配置",
  });
}

{
  const view = buildRecognitionStatusView({
    status: "succeeded",
    error_code: null,
    attempt: 2,
    material_candidates: [{ label: "绿松" }],
    product_form_candidate: "手串",
  });

  assert.equal(view.state, "ready");
  assert.equal(view.stateLabel, "AI候选已生成");
  assert.equal(view.statusText, "候选材质：绿松；候选形态：手串");
  assert.equal(view.retryEnabled, true);
  assert.equal(view.retryLabel, "重新识别");
}

{
  const detail = buildCandidateDetailView({
    status: "succeeded",
    visual_observations: ["多色调明显", "商品形态需训练员确认"],
    selling_point_drafts: ["多色调在图片中较醒目，便于展示整体视觉效果"],
    specification_candidates: ["商品实际尺寸待测量"],
    price_status: "pending_price_standard",
    authenticity_verification: "not_verified",
    origin_verification: "not_verified",
    quality_verification: "not_verified",
    warehouse_verification: "pending_warehouse_verification",
  });

  assert.deepEqual(detail, {
    visible: true,
    visualObservations: ["多色调明显", "商品形态需训练员确认"],
    sellingPointDrafts: ["多色调在图片中较醒目，便于展示整体视觉效果"],
    specificationCandidates: ["商品实际尺寸待测量"],
    priceStatusText: "价格建议：待录入该品类价格标准",
    verificationText: "真伪、产地、品质未验证；仓验待完成",
  });
}

{
  assert.deepEqual(buildCandidateDetailView(null), {
    visible: false,
    visualObservations: [],
    sellingPointDrafts: [],
    specificationCandidates: [],
    priceStatusText: "",
    verificationText: "",
  });
}

{
  const view = buildRecognitionStatusView({
    status: "succeeded",
    provider: "apple_vision",
    error_code: null,
    attempt: 1,
    material_candidates: [{ label: "绿色宝石或矿物类候选" }],
    product_form_candidate: "手串",
  });

  assert.equal(view.state, "ready");
  assert.equal(view.stateLabel, "基础资料已生成");
  assert.equal(
    view.statusText,
    "候选材质：绿色宝石或矿物类候选；候选形态：手串",
  );
  assert.equal(
    view.detailText,
    "系统已先生成可编辑资料 · 图片未离开本机 · 第 1 次；请训练员直接纠正。",
  );
  assert.equal(view.retryEnabled, true);
  assert.equal(view.retryLabel, "重新识别");
}

{
  const view = buildRecognitionStatusView({
    status: "failed",
    provider: "none",
    error_code: "local_vision_unavailable",
    attempt: 1,
    material_candidates: [],
    product_form_candidate: "待识别",
  });

  assert.equal(view.state, "not_started");
  assert.equal(view.stateLabel, "本机视觉不可用");
  assert.equal(view.retryEnabled, false);
  assert.equal(view.retryLabel, "本机功能不可用");
}

{
  const view = buildRecognitionStatusView({
    status: "failed",
    provider: "none",
    error_code: "local_vision_timeout",
    attempt: 1,
    material_candidates: [],
    product_form_candidate: "待识别",
  });

  assert.equal(view.state, "failed");
  assert.equal(view.stateLabel, "本机识别超时");
  assert.equal(view.retryEnabled, true);
  assert.equal(view.retryLabel, "重新识别");
}

{
  const view = buildRecognitionStatusView({
    status: "failed",
    error_code: "vision_timeout",
    attempt: 1,
    material_candidates: [],
    product_form_candidate: "待识别",
  });

  assert.equal(view.state, "failed");
  assert.equal(view.stateLabel, "AI识别失败");
  assert.equal(view.retryEnabled, true);
  assert.equal(view.retryLabel, "重新识别");
}

{
  assert.deepEqual(buildRecognitionStatusView(null), {
    state: "pending",
    stateLabel: "等待识别",
    statusText: "尚未产生系统识别候选。",
    detailText: "图片已保留，可以开始识别；不会用占位内容冒充结果。",
    retryEnabled: true,
    retryLabel: "开始识别",
  });
}

{
  const view = buildRecognitionStatusView(
    {
      status: "fallback_exact_match",
      provider: "local_exact_match",
      error_code: "vision_not_configured",
      attempt: 1,
      material_candidates: [{ label: "绿松" }],
      product_form_candidate: "手串",
    },
    {
      provider: "apple_vision",
      model: "apple-vision-vnclassify-v1",
      available: true,
    },
  );

  assert.equal(view.state, "pending");
  assert.equal(view.stateLabel, "本机视觉已就绪");
  assert.equal(view.retryEnabled, true);
  assert.equal(view.retryLabel, "开始本机识别");
}

console.log("recognition status view tests passed");
