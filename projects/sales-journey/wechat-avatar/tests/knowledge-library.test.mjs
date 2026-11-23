import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {KnowledgeLibrary,parseKnowledge,selectKnowledge} from '../lib/knowledge-library.mjs';
import {PersonaStudio} from '../lib/persona-studio.mjs';
import {createApp} from '../server.mjs';
import {knowledgeView} from '../web/knowledge-library.js';
import {studioView} from '../web/persona-studio.js';
test('ordinary reception records approved voice provenance and withdrawal invalidates the dependent trial',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"可以先看线条利落、带一点细节的方向。"}'}}]};});f.studio.save(f.owner,profile);const question='宽一点怕太夸张，细一点又觉得没特点，怎么办？';
 const voice=approve(f,draft(f,'voice',question,'你可以先试试'));const pending=draft(f,'voice',question,'没批准的本人表达');const trial=await f.studio.preview(f.owner,{version:1,question});
 assert.ok(payload.ownerVoiceReferences.some(x=>x.expression===voice.answer));assert.ok(!JSON.stringify(payload).includes(pending.answer));assert.ok(trial.styleSamplesUsed.some(x=>x.knowledgeId===voice.id));assert.ok(trial.knowledgeReferences.some(x=>x.id===voice.id&&x.version===voice.version));
 f.library.review(f.owner,{id:voice.id,version:voice.version,action:'withdraw'});assert.equal(f.store.read().personaStudio.trials.find(x=>x.id===trial.id).blocked,true);assert.equal(trial.rating,null);
});
const profile={version:0,name:'合成店主',addressing:'你',length:'简短',warmth:'自然直接',emoji:'不用',phrases:'',avoid:'',confirmed:true,examples:[{question:'我先看看',answer:'好，你慢慢看。'}]};
function fixture(t,complete=async()=>({choices:[{message:{content:'{"reply":"好，你说说顾虑，我陪你看看。"}'}}]})){
 const directory=mkdtempSync(join(tmpdir(),'knowledge-test-')),store=new Store(directory);let now=Date.now();const engine=new Engine(store,()=>now),owner=engine.setup({username:'owner',password:'knowledge-test-password'}),library=new KnowledgeLibrary(engine),studio=new PersonaStudio(engine,{library,model:{complete}});
 t.after(()=>{store.close();rmSync(directory,{recursive:true,force:true});});return{directory,store,engine,owner,library,studio,advance:ms=>now+=ms};
}
function draft(f,kind='faq',question='门店周日营业时间是什么？',answer='周日营业时间为10:00至18:00。'){
 return f.library.import(f.owner,{kind,title:'隔离演练资料',source:'合成测试说明，不是真实门店',text:`问题：${question}\n回答：${answer}`,confirmed:true}).items[0];
}
function approve(f,item,overrides={}){return f.library.review(f.owner,{id:item.id,version:item.version,action:'approve',question:item.question,answer:item.answer,source:item.source,expires:f.engine.clock()+86400000,confirmed:true,...overrides});}
test('parser preserves role-labelled dialogue and refuses ambiguous or oversized pairs',()=>{
 assert.deepEqual(parseKnowledge('客户：我有点犹豫\n担心不好搭\n店主：你平时穿什么？\n问题：我再看看\n回答：好，不急。'),[{question:'我有点犹豫\n担心不好搭',answer:'你平时穿什么？'},{question:'我再看看',answer:'好，不急。'}]);
 for(const raw of ['随便一段文字','店主：没有问题','问题：没有回答',Array.from({length:13},()=>`问题：你好\n回答：你好`).join('\n'),`问题：你好\n回答：${'长'.repeat(181)}`])assert.throws(()=>parseKnowledge(raw));
});
test('imports remain encrypted owner-only drafts and cannot be retrieved before explicit review',t=>{
 const f=fixture(t),item=draft(f);assert.equal(item.status,'draft');assert.deepEqual(selectKnowledge(f.store.read(),item.question,f.engine.clock()),[]);
 assert.ok(!readFileSync(join(f.directory,'avatar.sqlite')).includes(Buffer.from('合成测试说明')));
 assert.equal(f.engine.snapshot({id:'employee',role:'employee'}).knowledgeLibrary,undefined);
 assert.throws(()=>f.library.import({role:'employee'},{confirmed:true}),/老板/);assert.throws(()=>f.library.import(f.owner,{confirmed:false}),/有权/);
 assert.throws(()=>approve(f,item,{confirmed:false}),/逐条核实/);assert.throws(()=>approve(f,item,{version:2}),/版本/);
});
test('facts require source, finite future expiry and a non-transactional scope',t=>{
 const f=fixture(t),item=draft(f);
 assert.throws(()=>approve(f,item,{source:''}),/来源/);assert.throws(()=>approve(f,item,{expires:f.engine.clock()}),/有效期/);
 assert.throws(()=>approve(f,item,{question:'最低多少钱？',answer:'最低99元。'}),/非交易/);
 assert.throws(()=>approve(f,item,{question:'不合适退货吗？',answer:'可以退。'}),/非交易/);
 const experience=draft(f,'experience','客户嫌贵怎么聊？','给他99元。');assert.throws(()=>approve(f,experience),/方法/);
 const approved=approve(f,item);assert.equal(approved.version,2);assert.equal(selectKnowledge(f.store.read(),item.question,f.engine.clock())[0].id,item.id);
});
test('confirmed voice and experience reach the model, with exact references recorded and no send tasks',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"你主要担心哪一点？我帮你一起看看。"}'}}]};});f.studio.save(f.owner,profile);
 const voice=approve(f,draft(f,'voice','我有点犹豫，怕戴起来不适合。','你最担心哪一点？跟我说说。'));
 const experience=approve(f,draft(f,'experience','我有点犹豫，怕戴起来不适合。','先问清担心的是款式还是搭配，再继续了解需求。'));
 const ignored=draft(f,'experience','我有点犹豫，怕戴起来不适合。','未批准方法不能参考。');
 const trial=await f.studio.preview(f.owner,{version:1,question:voice.question});
 assert.ok(payload.styleExamplesOnly.some(x=>x.answer===voice.answer));assert.ok(payload.approvedKnowledge.some(x=>x.id===experience.id));assert.ok(!payload.approvedKnowledge.some(x=>x.id===ignored.id));
 assert.deepEqual(new Set(trial.knowledgeReferences.map(x=>x.id)),new Set([voice.id,experience.id]));assert.equal(trial.sent,false);assert.equal(f.store.read().tasks.length,0);
});
test('new owners can save expression preferences using confirmed library voice without duplicating samples',t=>{
 const f=fixture(t);approve(f,draft(f,'voice','我先看看','不急，慢慢挑。'));
 const saved=f.studio.save(f.owner,{...profile,examples:[]});assert.equal(saved.examples.length,0);assert.equal(saved.version,1);
});
test('a factual reply must exactly quote a selected approved FAQ; restart keeps that valid quote usable',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);const faq=payload.approvedKnowledge.find(x=>x.kind==='faq');return{choices:[{message:{content:JSON.stringify({reply:faq.answer,knowledgeId:faq.id})}}]};});f.studio.save(f.owner,profile);const item=approve(f,draft(f));
 const trial=await f.studio.preview(f.owner,{version:1,question:item.question});assert.equal(trial.reply,item.answer);assert.equal(trial.answerKnowledgeId,item.id);assert.equal(trial.replySource,'approved_faq');assert.equal(trial.replyGoal,'answer_verified_faq');assert.ok(f.library.grounded(trial));
 new PersonaStudio(f.engine,{library:f.library,model:{complete(){throw Error('no inference');}}});assert.ok(!f.store.read().personaStudio.trials[0].blocked);
 assert.throws(()=>f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'like'}),/不进入口吻/);
 assert.ok(studioView(f.engine.snapshot(f.owner)).includes('回答引用已核实门店问答原文'));assert.ok(!studioView(f.engine.snapshot(f.owner)).includes('data-action="studio-rate"'));
});
test('invented citations, changed fact wording and uncited numeric facts never become usable replies',async t=>{
 const replies=[];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify(replies.shift())}}]}));f.studio.save(f.owner,profile);const item=approve(f,draft(f,'faq','门店活动安排是什么？','合成演练活动从10:00至18:00。'));
 replies.push({reply:item.answer,knowledgeId:'made-up'},{reply:'每天24小时营业。',knowledgeId:item.id},{reply:'每天24小时营业。'});
 for(let i=0;i<3;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:item.question}));assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('withdrawal and expiry stop retrieval and quarantine dependent old trials',async t=>{
 const f=fixture(t,async p=>{const faq=JSON.parse(p.messages[1].content).approvedKnowledge.find(x=>x.kind==='faq');return{choices:[{message:{content:JSON.stringify({reply:faq.answer,knowledgeId:faq.id})}}]};});f.studio.save(f.owner,profile);const item=approve(f,draft(f));
 const trial=await f.studio.preview(f.owner,{version:1,question:item.question});f.library.review(f.owner,{id:item.id,version:2,action:'withdraw'});assert.equal(f.store.read().personaStudio.trials[0].blocked,true);assert.ok(!f.library.grounded(trial));assert.deepEqual(selectKnowledge(f.store.read(),item.question,f.engine.clock()),[]);
 const second=approve(f,draft(f));await f.studio.preview(f.owner,{version:1,question:second.question});f.advance(86400001);f.library.expire();const s=f.store.read();assert.equal(s.knowledgeLibrary.items.at(-1).status,'expired');assert.equal(s.personaStudio.trials.at(-1).blocked,true);
});
test('a source withdrawn while generation runs invalidates the late reply',async t=>{
 let finish;const f=fixture(t,p=>new Promise(resolve=>{const faq=JSON.parse(p.messages[1].content).approvedKnowledge.find(x=>x.kind==='faq');finish=()=>resolve({choices:[{message:{content:JSON.stringify({reply:faq.answer,knowledgeId:faq.id})}}]});}));f.studio.save(f.owner,profile);const item=approve(f,draft(f,'faq','门店活动安排是什么？','合成演练活动从10:00至18:00。'));
 const pending=f.studio.preview(f.owner,{version:1,question:item.question});f.library.review(f.owner,{id:item.id,version:2,action:'withdraw'});finish();await assert.rejects(()=>pending,/资料已变化/);assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('knowledge UI escapes raw content and exposes draft confirmation, source and expiry',t=>{
 const f=fixture(t);const item=draft(f,'faq','门店在哪里？','<script>不是可执行代码</script>');const html=knowledgeView(f.engine.snapshot(f.owner));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('确认并供试聊参考'));assert.ok(html.includes('有效至'));assert.ok(html.includes(item.source));
});
test('knowledge HTTP endpoints retain auth and origin checks and persist approved facts across restart',async t=>{
 const directory=mkdtempSync(join(tmpdir(),'knowledge-http-'));let app=createApp({directory,worker:false}),cookie='';
 await new Promise(done=>app.server.listen(0,'127.0.0.1',done));let origin=`http://127.0.0.1:${app.server.address().port}`;
 t.after(async()=>{await app.close();rmSync(directory,{recursive:true,force:true});});
 async function post(path,body,extra={}){const response=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie,...extra},body:JSON.stringify(body)});if(response.headers.has('set-cookie'))cookie=response.headers.get('set-cookie').split(';')[0];return{status:response.status,data:await response.json()};}
 const input={kind:'faq',title:'合成门店说明',source:'合成演练说明',text:'问题：门店在哪里？\n回答：合成演练路18号。',confirmed:true};
 assert.equal((await post('/api/knowledge/import',input)).status,401);await post('/api/setup',{username:'owner',password:'test-knowledge-http-password'});assert.equal((await post('/api/knowledge/import',input,{Origin:'https://unrelated.invalid'})).status,403);
 const imported=await post('/api/knowledge/import',input);assert.equal(imported.status,200);const item=imported.data.items[0];assert.equal((await post('/api/knowledge/review',{...item,action:'approve',confirmed:true,expires:Date.now()+86400000})).status,200);
 await app.close();app=createApp({directory,worker:false});await new Promise(done=>app.server.listen(0,'127.0.0.1',done));origin=`http://127.0.0.1:${app.server.address().port}`;
 const response=await fetch(origin+'/api/state',{headers:{Cookie:cookie}});assert.equal(response.status,200);assert.equal((await response.json()).knowledgeLibrary.items[0].status,'approved');
});

