import { DecisionCardForm } from "@/components/decision-card-form";
import { BootstrapOwnerButton } from "@/components/bootstrap-owner-button";

export default function Page() {
  return (
    <main className="app-shell">
      <header className="app-header">
        <p className="eyebrow">珠宝销售副驾</p>
        <h1>销售决策卡</h1>
        <p>整理客户问题，生成仅供人工复制的专业回复。</p>
        <p>Reply Agent 的评价仅用于人工复核后的优化分析，不会自动发送、自动改写或自动回写客户系统。</p>
        <nav aria-label="系统入口">
          <a href="/employee-guide">员工操作说明</a>
          <a href="/owner">进入老板后台</a>
        </nav>
        <BootstrapOwnerButton />
      </header>
      <DecisionCardForm />
    </main>
  );
}
