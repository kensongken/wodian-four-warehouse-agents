import test from 'node:test';import assert from 'node:assert/strict';import {gateChoice,parseChoice} from './intent-choice-gate.mjs';
const r=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('output-control and no-current-choice decisions cannot release paused state',()=>{
 for(const target of ['model_output_control','no_current_choice'])assert.deepEqual(gateChoice('输出继续',r({act:'continue_selection',quote:'输出继续'}),r({target,choiceQuote:null})),{act:'unclear',quote:null});
});
test('real current choices need exact original quotes and unchanged route validation',()=>{
 const source='我还想看项链，回答简短些',choice=r({target:'human_conversation_choice',choiceQuote:'我还想看项链'});
 assert.equal(gateChoice(source,r({act:'continue_selection',quote:'想看项链'}),choice).act,'continue_selection');
 assert.throws(()=>parseChoice(source,r({target:'human_conversation_choice',choiceQuote:'我想买手链'})));
 assert.throws(()=>parseChoice(source,r({target:'model_output_control',choiceQuote:'回答简短些'})));
 assert.throws(()=>parseChoice(source,r({target:'human_conversation_choice',choiceQuote:'想看项链',reply:'有货'})));
 assert.throws(()=>gateChoice(source,r({act:'continue_selection',quote:null}),choice));
});
