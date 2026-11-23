import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('two-step endpoint requires review and never treats client approval as authorization',async t=>{
 let calls=0,focus={kind:'request',quote:'问保养'};const app=createSandbox({completeFocus:async()=>{calls++;return raw(focus);},completeFocusRoute:async()=>{calls++;return raw({topic:'care'});}});
 await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const origin=`http://127.0.0.1:${app.server.address().port}`;
 const post=body=>fetch(origin+'/api/two-step-topic',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});
 assert.equal((await post({message:'问保养',approved:true})).status,400);assert.equal(calls,0);
 const r=await(await post({message:'问保养'})).json();assert.equal(r.suggestedScenario,'care_unknown');assert.equal(r.requiresHumanReview,true);assert.equal(r.sent,false);assert.equal(r.profileChanged,false);assert.equal(calls,2);
 focus={kind:'meta',quote:'演练'};const meta=await(await post({message:'演练'})).json();assert.equal(meta.suggestedScenario,null);assert.equal(meta.topic,'unknown');assert.equal(calls,3);
 focus={kind:'request',quote:'旧话'};assert.equal((await post({message:'现在问退货'})).status,422);assert.equal(calls,4);assert.equal((await fetch(origin+'/two-step')).status,200);
});
