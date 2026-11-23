import test from 'node:test';import assert from 'node:assert/strict';import {SelectionGuide} from './selection-guide.mjs';import {NeedDrafts,parseExtraction} from './need-drafts.mjs';
const source='给自己选手链，上班戴，预算800以内，喜欢简洁。';const response=items=>({choices:[{message:{content:JSON.stringify({items})}}]});
const rows=[{field:'recipient',value:'self',quote:'给自己'},{field:'budget',value:800,quote:'预算800以内'}];
test('refresh validation is read-only and rejects expired, foreign and revised drafts',()=>{
 let now=0;const guide=new SelectionGuide(),drafts=new NeedDrafts(guide,{clock:()=>now}),d=drafts.create(source,response(rows),0);drafts.assertCurrent(d.id);assert.equal(guide.preview().revision,0);assert.throws(()=>drafts.assertCurrent('foreign'));
 now=300001;assert.throws(()=>drafts.assertCurrent(d.id));now=0;guide.update({expectedRevision:0,confirmed:true,patch:{budget:500}});assert.throws(()=>drafts.assertCurrent(d.id));assert.equal(guide.preview().needs.budget,500);
});
test('extraction is a draft, checked fields alone are applied, and receipt cannot be reused',()=>{
 const guide=new SelectionGuide(),drafts=new NeedDrafts(guide),draft=drafts.create(source,response(rows),0);
 assert.equal(guide.preview().needs.recipient,null);assert.equal(guide.preview().revision,0);
 const result=drafts.apply({draftId:draft.id,fields:['budget'],confirmed:true});assert.equal(result.needs.budget,800);assert.equal(result.needs.recipient,null);
 assert.throws(()=>drafts.apply({draftId:draft.id,fields:['recipient'],confirmed:true}),/Stale/);
});
test('fabricated quotes, duplicates, business-state fields and null clears are rejected',()=>{
 for(const items of [[{...rows[0],quote:'原话没有'}],[rows[0],rows[0]],[{field:'stock',value:'available',quote:'手链'}],[{...rows[0],value:null}]])assert.throws(()=>parseExtraction(source,response(items)));
});
test('stale, expired, discarded and foreign draft identifiers cannot mutate guide',()=>{
 let now=0;const guide=new SelectionGuide(),drafts=new NeedDrafts(guide,{clock:()=>now});
 let d=drafts.create(source,response(rows),0);assert.throws(()=>drafts.apply({draftId:'foreign',fields:['budget'],confirmed:true}),/Stale/);
 guide.update({expectedRevision:0,confirmed:true,patch:{budget:500}});assert.throws(()=>drafts.apply({draftId:d.id,fields:['budget'],confirmed:true}),/Stale/);assert.equal(guide.preview().needs.budget,500);
 d=drafts.create(source,response(rows),1);now=300000;assert.throws(()=>drafts.apply({draftId:d.id,fields:['budget'],confirmed:true}),/Stale/);
 d=drafts.create(source,response(rows),1);drafts.discard();assert.throws(()=>drafts.apply({draftId:d.id,fields:['budget'],confirmed:true}),/Stale/);
});
test('explicit confirmation and an allowed nonempty field selection are mandatory',()=>{
 const guide=new SelectionGuide(),drafts=new NeedDrafts(guide),d=drafts.create(source,response(rows),0);
 for(const values of [{fields:['budget'],confirmed:false},{fields:[],confirmed:true},{fields:['stock'],confirmed:true}])assert.throws(()=>drafts.apply({draftId:d.id,...values}),/Invalid/);
 assert.equal(guide.preview().revision,0);
});
test('literal quote validation does not prove correct meaning; misleading draft still requires human review',()=>{
 const text='不是给妈妈，是给自己。';const parsed=parseExtraction(text,response([{field:'recipient',value:'mother',quote:'给妈妈'}]));
 assert.equal(parsed[0].value,'mother'); // Demonstrates a remaining semantic limitation, not an approved fact.
});
test('confirmed items keep exact source receipts; changed values are explicit and history cannot be mutated',()=>{
 const guide=new SelectionGuide();guide.update({expectedRevision:0,confirmed:true,patch:{budget:500}});const drafts=new NeedDrafts(guide),d=drafts.create(source,response(rows),1);
 assert.equal(d.items[1].currentValue,500);assert.equal(d.items[1].changesExisting,true);
 const plan=drafts.apply({draftId:d.id,fields:['budget'],confirmed:true});assert.equal(plan.provenanceReceipt.items.length,1);assert.equal(plan.provenanceReceipt.source,source);
 plan.provenanceReceipt.items[0].value=1;assert.equal(drafts.history()[0].items[0].value,800);
});
