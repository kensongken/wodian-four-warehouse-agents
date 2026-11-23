import { createHash } from 'node:crypto';

export const stages = ['discover', 'explain', 'evaluate', 'advance', 'follow_up'];
export const stageNames = { discover: '发现需求', explain: '介绍说明', evaluate: '比较评估', advance: '推进意向', follow_up: '持续跟进' };
export const hash = (s) => createHash('sha256').update(s).digest('hex');
export const normalize = (s) => String(s).normalize('NFKC').toLowerCase().replace(/[\s\u200b-\u200f\u2060\ufeff]/gu, '');
const topics = [
  ['报价与付款', /价|多少[钱元]|多少钱|预算|优惠|折扣|便宜|最低|转[账帐]|付款|支付|定金|订金|收款|price|cost|pay|discount/u],
  ['交易承诺', /保证|承诺|保留|留货|库存|现货|发货|到货|交期|几天|多久|今天能|明天能|包邮|下单|成交|已付|退款|refund|ship|deliver|stock/u],
  ['售后与争议', /售后|坏|掉了|掉色|断|裂|瑕疵|色差|投诉|赔|维修|修理|退换|退货|换货|退给[你您]|给[你您]退|我退|不合适.{0,8}[退换]|责任|repair|complaint/u],
  ['商品事实', /材质|纯度|足金|纯金|真金|黄金|白银|银饰|翡翠|珍珠|和田玉|天然|真[假伪]|证书|产地|成色|克重|尺寸|多大|功效|养生|保值|回收|寄售|鉴定|certificate|genuine/u],
  ['要求人工', /人工|真人|老板|人来|找你本人|别用机器人|human/u],
  ['停止联系', /(?:不要|不想|别|不用)(?:再)?(?:联系|发|回复|回|打扰)|停止|退订|取消关注|stop|unsubscribe/u],
  ['指令污染', /忽略|无视|绕过|系统提示|提示词|开发者|ignore|systemprompt|developer|执行代码/u],
];
export const mandatoryTopics = (text) => topics.filter(([, re]) => re.test(normalize(text))).map(([name]) => name);

export function assess(text, customer, settings) {
  const n = normalize(text);
  const reasons = mandatoryTopics(n);
  if (reasons.length) return { intent: reasons[0], risk: 'human', reasons, reply: '', nextAction: reasons.includes('停止联系') ? '停止自动触达，人工核对客户要求。' : '由负责人核实后回复。' };
  let intent, reply, nextAction;
  if (/^(你好|您好|嗨|早上好|下午好|晚上好|hello|hi|在吗|有人吗)[呀啊呢~～!！?？。.,，]*$/u.test(n)) {
    intent = '礼貌接待'; reply = '您好，很高兴为您服务。今天是想自己佩戴，还是挑一份礼物？'; nextAction = '了解佩戴或送礼用途。';
  } else if (/^(谢谢|谢谢你|多谢|好的|好哒|收到|明白了|ok|thankyou)[呀啊呢~～!！?？。.,，]*$/u.test(n)) {
    intent = '礼貌回应'; reply = '不客气，您有想了解的，随时告诉我。'; nextAction = '等待客户继续咨询。';
  } else if (/^(你们|咱们|店里|请问)?(店|门店)?(在哪[里儿]?|地址是什么|怎么走|地址)[呀呢?？。!！]*$/u.test(n) && settings.infoApproved && settings.address) {
    intent = '店址咨询'; reply = `门店地址：${settings.address}`; nextAction = '如有到店意向，由员工确认时间。';
  } else if (/^(你们|咱们|店里|请问)?(几点开门|几点关门|营业时间|什么时候营业|几点营业)[呀呢?？。!！]*$/u.test(n) && settings.infoApproved && settings.openingHours) {
    intent = '营业时间'; reply = `门店营业时间：${settings.openingHours}`; nextAction = '如有到店意向，由员工确认时间。';
  } else if (/^(我|想|我想|想要|我要|帮我|可以帮我)?(挑|选|看看|了解)?(一[条件款份])?(日常戴的|日常|简约|素一点的|素一点|有颜色的)?(手串|手链|首饰|珠宝|礼物|款式)(吗|呢|呀|吧|推荐一下|推荐)?[?？。!！]*$/u.test(n)) {
    intent = '需求了解'; reply = hasNeed(customer, '用途') ? '您平时更喜欢素一点的，还是有些颜色的？' : '您是想自己日常佩戴，还是挑一份礼物？'; nextAction = '记录本轮用途或风格偏好，交员工确认。';
  } else if (/^(自用|自己戴|送礼|送给妈妈|送妈妈|送给朋友|送朋友|素一点|素一点的|简约|日常戴|喜欢素一点|有颜色的)[呀呢吧?？。!！]*$/u.test(n)) {
    intent = '偏好澄清'; reply = hasNeed(customer, '风格') ? '明白，接下来我请同事结合您的需求帮您看合适的款式。' : '了解，您更喜欢素一点的，还是有些颜色的？'; nextAction = '确认需求候选并关联商品。';
  } else {
    return { intent: '需要确认', risk: 'human', reasons: ['不在自动回复白名单'], reply: '', nextAction: '查看客户原话，补充资料后人工回复。' };
  }
  return { intent, risk: 'low', reasons: [], reply, nextAction };
}

