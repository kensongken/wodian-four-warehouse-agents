const STEPS = [
  {
    title: "接待客户",
    description: "在微信里正常接待；不承诺真伪、产地、品质、证书、固定价格或后续处理结果。",
  },
  {
    title: "生成决策卡",
    description: "填写商品描述和客户最后一句话。先选业务大类，再选具体场景；不确定时选“其他/不确定”。",
  },
  {
    title: "人工核验后发送",
    description: "若卡片提示核验，先把该单点交给负责人核对；确认后复制草稿，由员工自行发送。",
  },
  {
    title: "记录结果并复盘",
    description: "选择回复是否采用及客户当前结果，生成不含客户身份信息的试用记录，供负责人每周复盘。",
  },
] as const;

export default function EmployeeGuidePage() {
  return (
    <main className="app-shell employee-guide">
      <header className="app-header">
        <p className="eyebrow">员工上手包</p>
        <h1>员工操作说明</h1>
        <p>把决策卡变成每一位销售都能照着做的日常流程。</p>
      </header>

      <section className="guide-card" aria-label="销售决策卡每日流程">
        <h2>每天按这四步做</h2>
        <ol className="guide-steps">
          {STEPS.map((step, index) => (
            <li key={step.title}>
              <span aria-hidden="true">{index + 1}</span>
              <div>
                <h3>{step.title}</h3>
                <p>{step.description}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="guide-card">
        <h2>八大业务类</h2>
        <p>商品咨询与购买、订单与交付、定制与调整、瑕疵与争议、售后与养护、身份珠与扫码、寄售与送回、客户跟进与复购</p>
      </section>

      <section className="guide-card">
        <h2>遇到这些问题，先核验</h2>
        <p>真伪、材质、产地、品质、证书、具体价格、保值，以及送回后的处理结果，都不能由员工自行承诺。</p>
        <p className="guide-quote">“我先把您关心的这一点和现有资料整理好，核对后给您明确答复；您可以先把相关图片或页面截图发我。”</p>
      </section>

      <section className="guide-card">
        <h2>负责人每周只看三件事</h2>
        <ul>
          <li>什么业务类咨询最多：购买、订单、定制、售后、扫码、寄售还是复购；</li>
          <li>哪些问题总要人工核验：这就是资料库和培训缺口；</li>
          <li>哪些安全话术带来了后续跟进或成交：沉淀成团队范例。</li>
        </ul>
      </section>

      <section className="guide-card">
        <h2>Reply Agent 评价怎么用</h2>
        <p>评价只进入人工复核的优化池；员工确认后再提交，系统不会自动发送、自动改写或自动回写 CRM。</p>
      </section>

      <a className="guide-start" href="/">开始生成决策卡</a>
    </main>
  );
}
