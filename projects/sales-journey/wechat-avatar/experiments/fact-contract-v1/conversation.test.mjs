import test from 'node:test';import assert from 'node:assert/strict';
import {EvidenceLedger} from './contract.mjs';import {receive} from './conversation.mjs';
const response=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
const binding={storeId:'s',customerId:'c',subjects:{order:'o',inventory:'p',delivery_today:'p',owner_presence:'boss'}};
const setup=route=>({message:'测试客户问题',model:'test',complete:async()=>response(route),ledger:new EvidenceLedger({clock:()=>150}),binding,lookupEvidence:async()=>({state:'completed',evidenceId:'fake',evidenceVersion:1})});
test('multiple business intents each remain unknown without valid evidence',async()=>{
 const r=await receive(setup({topics:['inventory','delivery_today','owner_presence','order'],socialAct:null}));
 assert.equal(r.parts.length,4);assert.ok(r.parts.every(x=>x.state==='unknown'));assert.equal(r.sent,false);
});
test('model reply, action or resource injection is rejected before trusted lookup',async()=>{
 for(const extra of [{reply:'订单已完成'},{action:'notify_owner'},{storeId:'other'},{state:'completed'}]) {
  let calls=0;const r=await receive({...setup({topics:['order'],socialAct:null,...extra}),lookupEvidence:async()=>{calls++;}});
  assert.equal(calls,0);assert.equal(r.source,'safe_route_fallback');assert.ok(!r.reply.includes('已完成'));
 }
});
test('wrong general classification cannot bypass ledger with arbitrary text',async()=>{
 const r=await receive({...setup({topics:[],socialAct:'greeting'}),message:'订单是否处理完？'});
 assert.equal(r.reply,'你好，想聊点什么？');assert.equal(r.source,'bounded_social_response');
 // Safe but unhelpful: this test is NOT evidence that the intent was correct.
});
test('network/JSON/schema failures produce explicit safe fallback',async()=>{
 for(const complete of [async()=>{throw Error('offline');},async()=>({choices:[{message:{content:'bad-json'}}]}),async()=>response({topics:['unknown'],socialAct:null})]) {
  const r=await receive({...setup(null),complete});assert.equal(r.source,'safe_route_fallback');
 }
});
test('only application-bound resource and matching trusted record can assert a state',async()=>{
 const options=setup({topics:['inventory'],socialAct:null});
 options.ledger.ingest({kind:'inventory',storeId:'s',customerId:'c',subjectId:'p',id:'e',version:1,state:'available',source:'inventory_snapshot',issuedAt:100,expiresAt:200,simulation:true});
 const r=await receive({...options,lookupEvidence:async c=>{assert.equal(c.subjectId,'p');return{state:'available',evidenceId:'e',evidenceVersion:1};}});
 assert.equal(r.parts[0].state,'available');assert.match(r.reply,/仅演练/);
});
test('missing resource or failing lookup cannot invent subject or state',async()=>{
 const a=await receive({...setup({topics:['order'],socialAct:null}),binding:{...binding,subjects:{}}});
 assert.equal(a.parts[0].state,'unknown');
 const b=await receive({...setup({topics:['order'],socialAct:null}),lookupEvidence:async()=>{throw Error('down');}});
 assert.equal(b.parts[0].state,'unknown');
});
