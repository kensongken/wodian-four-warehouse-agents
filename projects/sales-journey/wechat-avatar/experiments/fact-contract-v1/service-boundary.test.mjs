import test from 'node:test';import assert from 'node:assert/strict';import {ServiceBoundaryReception,renderAction,actionChoices} from './service-boundary.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('recognized pause never calls wording model, even if it would invent new stock',async()=>{
 let writes=0;const c=new ServiceBoundaryReception({model:'test',completeFocus:async()=>raw({kind:'stop',quote:'今天不买'}),completeRoute:async()=>{throw Error('must not route');},chooseAction:async()=>{writes++;return raw({actionId:'close',reply:'我们有新到货'});}});const r=await c.turn({message:'今天不买'});assert.equal(writes,0);assert.equal(r.actionId,'close');assert.equal(r.reply,'没事，你先考虑，按你的节奏来。');assert.equal(r.modelFreeTextRendered,false);assert.equal(r.sent,false);
});
test('free text, extra fields, out-of-scope action and invented repair claims are rejected rather than softened',()=>{
 for(const v of [{actionId:'repair_limits',reply:'3-5天能配好'},{actionId:'close'},{reply:'我们有新到的简约款'}])assert.throws(()=>renderAction(raw(v),['repair_limits']));
 const r=renderAction(raw({actionId:'repair_limits'}),['repair_limits']);assert.match(r.reply,/不能保证/);assert.match(r.reply,/不能给你定天数/);
});
test('implicit request after repair stays bounded; previously asked questions cannot be asked again',async()=>{
 let message='耳钉配件丢了',topic='repair',choices=0;const c=new ServiceBoundaryReception({model:'test',completeFocus:async()=>raw({kind:'request',quote:message}),completeRoute:async()=>raw({topic}),chooseAction:async()=>{choices++;return raw({actionId:'repair_preserve'});}});
 await c.turn({message});message='那几天能好';topic='chat';const r=await c.turn({message});assert.equal(r.actionId,'clarify_service');assert.equal(choices,1);
 const u={focus:{kind:'request'},projectedTopic:'selection'};assert.ok(!actionChoices(u,{asked:['compare_outline']}).includes('compare_outline'));
});

test('open service cannot silently become shopping; repeated uncertain turns cannot repeat the same clarification',async()=>{
 let message='耳钉配件丢了',topic='repair';const c=new ServiceBoundaryReception({model:'test',completeFocus:async()=>raw({kind:'request',quote:message}),completeRoute:async()=>raw({topic}),chooseAction:async()=>raw({actionId:'repair_preserve'})});
 await c.turn({message});topic='selection';message='那多久能弄好';assert.equal((await c.turn({message})).actionId,'clarify_service');
 message='就是问这个';assert.equal((await c.turn({message})).actionId,'repair_limits');
});
