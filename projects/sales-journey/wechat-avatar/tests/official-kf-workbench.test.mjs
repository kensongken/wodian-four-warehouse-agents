import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {hash} from '../lib/policy.mjs';
import {OfficialKfWorkbench} from '../lib/official-kf-workbench.mjs';
import {OfficialKfLocal} from '../lib/official-kf-local.mjs';

function fixture(text='你好',modelDraft={text:'您好，欢迎咨询！',topic:'greeting'}){
 const directory=mkdtempSync(join(tmpdir(),'kf-journal-'));const store=new Store(directory);let now=100000;
 const engine=new Engine(store,()=>now);const actor=engine.setup({username:'synthetic-owner',password:'test-synthetic-password'});
 store.transact(s=>{s.settings.modelProvider='qwen_local';s.settings.modelDataConsent=true;});
 const journal=new OfficialKfWorkbench(engine);const message={msgid:'synthetic-msg',origin:3,send_time:101,open_kfid:'synthetic-kf',external_userid:'synthetic-self',msgtype:'text',text:{content:text}};
 let calls=0,sends=0;let onDraft;
 const controller=new OfficialKfLocal(engine,{journal,load:()=>({corpId:'synthetic-corp',secret:'never-sent',openKfId:'synthetic-kf',testUser:'synthetic-self'}),apiFactory:()=>({session:async()=>({service_state:1}),page:async()=>({next_cursor:'cursor',has_more:0,msg_list:[message]}),send:async()=>{sends++;return {msgid:'synthetic-receipt'};}}),reply:{close(){},draft:async()=>{calls++;onDraft?.();return modelDraft;}},clock:()=>now,interval:3600000});
 return {store,engine,actor,journal,message,controller,now:n=>{now=n;},onDraft:fn=>{onDraft=fn;},counts:()=>({calls,sends}),close:()=>{controller.close();store.close();rmSync(directory,{recursive:true,force:true});}};
}

test('official incoming, model receipt and customer workspace form one deduplicated record',async()=>{
 const f=fixture();try{
  await f.controller.start(f.actor,{confirmed:true});f.now(102000);await f.controller.tick();await f.controller.tick();
  const s=f.store.read();assert.equal(s.customers.length,1);assert.equal(s.messages.length,2);assert.equal(s.drafts.length,1);assert.equal(s.tasks.length,0);
  assert.equal(s.customers[0].ownerTest,true);assert.equal(s.messages[0].source,'official_kf_test');assert.equal(s.messages[1].delivery,'api_accepted');assert.equal(s.messages[1].provenance.phoneVisibleConfirmed,false);
  assert.equal(s.drafts[0].status,'official_api_accepted');assert.deepEqual(f.counts(),{calls:1,sends:1});
  assert.throws(()=>f.engine.updateDraft(f.actor,s.drafts[0].id,{revision:s.drafts[0].revision,action:'edit',text:'改写'}),/不能覆盖/);
  assert.throws(()=>f.engine.generate(f.actor,s.customers[0].id),/已有受理回复/);
  assert.equal(f.controller.status(f.actor).receipts[0].customerId,s.customers[0].id);
 }finally{f.close();}
});
test('observing records needs as candidates and creates manual follow-up without model or sends',async()=>{
 const f=fixture('想送给妈妈，喜欢素一点的。');try{
  f.store.transact(s=>{s.officialKf={receipts:[],humanLocked:true};s.settings.modelDataConsent=false;});
  await f.controller.observe(f.actor,{confirmed:true});f.now(102000);await f.controller.tick();
  const s=f.store.read(),c=s.customers[0];assert.deepEqual(f.counts(),{calls:0,sends:0});assert.equal(s.tasks.length,0);assert.equal(c.stage,'discover');assert.equal(c.result,'ongoing');assert.equal(c.needs.length,0);assert.equal(c.candidates.length,2);assert.equal(s.reminders.filter(r=>r.type==='handoff').length,1);assert.equal(s.officialKf.humanLocked,true);
  f.engine.updateCustomer(f.actor,c.id,{version:c.version,candidateId:c.candidates[0].id,accept:true});assert.equal(f.store.read().customers[0].needs.length,1);
 }finally{f.close();}
});
test('quote goes into the same customer and locks local manual work',async()=>{
 const f=fixture('最低多少钱？');try{await f.controller.start(f.actor,{confirmed:true});f.now(102000);await f.controller.tick();const s=f.store.read();assert.equal(s.customers[0].locked,true);assert.equal(s.reminders.length,1);assert.equal(s.drafts[0].risk,'human');assert.deepEqual(f.counts(),{calls:0,sends:0});}finally{f.close();}
});
test('a manual claim while the model runs stops the pending official send',async()=>{
 const f=fixture();try{f.onDraft(()=>f.store.transact(s=>{s.customers[0].locked=true;}));await f.controller.start(f.actor,{confirmed:true});f.now(102000);await f.controller.tick();assert.equal(f.counts().sends,0);assert.equal(f.store.read().messages.filter(m=>m.direction==='out').length,0);}finally{f.close();}
});
test('different bindings remain separate and browser-entered messages cannot claim official provenance',()=>{
 const f=fixture();try{
  const a=f.journal.incoming({actorId:f.actor.id,binding:hash('binding-a'),key:hash('key-a'),message:f.message});
  f.journal.incoming({actorId:f.actor.id,binding:hash('binding-b'),key:hash('key-b'),message:f.message});assert.equal(f.store.read().customers.length,2);
  const msg=f.engine.ingest(f.actor,a.customerId,{text:'手工内容',synthetic:true,source:'official_kf_test',officialReceiptKey:hash('forged')});assert.equal(msg.source,'synthetic');assert.equal(msg.officialReceiptKey,undefined);
  assert.throws(()=>f.journal.incoming({actorId:f.actor.id,binding:hash('binding-a'),key:hash('key-a'),message:{...f.message,text:{content:'不同内容'}}}),/冲突/);
 }finally{f.close();}
});

