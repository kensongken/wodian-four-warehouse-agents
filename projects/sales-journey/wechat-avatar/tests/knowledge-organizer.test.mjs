import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {KnowledgeLibrary,selectKnowledge} from '../lib/knowledge-library.mjs';
import {QWEN_MODEL} from '../lib/qwen-model.mjs';
import {readKnowledgeTextFile,knowledgeView} from '../web/knowledge-library.js';
import {createApp} from '../server.mjs';
const input={kind:'faq',title:'合成门店说明',source:'合成演练文件',text:'合成门店周日营业时间为10:00至18:00。周一休息。',confirmed:true};
const item={question:'周日营业时间是什么？',answer:'合成门店周日营业时间为10:00至18:00。'};
function fixture(t,complete=async()=>({choices:[{message:{content:JSON.stringify({items:[item]})}}]})){
 const directory=mkdtempSync(join(tmpdir(),'organizer-test-')),store=new Store(directory),engine=new Engine(store),owner=engine.setup({username:'owner',password:'organizer-test-password'}),library=new KnowledgeLibrary(engine,{model:{complete}});t.after(()=>{store.close();rmSync(directory,{recursive:true,force:true});});return{store,engine,owner,library};
}
test('local organization stores only unapproved extracts with model origin and original source retained',async t=>{
 let payload;const f=fixture(t,async p=>{payload=p;return{choices:[{message:{content:JSON.stringify({items:[item]})}}]};});const result=await f.library.organize(f.owner,input);
 assert.equal(payload.model,QWEN_MODEL);assert.equal(payload.response_format.type,'json_object');assert.equal(result.items[0].status,'draft');assert.equal(result.items[0].origin,'local_model_extract');assert.equal(result.items[0].originalExtract,item.answer);assert.equal(f.store.read().knowledgeLibrary.imports[0].raw,input.text);assert.deepEqual(selectKnowledge(f.store.read(),item.question,Date.now()),[]);
 const html=knowledgeView({...f.engine.snapshot(f.owner),settings:{knowledgeInbox:'/local/<script>资料</script>'}});assert.ok(html.includes('&lt;script&gt;资料&lt;/script&gt;'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('问题为拟写'));assert.ok(html.includes('对照原文摘录'));assert.equal(f.store.read().tasks.length,0);
 f.library.review(f.owner,{...result.items[0],action:'approve',confirmed:true,expires:Date.now()+86400000});assert.equal(selectKnowledge(f.store.read(),item.question,Date.now())[0].answer,item.answer);
});
test('unlabelled owner voice, missing permission, oversized input and non-owner requests never reach model',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('must not call');});
 for(const data of [{...input,kind:'voice'},{...input,confirmed:false},{...input,text:'字'.repeat(1601)}])await assert.rejects(()=>f.library.organize(f.owner,data));
 await assert.rejects(()=>f.library.organize({role:'employee'},input),/老板/);assert.equal(calls,0);assert.equal(f.store.read().knowledgeLibrary.imports.length,0);
});
test('hallucinated answers, negation-trimming, extra fields and incomplete output never persist',async t=>{
 const outputs=['null','{"items":[]}',JSON.stringify({items:[{...item,answer:'每天24小时营业。'}]}),JSON.stringify({items:[{question:'周日开门吗？',answer:'营业。'}]}),JSON.stringify({items:[{...item,approved:true}]}),JSON.stringify({items:[item,item]})];
 const f=fixture(t,async()=>({choices:[{message:{content:outputs.shift()}}]}));
 for(let i=0;i<6;i++)await assert.rejects(()=>f.library.organize(f.owner,{...input,text:input.text+'周日不营业。'}));
 assert.equal(f.store.read().knowledgeLibrary.imports.length,0);assert.equal(f.library.organizing,false);
});
test('concurrent organizer calls are refused and a changed library invalidates late extraction',async t=>{
 let finish;const f=fixture(t,()=>new Promise(resolve=>finish=resolve));const pending=f.library.organize(f.owner,input);
 await assert.rejects(()=>f.library.organize(f.owner,input),/上一份/);f.library.import(f.owner,{...input,text:'问题：门店在哪里？\n回答：合成演练路18号。'});
 finish({choices:[{message:{content:JSON.stringify({items:[item]})}}]});await assert.rejects(()=>pending,/资料库已变化/);assert.equal(f.store.read().knowledgeLibrary.imports.length,1);assert.equal(f.library.organizing,false);
});
test('provider failure is visible with no fallback, saved drafts or stuck busy flag',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('local model offline');});await assert.rejects(()=>f.library.organize(f.owner,input),/offline/);assert.equal(calls,1);assert.equal(f.store.read().knowledgeLibrary.items.length,0);assert.equal(f.library.organizing,false);
});
test('TXT reader accepts Chinese UTF-8 and rejects unsupported, corrupt, binary or oversized files',async()=>{
 const parsed=await readKnowledgeTextFile(new File([input.text],'门店说明.TXT',{type:'text/plain'}));assert.equal(parsed.text,input.text);assert.equal(parsed.title,'门店说明');
 const files=[new File(['text'],'说明.pdf'),new File([new Uint8Array([0xff,0xfe,0x80])],'说明.txt'),new File(['a\0b'],'说明.txt'),new File(['x'.repeat(32001)],'说明.txt'),new File([' '],'说明.txt')];for(const file of files)await assert.rejects(()=>readKnowledgeTextFile(file));
});
test('organize HTTP route is authenticated, same-origin and never silently approves results',async t=>{
 const directory=mkdtempSync(join(tmpdir(),'organizer-http-'));let calls=0;const app=createApp({directory,worker:false,localQwen:{close(){},async complete(){calls++;return{choices:[{message:{content:JSON.stringify({items:[item]})}}]};}}});await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';t.after(async()=>{await app.close();rmSync(directory,{recursive:true,force:true});});
 async function post(path,body,extra={}){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie,...extra},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];return{status:r.status,data:await r.json()};}
 assert.equal((await post('/api/knowledge/organize',input)).status,401);await post('/api/setup',{username:'owner',password:'test-organizer-http-password'});assert.equal((await post('/api/knowledge/organize',input,{Origin:'https://elsewhere.invalid'})).status,403);const response=await post('/api/knowledge/organize',input);assert.equal(response.status,200);assert.equal(response.data.items[0].status,'draft');assert.equal(calls,1);assert.equal(app.store.read().tasks.length,0);
});
