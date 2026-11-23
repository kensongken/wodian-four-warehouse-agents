import { headers } from "next/headers";
import { notFound } from "next/navigation";

import type { EmployeeTimelineEntryView } from "@/domain/supervision/employee-view";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";
import {
  getOwnerCustomerVaultAssets,
  getOwnerDirectory,
  resolveOwnerCustomerRef,
  toOwnerTimelineView,
} from "@/server/supervision/owner-view";

export const dynamic = "force-dynamic";

type OwnerCustomerPageProps = Readonly<{
  params: Promise<Readonly<{ customerId: string }>>;
}>;

const ACTION_LABELS = {
  follow_up: "客户跟进",
  decision_usage: "决策卡使用",
  correction: "补充纠错",
} as const;

const VAULT_LABELS = {
  customer_identity: "查看客户身份资料",
  chat_text: "查看聊天原始资料",
  product_image: "查看商品图片",
} as const;

function displayTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function timelineSummary(entry: EmployeeTimelineEntryView): string {
  if (entry.kind === "follow_up") return entry.customerMessageSummary;
  if (entry.kind === "correction") return entry.reason;
  return entry.replySelection ?? "未选择回复";
}

export default async function OwnerCustomerPage({ params }: OwnerCustomerPageProps) {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireOwnerFromD1(await headers(), runtime.d1);
  const { customerId } = await params;
  const now = runtime.now?.() ?? new Date().toISOString();
  const directory = await getOwnerDirectory(runtime.d1, actor.tenantId, runtime.usageTokenSecret);
  const customer = resolveOwnerCustomerRef(directory, customerId);
  if (!customer) notFound();
  const internalCustomerId = customer.id;
  const [timeline, assets] = await Promise.all([
    runtime.repository.listTimeline({ tenantId: actor.tenantId, customerId: internalCustomerId }),
    getOwnerCustomerVaultAssets(runtime.d1, actor.tenantId, internalCustomerId, now),
  ]);
  const employeeLabels = new Map(
    directory.employees.map((employee) => [employee.userId, employee.displayLabel]),
  );
  const timelineView = toOwnerTimelineView(timeline, employeeLabels);

  return (
    <main className="app-shell owner-workspace">
      <header className="app-header owner-page-header">
        <div>
          <p className="eyebrow">客户不可改记录</p>
          <h1>{customer.customerAlias} · {customer.customerNumber}</h1>
          <p>{customer.category} · 页面只使用客户称呼和客户编号。</p>
        </div>
        <a href="/owner">返回老板看板</a>
      </header>

      <section className="owner-panel" aria-labelledby="customer-timeline-heading">
        <div className="owner-section-heading">
          <div>
            <p className="eyebrow">追加式记录，不能覆盖历史</p>
            <h2 id="customer-timeline-heading">客户时间线</h2>
          </div>
          <span className="immutable-chip">不可改</span>
        </div>
        {timelineView.length === 0 ? <p className="owner-empty">当前没有跟进记录。</p> : (
          <ol className="customer-timeline owner-readonly-timeline">
            {timelineView.map((entry) => (
              <li key={`${entry.kind}-${entry.id}`}>
                <span className="timeline-marker" aria-hidden="true" />
                <article>
                  <header>
                    <strong>{ACTION_LABELS[entry.kind]}</strong>
                    <time dateTime={entry.createdAt}>{displayTime(entry.createdAt)}</time>
                  </header>
                  <p className="timeline-actor">记录人：{entry.actorLabel}</p>
                  <p>{timelineSummary(entry)}</p>
                  {entry.kind === "follow_up" ? (
                    <p className="timeline-detail">下一步：{entry.nextAction}</p>
                  ) : null}
                </article>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="owner-panel" aria-labelledby="customer-vault-heading">
        <div className="owner-section-heading">
          <div>
            <p className="eyebrow">老板授权后才读取</p>
            <h2 id="customer-vault-heading">原始资料保险库</h2>
          </div>
        </div>
        {assets.length === 0 ? <p className="owner-empty">当前没有可查看的原始资料。</p> : (
          <ul className="owner-vault-list">
            {assets.map((asset) => (
              <li key={asset.id}>
                <a href={`/owner/vault/${encodeURIComponent(asset.id)}`}>{VAULT_LABELS[asset.kind]}</a>
                <span>删除时间：{displayTime(asset.deleteAfter)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
