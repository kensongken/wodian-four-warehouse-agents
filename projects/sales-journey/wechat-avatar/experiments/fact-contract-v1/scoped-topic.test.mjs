import test from 'node:test';import assert from 'node:assert/strict';
import {scopedRequest,parseScoped} from './scoped-topic.mjs';
const raw=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
const base={topic:'selection',topicQuote:'给自己选',action:'change_request',actionQuote:'给自己选',utterance:'customer_request'};
test('local request change does not project into ending shopping',()=>{
 const r=parseScoped('给自己选',raw(base));assert.equal(r.projectedTopic,'selection');assert.equal(r.sent,false);assert.equal(r.profileChanged,false);assert.equal(r.requiresHumanReview,true);
});
test('rejects stale evidence, extra execution fields and contradictory meta output',()=>{
 assert.throws(()=>parseScoped('改看项链',raw(base)));
 assert.throws(()=>parseScoped('给自己选',raw({...base,send:true})));
 assert.throws(()=>parseScoped('给自己选',raw({...base,utterance:'response_discussion'})));
});
test('discussion and unresolved requests cannot map into business actions',()=>{
 for(const utterance of ['response_discussion','needs_clarification'])assert.equal(parseScoped('怎么接话',raw({topic:'unknown',topicQuote:null,action:'none',actionQuote:null,utterance})).projectedTopic,'unknown');
 assert.throws(()=>scopedRequest('test','x',Array(5).fill('old')));
});
