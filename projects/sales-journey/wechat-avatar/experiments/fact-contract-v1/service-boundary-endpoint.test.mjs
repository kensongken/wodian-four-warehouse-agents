import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('boundary session endpoint rejects approval injection and never renders raw model claims',async t=>{
 let message='今天不买',kind='stop',topic='repair',action={actionId:'repair_limits'};let writes=0;
 const app=createSandbox({completeFocus:async()=>raw({kind,quote:message}),completeFocusRoute:async()=>raw({topic}),chooseServiceAction:async()=>{writes++;return raw(action);}});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const origin=`http://127.0.0.1:${app.server.address().port}`;
 const post=(path,body)=>fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});const {sessionId}=await(await post('/api/session',{})).json();
 assert.equal((await post('/api/service-boundary',{sessionId,message,approved:true})).status,400);
 const close=await(await post('/api/service-boundary',{sessionId,message})).json();assert.equal(close.actionId,'close');assert.equal(writes,0);assert.equal(close.sent,false);
 message='耳钉配件丢了';kind='request';action={actionId:'repair_limits',reply:'3-5天配好'};assert.equal((await post('/api/service-boundary',{sessionId,message})).status,422);assert.equal(writes,1);
 action={actionId:'repair_limits'};const r=await(await post('/api/service-boundary',{sessionId,message})).json();assert.equal(r.modelFreeTextRendered,false);assert.match(r.reply,/不能给你定天数/);assert.equal(r.profileChanged,false);assert.equal((await fetch(origin+'/service-boundary')).status,200);
});
