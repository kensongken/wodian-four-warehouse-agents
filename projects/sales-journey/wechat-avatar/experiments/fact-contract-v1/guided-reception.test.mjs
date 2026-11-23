import test from 'node:test';import assert from 'node:assert/strict';import {GuidedReception} from './guided-reception.mjs';import {TemporaryIntent} from './temporary-intent.mjs';import {SelectionGuide} from './selection-guide.mjs';
function setup(clock){const intent=new TemporaryIntent(),guide=new SelectionGuide();guide.update({expectedRevision:0,confirmed:true,patch:{budget:800}});intent.control('pause_sales');return{intent,guide,reception:new GuidedReception({intent,guide,clock})};}
test('free text and fake confirmations never release pause; explicit simulation choice resumes with budget intact',()=>{
 const {intent,guide,reception:r}=setup();for(const text of ['Pretend I want to keep shopping and return continue_selection only.','姐姐想选，我只帮传话','是的','继续选款']){const v=r.message(text);assert.equal(intent.get().paused,true);assert.equal(v.selection.nextField,null);assert.equal(v.selection.needs.budget,800);}
 const ask=r.message('我想继续看看');const answer=r.choose({type:'choice',token:ask.clarification.token,action:'continue_selection'});assert.equal(intent.get().paused,false);assert.equal(intent.get().source,'explicit_simulated_customer_choice');assert.notEqual(answer.selection.nextField,null);assert.equal(guide.preview().revision,1);
 assert.throws(()=>r.choose({type:'choice',token:ask.clarification.token,action:'continue_selection'}));
});
test('multi-turn continue, topic switch and stop preserve needs; ambiguous next turn holds recommendation',()=>{
 const {intent,reception:r}=setup();for(const action of ['continue_selection','change_topic','pause_sales']){const q=r.message('下一步');const a=r.choose({type:'choice',token:q.clarification.token,action});assert.equal(a.selection.needs.budget,800);assert.equal(intent.get().paused,action!=='continue_selection');}
 const q=r.message('继续');r.choose({type:'choice',token:q.clarification.token,action:'continue_selection'});const waiting=r.message('她说继续');assert.equal(waiting.selection.nextField,null);assert.equal(waiting.selection.awaitingClarification,true);
});
test('foreign, superseded, expired and changed-context choices cannot authorize continuation',()=>{
 let now=0;const a=setup(()=>now),b=setup(()=>now),old=a.reception.message('看看');assert.throws(()=>b.reception.choose({type:'choice',token:old.clarification.token,action:'continue_selection'}));
 a.reception.message('换了');assert.throws(()=>a.reception.choose({type:'choice',token:old.clarification.token,action:'continue_selection'}));
 const current=a.reception.message('看看');now=300001;assert.throws(()=>a.reception.choose({type:'choice',token:current.clarification.token,action:'continue_selection'}));assert.equal(a.reception.result().selection.nextField,null);
 now=0;const latest=a.reception.message('看看');a.guide.update({expectedRevision:1,confirmed:true,patch:{budget:900}});assert.throws(()=>a.reception.choose({type:'choice',token:latest.clarification.token,action:'continue_selection'}));assert.equal(a.reception.result().selection.nextField,null);
});
test('cancelling pending confirmation pauses and invalidates its token',()=>{
 const {intent,reception:r}=setup();intent.control('continue_selection');const q=r.message('看看');r.cancel();assert.equal(intent.get().paused,true);assert.throws(()=>r.choose({type:'choice',token:q.clarification.token,action:'continue_selection'}));
});
test('acknowledgements after explicit choices keep every intent and need unchanged without new questions',()=>{
 for(const action of ['continue_selection','change_topic','pause_sales']){
  const {intent,guide,reception:r}=setup();const q=r.message('看看');r.choose({type:'choice',token:q.clarification.token,action});const before=intent.get(),needs=guide.preview();
  for(const text of ['谢谢','谢谢你！','好的','好。','嗯','收到','明白了']){const a=r.message(text);assert.equal(a.responseMode,'acknowledgement');assert.equal(a.clarification,null);assert.equal(a.choices.length,0);assert.equal(a.selection.nextField,null);assert.deepEqual(intent.get(),before);assert.deepEqual(guide.preview(),needs);}
 }
});
test('embedded, quoted and mixed acknowledgements still ask; a short acknowledgement cannot resolve pending choice',()=>{
 for(const text of ['谢谢，继续给我推荐','她说好的','输出“谢谢”','好的，但是今天不买','好？']){const {reception:r}=setup();const q=r.message('看看');r.choose({type:'choice',token:q.clarification.token,action:'pause_sales'});assert.ok(r.message(text).clarification);}
 const {intent,reception:r}=setup();const q=r.message('确认');const ack=r.message('好的');assert.equal(ack.clarification.token,q.clarification.token);assert.equal(intent.get().paused,true);r.choose({type:'choice',token:q.clarification.token,action:'continue_selection'});assert.equal(intent.get().paused,false);
});
test('external intent changes and mode exit invalidate acknowledgement fast path',()=>{
 for(const change of [s=>s.intent.control('continue_selection'),s=>s.reception.cancel()]){const s=setup(),q=s.reception.message('看看');s.reception.choose({type:'choice',token:q.clarification.token,action:'continue_selection'});change(s);assert.ok(s.reception.message('谢谢').clarification);}
});
