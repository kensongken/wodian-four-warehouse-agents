import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {PersonaStudio} from '../lib/persona-studio.mjs';
import {QWEN_MODEL} from '../lib/qwen-model.mjs';
import {createApp} from '../server.mjs';
import {studioView} from '../web/persona-studio.js';
test('literal current requirements target stale ordinary advice without accepting caller-supplied need state',async t=>{
 const responses=[{serviceGoal:'choose',reply:'手链就行，上班戴简单款。'},{reply:'你还是选手链，周末出去玩想显眼一些。'}];let body;const f=fixture(t,async p=>{body=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify(responses.shift())}}]};});const profile=f.studio.save(f.owner,input);
 f.store.transact(s=>s.personaStudio.trials.push({id:'old',profileId:profile.id,question:'我想给自己选条上班戴的手链，简单一点。',reply:'好的。'},{id:'new',profileId:profile.id,question:'先不看上班的了，周末出去玩想显眼一点。',reply:'好的。',previousTrialId:'old'}));
 const result=await f.studio.preview(f.owner,{version:1,question:'不过还是想要手链，别绕到项链去了。',previousTrialId:'new',currentNeeds:{occasion:{values:['上班']}}});assert.equal(result.localModelAttempts,2);assert.deepEqual(body.currentNeeds.occasion.values,['周末']);assert.deepEqual(body.currentNeeds.occasion.excluded,['上班']);assert.deepEqual(result.customerNeedState.fields.occasion.values,['周末']);assert.match(result.receptionSelfCheck.reasons[0],/已明确排除/);assert.equal(f.store.read().customers.length,0);assert.equal(result.rating,null);
});
test('model-suggested service direction targets one bounded rewrite without authorizing facts or customer labels',async t=>{
 const requests=[],responses=[{serviceGoal:'deliberate',reply:'放心，选得对才留，不强求。'},{reply:'喜欢这个方向不用急，先想想日常会不会经常戴。'}];const f=fixture(t,async p=>{requests.push(p);return{choices:[{message:{content:JSON.stringify(responses.shift())}}]};});f.studio.save(f.owner,input);const result=await f.studio.preview(f.owner,{version:1,question:'就是怕一时上头，回去又塞抽屉。'});
 assert.equal(result.localModelAttempts,2);assert.equal(result.receptionAction.semanticGoal.key,'deliberate');assert.equal(result.receptionAction.semanticGoal.source,'local_model_suggested_service_goal');assert.equal(JSON.parse(requests[1].messages[1].content).serviceGuidance.key,'deliberate');assert.ok(!requests[1].messages.some(m=>m.role==='assistant'));assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(f.store.read().customers.length,0);assert.equal(result.rating,null);assert.equal(result.sent,false);
});
test('a model-recognized recap gets one source-based calibration and stores only the latest requirements',async t=>{
 const responses=[{serviceGoal:'recap',reply:'你自己戴手链，上班简单点。'},{reply:'你自己戴手链，现在改成周末出去玩，想显眼一点。'}];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify(responses.shift())}}]}));const p=f.studio.save(f.owner,input);f.store.transact(s=>s.personaStudio.trials.push({id:'old-use',profileId:p.id,question:'我想给自己选条上班戴的手链。',reply:'好的。'},{id:'new-use',profileId:p.id,question:'先不看上班的了，周末出去玩想显眼一点。',reply:'好的。',previousTrialId:'old-use'}));
 const result=await f.studio.preview(f.owner,{version:1,question:'把刚刚改过的要求捋一下。',previousTrialId:'new-use'});assert.equal(result.localModelAttempts,2);assert.equal(result.receptionAction.semanticGoal.key,'recap');assert.ok(result.reply.includes('周末'));assert.ok(!result.reply.includes('上班'));assert.equal(f.store.read().personaStudio.trials.length,3);
});
test('unrecognized suggested goals and actual factual claims are rejected without extra model calls',async t=>{
 for(const output of [{serviceGoal:'send_order',reply:'可以。'},{serviceGoal:'choose',reply:'这款99元。'}]){let calls=0;const f=fixture(t,async()=>{calls++;return{choices:[{message:{content:JSON.stringify(output)}}]};});f.studio.save(f.owner,input);await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'换个方向想想怎么挑。'}));assert.equal(calls,1);assert.equal(f.store.read().personaStudio.trials.length,0);assert.equal(f.store.read().tasks.length,0);}
});
test('unfamiliar concerns can use daily-use criteria or relationship reassurance without forced product advice',async t=>{
 const replies=['先想想平时会不会经常戴，没想清也不用急。','不会嫌你烦，慢慢问就好。'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 const usage=await f.studio.preview(f.owner,{version:1,question:'就是怕一时上头，回去又塞抽屉。'});assert.equal(usage.localModelAttempts,1);assert.equal(usage.receptionAction.key,'open');const relationship=await f.studio.preview(f.owner,{version:1,question:'我怕你觉得我很难伺候。'});assert.equal(relationship.localModelAttempts,1);assert.equal(relationship.rating,null);assert.equal(f.store.read().tasks.length,0);
});
test('an unfamiliar relationship concern may receive reassurance without being forced into selection advice',async t=>{
 let payload;const replies=['可以先看线条简单的方向。','不会，你慢慢问，问清楚才安心。'];const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);const first=await f.studio.preview(f.owner,{version:1,question:'我想给自己选条上班戴的手链，平时穿衬衫。'});
 const next=await f.studio.preview(f.owner,{version:1,question:'我这么问东问西，你不会觉得我难伺候吧？',previousTrialId:first.id});assert.equal(next.receptionAction.key,'open');assert.equal(next.localModelAttempts,1);assert.deepEqual(payload.recentCustomerStatements,[first.question]);assert.ok(!JSON.stringify(payload).includes('可以先看线条简单'));assert.equal(next.rating,null);assert.equal(next.sent,false);assert.equal(f.store.read().customers.length,0);
});
test('unfamiliar recap keeps old quotes as evidence but accepts latest occasion and unchanged wearer/category',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"你自己戴手链，现在想周末出去玩时显眼一点。"}'}}]};});const profile=f.studio.save(f.owner,input);
 const questions=['我想给自己选条上班戴的手链，简单一点。','我问得有点多。','我再比较看看。','我缓一缓。','先聊到这儿。','先不看上班的了，周末出去玩想显眼一点。','不过还是想要手链，别绕到项链去了。'];f.store.transact(s=>{questions.forEach((question,i)=>s.personaStudio.trials.push({id:'quote-'+i,profileId:profile.id,question,reply:'好的。',previousTrialId:i?'quote-'+(i-1):null,sent:false,rating:null}));});
 const result=await f.studio.preview(f.owner,{version:1,question:'那你帮我把刚刚改过的要求捋一下。',previousTrialId:'quote-6'});assert.equal(payload.earlierCustomerNotes[0].question,questions[0]);assert.ok(payload.recentCustomerStatements.includes(questions[5]));assert.ok(JSON.stringify(payload).indexOf(questions[0])<JSON.stringify(payload).indexOf(questions[5]));assert.equal(result.reply,'你自己戴手链，现在想周末出去玩时显眼一点。');assert.equal(result.receptionAction.key,'open');assert.equal(result.rating,null);
});
test('service conversation rewrites an invented selected item without losing stated use',async t=>{
 const replies=['可以先看线条简单一点的方向。','这条手链和你日常衣服搭不搭？','你可以先看看这个方向和上班穿的衣服搭不搭。'];let payload;
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我想给自己选条上班戴的手链，平时穿衬衫。'});const next=await f.studio.preview(f.owner,{version:1,question:'喜欢这种感觉，又怕自己冲动。',previousTrialId:first.id});assert.equal(next.localModelAttempts,2);assert.match(next.receptionSelfCheck.reasons[0],/尚未选中/);assert.deepEqual(payload.recentCustomerStatements,[first.question]);assert.equal(f.store.read().personaStudio.trials.length,2);assert.ok(!next.reply.includes('这条'));assert.equal(next.sent,false);assert.equal(next.rating,null);
});
test('continuous service and sales accepts concern, helps compare, and respects a pause without creating owner labels',async t=>{
 const replies=['可以先看线条简单一点的方向。','选细款就好。','没事，问清楚才安心，你最拿不准哪一点？','可以先想想日常会不会经常戴，喜欢也不用急着决定。','好，那先不聊，你慢慢来。'];let body;const f=fixture(t,async p=>{body=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我想给自己选一条上班戴的手链，平时穿衬衫。'});
 const cared=await f.studio.preview(f.owner,{version:1,question:'我问这么多你会不会嫌我烦？',previousTrialId:first.id});assert.equal(cared.receptionAction.serviceGuidance.key,'reassure');assert.equal(cared.localModelAttempts,2);assert.deepEqual(body.recentCustomerStatements,[first.question]);
 const deliberated=await f.studio.preview(f.owner,{version:1,question:'我挺喜欢这种感觉，又怕自己冲动。',previousTrialId:cared.id});assert.equal(deliberated.receptionAction.serviceGuidance.key,'deliberate');
 const paused=await f.studio.preview(f.owner,{version:1,question:'今天不想聊了，想静静。',previousTrialId:deliberated.id});assert.equal(paused.receptionAction.serviceGuidance.key,'pause');assert.ok(!/[?？]/u.test(paused.reply));const state=f.store.read();assert.equal(state.personaStudio.trials.length,4);assert.ok(state.personaStudio.trials.every(x=>x.rating===null&&!x.sent));assert.deepEqual(state.personaStudio.profile.examples,input.examples);assert.equal(state.tasks.length,0);assert.equal(state.customers.length,0);
});

const input={version:0,name:'测试店主',addressing:'你',length:'简短',warmth:'熟人聊天',emoji:'不用',phrases:'不急，慢慢看',avoid:'尊敬的顾客',confirmed:true,examples:[{question:'我先看看',answer:'好呀，不急，你慢慢看。'},{question:'我想送妈妈',answer:'送妈妈呀，她平时喜欢素一点的吗？'}]};
test('unfamiliar idle concern repairs unsupported reassurance once and stores only useful final draft',async t=>{
 const requests=[],outputs=[{serviceGoal:'deliberate',reply:'上班戴最实用，别担心闲置。'},{reply:'先看能不能配平时常穿的衣服，想不到怎么搭就先放着。'}];const f=fixture(t,async p=>{requests.push(p);return{choices:[{message:{content:JSON.stringify(outputs.shift())}}]};});f.studio.save(f.owner,input);
 const result=await f.studio.preview(f.owner,{version:1,question:'我怕一时上头，买了又塞抽屉。'});assert.equal(result.localModelAttempts,2);assert.match(result.receptionSelfCheck.reasons[0],/不能替客户保证/);assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(result.rating,null);assert.equal(result.sent,false);assert.equal(f.store.read().tasks.length,0);assert.equal(f.store.read().customers.length,0);assert.ok(!requests[1].messages.some(m=>m.role==='assistant'));
});
test('failed decision-help calibration stays failed without canned fallback or extra calls',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;return{choices:[{message:{content:JSON.stringify({reply:'绝对不会后悔。'})}}]};});f.studio.save(f.owner,input);await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我喜欢这种感觉，又怕自己冲动。'}),/不能替客户保证/);assert.equal(calls,2);assert.equal(f.store.read().personaStudio.trials.length,0);assert.equal(f.store.read().tasks.length,0);
});
test('model-recognized requests to explain use focused repair rather than importing idle-purchase advice',async t=>{
 const requests=[],outputs=[{serviceGoal:'repair',reply:'先想想平时会不会常戴，没想清就先放着。'},{reply:'想显眼又别复杂，可以先看轮廓清楚、细节少的方向。'}];const f=fixture(t,async p=>{requests.push(p);return{choices:[{message:{content:JSON.stringify(outputs.shift())}}]};});f.studio.save(f.owner,input);
 const result=await f.studio.preview(f.owner,{version:1,question:'别光说有特点，你就告诉我到底看哪里。'});assert.equal(result.localModelAttempts,2);assert.equal(result.receptionAction.semanticGoal.key,'repair');assert.equal(JSON.parse(requests[1].messages[1].content).serviceGuidance.key,'repair');assert.ok(!requests[1].messages.some(m=>m.role==='assistant'));assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(result.rating,null);assert.equal(result.sent,false);
});
function fixture(t,complete=async()=>({choices:[{message:{content:JSON.stringify({reply:'好呀，不急，你先说说喜欢什么感觉。'})}}]})){
 const dir=mkdtempSync(join(tmpdir(),'studio-test-')),store=new Store(dir),engine=new Engine(store),owner=engine.setup({username:'owner',password:'owner-test-password'});
 const studio=new PersonaStudio(engine,{model:{complete}});t.after(()=>{store.close();rmSync(dir,{recursive:true,force:true});});
 return{store,engine,owner,studio,dir};
}
test('owner samples are encrypted and isolated from active sending persona and employees',t=>{
 const f=fixture(t),p=f.studio.save(f.owner,input),s=f.store.read();
 assert.equal(p.version,1);assert.equal(s.personas.length,0);assert.equal(s.settings.personaId,undefined);
 assert.equal(f.engine.snapshot(f.owner).personaStudio.profile.name,'测试店主');
 assert.equal(f.engine.snapshot({id:'employee',role:'employee'}).personaStudio,undefined);
 assert.ok(!readFileSync(join(f.dir,'avatar.sqlite')).includes(Buffer.from('不急，慢慢看')));
 assert.throws(()=>f.studio.save({role:'employee'},input),/老板/);
 assert.throws(()=>f.studio.save(f.owner,input),/已变化/);
});
test('preview requires owner confirmation and saved samples, then uses fixed local model and no send tasks',async t=>{
 let payload;const f=fixture(t,async p=>{payload=p;return{choices:[{message:{content:'{"reply":"好呀，不急，你慢慢看。"}'}}]};});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:0,question:'你好'}),/先保存/);
 assert.throws(()=>f.studio.save(f.owner,{...input,confirmed:false}),/确认/);
 f.studio.save(f.owner,input);const result=await f.studio.preview(f.owner,{version:1,question:'我先看看'});
 assert.equal(payload.model,QWEN_MODEL);assert.equal(result.sent,false);assert.equal(result.reply,'好呀，不急，你慢慢看。');
 assert.deepEqual(JSON.parse(payload.messages[1].content).styleExamplesOnly[0],input.examples[0]);
 assert.equal(f.store.read().tasks.length,0);assert.equal(f.store.read().messages.length,0);
});
test('owner correction creates a new version used by subsequent preview; old feedback cannot overwrite it',async t=>{
 const calls=[];const f=fixture(t,async p=>{calls.push(p);return{choices:[{message:{content:'{"reply":"好呀，不急。"}'}}]};});
 f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'我先看看'});
 assert.throws(()=>f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'corrected',correction:'你慢慢挑，我陪你看看。'}),/确认/);
 f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'corrected',correction:'你慢慢挑，我陪你看看。',confirmed:true});
 assert.equal(f.store.read().personaStudio.profile.version,2);
 assert.throws(()=>f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'like'}),/旧口吻/);
 await f.studio.preview(f.owner,{version:2,question:'我先看看'});
 assert.equal(JSON.parse(calls[1].messages[1].content).styleExamplesOnly[0].answer,'你慢慢挑，我陪你看看。');
 assert.equal(f.store.read().personas.length,0);
});
test('no self-assigned likeness score: only owner feedback is saved',async t=>{
 const f=fixture(t);f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'我先看看'});
 assert.equal(trial.rating,null);f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'like'});
 const result=f.store.read().personaStudio.trials[0];assert.equal(result.rating,'like');assert.equal(result.reviewedBy,f.owner.id);
 assert.equal(f.store.read().personaStudio.profile.version,1);
});
test('malformed, extra-field and risky model outputs never become previews',async t=>{
 const cases=['null','[]','{"reply":"亲","send":true}','{"reply":"最低99元，保证现货"}','{"reply":"保证明天到"}'];
 const f=fixture(t,async()=>({choices:[{message:{content:cases.shift()}}]}));f.studio.save(f.owner,input);
 for(let i=0;i<5;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你好'}));
 assert.equal(f.store.read().personaStudio.trials.length,0);assert.equal(f.studio.busy,false);
});
test('a profile changed during inference invalidates the late result',async t=>{
 let finish;const f=fixture(t,()=>new Promise(resolve=>finish=resolve));f.studio.save(f.owner,input);
 const pending=f.studio.preview(f.owner,{version:1,question:'你好'});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你好'}),/上一轮/);
 f.studio.save(f.owner,{...input,version:1,name:'新版本'});
 finish({choices:[{message:{content:'{"reply":"好呀，你慢慢看。"}'}}]});
 await assert.rejects(()=>pending,/发生变化/);assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('risky question is tagged as verification-only and cannot teach a quoted price',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"你看中的是哪款？我先核实具体情况。"}'}}]}));f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'最低多少钱？'});
 assert.equal(trial.requiresVerification,true);
 assert.throws(()=>f.studio.feedback(f.owner,{id:trial.id,version:1,rating:'corrected',correction:'给你99元',confirmed:true}),/具体价格/);
});
test('provider errors do not fall back or leave preview busy',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;throw Error('model offline');});f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你好'}),/model offline/);
 assert.equal(calls,1);assert.equal(f.studio.busy,false);assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('owner urgency samples remain intact but cannot become an unverified scarcity claim',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"别考虑了，考虑了就没了"}'}}]}));
 f.studio.save(f.owner,{...input,examples:[{question:'【演练场景】我再考虑一下',answer:'别考虑了，考虑了就没了'}]});
 assert.equal(f.store.read().personaStudio.profile.examples[0].answer,'别考虑了，考虑了就没了');
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我还没决定'}),/待核实/);
 assert.equal(f.store.read().personaStudio.trials.length,0);
 assert.equal(f.store.read().tasks.length,0);
});
test('unapproved return promises in everyday language are blocked before a preview is retained',async t=>{
 const replies=['你先试试看，不合适我退给你','不合适我给你退','不合适随时换'];
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 for(let i=0;i<3;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我怕不合适'}),/待核实/);
 assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('existing unsafe trial is quarantined, hidden from usable replies and cannot receive a like vote',t=>{
 const f=fixture(t),p=f.studio.save(f.owner,input);
 f.store.transact(s=>s.personaStudio.trials.push({id:'old-unsafe',profileId:p.id,version:p.version,question:'我怕不合适',reply:'你先试试看，不合适我退给你',baseline:'可以说说你的顾虑。',rating:null,sent:false}));
 new PersonaStudio(f.engine,{model:{complete(){throw Error('must not infer');}}});
 const data=f.store.read().personaStudio;
 assert.equal(data.trials[0].blocked,true);assert.equal(data.trials[0].reply,'你先试试看，不合适我退给你');
 const html=studioView({personaStudio:data});assert.ok(html.includes('已拦截，不可用'));assert.ok(!html.includes('你先试试看，不合适我退给你'));
 assert.throws(()=>f.studio.feedback(f.owner,{id:'old-unsafe',version:1,rating:'like'}),/不能评价/);
 assert.equal(f.store.read().tasks.length,0);
});
test('style-only studio cannot invent a catalog when no product facts were supplied',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"素手链我有几款，你喜欢什么样的？"}'}}]}));f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'想看素一点的手链'}),/待核实/);
 assert.equal(f.store.read().personaStudio.trials.length,0);assert.equal(f.store.read().tasks.length,0);
});
test('generic suitability and unseen appearance claims are not accepted as voice personalization',async t=>{
 const replies=['欢迎光临，我这儿东西都挺适合你的','你今天非常的漂亮，先看看吧'];
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 for(let i=0;i<2;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你好，我第一次来'}),/待核实/);
 assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('first visit is directed to one usage question, rather than empty welcome or several questions',async t=>{
 const replies=['欢迎光临，你先看看。','欢迎，自己戴吗？喜欢什么风格？','欢迎来看看，想自己戴还是送礼？'];const payloads=[];
 const f=fixture(t,async p=>{payloads.push(p);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 for(let i=0;i<2;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'你好，我第一次来，想先了解一下。'}),/用途澄清/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'你好，我第一次来，想先了解一下。'});
 assert.equal(trial.replyGoal,'discover_usage');assert.equal(JSON.parse(payloads[2].messages[1].content).replyGoal,'discover_usage');assert.equal(trial.rating,null);assert.equal(trial.sent,false);
});
test('a known personal-use purpose is not forced through the first-visit usage question again',async t=>{
 let payload;const f=fixture(t,async p=>{payload=p;return{choices:[{message:{content:'{"reply":"你喜欢素一点还是带点设计感的？"}'}}]};});f.studio.save(f.owner,input);
 const trial=await f.studio.preview(f.owner,{version:1,question:'第一次来，我想自己戴'});
 assert.equal(trial.replyGoal,'respond_in_style');assert.equal(JSON.parse(payload.messages[1].content).action,'open');
});
test('a customer not buying today gets an acknowledgement goal without new reservations',async t=>{
 const replies=['你再逛逛，下次来我给你留个位置','你慢慢看，喜欢的咱们再聊。'];let payload;
 const f=fixture(t,async p=>{payload=p;return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我再逛逛，今天先不买。'}),/待核实/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'我再逛逛，今天先不买。'});
 assert.equal(trial.replyGoal,'acknowledge_browsing');assert.equal(JSON.parse(payload.messages[1].content).serviceGuidance.key,'pause');assert.equal(trial.reply,'你慢慢看，喜欢的咱们再聊。');assert.equal(trial.sent,false);
});
test('actual HTTP routes keep owner auth, origin checks and encrypted trial persistence',async t=>{
 const directory=mkdtempSync(join(tmpdir(),'studio-http-'));const app=createApp({directory,worker:false,localQwen:{close(){},complete:async()=>({choices:[{message:{content:'{"reply":"好呀，你慢慢看，不急。"}'}}]})}});
 await new Promise(done=>app.server.listen(0,'127.0.0.1',done));const origin=`http://127.0.0.1:${app.server.address().port}`;let cookie='';
 t.after(async()=>{await app.close();rmSync(directory,{recursive:true,force:true});});
 async function post(path,body,extra={}){const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json',Cookie:cookie,...extra},body:JSON.stringify(body)});if(r.headers.has('set-cookie'))cookie=r.headers.get('set-cookie').split(';')[0];return{status:r.status,data:await r.json()};}
 assert.equal((await post('/api/persona-studio/save',input)).status,401);
 await post('/api/setup',{username:'owner',password:'owner-http-test-password'});
 assert.equal((await post('/api/persona-studio/save',input,{Origin:'https://other.invalid'})).status,403);
 assert.equal((await post('/api/persona-studio/save',input)).status,200);
 const trial=await post('/api/persona-studio/preview',{version:1,question:'我先看看'});assert.equal(trial.status,200);
 assert.equal((await post('/api/persona-studio/feedback',{id:trial.data.id,version:1,rating:'like'})).status,200);
 assert.equal(app.store.read().personaStudio.trials[0].rating,'like');assert.equal(app.store.read().tasks.length,0);
});
test('continuation uses stored prior dialogue while independent questions remain isolated',async t=>{
 const payloads=[];const f=fixture(t,async p=>{payloads.push(JSON.parse(p.messages[1].content));return{choices:[{message:{content:'{"reply":"偏素的话，你更喜欢细一点还是宽一点的？"}'}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我想自己戴，喜欢素一点'});
 const next=await f.studio.preview(f.owner,{version:1,question:'第一次来，先了解一下',previousTrialId:first.id});
 assert.equal(next.replyGoal,'clarify_width');assert.equal(next.contextTurns,1);assert.equal(next.previousTrialId,first.id);
 assert.deepEqual(payloads[1].recentConversation,[{question:first.question,reply:first.reply}]);
 await f.studio.preview(f.owner,{version:1,question:'我想了解手链'});
 assert.deepEqual(payloads[2].recentCustomerStatements,[]);assert.equal(f.store.read().tasks.length,0);
});
test('continuation never uses missing, quarantined or outdated profile drafts',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;return{choices:[{message:{content:'{"reply":"好呀，你慢慢看。"}'}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我先看看'});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'好',previousTrialId:'missing'}),/过期/);
 f.store.transact(s=>{s.personaStudio.trials[0].blocked=true;});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'好',previousTrialId:first.id}),/拦截/);
 f.studio.save(f.owner,{...input,version:1});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:2,question:'好',previousTrialId:first.id}),/口吻已更新/);
 assert.equal(calls,1);assert.equal(f.studio.busy,false);
});
test('continuation bounds recent context and rejects oversized context before calling model',async t=>{
 const payloads=[];const f=fixture(t,async p=>{payloads.push(JSON.parse(p.messages[1].content));return{choices:[{message:{content:'{"reply":"好呀，你慢慢看。"}'}}]};});f.studio.save(f.owner,input);
 let previous;for(let i=0;i<5;i++)previous=await f.studio.preview(f.owner,{version:1,question:'想看看手链',previousTrialId:previous?.id});
 assert.equal(previous.contextTurns,3);assert.equal(payloads.at(-1).recentCustomerStatements.length,3);
 f.store.transact(s=>{for(const trial of s.personaStudio.trials){trial.question='素'.repeat(200);trial.reply='好'.repeat(180);}});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'好',previousTrialId:previous.id}),/内容过长/);
 assert.equal(payloads.length,5);assert.equal(f.studio.busy,false);
});
test('studio makes continuing optional and shows which conversation is used',t=>{
 const f=fixture(t),p=f.studio.save(f.owner,input);
 const html=studioView({personaStudio:{profile:p,trials:[{id:'prior',profileId:p.id,version:1,question:'自己戴',reply:'喜欢什么风格？',baseline:'您好',contextTurns:1}]}});
 assert.match(html,/接着上一轮聊/);assert.match(html,/前 1 轮/);assert.match(html,/name="continueConversation" value="prior">/);
});
test('plain style already stated leads to width clarification, then acknowledgement without repeated questions',async t=>{
 const replies=['喜欢什么风格？','想要细一点还是宽一点？','细一点呀，你喜欢什么风格？','好，记下你的想法，偏素、细一点，上班戴。'];
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'自己戴，想要素一点'}),/宽细/);
 const first=await f.studio.preview(f.owner,{version:1,question:'自己戴，想要素一点'});assert.equal(first.replyGoal,'clarify_width');
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'细一点，上班戴',previousTrialId:first.id}),/重复询问/);
 const next=await f.studio.preview(f.owner,{version:1,question:'细一点，上班戴',previousTrialId:first.id});assert.equal(next.replyGoal,'acknowledge_preferences');
});
test('owner like and unlike judgements enter the next prompt, and a negative duplicate overrides a positive one',async t=>{
 const payloads=[];const replies=['好呀，你慢慢看。','好呀，你慢慢看。','好呀，你慢慢看！'];
 const f=fixture(t,async p=>{payloads.push(JSON.parse(p.messages[1].content));return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我先看看'});f.studio.feedback(f.owner,{id:first.id,version:1,rating:'like'});
 const second=await f.studio.preview(f.owner,{version:1,question:'我先看看'});
 assert.deepEqual(payloads[1].ownerFeedback,[{trialId:first.id,rating:'like',question:first.question,reply:first.reply}]);assert.deepEqual(second.feedbackUsed,[{trialId:first.id,rating:'like'}]);
 f.studio.feedback(f.owner,{id:second.id,version:1,rating:'unlike'});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我先看看'}),/不像我/);
 assert.deepEqual(payloads[2].ownerFeedback.map(x=>x.rating),['unlike']);assert.equal(f.store.read().personaStudio.trials.length,2);assert.equal(f.store.read().tasks.length,0);
});
test('unlike feedback can inform a different reply, but an updated profile never inherits old ratings',async t=>{
 const payloads=[];const replies=['好呀，你慢慢看。','慢慢挑，喜欢再聊。','不急，先看看。'];
 const f=fixture(t,async p=>{payloads.push(JSON.parse(p.messages[1].content));return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我先看看'});f.studio.feedback(f.owner,{id:first.id,version:1,rating:'unlike'});
 const second=await f.studio.preview(f.owner,{version:1,question:'我先看看'});assert.equal(second.feedbackUsed[0].rating,'unlike');assert.equal(payloads[1].ownerFeedback[0].reply,first.reply);
 f.studio.save(f.owner,{...input,version:1});const third=await f.studio.preview(f.owner,{version:2,question:'我先看看'});assert.deepEqual(third.feedbackUsed,[]);assert.deepEqual(payloads[2].ownerFeedback,[]);
});
test('a new owner vote during inference invalidates a result generated with stale feedback',async t=>{
 let finish,calls=0;const f=fixture(t,()=>{if(++calls===1)return Promise.resolve({choices:[{message:{content:'{"reply":"好呀，你慢慢看。"}'}}]});return new Promise(resolve=>finish=resolve);});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我先看看'});const pending=f.studio.preview(f.owner,{version:1,question:'我再逛逛'});
 f.studio.feedback(f.owner,{id:first.id,version:1,rating:'like'});finish({choices:[{message:{content:'{"reply":"好呀，不急。"}'}}]});
 await assert.rejects(()=>pending,/评价已变化/);assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(f.studio.busy,false);
});
test('feedback context is bounded, excludes quarantined replies and reports the exact selected sources',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"你慢慢看就好。"}'}}]};});const profile=f.studio.save(f.owner,input);
 f.store.transact(s=>{for(let i=0;i<6;i++)s.personaStudio.trials.push({id:`fixture-${i}`,profileId:profile.id,version:1,question:'看看手链',reply:`${'好'.repeat(60)}${i%2?'呀':'呢'}`,rating:i%2?'like':'unlike',blocked:i===5});});
 const result=await f.studio.preview(f.owner,{version:1,question:'我先看看手链'});
 assert.ok(Buffer.byteLength(JSON.stringify(payload.ownerFeedback),'utf8')<=1500);assert.ok(payload.ownerFeedback.filter(x=>x.rating==='like').length<=2);assert.ok(payload.ownerFeedback.filter(x=>x.rating==='unlike').length<=2);assert.ok(!payload.ownerFeedback.some(x=>x.trialId==='fixture-5'));assert.deepEqual(result.feedbackUsed,payload.ownerFeedback.map(({trialId,rating})=>({trialId,rating})));
});
test('changed preferences are given a revision goal and old preference cues no longer force acknowledgement',async t=>{
 const payloads=[];const replies=['明白，偏素、细一点。','好，那现在看有设计感、宽一点的。','可以先从你最想突出的搭配效果聊起。'];
 const f=fixture(t,async p=>{payloads.push(JSON.parse(p.messages[1].content));return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我想自己戴，选素一点、细一点的手链。'});
 const change='改主意了，不要素一点的了，想要有设计感、宽一点的。';
 const second=await f.studio.preview(f.owner,{version:1,question:change,previousTrialId:first.id});assert.equal(second.replyGoal,'update_preferences');assert.equal(payloads[1].latestPreferenceRevision,change);
 const third=await f.studio.preview(f.owner,{version:1,question:'我还想了解一下',previousTrialId:second.id});assert.equal(third.replyGoal,'respond_in_style');assert.equal(payloads[2].latestPreferenceRevision,change);
});
test('styling follow-ups and thanks do not get trapped in the preference acknowledgement goal',async t=>{
 const replies=['明白，偏素、细一点。','衬衫搭配可以让手腕附近的配饰简单些。','不用客气，想好了再聊。'];
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'我想选素一点、细一点的手链。'});
 const second=await f.studio.preview(f.owner,{version:1,question:'平时穿衬衫，怎么搭配比较好？',previousTrialId:first.id});assert.equal(second.replyGoal,'answer_styling');
 const third=await f.studio.preview(f.owner,{version:1,question:'谢谢，我晚点再来。',previousTrialId:second.id});assert.equal(third.replyGoal,'respond_in_style');assert.equal(third.sent,false);
});
test('verification questions need a verification reply and still cannot claim a price or return promise',async t=>{
 const replies=['这款99元。','你看中的是哪款？我先核实具体情况。','不合适我退给你'];
 const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'最低多少钱？'}),/事实或承诺/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'最低多少钱？'});assert.equal(trial.replyGoal,'verify_only');assert.equal(trial.requiresVerification,true);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'不合适可以退吗？'}),/待核实/);assert.equal(f.store.read().tasks.length,0);
});
test('an explicit less-pressure preference takes priority over forceful owner samples and blocks clear purchase pressure',async t=>{
 const replies=['你为什么还不买，东西这么好','别犹豫，现在就买','你再想想，不急，有顾虑就跟我说。'];let payload;
 const f=fixture(t,async p=>{payload=p;return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});
 f.studio.save(f.owner,{...input,avoid:'强行催买、催促立刻决定',examples:[{question:'我有点犹豫',answer:'你为什么还不买，东西这么好'}]});
 for(let i=0;i<2;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我再考虑一下'}),/少催买偏好/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'我再考虑一下'});assert.equal(JSON.parse(payload.messages[1].content).lessPurchasePressure,true);assert.ok(payload.messages.some(m=>m.content.includes('优先于旧样本中的强势催促')));assert.equal(trial.sent,false);assert.equal(f.store.read().personaStudio.profile.examples[0].answer,'你为什么还不买，东西这么好');assert.equal(f.store.read().tasks.length,0);
});
test('new owner suitability phrases remain source material but cannot become an ungrounded personal claim',async t=>{
 const replies=['你先考虑，真的挺适合你的','没事，但是真的很适合你','你先考虑，有疑问再跟我聊。'];let payload;
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});
 f.studio.save(f.owner,{...input,examples:[{question:'我再考虑一下',answer:'没事，你先考虑，但是真的很适合你'}]});
 for(let i=0;i<2;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我想晚点再决定，现在还没想好。'}),/待核实/);
 const result=await f.studio.preview(f.owner,{version:1,question:'我想晚点再决定，现在还没想好。'});assert.equal(result.replyGoal,'acknowledge_browsing');assert.equal(payload.replyGoal,'acknowledge_browsing');assert.equal(f.store.read().personaStudio.profile.examples[0].answer,'没事，你先考虑，但是真的很适合你');assert.equal(result.sent,false);
});
test('model style context uses safe excerpts while complete owner originals remain unchanged',async t=>{
 let payload;const originals=[{question:'我再考虑一下',answer:'你今天非常的漂亮'},{question:'我再考虑一下',answer:'别考虑了，考虑了就没了'},{question:'我再考虑一下',answer:'没事，你先考虑，但是真的很适合你'}];
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"没事，你先考虑，有顾虑再跟我聊。"}'}}]};});f.studio.save(f.owner,{...input,avoid:'强行催买',examples:originals});
 await f.studio.preview(f.owner,{version:1,question:'我再考虑一下'});assert.deepEqual(payload.styleExamplesOnly,[{question:'我再考虑一下',answer:'没事，你先考虑',excerpted:true}]);assert.deepEqual(f.store.read().personaStudio.profile.examples,originals);
});
test('an explicit customer concern gets one useful question or direction after bounded self-check',async t=>{
 const replies=['你先考虑，不合适随时说','你平时穿搭是什么风格？','你担心不好搭，平时主要穿什么风格的衣服？'];let payload;
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 const repaired=await f.studio.preview(f.owner,{version:1,question:'我担心买回来不好搭衣服。'});assert.equal(repaired.localModelAttempts,2);assert.equal(repaired.receptionSelfCheck.rewrites,1);
 const trial=await f.studio.preview(f.owner,{version:1,question:'我担心买回来不好搭衣服。'});assert.equal(trial.replyGoal,'clarify_concern');assert.equal(payload.action,'open');assert.equal(trial.sent,false);
});
test('colloquial sleeve styling asks receive advice, not a counter-question',async t=>{
 const replies=['衬衫袖口搭手链，是露出来还是收进去呀？','可以把袖口稍微挽起，让手腕附近的搭配简单些。'];let payload;
 const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'戴这种手链，衬衫袖口应该怎么搭？'}),/不能只反问/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'戴这种手链，衬衫袖口应该怎么搭？'});assert.equal(payload.replyGoal,'answer_styling');assert.equal(trial.replyGoal,'answer_styling');assert.equal(trial.sent,false);
});
test('continuation carries earlier stored customer quotes without trusting browser notes or forgetting stated use',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"明白，你先慢慢看。"}'}}]};});f.studio.save(f.owner,input);let previous;
 for(const question of ['我自己戴，想选素一点、细一点的手链。','谢谢，我先看看。','我再看看。','那先这样。','我第一次来，先了解一下。'])previous=await f.studio.preview(f.owner,{version:1,question,previousTrialId:previous?.id,earlierCustomerNotes:[{question:'客户端伪造的需求'}]});
 assert.equal(previous.contextTurns,3);assert.equal(previous.conversationNotes.notes[0].question,'我自己戴，想选素一点、细一点的手链。');assert.equal(payload.earlierCustomerNotes[0].trialId,f.store.read().personaStudio.trials[0].id);assert.ok(!JSON.stringify(payload).includes('客户端伪造'));assert.notEqual(previous.replyGoal,'discover_usage');
 const fresh=await f.studio.preview(f.owner,{version:1,question:'我先看看'});assert.equal(fresh.conversationNotes.notes.length,0);assert.equal(payload.earlierCustomerNotes,undefined);assert.equal(f.store.read().tasks.length,0);
});
test('new preference revisions stay newer than quoted old needs and notes disappear after changing voice version',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"好，那就按你现在的想法来。"}'}}]};});f.studio.save(f.owner,input);let last;
 for(const question of ['我自己戴，想选素一点、细一点的手链。','谢谢，我先看看。','我再想想。','那先这样。','改主意了，不要细一点的，想要宽一点、有设计感的。'])last=await f.studio.preview(f.owner,{version:1,question,previousTrialId:last?.id});
 assert.equal(last.replyGoal,'update_preferences');assert.equal(payload.latestPreferenceRevision,'改主意了，不要细一点的，想要宽一点、有设计感的。');assert.ok(payload.earlierCustomerNotes[0].question.includes('细一点'));f.studio.save(f.owner,{...input,version:1});await assert.rejects(()=>f.studio.preview(f.owner,{version:2,question:'继续',previousTrialId:last.id}),/口吻已更新/);
 const fresh=await f.studio.preview(f.owner,{version:2,question:'我先看看'});assert.deepEqual(fresh.conversationNotes.notes,[]);
});
test('a recall request retains earlier stated purpose and latest style revision as distinct evidence',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"自己戴，想要宽一点、有设计感的。"}'}}]};});f.studio.save(f.owner,input);let last;
 for(const question of ['我自己戴，想选素一点、细一点的手链。','我先看看。','谢谢。','我再想想。','改主意了，不要细一点的，想要宽一点、有设计感的。','我刚才说的是自己戴还是送人，想要细的还是宽的？'])last=await f.studio.preview(f.owner,{version:1,question,previousTrialId:last?.id});
 assert.equal(last.replyGoal,'recall_needs');assert.equal(payload.replyGoal,'recall_needs');assert.ok(payload.recentConversation.every(t=>t.reply===undefined));assert.deepEqual(last.feedbackUsed,[]);assert.deepEqual(last.knowledgeReferences,[]);assert.ok(payload.earlierCustomerNotes.some(n=>n.question==='我自己戴，想选素一点、细一点的手链。'));assert.equal(payload.latestPreferenceRevision,'改主意了，不要细一点的，想要宽一点、有设计感的。');assert.equal(last.sent,false);
});
test('recall and preference-update drafts reject redundant counter-questions instead of saving them',async t=>{
 const replies=['你是自己戴还是送人？','这点你还没有明确说过。','宽一点的，你喜欢什么风格？'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'我刚才说的需求是什么？'}),/不能再反问/);assert.equal(f.store.read().personaStudio.trials.length,0);
 const trial=await f.studio.preview(f.owner,{version:1,question:'我刚才说的需求是什么？'});assert.equal(trial.replyGoal,'recall_needs');assert.deepEqual(trial.feedbackUsed,[]);assert.deepEqual(trial.knowledgeReferences,[]);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'改主意了，想要宽一点的。'}),/不能再反问/);assert.equal(f.store.read().personaStudio.trials.length,1);
});
test('owner item-pointer originals stay intact while focused replies learn only a safe verbatim expression fragment',async t=>{
 let payload;const replies=['可以看看这个款式','可以看看','可以看看宽一点、有设计感的款式。'];const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});
 const item=f.studio.library.import(f.owner,{kind:'voice',title:'合成采访',source:'合成店主采访，不是真实商品',text:'客户：改主意了，想要宽一点、有设计感的。\n店主：可以看看这个款式',confirmed:true}).items[0];f.studio.library.review(f.owner,{...item,action:'approve',confirmed:true});f.studio.save(f.owner,{...input,examples:[]});
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'改主意了，想要宽一点、有设计感的。'}),/事实或承诺/);
 await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'改主意了，想要宽一点、有设计感的。'}),/片段.*需求/);
 const trial=await f.studio.preview(f.owner,{version:1,question:'改主意了，想要宽一点、有设计感的。'});assert.equal(payload.styleExamplesOnly[0].answer,'可以看看');assert.equal(payload.styleExamplesOnly[0].excerpted,true);assert.equal(trial.styleSamplesUsed[0].knowledgeId,item.id);assert.equal(trial.styleSamplesUsed[0].expression,'可以看看');assert.equal(trial.knowledgeReferences[0].id,item.id);assert.equal(f.store.read().knowledgeLibrary.items[0].answer,'可以看看这个款式');assert.equal(trial.rating,null);assert.equal(trial.sent,false);assert.equal(f.store.read().tasks.length,0);
});
test('negative pointer phrases are never silently shortened into positive owner style',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"好，那按你最新的想法看看。"}'}}]};});f.studio.save(f.owner,{...input,examples:[{question:'换个方向看看',answer:'不可以看看这个款式'}]});const trial=await f.studio.preview(f.owner,{version:1,question:'改主意了，想要宽一点的。'});assert.deepEqual(payload.styleExamplesOnly,[]);assert.deepEqual(trial.styleSamplesUsed,[]);assert.equal(f.store.read().personaStudio.profile.examples[0].answer,'不可以看看这个款式');
});
test('tradeoff advice gives a direction rather than asking the customer to repeat the same choice',async t=>{
 const replies=['宽一点怕太夸张，细一点没特点，你更在意哪方面？','可以从适中的宽度、带一点细节的设计方向看看。'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 const trial=await f.studio.preview(f.owner,{version:1,question:'宽一点怕太夸张，细一点又觉得没特点，怎么办？'});assert.equal(trial.localModelAttempts,2);assert.equal(trial.receptionSelfCheck.rewrites,1);assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(trial.replyGoal,'help_choose');assert.equal(trial.receptionAction.key,'compare');assert.equal(trial.receptionAction.source,'assistant_designed_reception_policy');assert.equal(trial.styleSamplesUsed[0].expression,input.examples[0].answer);assert.deepEqual(trial.knowledgeReferences,[]);assert.equal(trial.rating,null);assert.equal(trial.sent,false);
});
test('price clarification can acknowledge unverified price while actual amounts and quotation promises stay blocked',async t=>{
 const replies=['先核实具体情况再答复','你问的是哪款？价格我先确认一下。','这款99元。','哪款？确认后给你报价。','价格已经确认。'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 const trial=await f.studio.preview(f.owner,{version:1,question:'最低多少钱？'});assert.equal(trial.replyGoal,'verify_only');assert.equal(trial.requiresVerification,true);assert.equal(trial.receptionAction.key,'clarify_item');
 for(let i=0;i<3;i++)await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'最低多少钱？'}),/事实或承诺/);assert.equal(f.store.read().tasks.length,0);
});
test('allowed unverified price acknowledgement survives reconstruction while a forged actual quote remains blocked',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"你问的是哪款？价格我先确认一下。"}'}}]}));f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'最低多少钱？'});
 f.store.transact(s=>s.personaStudio.trials.push({...trial,id:'unsafe-copy',reply:'这款99元。'}));const restored=new PersonaStudio(f.engine,{library:f.studio.library,model:{complete:async()=>{throw Error('no call');}}});assert.ok(restored);const records=f.store.read().personaStudio.trials;assert.ok(!records.find(t=>t.id===trial.id).blocked);assert.equal(records.find(t=>t.id==='unsafe-copy').blocked,true);
});
test('reception self-check rewrites at most once, sums complete timings, and stores only the valid result',async t=>{
 let calls=0;const replies=['妈妈喜欢什么风格？喜欢什么颜色？','妈妈平时喜欢什么风格？'];const f=fixture(t,async()=>({performance:{modelLoadMs:1,contextReadMs:2,generationMs:3,modelTotalMs:6,wallMs:7,shortCacheRequested:true},choices:[{message:{content:JSON.stringify({reply:replies[calls++]})}}]}));f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'想送妈妈，但不知道她喜不喜欢，该怎么选？'});assert.equal(calls,2);assert.equal(trial.localModelAttempts,2);assert.equal(trial.receptionSelfCheck.rewrites,1);assert.equal(trial.timing.model.generationMs,6);assert.equal(trial.timing.model.modelTotalMs,12);assert.equal(f.store.read().personaStudio.trials.length,1);assert.equal(trial.rating,null);
});
test('self-check failure stays bounded and does not retry factual claims or store rejected drafts',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;return{choices:[{message:{content:'{"reply":"妈妈喜欢什么风格？喜欢什么颜色？"}'}}]};});f.studio.save(f.owner,input);await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'想送妈妈，但不知道她喜不喜欢，该怎么选？'}),/多重追问/);assert.equal(calls,2);assert.equal(f.store.read().personaStudio.trials.length,0);assert.equal(f.studio.busy,false);
 calls=0;const g=fixture(t,async()=>{calls++;return{choices:[{message:{content:'{"reply":"这款99元。"}'}}]};});g.studio.save(g.owner,input);await assert.rejects(()=>g.studio.preview(g.owner,{version:1,question:'最低多少钱？'}),/事实或承诺/);assert.equal(calls,1);assert.equal(g.store.read().personaStudio.trials.length,0);
});
test('a changed profile prevents a second model call using old context during self-check',async t=>{
 let calls=0;const f=fixture(t,async()=>{calls++;f.studio.save(f.owner,{...input,version:1,name:'新口吻'});return{choices:[{message:{content:'{"reply":"妈妈喜欢什么风格？喜欢什么颜色？"}'}}]};});f.studio.save(f.owner,input);await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'想送妈妈，但不知道她喜不喜欢，该怎么选？'}),/自检改写前资料/);assert.equal(calls,1);assert.equal(f.store.read().personaStudio.trials.length,0);
});
test('an explicit tradeoff can receive direction on a first visit without being forced into an unrelated use question',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"可以先从适中的宽度、精致细节的方向看看。"}'}}]}));f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'我第一次来，宽一点怕太夸张，细一点又没特点，怎么办？'});assert.equal(trial.replyGoal,'help_choose');assert.equal(trial.localModelAttempts,1);assert.equal(trial.sent,false);
});
test('known recipient style can receive direction even when the current question includes a concern',async t=>{
 const replies=['可以先看线条简单、细节不太满的方向。','可以沿简约日常、细节精致的方向看看。'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);const first=await f.studio.preview(f.owner,{version:1,question:'是送妈妈的，她平时喜欢简约素一点，日常戴。'});const next=await f.studio.preview(f.owner,{version:1,question:'想送妈妈，怕她不喜欢，怎么办？',previousTrialId:first.id});assert.equal(next.replyGoal,'recipient_guidance');assert.equal(next.receptionAction.knownInformation.recipientStyleStatement,first.question);assert.equal(next.localModelAttempts,1);
});
test('reception advice uses explicit owner evaluations and exact owner corrections without auto-creating owner samples',async t=>{
 let payload;const replies=['可以先从适中的宽度和细节设计看起。','从线条简洁、局部有设计的方向看看。','可以先看适中的宽度，重点挑细节。'];const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]};});f.studio.save(f.owner,input);const question='宽一点怕太夸张，细一点又觉得没特点，怎么办？';const first=await f.studio.preview(f.owner,{version:1,question});f.studio.feedback(f.owner,{id:first.id,version:1,rating:'unlike'});
 const second=await f.studio.preview(f.owner,{version:1,question});assert.equal(payload.ownerFeedback[0].trialId,first.id);assert.equal(payload.ownerFeedback[0].rating,'unlike');assert.deepEqual(second.feedbackUsed,[{trialId:first.id,rating:'unlike'}]);assert.equal(second.rating,null);const correction='可以先看适中的宽度，重点挑细节。';f.studio.feedback(f.owner,{id:second.id,version:1,rating:'corrected',correction,confirmed:true});
 const third=await f.studio.preview(f.owner,{version:2,question});assert.equal(payload.ownerStyleExamples[0].answer,correction);assert.equal(payload.ownerFeedback,undefined);assert.equal(third.styleSamplesUsed[0].expression,correction);assert.equal(third.styleSamplesUsed[0].source,'owner_profile');assert.equal(third.rating,null);assert.equal(f.store.read().knowledgeLibrary.items.length,0);
});
test('an unseen concern can receive a practical direction without being forced to ask another question',async t=>{
 const f=fixture(t,async()=>({choices:[{message:{content:'{"reply":"先看轮廓轻巧一点的，别一味靠体积做存在感。"}'}}]}));f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'我手腕偏细，怕戴起来显得沉重。'});assert.equal(trial.receptionAction.key,'open');assert.equal(trial.localModelAttempts,1);assert.ok(!/[?？]/u.test(trial.reply));assert.equal(trial.rating,null);assert.equal(trial.sent,false);
});
test('ordinary reception borrows safe owner rhythm without importing sample scenarios as customer facts',async t=>{
 let payload;const f=fixture(t,async p=>{payload=JSON.parse(p.messages[1].content);return{choices:[{message:{content:'{"reply":"可以先看线条利落、细节少一点的方向。"}'}}]};});
 f.studio.save(f.owner,{...input,avoid:'少催买',examples:[{question:'我犹豫',answer:'你可以先试试'},{question:'我再想想',answer:'没事，你先考虑，但是真的很适合你'},{question:'催买',answer:'别考虑了，考虑了就没了'}]});
 const trial=await f.studio.preview(f.owner,{version:1,question:'想戴得有点存在感，又不想太招摇，往哪个方向挑？'});
 assert.equal(payload.action,'open');assert.ok(payload.ownerVoiceReferences.some(x=>x.expression==='你可以先试试'));assert.ok(payload.ownerVoiceReferences.every(x=>!x.question&&!/真的很适合|考虑了就没了/.test(x.expression)));assert.equal(payload.recentCustomerStatements.length,0);assert.ok(trial.styleSamplesUsed.length>0);assert.equal(trial.rating,null);assert.equal(f.store.read().personaStudio.profile.examples.length,3);assert.equal(f.store.read().tasks.length,0);
});
test('unrequested product combinations are rewritten once while customer-supplied details remain usable',async t=>{
 const replies=['选金属链，配个吊坠。','可以先看线条利落、带一点小细节的方向。','可以先看吊坠轮廓简单、细节少一点的方向。'];const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:replies.shift()})}}]}));f.studio.save(f.owner,input);
 const first=await f.studio.preview(f.owner,{version:1,question:'想戴得有点存在感，又不想太招摇，往哪个方向挑？'});assert.equal(first.localModelAttempts,2);assert.match(first.receptionSelfCheck.reasons[0],/没有具体商品资料/);assert.equal(f.store.read().personaStudio.trials.length,1);
 const second=await f.studio.preview(f.owner,{version:1,question:'吊坠我想看简单点的，有什么方向？'});assert.equal(second.localModelAttempts,1);assert.ok(second.reply.includes('吊坠'));
});

