import test from 'node:test';import assert from 'node:assert/strict';import {EditRetry} from './edit-retry.mjs';import {SelectionGuide} from './selection-guide.mjs';
test('retry permits are single use, scoped, time limited and do not change needs',()=>{
 let now=0;const guide=new SelectionGuide(),retry=new EditRetry({guide,assertContext:()=>{},clock:()=>now}),other=new EditRetry({guide,assertContext:()=>{},clock:()=>now});const p=retry.issue();assert.throws(()=>other.consume(p.token));retry.consume(p.token);assert.throws(()=>retry.consume(p.token));assert.equal(guide.preview().revision,0);const expired=retry.issue();now=300001;assert.throws(()=>retry.consume(expired.token));
});
test('changed needs, cleared permit or revoked edit context cannot retry',()=>{
 let valid=true;const guide=new SelectionGuide(),retry=new EditRetry({guide,assertContext:()=>{if(!valid)throw Error('revoked');}});let p=retry.issue();guide.update({expectedRevision:0,confirmed:true,patch:{budget:800}});assert.throws(()=>retry.consume(p.token));p=retry.issue();retry.clear();assert.throws(()=>retry.consume(p.token));p=retry.issue();valid=false;assert.throws(()=>retry.consume(p.token));assert.throws(()=>retry.issue());
});
