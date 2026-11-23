import{preferenceChoices,preferenceValues}from'../web/customer-preferences.js';
import { randomUUID, randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';
import { assess, extractNeeds, personalize, readiness, safeLearning, mandatoryTopics, stages, hash } from './policy.mjs';

export class Problem extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}
export const id = () => randomUUID();
export function requireText(value, name, max = 1000) {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > max) throw new Problem(`${name}不能为空，且最多${max}字。`);
  return value.trim();
}
export function passwordHash(password, salt = randomBytes(16).toString('hex')) {
  requireText(password, '密码', 256);
  if (password.length < 10) throw new Problem('密码至少10位。');
  return `${salt}:${scryptSync(password, salt, 32).toString('hex')}`;
}
export function passwordMatches(password, encoded) {
  try {
    const [salt, expected] = encoded.split(':');
    const actual = scryptSync(String(password), salt, 32);
    return timingSafeEqual(actual, Buffer.from(expected, 'hex'));
  } catch { return false; }
}

export class Engine {
  constructor(store, clock = () => Date.now()) {
    this.store = store; this.clock = clock;
    this.store.transact((s) => {
      for (const task of s.tasks.filter((t) => t.status === 'executing')) {
        task.status = 'unknown'; task.reason = '进程重启，请人工核验。';
        const c = s.customers.find((x) => x.id === task.customerId);
        if (c) { c.locked = true; c.lockReason = task.reason; }
      }
    });
  }
  audit(s, actor, kind, customerId, details = {}) {
    s.audit.push({ id: id(), at: this.clock(), actor: actor?.id || 'system', kind, customerId, details });
  }
  owner(actor) { if (actor.role !== 'owner') throw new Problem('只有老板可以操作。', 403); }
  customer(s, actor, customerId) {
    const c = s.customers.find((x) => x.id === customerId && !x.deletedAt);
    if (!c || (actor.role !== 'owner' && c.assigneeId !== actor.id)) throw new Problem('客户不存在或没有访问权限。', 404);
    return c;
  }
  latest(s, customerId) { return s.messages.filter((m) => m.customerId === customerId && m.direction === 'in').at(-1); }
  persona(s) { return s.personas.find((p) => p.id === s.settings.personaId); }
  setup(input) {
    const username = requireText(input.username, '账号', 50);
    const password = passwordHash(input.password);
    return this.store.transact((s) => {
      if (s.users.length) throw new Problem('已经初始化，请登录。', 409);
      const user = { id: id(), username, name: requireText(input.name || '店铺负责人', '称呼', 40), password, role: 'owner', active: true };
      s.users.push(user); this.audit(s, user, '创建本机工作空间');
      return user;
    });
  }
  session(user) {
    const token = randomBytes(32).toString('base64url');
    this.store.transact((s) => {
      s.sessions = s.sessions.filter((x) => x.expires > this.clock());
      s.sessions.push({ tokenHash: hash(token), userId: user.id, expires: this.clock() + 8 * 3600_000 });
    }); return token;
  }
  authenticate(token) {
    const s = this.store.read();
    const session = s.sessions.find((x) => x.tokenHash === hash(token || '') && x.expires > this.clock());
    const user = s.users.find((u) => u.id === session?.userId && u.active);
    if (!user) throw new Problem('请先登录。', 401);
    return { id: user.id, name: user.name, username: user.username, role: user.role, mustChangePassword: user.mustChangePassword === true };
  }
  logout(token) { this.store.transact((s) => { s.sessions = s.sessions.filter((x) => x.tokenHash !== hash(token || '')); }); }
  changePassword(actor, input) {
    return this.store.transact((s) => {
      const u = s.users.find((x) => x.id === actor.id && x.active);
      if (!u || !passwordMatches(input.oldPassword, u.password)) throw new Problem('原密码不正确。', 403);
      if (input.oldPassword === input.newPassword) throw new Problem('新密码不能与原密码相同。');
      u.password = passwordHash(input.newPassword); u.mustChangePassword = false;
      s.sessions = s.sessions.filter((x) => x.userId !== u.id);
      this.audit(s, actor, '修改个人密码并撤销旧会话'); return { ok: true, loginRequired: true };
    });
  }
  snapshot(actor) {
    const s = this.store.read();
    if (s.users.find((u) => u.id === actor.id)?.mustChangePassword) return { actor: { id: actor.id, name: actor.name, role: actor.role }, mustChangePassword: true };
    const customers = s.customers.filter((c) => !c.deletedAt && (actor.role === 'owner' || c.assigneeId === actor.id));
    const ids = new Set(customers.map((x) => x.id));
    const visible = (items) => items.filter((x) => ids.has(x.customerId));
    const { visionKey, modelKey, tarsKey, desktopToken, ...settings } = s.settings;
    const summary = { mode: settings.mode, paused: settings.paused };
    return {
      actor: { id: actor.id, name: actor.name, username: actor.username, role: actor.role }, customers, messages: visible(s.messages), drafts: visible(s.drafts), tasks: visible(s.tasks).map(({ grant, ...t }) => t),
      reminders: visible(s.reminders), learning: actor.role === 'owner' ? s.learning : [],
      materials: s.materials.filter((x) => x.status === 'approved' || actor.role === 'owner'),
      personas: actor.role === 'owner' ? s.personas : s.personas.map(({ id, name, version, verified, greeting }) => ({ id, name, version, verified, greeting })),
      ...(actor.role==='owner'?{personaStudio:s.personaStudio||{profile:null,trials:[]},knowledgeLibrary:s.knowledgeLibrary||{revision:0,imports:[],items:[]}}:{}),
      users: s.users.filter((u) => u.active && (actor.role === 'owner' || u.id === actor.id)).map(({ id, name, role }) => ({ id, name, role })),
      settings: actor.role === 'owner' ? { ...settings, visionKeySet: !!visionKey, modelKeySet: !!modelKey, tarsKeySet: !!tarsKey } : summary,
      blockers: actor.role === 'owner' ? readiness(s, this.clock()) : [],
      connectionReady: readiness(s, this.clock()).length === 0,
      pauseAcknowledged: s.devices.filter((d) => d.kind === 'real' && !d.revoked).every((d) => d.paused && d.ackPolicyVersion >= s.settings.policyVersion && this.clock() - d.lastSeen < 15_000),
      devices: actor.role === 'owner' ? s.devices.map(({ tokenHash, ...rest }) => rest) : [],
      audit: s.audit.filter((x) => actor.role === 'owner' || ids.has(x.customerId)).slice(-400),
      costs: actor.role === 'owner' ? s.costs : [], now: this.clock(),
    };
  }
  createCustomer(actor, input) {
    return this.store.transact((s) => {
      const assigneeId = actor.role === 'owner' ? (input.assigneeId || actor.id) : actor.id;
      if (!s.users.some((u) => u.id === assigneeId && u.active)) throw new Problem('负责人不存在。');
      const c = { id: id(), alias: requireText(input.alias, '客户称呼', 60), assigneeId, synthetic: input.synthetic === true,
        stage: 'discover', result: 'ongoing', needs: [], candidates: [], products: [], version: 1,
        locked: false, lockReason: '', optedOut: false, binding: null, createdAt: this.clock(), updatedAt: this.clock() };
      s.customers.push(c); this.audit(s, actor, '创建客户', c.id, { synthetic: c.synthetic }); return c;
    });
  }
  updateCustomer(actor, customerId, input) {
    return this.store.transact((s) => {
      const c = this.customer(s, actor, customerId);
      if (input.version !== c.version) throw new Problem('客户资料已变化，请刷新后重试。', 409);
      if(Object.hasOwn(input,'communicationPreferences')){
        const p=input.communicationPreferences;
        if(p!==null&&(typeof p!=='object'||Array.isArray(p)||Object.keys(p).some(k=>!Object.hasOwn(preferenceChoices,k))||['length','manner','addressing'].some(k=>!Object.hasOwn(p,k))||Object.entries(p).some(([k,v])=>!Object.hasOwn(preferenceChoices[k]||{},v))))throw new Problem('请选择有效的客户沟通偏好。');
        c.communicationPreferenceRevision=(c.communicationPreferenceRevision||0)+1;
        c.communicationPreferences=p===null?null:{...preferenceValues(p),version:c.communicationPreferenceRevision,source:'manual',updatedBy:actor.id,at:this.clock()};
        this.cancel(s,c.id,'客户接待偏好已变化');this.audit(s,actor,'人工设置客户沟通偏好',c.id,{preferences:p});
      }
      if (input.stage) {
        if (!stages.includes(input.stage)) throw new Problem('无效销售阶段。');
        c.stage = input.stage; this.audit(s, actor, '人工确认阶段', c.id, { stage: c.stage });
      }
      if (input.result) {
        if (!['ongoing', 'pending_sale', 'won', 'paused', 'lost'].includes(input.result)) throw new Problem('无效机会结果。');
        if (['won', 'lost'].includes(input.result)) requireText(input.resultEvidence, '核实依据或流失原因', 300);
        c.result = input.result; this.audit(s, actor, '人工核实机会结果', c.id, { result: c.result, evidence: input.resultEvidence || '' });
      }
      if (input.assigneeId) {
        this.owner(actor);
        if (!s.users.some((u) => u.id === input.assigneeId && u.active)) throw new Problem('负责人不可用。');
        c.assigneeId = input.assigneeId; c.locked = true; c.lockReason = '客户已转交，请新负责人确认。';
        this.cancel(s, c.id, '客户转交'); this.audit(s, actor, '客户转交', c.id);
      }
      if (input.binding) {
        const b = input.binding;
        requireText(b.account, '微信账号代号', 80); requireText(b.conversation, '稳定会话标识', 200);
        requireText(b.name, '已核对的窗口名称', 80);
        if (b.confirmed !== true) throw new Problem('请先核对客户身份。');
        const device = s.devices.find((d) => d.id === b.deviceId && !d.revoked);
        if (!device) throw new Problem('请先注册执行设备。');
        if (s.customers.some((x) => x.id !== c.id && !x.deletedAt && x.binding?.account === b.account && x.binding?.conversation === b.conversation)) throw new Problem('此会话已绑定其他客户。', 409);
        c.binding = { account: b.account, conversation: b.conversation, name: b.name, deviceId: b.deviceId, confirmedBy: actor.id, at: this.clock() };
        this.cancel(s, c.id, '身份绑定变化'); this.audit(s, actor, '人工确认会话绑定', c.id);
      }
      if (input.candidateId) {
        const candidate = c.candidates.find((x) => x.id === input.candidateId);
        if (!candidate) throw new Problem('需求候选不存在。');
        if (input.accept === true) {
          c.needs = c.needs.filter((x) => x.kind !== candidate.kind);
          c.needs.push({ ...candidate, confirmedBy: actor.id, at: this.clock() });
        }
        c.candidates = c.candidates.filter((x) => x.id !== candidate.id);
        this.audit(s, actor, input.accept ? '确认客户需求' : '排除需求候选', c.id);
      }
      if (Array.isArray(input.productIds)) {
        if (input.productIds.length > 3) throw new Problem('最多关联3份商品资料。');
        c.products = input.productIds.map((materialId) => {
          const m = s.materials.find((x) => x.id === materialId && x.status === 'approved' && x.expires > this.clock());
          if (!m) throw new Problem('商品资料不存在或已失效。');
          return { id: m.id, version: m.version };
        }); this.cancel(s, c.id, '商品关联变化'); this.audit(s, actor, '关联已审核资料', c.id);
      }
      c.version++; c.updatedAt = this.clock(); return c;
    });
  }
  ingest(actor, customerId, input, device, importReceipt) {
    return this.store.transact((s) => {
      const c = this.customer(s, actor, customerId);
      const text = requireText(input.text, '消息', 2000);
      if (s.messages.filter((m) => m.customerId === c.id).length >= 3000) throw new Problem('本机试点每客户最多3000条原始消息，请先清理。', 409);
      let source = input.synthetic === true && c.synthetic ? 'synthetic' : 'manual';
      let externalKey;
      if (importReceipt) {
        const r=importReceipt;
        if(device||r.kind!=='local_ocr'||!/^([a-f0-9]{64})$/.test(r.imageHash)||!Array.isArray(r.indexes)||!r.indexes.length||r.indexes.some(i=>!Number.isInteger(i)||i<0||i>=200)||new Set(r.indexes).size!==r.indexes.length)throw new Problem('本机识别来源无效。');
        const prior=s.messages.find(m=>m.customerId===c.id&&m.provenance?.kind==='local_ocr'&&m.provenance.imageHash===r.imageHash&&m.provenance.indexes.some(i=>r.indexes.includes(i)));
        if(prior){if(JSON.stringify(prior.provenance.indexes)===JSON.stringify(r.indexes)&&prior.text===text)return {...prior,duplicate:true};throw new Problem('截图中的部分文字已经导入，不能重复或改写后再次导入。',409);}
        if(c.version!==r.expectedVersion)throw new Problem('客户记录已变化，请重新识别并核对。',409);
        source='local_ocr_confirmed';externalKey=`${c.id}:local_ocr:${r.imageHash}:${r.indexes.join('.')}`;
      }
      if (device) {
        if (c.synthetic || device.kind !== 'real' || device.revoked) throw new Problem('设备与客户类型不匹配。');
        if (!c.binding || c.binding.deviceId !== device.id || input.account !== c.binding.account || input.conversation !== c.binding.conversation || input.name !== c.binding.name) throw new Problem('设备消息与客户绑定不一致。', 409);
        externalKey = `${device.id}:${requireText(input.eventId, '设备消息编号', 200)}`;
        source = 'device';
      } else if (input.eventId) externalKey = `${c.id}:manual:${requireText(input.eventId, '消息编号', 200)}`;
      if (externalKey) {
        const prior = s.messages.find((m) => m.externalKey === externalKey);
        if (prior) {
          if (prior.text !== text || prior.customerId !== c.id) throw new Problem('同一消息编号对应不同内容。', 409);
          return { ...prior, duplicate: true };
        }
      }
      this.cancel(s, c.id, '客户有新消息');
      const msg = { id: id(), customerId: c.id, direction: input.direction === 'out' && device ? 'out' : 'in', text, source, externalKey, at: this.clock() };
      if(importReceipt)msg.provenance={kind:'local_ocr',imageHash:importReceipt.imageHash,indexes:[...importReceipt.indexes],origin:importReceipt.origin==='window_crop'?'window_crop':'file',confirmedBy:actor.id,at:this.clock()};
      s.messages.push(msg); c.updatedAt = this.clock(); c.version++;
      if (msg.direction === 'out') { c.locked = true; c.lockReason = '观察到人工发言'; }
      else {
        for (const need of extractNeeds(text)) c.candidates.push({ ...need, id: id(), messageId: msg.id });
        c.pendingSince ??= this.clock(); c.processAfter = Math.min(this.clock() + 3000, c.pendingSince + 10_000);
        if (assess(text, c, s.settings).reasons.includes('停止联系')) { c.optedOut = true; c.locked = true; c.lockReason = '客户要求停止联系'; }
      }
      this.audit(s, actor, msg.direction === 'out' ? '观察到人工回复' : '接收消息', c.id, { messageId: msg.id, source });
      return msg;
    });
  }
  cancel(s, customerId, reason) {
    for (const t of s.tasks.filter((x) => x.customerId === customerId && x.status === 'queued')) { t.status = 'canceled'; t.reason = reason; }
    for (const t of s.tasks.filter((x) => x.customerId === customerId && x.status === 'executing')) {
      t.status = 'unknown'; t.reason = `${reason}，执行中的结果需要核验`;
      const c = s.customers.find((x) => x.id === customerId); if (c) { c.locked = true; c.lockReason = t.reason; }
    }
  }
  generate(actor, customerId) {
    return this.store.transact((s) => {
      const c = this.customer(s, actor, customerId); const msg = this.latest(s, c.id);
      if (!msg) throw new Problem('请先录入客户消息。');
      if(msg.source==='official_kf_test'&&s.messages.some(m=>m.direction==='out'&&m.officialReceiptKey===msg.officialReceiptKey&&m.delivery==='api_accepted'))throw new Problem('这条官方客服消息已有受理回复，请等待新消息。',409);
      const existing = s.drafts.find((x) => x.customerId === c.id && x.messageId === msg.id && x.policyVersion === s.settings.policyVersion && x.personaId === s.settings.personaId && x.customerVersion === c.version);
      if (existing) return existing;
      const decision = assess(msg.text, c, s.settings);
      const draft = { id: id(), customerId: c.id, messageId: msg.id, customerVersion: c.version,
        text: personalize(decision.reply, this.persona(s)), ...decision,
        status: 'draft', revision: 1, policyVersion: s.settings.policyVersion, personaId: s.settings.personaId,
        at: this.clock(), source: 'audited_rules', materialVersions: [...c.products] };
      draft.text = personalize(decision.reply, this.persona(s));
      s.drafts.push(draft); delete c.processAfter; delete c.pendingSince;
      if (decision.risk === 'human') {
        c.locked = true; c.lockReason = decision.reasons.join('、');
        this.cancel(s, c.id, c.lockReason);
        if (!s.reminders.some((x) => x.customerId === c.id && x.type === 'handoff' && x.status === 'open'))
          s.reminders.push({ id: id(), customerId: c.id, type: 'handoff', text: decision.nextAction, reason: c.lockReason, status: 'open', due: this.clock(), at: this.clock(), version: 1 });
      }
      this.audit(s, actor, '生成销售决策', c.id, { draftId: draft.id, risk: draft.risk, source: draft.source });
      return draft;
    });
  }
  updateDraft(actor, draftId, input) {
    return this.store.transact((s) => {
      const d = s.drafts.find((x) => x.id === draftId); if (!d) throw new Problem('草稿不存在。', 404);
      const c = this.customer(s, actor, d.customerId);
      if (input.revision !== d.revision) throw new Problem('草稿已更新，请刷新。', 409);
      if (['observed_sent', 'official_api_accepted', 'manual_marked', 'simulated', 'unknown'].includes(d.status)) throw new Problem('已处理的草稿不能覆盖修改。', 409);
      if (input.action === 'edit') {
        d.text = requireText(input.text, '最终回复', 1000); d.revision++; d.status = 'draft'; d.humanEdited = true; delete d.approval;
        this.cancel(s, c.id, '回复文字已修改');
      } else if (input.action === 'review') {
        this.fresh(s, c, d);
        requireText(d.text, '最终回复', 1000);
        if (input.textHash !== hash(d.text)) throw new Problem('审核文字摘要不一致。', 409);
        d.approval = { actor: actor.id, hash: hash(d.text), revision: d.revision, at: this.clock() };
        d.status = 'reviewed';
      } else if (input.action === 'copied') {
        this.fresh(s, c, d); this.approved(d); this.audit(s, actor, '已复制文字 不代表发送', c.id, { draftId: d.id }); return d;
      } else if (input.action === 'manual-sent') {
        this.fresh(s, c, d); this.approved(d);
        if (s.tasks.some((t) => (t.draftId === d.id || (t.customerId === c.id && t.messageId === d.messageId)) && ['executing', 'unknown', 'observed_sent'].includes(t.status))) throw new Problem('存在发送记录或结果未知，请先核验。', 409);
        this.cancel(s, c.id, '员工已标记手动发送');
        d.status = 'manual_marked'; c.locked = true; c.lockReason = '人工已回复，待确认恢复';
        s.messages.push({ id: id(), customerId: c.id, direction: 'out', text: d.text, source: 'manual_attestation', at: this.clock() });
        this.learning(s, d);
      } else throw new Problem('无效草稿动作。');
      this.audit(s, actor, ({ edit: '修改草稿', review: '精确文字审核', 'manual-sent': '人工标记已发送 未获通道回执' })[input.action], c.id, { draftId: d.id, revision: d.revision });
      return d;
    });
  }
  approved(d) {
    if (d.status !== 'reviewed' || !d.approval || d.approval.hash !== hash(d.text) || d.approval.revision !== d.revision) throw new Problem('请先核对并审核当前精确文字。', 409);
  }
  fresh(s, c, d) {
    if (d.messageId !== this.latest(s, c.id)?.id || d.customerVersion !== c.version || d.policyVersion !== s.settings.policyVersion || d.personaId !== s.settings.personaId) throw new Problem('客户、口吻或策略已变化，请重新生成。', 409);
    for (const v of d.materialVersions) if (!s.materials.some((m) => m.id === v.id && m.version === v.version && m.status === 'approved' && m.expires > this.clock())) throw new Problem('商品资料已更新或失效。', 409);
  }
  learning(s, d) {
    if (s.learning.some((x) => x.draftId === d.id)) return;
    const safe = safeLearning(d.text);
    s.learning.push({ id: id(), draftId: d.id, status: safe ? 'pending' : 'quarantined', text: safe ? d.text : '', at: this.clock() });
  }
  handoff(actor, customerId, input) {
    return this.store.transact((s) => {
      const c = this.customer(s, actor, customerId);
      if (input.action === 'resume') {
        if (c.optedOut) throw new Problem('客户已要求停止联系，不能恢复自动发言。', 409);
        if (s.tasks.some((t) => t.customerId === c.id && ['unknown', 'executing'].includes(t.status))) throw new Problem('有发送结果需要核验。', 409);
        c.locked = false; c.lockReason = ''; c.lastResumedAt = this.clock();
      } else if (input.action === 'claim') { c.locked = true; c.lockReason = '员工主动接管'; this.cancel(s, c.id, '员工主动接管'); }
      else throw new Problem('无效接管动作。');
      this.audit(s, actor, input.action === 'resume' ? '人工确认恢复' : '人工接管', c.id); return c;
    });
  }
  reminder(actor, input, reminderId) {
    return this.store.transact((s) => {
      if (!reminderId) {
        const c = this.customer(s, actor, input.customerId); const due = Date.parse(input.due);
        if (!Number.isFinite(due)) throw new Problem('请设置有效跟进时间。');
        const r = { id: id(), customerId: c.id, type: 'followup', text: requireText(input.text, '下一步', 300), due, status: 'open', at: this.clock(), version: 1 };
        s.reminders.push(r); this.audit(s, actor, '人工确认跟进任务', c.id); return r;
      }
      const r = s.reminders.find((x) => x.id === reminderId); if (!r) throw new Problem('待办不存在。', 404);
      this.customer(s, actor, r.customerId);
      if (r.version !== input.version) throw new Problem('待办已更新。', 409);
      if (input.action === 'postpone') {
        const due = Date.parse(input.due); if (!Number.isFinite(due)) throw new Problem('跟进时间无效。'); r.due = due;
      } else if (['complete', 'cancel'].includes(input.action)) r.status = input.action === 'complete' ? 'done' : 'canceled';
      else throw new Problem('无效待办动作。');
      r.version++; this.audit(s, actor, '更新待办', r.customerId, { action: input.action }); return r;
    });
  }
  localModelRuntime(actor,input){
    this.owner(actor);if(typeof input.warm!=='boolean')throw new Problem('请选择是否开启连聊加速。');
    return this.store.transact(s=>{const previous=s.settings.localModelRuntimeVersion||0;if(input.version!==previous)throw new Problem('加速设置已变化，请刷新后重试。',409);
      s.settings.localModelWarm=input.warm;s.settings.localModelRuntimeVersion=previous+1;
      this.audit(s,actor,'设置本机模型连聊加速',null,{warm:input.warm,runtimeVersion:previous+1,scope:'request_keep_alive_only'});return{warm:input.warm,version:previous+1};
    });
  }
  settings(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      const p = s.settings;
      const previousModelProvider=p.modelProvider || 'api';
      const previousVisual = JSON.stringify([p.visionProvider,p.tarsEndpoint,p.tarsKey,p.tarsModel]);
      if (input.version !== p.policyVersion) throw new Problem('策略已更新，请刷新。', 409);
      if (input.visionProvider !== undefined) {
        if (!['ui_tars','shiflow'].includes(input.visionProvider)) throw new Problem('视觉接入方式无效。');
        if (input.visionProvider !== p.visionProvider) p.mode = 'assist';
        p.visionProvider = input.visionProvider;
      }
      if(input.modelProvider!==undefined){if(!['api','codex_cli','qwen_local'].includes(input.modelProvider))throw new Problem('模型接入方式无效。');p.modelProvider=input.modelProvider;}
      for (const k of ['storeName', 'address', 'openingHours']) if (input[k] !== undefined) p[k] = String(input[k]).trim().slice(0, 200);
      for (const k of ['supplierConfirmed', 'dataConsent', 'modelDataConsent', 'infoApproved']) if (input[k] !== undefined) p[k] = input[k] === true;
      for (const k of ['dailyLimit', 'customerRetentionDays', 'maxRounds', 'perMinute', 'hoursStart', 'hoursEnd']) {
        if (input[k] === undefined) continue;
        const n = Number(input[k]); const max = k.includes('hours') ? 23 : k === 'customerRetentionDays' ? 365 : k === 'dailyLimit' ? 10000 : 10;
        if (!Number.isInteger(n) || n < (k.includes('hours') ? 0 : 1) || n > max) throw new Problem(`设置 ${k} 超出允许范围。`);
        p[k] = n;
      }
      for (const k of ['visionEndpoint', 'modelEndpoint', 'tarsEndpoint']) if (input[k] !== undefined) {
        const value = String(input[k]).trim();
        if (value) {
          let u; try { u = new URL(value); } catch { throw new Problem('接口地址无效。'); }
          if (u.protocol !== 'https:' || u.username || u.password || u.hash || u.search) throw new Problem('接口地址必须是没有凭据和查询参数的HTTPS地址。');
          if (k === 'visionEndpoint' && !/(^|\.)(thiflow|shiflow)\.com$/i.test(u.hostname)) throw new Problem('识流地址仅允许已知官方域名；企业自有域名需核实后适配。');
          if (k === 'tarsEndpoint' && (!/(^|\.)(volces\.com|volcengineapi\.com|huggingface\.cloud)$/i.test(u.hostname) || !u.pathname.endsWith('/chat/completions'))) throw new Problem('UI-TARS目前接受火山方舟或Hugging Face的HTTPS Chat Completions完整地址。');
          if (/^(localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|\[|.*\.local$)/i.test(u.hostname) || /^172\.(1[6-9]|2\d|3[01])\./.test(u.hostname)) throw new Problem('模型接口不能指向本机或内网。');
        }
        p[k] = value;
      }
      for (const k of ['visionKey', 'modelKey', 'tarsKey']) if (input[k]) p[k] = requireText(input[k], '接口密钥', 1000);
      if (input.clearVisionKey) p.visionKey = '';
      if (input.clearModelKey) p.modelKey = '';
      if (input.clearTarsKey) p.tarsKey = '';
      if (input.tarsModel !== undefined) p.tarsModel = String(input.tarsModel).trim() ? requireText(input.tarsModel, 'UI-TARS视觉模型或接入点编号', 150) : '';
      if (JSON.stringify([p.visionProvider,p.tarsEndpoint,p.tarsKey,p.tarsModel]) !== previousVisual) {
        p.mode = 'assist';
        for (const d of s.devices.filter(d => d.driver === 'ui_tars')) d.verified = false;
      }
      if (input.model !== undefined) {
        if (!/^gpt-5\.6(?:-[a-z0-9-]+)?$/.test(input.model)) throw new Problem('按当前模型要求，仅允许GPT-5.6系列，不自动切换。');
        p.model = input.model;
      }
      if (input.deviceId !== undefined) p.deviceId = String(input.deviceId);
      if (input.paused !== undefined) p.paused = input.paused === true;
      if (input.mode !== undefined) {
        if (!['observe', 'assist', 'controlled'].includes(input.mode)) throw new Problem('运行模式无效。');
        if (input.mode === 'controlled') {
          const blockers = readiness(s, this.clock()); if (blockers.length) throw new Problem(blockers.join('；'), 409);
        }
        p.mode = input.mode;
      }
      if(previousModelProvider!==(p.modelProvider || 'api'))p.modelDataConsent=false;
      p.policyVersion++;
      for (const c of s.customers) this.cancel(s, c.id, '策略更新或全局暂停');
      this.audit(s, actor, '发布运行策略', null, { version: p.policyVersion, mode: p.mode, paused: p.paused });
      return { version: p.policyVersion };
    });
  }
  user(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      if (input.id) {
        const u = s.users.find((x) => x.id === input.id && x.role === 'employee');
        if (!u) throw new Problem('员工不存在。');
        if (input.password) { u.password = passwordHash(input.password); u.mustChangePassword = true; }
        if (typeof input.active === 'boolean') u.active = input.active;
        s.sessions = s.sessions.filter((x) => x.userId !== u.id);
        for (const c of s.customers.filter((c) => c.assigneeId === u.id)) { this.cancel(s, c.id, '员工账号变更'); c.locked = true; c.lockReason = '员工账号变更'; }
        this.audit(s, actor, '变更员工账号'); return { id: u.id };
      }
      const username = requireText(input.username, '员工账号', 50);
      if (s.users.some((x) => x.username === username)) throw new Problem('账号已存在。', 409);
      const u = { id: id(), username, name: requireText(input.name, '员工称呼', 40), password: passwordHash(input.password), role: 'employee', active: true, mustChangePassword: true };
      s.users.push(u); this.audit(s, actor, '创建员工账号'); return { id: u.id };
    });
  }
  material(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      if (input.action === 'revoke') {
        const m = s.materials.find((x) => x.id === input.id); if (!m) throw new Problem('资料不存在。');
        m.status = 'withdrawn'; m.version++;
        for (const c of s.customers.filter((x) => x.products.some((v) => v.id === m.id))) this.cancel(s, c.id, '资料撤回');
        this.audit(s, actor, '撤回专业资料'); return m;
      }
      const expires = Date.parse(input.expires); if (!Number.isFinite(expires) || expires <= this.clock()) throw new Problem('资料有效期必须在未来。');
      const m = { id: id(), title: requireText(input.title, '资料名称', 100), code: requireText(input.code, '商品或政策编号', 80), body: requireText(input.body, '已核实正文', 4000), source: requireText(input.source, '核实依据', 500), expires, version: 1, status: 'approved', approvedBy: actor.id, at: this.clock() };
      s.materials.push(m); this.audit(s, actor, '批准专业资料'); return m;
    });
  }
  personaPublish(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      if (input.action === 'evaluate') {
        const p = s.personas.find((x) => x.id === input.id); if (!p) throw new Problem('口吻版本不存在。');
        if (!Array.isArray(input.votes) || input.votes.length !== 20 || !input.votes.every((x) => typeof x === 'boolean') || input.noFactErrors !== true) throw new Problem('请逐题填写20题盲测，并核实没有事实错误。');
        p.verified = input.votes.filter(Boolean).length >= 16;
        p.evaluation = { votes: input.votes, noFactErrors: true, reviewer: actor.id, at: this.clock(), source: 'human_recorded' };
        s.settings.policyVersion++; this.audit(s, actor, '录入人工口吻盲测', null, { personaId: p.id, verified: p.verified }); return p;
      }
      if (input.action === 'activate') {
        const p = s.personas.find((x) => x.id === input.id); if (!p) throw new Problem('口吻版本不存在。');
        s.settings.personaId = p.id; s.settings.policyVersion++;
        for (const c of s.customers) this.cancel(s, c.id, '口吻版本切换');
        this.audit(s, actor, '启用或回滚口吻', null, { personaId: p.id }); return p;
      }
      const samples = requireText(input.samples, '已获授权的本人样本', 12000).split('\n').map((x) => x.trim()).filter(Boolean);
      if (samples.length > 100 || !samples.every(safeLearning)) throw new Problem('样本含联系方式、交易事实或承诺，请先清理。');
      if (!['您好', '你好', '亲', '您'].includes(input.greeting)) throw new Problem('请选择支持的称呼。');
      const p = { id: id(), name: requireText(input.name, '分身名称', 60), greeting: input.greeting, samples, version: s.personas.length + 1, verified: false, at: this.clock(), approvedBy: actor.id };
      s.personas.push(p); s.settings.personaId = p.id; s.settings.policyVersion++;
      for (const c of s.customers) this.cancel(s, c.id, '口吻版本发布');
      this.audit(s, actor, '发布口吻版本 待评测', null, { personaId: p.id }); return p;
    });
  }
  learningReview(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      const l = s.learning.find((x) => x.id === input.id); if (!l) throw new Problem('学习候选不存在。');
      if (l.status !== 'pending') throw new Problem('此候选不在待审核区。', 409);
      if (input.action === 'reject') l.status = 'rejected';
      else if (input.action === 'accept') {
        if (!safeLearning(l.text)) throw new Problem('候选未通过隐私和事实检查。');
        const old = this.persona(s); if (!old) throw new Problem('请先建立口吻。');
        const p = { ...old, id: id(), samples: [...old.samples, l.text], version: s.personas.length + 1, verified: false, at: this.clock() };
        delete p.evaluation; s.personas.push(p); l.status = 'accepted'; s.settings.personaId = p.id; s.settings.policyVersion++;
        for (const c of s.customers) this.cancel(s, c.id, '学习发布新口吻');
      } else throw new Problem('学习动作无效。');
      this.audit(s, actor, '审核学习候选'); return l;
    });
  }
  registerDevice(actor, input) {
    this.owner(actor); const token = randomBytes(32).toString('base64url');
    return this.store.transact((s) => {
      if (s.devices.filter((x) => !x.revoked).length >= 5) throw new Problem('本机试点最多注册5个设备。');
      const device = { id: id(), name: requireText(input.name, '设备名称', 60), tokenHash: hash(token), kind: input.kind === 'real' ? 'real' : 'fixture', verified: false, lastSeen: 0, revoked: false };
      s.devices.push(device); this.audit(s, actor, '注册执行设备'); return { id: device.id, token, kind: device.kind };
    });
  }
  device(token) {
    const d = this.store.read().devices.find((x) => x.tokenHash === hash(token || '') && !x.revoked);
    if (!d) throw new Problem('设备凭据无效。', 401); return d;
  }
  deviceUpdate(actor, input) {
    this.owner(actor);
    return this.store.transact((s) => {
      const d = s.devices.find((x) => x.id === input.id); if (!d) throw new Problem('设备不存在。');
      if (input.action === 'revoke') d.revoked = true;
      else if (input.action === 'verify') {
        if (d.kind !== 'real' || input.confirmed !== true) throw new Problem('模拟设备不能通过真机验收。');
        d.verified = true; d.verification = requireText(input.evidence, '真机测试报告位置', 300);
      } else throw new Problem('设备动作无效。');
      s.settings.policyVersion++; for (const c of s.customers) this.cancel(s, c.id, '设备授权变化');
      this.audit(s, actor, '更新设备授权'); return { id: d.id };
    });
  }
  heartbeat(device, input) {
    return this.store.transact((s) => {
      const d = s.devices.find((x) => x.id === device.id && !x.revoked); if (!d) throw new Problem('设备已撤销。', 401);
      d.lastSeen = this.clock(); d.paused = input.paused === true;
      if (Number.isInteger(input.ackPolicyVersion) && input.ackPolicyVersion === s.settings.policyVersion) d.ackPolicyVersion = input.ackPolicyVersion;
      return { mode: s.settings.mode, paused: s.settings.paused, policyVersion: s.settings.policyVersion };
    });
  }
  queue(actor, draftId) {
    return this.store.transact((s) => {
      const d = s.drafts.find((x) => x.id === draftId); if (!d) throw new Problem('草稿不存在。');
      const c = this.customer(s, actor, d.customerId); this.fresh(s, c, d);
      const prior = s.tasks.find((x) => x.draftId === d.id || (x.customerId === c.id && x.messageId === d.messageId)); if (prior) return prior;
      if (d.status !== 'draft' || s.drafts.some((x) => x.customerId === c.id && x.messageId === d.messageId && ['manual_marked','observed_sent'].includes(x.status))) throw new Problem('这轮消息已经处理，不能再次自动发言。', 409);
      const blockers = readiness(s, this.clock());
      if (blockers.length) throw new Problem(blockers.join('；'), 409);
      if (s.settings.paused || c.locked || c.optedOut || c.synthetic || !c.binding || c.binding.deviceId !== s.settings.deviceId || s.settings.mode !== 'controlled' || this.latest(s, c.id)?.source !== 'device') throw new Problem('当前会话不能自动发送；仅经过绑定校验的设备消息可触发。', 409);
      if (d.risk !== 'low' || d.humanEdited || mandatoryTopics(d.text).length || d.text !== personalize(assess(this.latest(s, c.id).text, c, s.settings).reply, this.persona(s))) throw new Problem('此文字不符合受控自动白名单。', 409);
      const hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: s.settings.timezone, hour: '2-digit', hourCycle: 'h23' }).format(this.clock()));
      const start = s.settings.hoursStart, end = s.settings.hoursEnd;
      const open = start === end || (start < end ? hour >= start && hour < end : hour >= start || hour < end);
      if (!open) throw new Problem('当前不在自动接待时段。', 409);
      const today = new Intl.DateTimeFormat('en-CA', { timeZone: s.settings.timezone }).format(this.clock());
      const realTasks = s.tasks.filter((x) => !x.simulation && !['canceled', 'failed'].includes(x.status));
      if (realTasks.filter((x) => x.day === today).length >= s.settings.dailyLimit) throw new Problem('已达到当天自动发送硬上限。', 409);
      if (realTasks.filter((x) => x.customerId === c.id && this.clock() - x.at < 60_000).length >= s.settings.perMinute || realTasks.filter((x) => x.customerId === c.id && x.at >= (c.lastResumedAt || 0)).length >= s.settings.maxRounds) throw new Problem('会话已达到频次或连续轮数限制，请人工接管。', 409);
      const t = { id: id(), customerId: c.id, messageId: d.messageId, draftId: d.id, revision: d.revision, hash: hash(d.text), deviceId: c.binding.deviceId, binding: { ...c.binding }, status: 'queued', at: this.clock(), expires: this.clock() + 60_000, day: today, policyVersion: s.settings.policyVersion, simulation: false };
      s.tasks.push(t); this.audit(s, actor, '创建真实发送任务 尚未发送', c.id, { taskId: t.id }); return t;
    });
  }
  claimJob(device, input) {
    return this.store.transact((s) => {
      if (s.settings.paused || s.settings.mode !== 'controlled' || readiness(s, this.clock()).length) return null;
      const task = s.tasks.find((t) => t.deviceId === device.id && t.status === 'queued'); if (!task) return null;
      const c = s.customers.find((x) => x.id === task.customerId); const d = s.drafts.find((x) => x.id === task.draftId);
      if (!c || c.locked || c.optedOut || task.expires < this.clock() || task.policyVersion !== s.settings.policyVersion) { task.status = 'canceled'; return null; }
      this.fresh(s, c, d);
      if (input.account !== c.binding.account || input.conversation !== c.binding.conversation || input.name !== c.binding.name || input.latestTextHash !== hash(this.latest(s, c.id).text) || input.inputEmpty !== true || input.humanActive !== false) return null;
      task.status = 'executing'; task.started = this.clock(); task.grant = randomBytes(24).toString('hex');
      this.audit(s, null, '设备领取发送任务', c.id, { taskId: task.id });
      return { taskId: task.id, target: task.binding, text: d.text, hash: task.hash, grant: task.grant, grantExpires: this.clock() + 3000, policyVersion: task.policyVersion };
    });
  }
  authorizeJob(device, input) {
    return this.store.transact((s) => {
      const t = s.tasks.find((x) => x.id === input.taskId && x.deviceId === device.id);
      if (!t || t.status !== 'executing' || t.authorizedAt || t.grant !== input.grant || this.clock() - t.started > 3000) throw new Problem('执行授权已失效或已使用。', 409);
      const c = s.customers.find((x) => x.id === t.customerId); const d = s.drafts.find((x) => x.id === t.draftId);
      if (s.settings.paused || c.locked || t.policyVersion !== s.settings.policyVersion || readiness(s, this.clock()).length || input.account !== c.binding.account || input.conversation !== c.binding.conversation || input.name !== c.binding.name || input.latestTextHash !== hash(this.latest(s, c.id).text) || input.inputEmpty !== true || input.humanActive !== false) throw new Problem('目标窗口变化或会话暂停。', 409);
      this.fresh(s, c, d); t.authorizedAt = this.clock(); return { authorized: true, grantExpires: t.started + 3000 };
    });
  }
  finishJob(device, input) {
    return this.store.transact((s) => {
      const t = s.tasks.find((x) => x.id === input.taskId && x.deviceId === device.id); if (!t) throw new Problem('发送任务不存在。', 404);
      if (!['observed_sent', 'failed', 'unknown'].includes(input.outcome)) throw new Problem('结果状态无效。');
      if (t.status === input.outcome) return t;
      if (!['executing', 'unknown'].includes(t.status) || t.grant !== input.grant) throw new Problem('任务状态不允许写入此结果。', 409);
      if (input.outcome === 'observed_sent' && (!t.authorizedAt || !/^[a-f0-9]{64}$/.test(input.evidenceHash || '') || input.textHash !== t.hash)) throw new Problem('缺少发送前授权或发送后核验依据。');
      const d = s.drafts.find((x) => x.id === t.draftId); const c = s.customers.find((x) => x.id === t.customerId);
      t.status = input.outcome; t.finished = this.clock(); t.evidenceHash = input.evidenceHash || ''; d.status = input.outcome;
      if (input.outcome === 'observed_sent') s.messages.push({ id: id(), customerId: c.id, direction: 'out', text: d.text, source: 'device_observed', taskId: t.id, at: this.clock() });
      else { c.locked = true; c.lockReason = input.outcome === 'unknown' ? '发送结果未知，请人工核验' : '设备报告发送失败'; }
      this.audit(s, null, '设备报告本机执行结果', c.id, { taskId: t.id, outcome: input.outcome }); return t;
    });
  }
  simulate(actor, draftId, input) {
    return this.store.transact((s) => {
      const d = s.drafts.find((x) => x.id === draftId); if (!d) throw new Problem('草稿不存在。');
      const c = this.customer(s, actor, d.customerId);
      if (!c.synthetic || s.messages.find((m) => m.id === d.messageId)?.source !== 'synthetic') throw new Problem('仅演练客户和演练消息可以模拟。');
      const existing = s.tasks.find((x) => x.draftId === d.id || (x.customerId === c.id && x.messageId === d.messageId)); if (existing) return existing;
      this.fresh(s, c, d);
      if (c.locked || c.optedOut || s.settings.paused || d.risk !== 'low' || d.humanEdited) throw new Problem('当前场景不能模拟自动回复，请人工处理。', 409);
      if (!['success', 'failure', 'unknown'].includes(input.outcome)) throw new Problem('请选择演练结果。');
      const t = { id: id(), customerId: c.id, messageId: d.messageId, draftId: d.id, status: input.outcome === 'success' ? 'simulated' : input.outcome === 'failure' ? 'failed' : 'unknown', simulation: true, at: this.clock(), hash: hash(d.text) };
      s.tasks.push(t); d.status = t.status;
      if (t.status === 'simulated') s.messages.push({ id: id(), customerId: c.id, direction: 'out', text: d.text, source: 'simulation_not_sent', at: this.clock() });
      else { c.locked = true; c.lockReason = t.status === 'unknown' ? '演练发送结果未知' : '演练发送失败'; }
      this.audit(s, actor, '模拟执行 未调用微信', c.id, { taskId: t.id, outcome: t.status }); return t;
    });
  }
  resolveTask(actor, input) {
    return this.store.transact((s) => {
      const t = s.tasks.find((x) => x.id === input.id); if (!t) throw new Problem('任务不存在。');
      this.customer(s, actor, t.customerId);
      if (t.status !== 'unknown') throw new Problem('此任务无需核验。', 409);
      if (!['sent', 'not_sent'].includes(input.result)) throw new Problem('请确认人工核验结果。');
      t.status = 'human_resolved'; t.resolution = input.result; t.reason = requireText(input.reason, '核验依据', 300);
      this.audit(s, actor, '人工核验未知发送', t.customerId, { taskId: t.id, result: t.resolution }); return t;
    });
  }
  tick() {
    const s = this.store.read();
    const retentionDay = new Intl.DateTimeFormat('en-CA', { timeZone: s.settings.timezone }).format(this.clock());
    if (s.settings.lastCleanupDay !== retentionDay) {
      const owner = s.users.find((u) => u.role === 'owner' && u.active);
      if (owner) { this.cleanup(owner); this.store.transact((next) => { next.settings.lastCleanupDay = retentionDay; }); }
    }
    for (const c of s.customers.filter((c) => !c.deletedAt && c.processAfter && c.processAfter <= this.clock())) {
      const actor = s.users.find((u) => u.id === c.assigneeId && u.active);
      if (!actor) continue;
      try {
        const d = this.generate(actor, c.id);
        if (s.settings.mode === 'controlled' && !s.settings.paused && !c.locked) this.queue(actor, d.id);
      } catch (e) {
        if (!(e instanceof Problem)) throw e;
        this.store.transact((next) => { const current = next.customers.find((x) => x.id === c.id); if (current) { delete current.processAfter; delete current.pendingSince; } });
      }
    }
    if (s.tasks.some((t) => t.status === 'executing' && this.clock() - t.started > 30_000) || s.tasks.some((t) => t.status === 'queued' && t.expires < this.clock())) {
      this.store.transact((next) => {
        for (const t of next.tasks) {
          if (t.status === 'executing' && this.clock() - t.started > 30_000) {
            t.status = 'unknown'; const c = next.customers.find((x) => x.id === t.customerId);
            if (c) { c.locked = true; c.lockReason = '设备执行超时，结果需要人工核验'; }
          }
          if (t.status === 'queued' && t.expires < this.clock()) t.status = 'canceled';
        }
      });
    }
  }
  cleanup(actor) {
    this.owner(actor);
    const result = this.store.transact((s) => {
      const cutoff = this.clock() - 30 * 86400_000;
      const expiredCustomers = s.customers.filter((c) => !c.deletedAt && this.clock() - c.updatedAt > s.settings.customerRetentionDays * 86400_000).map((c) => c.id);
      for (const customerId of expiredCustomers) {
        const c = s.customers.find((x) => x.id === customerId);
        this.cancel(s, customerId, '客户主档到期'); c.alias = '[主档已到期清理]'; c.binding = null; c.needs = []; c.candidates = []; c.deletedAt = this.clock();
      }
      let removed = 0;
      const expired = (x) => x.at < cutoff || expiredCustomers.includes(x.customerId);
      s.messages = s.messages.filter((x) => { if (expired(x)) { removed++; return false; } return true; });
      const expiredDraftIds = new Set(s.drafts.filter(expired).map((x) => x.id));
      for (const task of s.tasks.filter((x) => expiredDraftIds.has(x.draftId))) {
        if (['queued','executing'].includes(task.status)) task.status = 'canceled';
        task.binding = null; delete task.grant;
      }
      s.drafts = s.drafts.filter((x) => !expired(x));
      const messageIds = new Set(s.messages.map((x) => x.id));
      for (const c of s.customers) c.candidates = c.candidates.filter((x) => messageIds.has(x.messageId));
      for (const r of s.reminders) if (r.at < cutoff || expiredCustomers.includes(r.customerId)) { r.text = '[原始内容已到期清理]'; r.reason = ''; if (expiredCustomers.includes(r.customerId)) r.status = 'canceled'; }
      for (const e of s.audit) if (e.at < cutoff) e.details = {};
      s.learning = s.learning.filter((x) => ['accepted','rejected'].includes(x.status) || x.at >= cutoff);
      s.audit = s.audit.filter((x) => x.at >= this.clock() - 365 * 86400_000);
      s.tasks = s.tasks.filter((x) => x.at >= this.clock() - 365 * 86400_000);
      s.costs = s.costs.filter((x) => x.at >= this.clock() - 180 * 86400_000);
      s.sessions = s.sessions.filter((x) => x.expires > this.clock());
      this.audit(s, actor, '执行原始资料到期清理', null, { removed, customerCount: expiredCustomers.length }); return { removed, customers: expiredCustomers.length };
    }); this.store.checkpoint(); return result;
  }
}
