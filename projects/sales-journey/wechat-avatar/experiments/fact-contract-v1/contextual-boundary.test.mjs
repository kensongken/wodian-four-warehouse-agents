import test from 'node:test';import assert from 'node:assert/strict';import {contextualUnderstanding} from './contextual-boundary.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('referents include actual prior reply and program topic; explicit current correction is retained',async()=>{
 let focusPayload,routePayload;const history=[{role:'user',content:'给我自己挑'},{role:'assistant',content:'可以比较一处细节的设计。'}];
 const r=await contextualUnderstanding({model:'test',message:'会太夸张吗',history,previousAction:'compare_detail',completeFocus:async q=>{focusPayload=JSON.parse(q.messages[1].content);return raw({kind:'request',quote:'会太夸张吗'});},completeRoute:async q=>{routePayload=JSON.parse(q.messages[1].content);return raw({topic:'selection'});}});
 assert.deepEqual(focusPayload.recentConversation,history);assert.equal(routePayload.previousDiscussion.topic,'selection');assert.equal(routePayload.previousDiscussion.reply,history[1].content);assert.equal(r.semanticVerified,false);
});
test('closing and chat do not bind later vague words to stale shopping context',async()=>{
 for(const previousAction of ['close','chat_ack'])await contextualUnderstanding({model:'test',message:'再说两句',history:[],previousAction,completeFocus:async()=>raw({kind:'request',quote:'再说两句'}),completeRoute:async q=>{assert.equal(JSON.parse(q.messages[1].content).previousDiscussion,null);return raw({topic:'unknown'});}});
});

test('experimental full context is opt-in; default service trial keeps existing interpretation',async()=>{
 const {ServiceBoundaryReception}=await import('./service-boundary.mjs');
 for(const contextual of [false,true]){let seen;const c=new ServiceBoundaryReception({model:'test',contextual,completeFocus:async q=>{seen=JSON.parse(q.messages[1].content);return raw({kind:'request',quote:'挑耳钉'});},completeRoute:async()=>raw({topic:'selection'}),chooseAction:async()=>raw({actionId:'compare_daily'})});await c.turn({message:'挑耳钉'});assert.equal(Object.hasOwn(seen,'recentConversation'),contextual);}
});
