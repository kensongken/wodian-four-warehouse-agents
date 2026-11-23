// Real local model organization and use, with synthetic text only.
import {createApp} from '../server.mjs';
import {LocalQwen} from '../lib/qwen-model.mjs';
import {mkdtempSync,mkdirSync,rmSync,readFileSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const directory=mkdtempSync(join(tmpdir(),'organizer-real-')),out=resolve(process.env.AVATAR_QA_OUTPUT||'.acceptance/organizer-real');mkdirSync(out,{recursive:true});
const calls=[],native=new LocalQwen(),app=createApp({directory,worker:false,localQwen:{close(){native.close();},async complete(payload){const at=Date.now();const result=await native.complete(payload);calls.push({input:payload.messages,output:result.choices[0].message.content,elapsedMs:Date.now()-at});return result;}}});
const report={version:JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8')).version,fixture:'Synthetic owner and plain-text shop note; not actual shop data or actual owner acceptance',realWechatCalls:0,actualOwnerRatings:0,calls,checks:[]};let cookie='';
try{
 await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;
 async function post(path,body){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];const data=await r.json();if(!r.ok)throw Error(JSON.stringify(data));return data;}
 await post('/api/setup',{username:'synthetic-owner',password:randomUUID()});
 const text='合成演练门店周日营业时间为10:00至18:00。门店周一休息。';
 const extracted=await post('/api/knowledge/organize',{kind:'faq',title:'合成普通文字说明',source:'合成演练文本，不是真实门店',text,confirmed:true});report.extracted=extracted;
 assert.ok(extracted.items.length>0);assert.ok(extracted.items.every(x=>x.status==='draft'&&x.origin==='local_model_extract'&&text.includes(x.answer)));report.checks.push('plain text becomes only source-grounded drafts, not approved facts');console.log(JSON.stringify({stage:'organized',items:extracted.items.map(({question,answer})=>({question,answer}))}));
 const record=extracted.items.find(x=>x.answer.includes('10:00'));assert.ok(record,'must extract the explicit opening-hours sentence');
 const approved=await post('/api/knowledge/review',{...record,action:'approve',confirmed:true,expires:Date.now()+86400000});
 await post('/api/persona-studio/save',{version:0,name:'合成演练分身',addressing:'你',length:'简短',warmth:'自然直接',emoji:'不用',phrases:'',avoid:'强行催买',examples:[{question:'你好',answer:'你好，有什么想了解的？'}],confirmed:true});
 const trial=await post('/api/persona-studio/preview',{version:1,question:approved.question});report.trial=trial;assert.equal(trial.answerKnowledgeId,approved.id);assert.equal(trial.reply,approved.answer);assert.equal(trial.sent,false);assert.equal(trial.generationSource,'approved_lookup');report.checks.push('after owner-fixture review, confirmed hours are quoted directly with a source; no extra generation');console.log(JSON.stringify({stage:'reply_after_review',reply:trial.reply}));
 assert.equal(app.store.read().tasks.length,0);report.checks.push('no WeChat messages or send tasks');report.status='passed';
}catch(error){report.status='failed';report.error=String(error);process.exitCode=1;}
finally{writeFileSync(join(out,'real-organizer.json'),JSON.stringify(report,null,2));await app.close();rmSync(directory,{recursive:true,force:true});}
