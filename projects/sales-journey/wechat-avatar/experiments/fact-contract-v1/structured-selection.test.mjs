import test from 'node:test';import assert from 'node:assert/strict';import {SelectionGuide} from './selection-guide.mjs';import {TemporaryIntent} from './temporary-intent.mjs';import {GuidedReception} from './guided-reception.mjs';import {StructuredSelection} from './structured-selection.mjs';
function setup(clock){const guide=new SelectionGuide(),intent=new TemporaryIntent(),reception=new GuidedReception({guide,intent,clock}),flow=new StructuredSelection({guide,intent,reception,clock});return{guide,intent,flow};}
function start(flow){const q=flow.message('想看看');return flow.chooseIntent({type:'choice',token:q.clarification.token,action:'continue_selection'});}
function pick(flow,q,value){return flow.chooseNeed({type:'need_choice',token:q.clarification.token,field:q.choices[0].field,value});}
test('five explicit customer choices build needs one field at a time, with no model or product claim',()=>{
 const {guide,flow}=setup();let q=start(flow);for(const [field,value]of [['recipient','mother'],['category','necklace'],['occasion','birthday'],['budget',1500],['style','simple']]){assert.equal(q.choices[0].field,field);q=pick(flow,q,value);assert.equal(q.needChoiceReceipt.source,'explicit_simulated_customer_selection');}
 assert.equal(q.clarification,null);assert.equal(q.selection.productMatched,false);assert.equal(q.selection.ownerVoiceLearned,false);assert.match(q.reply,/1500/);assert.deepEqual(guide.preview().needs,{recipient:'mother',category:'necklace',occasion:'birthday',budget:1500,style:'simple',concern:'none'});
});
test('known budget is not asked again; private budget and unsure style are accepted without loops',()=>{
 for(const budget of [800,'private']){const {guide,flow}=setup();guide.update({expectedRevision:0,confirmed:true,patch:{budget}});let q=start(flow);for(const v of ['self','ring','work','unsure'])q=pick(flow,q,v);assert.equal(q.clarification,null);assert.equal(q.selection.needs.budget,budget);}
});
test('pending needs accept acknowledgements without replacing token, and explicit interruptions preserve needs',()=>{
 const {flow,intent}=setup();let q=start(flow);q=pick(flow,q,'mother');const before=q.selection.needs;const ack=flow.message('谢谢');assert.equal(ack.clarification.token,q.clarification.token);
 const stop=flow.interrupt({type:'service_interrupt',token:q.clarification.token,action:'pause_sales'});assert.equal(intent.get().paused,true);assert.deepEqual(stop.selection.needs,before);assert.throws(()=>pick(flow,q,'necklace'));const next=start(flow);assert.equal(next.choices[0].field,'category');
});
test('wrong field, wrong value, foreign, replayed, expired and revised questions cannot write needs',()=>{
 let now=0;const a=setup(()=>now),b=setup(()=>now),q=start(a.flow),before=a.guide.preview();
 assert.throws(()=>a.flow.chooseNeed({type:'need_choice',token:q.clarification.token,field:'budget',value:1}));assert.throws(()=>pick(a.flow,q,'inventory_available'));assert.throws(()=>pick(b.flow,q,'self'));assert.deepEqual(a.guide.preview(),before);
 now=300001;assert.throws(()=>pick(a.flow,q,'self'));now=0;const r=start(a.flow);a.guide.update({expectedRevision:0,confirmed:true,patch:{budget:800}});assert.throws(()=>pick(a.flow,r,'self'));
 const latest=start(a.flow);pick(a.flow,latest,'self');assert.throws(()=>pick(a.flow,latest,'mother'));
});
test('free text never silently fills a pending preference and cancelled questions cannot be reused',()=>{
 const {flow,guide,intent}=setup(),q=start(flow);flow.message('客户说送妈妈');assert.equal(guide.preview().needs.recipient,null);assert.throws(()=>pick(flow,q,'mother'));const next=start(flow);flow.cancel();assert.equal(intent.get().paused,true);assert.throws(()=>pick(flow,next,'self'));
});
function finish(flow){let q=start(flow);for(const value of ['mother','necklace','birthday',1500,'simple'])q=pick(flow,q,value);return q;}
test('recipient revision retains other conditions and asks new wearer style instead of carrying old preference',()=>{
 const {flow,guide}=setup(),done=finish(flow),before=guide.preview();const edit=flow.editStart({type:'edit_start',token:done.review.token,field:'recipient'});assert.equal(guide.preview().revision,before.revision);assert.match(edit.reply,/自己选/);
 const changed=pick(flow,edit,'self');assert.equal(changed.selection.needs.style,null);assert.equal(changed.choices[0].field,'style');assert.equal(changed.selection.needs.budget,1500);assert.equal(changed.selection.needs.category,'necklace');assert.equal(changed.selection.needs.occasion,'birthday');assert.match(changed.reply,/暂沿用/);
 const final=pick(flow,changed,'bold');assert.equal(final.clarification,null);assert.equal(final.selection.needs.recipient,'self');assert.equal(final.selection.needs.style,'bold');
});
test('editing one field is explicit and needs neither repeated questions nor loss of other values',()=>{
 const {flow,guide}=setup(),done=finish(flow),before=guide.preview();flow.setTone('professional');const edit=flow.editStart({type:'edit_start',token:done.review.token,field:'budget'});assert.equal(edit.selection.nextField,'budget');assert.match(edit.reply,/预算上限/);assert.ok(edit.reply.startsWith('我们先把选择方向理清。'));assert.deepEqual(guide.preview().needs,before.needs);
 const changed=pick(flow,edit,500);assert.equal(changed.clarification,null);assert.deepEqual(changed.selection.needs,{...before.needs,budget:500});assert.equal(changed.needChoiceReceipt.previousValue,1500);assert.ok(changed.review);
});
test('review tokens reject duplicate, foreign, expired and changed-revision edit requests',()=>{
 let now=0;const a=setup(()=>now),b=setup(()=>now),done=finish(a.flow);finish(b.flow);const event={type:'edit_start',token:done.review.token,field:'style'};
 assert.throws(()=>b.flow.editStart(event));assert.throws(()=>a.flow.editStart({...event,field:'inventory'}));assert.throws(()=>a.flow.editStart({...event,value:'bold'}));
 now=300001;assert.throws(()=>a.flow.editStart(event));now=0;a.flow.editStart(event);assert.throws(()=>a.flow.editStart(event));
 const c=setup(),ready=finish(c.flow);c.guide.update({expectedRevision:c.guide.preview().revision,confirmed:true,patch:{budget:1000}});assert.throws(()=>c.flow.editStart({type:'edit_start',token:ready.review.token,field:'style'}));
});
test('thanks keeps revision actions available; stop or a new statement invalidates old review',()=>{
 const {flow,intent}=setup(),done=finish(flow),ack=flow.message('谢谢');assert.equal(ack.responseMode,'acknowledgement');assert.equal(ack.review.token,done.review.token);assert.ok(ack.choices.some(c=>c.kind==='edit'));
 flow.interrupt({type:'service_interrupt',token:done.review.token,action:'pause_sales'});assert.equal(intent.get().paused,true);assert.throws(()=>flow.editStart({type:'edit_start',token:done.review.token,field:'budget'}));
 const again=start(flow);flow.message('我改主意了');assert.throws(()=>flow.editStart({type:'edit_start',token:again.review.token,field:'budget'}));
});
test('fit and comparison concerns use known wearer/style without rewriting preferences or intent',()=>{
 for(const concern of ['fit','compare']){const {flow,guide,intent}=setup(),done=finish(flow),before=guide.preview(),state=intent.get();const response=flow.support({type:'support_choice',token:done.review.token,concern});assert.equal(response.responseMode,'service_support');assert.equal(response.supportReceipt.needsChanged,false);assert.deepEqual(guide.preview(),before);assert.deepEqual(intent.get(),state);assert.match(response.reply,/简洁/);if(concern==='fit')assert.match(response.reply,/妈妈/);assert.doesNotMatch(response.reply,/保证|肯定适合|免费|有货|马上买/);assert.throws(()=>flow.support({type:'support_choice',token:done.review.token,concern}));}
});
test('consideration pauses with explicit customer source and preserves every shopping condition',()=>{
 const {flow,guide,intent}=setup(),done=finish(flow),before=guide.preview();const r=flow.support({type:'support_choice',token:done.review.token,concern:'consider'});assert.equal(intent.get().paused,true);assert.equal(intent.get().source,'explicit_simulated_customer_choice');assert.match(r.reply,/不用现在决定/);assert.deepEqual(guide.preview(),before);assert.equal(r.review,undefined);assert.throws(()=>flow.editStart({type:'edit_start',token:done.review.token,field:'budget'}));
});
test('concern guards reject foreign, expired, stale and arbitrary data without modifying state',()=>{
 let now=0;const a=setup(()=>now),b=setup(()=>now),done=finish(a.flow);finish(b.flow);const e={type:'support_choice',token:done.review.token,concern:'fit'},before=a.guide.preview();assert.throws(()=>b.flow.support(e));assert.throws(()=>a.flow.support({...e,concern:'guaranteed_fit'}));assert.throws(()=>a.flow.support({...e,style:'bold'}));now=300001;assert.throws(()=>a.flow.support(e));assert.deepEqual(a.guide.preview(),before);
});
test('support tone stays consistent and editing clears the transient concern without adding a customer label',()=>{
 const {flow,guide}=setup(),done=finish(flow);flow.setTone('professional');const supported=flow.support({type:'support_choice',token:done.review.token,concern:'fit'});assert.ok(supported.reply.startsWith('我们先把顾虑理清。'));const edit=flow.editStart({type:'edit_start',token:supported.review.token,field:'style'});assert.match(edit.reply,/妈妈平时/);assert.doesNotMatch(edit.reply,/担心不合适很正常/);assert.equal(guide.preview().needs.concern,'none');
});
