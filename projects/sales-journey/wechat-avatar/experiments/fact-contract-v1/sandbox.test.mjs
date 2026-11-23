import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
async function fixture(t,completeRouting,completeExtraction){const app=createSandbox({completeExtraction,completeRouting:completeRouting||(async()=>({choices:[{message:{content:'{"topics":["inventory","delivery_today"],"socialAct":null}'}}]}))});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const url=`http://127.0.0.1:${app.server.address().port}`;const post=async(path,body,headers={})=>{const r=await fetch(url+path,{method:'POST',headers:{Origin:url,'Content-Type':'application/json','X-Sandbox':'1',...headers},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};return{url,post};}
test('sandbox serves page with framing protection and rejects foreign origins',async t=>{
 const f=await fixture(t);const page=await fetch(f.url);assert.equal(page.status,200);assert.equal(page.headers.get('x-frame-options'),'DENY');
 assert.equal((await f.post('/api/session',{}, {Origin:'https://example.com'})).status,403);
 assert.equal((await f.post('/api/session',{}, {'X-Sandbox':'0'})).status,403);
 assert.equal((await fetch(f.url+'/../private')).status,404);
});
test('inventory simulation is session scoped and never authorizes delivery today',async t=>{
 const f=await fixture(t);const a=(await f.post('/api/session',{})).data.sessionId,b=(await f.post('/api/session',{})).data.sessionId;
 assert.equal((await f.post('/api/stock',{sessionId:a,mode:'available',simulation:false})).status,400);
 await f.post('/api/stock',{sessionId:a,mode:'available'});
 const first=(await f.post('/api/turn',{sessionId:a,event:{type:'message',message:'有货今天能拿吗'}})).data;
 assert.deepEqual(first.states.map(x=>x.state),['available','unknown']);assert.match(first.reply,/仅演练/);assert.equal(first.sent,false);
 const other=(await f.post('/api/turn',{sessionId:b,event:{type:'message',message:'同样的问题'}})).data;
 assert.ok(other.states.every(x=>x.state==='unknown'));
 await f.post('/api/stock',{sessionId:a,mode:'unknown'});
 const next=(await f.post('/api/turn',{sessionId:a,event:{type:'confirm',token:first.clarification.token,topics:['inventory']}})).data;
 assert.equal(next.states[0].state,'unknown');
 assert.equal((await f.post('/api/turn',{sessionId:b,event:{type:'confirm',token:next.clarification.token,topics:['inventory']}})).status,400);
});
test('model failure remains visible and customer selection can continue without model',async t=>{
 let count=0;const f=await fixture(t,async()=>{count++;throw Error('offline');});const sessionId=(await f.post('/api/session',{})).data.sessionId;
 const failed=(await f.post('/api/turn',{sessionId,event:{type:'message',message:'问题'}})).data;
 assert.equal(failed.recognitionFailed,true);assert.equal(failed.choices.length,4);
 const next=(await f.post('/api/turn',{sessionId,event:{type:'confirm',token:failed.clarification.token,topics:['order']}})).data;
 assert.equal(count,1);assert.equal(next.states[0].state,'unknown');assert.equal(next.confirmedByCustomer,true);
});
test('oversized, malformed and expired session requests cannot run a turn',async t=>{
 const f=await fixture(t);assert.equal((await f.post('/api/turn',{sessionId:'missing',event:{type:'message',message:'x'}})).status,404);
 const sessionId=(await f.post('/api/session',{})).data.sessionId;
 assert.equal((await f.post('/api/turn',{sessionId,event:{type:'message',message:'x'.repeat(801)}})).status,400);
 assert.equal((await f.post('/api/stock',{sessionId,mode:'fake'})).status,400);
});
test('selection guide is confirmed, revisioned and isolated per sandbox session',async t=>{
 const f=await fixture(t);const a=(await f.post('/api/session',{})).data.sessionId,b=(await f.post('/api/session',{})).data.sessionId;
 assert.equal((await f.post('/api/service',{sessionId:a,expectedRevision:0,confirmed:false,patch:{recipient:'self'}})).status,400);
 const first=(await f.post('/api/service',{sessionId:a,expectedRevision:0,confirmed:true,patch:{recipient:'self',category:'bracelet',occasion:'work',budget:800,style:'simple'}})).data;
 assert.equal(first.nextField,null);assert.match(first.reply,/800/);assert.equal(first.productMatched,false);
 assert.equal((await f.post('/api/service',{sessionId:a,expectedRevision:0,confirmed:true,patch:{budget:900}})).status,409);
 const other=(await f.post('/api/service',{sessionId:b,expectedRevision:0,confirmed:true,patch:{}})).data;
 assert.equal(other.needs.recipient,null);assert.equal(other.needs.budget,null);
 assert.equal((await f.post('/api/service',{sessionId:b,expectedRevision:0,confirmed:true,patch:{stock:'available'}})).status,400);
});
const draftResponse=()=>({choices:[{message:{content:JSON.stringify({items:[{field:'recipient',value:'self',quote:'给自己'},{field:'budget',value:800,quote:'预算800'}]})}}]});
test('draft API never applies automatically, isolates sessions, rejects stale drafts and writes selected values only',async t=>{
 const f=await fixture(t,undefined,async()=>draftResponse());const a=(await f.post('/api/session',{})).data.sessionId,b=(await f.post('/api/session',{})).data.sessionId;
 const first=(await f.post('/api/extract',{sessionId:a,source:'给自己选手链，预算800。'})).data.draft;
 const untouched=(await f.post('/api/service',{sessionId:a,expectedRevision:0,confirmed:true,patch:{}})).data;
 assert.equal(untouched.needs.recipient,null);assert.equal(untouched.needs.budget,null);assert.equal(untouched.revision,0);
 await f.post('/api/service',{sessionId:a,expectedRevision:0,confirmed:true,patch:{concern:'hesitant'}});
 assert.equal((await f.post('/api/extract-confirm',{sessionId:a,draftId:first.id,fields:['budget'],confirmed:true})).status,409);
 const second=(await f.post('/api/extract',{sessionId:a,source:'给自己选手链，预算800。'})).data.draft;
 assert.equal((await f.post('/api/extract-confirm',{sessionId:b,draftId:second.id,fields:['budget'],confirmed:true})).status,409);
 assert.equal((await f.post('/api/extract-confirm',{sessionId:a,draftId:second.id,fields:['budget'],confirmed:true,patch:{budget:999}})).status,400);
 const applied=(await f.post('/api/extract-confirm',{sessionId:a,draftId:second.id,fields:['budget'],confirmed:true})).data;
 assert.equal(applied.needs.budget,800);assert.equal(applied.needs.recipient,null);assert.equal(applied.provenanceReceipt.items.length,1);
 assert.equal((await f.post('/api/extract-confirm',{sessionId:a,draftId:second.id,fields:['budget'],confirmed:true})).status,409);
});
test('failed extraction does not change needs and clears the session busy state',async t=>{
 const f=await fixture(t,undefined,async()=>{throw Error('offline');});const sessionId=(await f.post('/api/session',{})).data.sessionId;
 assert.equal((await f.post('/api/extract',{sessionId,source:'给自己，预算800'})).status,422);
 const plan=(await f.post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{}})).data;
 assert.equal(plan.needs.budget,null);assert.equal(plan.revision,0);
 assert.equal((await f.post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:500}})).status,200);
});
test('in-flight extraction blocks changes to the same session until its draft is formed',async t=>{
 let release,started;const signal=new Promise(r=>started=r);const f=await fixture(t,undefined,async()=>{started();return new Promise(r=>release=()=>r(draftResponse()));});const sessionId=(await f.post('/api/session',{})).data.sessionId;
 const pending=f.post('/api/extract',{sessionId,source:'给自己，预算800'});await signal;
 assert.equal((await f.post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:500}})).status,409);release();
 assert.equal((await pending).status,200);
});
test('two confirmed message drafts preserve prior budget/category while changing wearer and occasion',async t=>{
 const f=await fixture(t,undefined,async req=>{
  const source=JSON.parse(req.messages[1].content).customerStatement;
  const items=source.includes('妈妈')?[{field:'recipient',value:'mother',quote:'给妈妈'},{field:'category',value:'necklace',quote:'项链'},{field:'occasion',value:'birthday',quote:'生日'},{field:'budget',value:1500,quote:'1500'},{field:'style',value:'simple',quote:'简洁'}]:[{field:'recipient',value:'self',quote:'给自己'},{field:'occasion',value:'party',quote:'聚会'},{field:'style',value:'bold',quote:'设计感'}];
  return{choices:[{message:{content:JSON.stringify({items})}}]};
 });const sessionId=(await f.post('/api/session',{})).data.sessionId;
 const first=(await f.post('/api/extract',{sessionId,source:'给妈妈选项链，生日，1500以内，简洁。'})).data.draft;
 const a=(await f.post('/api/extract-confirm',{sessionId,draftId:first.id,fields:first.items.map(x=>x.field),confirmed:true})).data;
 assert.equal(a.nextField,null);assert.equal(a.needs.recipient,'mother');
 const second=(await f.post('/api/extract',{sessionId,source:'改给自己，聚会戴，喜欢设计感。'})).data.draft;
 const b=(await f.post('/api/extract-confirm',{sessionId,draftId:second.id,fields:second.items.map(x=>x.field),confirmed:true})).data;
 assert.equal(b.needs.recipient,'self');assert.equal(b.needs.category,'necklace');assert.equal(b.needs.budget,1500);assert.equal(b.needs.occasion,'party');assert.equal(b.nextField,null);assert.ok(!b.reply.includes('妈妈'));assert.equal(b.sent,false);
});
test('tone preview changes no revision and leaves pending extraction valid',async t=>{
 const f=await fixture(t,undefined,async()=>draftResponse());const sessionId=(await f.post('/api/session',{})).data.sessionId;
 const draft=(await f.post('/api/extract',{sessionId,source:'给自己，预算800'})).data.draft;
 const preview=(await f.post('/api/service-preview',{sessionId,tone:'friend'})).data;assert.equal(preview.revision,0);assert.equal(preview.needs.budget,null);assert.match(preview.reply,/咱们慢慢挑/);
 assert.equal((await f.post('/api/service-preview',{sessionId,tone:'friend',patch:{budget:999}})).status,400);
 const applied=await f.post('/api/extract-confirm',{sessionId,draftId:draft.id,fields:['budget'],confirmed:true,tone:'friend'});assert.equal(applied.status,200);assert.equal(applied.data.needs.budget,800);
});
