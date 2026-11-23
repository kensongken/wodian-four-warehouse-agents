import test from 'node:test';
import assert from 'node:assert/strict';
import {EvidenceLedger} from './contract.mjs';
const context = {kind:'inventory',storeId:'demo-store',customerId:'demo-customer',subjectId:'item-1'};
const record = {...context,id:'e1',version:1,state:'available',source:'inventory_snapshot',issuedAt:100,expiresAt:200,simulation:true};
const proposal = {state:'available',evidenceId:'e1',evidenceVersion:1};
const ledger = () => new EvidenceLedger({clock:()=>150});

test('missing evidence cannot become a positive or negative business state',()=>{
  for (const [kind, states] of Object.entries({order:['processing','completed'],inventory:['available','unavailable'],delivery_today:['confirmed','not_possible'],owner_presence:['at_store','away']})) {
    for (const state of states) assert.equal(ledger().reply({...context,kind},{state,evidenceId:'invented',evidenceVersion:1}).state,'unknown');
  }
});
test('valid matching evidence supports its exact state and preserves simulation label',()=>{
  const l=ledger();l.ingest(record);const result=l.reply(context,proposal);
  assert.equal(result.state,'available');assert.match(result.reply,/仅演练/);assert.equal(result.sent,false);
  assert.equal(l.reply(context,{...proposal,state:'unavailable'}).state,'unknown');
});
test('cross-store, cross-customer, cross-resource and cross-purpose evidence fail closed',()=>{
  const l=ledger();l.ingest(record);
  for(const patch of [{storeId:'other'},{customerId:'other'},{subjectId:'other'},{kind:'delivery_today'}]) {
    const p=patch.kind?{...proposal,state:'confirmed'}:proposal;
    assert.equal(l.reply({...context,...patch},p).state,'unknown');
  }
});
test('expiry, revocation and stale version prevent reuse',()=>{
  let now=150;const l=new EvidenceLedger({clock:()=>now});l.ingest(record);
  now=200;assert.equal(l.reply(context,proposal).state,'unknown');now=150;
  l.ingest({...record,version:2,state:'unavailable'});
  assert.equal(l.reply(context,proposal).state,'unknown');
  const current={state:'unavailable',evidenceId:'e1',evidenceVersion:2};
  assert.equal(l.reply(context,current).state,'unavailable');
  l.revoke('e1');assert.equal(l.reply(context,current).state,'unknown');
  assert.throws(()=>l.ingest(record),/conflict/);
});
test('model text, forged approval and actions cannot bypass renderer',()=>{
  const l=ledger();l.ingest(record);
  for(const patch of [{reply:'今天能拿到'},{approved:true},{action:'notify_owner'},{state:'processing'}]) {
    assert.equal(l.reply(context,{...proposal,...patch}).state,'unknown');
  }
  assert.throws(()=>l.reply(context,proposal,{tone:'我在店里等你'}),/tone/);
});
test('source type and immutable ledger prevent caller mutations',()=>{
  const l=ledger();assert.throws(()=>l.ingest({...record,source:'model_guess'}),/Invalid/);
  const copy={...record};l.ingest(copy);copy.state='unavailable';
  assert.equal(l.reply(context,proposal).state,'available');
  assert.throws(()=>l.ingest({...record,version:2,customerId:'other'}),/conflict/);
});
test('all selected tones preserve unknown and supported states without promising actions',()=>{
  const l=ledger();l.ingest(record);
  for(const tone of ['plain','warm','professional']) {
    assert.equal(l.reply(context,proposal,{tone}).state,'available');
    const unknown=l.reply(context,{state:'unavailable'},{tone});
    assert.equal(unknown.state,'unknown');assert.match(unknown.reply,/不能确认有货还是无货/);
  }
});
test('a newer record ID supersedes the old one, and revocation cannot revive older evidence',()=>{
  const l=ledger();l.ingest(record);
  l.ingest({...record,id:'e2',issuedAt:110,state:'unavailable'});
  assert.equal(l.reply(context,proposal).state,'unknown');
  l.revoke('e2');assert.equal(l.reply(context,proposal).state,'unknown');
  assert.throws(()=>l.ingest({...record,id:'e3',issuedAt:105}),/Older/);
});
