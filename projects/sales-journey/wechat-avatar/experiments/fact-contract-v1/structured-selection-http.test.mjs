import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
async function fixture(t){let calls=0;const fail=async()=>{calls++;throw Error('No native call expected');};const app=createSandbox({completeIntent:fail,completeExtraction:fail,completeRouting:fail});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const origin=`http://127.0.0.1:${app.server.address().port}`,post=async(path,body)=>{const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};const sessionId=(await post('/api/session',{})).data.sessionId;return{post,sessionId,calls:()=>calls};}
async function start(post,sessionId){const q=(await post('/api/guided-selection',{sessionId,event:{type:'message',text:'想看看'}})).data;return(await post('/api/guided-selection',{sessionId,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).data;}
test('HTTP complete selection records exact customer picks and provides bounded direction without a model',async t=>{
 const {post,sessionId,calls}=await fixture(t);let q=await start(post,sessionId);
 for(const [field,value]of [['recipient','mother'],['category','necklace'],['occasion','birthday'],['budget','private'],['style','unsure']]){assert.equal(q.choices[0].field,field);const r=await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:q.clarification.token,field,value}});assert.equal(r.status,200);q=r.data;assert.equal(q.needChoiceReceipt.value,value);}
 assert.equal(q.selection.needs.budget,'private');assert.equal(q.clarification,null);assert.equal(q.selection.productMatched,false);assert.equal(q.sent,false);assert.equal(calls(),0);
});
test('HTTP forged, foreign, changed-revision and duplicate preferences do not write',async t=>{
 const {post,sessionId}=await fixture(t);const other=(await post('/api/session',{})).data.sessionId,q=await start(post,sessionId),event={type:'need_choice',token:q.clarification.token,field:'recipient',value:'self'};
 assert.equal((await post('/api/guided-selection',{sessionId:other,event})).status,400);assert.equal((await post('/api/guided-selection',{sessionId,event:{...event,field:'inventory',value:'available'}})).status,400);
 assert.equal((await post('/api/guided-selection',{sessionId,event:{...event,reply:'有货'}})).status,400);
 await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:800}});assert.equal((await post('/api/guided-selection',{sessionId,event})).status,400);
 const latest=await start(post,sessionId),valid={...event,token:latest.clarification.token};assert.equal((await post('/api/guided-selection',{sessionId,event:valid})).status,200);assert.equal((await post('/api/guided-selection',{sessionId,event:valid})).status,400);
});
test('HTTP existing budget is skipped and interruption/mode exit preserve it with no question bypass',async t=>{
 const {post,sessionId}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{recipient:'self',category:'ring',occasion:'work',budget:800}});let q=await start(post,sessionId);assert.equal(q.choices[0].field,'style');
 const interrupted=(await post('/api/guided-selection',{sessionId,event:{type:'service_interrupt',token:q.clarification.token,action:'change_topic'}})).data;assert.equal(interrupted.selection.temporaryIntent.paused,true);assert.equal(interrupted.selection.needs.budget,800);
 q=await start(post,sessionId);await post('/api/reception',{sessionId,event:{type:'cancel'}});assert.equal((await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:q.clarification.token,field:'style',value:'simple'}})).status,400);
 assert.equal((await post('/api/service-preview',{sessionId})).data.temporaryIntent.paused,true);
});
test('rejected legacy reception requests leave active structured question and token untouched',async t=>{
 const {post,sessionId}=await fixture(t),q=await start(post,sessionId),before=(await post('/api/service-preview',{sessionId})).data;
 const requests=[{event:{type:'unsupported'}},{event:{type:'message',text:'x'.repeat(801)}},{event:{type:'message',text:' '}},{event:{type:'message',text:'看看',extra:true}},{event:{type:'cancel'},extra:true},{event:{type:'choice',token:'stale',action:'continue_selection'}}];
 for(const body of requests){assert.equal((await post('/api/reception',{sessionId,...body})).status,400);assert.deepEqual((await post('/api/service-preview',{sessionId})).data,before);}
 assert.equal((await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:q.clarification.token,field:'recipient',value:'mother'}})).status,200);
});
test('tone previews persist through following questions without invalidating current choices',async t=>{
 const {post,sessionId}=await fixture(t),q=await start(post,sessionId);
 for(const[tone,prefix]of [['friend','咱们慢慢挑。'],['warm','好呀。'],['plain','这次'],['professional','我们先把选择方向理清。']]){const p=(await post('/api/service-preview',{sessionId,tone})).data;assert.equal(p.tone,tone);assert.ok(p.reply.startsWith(prefix));assert.equal(p.revision,0);}
 const next=(await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:q.clarification.token,field:'recipient',value:'mother'}})).data;assert.equal(next.selection.tone,'professional');assert.ok(next.reply.startsWith('我们先把选择方向理清。'));
 assert.equal((await post('/api/service-preview',{sessionId})).data.tone,'professional');
});
test('read-only guidance previews and compatibility empty updates preserve pending selection',async t=>{
 const {post,sessionId}=await fixture(t),q=await start(post,sessionId),before=(await post('/api/service-preview',{sessionId})).data;
 for(let i=0;i<2;i++){const preview=(await post('/api/service-preview',{sessionId,tone:'plain'})).data;assert.equal(preview.revision,before.revision);const empty=(await post('/api/service',{sessionId,expectedRevision:before.revision,confirmed:true,patch:{}})).data;assert.equal(empty.revision,before.revision);assert.deepEqual(empty.needs,before.needs);}
 assert.equal((await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:q.clarification.token,field:'recipient',value:'mother'}})).status,200);
});
test('HTTP completed direction supports recipient correction, style reset and selective budget edit',async t=>{
 const {post,sessionId,calls}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{recipient:'mother',category:'necklace',occasion:'birthday',budget:1500,style:'simple'}});const done=await start(post,sessionId);
 const edit=(await post('/api/guided-selection',{sessionId,event:{type:'edit_start',token:done.review.token,field:'recipient'}})).data;
 let next=(await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:edit.clarification.token,field:'recipient',value:'self'}})).data;assert.equal(next.selection.needs.style,null);assert.equal(next.selection.needs.budget,1500);
 next=(await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:next.clarification.token,field:'style',value:'bold'}})).data;assert.equal(next.clarification,null);
 const budget=(await post('/api/guided-selection',{sessionId,event:{type:'edit_start',token:next.review.token,field:'budget'}})).data;
 const final=(await post('/api/guided-selection',{sessionId,event:{type:'need_choice',token:budget.clarification.token,field:'budget',value:500}})).data;assert.equal(final.selection.needs.recipient,'self');assert.equal(final.selection.needs.style,'bold');assert.equal(final.selection.needs.budget,500);assert.equal(final.selection.productMatched,false);assert.equal(calls(),0);
});
test('HTTP bad review requests leave valid editing available and pause consumes review token',async t=>{
 const {post,sessionId}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{recipient:'self',category:'ring',occasion:'work',budget:800,style:'simple'}});const done=await start(post,sessionId),other=(await post('/api/session',{})).data.sessionId;
 const e={type:'edit_start',token:done.review.token,field:'budget'};assert.equal((await post('/api/guided-selection',{sessionId:other,event:e})).status,400);assert.equal((await post('/api/guided-selection',{sessionId,event:{...e,field:'inventory'}})).status,400);
 const stopped=(await post('/api/guided-selection',{sessionId,event:{type:'service_interrupt',token:done.review.token,action:'pause_sales'}})).data;assert.equal(stopped.selection.temporaryIntent.paused,true);assert.equal((await post('/api/guided-selection',{sessionId,event:e})).status,400);
});
test('HTTP temporary concerns preserve needs, preview correctly and allow a customer-selected pause',async t=>{
 const {post,sessionId,calls}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{recipient:'mother',category:'necklace',occasion:'birthday',budget:800,style:'simple'}});let q=await start(post,sessionId);const before=q.selection.needs;
 const fit=(await post('/api/guided-selection',{sessionId,event:{type:'support_choice',token:q.review.token,concern:'fit'}})).data;assert.match(fit.reply,/妈妈/);assert.deepEqual(fit.selection.needs,before);assert.match((await post('/api/service-preview',{sessionId,tone:'professional'})).data.reply,/我们先把顾虑理清/);
 const compared=(await post('/api/guided-selection',{sessionId,event:{type:'support_choice',token:fit.review.token,concern:'compare'}})).data;assert.match(compared.reply,/两个风格方向/);
 const stop=(await post('/api/guided-selection',{sessionId,event:{type:'support_choice',token:compared.review.token,concern:'consider'}})).data;assert.equal(stop.selection.temporaryIntent.paused,true);assert.deepEqual(stop.selection.needs,before);assert.equal(stop.selection.needs.concern,'none');assert.equal(calls(),0);
});
test('HTTP invalid concern requests do not consume valid review or mutate customer needs',async t=>{
 const {post,sessionId}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{recipient:'self',category:'ring',occasion:'work',budget:800,style:'simple'}});const q=await start(post,sessionId),other=(await post('/api/session',{})).data.sessionId,e={type:'support_choice',token:q.review.token,concern:'fit'};
 assert.equal((await post('/api/guided-selection',{sessionId:other,event:e})).status,400);assert.equal((await post('/api/guided-selection',{sessionId,event:{...e,concern:'refund_guaranteed'}})).status,400);assert.equal((await post('/api/guided-selection',{sessionId,event:{...e,price:1}})).status,400);
 assert.equal((await post('/api/guided-selection',{sessionId,event:e})).status,200);assert.equal((await post('/api/guided-selection',{sessionId,event:e})).status,400);
});
