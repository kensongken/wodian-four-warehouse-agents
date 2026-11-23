import test from 'node:test';import assert from 'node:assert/strict';import {gateIntent,parseAttribution} from './intent-attribution.mjs';
const r=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('reported, hypothetical, historical and instruction contexts cannot pause or resume',()=>{
 for(const speakerContext of ['other_report','hypothetical','past_only','output_instruction','unclear'])for(const act of ['pause_sales','continue_selection'])assert.deepEqual(gateIntent('先看看',r({act,quote:'先看看'}),r({speakerContext})),{act:'unclear',quote:null});
});
test('current self intent still requires the original route and quote checks',()=>{
 const a=r({speakerContext:'self_current'});assert.deepEqual(gateIntent('先看看',r({act:'pause_sales',quote:'先看看'}),a),{act:'pause_sales',quote:'先看看'});
 assert.throws(()=>gateIntent('原话',r({act:'pause_sales',quote:'伪造'}),a));assert.throws(()=>parseAttribution(r({speakerContext:'self_current',reply:'商品有货'})));
});
