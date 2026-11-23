import test from 'node:test';import assert from 'node:assert/strict';import {topicRequest,parseTopic} from './service-topics.mjs';
import {createSandbox} from './sandbox-server.mjs';import {SERVICE_TOPICS} from './service-topics.mjs';import {BOUNDED_SCENARIOS} from './bounded-service.mjs';
const raw=x=>({choices:[{message:{content:JSON.stringify(x)}}]});
test('topic is explicitly an unverified proposal; repair cannot automatically imply missing earplug',()=>{const r=parseTopic('扣子松了想修',raw({topic:'repair',quote:'想修'}));assert.equal(r.suggestedScenario,'repair_general');assert.equal(r.semanticVerified,false);assert.equal(r.requiresHumanReview,true);assert.equal(r.sent,false);});
test('forged quotes, extra replies and unknown topic IDs fail; unknown has no action mapping',()=>{for(const v of [{topic:'repair',quote:'没有的原话'},{topic:'recycle',quote:'修',reply:'已退款'},{topic:'refund_now',quote:'修'}])assert.throws(()=>parseTopic('想修',raw(v)));assert.equal(parseTopic('在吗',raw({topic:'unknown',quote:null})).suggestedScenario,null);});
test('bounded customer history is data and cannot overwrite task instructions',()=>{const r=topicRequest('test','不是改款，想退货',['想换个样子']);assert(r.messages[1].content.includes('不是改款'));assert.equal(r.messages[0].role,'system');assert.throws(()=>topicRequest('test','x',Array(5).fill('old')));});
test('HTTP suggestions cannot adopt a scene, execute a service or accept injected approval',async t=>{
 let calls=0;const app=createSandbox({completeTopic:async()=>{calls++;return raw({topic:'repair',quote:'想修'});},chooseBounded:async()=>{throw Error('Service must not execute');}});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const origin=`http://127.0.0.1:${app.server.address().port}`;
 const post=body=>fetch(origin+'/api/service-topic',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});
 assert.equal((await post({message:'想修',approved:true})).status,400);assert.equal(calls,0);const r=await post({message:'想修'});assert.equal(r.status,200);const d=await r.json();assert.equal(d.requiresHumanReview,true);assert.equal(d.profileChanged,false);assert.equal(d.suggestedScenario,'repair_general');assert(!Object.hasOwn(d,'reply'));
 for(const[,scene]of Object.values(SERVICE_TOPICS))if(scene)assert(Object.hasOwn(BOUNDED_SCENARIOS,scene));
});
