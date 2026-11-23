import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {EventEmitter} from 'node:events';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {parseGrounding,pngInfo,locateComposer} from '../lib/ui-tars.mjs';
import {createDriver,inspectFrame} from '../drivers/ui-tars.mjs';
import {DesktopController} from '../lib/desktop.mjs';
import {hash} from '../lib/policy.mjs';

const png=()=>{const b=Buffer.alloc(60);Buffer.from([137,80,78,71,13,10,26,10]).copy(b);b.writeUInt32BE(1000,16);b.writeUInt32BE(800,20);b.write('SYNTHETIC TEST IMAGE',24);return b.toString('base64');};
function fixture(t){const directory=mkdtempSync(join(tmpdir(),'tars-test-'));const store=new Store(directory);const engine=new Engine(store,()=>Date.parse('2026-10-26T04:00:00Z'));const owner=engine.setup({username:'owner',name:'测试负责人',password:'test-owner-password'});t.after(()=>{store.close();rmSync(directory,{recursive:true,force:true});});return{store,engine,owner};}
const profile={customerId:'customer',account:'account',conversation:'unique-contact',name:'客户A',identityIdentifier:'contact.uniqueId',identityValue:'contact-uuid',inputIdentifier:'composer',sendIdentifier:'sendButton',messagePrefix:'message-',incomingDescription:'incoming',outgoingDescription:'outgoing'};
const raw=(extra=[])=>({frameId:'frame',at:1000,epoch:1,windowID:10,bounds:{x:100,y:100,width:1000,height:800},image:png(),width:1000,height:800,humanActive:false,nodes:[
 {nodeId:'identity',identifier:'contact.uniqueId',value:'contact-uuid',title:'',role:'AXStaticText'},
 {nodeId:'input',identifier:'composer',role:'AXTextArea',value:'',rect:{x:300,y:650,width:500,height:200}},
 {nodeId:'send',identifier:'sendButton',role:'AXButton',title:'发送'},
 {nodeId:'m1',identifier:'message-1',role:'AXStaticText',value:'你好',description:'incoming'},...extra]});

