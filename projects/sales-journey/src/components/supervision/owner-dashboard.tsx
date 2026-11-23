"use client";

import { useState } from "react";

import { SALES_SCENARIOS, SALES_STAGES } from "@/domain/decision-card/types";
import { scenarioLabelFor } from "@/domain/decision-card/scenarios/catalog";
import type { OwnerReplyQualityView } from "@/domain/supervision/contracts";
import { ownerCustomerMatches } from "@/domain/supervision/customer-query";
import type {
  OwnerBrowserCustomer,
  OwnerBrowserEmployee,
  OwnerBrowserRecord,
  OwnerDashboardBrowserView,
  OwnerReplyQualityFilter,
} from "@/server/supervision/owner-view";

const RESULTS = { pending: "待跟进", interested: "意向中", won: "已成交", lost: "未成交" } as const;
const USAGE = { adopted: "已采用", edited: "已改写", not_used: "未使用" } as const;
const STAGES = {
  discover: "需求发现", explain: "说明信息", evaluate: "比较评估",
  advance: "推进下一步", follow_up: "后续跟进",
} as const;
const FEEDBACK_REASONS = {
  too_stiff: "太生硬", too_long: "太长", wrong_stage: "阶段不符",
  fact_inaccurate: "事实不准确", no_next_step: "没有推进下一步", other: "其他",
} as const;

function Drill({ records, customers = [], refs }: {
  records: readonly OwnerBrowserRecord[];
  customers?: readonly OwnerBrowserCustomer[];
  refs: readonly string[];
}) {
  const byRef = new Map(records.map((record) => [record.ref, record]));
  const customerByRef = new Map(customers.map((customer) => [`customer-row-${customer.navigationId}`, customer]));
  return <div className="owner-drilldown" aria-live="polite"><h3>明细记录</h3>{refs.length ? <ul className="owner-detail-list">{refs.map((ref) => {
    const record = byRef.get(ref), customer = customerByRef.get(ref);
    return record ? <li key={ref} data-testid="detail-row" data-record-ref={ref}><strong>{record.customerAlias} · {record.customerNumber}</strong><span>{record.employeeLabel}</span>{record.customerNavigationId ? <a href={`/owner/customers/${record.customerNavigationId}`}>查看记录</a> : null}</li>
      : customer ? <li key={ref} data-testid="detail-row" data-record-ref={ref}><a href={`/owner/customers/${customer.navigationId}`}>{customer.customerAlias} · {customer.customerNumber}</a></li> : null;
  })}</ul> : <p className="owner-empty">当前没有匹配记录。</p>}</div>;
}

function ReplyQualityPanel({ quality, employees, customers, filters, agentVersions }: {
  quality: OwnerReplyQualityView;
  employees: readonly OwnerBrowserEmployee[];
  customers: readonly OwnerBrowserCustomer[];
  filters: OwnerReplyQualityFilter;
  agentVersions: readonly string[];
}) {
  const employeeLabels = new Map(employees.map((employee) => [employee.navigationId, employee.displayLabel]));
  const customerLabels = new Map(customers.map((customer) => [customer.navigationId, `${customer.customerAlias} · ${customer.customerNumber}`]));
  return <section className="owner-panel">
    <h2>回复质量（不用于员工打分）</h2>
    <p>用于改进回复工具，不生成员工分数或名次。</p>
    <form key={JSON.stringify(filters)} className="owner-filters" action="/owner" method="get">
      <label>质量负责人<select name="employeeRef" defaultValue={filters.employeeRef ?? ""}><option value="">全部员工</option>{employees.map((employee) => <option key={employee.navigationId} value={employee.navigationId}>{employee.displayLabel}</option>)}</select></label>
      <label>销售场景<select name="scenario" defaultValue={filters.scenario ?? ""}><option value="">全部场景</option>{SALES_SCENARIOS.map((scenario) => <option key={scenario} value={scenario}>{scenarioLabelFor(scenario)}</option>)}</select></label>
      <label>销售阶段<select name="salesStage" defaultValue={filters.salesStage ?? ""}><option value="">全部阶段</option>{SALES_STAGES.map((stage) => <option key={stage} value={stage}>{STAGES[stage]}</option>)}</select></label>
      <label>Agent 版本<select name="agentVersion" defaultValue={filters.agentVersion ?? ""}><option value="">全部版本</option>{agentVersions.map((version) => <option key={version}>{version}</option>)}</select></label>
      <button type="submit">应用质量筛选</button>
    </form>
    <div className="owner-metrics" aria-label="回复质量统计"><span>有帮助 {quality.totals.helpful}</span><span>需要改进 {quality.totals.needsImprovement}</span><span>隔离样本 {quality.totals.quarantined}</span></div>
    <div><h3>改进原因</h3>{quality.reasons.length ? <ul>{quality.reasons.map((reason) => <li key={reason.code}>{FEEDBACK_REASONS[reason.code]} {reason.count}</li>)}</ul> : <p className="owner-empty">当前没有改进原因。</p>}</div>
    <div><h3>脱敏样本</h3>{quality.samples.length ? <ul className="owner-detail-list">{quality.samples.map((sample) => <li key={sample.feedbackRef}>
      <strong>{employeeLabels.get(sample.employeeRef) ?? "已停用员工"}</strong>
      <span>{customerLabels.get(sample.customerRef) ?? "客户资料不可用"}</span>
      <span>{scenarioLabelFor(sample.scenario)} · {STAGES[sample.salesStage]} · {sample.agentVersion}</span>
      <span>{sample.rating === "helpful" ? "有帮助" : "需要改进"}</span>
      {sample.privacyStatus === "safe" ? (sample.sanitizedFinalReply === undefined ? <span>未填写最终回复</span> : <blockquote>{sample.sanitizedFinalReply}</blockquote>) : <span>隔离原因：{sample.privacyReasonCodes.join("、")}</span>}
    </li>)}</ul> : <p className="owner-empty">当前没有匹配的质量样本。</p>}</div>
  </section>;
}

