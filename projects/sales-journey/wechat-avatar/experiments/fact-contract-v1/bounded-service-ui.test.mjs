import test from 'node:test';import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';import vm from 'node:vm';
function fixture(path=''){const ids=['run','scenario','message','status','reply','receipt','suggest','proposal','use-proposal','page-title'];const nodes=Object.fromEntries(ids.map(id=>[id,{value:'',textContent:'',hidden:id==='use-proposal',disabled:false}]));const calls=[];let suggestion={label:'维修补配',quote:'想修',suggestedScenario:'repair_general'};let fail=false;
 vm.runInNewContext(readFileSync(new URL('./bounded-service-ui.mjs',import.meta.url),'utf8'),{location:{pathname:path},document:{getElementById:id=>nodes[id],querySelectorAll:()=>Object.values(nodes)},fetch:async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});return{ok:!fail,json:async()=>fail?{error:'controlled error'}:path.includes('topic')?suggestion:{reply:'固定维修参考',choiceAccepted:true}};}});
 return{nodes,calls,setSuggestion:v=>suggestion=v,setFail:v=>fail=v};}
async function render(f){const n=f.nodes;n.message.value='想修';await n.suggest.onclick();n['use-proposal'].onclick();await n.run.onclick();assert.equal(n.reply.textContent,'固定维修参考');}
test('editing an adopted message clears old proposal, scene, rendered reply, success and receipt without any automatic call',async()=>{
 const f=fixture();await render(f);const n=f.nodes,count=f.calls.length;n.message.value='不修了想退换';n.message.oninput();for(const id of ['proposal','reply','status','receipt'])assert.equal(n[id].textContent,'');assert.equal(n.scenario.value,'');assert.equal(n['use-proposal'].hidden,true);assert.equal(f.calls.length,count);await n.run.onclick();assert.equal(f.calls.length,count);
});
test('new suggestion failure or ambiguity cannot reuse old scene or output; manual scene edits also clear old output',async()=>{
 const f=fixture();await render(f);const n=f.nodes;n.scenario.value='recycle_unknown';n.scenario.onchange();assert.equal(n.reply.textContent,'');
 await render(f);f.setFail(true);await n.suggest.onclick();assert.equal(n.scenario.value,'');assert.equal(n.reply.textContent,'');assert.equal(n.receipt.textContent,'');assert.equal(n['use-proposal'].hidden,true);assert.equal(n.proposal.textContent,'controlled error');
 f.setFail(false);f.setSuggestion({label:'需澄清',quote:null,suggestedScenario:null});await n.suggest.onclick();assert.equal(n['use-proposal'].hidden,true);assert.equal(n.scenario.value,'');assert.equal(n.run.disabled,false);
});

test('two-step page uses its own endpoint and keeps meta proposals out of business adoption',async()=>{
 const f=fixture('/two-step'),n=f.nodes;n.message.value='演练接话';f.setSuggestion({label:'讨论接话方式',quote:null,suggestedScenario:null});await n.suggest.onclick();
 assert.equal(f.calls[0].path,'/api/two-step-topic');assert.equal(n['use-proposal'].hidden,true);assert.equal(n.scenario.value,'');assert.match(n['page-title'].textContent,/两步理解/);
});
