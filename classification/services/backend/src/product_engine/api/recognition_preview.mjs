function cleanLines(value) {
  if (Array.isArray(value)) {
    return value.map(item => String(item).trim()).filter(Boolean);
  }
  return String(value || "")
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
}

export function candidateToDraft(candidate) {
  const material = candidate?.material_candidates?.[0]?.label || "待确认";
  return {
    template_code: candidate?.template_code || "",
    main_image_template_code: candidate?.template_code || null,
    version: `AI候选 ${candidate?.attempt || 1}`,
    material_label: material,
    product_form: candidate?.product_form_candidate || "待确认",
    visual_observations: cleanLines(candidate?.visual_observations),
    selling_points: cleanLines(candidate?.selling_point_drafts),
    specification_note: cleanLines(candidate?.specification_candidates).join("；"),
    quality_note: "",
    defect_note: "",
    price_range: null,
    price_reason: "待录入该品类的价格标准",
    preview_source: "ai_candidate",
  };
}

export function formToDraft(base, values) {
  const suggestedPrice = String(values.suggested_price || "").trim();
  const minimum = String(values.price_minimum || "").trim();
  const maximum = String(values.price_maximum || "").trim();
  const basis = String(values.price_basis || "").trim();
  return {
    ...base,
    version: "未保存草稿",
    main_image_template_code: values.main_image_selected
      ? base?.template_code || null
      : null,
    material_label: String(values.material_label || "").trim() || "待确认",
    product_form: String(values.product_form || "").trim() || "待确认",
    visual_observations: cleanLines(values.visual_observations),
    selling_points: cleanLines(values.selling_points),
    specification_note: String(values.specification_note || "").trim(),
    quality_note: String(values.quality_note || "").trim(),
    defect_note: String(values.defect_note || "").trim(),
    price_range: suggestedPrice
      ? exactSuggestedPriceRange(suggestedPrice)
      : minimum || maximum || basis
        ? { currency: "CNY", minimum, maximum, basis }
        : base?.price_range || null,
    preview_source: "draft_unsaved",
  };
}

export function exactSuggestedPriceRange(value, fallback = null) {
  const amount = String(value || "").trim();
  if (!amount) return fallback;
  return {
    currency: "CNY",
    minimum: amount,
    maximum: amount,
    basis: "训练员输入的内部建议价",
  };
}

export function reviewItemStatusText(item) {
  if (item?.status === "reviewed") return "已审核";
  if (item?.status === "pending_review") return "待审核";
  return "不可审核";
}
