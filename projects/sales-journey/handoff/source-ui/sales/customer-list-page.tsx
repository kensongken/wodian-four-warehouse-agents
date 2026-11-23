import { headers } from "next/headers";

import type { FollowUpResult } from "@/domain/supervision/contracts";
import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
} from "@/server/supervision/customer-service";
import { toEmployeeCustomerView } from "@/server/supervision/employee-view";

export const dynamic = "force-dynamic";

const STATUS_LABELS: Record<FollowUpResult, string> = {
  pending: "待跟进",
  interested: "意向中",
  won: "已成交",
  lost: "未成交",
};

function displayTime(timestamp: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

export default async function EmployeeCustomersPage() {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireEmployeeFromD1(await headers(), runtime.d1);
  const customers = (await createCustomerService(runtime).listCustomers(actor))
    .map(toEmployeeCustomerView);

  return (
    <main className="app-shell employee-workspace">
      <header className="app-header employee-page-header">
        <div>
          <p className="eyebrow">员工工作台</p>
          <h1>我的客户跟进</h1>
          <p>按客户逐条记录，让负责人看到真实进度与下一步。</p>
        </div>
        <a className="guide-start" href="/employee/customers/new">建立客户档案</a>
      </header>

      {customers.length === 0 ? (
        <section className="employee-panel empty-customer-list">
          <h2>还没有客户档案</h2>
          <p>先建立一位客户，再开始生成决策卡和记录跟进。</p>
        </section>
      ) : (
        <ul className="customer-list">
          {customers.map((customer) => (
            <li key={customer.id}>
              <a href={`/employee/customers/${encodeURIComponent(customer.id)}`}>
                <div>
                  <strong>{customer.customerAlias}</strong>
                  <span>{customer.customerNumber}</span>
                </div>
                <dl>
                  <div><dt>品类</dt><dd>{customer.category}</dd></div>
                  <div><dt>计划跟进</dt><dd>{displayTime(customer.plannedFollowUpAt)}</dd></div>
                  <div><dt>状态</dt><dd>{STATUS_LABELS[customer.status]}</dd></div>
                </dl>
              </a>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
