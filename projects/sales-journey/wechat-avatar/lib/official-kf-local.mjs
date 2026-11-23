import {readFileSync,statSync} from 'node:fs';
import {parseEnv} from 'node:util';
import {Problem} from './engine.mjs';
import {hash,assess,mandatoryTopics,normalize} from './policy.mjs';
import {discoveryPlan} from './official-kf-discovery.mjs';
import {LocalQwenKfReply} from './qwen-kf-reply.mjs';

export function loadKfConfig(path=process.env.AVATAR_KF_ENV_FILE){
  if(!path)throw new Problem('尚未指定本机微信客服配置文件。',409);
  if((statSync(path).mode&0o777)!==0o600)throw new Problem('微信客服配置文件权限须为600。',409);
  const e=parseEnv(readFileSync(path,'utf8'));
  const config={corpId:e.WECOM_KF_CORP_ID,secret:e.WECOM_KF_SECRET,openKfId:e.WECOM_KF_OPEN_KFID,testUser:e.WECOM_KF_TEST_EXTERNAL_USER_ID};
  if(Object.values(config).some(v=>!v?.trim()))throw new Problem('缺少微信客服账号或本人测试微信配置。',409);
  return config;
}

export class OfficialKfAPI {
  constructor(config,{request=fetch}={}){this.config=config;this.request=request;this.token=null;}
  async json(url,options={}){
    const r=await this.request(url,{...options,signal:AbortSignal.timeout(10000),redirect:'error'});
    const text=await r.text();if(text.length>2_000_000)throw new Problem('微信客服返回过大，已停止。',502);
    let b;try{b=JSON.parse(text);}catch{throw new Problem('微信客服返回格式无效，已停止。',502);}
    if(!r.ok||b.errcode!==0){const error=new Problem(`微信客服接口未通过（${Number.isInteger(b.errcode)?b.errcode:'无有效状态'}）。`,502);error.providerCode=b.errcode;throw error;}
    return b;
  }
  async call(path,body){
    if(!['sync_msg','service_state/get','send_msg'].includes(path))throw new Problem('客服接口不在允许范围。',403);
    if(!this.token||this.token.until<Date.now()+60000){
      const u=new URL('https://qyapi.weixin.qq.com/cgi-bin/gettoken');u.searchParams.set('corpid',this.config.corpId);u.searchParams.set('corpsecret',this.config.secret);
      const b=await this.json(u);if(typeof b.access_token!=='string')throw new Problem('微信凭证无效。',502);
      this.token={value:b.access_token,until:Date.now()+Math.min(7200,Number(b.expires_in)||60)*1000};
    }
    const u=new URL('https://qyapi.weixin.qq.com/cgi-bin/kf/'+path);u.searchParams.set('access_token',this.token.value);
    return this.json(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  }
  async page(cursor=''){return this.call('sync_msg',{open_kfid:this.config.openKfId,cursor,limit:1000});}
  async session(){return this.call('service_state/get',{open_kfid:this.config.openKfId,external_userid:this.config.testUser});}
  async send(text,msgid){return this.call('send_msg',{open_kfid:this.config.openKfId,touser:this.config.testUser,msgid,msgtype:'text',text:{content:text}});}
}

// This first pilot is strictly bound to the owner's configured test WeChat.
// Credentials never enter the browser or the encrypted customer ledger.
export class OfficialKfLocal {
  constructor(engine,{load=loadKfConfig,apiFactory=c=>new OfficialKfAPI(c),reply=new LocalQwenKfReply(),clock=()=>Date.now(),interval=10000,journal}={}){
    Object.assign(this,{engine,load,apiFactory,reply,clock,interval,journal});this.running=false;this.busy=false;this.epoch=0;this.timer=null;this.active=null;this.lastError='';
    if(this.engine.store.read().officialKf)this.update(s=>{for(const r of s.receipts){if(r.status==='sending'){r.status='unknown';r.reason='上次发送期间服务中断，需人工核实';}else if(r.status==='processing'){r.status='human';r.reason='上次生成期间服务中断，未自动重试';}}});
  }
  state(){return this.engine.store.read().officialKf||{receipts:[]};}
  update(fn){return this.engine.store.transact(s=>{s.officialKf??={receipts:[]};return fn(s.officialKf);});}
  quota(){
    const now=this.clock(),day=t=>new Date(t+8*3600000).toISOString().slice(0,10);
    const used=this.state().receipts.filter(r=>r.status==='api_accepted'&&(this.dailyTest?day(r.acceptedAt??r.at)===day(now):(r.acceptedAt??r.at)>now-48*3600000)).length;
    if(this.unlimitedTest)return {period:'unlimited',limit:null,used:this.state().receipts.filter(r=>r.status==='api_accepted').length,remaining:null,day:null};
    const limit=this.dailyTest?20:3;return {period:this.dailyTest?'taipei_day':'rolling_48h',limit,used,remaining:Math.max(0,limit-used),day:this.dailyTest?day(now):null};
  }
  quotaReason(){return this.dailyTest?'本人测试今日20条额度已用完，接待已暂停；不自动补发。':'本人测试48小时3条额度已用完，接待已暂停；不自动补发。';}
  status(actor){this.engine.owner(actor);const s=this.state();return {running:this.running,busy:this.busy,observationOnly:!!this.observationOnly,discovery:!!this.discovery,quota:this.quota(),humanLocked:!!s.humanLocked,scope:'owner_test_only',provider:'qwen_local',sessionCheckMode:this.exclusiveTest?'owner_exclusive_test':'official',lastError:this.lastError,receipts:s.receipts.slice(-30).map(({key,at,status,reason,text,customerId})=>({key,at,status,reason,text,customerId})),startedAt:this.startedAt||null};}
  async observe(actor,{confirmed}={}){
    this.engine.owner(actor);if(confirmed!==true)throw new Problem('请确认观察本人测试微信。',409);
    if(!this.journal)throw new Problem('客户档案同步组件不可用。',409);
    if(this.running||this.busy)throw new Problem('请先暂停当前接待。',409);
    if(this.engine.store.read().settings.paused)throw new Problem('全局暂停中。',409);
    const config=this.load();this.active={config,api:this.apiFactory(config),binding:hash(config.corpId+'|'+config.openKfId+'|'+config.testUser)};
    this.actorId=actor.id;this.discovery=false;this.observationOnly=true;this.exclusiveTest=false;this.startedAt=this.clock();this.epoch++;this.running=true;this.lastError='';
    this.timer=setInterval(()=>void this.tick(),this.interval);this.timer.unref();return this.status(actor);
  }
  async start(actor,{confirmed,exclusiveTestConfirmed=false,discoveryConfirmed=false,dailyTestConfirmed=false,unlimitedTestConfirmed=false}={}){
    this.engine.owner(actor);if(confirmed!==true)throw new Problem('须明确确认仅向本人的测试微信自动回复。',409);
    if(this.running||this.busy)throw new Problem('微信客服正在运行或收尾，请先暂停并等待。',409);
    if(this.state().receipts.some(r=>['sending','unknown'].includes(r.status)))throw new Problem('存在发送结果待核实，不能自动重新启动。',409);
    if(this.state().humanLocked)throw new Problem('本人测试处于人工接管锁定，请先核对待处理问题，再手动解除测试人工锁。',409);
    const state=this.engine.store.read();if(state.settings.paused)throw new Problem('全局暂停中，不能启动。',409);
    if(!state.settings.modelDataConsent||state.settings.modelProvider!=='qwen_local')throw new Problem('请先选择本机千问并确认本机处理。',409);
    if(dailyTestConfirmed===true&&discoveryConfirmed!==true)throw new Problem('每天20条仅用于明确确认的本人需求自动接待。',409);
    if(unlimitedTestConfirmed===true&&discoveryConfirmed!==true)throw new Problem('无条数上限仅用于明确确认的本人需求自动接待。',409);
    this.unlimitedTest=unlimitedTestConfirmed===true;
    this.dailyTest=dailyTestConfirmed===true;
    if(this.quota().remaining===0)throw new Problem(this.quotaReason(),409);
    const startEpoch=++this.epoch;this.busy=true;this.exclusiveTest=false;this.observationOnly=false;this.discovery=discoveryConfirmed===true;this.actorId=actor.id;
    try{
      const config=this.load();this.active={config,api:this.apiFactory(config),binding:hash(config.corpId+'|'+config.openKfId+'|'+config.testUser)};
      try{
        const session=await this.active.api.session();if(!Number.isInteger(session.service_state))throw new Problem('微信会话状态无效。',502);
      }catch(error){
        if(error?.providerCode!==48002||exclusiveTestConfirmed!==true)throw error;
        this.exclusiveTest=true;
      }
      if(this.epoch!==startEpoch||this.engine.store.read().settings.paused)throw new Problem('启动期间已暂停。',409);
    }catch(error){this.lastError=error instanceof Problem?error.message:'启动检查失败，接待保持暂停。';throw error;}finally{this.busy=false;}
    this.startedAt=this.clock();this.running=true;this.lastError='';
    this.timer=setInterval(()=>void this.tick(),this.interval);this.timer.unref();
    return this.status(actor);
  }
  stop(actor){if(actor)this.engine.owner(actor);this.running=false;this.epoch++;clearInterval(this.timer);this.timer=null;this.reply.close();return actor?this.status(actor):undefined;}
  eligible(epoch,binding,refs){
    if(!this.running||this.epoch!==epoch||this.active?.binding!==binding)return false;
    const s=this.engine.store.read();if(s.settings.paused||(!this.observationOnly&&(s.settings.modelProvider!=='qwen_local'||!s.settings.modelDataConsent)))return false;
    if(this.journal&&!this.observationOnly){const c=s.customers.find(c=>c.officialKf?.binding===binding);if(c&&(c.deletedAt||c.locked||c.assigneeId!==this.actorId||(refs&&c.version!==refs.customerVersion)))return false;}
    const c=this.load();return hash(c.corpId+'|'+c.openKfId+'|'+c.testUser)===binding;
  }
  async tick(){
    if(!this.running||this.busy)return;this.busy=true;
    const epoch=this.epoch,{config,api,binding}=this.active;
    try{
      if(!this.eligible(epoch,binding)){this.stop();return;}
      let cursor=this.state().binding===binding?this.state().cursor||'':'';
      for(let pageCount=0;pageCount<10&&this.running;pageCount++){
        const page=await api.page(cursor);if(!Array.isArray(page.msg_list)||typeof page.next_cursor!=='string')throw new Problem('客服消息页格式无效。',502);
        for(const m of page.msg_list){
          if(m.open_kfid!==config.openKfId||m.external_userid!==config.testUser||m.origin!==3||typeof m.msgid!=='string'||!m.msgid||!Number.isFinite(m.send_time)||m.send_time*1000<this.startedAt||m.send_time*1000>this.clock()+1000)continue;
          const key=hash(binding+'|'+m.msgid);let fresh=false;
          let refs;
          if(this.journal&&m.msgtype==='text'){
            refs=this.journal.incoming({actorId:this.actorId,binding,key,message:m,discovery:this.discovery&&!this.observationOnly});
          }
          this.update(s=>{if(!s.receipts.some(r=>r.key===key)){s.receipts.push({key,at:this.clock(),status:'processing',...refs});fresh=true;}});
          if(!fresh)continue;
          const finish=(status,reason,text)=>{this.update(s=>Object.assign(s.receipts.find(r=>r.key===key),{status,reason,...(status==='api_accepted'?{acceptedAt:this.clock()}:{}),...(text?{text}:{})}));this.journal?.outcome({key,status,reason,text});if(status==='human')this.update(s=>{s.humanLocked=true;});};
          if(this.observationOnly){finish(refs?'observed':'human',refs?'本人消息已同步到客户档案，未调用模型或发送':'非文本消息留在官方客服界面，需人工查看');continue;}
          const message=m.msgtype==='text'?m.text?.content:'';
          const customer=this.engine.store.read().customers?.find(c=>c.id===refs?.customerId)||{};
          const plan=this.discovery?discoveryPlan(message,customer,this.engine.store.read().settings):null;
          const risk=typeof message==='string'?assess(message,{},this.engine.store.read().settings):null;
          if(this.state().humanLocked||!risk||(!plan&&(risk.risk!=='low'||!['礼貌接待','礼貌回应'].includes(risk.intent)))){
            finish('human',this.discovery?'超出低风险需求范围，需人工接管':'超出本轮测试问候范围，需人工接管');this.update(s=>{s.humanLocked=true;});continue;
          }
          if(this.quota().remaining===0){finish('limited',this.quotaReason());this.lastError=this.quotaReason();this.stop();break;}
          if(!this.eligible(epoch,binding)){finish('canceled','生成前已暂停');continue;}
          let draft;try{draft=await this.reply.draft({customerMessage:message,...(plan?{allowedReplies:plan.replies}:{})});}catch{finish('human','模型未返回可用草稿');continue;}
          const n=normalize(draft.text);
          const discoveryValid=plan&&draft.topic==='discovery'&&plan.replies.includes(draft.text);
          if(plan?!discoveryValid:(draft.topic!=='greeting'||mandatoryTopics(draft.text).length||/[0-9零一二两三四五六七八九十百千万亿¥￥$]/u.test(n)||!/^您好[，,！!。.]*(欢迎咨询|很高兴为您服务|有什么可以帮您)[，,！!。.?？]*$/u.test(n))){
            finish('human',this.discovery?'模型未使用已审核的需求问句':'模型回复不在测试问候白名单');continue;
          }
          if(!this.exclusiveTest){
            const session=await api.session();
            if(![0,1].includes(session.service_state)){finish('human','当前会话非机器人接待状态');this.update(s=>{s.humanLocked=true;});continue;}
          }
          const latest=await api.page(cursor);
          if(!Array.isArray(latest.msg_list)||latest.has_more===1){finish('human','发送前无法完整核对新消息');continue;}
          if(this.exclusiveTest&&latest.msg_list.some(x=>x.open_kfid===config.openKfId&&x.origin!==3&&x.send_time*1000>=this.startedAt&&
            !(x.msgtype==='event'&&x.event?.event_type==='enter_session'))){
            finish('human','独占测试期间出现非客户消息或会话事件，已暂停');this.update(s=>{s.humanLocked=true;});this.stop();break;
          }
          const index=page.msg_list.findIndex(x=>x.msgid===m.msgid);
          const newer=latest.msg_list.some(x=>x.open_kfid===config.openKfId&&x.external_userid===config.testUser&&x.origin===3&&x.msgid!==m.msgid&&
            (x.send_time>m.send_time||!page.msg_list.some(old=>old.msgid===x.msgid)||page.msg_list.findIndex(old=>old.msgid===x.msgid)>index));
          if(newer){finish('canceled','客户已有新消息，旧回复作废');continue;}
          if(!this.eligible(epoch,binding,refs)){finish('canceled','发送前已暂停、客户变化或配置已更改');continue;}
          if(this.quota().remaining===0){finish('limited',this.quotaReason());this.lastError=this.quotaReason();this.stop();break;}
          finish('sending','请求已准备；未知结果不自动重发',draft.text);
          try{const sent=await api.send(draft.text,'av-'+key.slice(0,24));if(typeof sent.msgid!=='string'||!sent.msgid)throw new Error('missing receipt');}
          catch{finish('unknown','发送失败或结果不明，已暂停；先核对微信');this.stop();break;}
          finish('api_accepted','微信接口已受理，需本人确认手机可见',draft.text);
          if(this.quota().remaining===0){this.lastError=this.quotaReason();this.stop();break;}
        }
        this.update(s=>{s.cursor=page.next_cursor;s.binding=binding;});
        if(page.has_more!==1)break;
        if(!page.next_cursor||page.next_cursor===cursor)throw new Problem('消息游标没有前进，已停止。',502);cursor=page.next_cursor;
      }
    }catch(e){this.lastError=e instanceof Problem?e.message:'微信客服读取失败，已暂停；不自动重试。';this.stop();}
    finally{this.busy=false;}
  }
  resumeTest(actor){this.engine.owner(actor);if(this.running||this.busy)throw new Problem('请先暂停，再恢复本人测试接待。',409);if(this.journal){const c=this.load();this.journal.resume(actor,hash(c.corpId+'|'+c.openKfId+'|'+c.testUser));}this.update(s=>{s.humanLocked=false;});return this.status(actor);}
  close(){this.stop();}
}