test('UI-TARS validates PNG dimensions without trusting client-provided bounds',()=>{assert.deepEqual(pngInfo(png()),{width:1000,height:800});assert.throws(()=>pngInfo(Buffer.from('not an image').toString('base64')),/PNG/);});
test('official pinned parser accepts one normalized composer click',async()=>{const point=await parseGrounding("Thought: Locate input.\nAction: click(start_box='(500, 800)')",1000,800);assert.equal(point.x,500);assert.equal(point.y,640);assert.equal(point.parser,'@ui-tars/action-parser@1.2.3');});
test('model cannot turn grounding into arbitrary typing, sending, hotkeys or multiple actions',async()=>{
 for(const text of ["Action: type(content='hello\\n')","Action: hotkey(key='Return')","Action: click(start_box='(500, 500)')\nAction: click(start_box='(900, 900)')","Action: click(start_box='(1001, 10)')","Action: call_user()","Action: click(start_box='(__import__(\"os\"), 3)')"]){await assert.rejects(()=>parseGrounding(text,1000,800));}
});
test('new installations use UI-TARS, legacy configured providers retain compatibility',t=>{const f=fixture(t);assert.equal(f.store.read().settings.visionProvider,'ui_tars');f.store.transact(s=>{delete s.settings.visionProvider;s.settings.visionEndpoint='https://api.thiflow.com/vision';});assert.equal(f.store.read().settings.visionProvider,'shiflow');});
test('unconfigured UI-TARS does not call a paid service',async t=>{const f=fixture(t);f.engine.settings(f.owner,{version:1,visionProvider:'ui_tars',tarsModel:''});let calls=0;await assert.rejects(()=>locateComposer(f.engine,async()=>{calls++;},f.owner,{image:png()}),/先配置/);assert.equal(calls,0);});
test('UI-TARS cloud test is a locator candidate, never executes or persists screenshot',async t=>{
 const f=fixture(t);f.engine.settings(f.owner,{version:1,tarsEndpoint:'https://ark.cn-beijing.volces.com/api/v3/chat/completions',tarsKey:'private-tars-key',tarsModel:'test-ui-tars-endpoint',dataConsent:true});let called;
 const point=await locateComposer(f.engine,async(url,headers,payload)=>{called={url,headers,payload};return{choices:[{message:{content:"Action: click(start_box='(500, 800)')"}}]};},f.owner,{image:png()});
 assert.equal(point.executed,false);assert.equal(called.payload.model,'test-ui-tars-endpoint');assert.equal(f.store.read().tasks.length,0);assert.ok(!JSON.stringify(f.store.read()).includes(png()));assert.ok(!JSON.stringify(f.engine.snapshot(f.owner)).includes('private-tars-key'));
});
test('provider errors do not retry, use another model, or record a successful invocation',async t=>{const f=fixture(t);f.engine.settings(f.owner,{version:1,tarsEndpoint:'https://ark.cn-beijing.volces.com/api/v3/chat/completions',tarsKey:'key',tarsModel:'ui-tars',dataConsent:true});let calls=0;await assert.rejects(()=>locateComposer(f.engine,async()=>{calls++;throw new Error('provider failure');},f.owner,{image:png()}));assert.equal(calls,1);assert.equal(f.store.read().costs[0].status,'failed');assert.equal(f.store.read().settings.model,'gpt-5.6-sol');});
test('non-approved endpoint and sales-model family changes are still rejected',t=>{const f=fixture(t);assert.throws(()=>f.engine.settings(f.owner,{version:1,tarsEndpoint:'https://evil.invalid/chat/completions'}),/UI-TARS/);assert.throws(()=>f.engine.settings(f.owner,{version:1,model:'gpt-6-astra'}),/GPT-5.6/);});
test('driver refuses duplicate contact identity or missing stable message identifiers',()=>{assert.equal(inspectFrame(raw(),profile).latestCustomerText,'你好');assert.throws(()=>inspectFrame(raw([{nodeId:'duplicate',identifier:'contact.uniqueId',value:'contact-uuid'}]),profile),/IDENTITY/);const frame=raw();frame.nodes.at(-1).identifier='';assert.throws(()=>inspectFrame(frame,profile),/STABLE_MESSAGE/);});
test('first observation establishes a baseline; new identical text with distinct stable IDs remains new',async()=>{
 let current=raw();const driver=createDriver({bridge:{call:async()=>current,stop(){},close(){}},post:async()=>profile,clock:()=>1000});
 assert.equal((await driver.capture()).messages.length,0);
 current=raw([{nodeId:'m2',identifier:'message-2',value:'你好',description:'incoming'}]);const next=await driver.capture();assert.equal(next.messages.length,1);assert.notEqual(next.messages[0].eventId,hash(`${profile.account}:${profile.conversation}:message-1`));driver.accepted(next.messages[0]);assert.equal((await driver.capture()).messages.length,0);
});
test('physical human activity requires manual rearming; it never silently resumes',async()=>{let current=raw();const driver=createDriver({bridge:{call:async()=>current,stop(){},close(){}},post:async()=>profile,clock:()=>1000});await driver.capture();current={...raw(),epoch:2};await assert.rejects(()=>driver.capture(),/HUMAN_INTERVENED/);await driver.stop();await assert.rejects(()=>driver.capture(),/HUMAN_INTERVENED/);});
test('off-target VLM grounding cannot reach the native send function',async()=>{
 const calls=[];const driver=createDriver({bridge:{call:async command=>{calls.push(command);return raw();},stop(){},close(){}},post:async(path)=>path.endsWith('/profile')?profile:{x:0,y:0},clock:()=>1000});await driver.capture();await assert.rejects(()=>driver.prepare(),/GROUNDING/);assert.ok(!calls.includes('send'));
});
test('driver sends only the authorized exact text and suppresses its own observed outgoing echo',async()=>{
 const calls=[];const bridge={call:async(command,payload)=>{calls.push({command,payload});return command==='send'?{status:'observed_sent',evidenceHash:'a'.repeat(64),outgoingId:'message-out-1'}:raw();},stop(){},close(){}};
 const driver=createDriver({bridge,post:async path=>path.endsWith('/profile')?profile:{x:500,y:640},clock:()=>1000});await driver.capture();await driver.prepare();await driver.capture();
 await assert.rejects(()=>driver.execute({text:'未授权文字',hash:hash('other'),target:profile,grantExpires:2000}),/STALE/);
 const result=await driver.execute({text:'已授权的固定回复',hash:hash('已授权的固定回复'),target:profile,grantExpires:2000});assert.equal(result.status,'observed_sent');assert.equal(calls.filter(x=>x.command==='send').length,1);assert.equal(calls.at(-1).payload.text,'已授权的固定回复');
});
test('stale grants and edited input cannot be executed',async()=>{const frame=raw();frame.nodes[1].value='员工正在编辑';const driver=createDriver({bridge:{call:async()=>frame,stop(){},close(){}},post:async()=>profile,clock:()=>1000});await driver.capture();await assert.rejects(()=>driver.prepare(),/INPUT_NOT_EMPTY/);await assert.rejects(()=>driver.execute({grantExpires:999,text:'x',hash:hash('x'),target:profile}),/STALE/);});
test('desktop profile stores the local device token encrypted and never returns it to state',t=>{
 const f=fixture(t);const c=f.engine.createCustomer(f.owner,{alias:'授权测试客户'});const desktop=new DesktopController(f.engine);const result=desktop.saveProfile(f.owner,{...profile,customerId:c.id});assert.ok(result.deviceId);const token=f.store.read().settings.desktopToken;assert.ok(token);assert.ok(!JSON.stringify(f.engine.snapshot(f.owner)).includes(token));assert.equal(f.store.read().devices[0].verified,false);assert.equal(desktop.profile(f.engine.device(token)).conversation,profile.conversation);
});
test('missing desktop WeChat blocks starting before any child process is launched',async t=>{
 const f=fixture(t);const c=f.engine.createCustomer(f.owner,{alias:'授权测试客户'});let spawned=0;const desktop=new DesktopController(f.engine,{status:async()=>({prepared:true,wechatInstalled:false}),startProcess:()=>{spawned++;}});desktop.saveProfile(f.owner,{...profile,customerId:c.id});await assert.rejects(()=>desktop.start(f.owner),/微信安装入口/);assert.equal(spawned,0);
});
test('starting local capture always returns to observe mode even if old state was controlled',async t=>{
 const f=fixture(t);const c=f.engine.createCustomer(f.owner,{alias:'授权测试客户'});const child=new EventEmitter();child.stderr=new EventEmitter();child.kill=()=>true;
 const desktop=new DesktopController(f.engine,{status:async()=>Object.fromEntries(['prepared','wechatInstalled','wechatRunning','accessibility','screenRecording','inputMonitoring'].map(k=>[k,true])),startProcess:()=>child});desktop.saveProfile(f.owner,{...profile,customerId:c.id});f.store.transact(s=>s.settings.mode='controlled');const result=await desktop.start(f.owner);assert.equal(result.mode,'observe');assert.equal(f.store.read().settings.mode,'observe');desktop.stop(f.owner);
});
test('employee cannot inspect local window or configure an execution profile',async t=>{const f=fixture(t);const desktop=new DesktopController(f.engine);await assert.rejects(()=>desktop.probe({role:'employee'}),/老板/);assert.throws(()=>desktop.saveProfile({role:'employee'},profile),/老板/);});
test('changing UI-TARS credentials invalidates previous device acceptance and controlled mode',t=>{
 const f=fixture(t);const c=f.engine.createCustomer(f.owner,{alias:'授权测试客户'});new DesktopController(f.engine).saveProfile(f.owner,{...profile,customerId:c.id});
 f.store.transact(s=>{s.settings.mode='controlled';s.devices[0].verified=true;});
 f.engine.settings(f.owner,{version:f.store.read().settings.policyVersion,tarsKey:'new-private-key'});
 assert.equal(f.store.read().settings.mode,'assist');assert.equal(f.store.read().devices[0].verified,false);
});
test('a newer identical customer message invalidates a prepared point by event identity',async()=>{
 let current=raw();let sends=0;const driver=createDriver({bridge:{call:async command=>{if(command==='send')sends++;return current;},stop(){},close(){}},post:async path=>path.endsWith('/profile')?profile:{x:500,y:640},clock:()=>1000});
 await driver.capture();await driver.prepare();current=raw([{nodeId:'m2',identifier:'message-2',value:'你好',description:'incoming'}]);
 await assert.rejects(()=>driver.capture(),/CUSTOMER_MESSAGE_CHANGED/);
 await assert.rejects(()=>driver.execute({text:'您好',hash:hash('您好'),target:profile,grantExpires:2000}),/STALE/);assert.equal(sends,0);
});
test('owner preview uses a distinct focus command, returns diagnostics and leaves automatic mode unverified',async t=>{
 const f=fixture(t);const commands=[];let closed=false;const desktop=new DesktopController(f.engine,{bridge:()=>({call:async command=>{commands.push(command);return{frameId:'test',width:1000,height:800,image:'',nodes:[{role:'AXWindow',identifier:''}]};},close:()=>closed=true})});
 const result=await desktop.probe(f.owner);assert.deepEqual(commands,['preview']);assert.deepEqual(result.diagnostic,{elementCount:1,identifiedControls:0,editableInputs:0});assert.equal(closed,true);assert.equal(f.store.read().settings.mode,'assist');assert.equal(f.store.read().devices.length,0);
});
test('a failed owner preview always closes its native bridge',async t=>{
 const f=fixture(t);let closed=false;const desktop=new DesktopController(f.engine,{bridge:()=>({call:async()=>{throw new Error('WECHAT_NOT_FRONTMOST');},close:()=>closed=true})});await assert.rejects(()=>desktop.probe(f.owner),/FRONTMOST/);assert.equal(closed,true);
});

test('owner desktop preview refuses login controls and always closes the native bridge',async t=>{
 const f=fixture(t);let closed=0;const desktop=new DesktopController(f.engine,{bridge:()=>({call:async()=>({width:1000,height:800,image:'',nodes:[{role:'AXStaticText',value:'提示 为了你的账号安全，请重新登录。'}]}),close:()=>closed++})});await assert.rejects(()=>desktop.probe(f.owner),/登录或安全确认页面/);assert.equal(closed,1);assert.equal(f.store.read().messages.length,0);
});
