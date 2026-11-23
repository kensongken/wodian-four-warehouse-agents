import{preferenceFields,preferenceLabel}from'./customer-preferences.js';
import{rehearsalQuestion}from'./rehearsal-scenarios.js';
import{duelView}from'./customer-duel.js';
import {coachView,coachCaseOptions} from './coach-lab.js';
import {knowledgeView,readKnowledgeTextFile} from './knowledge-library.js';
import {studioView,sampleFields,studioProfileInput} from './persona-studio.js';
let studioCustomerId='',studioDirty=false,knowledgeFileRead=0,studioReviewId=null,coachRunId=null,duelRunId=null;
const app = document.querySelector('#app');
const dialog = document.querySelector('#dialog');
let localPreview, localOCRImage='', localOCRRequest, windowCapture, windowRegion, cropDrag;
let qwenRun;
let state, view = 'workbench', selected, filter = 'all', preview, setupMode = false, loading = false;
const E = (value = '') => String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const names = { discover: '发现需求', explain: '介绍说明', evaluate: '比较评估', advance: '推进意向', follow_up: '持续跟进', ongoing: '进行中', pending_sale: '待核实成交', won: '已核实成交', paused: '暂缓', lost: '流失' };
const statuses = { official_api_accepted:'官方接口已受理', draft: '待审核', reviewed: '已核对文字', manual_marked: '人工标记已发', simulated: '演练完成 · 未发微信', observed_sent: '本机观察到已发', queued: '任务已创建 · 尚未发', executing: '设备执行中', failed: '执行失败', unknown: '结果未知 · 待核验', human_resolved: '已人工核验', canceled: '任务已取消' };
const sourceNames = { official_kf_test:'官方微信客服 · 本人测试原话',official_kf_api_accepted:'官方微信客服 · 接口已受理，手机可见待确认', local_ocr_confirmed:'本机截图 · 人工核对', manual: '人工录入', synthetic: '演练消息', device: '设备观察', manual_attestation: '人工标记 · 无通道回执', simulation_not_sent: '模拟回复 · 未发送', device_observed: '本机已观察发送' };
const badge = (text, color = '') => `<span class="badge ${color}">${E(text)}</span>`;
const date = (value) => new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const empty = (title, text, action = '') => `<div class="empty"><div class="empty-icon">◌</div><h3>${E(title)}</h3><p>${E(text)}</p>${action}</div>`;
const button = (text, action, attrs = '', cls = '') => `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${text}</button>`;
const digest = async (text) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))].map((x) => x.toString(16).padStart(2,'0')).join('');
async function api(path, body, options = {}) {
  const response = await fetch(path, { ...options, ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
  const output = await response.json();
  if (!response.ok) { if (response.status === 401 && state) { state = null; auth(false); } throw new Error(output.error || '处理失败，请重试。'); }
  return output;
}
let toastTimer;
function toast(text, error = false) {
  const el = document.querySelector('#toast'); el.textContent = text; el.className = `show ${error ? 'error' : ''}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = '', 6500);
}
function modal(title, body) { dialog.innerHTML = `<div class="modal-head"><h2>${E(title)}</h2>${button('✕', 'close', 'aria-label="关闭对话框"', 'quiet')}</div>${body}`; if (!dialog.open) dialog.showModal(); }
function passwordForm(){return '<form data-form="password"><label>原密码<input name="oldPassword" type="password" required autocomplete="current-password"></label><label>新密码<input name="newPassword" type="password" required minlength="10" autocomplete="new-password"></label><label>再次输入新密码<input name="confirmPassword" type="password" required minlength="10" autocomplete="new-password"></label><button class="primary">修改密码并重新登录</button></form>';}
function passwordPage(){app.innerHTML='<div class="auth"><div class="auth-card"><h1>请先修改初始密码</h1><p class="muted">设置自己的密码后，再进入客户工作台。</p>'+passwordForm()+'</div></div>';}
function auth(setup) {
  setupMode = setup;
  app.innerHTML = `<div class="auth"><div class="auth-card"><div class="eyebrow">SALES AVATAR / LOCAL</div><h1>微信销售分身</h1><p class="muted">把每次对话，接成下一步。</p><form data-form="auth">${setup ? '<label>你的称呼<input name="name" required maxlength="40" autocomplete="name" placeholder="例如：店铺负责人"></label>' : ''}<label>账号<input name="username" required maxlength="50" autocomplete="username" placeholder="${setup ? '创建老板账号' : '输入你的账号'}"></label><label>密码<input name="password" type="password" required minlength="10" maxlength="256" autocomplete="${setup ? 'new-password' : 'current-password'}" placeholder="至少10位"></label><button class="primary">${setup ? '创建本机工作空间' : '进入工作台'}</button></form><p class="auth-footer">资料保存在这台电脑的加密账本中。首次使用默认未连接微信，自动发送关闭。</p></div></div>`;
}
async function refresh() {
  state = await api('/api/state');
  if (state.mustChangePassword) { passwordPage(); return; }
  if(studioCustomerId&&!state.customers.some(c=>c.id===studioCustomerId)){studioCustomerId='';studioReviewId=null;}
  if (!state.customers.some((c) => c.id === selected)) selected = state.customers[0]?.id;
  render();
}
function render() {
  if (!state) return;
  studioDirty=false;
  const owner = state.actor.role === 'owner';
  const nav = [['workbench','◫','客户工作台'], ['reminders','◷','跟进与接管'], ['dashboard','▥','经营概览'], ['materials','▤','商品与教材'], ...(owner ? [['knowledge','▧','资料录入'],['studio','✳','店主分身'],['coach','◇','店主带教'],['duel','⇄','客户对练'],['personas','♧','口吻版本'],['settings','⚙','设备与设置']] : [])];
  const titles = { workbench: '客户工作台', reminders: '跟进与接管', dashboard: '经营概览', materials: '商品与教材', knowledge:'资料录入', studio:'店主分身',coach:'店主带教',duel:'客户对练', personas: '口吻版本', settings: '设备与设置' };
  app.innerHTML = `<div class="shell"><aside class="sidebar"><div class="brand"><span class="brand-icon">✳</span><div><strong>销售分身</strong><small>SALES AVATAR</small></div></div><div class="nav-label">日常工作</div>${nav.map(([key,icon,title]) => `<button data-action="nav" data-view="${key}" class="nav-item ${view === key ? 'active' : ''}"><span class="nav-symbol">${icon}</span>${title}</button>`).join('')}<div class="sidebar-bottom"><div class="user-row"><span class="avatar">${E(state.actor.name.slice(0,1))}</span><div>${E(state.actor.name)}<br><small>${owner ? '老板工作空间' : '员工工作空间'}</small></div></div><p class="tagline">本机版 V2.0 · 资料加密保存</p>${button('退出登录', 'logout', '', 'quiet')}</div></aside><main class="content"><header class="topbar"><div><p class="eyebrow">WORKSPACE / ${new Date().toLocaleDateString('zh-CN')}</p><h1>${titles[view]}</h1><p class="tagline">${view === 'workbench' ? '先理解客户，再推进一步。' : '每一步有记录，每一次处理可回看。'}</p></div><div class="top-actions">${badge(state.settings.paused ? (state.pauseAcknowledged ? '全局已暂停' : '等待暂停确认') : ({ observe: '观察模式', assist: '辅助模式', controlled: '受控自动模式' })[state.settings.mode], state.settings.mode === 'controlled' ? '' : 'gray')}${owner ? button(state.settings.paused ? '恢复工作台' : '暂停自动处理', 'toggle-pause', '', state.settings.paused ? '' : 'danger') : ''}${button('＋ 新建客户', 'new-customer', '', 'primary')}${button('账号', 'account', '', 'small')}</div></header>${connectionNotice()}<div id="view">${({ workbench, reminders, dashboard, materials, personas, knowledge:()=>knowledgeView(state), studio:()=>studioView(state,studioReviewId,studioCustomerId), coach:()=>coachView(state,coachRunId),duel:()=>duelView(state,duelRunId), settings })[view]()}</div></main></div>`;
}
function connectionNotice() {
  if (state.settings.paused && !state.pauseAcknowledged) return '<div class="notice"><div><strong>暂停请求已提交，等待执行设备确认</strong>　新的发送任务已阻止；设备离线或执行结果不明时，需人工核验。</div></div>';
  if(state.officialKf?.running&&state.officialKf.observationOnly)return '<div class="notice"><span>◉</span><div><strong>仅观察本人微信消息</strong>　同步到客户档案与跟进待办，不调用模型、不发送回复；原人工锁继续保留。</div></div>';
  if(state.officialKf?.humanLocked)return '<div class="notice"><span>◉</span><div><strong>本人微信客服测试已转人工</strong>　自动问候已锁定，请核对下方待处理记录。真实客户接待仍未开放。</div></div>';
  if(state.officialKf?.running)return '<div class="notice success"><span>●</span><div><strong>官方客服本人测试运行中</strong>　仅向已绑定的本人微信自动回复已开启的低风险测试内容，真实客户接待未开放。下方客户档案仍按工作台辅助模式处理。</div></div>';
  if (state.settings.mode === 'controlled' && state.connectionReady && !state.settings.paused) return '<div class="notice success"><span>●</span><div><strong>受控自动模式已启用</strong>　仅白名单问题可交设备处理。价格、承诺和售后始终转人工。</div></div>';
  return `<div class="notice"><span>◉</span><div><strong>当前未启用微信自动发送</strong>　客户跟进、草稿审核和演练可使用。真实微信接入仍需已配置的视觉服务与经过验收的本机执行端。</div></div>`;
}
function counts() {
  return { clients: state.customers.length, handoffs: state.customers.filter((c) => c.locked && (!c.synthetic || c.ownerTest)).length, overdue: state.reminders.filter((r) => r.status === 'open' && r.due < Date.now()).length, observed: state.tasks.filter((t) => t.status === 'observed_sent').length };
}
function statCards() {
  const c = counts(); return `<div class="stats">${[[c.clients,'客户档案','包含明确标记的演练客户'],[c.handoffs,'待人工接续','报价、承诺、售后优先'],[c.overdue,'已到期待办','回到客户空间继续处理'],[c.observed,'本机观察发送','不含复制、人工标记与演练']].map(([n,title,note]) => `<div class="stat"><span>${title}</span><strong>${n}</strong><small>${note}</small></div>`).join('')}</div>`;
}
function workbench() {
  const c = state.customers.find((c) => c.id === selected);
  const list = state.customers.filter((c) => filter === 'all' || (filter === 'handoff' ? c.locked : c.synthetic && !c.ownerTest));
  return `${statCards()}<div class="workbench"><section class="panel"><div class="panel-head"><h2>客户队列 <span class="muted">${state.customers.length}</span></h2><select aria-label="筛选客户" class="switcher" id="queue-filter"><option value="all" ${filter === 'all' ? 'selected' : ''}>全部</option><option value="handoff" ${filter === 'handoff' ? 'selected' : ''}>人工接管</option><option value="synthetic" ${filter === 'synthetic' ? 'selected' : ''}>演练客户</option></select></div><div class="panel-body"><input id="customer-search" class="search" aria-label="搜索客户" placeholder="搜索客户称呼…"></div><div class="client-list">${list.map((c) => { const last = state.messages.filter((m) => m.customerId === c.id).at(-1); return `<button class="client-card ${c.id === selected ? 'active' : ''}" data-action="select" data-id="${c.id}" data-search="${E(c.alias)}"><div class="row"><strong>${E(c.alias)}</strong><span class="small-text muted">${date(c.updatedAt).slice(6)}</span></div><p>${E(last?.text || '还没有客户消息')}</p><div class="client-meta">${badge(c.ownerTest?'本人测试':c.synthetic ? '演练' : names[c.stage], c.synthetic ? 'gray' : '')}${c.locked ? badge('待人工', 'amber') : ''}</div></button>`; }).join('') || empty('先建立第一位客户','客户信息、回复和下一步会放在同一个空间里。',button('创建演练客户', 'demo', '', 'small'))}</div></section><section class="panel">${c ? customerChat(c) : empty('开启一次完整接待','可以先用演练客户，体验低风险回复和高风险转人工。',button('开始演练', 'demo', '', 'primary'))}</section>${c ? insights(c) : `<section class="panel insights"><div class="panel-body"><h3>一个客户，一个处理空间</h3><p class="tagline">客户原话 → 销售判断 → 本人口吻 → 人工或受控发送 → 跟进记录</p></div></section>`}</div>`;
}
function customerChat(c) {
  const messages = state.messages.filter((m) => m.customerId === c.id);
  const d = state.drafts.filter((d) => d.customerId === c.id).at(-1);
  return `<div class="chat-head"><span class="avatar">${E(c.alias.slice(0,1))}</span><div class="info"><h3>${E(c.alias)} ${c.ownerTest?badge('本人测试 · 官方客服','gray'):c.synthetic ? badge('演练客户','gray') : ''}</h3><p>${E(state.users.find((u) => u.id === c.assigneeId)?.name || '已分配员工')}负责 · ${c.officialKf?'已绑定官方客服本人测试':c.binding ? '已人工绑定会话' : '未绑定微信会话'}</p></div>${button(c.locked ? '确认恢复' : '人工接管', c.locked ? 'resume' : 'claim', `data-id="${c.id}"`, c.locked ? 'small' : 'small danger')}</div>${c.locked ? `<div class="notice"><strong>人工接管中</strong><span>${E(c.lockReason)}</span></div>` : ''}<div class="chat-stream" id="chat-stream">${messages.map((m) => `<div class="bubble-row ${m.direction === 'out' ? 'out' : ''}"><div><div class="bubble">${E(m.text)}</div><div class="bubble-source">${E(sourceNames[m.source] || m.source)} · ${date(m.at)}</div></div></div>`).join('') || empty('等待客户消息','粘贴客户原话，或到设备与设置准备本机接入。')}</div><form class="compose" data-form="message" data-id="${c.id}"><textarea name="text" required maxlength="2000" aria-label="客户最新消息" placeholder="粘贴客户刚刚说的话…"></textarea><div class="row"><span class="small-text muted">${c.ownerTest?'官方消息自动同步；此处手填内容标为演练，不冒充官方来源':c.synthetic ? '此客户的消息仅用于演练' : '人工录入，不等于微信实时同步'}</span><div class="row">${state.actor.role === 'owner' ? button('读取微信窗口','local-ocr-window',`data-id="${c.id}"`,'small') + button('本机截图识别','local-ocr',`data-id="${c.id}"`,'small') + (state.settings.visionProvider==='shiflow'?button('截图识别', 'vision', `data-id="${c.id}"`, 'small'):button('本机接入', 'desktop-settings', '', 'small')) : ''}<button class="primary small">录入并生成建议</button></div></div></form>${d ? draftCard(d,c) : ''}`;
}
function draftCard(d,c) {
  const done = ['simulated','manual_marked','observed_sent','official_api_accepted','unknown'].includes(d.status);
  return `<div class="reply-card"><div class="row wrap"><h3>✳ 销售回复建议</h3>${badge(d.risk === 'low' ? '低风险' : '需要人工',d.risk === 'low' ? '' : 'amber')}${badge(statuses[d.status] || d.status,'gray')}</div><p class="tagline">${E(d.intent)} · ${d.source==='official_kf_model_accepted'?'本机千问回复 · 官方接口已受理，手机可见待确认':d.source==='official_kf_rule_suggestion'?'官方消息的规则建议，待人工核对':d.source === 'model_pending_review' ? '模型草稿，待人工核对' : '已审核规则与口吻'}</p><textarea id="draft-text" ${done ? 'readonly' : ''} aria-label="回复草稿" maxlength="1000" placeholder="此问题请由负责人核实后填写回复。">${E(d.text)}</textarea><div class="reply-reason">下一步：${E(d.nextAction)}${d.reasons.length ? `<br>转人工原因：${E(d.reasons.join('、'))}` : ''}</div><div class="reply-actions">${done ? '' : button('保存修改','save-draft',`data-id="${d.id}"`,'small')}${done ? '' : button('核对文字并复制','review-copy',`data-id="${d.id}"`,'primary small')}${d.status === 'reviewed' ? button('我已在微信手动发送','manual-sent',`data-id="${d.id}"`,'small') : ''}${!done && c.synthetic && d.risk === 'low' ? button('模拟自动回复','simulate',`data-id="${d.id}"`,'small') : ''}${!done && state.actor.role === 'owner' ? button('让模型起草','model',`data-id="${d.id}"`,'small') : ''}${d.status==='official_api_accepted'?'':button('重新生成','generate',`data-id="${c.id}"`,'small')}</div>${c.synthetic ? '<p class="form-note">模拟不会调用微信，也不计入真实发送量。</p>' : ''}</div>`;
}
function insights(c) {
  const index = Object.keys(names).indexOf(c.stage);
  return `<aside class="panel insights"><div class="insight-block"><h3>这位客户的接待偏好</h3><p>${E(preferenceLabel(c.communicationPreferences))}</p><p class="form-note">保留店主底色，只调整说法；人工设置，不猜性格。</p>${button('设置接待偏好','customer-preferences',`data-id="${E(c.id)}"`,'small')}</div><div class="insight-block"><h3>销售之旅</h3><p><strong>${names[c.stage]}</strong>　${badge(names[c.result] || c.result,'gray')}</p><div class="step-list">${[0,1,2,3,4].map((i) => `<span class="step ${i <= index ? 'active' : ''}"></span>`).join('')}</div><p class="muted small-text">阶段与成交结果由人工确认</p><div class="spacer"></div>${button('更新阶段与结果','journey',`data-id="${c.id}"`,'small')}</div><div class="insight-block"><h3>已确认需求</h3><div class="chips">${c.needs.map((n) => `<span class="chip">${E(n.kind)}：${E(n.value)}</span>`).join('') || '<p class="muted">暂未确认</p>'}</div>${c.candidates.length ? '<div class="spacer"></div><h3>待确认的客户表达</h3>' : ''}${c.candidates.map((n) => `<div class="candidate">${E(n.kind)}：${E(n.value)}<div class="row"><span class="muted">来源：客户消息</span><div>${button('确认','need-accept',`data-id="${n.id}"`,'small')}${button('排除','need-reject',`data-id="${n.id}"`,'quiet small')}</div></div></div>`).join('')}</div><div class="insight-block"><h3>关联商品与资料</h3>${c.products.map((v) => { const m = state.materials.find((x) => x.id === v.id); return `<p>${E(m?.title || '资料已撤回')} ${badge(`V${v.version}`,'gray')}</p>`; }).join('') || '<p class="muted">未关联具体商品</p>'}<div class="spacer"></div>${button('关联已审核资料','link-material',`data-id="${c.id}"`,'small')}</div><div class="insight-block"><h3>下一次跟进</h3>${state.reminders.filter((r) => r.customerId === c.id && r.status === 'open').slice(-3).map((r) => `<p>${E(r.text)}<br><span class="muted small-text">${date(r.due)}</span></p>`).join('') || '<p class="muted">还没有待办</p>'}<div class="spacer"></div>${button('安排下一步','new-reminder',`data-id="${c.id}"`,'small')}</div><div class="insight-block"><h3>微信会话身份</h3><p class="muted">${c.officialKf?'官方客服本人测试绑定；仅用于已批准的本人测试':c.binding ? E(c.binding.name) : '未核对身份时，不允许自动发言。'}</p><div class="spacer"></div>${c.officialKf?'':button('核对并绑定','binding',`data-id="${c.id}"`,'small')}${state.actor.role === 'owner' ? button('转交负责人','assign',`data-id="${c.id}"`,'small') : ''}</div></aside>`;
}
function reminders() {
  const items = state.reminders.filter((r) => r.status === 'open').sort((a,b) => a.due - b.due);
  const unknown = state.tasks.filter((t) => t.status === 'unknown');
  return `${unknown.length ? `<div class="section-title"><h2>发送结果待核验</h2></div><div class="alert-list">${unknown.map((t) => `<div class="alert-item"><div><strong>${E(state.customers.find((c) => c.id === t.customerId)?.alias)}</strong><p>${t.simulation ? '演练结果未知，未调用真实微信' : '结果未知，请先检查微信实际内容，不能自动重发。'}</p></div>${button('记录核验结果','resolve-task',`data-id="${t.id}"`,'small')}</div>`).join('')}</div>` : ''}<div class="section-title"><h2>待接续事项</h2><span class="muted">${items.length}项</span></div><section class="panel">${items.length ? `<div class="table-wrap"><table><thead><tr><th>客户</th><th>要做什么</th><th>时间</th><th>操作</th></tr></thead><tbody>${items.map((r) => `<tr><td>${E(state.customers.find((c) => c.id === r.customerId)?.alias)}<br>${badge(r.type === 'handoff' ? '人工接管' : '跟进任务',r.type === 'handoff' ? 'amber' : '')}</td><td>${E(r.text)}<p class="sub">${E(r.reason || '员工已确认')}</p></td><td>${date(r.due)}<br>${r.due < Date.now() ? badge('已到期','red') : ''}</td><td><div class="row wrap">${button('进入客户','open-customer',`data-id="${r.customerId}"`,'small')}${button('完成','reminder-complete',`data-id="${r.id}"`,'small')}${button('延期','reminder-postpone',`data-id="${r.id}"`,'small')}${button('取消','reminder-cancel',`data-id="${r.id}"`,'quiet small')}</div></td></tr>`).join('')}</tbody></table></div>` : empty('今天没有待办','可以在客户处理页设置下一步和计划时间。')}</section>`;
}
function dashboard() {
  const logs = state.audit.slice(-15).reverse();
  const tasks = state.tasks;
  return `${statCards()}<div class="cards"><section class="panel"><div class="panel-head"><h2>处理结果分开统计</h2>${state.actor.role === 'owner' ? '<a class="small-text" href="/api/export">导出脱敏汇总</a>' : ''}</div><table><tbody>${[['本机观察到已发送',tasks.filter((t) => t.status === 'observed_sent').length],['演练完成，未发微信',tasks.filter((t) => t.status === 'simulated').length],['人工标记已发，无通道回执',state.drafts.filter((d) => d.status === 'manual_marked').length],['已核实成交客户',state.customers.filter((c) => c.result === 'won').length],['接口调用次数',state.costs.length]].map(([name,n]) => `<tr><td>${name}</td><td>${n}</td></tr>`).join('')}</tbody></table><div class="panel-body muted small-text">接口金额尚未配置单价，不显示虚构费用；演练、复制与消息量都不代表成交。</div></section><section class="panel"><div class="panel-head"><h2>最近处理记录</h2></div><div class="panel-body">${logs.map((l) => `<div class="log-line">${E(l.kind)}<small>${date(l.at)} · ${E(state.customers.find((c) => c.id === l.customerId)?.alias || '工作空间')}</small></div>`).join('') || empty('暂无操作','处理客户后，这里会显示可追溯记录。')}</div></section></div>`;
}
function materials() {
  return `<div class="section-title"><h2>已审核的专业资料</h2>${state.actor.role === 'owner' ? button('＋ 批准一份资料','new-material','','primary') : ''}</div><div class="notice blue"><div>资料由人工核实后发布，并记录依据与有效期。商品资料不会让报价、真伪或售后承诺自动放行。</div></div><div class="cards">${state.materials.map((m) => `<article class="card"><div class="row wrap"><h3>${E(m.title)}</h3>${badge(m.status === 'approved' && m.expires > Date.now() ? '已批准' : '已失效',m.status === 'approved' && m.expires > Date.now() ? '' : 'red')}</div><p>${E(m.body)}</p><div class="spacer"></div><p>商品或政策编号：${E(m.code)}<br>核实依据：${E(m.source)}<br>有效至：${date(m.expires)} · V${m.version}</p>${state.actor.role === 'owner' && m.status === 'approved' ? `<div class="row">${button('撤回资料','revoke-material',`data-id="${m.id}"`,'small danger')}</div>` : ''}</article>`).join('') || `<section class="panel">${empty('先建立可靠的商品资料','填写已核实的内容和依据后，再关联客户。')}</section>`}</div>`;
}
function personas() {
  const active = state.personas.find((p) => p.id === state.settings.personaId);
  return `<div class="section-title"><h2>让表达接近本人</h2>${button('建立新口吻版本','new-persona','','primary')}</div><div class="notice blue"><div>本人样本用于表达参考，不能把价格、库存或承诺学成永久事实。自动模式前，需要20题人工盲测记录；系统不会自己填写评测结果。</div></div><div class="cards">${state.personas.map((p) => `<article class="card"><div class="row wrap"><h3>${E(p.name)} · V${p.version}</h3>${p.id === active?.id ? badge('当前启用') : ''}${badge(p.verified ? '人工评测通过' : '待人工评测',p.verified ? '' : 'amber')}</div><p>常用称呼：${E(p.greeting)}<br>正式样本：${p.samples.length}条<br>${p.evaluation ? `人工记录：${p.evaluation.votes.filter(Boolean).length}/20题偏好个性化表达` : '尚未录入20题盲测'}</p><div class="comparison">${E(p.samples.slice(0,3).join('\n'))}</div><div class="row wrap">${button('录入20题评测','evaluate',`data-id="${p.id}"`,'small')}${p.id !== active?.id ? button('启用此版本','activate-persona',`data-id="${p.id}"`,'small') : ''}</div></article>`).join('') || `<section class="panel">${empty('从几句真实表达开始','导入之前先确认：这些话确实由本人说过，并已获得使用授权。')}</section>`}</div><div class="section-title"><h2>待学习区</h2><span class="muted">仅人工最终采用的表达进入候选</span></div><section class="panel"><div class="panel-body">${state.learning.map((l) => `<div class="log-line">${l.status === 'quarantined' ? '已隔离，原文未保存' : E(l.text)}<div class="row wrap">${badge(({ pending:'待审核',quarantined:'已隔离',accepted:'已纳入新版本',rejected:'已排除' })[l.status],'gray')}${l.status === 'pending' ? button('核对并记住','learn-accept',`data-id="${l.id}"`,'small') + button('丢弃','learn-reject',`data-id="${l.id}"`,'quiet small') : ''}</div></div>`).join('') || empty('暂无学习候选','人工标记已发送的文字先检查，再进入此处；模拟回复不会自动学进去。')}</div></section>`;
}
function officialKfPanel(){
  const k=state.officialKf;if(!k)return '';
  const labels={limited:'测试额度用尽',processing:'处理中',observed:'已同步 · 未发送',human:'转人工',canceled:'已取消',sending:'发送中',unknown:'结果待核实',api_accepted:'微信接口已受理'};
  return `<section class="panel"><div class="panel-head"><h2>官方微信客服 · 本机千问</h2>${badge(k.running&&k.observationOnly?'本人消息观察中':k.humanLocked?'人工接管锁定':k.running?(k.discovery?'需求自动接待中':'问候测试接待中'):'已暂停',k.humanLocked?'amber':k.running?'':'gray')}</div>${k.sessionCheckMode==='owner_exclusive_test'?'<div class="notice">本人独占测试：无法查询官方人工接待状态，限本人微信；出现非客户消息或异常会话事件即暂停。</div>':''}<div class="panel-body"><p>仅接待已绑定的本人测试微信。可启动问候测试，或选择模型自动了解需求；需求自动接待不设回复条数或固定轮数上限；原问候测试48小时最多3条。报价、承诺、售后与范围外问题转人工。这台Mac需保持开机，重启后不会自行恢复。</p>${k.quota?`<p><strong>${k.quota.period==='unlimited'?`无条数上限 · 已受理 ${k.quota.used} 条`:`${k.quota.period==='taipei_day'?'今日（台北时间）':'最近48小时'}已用 ${k.quota.used} / ${k.quota.limit} 条，剩余 ${k.quota.remaining} 条`}</strong></p>`:''}<p class="form-note">官方客服入口与个人微信好友聊天不同。接口受理后仍需在手机确认可见。</p>${k.lastError?`<div class="notice">${E(k.lastError)}</div>`:''}<div class="row wrap">${button('仅观察本人消息，不发送','official-kf-observe')}${button('启动需求自动接待','official-kf-discovery','','primary')}${button('启动本人测试接待','official-kf-start')}${button('立即暂停客服','official-kf-stop','','danger')}${button('刷新接待记录','official-kf-refresh')}${button('解除本人测试人工锁','official-kf-resume','','quiet')}</div>${k.receipts.length?`<table><thead><tr><th>时间</th><th>处理结果</th><th>说明</th></tr></thead><tbody>${k.receipts.slice().reverse().map(r=>`<tr><td>${date(r.at)}</td><td>${E(labels[r.status]||r.status)}</td><td>${E(r.reason||'')}${r.customerId?button('打开客户档案','open-customer',`data-id="${E(r.customerId)}"`,'small'):''}${r.text?`<p>${E(r.text)}</p>`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">尚未处理新的本人测试消息。启动后再发“你好”。</p>'}</div></section>`;
}
function settings() {
  const s = state.settings;
  return `<section class="panel"><div class="panel-head"><h2>本机模型连聊加速</h2></div><div class="panel-body"><form data-form="local-model-runtime" data-version="${s.localModelRuntimeVersion||0}"><div class="checks"><label><input name="warm" type="checkbox" ${s.localModelWarm?'checked':''}>开启连续接待加速，模型短时留在内存</label></div><p class="form-note">连续聊天更快，首条冷启动仍可能慢。模型会短时占用内存，空闲后请求保留约2分钟；关闭后新请求不再保留，已有缓存按原到期时间释放，不强制卸载其他任务使用的模型。</p><button>保存加速设置</button></form></div></section>${officialKfPanel()}${desktopPanel()}<div class="cards"><section class="card"><h3>运行状态与接入检查</h3><p>仅所有条件通过，才允许启用受控自动回复。</p><ul class="readiness">${state.blockers.map((b) => `<li>○ ${E(b)}</li>`).join('') || '<li>✓ 接入条件已满足，仍按会话逐条检查。</li>'}</ul><form data-form="mode"><label>运行模式<select name="mode"><option value="observe" ${s.mode === 'observe' ? 'selected' : ''}>观察：只生成候选</option><option value="assist" ${s.mode === 'assist' ? 'selected' : ''}>辅助：人工审核与复制</option><option value="controlled" ${s.mode === 'controlled' ? 'selected' : ''}>受控自动：需要真机验收</option></select></label><label>当前执行设备<select name="deviceId"><option value="">尚未选择</option>${state.devices.filter((d) => !d.revoked).map((d) => `<option value="${d.id}" ${s.deviceId === d.id ? 'selected' : ''}>${E(d.name)} · ${d.kind === 'real' ? '真实设备' : '模拟设备'}</option>`).join('')}</select></label><button class="primary">保存运行策略</button></form></section><section class="card"><h3>识流接口 可选兼容</h3><p>如已有识流企业服务，可保留原有配置；启用前需核实当前接口及截图处理规则。密钥加密保存，不回传到页面。</p><div class="spacer"></div><form data-form="vision-settings"><label>已确认的完整识别接口地址<input name="visionEndpoint" type="url" value="${E(s.visionEndpoint)}" placeholder="https://供应商提供的地址/…"></label><label>接口密钥 ${s.visionKeySet ? '· 已配置，留空保留' : '· 尚未配置'}<input name="visionKey" type="password" autocomplete="new-password" placeholder="仅在本机设置页填写"></label><div class="checks"><label><input name="supplierConfirmed" type="checkbox" ${s.supplierConfirmed ? 'checked' : ''}>已确认企业服务持续提供及当前接口合同</label><label><input name="dataConsent" type="checkbox" ${s.dataConsent ? 'checked' : ''}>已确认截图、对话用途及供应商保存规则</label></div><button class="primary">保存识流配置</button></form></section><section class="card"><h3>模型草稿接口</h3><p>支持返回choices消息结构的兼容接口。仅用于人工草稿，Codex/企业API限定GPT-5.6；国产本机通道固定千问，不自动切换。</p><div class="spacer"></div><form data-form="model-settings"><label>模型接入方式<select name="modelProvider"><option value="qwen_local" ${s.modelProvider === 'qwen_local' ? 'selected' : ''}>国产本机千问：不上传云端</option><option value="api" ${(!s.modelProvider || s.modelProvider === 'api') ? 'selected' : ''}>企业API：填写接口地址和密钥</option><option value="codex_cli" ${s.modelProvider === 'codex_cli' ? 'selected' : ''}>本机Codex：复用已登录账户</option></select></label><p class="form-note">国产本机千问使用已安装的 qwen3-vl:4b-instruct，仅本机Ollama推理，不用API密钥。本机Codex方式也无需填写下面的API地址和密钥，使用当前电脑的账户额度。仅适用于装有并已登录Codex的Mac；原有API配置可保留。</p><label>完整生成接口地址<input name="modelEndpoint" type="url" value="${E(s.modelEndpoint)}" placeholder="https://模型服务地址/…"></label><label>模型密钥 ${s.modelKeySet ? '· 已配置' : '· 尚未配置'}<input name="modelKey" type="password" autocomplete="new-password"></label><label>Codex/企业API模型名称<input name="model" value="${E(s.model)}" pattern="gpt-5\.6(-[a-z0-9-]+)?"></label><div class="checks"><label><input name="modelDataConsent" type="checkbox" ${s.modelDataConsent ? 'checked' : ''}>允许将当前客户最近12条文字、已确认需求、口吻样本及已审核关联资料交给所选模型通道起草</label></div><p class="form-note">切换模型通道后，此授权会清除，需重新核对。此授权独立于截图服务。撤回后停止新的草稿调用，尚未完成的结果也会丢弃。</p><button>保存模型配置</button></form><div class="spacer"></div><p class="form-note">连接测试只发送内置演练文字，不读取客户记录；可能消耗接口额度。请先保存配置。</p>${button('用演练文字测试连接','model-test')}</section><section class="card"><h3>店铺信息与接待限制</h3><form data-form="policy-settings"><label>店铺名称<input name="storeName" value="${E(s.storeName)}" maxlength="200"></label><label>已核实店址<input name="address" value="${E(s.address)}" maxlength="200"></label><label>已核实营业时间<input name="openingHours" value="${E(s.openingHours)}" maxlength="200"></label><div class="checks"><label><input name="infoApproved" type="checkbox" ${s.infoApproved ? 'checked' : ''}>上述基础信息已由我核实并批准</label></div><div class="form-grid">${[['hoursStart','自动接待开始时',0,23],['hoursEnd','自动接待结束时',0,23],['perMinute','每会话每分钟上限',1,10],['maxRounds','连续自动轮数上限',1,10],['dailyLimit','每日任务／接口硬上限',1,10000],['customerRetentionDays','客户主档保存天数',1,365]].map(([k,title,min,max]) => `<label>${title}<input name="${k}" type="number" min="${min}" max="${max}" value="${s[k]}"></label>`).join('')}</div><button>发布基础信息与规则</button></form></section></div><div class="section-title"><h2>执行设备</h2>${button('注册设备','new-device','','primary')}</div><section class="panel"><div class="panel-body">${state.devices.map((d) => `<div class="alert-item"><div><strong>${E(d.name)}</strong><p>${d.kind === 'real' ? '真实执行器' : '模拟设备'} · ${d.revoked ? '已撤销' : d.verified ? '已记录真机验收' : '未验收'} · ${Date.now() - d.lastSeen < 15000 ? '设备在线' : '设备离线'}</p></div><div class="row">${!d.revoked && d.kind === 'real' && !d.verified ? button('记录真机验收','verify-device',`data-id="${d.id}"`,'small') : ''}${!d.revoked ? button('撤销设备','revoke-device',`data-id="${d.id}"`,'small danger') : ''}</div></div>`).join('') || '<p class="muted">尚未注册。已提供本机执行端；需完成窗口映射和真机验收后才能自动回复。</p>'}</div></section><div class="section-title"><h2>员工与资料管理</h2><div class="row">${button('创建员工账号','new-user')}${button('执行到期资料清理','cleanup','','danger')}</div></div><section class="panel"><div class="panel-body"><div class="chips">${state.users.map((u) => `<span class="chip">${E(u.name)} · ${u.role === 'owner' ? '老板' : '员工'}${u.role === 'employee' ? button('重置密码','reset-user',`data-id="${u.id}"`,'quiet small') + button('停用','disable-user',`data-id="${u.id}"`,'quiet small') : ''}</span>`).join('')}</div><p class="form-note">原始消息与草稿最多保留30天。客户主档按上方期限清理。加密备份需连同本机钥匙妥善保存，当前版本不自动上传云端。</p></div></section>`;
}

async function action(action, el) {
  if(action==='duel-start'){const started=await api('/api/duel/start',{scenarioId:document.querySelector('#duel-scenario').value,simulationConfirmed:true,maxTurns:8});duelRunId=started.id;await refresh();return;}
  if(action==='duel-show'){duelRunId=el.dataset.id;render();return;}
  if(action==='duel-stop'){await api('/api/duel/stop',{});await refresh();return;}
  if(action==='coach-show'){coachRunId=el.dataset.id;render();document.querySelector('.coach-comparison')?.scrollIntoView({block:'center'});return;}
  if(action==='coach-stop'){await api('/api/coach/stop',{});toast('已请求停止，不会自动重试。');return;}
  if(action==='coach-withdraw'){await api('/api/coach/withdraw',{id:el.dataset.id});await refresh();return;}
  if(action==='knowledge-gap-prepare'){
    const gap=state.knowledgeGaps?.basics.find(b=>b.key===el.dataset.key&&b.status==='missing');
    if(!gap)throw new Error('这项资料状态已变化，请刷新后核对。');
    const form=document.querySelector('[data-form=knowledge-import]');if(!form)return;
    if(['title','source','text'].some(name=>form.elements.namedItem(name).value.trim())||form.elements.namedItem('textFile').files?.length)throw new Error('当前有未整理的资料，请先处理，避免覆盖输入。');
    form.elements.namedItem('title').value='门店问答 · '+gap.label;form.elements.namedItem('kind').value='faq';
    form.elements.namedItem('text').value='问题：【演练问法】'+gap.question+'\n回答：';
    form.elements.namedItem('confirmed').checked=false;form.elements.namedItem('autoOrganize').disabled=false;form.elements.namedItem('autoOrganize').checked=false;
    form.querySelector('#knowledge-status').textContent='已准备问题，请填真实回答和来源依据；尚未保存，也没有批准。';
    form.scrollIntoView({block:'start'});form.elements.namedItem('text').focus();return;
  }
  if(['knowledge-reject','knowledge-withdraw'].includes(action)){await api('/api/knowledge/review',{id:el.dataset.id,version:Number(el.dataset.version),action:action==='knowledge-reject'?'reject':'withdraw'});await refresh();return;}
  if(action==='studio-open-knowledge'){
    if(studioDirty)throw new Error('请先保存口吻设置，再去录入资料，避免丢失未保存内容。');
    view='knowledge';render();return;
  }
  if(action==='studio-add-sample'){
    const box=document.querySelector('#studio-samples');const count=box.querySelectorAll('fieldset').length;
    if(count>=12)throw new Error('最多保存12组表达，优先保留最能代表你的说法。');
    box.insertAdjacentHTML('beforeend',sampleFields({},count));studioDirty=true;return;
  }
  if(action==='studio-review'){
    if(studioDirty)throw new Error('请先保存左侧修改，再点评其他回复。');
    studioReviewId=el.dataset.id;studioCustomerId=state.personaStudio?.trials.find(t=>t.id===studioReviewId)?.customerId||'';render();document.querySelector('.studio-comparison')?.scrollIntoView({block:'center'});return;
  }
  if(action==='studio-rate'){
    if(studioDirty)throw new Error('请先保存左侧修改，再评价本轮试聊。');
    await api('/api/persona-studio/feedback',{id:el.dataset.id,rating:el.dataset.rating,version:state.personaStudio?.profile?.version});await refresh();return;
  }
  const id = el.dataset.id;
  if(action==='official-kf-observe'){await api('/api/official-kf/observe',{confirmed:true});await refresh();toast('已开始观察本人微信，新消息会进入客户档案；不发送回复。');return;}
  if(action==='official-kf-start'||action==='official-kf-discovery')return modal('启动本人微信客服测试',`<form data-form="official-kf-start" data-discovery="${action==='official-kf-discovery'}"><p>${action==='official-kf-discovery'?'模型自动回复问候与佩戴需求追问，不必逐句审核；仅使用本轮审核的问句，不编造商品事实。':'仅自动回复问候。'}将通过微信官方接口，仅向已绑定的本人测试微信发送；${action==='official-kf-discovery'?'不设回复条数和固定轮数上限':'48小时最多3条'}。报价、承诺和售后转人工。请在手机打开“美学智能客服”，启动后再发送“你好”。</p><label><input name="confirmed" type="checkbox" required>确认仅在本人的测试会话验证自动回复</label><div class="notice official-test-notice"><strong>会话状态权限不足时的本人独占测试</strong><p>此选项不能查询微信后台的人工接待状态。只允许绑定的本人微信测试，不适用于真实客户；测试期间请勿在其他客服后台人工回复。${action==='official-kf-discovery'?'本人需求接待不设回复条数或固定轮数上限；仍只处理本人新消息，异常或高风险问题暂停，不补发历史消息。':'48小时内最多受理3条本人测试回复，含已确认的单条诊断。'}</p><label><input name="exclusiveTestConfirmed" type="checkbox">我接受上述限制，明确允许会话状态接口返回48002时使用本人独占测试</label></div><button class="primary">启动测试接待</button></form>`);
  if(action==='official-kf-stop'){await api('/api/official-kf/stop',{});await refresh();toast('已暂停新回复；已提交微信的请求需核对结果。');return;}
  if(action==='official-kf-refresh'){await refresh();return;}
  if(action==='official-kf-resume'){await api('/api/official-kf/resume-test',{});await refresh();toast('本人的测试人工锁已清除，仍需手动启动。');return;}
  if (action === 'model-test') return modal('测试模型连接',`<form data-form="model-test" data-version="${state.settings.policyVersion}"><p>将通过已保存的模型通道发送一段内置演练问候，不包含客户聊天、资料或截图。</p><p>测试模型：${E(state.settings.modelProvider==='qwen_local' ? 'qwen3-vl:4b-instruct' : state.settings.model)}；通道：${E(state.settings.modelProvider==='qwen_local' ? '国产千问 · 本机Ollama' : state.settings.modelProvider==='codex_cli' ? '本机已登录的Codex' : state.settings.modelEndpoint || '尚未配置')}。</p><div class="checks"><label><input type="checkbox" name="confirmed" required>同意发送内置演练文字，了解本次可能消耗接口额度</label></div><button class="primary">发送演练文字并测试</button></form>`);
  if (action === 'close') {clearLocalOCRState();dialog.close();return;}
  if(action==='local-ocr-window'){clearLocalOCRState();const c=state.customers.find(c=>c.id===id);return modal('读取一次微信窗口',`<form data-form="local-ocr-window" data-id="${id}"><div class="notice blue">先在微信打开授权单聊或本人的文件传输助手，再点击读取。只取一张当前微信窗口截图，不识别、不导入、不发送。</div><label>目标客户档案<input name="customerName" readonly value="${E(c.alias)}"></label><div class="checks"><label><input name="confirmed" type="checkbox" required>已打开授权单聊或本人测试会话，稍后还会核对客户；不处理群聊</label></div><p class="form-note">微信主窗口可以留在后台，但请勿最小化或关闭。截图只在本机内存临时保留，读取后可框选文字区域。</p><button class="primary">读取当前窗口，不发送</button></form>`);}
  if(action==='clear-window-region'){windowRegion=null;updateWindowRegion({x:0,y:0,width:0,height:0});return;}
  if(action==='local-ocr'){clearLocalOCRState();localPreview=null;localOCRImage='';localOCRRequest=null;return modal('本机识别聊天截图',`<form data-form="local-ocr" data-id="${id}"><p class="form-note">使用Mac本机文字识别，不需要API密钥。请只截授权客户的当前对话；识别后逐条核对，不自动导入或发送。</p><label>客户对话PNG截图<input name="image" type="file" accept="image/png" required></label><p class="form-note">支持2MB以内PNG，最长边不超过4000像素。图片只在本机内存处理，不保存到资料库。</p><button class="primary">本机识别，不上传</button></form>`);}
  if (action === 'desktop-settings') { view='settings'; render(); return; }
  if (action === 'desktop-status') { const status=await api('/api/desktop/status',{}); modal('本机接入条件', '<div class="gap">'+Object.entries({prepared:'原生组件已编译',wechatInstalled:'桌面微信安装入口可用',wechatRunning:'桌面微信正在运行',accessibility:'辅助功能权限',screenRecording:'屏幕录制权限',inputMonitoring:'人工活动监控权限',running:'本机执行端已启动'}).map(([k,label])=>'<p>'+E(label)+'：'+badge(status[k]?'已满足':'待准备',status[k]?'':'amber')+'</p>').join('')+'<p class="form-note">检查本机条件不代表真机验收通过，也不会上传截图或发送消息。</p></div>');return;}
  if (action === 'desktop-probe') { const data=await api('/api/desktop/probe',{}); modal('本机微信窗口预览','<p class="form-note">仅在本机读取已打开的微信主窗口，不抢占前台、不上传视觉服务、不输入或发送。</p><p>读取控件：'+E(data.diagnostic.elementCount)+'；带标识控件：'+E(data.diagnostic.identifiedControls)+'；可识别输入框：'+E(data.diagnostic.editableInputs)+'。</p>'+(!data.diagnostic.editableInputs?'<div class="notice">未读到可识别的聊天输入框，保持人工辅助；不能据此开启自动回复。</div>':'')+'<textarea readonly aria-label="原生控件预览">'+E(JSON.stringify(data.nodes,null,2))+'</textarea>');return;}
  if (action === 'desktop-start' || action === 'desktop-stop') { await api('/api/desktop/'+(action === 'desktop-start'?'start':'stop'),{}); await refresh(); toast(action === 'desktop-start'?'执行端已启动，当前模式仍受工作台策略控制。':'执行端已停止，旧任务已取消或转待核验。'); return;}
  if (action === 'tars-test') return modal('测试UI-TARS输入框定位', '<form data-form="tars-test"><p class="form-note">只上传你选择的授权测试截图，返回输入框定位候选。不点击、不输入、不发微信。</p><label>授权的PNG测试截图<input name="image" type="file" accept="image/png" required></label><div class="footer"><button class="primary">调用已配置视觉服务</button></div></form>');
  if (action === 'account') return modal('个人账号', passwordForm() + '<div class="footer">' + button('退出登录','logout') + '</div>');
  if (action === 'nav') { view = el.dataset.view; document.querySelector('#toast').className=''; clearTimeout(toastTimer); render(); return; }
  if (action === 'select' || action === 'open-customer') { selected = id; view = 'workbench'; render(); document.querySelector('#chat-stream')?.scrollTo(0,100000); return; }
  if (action === 'logout') { await api('/api/logout',{}); dialog.close(); state = null; auth(false); return; }
  if(action==='customer-preferences'){const c=state.customers.find(c=>c.id===id);return modal('设置这位客户的接待偏好',`<form data-form="customer-preferences" data-id="${E(c.id)}" data-version="${c.version}"><p>客户：${E(c.alias)}。这些设置只影响这位客户，不改变店主本人资料，不开放微信发送。</p>${preferenceFields(c.communicationPreferences,E)}<button class="primary">保存这位客户的偏好</button>${button('恢复店主默认','reset-customer-preferences',`data-id="${E(c.id)}" data-version="${c.version}"`,'quiet')}</form>`);}
  if(action==='reset-customer-preferences'){await api(`/api/customers/${id}`,{version:Number(el.dataset.version),communicationPreferences:null});dialog.close();await refresh();return;}
  if (action === 'new-customer') return modal('建立客户档案', `<form data-form="customer"><label>客户称呼或代号<input name="alias" required maxlength="60" placeholder="例如：客户A"></label><div class="checks"><label><input type="checkbox" name="synthetic">这是演练客户，消息不用于真实发送</label></div><div class="footer"><button class="primary">建立并进入客户</button></div></form>`);
  if (action === 'demo') {
    const c = await api('/api/customers',{ alias:'演练客户 · 选款咨询',synthetic:true }); selected = c.id;
    await api(`/api/customers/${c.id}/messages`,{text:'你好',synthetic:true,eventId:crypto.randomUUID()}); await api(`/api/customers/${c.id}/generate`,{});
    await refresh(); toast('已创建演练客户，没有连接微信。'); return;
  }
  if (action === 'toggle-pause') { await api('/api/settings',{version:state.settings.policyVersion,paused:!state.settings.paused}); await refresh(); toast(state.settings.paused ? (state.pauseAcknowledged ? '已收到设备暂停确认。' : '已阻止新任务，等待执行设备确认暂停。') : '已解除全局暂停，仍按当前模式运行。'); return; }
  if (action === 'claim' || action === 'resume') { await api(`/api/customers/${id}/handoff`,{action}); await refresh(); toast(action === 'claim' ? '已接管此会话。' : '已确认恢复，仍受店铺策略控制。'); return; }
  if (action === 'generate') { await api(`/api/customers/${id}/generate`,{}); await refresh(); toast('销售建议已更新。'); return; }
  if (['save-draft','review-copy','manual-sent'].includes(action)) {
    let draft = state.drafts.find((d) => d.id === id);
    const text = document.querySelector('#draft-text')?.value || draft.text;
    if (text !== draft.text) draft = await api(`/api/drafts/${id}`,{action:'edit',text,revision:draft.revision});
    if (action === 'save-draft') { await refresh(); toast('修改已保存，旧审核已失效。'); return; }
    if (action === 'review-copy') {
      draft = await api(`/api/drafts/${id}`,{action:'review',revision:draft.revision,textHash:await digest(draft.text)});
      try { await navigator.clipboard.writeText(draft.text); await api(`/api/drafts/${id}`,{action:'copied',revision:draft.revision}); toast('文字已审核并复制，请回微信核对联系人后发送。'); }
      catch { toast('文字已审核。剪贴板不可用，请选中文字手工复制。',true); }
    } else await api(`/api/drafts/${id}`,{action:'manual-sent',revision:draft.revision});
    await refresh(); return;
  }
  if (action === 'simulate') return modal('演练自动回复', `<p class="form-note">仅测试本机处理链路，不点击微信，不产生真实发送。</p><form data-form="simulate" data-id="${id}"><label>演练设备返回结果<select name="outcome"><option value="success">模拟成功</option><option value="failure">模拟失败</option><option value="unknown">点击后断线，结果未知</option></select></label><div class="footer"><button class="primary">运行演练</button></div></form>`);
  if (action === 'model') {
    const draft=state.drafts.find(d=>d.id===id);const editor=document.querySelector('[aria-label="回复草稿"]');
    if(editor&&draft&&editor.value!==draft.text)throw new Error('请先保存当前修改，再让模型起草，避免覆盖未保存文字。');
    if(state.settings.modelProvider!=='qwen_local'){await api(`/api/drafts/${id}/model`,{});await refresh();toast('模型草稿已生成，必须重新人工核对。');return;}
    const run={jobId:crypto.randomUUID(),draftId:id,started:Date.now()};qwenRun=run;
    modal('千问正在起草',`<p>本次仅生成待人工核对的草稿，不发送微信。</p><progress aria-label="千问起草进度"></progress><p id="qwen-wait" role="status">已等待0秒。冷启动可能需要数十秒，最长120秒。</p><p class="form-note">停止后保留已保存的原稿，不自动重试。</p>${button('停止起草','stop-model','','danger')}`);
    const timer=setInterval(()=>{const label=document.querySelector('#qwen-wait');if(label&&!run.stopAcknowledged)label.textContent=`已等待${Math.floor((Date.now()-run.started)/1000)}秒。冷启动可能需要数十秒，最长120秒。`;},1000);
    try{await api(`/api/drafts/${id}/model`,{jobId:run.jobId});qwenRun=null;dialog.close();await refresh();toast('千问草稿已生成，必须重新人工核对。');}
    catch(error){qwenRun=null;dialog.close();await refresh();if(error.message==='本次起草已停止，原稿未更改。')toast('已停止起草，原稿未更改。');else throw error;}
    finally{clearInterval(timer);if(qwenRun===run)qwenRun=null;}
    return;
  }
  if (action === 'need-accept' || action === 'need-reject') {
    const c = state.customers.find((c) => c.id === selected); await api(`/api/customers/${c.id}`,{version:c.version,candidateId:id,accept:action === 'need-accept'}); await refresh(); return;
  }
  if (action === 'journey') {
    const c = state.customers.find((c) => c.id === id);
    return modal('确认销售之旅',`<form data-form="journey" data-id="${id}"><label>当前阶段<select name="stage">${Object.entries(names).slice(0,5).map(([k,v])=>`<option value="${k}" ${c.stage === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label><label>机会结果<select name="result">${Object.entries(names).slice(5).map(([k,v])=>`<option value="${k}" ${c.result === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label><label>成交核实依据或流失原因<input name="resultEvidence" maxlength="300" placeholder="标记成交、流失时必填"></label><p class="form-note">聊天里说“已付款”或一张付款截图，不能自动确认成交。</p><div class="footer"><button class="primary">确认业务记录</button></div></form>`);
  }
  if (action === 'new-reminder') return modal('安排下一步跟进',`<form data-form="reminder" data-id="${id}"><label>下一步做什么<textarea name="text" required maxlength="300" placeholder="例如：核实尺寸资料后回复客户"></textarea></label><label>计划时间<input name="due" type="datetime-local" required></label><p class="form-note">此操作只创建待办，不会主动发送微信。</p><div class="footer"><button class="primary">确认跟进任务</button></div></form>`);
  if (action.startsWith('reminder-')) {
    const r = state.reminders.find((r)=>r.id === id); const kind = action.slice(9);
    if (kind === 'postpone') return modal('调整跟进时间',`<form data-form="postpone" data-id="${id}"><label>新的计划时间<input name="due" type="datetime-local" required></label><div class="footer"><button class="primary">保存新时间</button></div></form>`);
    await api(`/api/reminders/${id}`,{action:kind,version:r.version}); await refresh(); return;
  }
  if (action === 'resolve-task') return modal('人工核验发送结果',`<form data-form="resolve" data-id="${id}"><label>检查实际内容后选择<select name="result"><option value="not_sent">核实未发出</option><option value="sent">核实已发出</option></select></label><label>核验依据<textarea name="reason" required maxlength="300" placeholder="说明查看了哪个会话，以及实际看到的内容"></textarea></label><p class="form-note">只记录核验结果，不重新发送；会话仍由人工接管。</p><div class="footer"><button class="primary">保存核验记录</button></div></form>`);
  if (action === 'binding') return modal('核对微信会话身份',`<form data-form="binding" data-id="${id}"><label>执行设备<select name="deviceId" required><option value="">请选择</option>${state.devices.filter((d)=>!d.revoked).map((d)=>`<option value="${d.id}">${E(d.name)}</option>`).join('')}</select></label><label>微信账号代号<input name="account" required maxlength="80" placeholder="由已验收设备提供"></label><label>稳定会话标识<input name="conversation" required maxlength="200" placeholder="不能只使用可重复的昵称"></label><label>当前窗口核对名称<input name="name" required maxlength="80"></label><div class="checks"><label><input name="confirmed" type="checkbox" required>我已核对账号、会话与该客户一致</label></div><div class="footer"><button class="primary">确认绑定</button></div></form>`);
  if (action === 'assign') return modal('转交客户负责人',`<form data-form="assign" data-id="${id}"><label>新负责人<select name="assigneeId">${state.users.map((u)=>`<option value="${u.id}">${E(u.name)}</option>`).join('')}</select></label><p class="form-note">转交后旧负责人的访问权限失效，排队任务取消。</p><div class="footer"><button class="primary">确认转交</button></div></form>`);
  if (action === 'new-material') return modal('批准专业资料',`<form data-form="material"><div class="form-grid"><label>资料名称<input name="title" required maxlength="100"></label><label>商品或政策编号<input name="code" required maxlength="80"></label><label class="wide">已核实的内容<textarea name="body" required maxlength="4000"></textarea></label><label class="wide">核实依据<input name="source" required maxlength="500" placeholder="核验记录、政策版本或可信来源"></label><label class="wide">有效期<input name="expires" type="datetime-local" required></label></div><p class="form-note">保存代表你批准这份资料供员工使用；不会自动放行报价、承诺或售后。</p><div class="footer"><button class="primary">核实并批准</button></div></form>`);
  if (action === 'revoke-material') { await api('/api/materials',{action:'revoke',id}); await refresh(); toast('资料已撤回，相关发送任务已取消。'); return; }
  if (action === 'link-material') return modal('关联已审核资料',`<form data-form="link-material" data-id="${id}">${state.materials.filter((m)=>m.status==='approved' && m.expires>Date.now()).map((m)=>`<label><input type="checkbox" name="material" value="${m.id}">${E(m.title)} · V${m.version}</label>`).join('') || '<p class="muted">暂无有效资料，请先由老板批准。</p>'}<p class="form-note">最多选择3份资料，关联保留当前版本。</p><div class="footer"><button class="primary">确认关联</button></div></form>`);
  if (action === 'new-persona') return modal('发布个人口吻版本',`<form data-form="persona"><label>分身名称<input name="name" required maxlength="60" placeholder="例如：店长的温和表达"></label><label>常用称呼<select name="greeting"><option>您好</option><option>你好</option><option>亲</option><option>您</option></select></label><label>已核对的本人表达，每行一条<textarea name="samples" required maxlength="12000" placeholder="例如：不着急，咱们先看看您平时喜欢什么风格。"></textarea></label><p class="form-note">请移除客户身份、联系方式、价格和承诺。发布后仍需人工评测，未评测不开放自动模式。</p><div class="footer"><button class="primary">核对授权并发布</button></div></form>`);
  if (action === 'evaluate') return modal('录入人工盲测结果',`<form data-form="evaluate" data-id="${id}"><p class="form-note">请先在独立20题留出集上完成盲测。勾选表示该题更偏好个性化表达；未勾选表示未胜出。至少16题且没有事实错误才通过。此处是人工记录，不是系统自动测评。</p><div class="votes">${Array.from({length:20},(_,i)=>`<label><input type="checkbox" name="vote" value="${i}">第${i+1}题</label>`).join('')}</div><div class="checks"><label><input type="checkbox" name="noFactErrors" required>已完成真实盲测，并核实20题均没有新增事实错误</label></div><div class="footer"><button class="primary">保存人工评测记录</button></div></form>`);
  if (action === 'activate-persona') { await api('/api/personas',{action:'activate',id}); await refresh(); toast('口吻版本已切换，旧待发任务已失效。'); return; }
  if (action === 'learn-accept' || action === 'learn-reject') { await api('/api/learning',{id,action:action === 'learn-accept'?'accept':'reject'}); await refresh(); toast('学习审核已记录。'); return; }
  if (action === 'new-device') return modal('注册执行设备',`<form data-form="device"><label>设备名称<input name="name" required maxlength="60"></label><label>设备类型<select name="kind"><option value="fixture">模拟设备，仅合同演练</option><option value="real">真实执行器，需要另外真机验收</option></select></label><p class="form-note">登记设备不会自动让它连接微信。需要按交付协议安装供应商执行适配器。</p><div class="footer"><button class="primary">注册并生成设备凭据</button></div></form>`);
  if (action === 'verify-device') return modal('记录已完成的真机验收',`<form data-form="device-verify" data-id="${id}"><label>真机联合测试报告位置<input name="evidence" required maxlength="300"></label><div class="checks"><label><input type="checkbox" name="confirmed" required>已实际验证客户绑定、防重复、人工抢占、暂停与发送后核验</label></div><p class="form-note">未做实测不能确认，模拟设备不能通过。</p><div class="footer"><button class="primary">记录验收依据</button></div></form>`);
  if (action === 'revoke-device') { await api('/api/devices',{action:'revoke',id}); await refresh(); toast('设备已撤销。'); return; }
  if (action === 'reset-user') return modal('重置员工初始密码',`<form data-form="reset-user" data-id="${id}"><label>新的初始密码<input name="password" type="password" required minlength="10" autocomplete="new-password"></label><p class="form-note">旧登录会话立即撤销，员工下次登录必须先改密。</p><div class="footer"><button class="primary">重置并撤销旧会话</button></div></form>`);
  if (action === 'disable-user') return modal('停用员工账号',`<form data-form="disable-user" data-id="${id}"><p>停用后撤销旧会话，暂停该员工客户的自动处理。客户档案保留，可由老板转交。</p><div class="footer"><button class="danger">确认停用</button></div></form>`);
  if (action === 'new-user') return modal('创建员工独立账号',`<form data-form="user"><label>员工称呼<input name="name" required maxlength="40"></label><label>登录账号<input name="username" required maxlength="50" autocomplete="off"></label><label>初始密码<input type="password" name="password" required minlength="10" autocomplete="new-password"></label><p class="form-note">员工只能看到分配给自己的客户，不共享老板登录状态。</p><div class="footer"><button class="primary">创建员工</button></div></form>`);
  if (action === 'cleanup') return modal('执行到期资料清理',`<p>这会删除已超过30天的原始消息与草稿，并清理超过主档保存期限的客户身份资料。到期内容无法从工作台恢复。</p><form data-form="cleanup"><div class="checks"><label><input type="checkbox" required>我已了解清理范围</label></div><div class="footer"><button class="danger">执行到期清理</button></div></form>`);
  if (action === 'vision') { preview = null; return modal('用识流识别客户截图',`<form data-form="vision" data-id="${id}"><p class="form-note">仅上传已获授权的单聊截图，2MB以内PNG/JPG/WebP。识别后逐条核对，再选择客户原话导入；不会据此自动发送。</p><label>选择截图<input type="file" name="image" accept="image/png,image/jpeg,image/webp" required></label><div class="footer"><button class="primary">调用已配置识流接口</button></div></form>`); }
}

async function stopCurrentQwen(){
  const run=qwenRun;if(!run||run.stopping)return;run.stopping=true;
  const btn=dialog.querySelector('[data-action="stop-model"]');if(btn)btn.disabled=true;
  try{const result=await api(`/api/drafts/${run.draftId}/stop-model`,{jobId:run.jobId});run.stopAcknowledged=result.stopped;const label=document.querySelector('#qwen-wait');if(label)label.textContent=result.stopped?'停止请求已确认，正在收尾；迟到结果不会覆盖原稿。':'本次起草已经结束，请核对返回的文字。';}
  catch(error){toast(error.message,true);run.stopping=false;if(btn?.isConnected)btn.disabled=false;}
}
document.addEventListener('click', async (event) => {
  const el = event.target.closest('[data-action]'); if (!el || el.disabled) return;
  if(qwenRun&&['stop-model','close'].includes(el.dataset.action)){await stopCurrentQwen();return;}
  if(loading)return;
  loading = true; el.disabled = true;
  try { await action(el.dataset.action,el); } catch (error) { toast(error.message,true); }
  finally { loading = false; if (el.isConnected) el.disabled = false; }
});
function clearLocalOCRState(){
  if(!localPreview&&!localOCRRequest&&!windowCapture&&!dialog.querySelector('[data-form^="local-ocr"]'))return;
  const abandoned=localPreview?.previewId,capture=windowCapture?.captureId;localPreview=null;localOCRRequest=null;localOCRImage='';windowCapture=null;windowRegion=null;cropDrag=null;dialog.innerHTML='';
  if(abandoned)api('/api/local-ocr/discard',{previewId:abandoned}).catch(()=>{});
  if(capture)api('/api/local-ocr/discard-window',{captureId:capture}).catch(()=>{});
}
dialog.addEventListener('close',()=>{if(!dialog.open)clearLocalOCRState();});
dialog.addEventListener('cancel',event=>{if(qwenRun){event.preventDefault();void stopCurrentQwen();}else clearLocalOCRState();});
document.addEventListener('change',(event)=>{if(event.target.id==='queue-filter'){filter=event.target.value;render();}});
document.addEventListener('input',(event)=>{if(event.target.id==='customer-search'){const q=event.target.value.toLowerCase();document.querySelectorAll('.client-card').forEach((x)=>x.hidden=!x.dataset.search.toLowerCase().includes(q));}});
document.addEventListener('input',event=>{if(event.target.closest?.('[data-form=studio-profile]'))studioDirty=true;});
document.addEventListener('change',event=>{if(event.target.closest?.('[data-form=studio-profile]'))studioDirty=true;const form=event.target.closest?.('[data-form=studio-preview]');if(form&&event.target.name==='customerId'){if(studioDirty){event.target.value=studioCustomerId;toast('请先保存左侧口吻修改。',true);return;}studioCustomerId=event.target.value;studioReviewId=null;render();return;}if(form&&event.target.name==='rehearsalQuestion'){const q=rehearsalQuestion(event.target.value);if(q){form.querySelector('[name=question]').value=q.question;const previous=form.querySelector('[name=continueConversation]');if(previous)previous.checked=false;}}if(form&&event.target.checked){if(event.target.name==='compareOwner'){const old=form.querySelector('[name=continueConversation]');if(old)old.checked=false;}if(event.target.name==='continueConversation'){const compare=form.querySelector('[name=compareOwner]');if(compare)compare.checked=false;}}});
document.addEventListener('change',async event=>{
 const form=event.target.closest?.('[data-form=knowledge-import]');if(!form)return;
 const auto=form.querySelector('[name=autoOrganize]'),status=form.querySelector('#knowledge-status');
 if(event.target.name==='kind'){auto.disabled=event.target.value==='voice';if(auto.disabled)auto.checked=false;}
 if(event.target.name!=='textFile'||!event.target.files?.length)return;
 if(loading){toast('上一份资料正在整理，请完成后再读取文件。');return;}
 const ticket=++knowledgeFileRead;
 try{const result=await readKnowledgeTextFile(event.target.files[0]);if(ticket!==knowledgeFileRead||!form.isConnected)return;
  form.querySelector('[name=text]').value=result.text;
  if(!form.querySelector('[name=title]').value.trim())form.querySelector('[name=title]').value=result.title;
  if(!form.querySelector('[name=source]').value.trim())form.querySelector('[name=source]').value=('店主本机TXT文件：'+event.target.files[0].name).slice(0,200);
  form.querySelector('[name=confirmed]').checked=false;status.textContent='TXT已读取到下方文字框，请先检查内容和说话人，再确认使用。';
 }catch(error){if(ticket!==knowledgeFileRead||!form.isConnected)return;status.textContent=error.message;event.target.value='';toast(error.message);}
});
document.addEventListener('submit',async(event)=>{
  const form=event.target.closest('[data-form]'); if(!form)return; event.preventDefault();
  if(loading)return; loading=true; const submit=form.querySelector('button[type=submit],button:not([type])'); if(submit)submit.disabled=true;
  const values=Object.fromEntries(new FormData(form)); const kind=form.dataset.form; const id=form.dataset.id;
  const checked=(name)=>Boolean(form.querySelector(`[name="${name}"]`)?.checked);
  if(kind==='local-ocr-window')form.querySelector('[data-window-error]')?.remove();
  if(kind==='official-kf-start')form.querySelector('[data-official-error]')?.remove();
  try{
    if(kind==='password'){if(values.newPassword!==values.confirmPassword)throw new Error('两次新密码不一致。');await api('/api/account/password',values);dialog.close();state=null;auth(false);toast('密码已修改，请重新登录。');return;}
    if(kind==='auth'){await api(setupMode?'/api/setup':'/api/login',values);await refresh();return;}
    if(kind==='coach-compare'){const label=form.querySelector('#coach-status');if(label)label.textContent='两个模型正在依次生成… 可以随时停止，不会外发。';try{const r=await api('/api/coach/compare',{version:state.personaStudio?.profile?.version,caseId:values.caseId||undefined,question:values.question,context:values.context.split('\n').map(x=>x.trim()).filter(Boolean),rehearsalConfirmed:checked('rehearsalConfirmed')});coachRunId=r.id;}catch(error){if(label)label.textContent=error.message;throw error;}}
    else if(kind==='coach-review')await api('/api/coach/review',{id,service:values.service,voice:values.voice,note:values.note});
    else if(kind==='coach-approve')await api('/api/coach/approve',{id,side:form.dataset.side,confirmed:checked('confirmed')});
    else if(kind==='local-model-runtime')await api('/api/model/local-runtime',{version:Number(form.dataset.version),warm:checked('warm')});
    else if(kind==='knowledge-import'){
      const label=form.querySelector('#knowledge-status'),automatic=checked('autoOrganize');
      if(label)label.textContent=automatic?'本机模型正在摘录资料，请稍候；结果仍需逐条核实。':'正在按说话人标记整理资料…';
      try{await api(automatic?'/api/knowledge/organize':'/api/knowledge/import',{kind:values.kind,title:values.title,source:values.source,text:values.text,confirmed:checked('confirmed')});}
      catch(error){if(label)label.textContent=error.message;throw error;}
    }
    else if(kind==='knowledge-approve')await api('/api/knowledge/review',{...values,id,version:Number(form.dataset.version),action:'approve',confirmed:checked('confirmed'),expires:values.expires?new Date(values.expires+'T23:59:59+08:00').getTime():null});
    else if(kind==='studio-profile'){await api('/api/persona-studio/save',studioProfileInput(form));studioDirty=false;}
    else if(kind==='studio-preview'){
      if(studioDirty)throw new Error('请先保存左侧修改，再试聊，避免使用旧口吻。');
      const label=document.querySelector('#studio-status');if(label)label.textContent=checked('compareOwner')?'正在分别生成基础版和有资料版，完成后一起显示；失败不会用模板冒充。':'分身正在参考本人样本生成对比，请稍候…';
      try{await api('/api/persona-studio/preview',{version:state.personaStudio?.profile?.version,customerId:studioCustomerId||undefined,question:values.question,compareOwner:checked('compareOwner'),previousTrialId:checked('continueConversation')?values.continueConversation:undefined});studioReviewId=null;}
      catch(error){if(label)label.textContent=error.message;throw error;}
    }
    else if(kind==='studio-correction'){if(studioDirty)throw new Error('请先保存左侧修改。');await api('/api/persona-studio/feedback',{id,version:state.personaStudio?.profile?.version,rating:'corrected',correction:values.correction,confirmed:checked('confirmed')});}
    else if(kind==='official-kf-start')await api('/api/official-kf/start',{confirmed:checked('confirmed'),exclusiveTestConfirmed:checked('exclusiveTestConfirmed'),discoveryConfirmed:form.dataset.discovery==='true',unlimitedTestConfirmed:form.dataset.discovery==='true'});
    else if(kind==='customer-preferences'){await api(`/api/customers/${id}`,{version:Number(form.dataset.version),communicationPreferences:{length:values.length,manner:values.manner,reception:values.reception||'inherit',addressing:values.addressing}});}
    else if(kind==='customer'){const c=await api('/api/customers',{...values,synthetic:checked('synthetic')});selected=c.id;}
    else if(kind==='message'){await api(`/api/customers/${id}/messages`,{text:values.text,synthetic:state.customers.find((c)=>c.id===id).synthetic,eventId:crypto.randomUUID()});await api(`/api/customers/${id}/generate`,{});}
    else if(kind==='simulate')await api(`/api/drafts/${id}/simulate`,values);
    else if(kind==='journey'||kind==='assign')await api(`/api/customers/${id}`,{...values,version:state.customers.find((c)=>c.id===id).version});
    else if(kind==='binding')await api(`/api/customers/${id}`,{version:state.customers.find((c)=>c.id===id).version,binding:{...values,confirmed:checked('confirmed')}});
    else if(kind==='link-material')await api(`/api/customers/${id}`,{version:state.customers.find((c)=>c.id===id).version,productIds:new FormData(form).getAll('material')});
    else if(kind==='reminder')await api('/api/reminders',{...values,customerId:id,due:new Date(values.due).toISOString()});
    else if(kind==='postpone')await api(`/api/reminders/${id}`,{action:'postpone',version:state.reminders.find((r)=>r.id===id).version,due:new Date(values.due).toISOString()});
    else if(kind==='resolve')await api('/api/tasks/resolve',{...values,id});
    else if(kind==='material')await api('/api/materials',{...values,expires:new Date(values.expires).toISOString()});
    else if(kind==='persona')await api('/api/personas',values);
    else if(kind==='evaluate'){const votes=new FormData(form).getAll('vote').map(Number);await api('/api/personas',{action:'evaluate',id,votes:Array.from({length:20},(_,i)=>votes.includes(i)),noFactErrors:checked('noFactErrors')});}
    else if(kind==='user')await api('/api/users',values);
    else if(kind==='reset-user')await api('/api/users',{id,password:values.password});
    else if(kind==='disable-user')await api('/api/users',{id,active:false});
    else if(kind==='device'){
      const d=await api('/api/devices',values);await refresh();
      modal('设备已注册，凭据仅显示这一次',`<p class="form-note">请把凭据保存到本机执行器环境变量，不要发到聊天或写入代码。</p><label>设备编号<input readonly value="${E(d.id)}"></label><label>设备凭据<input type="password" readonly value="${E(d.token)}" id="device-token"></label><div class="footer">${button('查看凭据','show-token','','small')}${button('我已保存','close','','primary')}</div>`);return;
    }
    else if(kind==='device-verify')await api('/api/devices',{action:'verify',id,evidence:values.evidence,confirmed:checked('confirmed')});
    else if(kind==='local-ocr-window'){
      const token=crypto.randomUUID();localOCRRequest=token;
      const data=await api('/api/local-ocr/window',{customerId:id,customerName:values.customerName,confirmed:checked('confirmed')});
      if(localOCRRequest!==token||!dialog.open){await api('/api/local-ocr/discard-window',{captureId:data.captureId});return;}
      windowCapture=data;windowRegion=null;modal('框选客户原话区域',windowCropForm(data));return;
    }
    else if(kind==='local-ocr-region'){
      if(!windowCapture||!windowRegion)throw new Error('请先框选客户消息区域。');
      const token=crypto.randomUUID();localOCRRequest=token;
      const data=await api('/api/local-ocr/region',{captureId:windowCapture.captureId,customerId:windowCapture.customerId,region:windowRegion});
      if(localOCRRequest!==token||!dialog.open){await api('/api/local-ocr/discard',{previewId:data.previewId});return;}
      windowCapture=null;windowRegion=null;localPreview=data;localOCRImage=data.image;modal('核对本机识别结果',localOCRReview(data,data.image));return;
    }
    else if(kind==='local-ocr'){
      const file=new FormData(form).get('image');if(file.size>2*1024*1024)throw new Error('截图不能超过2MB。');
      const token=crypto.randomUUID();localOCRRequest=token;
      const image=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const data=await api('/api/local-ocr/preview',{customerId:id,image});
      if(localOCRRequest!==token||!dialog.open){await api('/api/local-ocr/discard',{previewId:data.previewId});return;}
      localPreview=data;localOCRImage=image;modal('核对本机识别结果',localOCRReview(data,image));return;
    }
    else if(kind==='local-ocr-confirm'){
      if(!localPreview)throw new Error('识别预览已失效，请重新识别。');
      const rows=[...form.querySelectorAll('[name=ocrIndex]:checked')].map(check=>({index:Number(check.value),text:form.querySelector(`[name="ocr-text-${check.value}"]`).value}));
      const result=await api('/api/local-ocr/confirm',{previewId:localPreview.previewId,rows,customerName:values.customerName,confirmed:checked('confirmed'),singleMessage:checked('singleMessage')});
      localPreview=null;localOCRImage='';localOCRRequest=null;dialog.close();selected=result.customerId;view='workbench';await refresh();
      if(!result.duplicate){try{await api(`/api/customers/${result.customerId}/generate`,{});await refresh();}catch{toast('客户原话已导入，建议生成暂未完成；可稍后刷新查看。',true);return;}}
      toast(result.duplicate?'这条原话已存在，没有重复导入。':'已导入人工核对的客户原话，没有发送微信。');return;
    }
    else if(kind==='model-test'){const result=await api('/api/model/test',{version:Number(form.dataset.version),confirmed:checked('confirmed')});modal('模型接口已返回演练文字',`<div class="notice blue">仅完成连接测试，未发送微信，也不代表销售效果已通过验收。</div><p>配置模型：${E(result.model)}</p><blockquote>${E(result.text)}</blockquote><p class="form-note">${E(result.note)}</p>`);return;}
    else if(kind==='desktop-profile')await api('/api/desktop/profile',values);
    else if(kind==='tars-settings'){await api('/api/settings',{...values,version:state.settings.policyVersion,dataConsent:checked('dataConsent')});}
    else if(kind==='tars-test'){const file=new FormData(form).get('image');if(file.size>2*1024*1024)throw new Error('截图不能超过2MB。');const image=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});const point=await api('/api/ui-tars/locate',{image});modal('输入框定位候选 未执行操作','<p>候选位置：'+Math.round(point.x)+'，'+Math.round(point.y)+'</p><p class="form-note">这只证明一次视觉定位调用，不能证明微信自动发送已就绪。</p><svg class="grounding-preview" viewBox="0 0 '+point.width+' '+point.height+'"><image href="data:image/png;base64,'+E(image)+'" width="'+point.width+'" height="'+point.height+'"/><circle cx="'+point.x+'" cy="'+point.y+'" r="14" fill="none" stroke="#e53737" stroke-width="4"/></svg>');return;}
    else if(['mode','vision-settings','model-settings','policy-settings'].includes(kind)){
      const body={...values,version:state.settings.policyVersion};
      if(kind==='model-settings')body.modelDataConsent=checked('modelDataConsent');
      if(kind==='vision-settings'){body.supplierConfirmed=checked('supplierConfirmed');body.dataConsent=checked('dataConsent');}
      if(kind==='policy-settings'){body.infoApproved=checked('infoApproved');for(const k of ['hoursStart','hoursEnd','perMinute','maxRounds','dailyLimit','customerRetentionDays'])body[k]=Number(body[k]);}
      await api('/api/settings',body);
    }
    else if(kind==='cleanup')await api('/api/retention',{});
    else if(kind==='vision'){
      const file=new FormData(form).get('image');if(file.size>2*1024*1024)throw new Error('截图不能超过2MB。');
      const image=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
      preview=await api('/api/vision/preview',{customerId:id,image});
      modal('逐条核对识别结果',`<form data-form="vision-confirm"><p class="form-note">识别窗口：${E(preview.name)}。只勾选核实属于当前客户的原话。昵称、方向与内容都必须由你确认。</p>${preview.messages.map((m)=>`<div class="selection-line"><label><input type="checkbox" name="index" value="${m.index}"><strong>${E(m.nickname)}</strong>：${E(m.text)}</label></div>`).join('')}<div class="checks"><label><input name="confirmed" type="checkbox" required>已核实窗口与客户一致，并逐条检查所选原话</label></div><div class="footer"><button class="primary">导入已核实消息</button></div></form>`);return;
    }
    else if(kind==='vision-confirm')await api('/api/vision/confirm',{previewId:preview.previewId,name:preview.name,indexes:new FormData(form).getAll('index').map(Number),confirmed:checked('confirmed')});
    dialog.close();await refresh();toast(kind==='simulate'?'演练已完成，没有向微信发送。':'已保存，可回看处理记录。');
  }catch(error){
    if(kind==='official-kf-start'&&form.isConnected){const message=document.createElement('p');message.className='notice';message.dataset.officialError='true';message.setAttribute('role','alert');message.textContent=error.message;form.prepend(message);}
    if(kind==='local-ocr-window'&&form.isConnected){const message=document.createElement('p');message.className='notice window-read-error';message.dataset.windowError='true';message.setAttribute('role','alert');message.textContent=error.message;form.prepend(message);}
    toast(error.message,true);
  }finally{loading=false;if(submit?.isConnected)submit.disabled=false;}
});
document.addEventListener('click',(event)=>{if(event.target.closest('[data-action="show-token"]')){const input=document.querySelector('#device-token');input.type=input.type==='password'?'text':'password';}});
async function boot(){try{const info=await api('/api/bootstrap');if(info.needsSetup)auth(true);else{try{await refresh();}catch{auth(false);}}}catch(error){app.innerHTML=`<main class="loading">${E(error.message)}</main>`;}}
await boot();
setInterval(async()=>{if(!state||state.mustChangePassword||loading||dialog.open||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;try{const next=await api('/api/state');if(JSON.stringify({...next,now:0})!==JSON.stringify({...state,now:0})){state=next;render();}}catch{}},5000);

function desktopPanel(){const s=state.settings;const p=s.desktopProfile||{};const labels={limited:'测试额度用尽',account:'微信账号代号',conversation:'稳定会话标识',name:'核对的窗口称呼',identityIdentifier:'客户身份控件标识',identityValue:'控件中已核对的唯一身份值',inputIdentifier:'消息输入框控件标识',sendIdentifier:'发送按钮控件标识',messagePrefix:'消息唯一标识前缀',incomingDescription:'客户消息方向标签',outgoingDescription:'本人消息方向标签'};return `<section class="panel"><div class="panel-head"><h2>UI-TARS 本机接入</h2><div class="row wrap">${button('检查本机条件','desktop-status','','small')}${button('预览微信主窗口','desktop-probe','','small')}${button('停止执行端','desktop-stop','','small danger')}</div></div><div class="panel-body"><div class="notice blue"><div>视觉模型负责定位输入框，本机执行端只使用系统批准的回复。报价、承诺和售后仍转人工。未开通模型服务时，可以先准备本机组件和配置。</div></div><div class="cards"><form data-form="tars-settings"><label>视觉接入方式<select name="visionProvider"><option value="ui_tars" ${s.visionProvider==='ui_tars'?'selected':''}>UI-TARS 推荐本机路线</option><option value="shiflow" ${s.visionProvider==='shiflow'?'selected':''}>识流 兼容路线</option></select></label><label>UI-TARS完整生成接口地址<input name="tarsEndpoint" type="url" value="${E(s.tarsEndpoint||'')}" placeholder="供应商提供的HTTPS地址，以/chat/completions结尾"></label><label>UI-TARS视觉模型或接入点编号<input name="tarsModel" value="${E(s.tarsModel||'')}" placeholder="开通服务后填写，不代替销售回复模型"></label><label>视觉服务密钥 ${s.tarsKeySet?'已保存，留空保留':'尚未配置'}<input name="tarsKey" type="password" autocomplete="new-password"></label><div class="checks"><label><input type="checkbox" name="dataConsent" ${s.dataConsent?'checked':''}>已确认选定视觉服务处理截图的用途和保存规则</label></div><div class="row wrap"><button class="primary">保存视觉接入配置</button>${button('测试截图定位','tars-test','','small')}</div></form><div><h3>接入顺序</h3><p class="tagline">① 准备本机组件 → ② 打开桌面微信 → ③ 核对窗口映射 → ④ 启动观察 → ⑤ 实际联合验收后开放自动模式。</p><p class="form-note">首次观察只建立可见消息基线，不自动回复历史消息。检测到人工操作后暂停，须人工停止并重新启动观察。当前按单个前台微信会话验证。</p><details><summary>高级窗口映射 由实施人员核对</summary><form data-form="desktop-profile"><label>绑定客户<select name="customerId" required><option value="">请选择实际授权测试客户</option>${state.customers.filter(c=>!c.synthetic).map(c=>`<option value="${c.id}" ${p.customerId===c.id?'selected':''}>${E(c.alias)}</option>`).join('')}</select></label>${Object.entries(labels).map(([key,label])=>`<label>${label}<input name="${key}" value="${E(p[key]||'')}" maxlength="200" required></label>`).join('')}<p class="form-note">从本机窗口预览核对。昵称和截图哈希不能冒充稳定身份；没有可靠消息ID时保持辅助模式。保存映射不会自动通过真机验收。</p><button>核对并保存窗口映射</button></form></details><div class="spacer"></div>${button('启动本机观察','desktop-start','','primary')}</div></div></div></section><div class="spacer"></div>`;}

function localOCRReview(data,image){return `<form data-form="local-ocr-confirm"><div class="notice blue">目标客户：${E(data.customerAlias)}。以下只是读字结果，尚未导入。</div><p class="form-note">请对照截图，只勾选同一条客户原话的文字。不要勾选本人回复、昵称、时间或其他人的消息；识别有误可直接改字。</p><details open><summary>对照本次截图</summary><img class="ocr-image" src="data:image/png;base64,${E(image)}" alt="本次本机识别截图"></details><div class="ocr-lines">${data.lines.map(line=>`<div class="ocr-line"><label><input type="checkbox" name="ocrIndex" value="${line.index}">选择第${line.index+1}行，归入同一条客户原话</label><textarea name="ocr-text-${line.index}" aria-label="第${line.index+1}行识别文字" maxlength="2000" rows="2">${E(line.text)}</textarea></div>`).join('')}</div><label>重新输入目标客户称呼<input name="customerName" required placeholder="${E(data.customerAlias)}" autocomplete="off"></label><div class="checks"><label><input name="confirmed" type="checkbox" required>已对照截图，核实窗口对应上方客户</label><label><input name="singleMessage" type="checkbox" required>所选文字仅属于同一条客户原话，已排除本人及其他内容</label></div><p class="form-note">预览5分钟内有效。导入记录为人工核对来源，不能触发微信自动回复。</p><button class="primary">确认导入客户原话</button></form>`;}

function windowCropForm(data){return `<form data-form="local-ocr-region"><div class="notice blue">目标档案：${E(data.customerAlias)}。截图时间：${date(data.capturedAt)}。请框住一条客户原话，避开联系人列表、本人回复和其他消息。</div><p class="form-note">用鼠标拖出矩形，也可用数字调整。截图2分钟内有效；不会继续监看微信，也不会自动核实联系人。</p><svg id="window-crop" class="window-crop" viewBox="0 0 ${data.width} ${data.height}" role="img" aria-label="拖动框选客户消息区域"><image href="data:image/png;base64,${E(data.image)}" width="${data.width}" height="${data.height}"/><rect id="window-selection" class="crop-selection" x="0" y="0" width="0" height="0" vector-effect="non-scaling-stroke"/></svg><p id="window-region-info" class="form-note" aria-live="polite">尚未选择区域。</p><details><summary>用数字调整选区</summary><div class="form-grid">${[['x','左边距'],['y','上边距'],['width','选区宽度'],['height','选区高度']].map(([key,label])=>`<label>${label}<input type="number" name="crop-${key}" min="0" value="0" data-crop-coordinate="${key}"></label>`).join('')}</div><p class="form-note">按原图像素计算；选区必须完整落在图片内。</p></details><div class="footer">${button('重画选区','clear-window-region','','small')}<button id="recognize-window-region" class="primary" disabled>只识别框选区域</button></div></form>`;}
function updateWindowRegion(region,writeFields=true){
  if(!windowCapture)return;const valid=Object.values(region).every(Number.isInteger)&&region.x>=0&&region.y>=0&&region.width>=16&&region.height>=16&&region.x+region.width<=windowCapture.width&&region.y+region.height<=windowCapture.height;
  windowRegion=valid?region:null;const box=document.querySelector('#window-selection');if(box)for(const key of ['x','y','width','height'])box.setAttribute(key,String(valid?region[key]:0));
  if(writeFields)for(const key of ['x','y','width','height']){const input=dialog.querySelector(`[name="crop-${key}"]`);if(input)input.value=String(region[key]);}
  const info=document.querySelector('#window-region-info');if(info)info.textContent=valid?`已选区域：宽${region.width}、高${region.height}；请确认只包含需要的客户原话。`:'尚未选择有效区域；请框选至少16×16像素，且不超出图片。';
  const submit=document.querySelector('#recognize-window-region');if(submit)submit.disabled=!valid;
}
function cropPoint(svg,event){const matrix=svg.getScreenCTM();if(!matrix||!windowCapture)return null;const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;const local=point.matrixTransform(matrix.inverse());return{x:Math.max(0,Math.min(windowCapture.width,local.x)),y:Math.max(0,Math.min(windowCapture.height,local.y))};}
document.addEventListener('pointerdown',event=>{const svg=event.target.closest?.('#window-crop');if(!svg||!windowCapture||loading||event.button!==0)return;const start=cropPoint(svg,event);if(!start)return;event.preventDefault();svg.setPointerCapture(event.pointerId);cropDrag={start,pointerId:event.pointerId};});
document.addEventListener('pointermove',event=>{if(!cropDrag||cropDrag.pointerId!==event.pointerId)return;const svg=document.querySelector('#window-crop');if(!svg)return;const end=cropPoint(svg,event);if(!end)return;const x=Math.floor(Math.min(cropDrag.start.x,end.x)),y=Math.floor(Math.min(cropDrag.start.y,end.y));updateWindowRegion({x,y,width:Math.ceil(Math.max(cropDrag.start.x,end.x))-x,height:Math.ceil(Math.max(cropDrag.start.y,end.y))-y});});
document.addEventListener('pointerup',event=>{if(cropDrag?.pointerId===event.pointerId)cropDrag=null;});
document.addEventListener('input',event=>{if(!event.target.matches?.('[data-crop-coordinate]')||!windowCapture)return;const region={};for(const key of ['x','y','width','height'])region[key]=Number(dialog.querySelector(`[name="crop-${key}"]`).value);updateWindowRegion(region,false);});

document.addEventListener('change',event=>{if(event.target.matches('[data-form=coach-compare] [name=caseId]')){const form=event.target.form,c=state.coachLab.cases.find(c=>c.id===event.target.value);for(const name of ['question','context']){const field=form.elements.namedItem(name);field.readOnly=!!c;field.value=c?(name==='context'?c.context.join('\n'):c.question):'';}}});

document.addEventListener('change',event=>{if(event.target.matches('[data-form=coach-compare] [name=caseFamily]')){const form=event.target.form,chosen=state.coachLab.cases.find(c=>!event.target.value||c.family===event.target.value);const select=form.elements.namedItem('caseId');select.innerHTML=coachCaseOptions(state.coachLab.cases,chosen?.id,event.target.value);select.dispatchEvent(new Event('change',{bubbles:true}));}});
