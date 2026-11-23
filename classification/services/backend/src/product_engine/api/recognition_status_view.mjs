function cleanItems(values) {
  return Array.isArray(values)
    ? values.map(value => String(value).trim()).filter(Boolean)
    : [];
}

export function buildCandidateDetailView(candidate) {
  if (candidate?.status !== "succeeded") {
    return {
      visible: false,
      visualObservations: [],
      sellingPointDrafts: [],
      specificationCandidates: [],
      priceStatusText: "",
      verificationText: "",
    };
  }
  return {
    visible: true,
    visualObservations: cleanItems(candidate.visual_observations),
    sellingPointDrafts: cleanItems(candidate.selling_point_drafts),
    specificationCandidates: cleanItems(candidate.specification_candidates),
    priceStatusText:
      candidate.price_status === "pending_price_standard"
        ? "价格建议：待录入该品类价格标准"
        : "价格建议：待人工确认",
    verificationText: "真伪、产地、品质未验证；仓验待完成",
  };
}

export function buildRecognitionStatusView(candidate, runtime = null) {
  if (!candidate) {
    return {
      state: "pending",
      stateLabel: "等待识别",
      statusText: "尚未产生系统识别候选。",
      detailText: "图片已保留，可以开始识别；不会用占位内容冒充结果。",
      retryEnabled: true,
      retryLabel: "开始识别",
    };
  }

  if (
    candidate.error_code === "vision_not_configured" &&
    runtime?.available &&
    runtime.provider === "apple_vision"
  ) {
    return {
      state: "pending",
      stateLabel: "本机视觉已就绪",
      statusText: "本机视觉服务已经准备，可以为这张图片生成新的系统候选。",
      detailText: "历史人工标签仍只显示在下方参考区，不会被冒充为本机结果。",
      retryEnabled: true,
      retryLabel: "开始本机识别",
    };
  }

  if (candidate.error_code === "vision_not_configured") {
    return {
      state: "not_started",
      stateLabel: "AI识别未启动",
      statusText: "当前未配置在线视觉服务，未生成 AI 识别候选。",
      detailText: "相同图片的历史人工标签只会显示在下方参考区。",
      retryEnabled: false,
      retryLabel: "视觉服务未配置",
    };
  }

  if (candidate.error_code === "local_vision_unavailable") {
    return {
      state: "not_started",
      stateLabel: "本机视觉不可用",
      statusText: "当前电脑未能启动本机视觉识别，未生成系统候选。",
      detailText: "图片已安全保留，历史人工标签仍只显示在下方参考区。",
      retryEnabled: false,
      retryLabel: "本机功能不可用",
    };
  }

  if (candidate.error_code === "local_vision_timeout") {
    return {
      state: "failed",
      stateLabel: "本机识别超时",
      statusText: "本次本机视觉分析超时，没有生成系统候选。",
      detailText: "图片已安全保留，可以重新识别或继续人工审核。",
      retryEnabled: true,
      retryLabel: "重新识别",
    };
  }

  if (candidate.status === "succeeded" && candidate.provider === "apple_vision") {
    const material = candidate.material_candidates?.[0]?.label || "待确认";
    const form = candidate.product_form_candidate || "待确认";
    return {
      state: "ready",
      stateLabel: "基础资料已生成",
      statusText: `候选材质：${material}；候选形态：${form}`,
      detailText: `系统已先生成可编辑资料 · 图片未离开本机 · 第 ${candidate.attempt || 1} 次；请训练员直接纠正。`,
      retryEnabled: true,
      retryLabel: "重新识别",
    };
  }

  if (candidate.status === "succeeded") {
    const material = candidate.material_candidates?.[0]?.label || "待确认";
    const form = candidate.product_form_candidate || "待确认";
    return {
      state: "ready",
      stateLabel: "AI候选已生成",
      statusText: `候选材质：${material}；候选形态：${form}`,
      detailText: `在线候选 · 第 ${candidate.attempt || 1} 次；请训练员结合实物与标准复核。`,
      retryEnabled: true,
      retryLabel: "重新识别",
    };
  }

  return {
    state: "failed",
    stateLabel: "AI识别失败",
    statusText: "系统识别暂时失败，本次没有生成 AI 识别结果。",
    detailText: "图片已安全保留；可以重新识别或继续人工审核。",
    retryEnabled: true,
    retryLabel: "重新识别",
  };
}
