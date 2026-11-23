import { headers } from "next/headers";
import { notFound } from "next/navigation";

import { requireOwnerFromD1 } from "@/server/auth/authorization";
import { OwnerEmployeeDetail } from "@/components/supervision/owner-dashboard";
import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";
import { getOwnerDirectory, resolveOwnerEmployeeRef, toOwnerDashboardBrowserView } from "@/server/supervision/owner-view";

export const dynamic = "force-dynamic";

type OwnerEmployeePageProps = Readonly<{
  params: Promise<Readonly<{ userId: string }>>;
}>;

export default async function OwnerEmployeePage({ params }: OwnerEmployeePageProps) {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireOwnerFromD1(await headers(), runtime.d1);
  const { userId } = await params;
  const now = runtime.now?.() ?? new Date().toISOString();
  const directory = await getOwnerDirectory(runtime.d1, actor.tenantId, runtime.usageTokenSecret);
  const directoryEmployee = resolveOwnerEmployeeRef(directory, userId);
  if (!directoryEmployee) notFound();
  const internalUserId = directoryEmployee.userId;
  const dashboard = await runtime.repository.getOwnerDashboard({
      tenantId: actor.tenantId, now, employeeUserId: internalUserId,
  });
  const view = toOwnerDashboardBrowserView(dashboard, directory);
  const browserEmployee = view.employees.find((employee) => employee.navigationId === userId);
  if (!browserEmployee) notFound();
  const assignedCustomers = view.customers.filter((customer) => directory.customers.find((raw) => raw.navigationId === customer.navigationId)?.employeeUserId === internalUserId);
  const employeeLabel = browserEmployee.displayLabel;

  return (
    <main className="app-shell owner-workspace">
      <header className="app-header owner-page-header">
        <div>
          <p className="eyebrow">员工执行详情</p>
          <h1>{employeeLabel}</h1>
          <p>只呈现可核对的动作与结果，不生成自动绩效结论。</p>
        </div>
        <a href="/owner">返回老板看板</a>
      </header>

      <OwnerEmployeeDetail employee={browserEmployee} records={view.records} customers={assignedCustomers} />

    </main>
  );
}
