import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useState } from "react";

import { ProductStrategyPicker } from "@/components/product-strategy-picker";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("加载已同步商品并选中其商品编号", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ products: [{
    productCode: "SYNTHETIC-001",
    strategyInputVersion: 1,
    materialCandidate: "南红",
    formCandidate: "手串",
    status: "preview_only",
    inventoryStatus: "pending",
    priceStatus: "pending",
    visualObservations: ["红色珠串，可见自然纹理"],
    sellingPointCandidates: ["适合日常佩戴"],
    serviceRuleSummaries: ["尺寸调整需门店确认"],
    unverifiedItems: ["材质待人工确认"],
    forbiddenExpressions: ["官方保真"],
  }] })));
  const onChange = vi.fn();
  function Harness() {
    const [value, setValue] = useState("");
    return <ProductStrategyPicker value={value} onChange={(next) => {
      onChange(next);
      setValue(next);
    }} />;
  }
  render(<Harness />);

  fireEvent.click(screen.getByRole("button", { name: "加载已同步商品" }));
  const selector = await screen.findByLabelText("选择已同步商品");
  expect(selector).toHaveTextContent("SYNTHETIC-001｜南红候选｜手串候选");
  fireEvent.change(selector, { target: { value: "SYNTHETIC-001" } });

  expect(onChange).toHaveBeenCalledWith("SYNTHETIC-001");
  const preview = screen.getByRole("region", { name: "商品安全预览" });
  expect(preview).toHaveTextContent("红色珠串，可见自然纹理");
  expect(preview).toHaveTextContent("适合日常佩戴");
  expect(preview).toHaveTextContent("材质待人工确认");
  expect(preview).toHaveTextContent("禁止对客表达：官方保真");
  expect(preview).toHaveTextContent("库存待确认｜价格待确认｜仅供人工预览");
});

it("列表为空或加载失败时保留人工输入路径", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({ products: [] }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: "unavailable" }), { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ProductStrategyPicker value="MANUAL-001" onChange={() => {}} />);

  expect(screen.getByLabelText("商品编号（已同步时优先）")).toHaveValue("MANUAL-001");
  fireEvent.click(screen.getByRole("button", { name: "加载已同步商品" }));
  expect(await screen.findByRole("status")).toHaveTextContent("可继续手工填写");
  fireEvent.click(screen.getByRole("button", { name: "加载已同步商品" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("可手输编号");
  expect(screen.getByLabelText("商品编号（已同步时优先）")).toHaveValue("MANUAL-001");
});
