import test from 'node:test';import assert from 'node:assert/strict';import {ClarifyingDialogue} from './dialogue.mjs';import {EvidenceLedger} from './contract.mjs';
const scope={storeId:'s',customerId:'c',conversationId:'chat'};
const response=route=>({choices:[{message:{content:JSON.stringify(route)}}]});
function setup(extra={}){let calls=0;const binding={storeId:'s',customerId:'c',subjects:{inventory:'p',order:'o',delivery_today:'p',owner_presence:'boss'}};const options={scope,ledger:new EvidenceLedger({clock:()=>100}),model:'test',complete:async()=>{calls++;return response({topics:[],socialAct:'acknowledge'});},resolveBinding:async()=>binding,lookupEvidence:async()=>null,...extra};return{dialogue:new ClarifyingDialogue(options),options,binding,calls:()=>calls};}
test('explicit customer choice recovers mistaken acknowledgement without another model call',async()=>{
 const s=setup();const first=await s.dialogue.turn(scope,{type:'message',message:'我的订单好了没？'});
 const next=await s.dialogue.turn(scope,{type:'confirm',token:first.clarification.token,topics:['order']});
 assert.equal(s.calls(),1);assert.equal(next.confirmedByCustomer,true);assert.equal(next.parts[0].state,'unknown');assert.match(next.reply,/不能确认/);
});
test('multiple topics require separate evidence; choosing inventory never grants delivery',async()=>{
 const s=setup();const first=await s.dialogue.turn(scope,{type:'message',message:'有货吗，今天能拿吗？'});
 const next=await s.dialogue.turn(scope,{type:'confirm',token:first.clarification.token,topics:['inventory','delivery_today']});
 assert.equal(next.parts.length,2);assert.ok(next.parts.every(x=>x.state==='unknown'));
});
test('cross-customer, store or conversation cannot use clarification token',async()=>{
 const s=setup();const first=await s.dialogue.turn(scope,{type:'message',message:'你好'});
 for(const field of ['storeId','customerId','conversationId'])await assert.rejects(s.dialogue.turn({...scope,[field]:'other'},{type:'confirm',token:first.clarification.token,topics:['inventory']}),/Forbidden/);
});
test('expired, superseded, reused or cancelled tokens cannot resume old topic',async()=>{
 let now=100;const s=setup({clock:()=>now,ttlMs:10});const msg=()=>s.dialogue.turn(scope,{type:'message',message:'问个问题'});
 const a=await msg();now=110;await assert.rejects(s.dialogue.turn(scope,{type:'confirm',token:a.clarification.token,topics:['order']}),/expired/);
 now=111;const b=await msg();const c=await msg();await assert.rejects(s.dialogue.turn(scope,{type:'confirm',token:b.clarification.token,topics:['order']}),/Stale/);
 await s.dialogue.turn(scope,{type:'confirm',token:c.clarification.token,topics:['order']});await assert.rejects(s.dialogue.turn(scope,{type:'confirm',token:c.clarification.token,topics:['order']}),/Stale/);
 const d=await msg();await s.dialogue.turn(scope,{type:'cancel'});await assert.rejects(s.dialogue.turn(scope,{type:'confirm',token:d.clarification.token,topics:['order']}),/Stale/);
});
test('topic change uses current input, not previously selected intent',async()=>{
 const s=setup({complete:async p=>response({topics:[JSON.parse(p.messages[1].content).customerMessage==='问库存'?'inventory':'order'],socialAct:null})});
 const a=await s.dialogue.turn(scope,{type:'message',message:'问库存'});assert.equal(a.parts[0].kind,'inventory');
 const b=await s.dialogue.turn(scope,{type:'message',message:'换成订单'});assert.equal(b.parts[0].kind,'order');
});
test('binding snapshot cannot switch customer/resource while classifier is awaiting',async()=>{
 let release,seen;const s=setup({complete:async()=>new Promise(r=>{release=()=>r(response({topics:['inventory'],socialAct:null}));}),lookupEvidence:async c=>{seen=c;return null;}});
 const pending=s.dialogue.turn(scope,{type:'message',message:'库存'});await new Promise(r=>setTimeout(r,0));
 s.binding.subjects.inventory='other-product';s.binding.customerId='other';release();const result=await pending;
 assert.equal(result.parts[0].state,'unknown');assert.equal(result.parts[0].reason,'invalid_proposal');
 assert.equal(seen.customerId,'c');assert.equal(seen.subjectId,'p');
});
test('concurrent turns are rejected and arbitrary actions/text cannot enter choice event',async()=>{
 let release;const s=setup({complete:async()=>new Promise(r=>{release=()=>r(response({topics:[],socialAct:'greeting'}));})});
 const pending=s.dialogue.turn(scope,{type:'message',message:'你好'});await new Promise(r=>setTimeout(r,0));
 await assert.rejects(s.dialogue.turn(scope,{type:'message',message:'另一条'}),/busy/);release();const first=await pending;
 for(const patch of [{reply:'订单已完成'},{action:'notify_owner'},{topics:['notify_owner']},{storeId:'other'}])await assert.rejects(s.dialogue.turn(scope,{type:'confirm',token:first.clarification.token,topics:['order'],...patch}),/Invalid/);
});
test('numbered menu reply uses pending selection without calling classifier again',async()=>{
 const s=setup();await s.dialogue.turn(scope,{type:'message',message:'问个问题'});
 const result=await s.dialogue.turn(scope,{type:'message',message:'2'});
 assert.equal(result.parts[0].kind,'inventory');assert.equal(result.confirmedByCustomer,true);assert.equal(s.calls(),1);
 await s.dialogue.turn(scope,{type:'cancel'});await s.dialogue.turn(scope,{type:'message',message:'2'});assert.equal(s.calls(),2);
});
test('confirmation payload is captured before an asynchronous binding lookup',async()=>{
 let release,count=0;const s=setup({resolveBinding:async()=>{count++;if(count===2)await new Promise(r=>{release=r;});return{storeId:'s',customerId:'c',subjects:{inventory:'p',order:'o'}};}});
 const first=await s.dialogue.turn(scope,{type:'message',message:'你好'});
 const event={type:'confirm',token:first.clarification.token,topics:['inventory']};
 const pending=s.dialogue.turn(scope,event);await new Promise(r=>setTimeout(r,0));event.topics[0]='order';release();
 assert.equal((await pending).parts[0].kind,'inventory');
});
test('confirmation revalidates evidence instead of reusing earlier known status',async()=>{
 let now=100;const ledger=new EvidenceLedger({clock:()=>now});
 ledger.ingest({id:'stock',version:1,kind:'inventory',storeId:'s',customerId:'c',subjectId:'p',state:'available',source:'inventory_snapshot',issuedAt:90,expiresAt:200,simulation:true});
 const s=setup({ledger,lookupEvidence:async()=>({state:'available',evidenceId:'stock',evidenceVersion:1})});
 const a=await s.dialogue.turn(scope,{type:'message',message:'想问库存'});
 const known=await s.dialogue.turn(scope,{type:'confirm',token:a.clarification.token,topics:['inventory']});
 assert.equal(known.parts[0].state,'available');assert.match(known.reply,/仅演练/);
 ledger.revoke('stock');
 const next=await s.dialogue.turn(scope,{type:'confirm',token:known.clarification.token,topics:['inventory']});
 assert.equal(next.parts[0].state,'unknown');assert.equal(next.parts[0].reason,'missing_or_revoked_evidence');
});
