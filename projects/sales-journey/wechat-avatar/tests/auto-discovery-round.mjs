// Explicit manual acceptance: actual local Qwen with simulated WeChat API only.
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {Store} from '../lib/store.mjs';import {Engine} from '../lib/engine.mjs';
import {OfficialKfLocal} from '../lib/official-kf-local.mjs';import {OfficialKfWorkbench} from '../lib/official-kf-workbench.mjs';import {LocalQwenKfReply} from '../lib/qwen-kf-reply.mjs';
const dir=mkdtempSync(join(tmpdir(),'avatar-auto-round-'));const store=new Store(dir);let now=Date.now();const engine=new Engine(store,()=>now),owner=engine.setup({username:'synthetic-round-owner',password:'test-synthetic-round-password'});store.transact(s=>{s.settings.modelProvider='qwen_local';s.settings.modelDataConsent=true;});
let message=null,calls=0,sends=0;const qwen=new LocalQwenKfReply();const transcript=[];
const controller=new OfficialKfLocal(engine,{journal:new OfficialKfWorkbench(engine),clock:()=>now,interval:3600000,load:()=>({corpId:'synthetic',secret:'synthetic',openKfId:'synthetic-kf',testUser:'synthetic-self'}),apiFactory:()=>({session:async()=>({service_state:1}),page:async()=>({next_cursor:'synthetic-cursor',has_more:0,msg_list:message?[message]:[]}),send:async()=>({msgid:'simulated-out-'+(++sends)})}),reply:{close:()=>qwen.close(),draft:async input=>{calls++;return qwen.draft(input);}}});
const report={version:'2.0.0-local.19',testKind:'actual_local_qwen_with_simulated_wechat',realWechatCalls:0,realLocalModelCalls:0,syntheticApiAccepted:0,transcript,status:'running'};
try{
 await controller.start(owner,{confirmed:true,discoveryConfirmed:true,unlimitedTestConfirmed:true});
 for(const [i,text] of ['你好','送给妈妈','素一点','手链','小巧一点','日常佩戴','谢谢','再见','最低多少钱？'].entries()){
  now+=1000;message={msgid:'round-'+i,open_kfid:'synthetic-kf',external_userid:'synthetic-self',origin:3,msgtype:'text',send_time:Math.floor(now/1000),text:{content:text}};
  const start=Date.now(),before=calls;await controller.tick();const r=controller.status(owner).receipts.at(-1);
  transcript.push({incoming:text,status:r.status,reply:r.text||'',reason:r.reason,elapsedMs:Date.now()-start,modelCalls:calls-before});
  console.log(JSON.stringify({turn:i+1,...transcript.at(-1)}));
  assert.equal(r.status,i===8?'human':'api_accepted');
  if(i===8)assert.equal(calls,before);
  else {const oldSends=sends;await controller.tick();assert.equal(sends,oldSends);}
 }
 const s=store.read(),c=s.customers[0];assert.equal(sends,8);assert.equal(calls,8);assert.equal(c.needs.length,0);assert.equal(c.candidates.length,5);assert.equal(c.stage,'discover');assert.equal(c.result,'ongoing');assert.equal(c.locked,true);assert.equal(s.tasks.length,0);assert.equal(s.reminders.filter(r=>r.type==='handoff').length,1);controller.stop(owner);now+=1000;message={...message,msgid:'paused',send_time:Math.floor(now/1000),text:{content:'你好'}};await controller.tick();assert.equal(sends,8);
 report.status='passed';report.checks=['eight automatic local-model turns without manual review','five preference categories recorded as candidates','no duplicate sends for repeated message IDs','quote locks before model call','pause prevents processing','no fabricated stage or sale'];
}catch(e){report.status='failed';report.error=e.message;process.exitCode=1;}finally{report.realLocalModelCalls=calls;report.syntheticApiAccepted=sends;report.checkedAt=new Date().toISOString();if(process.env.AVATAR_AUTO_ROUND_OUTPUT)writeFileSync(process.env.AVATAR_AUTO_ROUND_OUTPUT,JSON.stringify(report,null,2));console.log(JSON.stringify({final:report.status,realLocalModelCalls:calls,realWechatCalls:0,error:report.error}));controller.close();store.close();rmSync(dir,{recursive:true,force:true});}
