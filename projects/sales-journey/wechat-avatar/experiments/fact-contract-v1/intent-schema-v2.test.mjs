import test from 'node:test';import assert from 'node:assert/strict';import {INTENT_SCHEMA_V2} from './intent-schema-v2.mjs';import {parseIntent,TemporaryIntent} from './temporary-intent.mjs';
const response=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('schema separates definite intent with a required string from unclear with null',()=>{
 const [definite,unclear]=INTENT_SCHEMA_V2.oneOf;
 assert.deepEqual(definite.required,['act','quote']);assert.equal(definite.additionalProperties,false);assert.equal(definite.properties.quote.type,'string');assert.equal(definite.properties.quote.minLength,1);assert.ok(!definite.properties.act.enum.includes('unclear'));
 assert.equal(unclear.properties.act.const,'unclear');assert.equal(unclear.properties.quote.type,'null');assert.equal(unclear.additionalProperties,false);
});
test('grammar does not relax exact source checks or establish semantic correctness',()=>{
 assert.throws(()=>parseIntent('先别推荐',response({act:'pause_sales',quote:'我不买'})));
 assert.throws(()=>parseIntent('先别推荐',response({act:'pause_sales',quote:null})));
 const wrong=parseIntent('她说今天不买',response({act:'pause_sales',quote:'今天不买'}));assert.equal(wrong.act,'pause_sales');
 const state=new TemporaryIntent();state.control('pause_sales');state.apply({act:'unclear',quote:null},'谢谢');assert.equal(state.get().paused,true);
});
