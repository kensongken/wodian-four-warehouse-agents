import { headers } from "next/headers";

import { OwnerDashboard } from "@/components/supervision/owner-dashboard";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";
import {
  getOwnerDirectory,
  getOwnerReplyQualityView,
  parseOwnerReplyQualityFilters,
  resolveOwnerEmployeeRef,
  toOwnerDashboardBrowserView,
} from "@/server/supervision/owner-view";

export const dynamic = "force-dynamic";

type OwnerPageProps = Readonly<{
  searchParams?: Promise<Readonly<Record<string, string | string[] | undefined>>>;
}>;

export default async function OwnerPage(props: OwnerPageProps) {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireOwnerFromD1(await headers(), runtime.d1);
  const now = runtime.now?.() ?? new Date().toISOString();
  const rawSearchParams = await props.searchParams ?? {};
  const qualityFilters = parseOwnerReplyQualityFilters(Object.entries(rawSearchParams));
  const [dashboard, directory] = await Promise.all([
    runtime.repository.getOwnerDashboard({ tenantId: actor.tenantId, now }),
    getOwnerDirectory(runtime.d1, actor.tenantId, runtime.usageTokenSecret),
  ]);
  const employee = qualityFilters.employeeRef
    ? resolveOwnerEmployeeRef(directory, qualityFilters.employeeRef)
    : undefined;
  const [replyQuality, unfilteredReplyQuality] = await Promise.all([
    getOwnerReplyQualityView(runtime.repository, directory, {
      tenantId: actor.tenantId,
      employeeUserId: qualityFilters.employeeRef
        ? employee?.userId ?? "__invalid_employee_ref__"
        : undefined,
      scenario: qualityFilters.scenario,
      salesStage: qualityFilters.salesStage,
      agentVersion: qualityFilters.agentVersion,
    }),
    getOwnerReplyQualityView(runtime.repository, directory, { tenantId: actor.tenantId }),
  ]);

  return (
    <main className="app-shell owner-workspace">
      <header className="app-header owner-page-header">
        <div>
          <p className="eyebrow">店铺经营监督</p>
          <h1>老板执行看板</h1>
          <p>先处理跟进动作，再核对业务结果和需要人工复核的风险。</p>
        </div>
        <span className="owner-identity">{actor.email}</span>
      </header>
      <OwnerDashboard
        view={toOwnerDashboardBrowserView(dashboard, directory)}
        replyQuality={replyQuality}
        replyQualityFilters={qualityFilters}
        replyQualityAgentVersions={[
          ...new Set(unfilteredReplyQuality.samples.map((sample) => sample.agentVersion)),
        ]}
      />
    </main>
  );
}
