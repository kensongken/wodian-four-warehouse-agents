import test from 'node:test';import assert from 'node:assert/strict';
import {twoStepTopic,focusRequest,parseFocus} from './two-step-topic.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('stale or fabricated current evidence stops before second inference',async()=>{
 let routed=0;await assert.rejects(()=>twoStepTopic({model:'test',message:'问保养',context:['想改款'],completeFocus:async()=>raw({kind:'request',quote:'想改款'}),completeRoute:async()=>{routed++;return raw({topic:'care'});}}));assert.equal(routed,0);
 assert.throws(()=>parseFocus('问保养',raw({kind:'request',quote:'问保养',send:true})));
});
test('only current verbatim focus enters business classifier; no old conversation leaks',async()=>{
 let request;const r=await twoStepTopic({model:'test',message:'不改款了，问保养',context:['想改款'],completeFocus:async()=>raw({kind:'request',quote:'问保养'}),completeRoute:async r=>{request=r;return raw({topic:'care'});}});
 assert.equal(JSON.parse(request.messages[1].content).currentRequestQuote,'问保养');assert.ok(!JSON.stringify(request.messages).includes('想改款'));assert.equal(r.projectedTopic,'care');assert.equal(r.sent,false);assert.equal(r.requiresHumanReview,true);
});
test('meta unclear chat and stop do not trigger business inference or execute actions',async()=>{
 for(const[kind,expected]of Object.entries({meta:'unknown',unclear:'unknown',chat:'chat',stop:'pause'})){
  const r=await twoStepTopic({model:'test',message:'当前原话',completeFocus:async()=>raw({kind,quote:'当前原话'}),completeRoute:async()=>{throw Error('must not route');}});
  assert.equal(r.projectedTopic,expected);assert.equal(r.profileChanged,false);
 }
 assert.throws(()=>focusRequest('test','x',Array(5).fill('old')));
});
