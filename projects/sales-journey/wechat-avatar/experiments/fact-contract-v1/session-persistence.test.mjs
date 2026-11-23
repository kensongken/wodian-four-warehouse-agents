import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync,writeFileSync,lstatSync,readdirSync,symlinkSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';import {spawn} from 'node:child_process';import {once} from 'node:events';import {randomUUID} from 'node:crypto';
import {SessionStore} from './session-store.mjs';
const needs={recipient:'mother',category:'necklace',occasion:'birthday',budget:1500,style:'simple'};
function directory(t){const d=mkdtempSync(join(tmpdir(),'avatar-persistence-'));t.after(()=>rmSync(d,{recursive:true,force:true}));return d;}
async function child(t,d){
 const code=`import {createSandbox} from ${JSON.stringify(new URL('./sandbox-server.mjs',import.meta.url).href)};const app=createSandbox({stateDirectory:process.env.TEST_STATE_DIR,completeExtraction:async()=>({choices:[{message:{content:JSON.stringify({items:[{field:'budget',value:1000,quote:'预算1000以内'}]})}}]})});app.server.listen(0,'127.0.0.1',()=>console.log(app.server.address().port));`;
 const p=spawn(process.execPath,['--input-type=module','-e',code],{env:{...process.env,TEST_STATE_DIR:d},stdio:['ignore','pipe','pipe']});let stderr='';p.stderr.on('data',b=>stderr+=b);
 const port=await new Promise((resolve,reject)=>{p.stdout.once('data',b=>resolve(Number(String(b).trim())));p.once('exit',()=>reject(Error(stderr||'child exited')));p.once('error',reject);});assert(port>0);
 const stop=async()=>{if(p.exitCode!==null||p.signalCode)return;const exit=once(p,'exit');p.kill('SIGKILL');await exit;};t.after(stop);
 const origin=`http://127.0.0.1:${port}`,post=async(path,body)=>{const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};return{post,stop};
}
test('real process kill/restart restores confirmed needs and tone, never unconfirmed draft, old ticket or inventory',async t=>{
 const d=directory(t),a=await child(t,d),id=(await a.post('/api/session',{})).data.sessionId;
 assert.equal((await a.post('/api/service',{sessionId:id,expectedRevision:0,confirmed:true,patch:needs})).status,200);
 await a.post('/api/service-preview',{sessionId:id,tone:'professional'});await a.post('/api/stock',{sessionId:id,mode:'available'});
 const q=(await a.post('/api/guided-selection',{sessionId:id,event:{type:'message',text:'继续'}})).data;
 const done=(await a.post('/api/guided-selection',{sessionId:id,event:{type:'choice',token:q.clarification.token,action:'continue_selection'}})).data;
 const draft=(await a.post('/api/customer-edit',{sessionId:id,token:done.review.token,source:'预算1000以内'})).data;
 const persisted=readFileSync(join(d,id+'.json'),'utf8');assert(!persisted.includes('quote'));assert(!persisted.includes('预算1000以内'));assert(!persisted.includes(draft.draft.id));assert(!persisted.includes('stock'));assert.equal(lstatSync(join(d,id+'.json')).mode&0o777,0o600);
 await a.stop();const b=await child(t,d);assert.equal((await b.post('/api/service-preview',{sessionId:id})).status,404);
 const r=await b.post('/api/session-resume',{sessionId:id});assert.equal(r.status,200);assert.equal(r.data.restoredFromDisk,true);assert.equal(r.data.persistenceEnabled,true);assert.equal(r.data.selection.revision,1);assert.equal(r.data.selection.tone,'professional');assert.deepEqual(r.data.selection.needs,{...needs,concern:'none'});assert.equal(r.data.selection.temporaryIntent.paused,true);assert.equal(r.data.historyRestored,false);assert.equal(r.data.sent,false);assert.equal(r.data.modelCalls,0);
 assert.equal((await b.post('/api/customer-edit-confirm',{sessionId:id,draftId:draft.draft.id,fields:['budget'],confirmed:true})).status,400);
 assert.equal((await b.post('/api/guided-selection',{sessionId:id,event:{type:'edit_start',token:done.review.token,field:'budget'}})).status,400);
 const other=(await b.post('/api/session',{})).data;assert.notEqual(other.sessionId,id);assert.equal(other.selection.needs.budget,null);
});
test('corrupt, incompatible and forged preference records fail recovery without replacement',async t=>{
 const d=directory(t),a=await child(t,d),id=randomUUID(),file=join(d,id+'.json');
 for(const raw of ['{broken',JSON.stringify({version:9}),JSON.stringify({version:1,id,updatedAt:Date.now(),revision:1,tone:'plain',needs:{...needs,budget:-1}})]){
  writeFileSync(file,raw);const r=await a.post('/api/session-resume',{sessionId:id});assert.equal(r.status,503);assert.equal(readFileSync(file,'utf8'),raw);
 }
});
test('expired checkpoint is removed; missing id does not create or guess a session',async t=>{
 const d=directory(t),a=await child(t,d),id=randomUUID();writeFileSync(join(d,id+'.json'),JSON.stringify({version:1,id,updatedAt:Date.now()-1800001,revision:1,tone:'plain',needs}));
 assert.equal((await a.post('/api/session-resume',{sessionId:id})).status,404);assert.deepEqual(readdirSync(d),[]);
 assert.equal((await a.post('/api/session-resume',{sessionId:randomUUID()})).status,404);assert.deepEqual(readdirSync(d),[]);
});
test('failed save returns explicit failure and blocks further use of unsaved in-memory state',async t=>{
 const d=directory(t),a=await child(t,d),id=(await a.post('/api/session',{})).data.sessionId,path=join(d,id+'.json');rmSync(path);symlinkSync(join(d,'unrelated'),path);
 assert.equal((await a.post('/api/service',{sessionId:id,expectedRevision:0,confirmed:true,patch:{budget:1500}})).status,503);
 assert.equal((await a.post('/api/service-preview',{sessionId:id})).status,503);assert.equal((await a.post('/api/session-resume',{sessionId:id})).status,503);
});
test('store rejects path traversal, symlinks, oversized and future records',t=>{
 const d=directory(t),s=new SessionStore(d),id=randomUUID(),path=join(d,id+'.json');assert.throws(()=>s.read('../outside'));
 writeFileSync(path,' '.repeat(4097));assert.throws(()=>s.read(id));rmSync(path);symlinkSync('/dev/null',path);assert.throws(()=>s.read(id));rmSync(path);
 writeFileSync(path,JSON.stringify({version:1,id,updatedAt:Date.now()+100000,revision:1,tone:'plain',needs}));assert.throws(()=>s.read(id));
});
test('store bounds files to 50 and prunes expired records before admitting a new one',t=>{
 const d=directory(t);let now=100000000;const s=new SessionStore(d,{clock:()=>now}),plan={needs,revision:1,tone:'warm'};
 for(let i=0;i<50;i++)s.save(randomUUID(),plan);assert.throws(()=>s.save(randomUUID(),plan));assert.equal(readdirSync(d).length,50);
 now+=1800001;s.save(randomUUID(),plan);assert.equal(readdirSync(d).length,1);assert.equal(lstatSync(d).mode&0o777,0o700);
});
