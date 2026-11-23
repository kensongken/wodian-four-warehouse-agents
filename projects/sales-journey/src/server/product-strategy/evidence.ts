import type { EvidenceRef } from "@/domain/decision-card/types";
import type { ProductStrategyReceipt } from "./repository";

export function productStrategyEvidence(receipt: ProductStrategyReceipt): EvidenceRef {
  const input = receipt.strategyInput;
  return {
    id: `product-strategy-${input.product_code}-v${input.strategy_input_version}`,
    kind: "product_record",
    title: [
      `商品 ${input.product_code}`,
      `材质候选：${input.material.value}`,
      `形态候选：${input.form.value}`,
      "库存待确认",
      "价格待确认",
      "仅供人工预览",
    ].join("｜"),
    // Candidate facts are readable context, but must not authorize factual claims.
    verifiedFields: [],
    status: "approved",
  };
}

export function productStrategyDecisionSummary(receipt: ProductStrategyReceipt): string {
  const input = receipt.strategyInput;
  const observations = input.visual_observations.join("、") || "暂无可见特征记录";
  const candidates = [
    `材质 ${input.material.value}`,
    `形态 ${input.form.value}`,
    ...input.selling_point_candidates,
  ].join("、");
  const services = input.service_rule_summaries.join("、") || "暂无服务规则摘要";
  const unverified = [
    ...input.unverified_items,
    "库存状态",
    "价格状态",
  ].join("、");
  const forbidden = input.forbidden_expressions.join("、") || "无新增记录";
  return [
    `商品安全资料（内部）：${input.product_code}｜v${input.strategy_input_version}｜preview_only`,
    `可用于观察：${observations}`,
    `仅作候选：${candidates}`,
    `服务规则待核：${services}`,
    `必须人工核验：${unverified}`,
    `禁止对客表达：${forbidden}`,
  ].join("\n");
}
