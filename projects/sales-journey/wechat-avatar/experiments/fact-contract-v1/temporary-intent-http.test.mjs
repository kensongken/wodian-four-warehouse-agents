import test from 'node:test';import assert from 'node:assert/strict';import {createSandbox} from './sandbox-server.mjs';
const response=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
async function fixture(t,{intent={act:'pause_sales',quote:'今天不买'},fail=false}={}){
 let extracts=0;const app=createSandbox({completeRouting:async()=>response({topics:['inventory'],socialAct:null}),completeIntent:async()=>{if(fail)throw Error('offline');return response(intent);},completeExtraction:async()=>{extracts++;return response({items:[{field:'recipient',value:'self',quote:'给自己'}]});}});
 await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());const url=`http://127.0.0.1:${app.server.address().port}`;
 const post=async(path,body)=>{const r=await fetch(url+path,{method:'POST',headers:{Origin:url,'X-Sandbox':'1','Content-Type':'application/json'},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};
 const sessionId=(await post('/api/session',{})).data.sessionId;return{post,sessionId,extracts:()=>extracts};
}
test('pause skips extraction and preserves preferences across previews, manual edits and business mode',async t=>{
 const {post,sessionId,extracts}=await fixture(t);
 const saved=(await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{budget:800}})).data;
 const paused=(await post('/api/selection-message',{sessionId,source:'今天不买，先看看。'})).data;
 assert.equal(paused.selection.temporaryIntent.paused,true);assert.equal(paused.draft,null);assert.equal(extracts(),0);assert.deepEqual(paused.selection.needs,saved.needs);assert.equal(paused.selection.revision,saved.revision);
 for(const tone of ['friend','warm','plain','professional']){const p=(await post('/api/service-preview',{sessionId,tone})).data;assert.equal(p.nextField,null);assert.match(p.reply,/不继续推荐/);}
 const changed=(await post('/api/service',{sessionId,expectedRevision:1,confirmed:true,patch:{budget:900}})).data;assert.match(changed.reply,/不继续推荐/);
 assert.equal((await post('/api/turn',{sessionId,event:{type:'message',message:'有货吗'}})).status,200);
 assert.equal((await post('/api/service-preview',{sessionId})).data.temporaryIntent.paused,true);
 const other=(await post('/api/session',{})).data;assert.equal(other.selection.temporaryIntent.paused,false);
 const resumed=(await post('/api/selection-control',{sessionId,action:'continue_selection'})).data;assert.equal(resumed.temporaryIntent.paused,false);assert.equal(resumed.needs.budget,900);assert.notEqual(resumed.nextField,null);
});
test('pause invalidates old drafts; paste confirmation remains protected when paused',async t=>{
 const {post,sessionId}=await fixture(t);const old=(await post('/api/extract',{sessionId,source:'给自己'})).data.draft;
 await post('/api/selection-control',{sessionId,action:'pause_sales'});
 assert.equal((await post('/api/extract-confirm',{sessionId,draftId:old.id,fields:['recipient'],confirmed:true})).status,409);
 const fresh=(await post('/api/extract',{sessionId,source:'给自己'})).data.draft;
 const plan=(await post('/api/extract-confirm',{sessionId,draftId:fresh.id,fields:['recipient'],confirmed:true})).data;
 assert.equal(plan.needs.recipient,'self');assert.equal(plan.nextField,null);assert.match(plan.reply,/不继续推荐/);
});
test('unclear cannot resume; explicit current continuation can',async t=>{
 for(const act of ['unclear','continue_selection']){const {post,sessionId,extracts}=await fixture(t,{intent:{act,quote:act==='unclear'?null:'给自己'}});await post('/api/selection-control',{sessionId,action:'pause_sales'});const r=(await post('/api/selection-message',{sessionId,source:'给自己'})).data;assert.equal(r.selection.temporaryIntent.paused,act==='unclear');assert.equal(extracts(),act==='unclear'?0:1);}
});
test('intent failure, invalid control and legacy no-buy cannot write preferences',async t=>{
 const {post,sessionId,extracts}=await fixture(t,{fail:true});assert.equal((await post('/api/selection-message',{sessionId,source:'今天不买'})).status,422);assert.equal(extracts(),0);
 assert.equal((await post('/api/selection-control',{sessionId,action:'bad'})).status,400);
 assert.equal((await post('/api/service',{sessionId,expectedRevision:0,confirmed:true,patch:{concern:'no_buy'}})).status,400);
 assert.equal((await post('/api/service-preview',{sessionId})).data.revision,0);
});