test('automatic discovery archives composite preferences and sends without manual need approval',async()=>{
 const f=fixture('想送给妈妈，喜欢素一点的。',{text:'了解，您更想先看看手链，还是手串？',topic:'discovery'});try{
  await f.controller.start(f.actor,{confirmed:true,discoveryConfirmed:true});f.now(102000);await f.controller.tick();
  const s=f.store.read(),c=s.customers[0];assert.deepEqual(f.counts(),{calls:1,sends:1});assert.equal(c.locked,false);assert.equal(c.candidates.length,2);assert.equal(c.needs.length,0);assert.equal(s.reminders.length,0);assert.equal(s.drafts[0].status,'official_api_accepted');assert.equal(c.stage,'discover');
 }finally{f.close();}
});
test('automatic discovery still locks quote before inference in the integrated workbench',async()=>{
 const f=fixture('最低多少钱？');try{await f.controller.start(f.actor,{confirmed:true,discoveryConfirmed:true});f.now(102000);await f.controller.tick();assert.deepEqual(f.counts(),{calls:0,sends:0});assert.equal(f.store.read().customers[0].locked,true);assert.equal(f.store.read().reminders.length,1);}finally{f.close();}
});

test('discovery resumes an older customer using prior official preferences without confirming them',()=>{
 const f=fixture('手链');try{
  const binding=hash('legacy-binding');const prior=f.journal.incoming({actorId:f.actor.id,binding,key:hash('legacy-message'),message:f.message});
  f.store.transact(s=>{const c=s.customers[0];c.candidates=[];c.needs=[{kind:'用途',value:'送给妈妈'},{kind:'风格',value:'简约素雅'}];});
  f.journal.incoming({actorId:f.actor.id,binding,key:hash('next-message'),message:{...f.message,msgid:'next',text:{content:'小巧一点'}},discovery:true});
  const c=f.store.read().customers[0];assert.equal(c.needs.length,2);assert.equal(c.candidates.find(n=>n.kind==='款式').messageId,prior.messageId);assert.equal(c.candidates.find(n=>n.kind==='款式').value,'手链');assert.equal(c.candidates.find(n=>n.kind==='佩戴感').value,'小巧');assert.equal(c.locked,false);
 }finally{f.close();}
});
