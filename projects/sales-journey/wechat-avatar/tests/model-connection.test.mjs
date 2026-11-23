import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {Connectors} from '../lib/connectors.mjs';
function fixture(t) {
  const dir=mkdtempSync(join(tmpdir(),'avatar-model-'));const store=new Store(dir);const engine=new Engine(store);
  const owner=engine.setup({username:'owner',name:'演练负责人',password:'synthetic-test-password'});
  t.after(()=>{store.close();rmSync(dir,{recursive:true,force:true});});
  const configure=(patch={})=>engine.settings(owner,{version:store.read().settings.policyVersion,...patch});
  configure({modelEndpoint:'https://model.example.com/chat/completions',modelKey:'synthetic-key'});
  const customer=engine.createCustomer(owner,{alias:'测试秘密称呼',synthetic:true});engine.ingest(owner,customer.id,{text:'测试秘密客户原话',synthetic:true});
  const draft=()=>engine.generate(owner,customer.id);
  return {store,engine,owner,configure,customer,draft};
}
const output={choices:[{message:{content:'您好，我先了解一下您的喜好。'}}]};
test('model connection test sends only fixed synthetic text and does not unlock sending',async t=>{
  const f=fixture(t);let payload;const c=new Connectors(f.engine,async(_url,_headers,body)=>{payload=body;return output;});const before=f.store.read();
  const r=await c.testModel(f.owner,{version:before.settings.policyVersion,confirmed:true});
  assert.equal(r.customerDataSent,false);assert.equal(r.automaticSendEligible,false);assert.equal(payload.model,'gpt-5.6-sol');
  assert.ok(JSON.stringify(payload).includes('内置演练'));assert.ok(!JSON.stringify(payload).includes('测试秘密'));
  const after=f.store.read();assert.deepEqual(after.messages,before.messages);assert.deepEqual(after.drafts,before.drafts);assert.deepEqual(after.tasks,[]);assert.equal(after.settings.mode,before.settings.mode);
  assert.equal(after.costs[0].kind,'model_test');assert.equal(after.costs[0].status,'success');assert.ok(!JSON.stringify(after).includes(r.text));
});
test('test requires saved configuration version and explicit call confirmation before reserving cost',async t=>{
  const f=fixture(t);let calls=0;const c=new Connectors(f.engine,async()=>{calls++;return output;});
  await assert.rejects(()=>c.testModel(f.owner,{version:2}),/确认/);
  await assert.rejects(()=>c.testModel(f.owner,{version:1,confirmed:true}),/设置已变化/);
  f.configure({clearModelKey:true});await assert.rejects(()=>c.testModel(f.owner,{version:f.store.read().settings.policyVersion,confirmed:true}),/保存模型/);
  assert.equal(calls,0);assert.equal(f.store.read().costs.length,0);
});
test('screenshot consent cannot authorize customer text model calls',async t=>{
  const f=fixture(t);f.configure({dataConsent:true});let calls=0;const c=new Connectors(f.engine,async()=>{calls++;return output;});
  await assert.rejects(()=>c.modelDraft(f.owner,f.draft().id),/单独确认/);assert.equal(calls,0);assert.equal(f.store.read().costs.length,0);
  f.configure({modelDataConsent:true,dataConsent:false});await c.modelDraft(f.owner,f.draft().id);assert.equal(calls,1);
});
test('stale customer draft is refused before customer data or quota is sent',async t=>{
  const f=fixture(t);f.configure({modelDataConsent:true});const d=f.draft();f.engine.ingest(f.owner,f.customer.id,{text:'新消息',synthetic:true});let calls=0;
  const c=new Connectors(f.engine,async()=>{calls++;return output;});await assert.rejects(()=>c.modelDraft(f.owner,d.id),/变化/);assert.equal(calls,0);assert.equal(f.store.read().costs.length,0);
});
for(const [name,patch] of [['consent revoked',{modelDataConsent:false}],['key rotated',{modelKey:'new-synthetic-key'}]])test(`in-flight model result is discarded when ${name}`,async t=>{
  const f=fixture(t);f.configure({modelDataConsent:true});const d=f.draft();let resolve;const waiting=new Promise(r=>resolve=r);const c=new Connectors(f.engine,()=>waiting);
  const pending=c.modelDraft(f.owner,d.id);f.configure(patch);resolve(output);await assert.rejects(()=>pending,/变化|撤回/);
  const state=f.store.read();assert.equal(state.drafts.find(x=>x.id===d.id).text,d.text);assert.equal(state.costs[0].status,'failed');assert.equal(state.tasks.length,0);
});
test('connection result is discarded when configuration changes during request',async t=>{
  const f=fixture(t);let resolve;const c=new Connectors(f.engine,()=>new Promise(r=>resolve=r));const pending=c.testModel(f.owner,{version:f.store.read().settings.policyVersion,confirmed:true});
  f.configure({modelKey:'replacement-key'});resolve(output);await assert.rejects(()=>pending,/变化/);assert.equal(f.store.read().costs[0].status,'failed');
});
test('malformed connection response consumes one attempt without retries or model fallback',async t=>{
  const f=fixture(t);let calls=0;const c=new Connectors(f.engine,async()=>{calls++;return {choices:[]};});
  await assert.rejects(()=>c.testModel(f.owner,{version:f.store.read().settings.policyVersion,confirmed:true}),/未通过/);
  assert.equal(calls,1);assert.equal(f.store.read().costs[0].status,'failed');assert.equal(f.store.read().settings.model,'gpt-5.6-sol');
});
test('model test shares daily hard quota with other interfaces',async t=>{
  const f=fixture(t);f.configure({dailyLimit:1});let calls=0;const c=new Connectors(f.engine,async()=>{calls++;return output;});const input={version:f.store.read().settings.policyVersion,confirmed:true};
  await c.testModel(f.owner,input);await assert.rejects(()=>c.testModel(f.owner,input),/硬上限/);assert.equal(calls,1);
});

