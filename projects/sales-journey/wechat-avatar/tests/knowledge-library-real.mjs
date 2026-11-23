// Synthetic materials, real local Qwen. Never uses the owner's actual ledger or WeChat.
import {createApp} from '../server.mjs';
import {LocalQwen} from '../lib/qwen-model.mjs';
import {mkdtempSync,mkdirSync,rmSync,writeFileSync,readFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const directory=mkdtempSync(join(tmpdir(),'knowledge-real-')),out=resolve(process.env.AVATAR_QA_OUTPUT||'.acceptance/knowledge-real');mkdirSync(out,{recursive:true});
const calls=[],native=new LocalQwen(),app=createApp({directory,worker:false,localQwen:{close(){native.close();},async complete(payload){const start=Date.now();const result=await native.complete(payload);calls.push({input:JSON.parse(payload.messages[1].content),output:result.choices[0].message.content,elapsedMs:Date.now()-start});return result;}}});
const report={version:JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8')).version,fixture:'Synthetic owner, voice, experience and shop facts. Not actual shop data or owner likeness acceptance.',realWechatCalls:0,actualOwnerRatings:0,calls,checks:[],trials:[]};let cookie='';
try{
 await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;
 async function post(path,body){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];const data=await r.json();if(!r.ok)throw Error(JSON.stringify(data));return data;}
 await post('/api/setup',{username:'synthetic-owner',password:randomUUID()});
 const records=[];
 for(const [kind,question,answer] of [['voice','我有点犹豫，怕戴起来不适合。','不急，你先说说担心什么，我们一起看。'],['experience','我有点犹豫，怕戴起来不适合。','先问客户担心的是款式还是搭配，再了解平时的穿着，不急着让客户决定。'],['faq','周日营业时间是什么？','合成演练门店周日营业时间为10:00至18:00。']]){
  const imported=await post('/api/knowledge/import',{kind,title:'合成资料联测',source:'合成演练资料，不是真实门店',text:`问题：${question}\n回答：${answer}`,confirmed:true});const item=imported.items[0];assert.equal(item.status,'draft');
  records.push(await post('/api/knowledge/review',{...item,action:'approve',confirmed:true,expires:Date.now()+86400000}));
 }
 report.checks.push('three material types imported as drafts and confirmed using authenticated HTTP');
 await post('/api/persona-studio/save',{version:0,name:'合成演练店主',addressing:'你',length:'简短',warmth:'自然直接',emoji:'不用',phrases:'',avoid:'强行催买、催促立刻决定',examples:[],confirmed:true});
 const faq=records.find(x=>x.kind==='faq');const factual=await post('/api/persona-studio/preview',{version:1,question:faq.question});report.trials.push(factual);
 assert.equal(factual.reply,faq.answer);assert.equal(factual.answerKnowledgeId,faq.id);assert.equal(factual.replySource,'approved_faq');assert.equal(factual.generationSource,'approved_lookup');report.checks.push('approved hours are quoted directly and exactly, without asking the model to invent facts');console.log(JSON.stringify({stage:'confirmed_faq',reply:factual.reply}));
 const conversational=await post('/api/persona-studio/preview',{version:1,question:'我有点犹豫，怕戴起来不适合。'});report.trials.push(conversational);
 assert.ok(conversational.knowledgeReferences.some(x=>x.kind==='experience'));assert.ok(calls.at(-1).input.styleExamplesOnly.some(x=>x.answer===records[0].answer));report.checks.push('confirmed voice and experience supplied to actual local Qwen');console.log(JSON.stringify({stage:'voice_and_experience',reply:conversational.reply}));
 await post('/api/knowledge/review',{id:faq.id,version:faq.version,action:'withdraw'});assert.equal(app.store.read().personaStudio.trials[0].blocked,true);
 try{const after=await post('/api/persona-studio/preview',{version:1,question:faq.question});report.trials.push(after);assert.notEqual(after.reply,faq.answer);assert.equal(after.answerKnowledgeId,null);assert.ok(!after.knowledgeReferences.some(x=>x.id===faq.id));console.log(JSON.stringify({stage:'after_withdraw',reply:after.reply}));}catch(error){if(!String(error).includes('待核实'))throw error;report.afterWithdraw='unverified response blocked';}
 assert.ok(!calls.at(-1).input.approvedKnowledge.some(x=>x.id===faq.id));report.checks.push('withdrawal removes FAQ from prompt and quarantines its old answer');
 assert.equal(app.store.read().tasks.length,0);assert.equal(app.store.read().messages.length,0);report.checks.push('no send tasks or real messages');report.status='passed';
}catch(error){report.status='failed';report.error=String(error);process.exitCode=1;}
finally{writeFileSync(join(out,'real-knowledge.json'),JSON.stringify(report,null,2));await app.close();rmSync(directory,{recursive:true,force:true});}
