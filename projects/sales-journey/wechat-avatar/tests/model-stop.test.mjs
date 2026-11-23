import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {Connectors} from '../lib/connectors.mjs';
const output={choices:[{message:{content:'迟到的合成模型文字'}}]};
function fixture(t){const dir=mkdtempSync(join(tmpdir(),'avatar-stop-'));const store=new Store(dir);let now=Date.now();const engine=new Engine(store,()=>now);const owner=engine.setup({username:'owner',name:'合成起草停止验收',password:randomUUID()});engine.settings(owner,{version:1,modelProvider:'qwen_local'});engine.settings(owner,{version:2,modelDataConsent:true});const c=engine.createCustomer(owner,{alias:'合成客户',synthetic:true});engine.ingest(owner,c.id,{text:'你好',synthetic:true});engine.generate(owner,c.id);const draft=store.read().drafts[0];t.after(()=>{store.close();rmSync(dir,{recursive:true,force:true});});let resolve,calls=0,signal;
 const q={available:()=>true,close(){},complete:(_payload,options)=>{calls++;signal=options.signal;return new Promise(r=>resolve=r);}};const connector=new Connectors(engine,undefined,undefined,q);
 return {store,engine,owner,draft,c,connector,resolve:()=>resolve(output),calls:()=>calls,signal:()=>signal,advance:ms=>now+=ms};}
test('stopping active local draft rejects a late result and preserves exact prior revision and text',async t=>{
 const f=fixture(t),jobId=randomUUID();const pending=f.connector.modelDraft(f.owner,f.draft.id,{jobId});assert.equal(f.calls(),1);assert.equal(f.connector.stopModelDraft(f.owner,f.draft.id,{jobId}).stopped,true);assert.equal(f.signal().aborted,true);f.connector.stopModelDraft(f.owner,f.draft.id,{jobId});f.resolve();await assert.rejects(()=>pending,/已停止/);const s=f.store.read();assert.deepEqual(s.drafts[0],f.draft);assert.equal(s.costs[0].status,'canceled');assert.equal(s.tasks.length,0);assert.equal(s.audit.filter(x=>x.kind==='停止本次模型起草').length,1);
});
test('stop arriving before start prevents both model call and quota reservation',async t=>{
 const f=fixture(t),jobId=randomUUID();f.connector.stopModelDraft(f.owner,f.draft.id,{jobId});await assert.rejects(()=>f.connector.modelDraft(f.owner,f.draft.id,{jobId}),/已停止/);assert.equal(f.calls(),0);assert.equal(f.store.read().costs.length,0);
 const next=f.connector.modelDraft(f.owner,f.draft.id,{jobId:randomUUID()});f.resolve();await next;assert.equal(f.calls(),1);
});
test('a stop after completed persistence reports finished without rolling text back',async t=>{
 const f=fixture(t),jobId=randomUUID();const pending=f.connector.modelDraft(f.owner,f.draft.id,{jobId});f.resolve();await pending;assert.deepEqual(f.connector.stopModelDraft(f.owner,f.draft.id,{jobId}),{stopped:false,finished:true});assert.equal(f.store.read().drafts[0].text,'迟到的合成模型文字');assert.equal(f.store.read().costs[0].status,'success');
});
test('job token belongs to its actor and draft; employees cannot stop owner draft',async t=>{
 const f=fixture(t),jobId=randomUUID();const pending=f.connector.modelDraft(f.owner,f.draft.id,{jobId});const c2=f.engine.createCustomer(f.owner,{alias:'第二合成客户',synthetic:true});f.engine.ingest(f.owner,c2.id,{text:'你好',synthetic:true});const d2=f.engine.generate(f.owner,c2.id);
 assert.throws(()=>f.connector.stopModelDraft(f.owner,d2.id,{jobId}),/不属于/);assert.throws(()=>f.connector.stopModelDraft({role:'employee',id:'another'},f.draft.id,{jobId}),/老板/);assert.equal(f.signal().aborted,false);f.resolve();await pending;
});
test('duplicate or concurrent local draft requests do not reserve another attempt',async t=>{
 const f=fixture(t),jobId=randomUUID();const pending=f.connector.modelDraft(f.owner,f.draft.id,{jobId});await assert.rejects(()=>f.connector.modelDraft(f.owner,f.draft.id,{jobId}),/已使用/);await assert.rejects(()=>f.connector.modelDraft(f.owner,f.draft.id,{jobId:randomUUID()}),/正在起草/);assert.equal(f.calls(),1);assert.equal(f.store.read().costs.length,1);f.resolve();await pending;
});
test('invalid cancellation tokens cannot create stop records',t=>{
 const f=fixture(t);assert.throws(()=>f.connector.stopModelDraft(f.owner,f.draft.id,{jobId:'invalid'}),/编号无效/);assert.equal(f.connector.modelRuns.size,0);
});
test('finished stop records expire and cannot fill memory indefinitely',t=>{
 const f=fixture(t);for(let i=0;i<128;i++)f.connector.stopModelDraft(f.owner,f.draft.id,{jobId:randomUUID()});assert.throws(()=>f.connector.stopModelDraft(f.owner,f.draft.id,{jobId:randomUUID()}),/请求过多/);f.advance(120001);f.connector.stopModelDraft(f.owner,f.draft.id,{jobId:randomUUID()});assert.equal(f.connector.modelRuns.size,1);
});