test('idle duration is understood locally as a use concern with the full original quote retained',async t=>{let request;const f=fixture(t,async p=>{request=p;return{choices:[{message:{content:JSON.stringify({serviceGoal:'deliberate',reply:'先想想能不能搭平时常穿的衣服，暂时想不到就先放着。'})}}]};});f.studio.save(f.owner,input);const question='当下觉得好看，过几天估计就懒得戴了。',trial=await f.studio.preview(f.owner,{version:1,question});assert.equal(trial.question,question);assert.equal(trial.requiresVerification,false);assert.equal(trial.receptionAction.semanticGoal.key,'deliberate');assert.equal(JSON.parse(request.messages[1].content).customerQuestion,question);assert.equal(trial.sent,false);assert.equal(trial.rating,null);});

test('unverified after-sales uses a short focused request without mixing selection examples or claiming a handoff',async t=>{let request;const f=fixture(t,async p=>{request=p;return{choices:[{message:{content:JSON.stringify({reply:'这块我先核实清楚，再答复你。'})}}]};});f.studio.save(f.owner,input);const trial=await f.studio.preview(f.owner,{version:1,question:'我怕过几天就不戴了，不喜欢能退货吗？'});assert.equal(trial.requiresVerification,true);assert.equal(trial.receptionAction.key,'aftercare');assert.deepEqual(trial.styleSamplesUsed,[]);assert.deepEqual(trial.feedbackUsed,[]);assert.deepEqual(trial.knowledgeReferences,[]);assert.equal(request.messages.length,2);assert.ok(!JSON.stringify(request).includes(input.examples[0].answer));});
test('a plain return question is verification-only, and a mixed unsupported promise is blocked',async t=>{const f=fixture(t,async()=>({choices:[{message:{content:JSON.stringify({reply:'我先核实，应该可以退。'})}}]}));f.studio.save(f.owner,input);await assert.rejects(()=>f.studio.preview(f.owner,{version:1,question:'不喜欢能退吗？'}),/已拦住/);assert.equal(f.store.read().personaStudio.trials.length,0);});
