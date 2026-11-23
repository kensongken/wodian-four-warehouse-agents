"use client";

import { useState } from "react";

type ProductSummary = Readonly<{
  productCode: string;
  strategyInputVersion: number;
  materialCandidate: string;
  formCandidate: string;
  status: "preview_only";
  inventoryStatus: "pending";
  priceStatus: "pending";
  visualObservations: string[];
  sellingPointCandidates: string[];
  serviceRuleSummaries: string[];
  unverifiedItems: string[];
  forbiddenExpressions: string[];
}>;

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function summaries(payload: unknown): ProductSummary[] | undefined {
  if (!payload || typeof payload !== "object" || !("products" in payload)
      || !Array.isArray(payload.products)) return undefined;
  const products = payload.products.filter((item): item is ProductSummary => (
    item !== null
    && typeof item === "object"
    && "productCode" in item && typeof item.productCode === "string"
    && "strategyInputVersion" in item && Number.isInteger(item.strategyInputVersion)
    && "materialCandidate" in item && typeof item.materialCandidate === "string"
    && "formCandidate" in item && typeof item.formCandidate === "string"
    && "status" in item && item.status === "preview_only"
    && "inventoryStatus" in item && item.inventoryStatus === "pending"
    && "priceStatus" in item && item.priceStatus === "pending"
    && "visualObservations" in item && isStringArray(item.visualObservations)
    && "sellingPointCandidates" in item && isStringArray(item.sellingPointCandidates)
    && "serviceRuleSummaries" in item && isStringArray(item.serviceRuleSummaries)
    && "unverifiedItems" in item && isStringArray(item.unverifiedItems)
    && "forbiddenExpressions" in item && isStringArray(item.forbiddenExpressions)
  ));
  return products.length === payload.products.length ? products : undefined;
}

export function ProductStrategyPicker({ value, onChange }: Readonly<{
  value: string;
  onChange: (value: string) => void;
}>) {
  const [products, setProducts] = useState<ProductSummary[]>();
  const [status, setStatus] = useState<"idle" | "loading" | "failed">("idle");
  const selected = products?.find((product) => product.productCode === value);

  async function load(): Promise<void> {
    setStatus("loading");
    try {
      const response = await fetch("/api/product-strategy-inputs");
      const payload: unknown = await response.json();
      const parsed = response.ok ? summaries(payload) : undefined;
      if (!parsed) throw new Error("invalid product summaries");
      setProducts(parsed);
      setStatus("idle");
    } catch {
      setStatus("failed");
    }
  }

  return (
    <>
      <label htmlFor="product-code">商品编号（已同步时优先）</label>
      <input
        id="product-code"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        pattern="[A-Za-z0-9_-]+"
      />
      <button type="button" className="secondary-action" onClick={load} disabled={status === "loading"}>
        {status === "loading" ? "正在加载…" : "加载已同步商品"}
      </button>
      {products ? (
        products.length === 0 ? <p role="status">暂无已同步商品，可继续手工填写商品描述。</p> : (
          <label>
            选择已同步商品
            <select value={value} onChange={(event) => onChange(event.target.value)}>
              <option value="">请选择</option>
              {products.map((product) => (
                <option key={product.productCode} value={product.productCode}>
                  {product.productCode}｜{product.materialCandidate}候选｜{product.formCandidate}候选
                </option>
              ))}
            </select>
          </label>
        )
      ) : null}
      {status === "failed" ? <p role="alert">已同步商品加载失败，可手输编号或改用商品描述。</p> : null}
      {selected ? (
        <section className="product-strategy-preview" aria-label="商品安全预览">
          <p className="eyebrow">preview_only｜策略版本 {selected.strategyInputVersion}</p>
          <h3>{selected.productCode}｜{selected.materialCandidate}候选｜{selected.formCandidate}候选</h3>
          <p>库存待确认｜价格待确认｜仅供人工预览</p>
          <div>
            <h4>可见特征</h4>
            <p>{selected.visualObservations.join("、") || "暂无，请人工观察。"}</p>
          </div>
          <div>
            <h4>候选卖点</h4>
            <p>{selected.sellingPointCandidates.join("、") || "暂无，不要自行补充。"}</p>
          </div>
          <div>
            <h4>服务规则摘要</h4>
            <p>{selected.serviceRuleSummaries.join("、") || "暂无，需门店核对。"}</p>
          </div>
          <div>
            <h4>待核验事项</h4>
            <p>{selected.unverifiedItems.join("、") || "无新增记录。"}</p>
          </div>
          <div>
            <h4>禁止对客表达</h4>
            <p>{selected.forbiddenExpressions.length > 0
              ? `禁止对客表达：${selected.forbiddenExpressions.join("、")}`
              : "继续遵守人工复核和禁止自动发送规则。"}</p>
          </div>
        </section>
      ) : null}
    </>
  );
}
