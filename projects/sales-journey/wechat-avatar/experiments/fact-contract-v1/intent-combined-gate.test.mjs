import test from 'node:test';import assert from 'node:assert/strict';import {gateCombined} from './intent-combined-gate.mjs';import {TemporaryIntent} from './temporary-intent.mjs';
const r=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('either veto preserves existing pause and both valid approvals allow actual continuation',()=>{
 for(const speakerContext of ['self_current','other_report'])for(const target of ['human_conversation_choice','model_output_control']){
  const s=new TemporaryIntent();s.control('pause_sales');const result=gateCombined('继续看项链',r({act:'continue_selection',quote:'继续看项链'}),r({speakerContext}),r({target,choiceQuote:target==='human_conversation_choice'?'继续看项链':null}));s.apply(result,'继续看项链');
  assert.equal(s.get().paused,!(speakerContext==='self_current'&&target==='human_conversation_choice'));
 }
});
test('a malformed required approval cannot release pause',()=>{
 assert.throws(()=>gateCombined('继续看',r({act:'continue_selection',quote:'继续看'}),r({speakerContext:'self_current'}),r({target:'human_conversation_choice',choiceQuote:'别人的话'})));
});
