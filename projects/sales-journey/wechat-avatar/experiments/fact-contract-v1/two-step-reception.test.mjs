import test from 'node:test';import assert from 'node:assert/strict';import {TwoStepReception} from './two-step-reception.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('generator receives both sides of prior conversation, current evidence and no verified merchant claim',async()=>{
 let message='我想选耳钉',requests=[];const c=new TwoStepReception({model:'test',completeFocus:async()=>raw({kind:'request',quote:message}),completeRoute:async()=>raw({topic:'selection'}),completeReply:async r=>{requests.push(r);return raw({reply:requests.length===1?'你平时喜欢简洁还是有细节的？':'那先按简洁的方向比较。'});}});
 await c.turn({message});message='简洁一点';const r=await c.turn({message});const msgs=requests[1].messages;assert.ok(msgs.some(m=>m.role==='assistant'&&m.content==='你平时喜欢简洁还是有细节的？'));assert.ok(msgs.some(m=>m.role==='user'&&m.content==='我想选耳钉'));assert.equal(r.sent,false);assert.equal(r.ownerVoiceVerified,false);
});
test('invalid evidence stops generation and failed turns never become conversation memory',async()=>{
 let n=0;const c=new TwoStepReception({model:'test',completeFocus:async()=>raw({kind:'request',quote:'历史话'}),completeRoute:async()=>{throw Error('must not route');},completeReply:async()=>{n++;return raw({reply:'must not run'});}});await assert.rejects(()=>c.turn({message:'当前话'}));assert.equal(n,0);
});
test('unknown store facts use explicit unknown reply without model or invented appointment',async()=>{
 const fail=async()=>{throw Error('must not infer facts');},c=new TwoStepReception({model:'test',completeFocus:fail,completeRoute:fail,completeReply:fail});const r=await c.turn({message:'你们门店地址在哪里？'});assert.equal(r.modelCalls,0);assert.match(r.reply,/没有核准资料/);assert.equal(r.businessFactsVerified,false);
});
