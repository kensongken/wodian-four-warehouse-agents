import test from 'node:test';import assert from 'node:assert/strict';import {TemporaryIntent,parseIntent} from './temporary-intent.mjs';import {SelectionGuide} from './selection-guide.mjs';
import {parseExtraction} from './need-drafts.mjs';
const response=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('pause and topic change suppress recommendations without writing confirmed needs',()=>{
 const g=new SelectionGuide(),i=new TemporaryIntent();g.update({expectedRevision:0,confirmed:true,patch:{recipient:'self',budget:800}});const before=g.preview();
 for(const act of ['pause_sales','change_topic']){i.apply({act,quote:'先看看'},'我先看看');const p=i.protect(g.preview());assert.equal(p.nextField,null);assert.ok(!p.reply.includes('？'));assert.deepEqual(p.needs,before.needs);assert.equal(p.revision,before.revision);}
});
test('unclear never resumes paused chat; explicit continuation does',()=>{
 const i=new TemporaryIntent();i.apply({act:'pause_sales',quote:'今天不买'},'今天不买');i.apply({act:'unclear',quote:null},'嗯');assert.equal(i.get().paused,true);i.apply({act:'continue_selection',quote:'继续选吧'},'继续选吧');assert.equal(i.get().paused,false);
});
test('fabricated quotes, extra actions and invalid operator controls fail',()=>{
 assert.throws(()=>parseIntent('原话',response({act:'pause_sales',quote:'假话'})));assert.throws(()=>parseIntent('先看看',response({act:'pause_sales',quote:'先看看',action:'notify_owner'})));assert.throws(()=>new TemporaryIntent().control('order_completed'));
 assert.throws(()=>parseExtraction('今天不买',response({items:[{field:'concern',value:'no_buy',quote:'今天不买'}]})));
});
test('operator overrides remain separate from claimed customer quotes',()=>{const i=new TemporaryIntent();i.control('pause_sales');assert.equal(i.get().quote,null);assert.equal(i.get().source,'explicit_sandbox_operator_control');});