function hasNeed(customer, name) {
  return [...(customer.needs || []), ...(customer.candidates || [])].some((x) => x.kind === name);
}

export function extractNeeds(text) {
  const n = normalize(text); const values = [];
  if (/送妈妈|送给妈妈/u.test(n)) values.push({ kind: '用途', value: '送给妈妈' });
  else if (/送礼|送朋友|送给朋友/u.test(n)) values.push({ kind: '用途', value: '送礼' });
  else if (/自用|自己戴|自己佩戴/u.test(n)) values.push({ kind: '用途', value: '自己佩戴' });
  if (/素一点|简约/u.test(n)) values.push({ kind: '风格', value: '简约素雅' });
  if (/有颜色/u.test(n)) values.push({ kind: '风格', value: '有颜色' });
  if(/手链/u.test(n))values.push({kind:'款式',value:'手链'});
  else if(/手串/u.test(n))values.push({kind:'款式',value:'手串'});
  if(/小巧/u.test(n))values.push({kind:'佩戴感',value:'小巧'});
  else if(/存在感强/u.test(n))values.push({kind:'佩戴感',value:'存在感强'});
  if(/日常/u.test(n))values.push({kind:'场景',value:'日常佩戴'});
  else if(/聚会/u.test(n))values.push({kind:'场景',value:'聚会搭配'});
  return values;
}

/** Style only changes approved greetings; facts cannot be rewritten by a model. */
export function personalize(reply, persona) {
  if (!reply) return '';
  const greeting = ['您好', '你好', '亲', '您'].includes(persona?.greeting) ? persona.greeting : '您好';
  return reply.replace(/^您好/u, greeting);
}

export function safeLearning(text) {
  if (text.length > 1000 || /(?:\+?\d[\s-]?){7,}|@|微信|wechat|\bwx\b|手机号|电话|收货|地址|身份证|银行卡|[\u4e00-\u9fff]{1,4}(先生|女士)/iu.test(text.normalize('NFKC'))) return false;
  return !topics.slice(0, 4).some(([, re]) => re.test(normalize(text)));
}

export function readiness(state, now = Date.now()) {
  const s = state.settings;
  const device = state.devices.find((d) => d.id === s.deviceId);
  const persona = state.personas.find((p) => p.id === s.personaId);
  const tars = s.visionProvider === 'ui_tars';
  return [
    [tars ? Boolean(s.tarsEndpoint && s.tarsKey && s.tarsModel) : Boolean(s.visionEndpoint && s.visionKey), tars ? 'UI-TARS视觉接口、密钥或模型未配置' : '识流企业接口与密钥未配置'],
    [tars || s.supplierConfirmed === true, '供应商持续服务尚未确认'],
    [s.dataConsent === true, '真实资料用途尚未确认'],
    [Boolean(device && device.verified && !device.revoked), '执行设备未完成真机验收'],
    [Boolean(device && device.kind === 'real' && now - device.lastSeen < 15_000), '真实执行设备未在线'],
    [Boolean(persona?.verified), '个人口吻尚未通过20题人工盲测'],
    [s.customerRetentionDays > 0, '客户主档保存期限未配置'],
  ].filter(([ok]) => !ok).map(([, reason]) => reason);
}
