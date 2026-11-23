// Manual real local-model cancellation check with built-in synthetic data only.
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createApp} from '../server.mjs';
import {LocalQwen,localOllamaJSON} from '../lib/qwen-model.mjs';
const directory=mkdtempSync(join(tmpdir(),'avatar-native-stop-'));let enteredResolve;const entered=new Promise(r=>enteredResolve=r);
const localQwen=new LocalQwen({post:(path,body,options)=>{const pending=localOllamaJSON(path,body,options);if(path==='/api/chat')enteredResolve();return pending;}});
const app=createApp({directory,worker:false,localQwen});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';
async function post(path,body,expected=200){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',...(cookie?{Cookie:cookie}:{})},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];const data=await r.json();assert.equal(r.status,expected,JSON.stringify(data));return data;}
try{
 await post('/api/setup',{username:'native-stop-test',name:'合成本机停止测试',password:randomUUID()});await post('/api/settings',{version:1,modelProvider:'qwen_local'});await post('/api/settings',{version:2,modelDataConsent:true});
 const c=await post('/api/customers',{alias:'合成停止验收客户',synthetic:true});await post(`/api/customers/${c.id}/messages`,{text:'你好',synthetic:true});const draft=await post(`/api/customers/${c.id}/generate`,{});const jobId=randomUUID();
 const pending=post(`/api/drafts/${draft.id}/model`,{jobId},409);await entered;await new Promise(r=>setTimeout(r,100));const started=Date.now();const stopped=await post(`/api/drafts/${draft.id}/stop-model`,{jobId});assert.equal(stopped.stopped,true);await pending;
 const s=app.store.read();assert.deepEqual(s.drafts[0],draft);assert.equal(s.costs[0].status,'canceled');assert.equal(s.tasks.length,0);assert.equal(localQwen.busy,false);
 const result={status:'passed',date:'2026-10-27',model:'qwen3-vl:4b-instruct',material:'built-in synthetic greeting only',actualLocalHTTPRequestStarted:true,stopToCompletionMs:Date.now()-started,originalTextAndRevisionPreserved:true,costAttemptStatus:'canceled',actualExternalModelCalls:0,realCustomerDataUsed:false,realWechatMessagesSent:0,note:'Stopped a real local model HTTP request; does not establish GPU memory release timing or real customer acceptance'};
 if(process.env.AVATAR_NATIVE_STOP_OUTPUT)writeFileSync(process.env.AVATAR_NATIVE_STOP_OUTPUT,JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}finally{await app.close();rmSync(directory,{recursive:true,force:true});}