test('local Codex provider needs no API key and preserves independent draft consent',async t=>{
 const f=fixture(t);f.configure({modelProvider:'codex_cli',modelEndpoint:'',clearModelKey:true});let calls=0,apiCalls=0;
 const local={available:()=>'/official/codex',complete:async body=>{calls++;assert.equal(body.model,'gpt-5.6-sol');return output;}};
 const c=new Connectors(f.engine,async()=>apiCalls++,local);
 const r=await c.testModel(f.owner,{version:f.store.read().settings.policyVersion,confirmed:true});assert.equal(r.provider,'codex_cli');assert.equal(calls,1);assert.equal(apiCalls,0);
 await assert.rejects(()=>c.modelDraft(f.owner,f.draft().id),/单独确认/);f.configure({modelDataConsent:true});await c.modelDraft(f.owner,f.draft().id);assert.equal(calls,2);assert.equal(apiCalls,0);assert.equal(f.store.read().drafts.at(-1).source,'model_pending_review');assert.equal(f.store.read().tasks.length,0);
});

test('domestic Qwen selection uses local adapter and records the actual requested model',async t=>{
 const f=fixture(t);f.configure({modelProvider:'qwen_local',modelDataConsent:true});assert.equal(f.store.read().settings.modelDataConsent,false);f.configure({modelDataConsent:true});let calls=0,cloudCalls=0;
 const q={available:()=>true,complete:async body=>{calls++;assert.equal(body.model,'qwen3-vl:4b-instruct');return output;}};
 const c=new Connectors(f.engine,async()=>cloudCalls++,undefined,q);const r=await c.testModel(f.owner,{version:f.store.read().settings.policyVersion,confirmed:true});assert.equal(r.model,'qwen3-vl:4b-instruct');assert.equal(r.provider,'qwen_local');await c.modelDraft(f.owner,f.draft().id);assert.equal(calls,2);assert.equal(cloudCalls,0);assert.equal(f.store.read().audit.at(-1).details.model,'qwen3-vl:4b-instruct');
 f.configure({modelProvider:'codex_cli',modelDataConsent:true});assert.equal(f.store.read().settings.modelDataConsent,false);
});
