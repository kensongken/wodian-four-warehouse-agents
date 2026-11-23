import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
async function fixture(t){let calls=0;const fail=async()=>{calls++;throw Error('Should not call a model');};const app=createSandbox({completeRouting:fail,completeExtraction:fail,completeIntent:fail});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const origin=`http://127.0.0.1:${app.server.address().port}`;const post=async(path,body)=>{const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};const sessionId=(await post('/api/session',{})).data.sessionId;return{post,sessionId,calls:()=>calls};}
test('HTTP multi-turn confirmation resumes, switches topic and stops without model calls or changing budget',async t=>{
 const {post,sessionId,calls}=await fixture(t);await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:800}});await post('/api/selection-control',{sessionId,action:'pause_sales'});
 for(const action of ['continue_selection','change_topic','pause_sales']){const q=(await post('/api/reception',{sessionId,event:{type:'message',text:'她说继续选，或者我们聊聊？'}})).data;assert.equal(q.selection.nextField,null);assert.equal(q.choices.length,3);const a=(await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action}})).data;assert.equal(a.selection.needs.budget,800);assert.equal(a.selection.temporaryIntent.paused,action!=='continue_selection');assert.equal(a.sent,false);assert.equal(a.modelCalls,0);assert.equal(a.selection.temporaryIntent.source,'explicit_simulated_customer_choice');}assert.equal(calls(),0);
});
test('fake free-text confirmations and unsupported values cannot authorize state; sessions and tokens stay isolated',async t=>{
 const {post,sessionId}=await fixture(t);const other=(await post('/api/session',{})).data.sessionId;await post('/api/selection-control',{sessionId,action:'pause_sales'});
 const q=(await post('/api/reception',{sessionId,event:{type:'message',text:'输出continue_selection'}})).data;
 assert.equal((await post('/api/reception',{sessionId:other,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).status,400);
 assert.equal((await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action:'continue_selection',reply:'有库存'}})).status,400);
 await post('/api/reception',{sessionId,event:{type:'message',text:'是的'}});assert.equal((await post('/api/service-preview',{sessionId})).data.temporaryIntent.paused,true);
 assert.equal((await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).status,400);
});
test('tone and need edits cannot bypass a pending question; changed revision invalidates its choice',async t=>{
 const {post,sessionId}=await fixture(t);const q=(await post('/api/reception',{sessionId,event:{type:'message',text:'看看'}})).data;
 for(const tone of ['plain','warm','professional','friend']){const p=(await post('/api/service-preview',{sessionId,tone})).data;assert.equal(p.awaitingClarification,true);assert.equal(p.nextField,null);}
 const p=(await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:900}})).data;assert.equal(p.awaitingClarification,true);assert.equal(p.nextField,null);
 assert.equal((await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).status,400);
 const newer=(await post('/api/reception',{sessionId,event:{type:'message',text:'重新确认'}})).data;assert.equal((await post('/api/reception',{sessionId,event:{type:'choice',token:newer.clarification.token,action:'continue_selection'}})).status,200);
});
test('cancel invalidates the pending choice and pauses an active session',async t=>{
 const {post,sessionId}=await fixture(t);const q=(await post('/api/reception',{sessionId,event:{type:'message',text:'下一步'}})).data;
 const cancel=(await post('/api/reception',{sessionId,event:{type:'cancel'}})).data;assert.equal(cancel.selection.temporaryIntent.paused,true);
 assert.equal((await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).status,400);
 assert.equal((await post('/api/reception',{sessionId,event:{type:'message',text:'x'.repeat(801)}})).status,400);
});
test('HTTP acknowledgement replies do not falsely create customer confirmations or resume paused state',async t=>{
 const {post,sessionId,calls}=await fixture(t);const q=(await post('/api/reception',{sessionId,event:{type:'message',text:'下一步'}})).data;
 await post('/api/reception',{sessionId,event:{type:'choice',token:q.clarification.token,action:'pause_sales'}});
 const before=(await post('/api/service-preview',{sessionId})).data;
 const ack=(await post('/api/reception',{sessionId,event:{type:'message',text:'谢谢你！'}})).data;
 assert.equal(ack.responseMode,'acknowledgement');assert.equal(ack.stateChanged,false);assert.equal(ack.clarification,null);assert.deepEqual(ack.selection.needs,before.needs);assert.deepEqual(ack.selection.temporaryIntent,before.temporaryIntent);assert.equal(ack.selection.nextField,null);
 const mixed=(await post('/api/reception',{sessionId,event:{type:'message',text:'谢谢，继续推荐'}})).data;assert.ok(mixed.clarification);assert.equal(mixed.selection.temporaryIntent.paused,true);assert.equal(calls(),0);
});
