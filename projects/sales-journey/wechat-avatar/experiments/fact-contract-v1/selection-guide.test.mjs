import test from 'node:test';import assert from 'node:assert/strict';import {SelectionGuide} from './selection-guide.mjs';
const update=(g,patch,tone='plain')=>g.update({expectedRevision:g.preview().revision,confirmed:true,patch,tone});
test('asks only the next missing dimension, not already-confirmed information',()=>{
 const g=new SelectionGuide();assert.equal(g.preview().nextField,'recipient');
 assert.equal(update(g,{recipient:'self'}).nextField,'category');
 assert.equal(update(g,{category:'bracelet',occasion:'work',budget:800}).nextField,'style');
 const p=update(g,{style:'simple'});assert.equal(p.nextField,null);assert.match(p.reply,/800/);assert.ok(!p.reply.includes('？'));assert.equal(p.productMatched,false);
});
test('recipient change preserves budget and category but not a different wearer style',()=>{
 const g=new SelectionGuide();update(g,{recipient:'mother',category:'necklace',occasion:'daily',budget:1500,style:'simple'});
 const changed=update(g,{recipient:'self',occasion:'party'});
 assert.equal(changed.needs.budget,1500);assert.equal(changed.needs.category,'necklace');assert.equal(changed.needs.style,null);assert.equal(changed.nextField,'style');assert.ok(!changed.reply.includes('妈妈'));
});
test('gift questions address recipient, not purchaser',()=>{
 const g=new SelectionGuide();const p=update(g,{recipient:'mother',category:'ring',occasion:'birthday',budget:800});assert.match(p.reply,/妈妈平时/);
});
test('no-buy overrides all further questions while retaining confirmed needs',()=>{
 const g=new SelectionGuide();update(g,{recipient:'self',budget:500});const stopped=update(g,{concern:'no_buy'});
 assert.equal(stopped.nextField,null);assert.ok(!stopped.reply.includes('？'));assert.match(stopped.reply,/今天不决定也可以/);assert.equal(update(g,{concern:'none'}).needs.budget,500);
});
test('private budget and unsure style are respected without repeated interrogation',()=>{
 const g=new SelectionGuide();const p=update(g,{recipient:'friend',category:'undecided',occasion:'daily',budget:'private',style:'unsure'});
 assert.equal(p.nextField,null);assert.match(p.reply,/预算先不展开/);
});
test('unconfirmed input, arbitrary replies, model claims and stale revisions cannot write state',()=>{
 const g=new SelectionGuide();for(const patch of [{reply:'店里有货'},{stock:'available'},{budget:-1},{style:'guaranteed-fit'}])assert.throws(()=>update(g,patch),/Invalid/);
 assert.throws(()=>g.update({expectedRevision:0,confirmed:false,patch:{recipient:'self'}}),/Invalid/);update(g,{recipient:'self'});assert.throws(()=>g.update({expectedRevision:0,confirmed:true,patch:{recipient:'mother'}}),/Stale/);
});
test('base tones preserve needs, budget and absence of product matching',()=>{
 for(const tone of ['friend','warm','plain','professional']){const g=new SelectionGuide();const p=update(g,{recipient:'self',category:'bracelet',occasion:'work',budget:800,style:'simple'},tone);assert.equal(p.needs.budget,800);assert.match(p.reply,/真实商品资料核对/);assert.equal(p.ownerVoiceLearned,false);}
});
test('empty updates preserve revision but still enforce confirmation, tone and current version',()=>{
 const g=new SelectionGuide(),before=g.preview();assert.equal(update(g,{},'professional').revision,before.revision);assert.deepEqual(g.preview(),before);
 assert.throws(()=>g.update({expectedRevision:0,confirmed:false,patch:{}}));assert.throws(()=>update(g,{},'invented'));
 update(g,{budget:800});assert.throws(()=>g.update({expectedRevision:0,confirmed:true,patch:{}}),/Stale/);
});
