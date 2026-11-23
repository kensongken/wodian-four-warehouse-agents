import test from 'node:test';import assert from 'node:assert/strict';import {notebookRequest} from './conversation-notebook.mjs';import {cases} from './conversation-notebook-cases.mjs';
const request={model:'test',messages:[{role:'system',content:'base'},{role:'user',content:'current'}]};
test('every frozen oracle note has a quote from current or earlier customer statements, never future turns',()=>{
 for(const c of cases){const statements=[];for(const t of c.turns){statements.push(t.message);const r=notebookRequest(request,t.card,statements);assert.equal(r.messages[0].content,'base');assert.equal(r.messages.at(-1).content,'current');assert.match(r.messages.at(-2).content,/不是自动理解结果/);}}
 assert.equal(cases.length,5);assert.equal(cases.reduce((n,c)=>n+c.turns.length,0),20);
});
test('notebook rejects fabricated sources, extra keys and oversized context without mutating baseline request',()=>{
 const before=structuredClone(request);assert.throws(()=>notebookRequest(request,{goal:'测试',notes:[{meaning:'假设',quote:'未来消息'}]},['现在消息']));
 assert.throws(()=>notebookRequest(request,{goal:'测试',notes:[],autoConfirmed:true},[]));
 assert.throws(()=>notebookRequest({...request,messages:[{role:'system',content:'x'.repeat(7000)},{role:'user',content:'last'}]},{goal:'测试',notes:[]},[]));assert.deepEqual(request,before);
});
