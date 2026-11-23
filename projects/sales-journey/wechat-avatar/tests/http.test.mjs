import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createApp } from '../server.mjs';
import http from 'node:http';

async function app(t,options={}) {
  const dir=mkdtempSync(join(tmpdir(),'avatar-http-'));const instance=createApp({directory:dir,worker:false,...options});
  await new Promise(resolve=>instance.server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${instance.server.address().port}`; let cookie='';
  const request=async(path,body,extra={})=>{
    const response=await fetch(origin+path,{...extra,headers:{...(body===undefined?{}:{Origin:origin,'Content-Type':'application/json'}),...(cookie?{Cookie:cookie}:{}),...extra.headers},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
    if(response.headers.has('set-cookie'))cookie=response.headers.get('set-cookie').split(';')[0];
    const data=response.headers.get('content-type')?.includes('application/json')?await response.json():await response.text();
    return{status:response.status,data,headers:response.headers};
  };
  t.after(async()=>{await instance.close();rmSync(dir,{recursive:true,force:true});});
  const setup=()=>request('/api/setup',{username:'owner',name:'本机负责人',password:'local-test-password-123'});
  return{...instance,origin,request,setup,setCookie:(value)=>cookie=value};
}

test('initialization, authenticated session and secure headers work over actual HTTP',async t=>{
  const a=await app(t);assert.equal((await a.request('/api/bootstrap')).data.needsSetup,true);
  assert.equal((await a.request('/api/state')).status,401);
  const setup=await a.setup();assert.equal(setup.status,200);assert.match(setup.headers.get('set-cookie'),/HttpOnly/);assert.match(setup.headers.get('set-cookie'),/SameSite=Strict/);
  const state=await a.request('/api/state');assert.equal(state.status,200);assert.equal(state.data.actor.role,'owner');assert.ok(!JSON.stringify(state.data).includes('local-test-password'));
  assert.equal((await a.setup()).status,409);
  const page=await a.request('/');assert.equal(page.status,200);assert.match(page.headers.get('content-security-policy'),/frame-ancestors 'none'/);
  assert.equal((await a.request('/.data/encryption.key')).status,404);
});
test('cross-origin mutation and browser calls to device routes are rejected',async t=>{
  const a=await app(t);await a.setup();
  assert.equal((await a.request('/api/customers',{alias:'非法跨站'},{headers:{Origin:'https://attacker.invalid'}})).status,403);
  assert.equal((await a.request('/api/device/heartbeat',{})).status,403);
  const hostStatus = await new Promise((resolve,reject)=>{const req=http.request(a.origin+'/api/state',{headers:{Host:'attacker.invalid'}},res=>{res.resume();resolve(res.statusCode);});req.on('error',reject);req.end();});
  assert.equal(hostStatus,403);
});
test('full human workflow persists customer, review, distinct manual result and next action',async t=>{
  const a=await app(t);await a.setup();
  const c=(await a.request('/api/customers',{alias:'客户A'})).data;
  assert.equal((await a.request(`/api/customers/${c.id}/messages`,{text:'最低多少',eventId:'m1'})).status,200);
  let d=(await a.request(`/api/customers/${c.id}/generate`,{})).data;assert.equal(d.risk,'human');
  d=(await a.request(`/api/drafts/${d.id}`,{action:'edit',revision:1,text:'我先请同事核实具体方案，再接着为您处理。'})).data;
  const {createHash}=await import('node:crypto');const digest=createHash('sha256').update(d.text).digest('hex');
  assert.equal((await a.request(`/api/drafts/${d.id}`,{action:'review',revision:d.revision,textHash:digest})).status,200);
  assert.equal((await a.request(`/api/drafts/${d.id}`,{action:'manual-sent',revision:d.revision})).status,200);
  assert.equal((await a.request('/api/reminders',{customerId:c.id,text:'核实后接续处理',due:'2027-01-01T09:00:00+08:00'})).status,200);
  const state=(await a.request('/api/state')).data;assert.equal(state.tasks.length,0);assert.equal(state.messages.at(-1).source,'manual_attestation');assert.equal(state.reminders.length,2);
});
test('device token never appears in state; credential is required on native routes',async t=>{
  const a=await app(t);await a.setup();const d=(await a.request('/api/devices',{name:'模拟设备',kind:'fixture'})).data;
  const state=(await a.request('/api/state')).data;assert.ok(!JSON.stringify(state).includes(d.token));
  let r=await fetch(a.origin+'/api/device/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});assert.equal(r.status,401);
  r=await fetch(a.origin+'/api/device/heartbeat',{method:'POST',headers:{Authorization:`Bearer ${d.token}`,'Content-Type':'application/json'},body:'{}'});assert.equal(r.status,200);
});
test('login failures lock after five attempts without exposing username existence',async t=>{
  const a=await app(t);await a.setup();await a.request('/api/logout',{});
  for(let i=0;i<5;i++){const r=await a.request('/api/login',{username:'not-a-user',password:'test-wrong-password'});assert.equal(r.status,401);assert.equal(r.data.error,'账号或密码不正确。');}
  assert.equal((await a.request('/api/login',{username:'owner',password:'local-test-password-123'})).status,429);
});
test('owner-only settings remain inaccessible to employee HTTP sessions',async t=>{
  const a=await app(t);await a.setup();await a.request('/api/users',{name:'员工',username:'worker',password:'test-worker-password-123'});
  await a.request('/api/logout',{});await a.request('/api/login',{username:'worker',password:'test-worker-password-123'});
  assert.equal((await a.request('/api/state')).data.mustChangePassword,true);
  assert.equal((await a.request('/api/customers',{alias:'首次改密前不可创建'})).status,403);
  assert.equal((await a.request('/api/account/password',{oldPassword:'test-worker-password-123',newPassword:'test-new-test-worker-password-123'})).status,200);
  assert.equal((await a.request('/api/state')).status,401);
  await a.request('/api/login',{username:'worker',password:'test-new-test-worker-password-123'});
  const state=(await a.request('/api/state')).data;assert.equal(state.actor.role,'employee');assert.deepEqual(Object.keys(state.settings).sort(),['mode','paused']);
  assert.equal((await a.request('/api/settings',{version:1,visionKey:'should-not-be-stored'})).status,403);
  assert.equal((await a.request('/api/export')).status,403);
  for(const path of ['/api/drafts/any/stop-model','/api/model/test','/api/local-ocr/preview','/api/local-ocr/confirm','/api/local-ocr/discard','/api/local-ocr/window','/api/local-ocr/region','/api/local-ocr/discard-window'])assert.equal((await a.request(path,{})).status,403,path);
});
test('desktop and UI-TARS routes require a logged-in owner and never provision a service',async t=>{
 const a=await app(t);assert.equal((await a.request('/api/desktop/status',{})).status,401);await a.setup();
 assert.equal((await a.request('/api/desktop/start',{})).status,409);
 assert.equal((await a.request('/api/ui-tars/locate',{image:'invalid'})).status,400);
 assert.equal(a.desktop.process,null);assert.equal(a.store.read().costs.length,0);
 await a.request('/api/users',{name:'员工',username:'worker2',password:'test-worker-password-123'});
 await a.request('/api/logout',{});await a.request('/api/login',{username:'worker2',password:'test-worker-password-123'});
 await a.request('/api/account/password',{oldPassword:'test-worker-password-123',newPassword:'test-new-test-worker-password-123'});
 await a.request('/api/login',{username:'worker2',password:'test-new-test-worker-password-123'});
 for(const path of ['/api/desktop/status','/api/desktop/probe','/api/desktop/start','/api/desktop/stop','/api/desktop/profile','/api/ui-tars/locate'])assert.equal((await a.request(path,{})).status,403,path);
});
test('desktop device profile is token-bound and refuses another device or customer',async t=>{
 const a=await app(t);await a.setup();const c=(await a.request('/api/customers',{alias:'授权测试联系人'})).data;
 const p={customerId:c.id,account:'test-account',conversation:'test-contact',name:'测试联系人',identityIdentifier:'contact-id',identityValue:'uuid-1',inputIdentifier:'composer',sendIdentifier:'send',messagePrefix:'message-',incomingDescription:'incoming',outgoingDescription:'outgoing'};
 assert.equal((await a.request('/api/desktop/profile',p)).status,200);
 const token=a.store.read().settings.desktopToken;
 const deviceRequest=async(path,body,key=token)=>{const r=await fetch(a.origin+path,{method:'POST',headers:{Authorization:`Bearer ${key}`,'Content-Type':'application/json'},body:JSON.stringify(body)});return {status:r.status,data:await r.json()};};
 assert.equal((await deviceRequest('/api/device/profile',{})).data.customerId,c.id);
 assert.equal((await deviceRequest('/api/device/vision/locate',{customerId:'another'})).status,403);
 const other=(await a.request('/api/devices',{name:'其他设备',kind:'real'})).data;
 assert.equal((await deviceRequest('/api/device/profile',{},other.token)).status,409);
 assert.ok(!JSON.stringify((await a.request('/api/state')).data).includes(token));
});

test('local OCR HTTP preview and confirmation are local, authenticated and separate actions',async t=>{
 let calls=0;const a=await app(t,{localOCROptions:{bridge:()=>({call:async command=>{assert.equal(command,'ocr');calls++;return{lines:[{text:'最低多少钱',confidence:.9}]};},close(){}})}});await a.setup();
 const c=(await a.request('/api/customers',{alias:'合成截图客户'})).data;
 const image='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0i8AAAAASUVORK5CYII=';
 const p=(await a.request('/api/local-ocr/preview',{customerId:c.id,image})).data;assert.ok(p.previewId);assert.equal(a.store.read().messages.length,0);
 const body={previewId:p.previewId,customerName:c.alias,rows:[{index:0,text:'最低多少钱'}],confirmed:true,singleMessage:true};
 assert.equal((await a.request('/api/local-ocr/confirm',{...body,customerName:'另一客户'})).status,400);assert.equal(a.store.read().messages.length,0);
 const result=await a.request('/api/local-ocr/confirm',body);assert.equal(result.status,200);assert.equal(result.data.sent,false);assert.equal(a.store.read().messages[0].source,'local_ocr_confirmed');assert.equal(a.store.read().costs.length,0);assert.equal(a.store.read().tasks.length,0);assert.equal(calls,1);
});


test('authenticated model test uses builtin text and leaves customer work untouched',async t=>{
 let calls=0;let sent;const a=await app(t,{transport:async(_url,_headers,payload)=>{calls++;sent=payload;return{choices:[{message:{content:'合成连接测试回复'}}]};}});await a.setup();
 assert.equal((await a.request('/api/model/test',{version:1,confirmed:true})).status,409);assert.equal(calls,0);
 assert.equal((await a.request('/api/settings',{version:1,modelEndpoint:'https://model.example.com/chat/completions',modelKey:'fake-key'})).status,200);
 assert.equal((await a.request('/api/model/test',{version:2})).status,400);
 const r=await a.request('/api/model/test',{version:2,confirmed:true});assert.equal(r.status,200);assert.equal(r.data.customerDataSent,false);assert.equal(r.data.automaticSendEligible,false);assert.equal(calls,1);assert.match(sent.messages[1].content,/内置演练/);
 const state=(await a.request('/api/state')).data;assert.equal(state.messages.length,0);assert.equal(state.drafts.length,0);assert.equal(state.tasks.length,0);assert.equal(state.costs[0].status,'success');assert.ok(!JSON.stringify(state).includes('fake-key'));
});

test('owner stops an in-flight local model request over HTTP and late response cannot overwrite draft',async t=>{
 let resolve,startedResolve;const started=new Promise(r=>startedResolve=r);const a=await app(t,{localQwen:{available:()=>true,close(){},complete:()=>{startedResolve();return new Promise(r=>resolve=r);}}});await a.setup();await a.request('/api/settings',{version:1,modelProvider:'qwen_local'});await a.request('/api/settings',{version:2,modelDataConsent:true});const c=(await a.request('/api/customers',{alias:'HTTP合成停止客户',synthetic:true})).data;await a.request(`/api/customers/${c.id}/messages`,{text:'你好',synthetic:true});const d=(await a.request(`/api/customers/${c.id}/generate`,{})).data;const jobId=crypto.randomUUID();const pending=a.request(`/api/drafts/${d.id}/model`,{jobId});await started;
 assert.equal((await a.request(`/api/drafts/${d.id}/stop-model`,{jobId})).data.stopped,true);resolve({choices:[{message:{content:'迟到模型文字'}}]});assert.equal((await pending).status,409);assert.deepEqual(a.store.read().drafts[0],d);assert.equal(a.store.read().costs[0].status,'canceled');
});
