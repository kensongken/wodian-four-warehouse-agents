import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';
import {Store} from '../lib/store.mjs';import {Engine} from '../lib/engine.mjs';import {LocalQwen,QWEN_MODEL} from '../lib/qwen-model.mjs';import {Connectors} from '../lib/connectors.mjs';import {createApp} from '../server.mjs';import {studioView} from '../web/persona-studio.js';
const info={details:{format:'gguf',family:'qwen3vl'}},output={model:QWEN_MODEL,done:true,done_reason:'stop',message:{content:'{"reply":"你好，有什么想了解的？"}'},load_duration:2000000,prompt_eval_duration:3000000,eval_duration:4000000,total_duration:9000000};
const payload={model:QWEN_MODEL,messages:[{role:'user',content:'你好'}]};
function fixture(t){const directory=mkdtempSync(join(tmpdir(),'runtime-test-')),store=new Store(directory),engine=new Engine(store),owner=engine.setup({username:'owner',password:'runtime-test-password'});t.after(()=>{store.close();rmSync(directory,{recursive:true,force:true});});return{store,engine,owner};}
test('short cache is explicit, reversible and restricted to two bounded retention choices',async()=>{
 let enabled=false;const requests=[];const model=new LocalQwen({keepAlive:()=>enabled?'120s':0,post:async(path,body)=>{if(path==='/api/show')return info;requests.push(body);return output;}});
 const first=await model.complete(payload);assert.equal(first.performance.modelLoadMs,2);assert.equal(first.performance.contextReadMs,3);assert.equal(first.performance.generationMs,4);assert.equal(first.performance.shortCacheRequested,false);
 enabled=true;assert.equal((await model.complete(payload)).performance.shortCacheRequested,true);enabled=false;await model.complete(payload);assert.deepEqual(requests.map(x=>x.keep_alive),[0,'120s',0]);
 await assert.rejects(()=>new LocalQwen({keepAlive:'forever',post:async()=>{throw Error('must not call');}}).complete(payload),/约2分钟/);
});
test('missing or invalid provider timing is omitted rather than displayed as fabricated zero latency',async()=>{
 for(const result of [{...output,load_duration:undefined},{...output,eval_duration:-1}]){const model=new LocalQwen({post:async path=>path==='/api/show'?info:result});assert.equal((await model.complete(payload)).performance,undefined);}
});
test('runtime preference has its own version and does not change send policy, consent or queued jobs',t=>{
 const f=fixture(t);f.store.transact(s=>{s.settings.modelDataConsent=true;s.tasks.push({id:'sentinel-job',status:'queued'});});const before=f.store.read();
 f.engine.localModelRuntime(f.owner,{version:0,warm:true});const after=f.store.read();assert.equal(after.settings.localModelWarm,true);assert.equal(after.settings.localModelRuntimeVersion,1);assert.equal(after.settings.policyVersion,before.settings.policyVersion);assert.equal(after.settings.mode,before.settings.mode);assert.equal(after.settings.modelDataConsent,true);assert.deepEqual(after.tasks,before.tasks);
 assert.throws(()=>f.engine.localModelRuntime(f.owner,{version:0,warm:false}),/已变化/);assert.throws(()=>f.engine.localModelRuntime({role:'employee'},{version:1,warm:false}),/老板/);assert.throws(()=>f.engine.localModelRuntime(f.owner,{version:1,warm:'true'}),/是否/);
 const connector=new Connectors(f.engine);assert.equal(connector.localQwen.keepAlive(),'120s');f.engine.localModelRuntime(f.owner,{version:1,warm:false});assert.equal(connector.localQwen.keepAlive(),0);connector.close();
});
test('owner runtime HTTP route retains authentication and same-origin checks',async t=>{
 const directory=mkdtempSync(join(tmpdir(),'runtime-http-')),app=createApp({directory,worker:false});await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';t.after(async()=>{await app.close();rmSync(directory,{recursive:true,force:true});});
 async function post(path,body,extra={}){const response=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie,...extra},body:JSON.stringify(body)});if(response.headers.has('set-cookie'))cookie=response.headers.get('set-cookie').split(';')[0];return{status:response.status,data:await response.json()};}
 assert.equal((await post('/api/model/local-runtime',{version:0,warm:true})).status,401);await post('/api/setup',{username:'owner',password:'test-runtime-http-password'});assert.equal((await post('/api/model/local-runtime',{version:0,warm:true},{Origin:'https://elsewhere.invalid'})).status,403);assert.equal((await post('/api/model/local-runtime',{version:0,warm:true})).status,200);assert.equal(app.store.read().settings.policyVersion,1);assert.equal(app.store.read().settings.localModelWarm,true);
});
test('preview timing distinguishes real model preparation from direct fact quotes',()=>{
 const trial={id:'t',profileId:'p',version:1,question:'你好',reply:'你好',baseline:'您好',timing:{elapsedMs:1458,model:{modelLoadMs:9,contextReadMs:86,generationMs:1012}},rating:null};const state={personaStudio:{profile:{id:'p',version:1,examples:[]},trials:[trial]}};let html=studioView(state);assert.ok(html.includes('本轮用时 1.5 秒'));assert.ok(html.includes('生成回复 1.0 秒'));
 trial.timing={elapsedMs:8,model:null};trial.generationSource='approved_lookup';html=studioView(state);assert.ok(html.includes('无需等待模型生成'));assert.ok(!html.includes('生成回复 1.0 秒'));
});
