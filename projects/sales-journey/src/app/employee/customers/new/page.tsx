import { headers } from "next/headers";

import { CustomerCreateForm } from "@/components/supervision/follow-up-form";
import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";

export const dynamic = "force-dynamic";

export default async function NewEmployeeCustomerPage() {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireEmployeeFromD1(await headers(), runtime.d1);

  return (
    <main className="app-shell employee-workspace">
      <header className="app-header">
        <p className="eyebrow">员工工作台</p>
        <h1>建立客户档案</h1>
        <p>日常页面只使用客户称呼和编号；真实姓名只进入保险柜。</p>
        <a href="/employee/customers">返回我的客户</a>
      </header>
      <CustomerCreateForm employeeLabel={actor.email} />
    </main>
  );
}
