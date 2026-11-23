// Isolated rehearsal using user-provided utterances and the user's explicit less-pressure choice.
import {createApp} from '../server.mjs';
import {LocalQwen} from '../lib/qwen-model.mjs';
import {mkdtempSync,mkdirSync,rmSync,readFileSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const directory=mkdtempSync(join(tmpdir(),'owner-soft-tone-')),out=resolve(process.env.AVATAR_QA_OUTPUT||'.acceptance/persona-low-pressure');mkdirSync(out,{recursive:true});
const modelOutputs=[],native=new LocalQwen(),app=createApp({directory,worker:false,localQwen:{close(){native.close();},async complete(payload){const response=await native.complete(payload);modelOutputs.push(response.choices[0].message.content);return response;}}});
const report={appVersion:JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8')).version,ownerDecision:'学热情直接的感觉，少催买',questions:'agent-authored rehearsal only; user utterances unchanged',actualOwnerRatings:0,realWechatCalls:0,modelOutputs,turns:[]};
try{
 const owner=app.engine.setup({username:'synthetic-owner',password:randomUUID()});
 app.personaStudio.save(owner,{version:0,name:'店主分身 · 热情直接',addressing:'你',length:'简短',warmth:'自然直接',emoji:'不用',phrases:'',avoid:'强行催买、催促立刻决定、你为什么还不买、别考虑了、别犹豫、虚构稀缺或涨价',confirmed:true,examples:[{question:'【演练场景】你好，我先看看。',answer:'你今天非常的漂亮'},{question:'【演练场景】我有点犹豫，还没决定。',answer:'你为什么还不买，东西这么好'},{question:'【演练场景】我再考虑一下。',answer:'别考虑了，考虑了就没了'}]});
 for(const question of ['我有点犹豫，怕戴起来不适合。','我再考虑一下。','我今天先不买，再看看。','谢谢，我晚点再来。']){
  const started=Date.now();const trial=await app.personaStudio.preview(owner,{version:1,question});
  report.turns.push({question,reply:trial.reply,replyGoal:trial.replyGoal,elapsedMs:Date.now()-started,sent:trial.sent,rating:trial.rating});console.log(JSON.stringify(report.turns.at(-1)));
  assert.ok(!/为什么还不买|别考虑|别犹豫|赶紧买|马上买|现在就买|错过|快没|只剩/u.test(trial.reply),'回复含明确不喜欢的催买词');assert.equal(trial.sent,false);assert.equal(trial.rating,null);
 }
 assert.equal(app.store.read().tasks.length,0);report.status='passed';
}catch(error){report.status='failed';report.error=String(error);process.exitCode=1;}
finally{writeFileSync(join(out,'real-low-pressure.json'),JSON.stringify(report,null,2));await app.close();rmSync(directory,{recursive:true,force:true});}
