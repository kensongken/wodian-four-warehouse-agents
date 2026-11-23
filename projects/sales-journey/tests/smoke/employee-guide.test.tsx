import { render, screen } from "@testing-library/react";

import EmployeeGuidePage from "@/app/employee-guide/page";

it("renders the employee operating guide with the daily workflow", () => {
  render(<EmployeeGuidePage />);

  expect(screen.getByRole("heading", { name: "员工操作说明" })).toBeInTheDocument();
  expect(screen.getByText("接待客户")).toBeInTheDocument();
  expect(screen.getByText("生成决策卡")).toBeInTheDocument();
  expect(screen.getByText(/先选业务大类，再选具体场景；不确定时选“其他\/不确定”。/)).toBeInTheDocument();
  expect(screen.getByText("商品咨询与购买、订单与交付、定制与调整、瑕疵与争议、售后与养护、身份珠与扫码、寄售与送回、客户跟进与复购")).toBeInTheDocument();
  expect(screen.getByText("人工核验后发送")).toBeInTheDocument();
  expect(screen.getByText("记录结果并复盘")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Reply Agent 评价怎么用" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "开始生成决策卡" })).toHaveAttribute("href", "/");
});
