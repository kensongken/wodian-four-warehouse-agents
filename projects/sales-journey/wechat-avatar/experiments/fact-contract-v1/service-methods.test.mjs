import test from 'node:test';import assert from 'node:assert/strict';import {SERVICE_METHODS,serviceMethodRequest} from './service-methods.mjs';
test('all four draft methods expose bounded steps, no approval or merchant facts; requests keep original messages intact',()=>{
 const request={model:'test',messages:[{role:'system',content:'base'},{role:'user',content:'current'}]},before=structuredClone(request);
 assert.equal(Object.keys(SERVICE_METHODS).length,4);
 for(const[id,m]of Object.entries(SERVICE_METHODS)){assert.equal(m.status,'draft');for(const step of Object.keys(m.steps)){const r=serviceMethodRequest(request,id,step);assert.deepEqual(r.messages[0],request.messages[0]);assert.deepEqual(r.messages.at(-1),request.messages.at(-1));assert.match(r.messages.at(-2).content,/不证明门店实际能力/);}}
 assert.deepEqual(request,before);assert.throws(()=>serviceMethodRequest(request,'toString','x'));assert.throws(()=>serviceMethodRequest(request,'gift','forged'));
});
