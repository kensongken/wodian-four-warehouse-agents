import test from 'node:test';import assert from 'node:assert/strict';import {NaturalReception} from './natural-reception.mjs';import {createSandbox} from './sandbox-server.mjs';
const response=reply=>({choices:[{message:{content:JSON.stringify({reply})}}]});
test('natural preview receives full need and recent turns without confirmation buttons or profile writes',async()=>{
 const requests=[],n=new NaturalReception({model:'test',complete:async r=>{requests.push(r);return response(requests.length===1?'送妈妈的话，简洁的方向挺合适。她平时更常戴项链还是手链？':'那先不看项链，手链也可以。她平时喜欢贴手一点，还是松一点？');}});
 const first=await n.turn({message:'送妈妈生日，预算1500，想简洁一点。',tone:'warm'});assert.equal(first.profileChanged,false);assert.equal(first.sent,false);assert.equal(first.businessFactsVerified,false);assert.equal(first.ownerVoiceVerified,false);assert(!first.choices);
 await n.turn({message:'她不喜欢项链，看看手链。'});const last=requests[1].messages;assert(last.some(m=>m.content.includes('送妈妈生日')));assert(last.some(m=>m.role==='assistant'&&m.content===first.reply));assert(last.at(-1).content.includes('不喜欢项链'));assert.match(last[0].content,/热情直接、少催买/);
});
test('invalid output or failed inference never enters conversation history, and clear removes prior turns',async()=>{
 let bad=false;const requests=[],n=new NaturalReception({model:'test',complete:async r=>{requests.push(r);return bad?{choices:[{message:{content:'{"reply":"承诺","action":"send"}'}}]}:response('好，先聊你拿不定的。');}});
 await n.turn({message:'原始消息'});bad=true;await assert.rejects(n.turn({message:'失败消息'}));bad=false;await n.turn({message:'继续'});assert(!requests.at(-1).messages.slice(0,-1).some(m=>m.content.includes('失败消息')));
 n.clear();await n.turn({message:'新开始'});assert.equal(requests.at(-1).messages.length,2);
});
test('history bound preserves complete instructions and newest input; validation rejects unknown style and oversized messages',async()=>{
 const requests=[],n=new NaturalReception({model:'test',complete:async r=>{requests.push(r);return response('回复'.repeat(100));}});
 for(let i=0;i<8;i++)await n.turn({message:`第${i}条`+'需求'.repeat(300)});const last=requests.at(-1);assert(Buffer.byteLength(JSON.stringify(last.messages))<=6800);assert.equal(last.messages[0].role,'system');assert(last.messages.at(-1).content.includes('第7条'));
 await assert.rejects(n.turn({message:'x'.repeat(801)}));await assert.rejects(n.turn({message:'看看',tone:'forged'}));
});
test('store FAQ with no approved facts uses unknown response rather than invented model hours',async()=>{
 let calls=0;const n=new NaturalReception({model:'test',complete:async()=>{calls++;return response('我们18:00关门。');}});
 const r=await n.turn({message:'你们店几点关门，明天能过来吗？'});assert.equal(calls,0);assert.equal(r.modelCalls,0);assert.equal(r.responseMode,'natural_fact_unknown');assert.match(r.reply,/没有核准/);assert(!r.reply.includes('18:00'));
 await n.turn({message:'我有一条和田玉，想要改款的'});assert.equal(calls,1);
});
test('HTTP natural mode is session-isolated, busy-protected, leaves confirmed needs untouched, clears history on resume',async t=>{
 const requests=[];let release;const app=createSandbox({completeConversation:async r=>{requests.push(r);if(requests.length===1)return new Promise(yes=>release=()=>yes(response('先接住需求。')));return response('接着说。');}});await new Promise(r=>app.server.listen(0,'127.0.0.1',r));t.after(()=>app.close());
 const origin=`http://127.0.0.1:${app.server.address().port}`,post=async(path,body)=>{const r=await fetch(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});return{status:r.status,data:await r.json()};};
 const a=(await post('/api/session',{})).data.sessionId,b=(await post('/api/session',{})).data.sessionId;
 const pending=post('/api/natural-chat',{sessionId:a,message:'预算1500，送妈妈。'});while(!release)await new Promise(r=>setTimeout(r,1));assert.equal((await post('/api/session-resume',{sessionId:a})).status,409);release();assert.equal((await pending).status,200);
 assert.equal((await post('/api/service-preview',{sessionId:a})).data.needs.budget,null);await post('/api/natural-chat',{sessionId:b,message:'自己戴。'});assert.equal(requests[1].messages.length,2);
 await post('/api/session-resume',{sessionId:a});await post('/api/natural-chat',{sessionId:a,message:'重新聊。'});assert.equal(requests[2].messages.length,2);
 const before=requests.length;assert.equal((await post('/api/natural-chat',{sessionId:a,message:'看看',patch:{budget:1}})).status,400);assert.equal(requests.length,before);
 assert.equal((await post('/api/natural-clear',{sessionId:a})).status,200);await post('/api/natural-chat',{sessionId:a,message:'改款重新聊。'});assert.equal(requests.at(-1).messages.length,2);
});
