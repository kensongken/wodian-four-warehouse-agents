// Background-only real-model acceptance with an isolated encrypted ledger.
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,rmSync,writeFileSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createApp} from '../server.mjs';
import {LocalQwen} from '../lib/qwen-model.mjs';
const directory=mkdtempSync(join(tmpdir(),'studio-conversation-'));
const out=resolve(process.env.AVATAR_QA_OUTPUT||'.acceptance/persona-reception');mkdirSync(out,{recursive:true});
const modelOutputs=[],native=new LocalQwen();
const app=createApp({directory,worker:false,localQwen:{close(){native.close();},async complete(payload){const response=await native.complete(payload);modelOutputs.push(response.choices[0].message.content);return response;}}});let cookie='';const turns=[];
const report={version:JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8')).version,fixture:'User-provided owner utterances with agent-authored rehearsal questions; isolated encrypted ledger. Rehearsal of changed preference, styling follow-up, browsing and verification; no likeness rating.',realWechatCalls:0,modelOutputs,turns};
try{
 await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;
 async function post(path,body){const response=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie},body:JSON.stringify(body)});if(response.headers.has('set-cookie'))cookie=response.headers.get('set-cookie').split(';')[0];const data=await response.json();if(!response.ok)throw Error(JSON.stringify(data));return data;}
 await post('/api/setup',{username:'rehearsal-owner',password:randomUUID()});
 await post('/api/persona-studio/save',{version:0,name:'店主分身 · 热情直接',addressing:'你',length:'简短',warmth:'自然直接',emoji:'不用',phrases:'',avoid:'',confirmed:true,examples:[{question:'【演练场景】你好，我先看看。',answer:'你今天非常的漂亮'},{question:'【演练场景】我有点犹豫，还没决定。',answer:'你为什么还不买，东西这么好'},{question:'【演练场景】我再考虑一下。',answer:'别考虑了，考虑了就没了'}]});
 let previous;
 for(const question of ['我想自己戴，选素一点、细一点的手链。','改主意了，不要素一点的了，想要有设计感、宽一点的。','平时穿衬衫，怎么搭配比较好？','我再逛逛，今天先不买。','最低多少钱？','不合适可以退吗？']){
  const started=Date.now();const trial=await post('/api/persona-studio/preview',{version:1,question,previousTrialId:previous?.id});
  assert.equal(trial.sent,false);assert.equal(trial.rating,null);assert.equal(trial.contextTurns,Math.min(turns.length,3));
  turns.push({question,reply:trial.reply,replyGoal:trial.replyGoal,contextTurns:trial.contextTurns,elapsedMs:Date.now()-started,sent:trial.sent,rating:trial.rating});previous=trial;console.log(JSON.stringify(turns.at(-1)));
 }
 assert.equal(app.store.read().tasks.length,0);assert.equal(app.store.read().messages.length,0);report.status='passed';report.realModelCalls=turns.length;
}catch(error){report.status='failed';report.error=String(error);process.exitCode=1;}
finally{writeFileSync(join(out,'real-conversation.json'),JSON.stringify(report,null,2));await app.close();rmSync(directory,{recursive:true,force:true});}