export function OwnerDashboard({ view, replyQuality, replyQualityFilters = {}, replyQualityAgentVersions }: {
  view: OwnerDashboardBrowserView;
  replyQuality: OwnerReplyQualityView;
  replyQualityFilters?: OwnerReplyQualityFilter;
  replyQualityAgentVersions?: readonly string[];
}) {
  const [refs, setRefs] = useState(view.summaryMetrics[0]?.recordRefs ?? []), [employee, setEmployee] = useState(""), [customer, setCustomer] = useState(""), [category, setCategory] = useState(""), [result, setResult] = useState("");
  const filtered = view.records.filter((record) => (!employee || record.employeeNavigationId === employee) && (!customer.trim() || ownerCustomerMatches(record.customerAlias, record.customerNumber, customer)) && (!category || record.category === category) && (!result || record.result === result));
  const params = new URLSearchParams(); if (employee) params.set("employeeRef", employee); if (customer) params.set("customerQuery", customer); if (category) params.set("category", category); if (result) params.set("result", result);
  const categories = [...new Set(view.customers.map((item) => item.category))];
  const versions = replyQualityAgentVersions ?? [...new Set(replyQuality.samples.map((sample) => sample.agentVersion))];
  return <div className="owner-dashboard">
    <section className="owner-panel"><h2>今日执行</h2><p>点数字直接核对对应记录，不生成员工分数。</p><div className="owner-metrics">{view.summaryMetrics.map((metric) => <button className="owner-metric" key={metric.key} onClick={() => setRefs(metric.recordRefs)}><span>{metric.label}</span><strong>{metric.valueLabel}</strong></button>)}</div><Drill records={view.records} customers={view.customers} refs={refs} /></section>
    <section className="owner-panel"><h2>员工执行</h2><div className="owner-employee-table">{view.employees.map((item) => <div className="owner-employee-row" key={item.navigationId}><a aria-label={`查看${item.displayLabel}详情`} href={`/owner/employees/${item.navigationId}`}><strong>{item.displayLabel}</strong></a>{item.metrics.slice(0, 3).map((metric) => <button key={metric.key} aria-label={`${item.displayLabel}：${metric.label} ${metric.valueLabel}`} onClick={() => setRefs(metric.recordRefs)}>{metric.label} {metric.valueLabel}</button>)}</div>)}</div></section>
    <ReplyQualityPanel quality={replyQuality} employees={view.employees} customers={view.customers} filters={replyQualityFilters} agentVersions={versions} />
    <section className="owner-panel"><div className="owner-section-heading"><h2>业务结果</h2><a className="owner-export" href={`/api/supervision/owner/export${params.size ? `?${params}` : ""}`}>导出当前筛选</a></div><div className="owner-filters"><label>负责人<select value={employee} onChange={(event) => setEmployee(event.target.value)}><option value="">全部员工</option>{view.employees.map((item) => <option key={item.navigationId} value={item.navigationId}>{item.displayLabel}</option>)}</select></label><label>客户编号或称呼<input value={customer} onChange={(event) => setCustomer(event.target.value)} /></label><label>品类<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">全部品类</option>{categories.map((item) => <option key={item}>{item}</option>)}</select></label><label>结果<select value={result} onChange={(event) => setResult(event.target.value)}><option value="">全部结果</option>{Object.entries(RESULTS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div><table aria-label="业务结果明细" className="owner-table"><thead><tr><th>客户</th><th>负责人</th><th>回复使用</th><th>结果</th><th>下一步</th></tr></thead><tbody>{filtered.map((record) => <tr key={record.ref}><td>{record.customerAlias} · {record.customerNumber}</td><td>{record.employeeLabel}</td><td>{USAGE[record.decisionUsage]}</td><td>{RESULTS[record.result]}</td><td>{record.nextAction}</td></tr>)}</tbody></table></section>
    <section className="owner-panel"><h2>客户目录</h2><ul>{view.customers.map((item) => <li key={item.navigationId}><a href={`/owner/customers/${item.navigationId}`}>{item.customerAlias} · {item.customerNumber}</a></li>)}</ul></section><section className="owner-panel"><h2>风险提醒</h2><ul className="owner-risk-list">{view.risks.map((risk) => <li key={risk.kind}><button onClick={() => setRefs(risk.recordRefs)}>{risk.label} {risk.recordRefs.length}</button></li>)}</ul></section>
  </div>;
}

export function OwnerEmployeeDetail({ employee, records, customers }: { employee: OwnerBrowserEmployee; records: readonly OwnerBrowserRecord[]; customers: readonly OwnerBrowserCustomer[] }) {
  const [refs, setRefs] = useState<readonly string[]>([]); const wanted = new Set(["completionRate", "adopted", "edited", "notUsed", "won", "interested", "lost", "overdue"]);
  return <><section className="owner-panel"><h2>跟进完成情况</h2>{employee.metrics.filter((metric) => wanted.has(metric.key)).map((metric) => <button key={metric.key} onClick={() => setRefs(metric.recordRefs)}>{metric.label} {metric.valueLabel}</button>)}<Drill records={records} refs={refs} /></section><section className="owner-panel"><h2>负责客户</h2><ul>{customers.map((customer) => <li key={customer.navigationId}><a href={`/owner/customers/${customer.navigationId}`}>{customer.customerAlias} · {customer.customerNumber}</a></li>)}</ul></section></>;
}
