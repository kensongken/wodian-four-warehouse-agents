import test from 'node:test';
import assert from 'node:assert/strict';
import {recoverOwner} from '../recover-owner.mjs';
import {passwordMatches} from '../lib/engine.mjs';
function fixture(owners=1){
  const state={users:Array.from({length:owners},(_,i)=>({id:'owner-'+i,username:'test-owner',role:'owner',active:true,password:'old',mustChangePassword:true})),sessions:[{userId:'owner-0'},{userId:'employee'}],settings:{paused:false,mode:'controlled',policyVersion:3},tasks:[{status:'queued'},{status:'executing'},{status:'observed_sent'}],audit:[],customers:[{id:'preserve'}],messages:[{text:'preserve'}]};
  return {state,store:{transact:fn=>{const copy=structuredClone(state);const result=fn(copy);Object.assign(state,copy);return result;}}};
}
test('owner recovery preserves business data and pauses all new delivery',()=>{
  const {store,state}=fixture();const result=recoverOwner(store,'test-password-only');
  assert.equal(result.username,'test-owner');assert.ok(passwordMatches('test-password-only',state.users[0].password));
  assert.deepEqual(state.sessions,[{userId:'employee'}]);assert.equal(state.settings.paused,true);assert.equal(state.settings.mode,'assist');
  assert.deepEqual(state.tasks.map(t=>t.status),['canceled','unknown','observed_sent']);
  assert.deepEqual(state.customers,[{id:'preserve'}]);assert.deepEqual(state.messages,[{text:'preserve'}]);
  assert.doesNotMatch(JSON.stringify(state.audit),/test-password-only/);
});
test('ambiguous owners and weak passwords do not change data',()=>{
  for(const n of [0,2]){const {store,state}=fixture(n);const before=structuredClone(state);assert.throws(()=>recoverOwner(store,'test-password-only'));assert.deepEqual(state,before);}
  const {store,state}=fixture();const before=structuredClone(state);assert.throws(()=>recoverOwner(store,'short'));assert.deepEqual(state,before);
});