test('an exact unique approved answer can be attributed by server matching when the model omits its ID',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:'合成演练活动从10:00至18:00。'})}}]}));f.studio.save(f.owner,profile);const item=approve(f,draft(f,'faq','门店活动安排是什么？','合成演练活动从10:00至18:00。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:item.question});assert.equal(trial.answerKnowledgeId,item.id);assert.equal(trial.citationMethod,'exact_approved_answer_match');assert.equal(trial.reply,item.answer);
});
test('explicit basic shop questions quote confirmed facts directly without a model call',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('should not ask model to invent address');});f.studio.save(f.owner,profile);
 const item=approve(f,draft(f,'faq','门店在哪里？地址是什么？','【模拟地址】演练市示例区体验路18号。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'细一点的。门店在哪里？'});assert.equal(trial.reply,item.answer);assert.equal(trial.source,'approved_faq_lookup');assert.equal(trial.generationSource,'approved_lookup');assert.equal(trial.citationMethod,'approved_lookup');assert.equal(calls,0);assert.equal(trial.sent,false);
});
test('conflicting current facts are not silently chosen, and withdrawal resolves the conflict',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('must not resolve conflicting facts by guessing');});f.studio.save(f.owner,profile);
 const first=approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。')),second=approve(f,draft(f,'faq','门店在哪里？','合成演练路20号。'));
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'门店在哪里？'}),/不同答案/);assert.equal(calls,0);
 f.library.review(f.owner,{id:second.id,version:second.version,action:'withdraw'});const trial=await f.studio.preview(f.owner,{version:1,question:'门店在哪里？'});assert.equal(trial.reply,first.answer);
});
test('an ambiguous concern containing 哪里 is not mistaken for an address request',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;return{choices:[{message:{content:'{"reply":"你平时穿搭是什么风格？"}'}}]};});f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'我担心哪里不合适'});assert.equal(trial.replyGoal,'clarify_concern');assert.equal(trial.answerKnowledgeId,null);assert.equal(calls,1);
});
test('unknown colloquial shop location and hours require verification rather than echoing the question',async t=>{
 const replies=['你家在哪，明天几点开门？','这部分我先核实清楚，再跟你说。'];let payload;
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,profile);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你家在哪，明天几点开门？'}),/重复客户问题/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，明天几点开门？'});assert.equal(payload.replyGoal,'verify_only');assert.equal(trial.requiresVerification,true);assert.equal(trial.sent,false);assert.equal(trial.answerKnowledgeId,null);
});
test('compound shop questions cannot quote only a known address and silently omit unknown opening hours',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"具体情况我先核实，再跟你说。"}'}}]}));f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，明天几点开门？'});assert.equal(trial.replyGoal,'verify_only');assert.equal(trial.generationSource,'local_qwen');assert.equal(trial.answerKnowledgeId,null);
});
test('a colloquial shop location question can still quote its single confirmed answer',async t=>{
 const f=fixture(t,async()=>{throw Error('no generation needed');});f.studio.save(f.owner,profile);const item=approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪？'});assert.equal(trial.reply,item.answer);assert.equal(trial.generationSource,'approved_lookup');
});
test('knowledge gaps are owner-only, read-only and count only current confirmed unexpired basic facts',t=>{
 const f=fixture(t);f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店叫什么名字？','合成演练店。'));const address=approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。'));
 f.library.review(f.owner,{id:address.id,version:address.version,action:'withdraw'});const before=JSON.stringify(f.store.read());const gaps=f.library.gaps(f.owner);assert.equal(gaps.basics.find(b=>b.key==='name').status,'confirmed');assert.equal(gaps.basics.find(b=>b.key==='address').status,'missing');assert.equal(gaps.basics.find(b=>b.key==='hours').status,'missing');assert.equal(JSON.stringify(f.store.read()),before);assert.throws(()=>f.library.gaps({role:'employee'}),/老板/);
 f.advance(86400001);assert.equal(f.library.gaps(f.owner).basics.find(b=>b.key==='name').status,'missing');
});
test('knowledge gaps separate business-data needs from missing facts and exclude old-version or repeated questions',t=>{
 const f=fixture(t);const p=f.studio.save(f.owner,profile);f.store.transact(s=>{s.personaStudio.trials=[{id:'a',profileId:p.id,question:'你家在哪，明天几点开门？',requiresVerification:true},{id:'b',profileId:p.id,question:'最低多少钱，不合适能退吗？',requiresVerification:true},{id:'repeat',profileId:p.id,question:'最低多少钱，不合适能退吗？',requiresVerification:true},{id:'old',profileId:'old',question:'旧口吻问题',requiresVerification:true},{id:'blocked',profileId:p.id,question:'被拦截的旧回复',requiresVerification:true,blocked:true}];});
 const g=f.library.gaps(f.owner);assert.equal(g.questions.length,2);assert.equal(g.questions.find(q=>q.trialId==='a').resolution,'missing_faq');assert.equal(g.questions.find(q=>q.trialId==='repeat').resolution,'business_data');assert.ok(g.questions.every(q=>q.questionSource==='studio_rehearsal'));
});
test('newly confirmed facts resolve single and combined gaps while conflicts remain explicit',t=>{
 const f=fixture(t),p=f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店在哪里？','合成演练路18号。'));approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));
 f.store.transact(s=>s.personaStudio.trials.push({id:'single',profileId:p.id,question:'你家在哪？',requiresVerification:true},{id:'combined',profileId:p.id,question:'你家在哪，几点开门？',requiresVerification:true}));
 const g=f.library.gaps(f.owner);assert.deepEqual(g.questions,[]);approve(f,draft(f,'faq','门店在哪里？','合成演练路20号。'));const conflict=f.library.gaps(f.owner);assert.equal(conflict.basics.find(b=>b.key==='address').status,'conflict');assert.ok(conflict.questions.every(q=>q.resolution==='conflicting_faq'));
});
test('compound basic questions quote each approved answer exactly with separate provenance and no inference',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('facts must not be regenerated');});f.studio.save(f.owner,profile);
 const address=approve(f,draft(f,'faq','门店地址在哪里？','【模拟地址】合成演练路18号。'));
 const hours=approve(f,draft(f,'faq','门店营业时间是什么？','【模拟时间】周日10:00至18:00。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'});assert.equal(trial.reply,address.answer+'\n'+hours.answer);assert.equal(trial.answerKnowledgeId,null);assert.deepEqual(trial.answerKnowledgeIds,[address.id,hours.id]);assert.deepEqual(trial.knowledgeReferences.map(r=>r.id),[address.id,hours.id]);assert.equal(trial.generationSource,'approved_lookup');assert.equal(trial.requiresVerification,false);assert.equal(trial.replySource,'approved_faq');assert.equal(trial.sent,false);assert.equal(calls,0);assert.equal(f.library.grounded(trial),true);assert.throws(()=>f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'like'}),/门店问答/);
 const tampered={...trial,reply:trial.reply+'另外保证包邮'};assert.equal(f.library.grounded(tampered),false);
});
test('withdrawing any quoted part invalidates the whole combined reply and restoring facts does not revive old records',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"这部分先核实，再跟你说。"}'}}]}));f.studio.save(f.owner,profile);const address=approve(f,draft(f,'faq','门店地址在哪里？','合成演练路18号。')),hours=approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'});f.library.review(f.owner,{id:hours.id,version:hours.version,action:'withdraw'});assert.equal(f.library.grounded(trial),false);assert.equal(f.store.read().personaStudio.trials.find(x=>x.id===trial.id).blocked,true);
 const fallback=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'});assert.equal(fallback.replyGoal,'verify_only');assert.equal(fallback.answerKnowledgeIds.length,0);approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));assert.equal(f.store.read().personaStudio.trials.find(x=>x.id===trial.id).blocked,true);assert.ok(address.id);
});
test('expiry of a quoted part invalidates combined grounding and conflicts never produce a stitched answer',async t=>{
 const f=fixture(t,async()=>{throw Error('must not guess conflict');});f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店地址在哪里？','合成演练路18号。'));approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'});f.advance(86400001);assert.equal(f.library.grounded(trial),false);f.library.expire();assert.equal(f.store.read().personaStudio.trials[0].blocked,true);
 approve(f,draft(f,'faq','门店地址在哪里？','合成演练路18号。'));approve(f,draft(f,'faq','门店地址在哪里？','合成演练路20号。'));approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'}),/不同答案/);
});
test('known basic facts never allow transaction questions to bypass verification',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"先核实具体情况，再给你答复。"}'}}]}));f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店地址在哪里？','合成演练路18号。'));approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));
 const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门，最低多少钱？'});assert.equal(trial.replyGoal,'verify_only');assert.deepEqual(trial.answerKnowledgeIds,[]);assert.equal(trial.replySource,'model_style');
});
test('long combined facts are refused without truncation and stay visible as a combination gap',async t=>{
 const f=fixture(t,async()=>{throw Error('must not replace exact facts with a guess');});const p=f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店地址在哪里？','【模拟地址】'+('演练位置说明'.repeat(20))));approve(f,draft(f,'faq','门店营业时间是什么？','【模拟时间】'+('营业说明'.repeat(25))));
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'}),/过长.*未截断/);f.store.transact(s=>s.personaStudio.trials.push({id:'long',profileId:p.id,question:'你家在哪，几点开门？',requiresVerification:true}));assert.equal(f.library.gaps(f.owner).questions[0].resolution,'combined_question');assert.equal(f.store.read().personaStudio.trials.length,1);
});
test('combined approved quotes survive application reconstruction without being mistaken for ungrounded numeric facts',async t=>{
 const f=fixture(t,async()=>{throw Error('no inference');});f.studio.save(f.owner,profile);approve(f,draft(f,'faq','门店地址在哪里？','合成演练路18号。'));approve(f,draft(f,'faq','门店营业时间是什么？','周日10:00至18:00。'));const trial=await f.studio.preview(f.owner,{version:1,question:'你家在哪，几点开门？'});
 const restored=createApp({directory:f.directory,worker:false,localQwen:{close(){},complete(){throw Error('no inference');}}});try{assert.equal(restored.knowledge.grounded(restored.store.read().personaStudio.trials.find(t=>t.id===trial.id)),true);assert.ok(!restored.store.read().personaStudio.trials.find(t=>t.id===trial.id).blocked);}finally{await restored.close();}
});
