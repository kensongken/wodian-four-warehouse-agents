import { render, screen } from "@testing-library/react";
import Page from "@/app/page";

it("renders the decision-card shell", () => {
  const { container } = render(<Page />);

  expect(screen.getByRole("heading", { name: "销售决策卡" })).toBeInTheDocument();
  expect(container.querySelector("main.app-shell")).not.toBeNull();
  expect(container.querySelector(".decision-workspace")).not.toBeNull();
  expect(container.querySelector("form.decision-form")).not.toBeNull();
  expect(screen.getByLabelText("商品描述")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "员工操作说明" })).toHaveAttribute(
    "href",
    "/employee-guide",
  );
  expect(screen.getByRole("button", { name: "初始化并进入老板后台" })).toBeInTheDocument();
  expect(screen.getByText(/Reply Agent 的评价仅用于人工复核后的优化分析/)).toBeInTheDocument();
});
