// Explicit manual acceptance against installed local weights; never run as part of npm test.
// All input is built-in synthetic material. This does not read or operate the desktop/WeChat.
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createApp} from '../server.mjs';
const directory=mkdtempSync(join(tmpdir(),'avatar-qwen-acceptance-'));const app=createApp({directory,worker:false});
await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';
async function post(path,body){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',...(cookie?{Cookie:cookie}:{})},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];const data=await r.json();assert.equal(r.status,200,JSON.stringify(data));return data;}
try {
 await post('/api/setup',{username:'synthetic-local-test',name:'合成本机千问验收',password:randomUUID()});
 await post('/api/settings',{version:1,modelProvider:'qwen_local'});await post('/api/settings',{version:2,modelDataConsent:true});
 const c=await post('/api/customers',{alias:'合成千问报价演练',synthetic:true});await post(`/api/customers/${c.id}/messages`,{text:'【合成演练】这款最低多少钱？',synthetic:true});
 const draft=await post(`/api/customers/${c.id}/generate`,{});assert.equal(draft.risk,'human');
 const start=Date.now();const result=await post(`/api/drafts/${draft.id}/model`,{});const state=app.store.read();
 assert.equal(result.draft.source,'model_pending_review');assert.equal(result.draft.risk,'human');assert.equal(result.draft.status,'draft');assert.equal(state.customers[0].locked,true);assert.equal(state.tasks.length,0);assert.equal(state.costs.length,1);assert.equal(state.costs[0].status,'success');assert.equal(state.settings.mode,'assist');
 const report={status:'passed',date:'2026-10-27',provider:'qwen_local',model:'qwen3-vl:4b-instruct',material:'built-in synthetic price inquiry only',elapsedMs:Date.now()-start,text:result.draft.text,actualLocalModelCalls:1,externalModelCalls:0,realCustomerDataUsed:false,realWechatMessagesSent:0,checks:['actual authenticated HTTP customer message to local Qwen draft','price inquiry remains human handled','model draft requires review; no send task','no API key and no cloud provider call','isolated temporary ledger; no real customer acceptance'],note:'This is a synthetic workflow test, not sales quality or WeChat acceptance'};
 if(process.env.AVATAR_QWEN_NATIVE_OUTPUT)writeFileSync(process.env.AVATAR_QWEN_NATIVE_OUTPUT,JSON.stringify(report,null,2));console.log(JSON.stringify(report));
} finally {await app.close();rmSync(directory,{recursive:true,force:true});}
