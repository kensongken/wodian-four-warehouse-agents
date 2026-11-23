import test from 'node:test';import assert from 'node:assert/strict';import {conversationContext} from '../lib/conversation-notes.mjs';import {conversationNotesView} from '../web/persona-studio.js';
const chain=n=>Array.from({length:n},(_,i)=>({id:'t'+i,profileId:'p',question:i===0?'我自己戴，想要细一点的。':'客户原话'+i,reply:'模型回复不得当需求'+i,previousTrialId:i?'t'+(i-1):null}));
test('bounded older quotes retain wearer/category evidence through small talk without inventing slot values',()=>{
 const trials=chain(10);trials[0].question='我想给自己选条上班戴的手链，简单一点。';trials[5].question='先不看上班的了，周末出去玩想显眼一点。';const result=conversationContext(trials,'p','t9');assert.equal(result.notes.length,4);assert.ok(result.notes.some(n=>n.trialId==='t0'));assert.ok(result.notes.some(n=>n.trialId==='t5'));assert.ok(result.notes.findIndex(n=>n.trialId==='t0')<result.notes.findIndex(n=>n.trialId==='t5'));assert.ok(result.notes.every(n=>Object.keys(n).join(',')==='trialId,question'));assert.ok(result.olderSkipped>0);assert.ok(Buffer.byteLength(JSON.stringify(result.notes),'utf8')<=900);
});
test('later explicit wearer and category quotes remain available, while questions do not displace their evidence',()=>{
 const trials=chain(10);trials[0].question='我自己戴手链。';trials[3].question='换成送给妈妈的项链，日常戴。';trials[5].question='你说的是自己戴手链吗？';const result=conversationContext(trials,'p','t9');assert.ok(result.notes.some(n=>n.trialId==='t3'));assert.ok(!result.notes.some(n=>n.trialId==='t0'));assert.ok(!JSON.stringify(result.notes).includes('模型回复'));const source=result.notes.find(n=>n.trialId==='t3');assert.equal(source.question,trials[3].question);
});
test('earlier notes preserve whole customer utterances with source IDs while full dialogue stays at three turns',()=>{
 const trials=chain(7),r=conversationContext(trials,'p','t6');assert.equal(r.history.length,3);assert.equal(r.notes.length,4);assert.equal(r.notes[0].trialId,'t0');assert.equal(r.notes[0].question,trials[0].question);assert.ok(!JSON.stringify(r.notes).includes('模型回复'));assert.equal(r.olderSkipped,0);assert.equal(r.chainLimitReached,false);
});
test('new conversation, unrelated branches and old profiles cannot supply inferred customer notes',()=>{
 const trials=chain(6);trials.push({id:'other',profileId:'p',question:'另一个分支的需求',reply:'好'});assert.deepEqual(conversationContext(trials,'p',null).notes,[]);assert.ok(!JSON.stringify(conversationContext(trials,'p','t5')).includes('另一个分支'));assert.throws(()=>conversationContext(trials,'new-profile','t5'),/口吻已更新/);assert.throws(()=>conversationContext(trials,'p',{}),/有效/);
});
test('older unavailable records stop collection without reviving blocked text; recent unavailable records reject continuation',()=>{
 const trials=chain(7);trials[2].blocked=true;const r=conversationContext(trials,'p','t6');assert.equal(r.olderUnavailable,true);assert.deepEqual(r.notes.map(n=>n.trialId),['t3']);assert.ok(!r.notes.some(n=>n.trialId==='t2'));trials[5].blocked=true;assert.throws(()=>conversationContext(trials,'p','t6'),/被拦截/);
});
test('collection is bounded, cycles reject, and notes skip long whole utterances instead of silently truncating',()=>{
 const trials=chain(14);const limited=conversationContext(trials,'p','t13');assert.equal(limited.chainLimitReached,true);assert.ok(limited.notes.length<=4);assert.ok(limited.olderSkipped>0);assert.ok(Buffer.byteLength(JSON.stringify(limited.notes),'utf8')<=900);
 const long=chain(5);long[0].question='长'.repeat(400);const r=conversationContext(long,'p','t4');assert.equal(r.olderSkipped,1);assert.ok(!r.notes.some(n=>n.trialId==='t0'));long[3].previousTrialId='t4';assert.throws(()=>conversationContext(long,'p','t4'),/关联异常/);
});
test('notes UI escapes original speech and labels incomplete history without implying permanent customer memory',()=>{
 const html=conversationNotesView({conversationNotes:{notes:[{trialId:'x',question:'<script>bad()</script>'}],olderSkipped:1}});assert.ok(html.includes('不是完整历史'));assert.ok(html.includes('未推断成永久客户档案'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>bad()'));assert.equal(conversationNotesView({}),'');
});
