import { headers } from "next/headers";

import { CustomerTimeline } from "@/components/supervision/customer-timeline";
import { FollowUpForm } from "@/components/supervision/follow-up-form";
import { requireEmployeeFromD1 } from "@/server/auth/authorization";
import {
  createCustomerService,
  getSupervisionApiRuntime,
} from "@/server/supervision/customer-service";
import {
  toEmployeeCustomerView,
  toEmployeeTimelineView,
} from "@/server/supervision/employee-view";

export const dynamic = "force-dynamic";

type CustomerPageProps = Readonly<{
  params: Promise<Readonly<{ customerId: string }>>;
}>;

export default async function EmployeeCustomerPage({ params }: CustomerPageProps) {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireEmployeeFromD1(await headers(), runtime.d1);
  const { customer, timeline } = await createCustomerService(runtime).getCustomerTimeline(
    actor,
    (await params).customerId,
  );
  const customerView = toEmployeeCustomerView(customer);
  const timelineView = toEmployeeTimelineView(
    timeline,
    (actorUserId) => actorUserId === actor.accountUserId ? actor.email : "店员",
  );

  return (
    <main className="app-shell employee-workspace">
      <header className="app-header customer-page-heading">
        <div>
          <p className="eyebrow">客户跟进工作台</p>
          <h1>{customerView.customerAlias} · {customerView.customerNumber}</h1>
          <p>{customerView.category} · 负责人：{actor.email}</p>
        </div>
        <a href="/employee/customers">返回我的客户</a>
      </header>
      <div className="employee-customer-grid">
        <FollowUpForm customer={customerView} employeeLabel={actor.email} />
        <CustomerTimeline
          customerId={customerView.id}
          entries={timelineView}
        />
      </div>
    </main>
  );
}
