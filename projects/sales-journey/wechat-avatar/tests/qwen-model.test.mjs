import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {LocalQwen,LocalTextTeacher,QWEN_MODEL,QWEN_TEACHER_MODEL,localOllamaJSON} from '../lib/qwen-model.mjs';
const payload={model:QWEN_MODEL,messages:[{role:'system',content:'只起草，不承诺'},{role:'user',content:'合成测试你好',images:['must-not-send']}]};
const info={details:{format:'gguf',family:'qwen3vl'}};
const output={model:QWEN_MODEL,done:true,done_reason:'stop',message:{content:'您好，我先了解一下您的喜好。'}};
test('local Qwen request contains only text and uses pinned installed weights without cloud calls',async()=>{
 const calls=[];const q=new LocalQwen({post:async(path,body)=>{calls.push({path,body});return path==='/api/show'?info:output;}});
 assert.equal((await q.complete(payload)).choices[0].message.content,output.message.content);assert.equal(calls.length,2);assert.equal(calls[1].body.model,QWEN_MODEL);assert.equal(calls[1].body.stream,false);assert.equal(calls[1].body.keep_alive,0);assert.ok(!JSON.stringify(calls).includes('must-not-send'));assert.equal(q.busy,false);
});
test('cloud aliases or missing local GGUF proof stop before generation',async()=>{
 for(const bad of [{...info,remote_host:'cloud.example'},{...info,remote_model:'cloud-qwen'},{details:{format:'remote',family:'qwen3vl'}}]){
 let calls=0;const q=new LocalQwen({post:async()=>{calls++;return bad;}});await assert.rejects(()=>q.complete(payload),/本机千问权重/);assert.equal(calls,1);assert.equal(q.busy,false);
 }
});
test('truncated, tool-bearing, mismatched and incomplete local model outputs are refused',async()=>{
 for(const bad of [{...output,done_reason:'length'},{...output,done:false},{...output,model:'other-model'},{...output,message:{content:'调用工具',tool_calls:[{}]}}]){
 const q=new LocalQwen({post:async path=>path==='/api/show'?info:bad});await assert.rejects(()=>q.complete(payload),/完整有效/);
 }
});
test('context budget refuses oversize input before inference instead of silent truncation',async()=>{
 let calls=0;const q=new LocalQwen({post:async()=>calls++});await assert.rejects(()=>q.complete({...payload,messages:[{role:'user',content:'中'.repeat(2400)}]}),/上下文过长/);assert.equal(calls,0);
});
test('a model change is rejected before any request rather than downloaded or substituted',async()=>{
 let calls=0;const q=new LocalQwen({post:async()=>calls++});await assert.rejects(()=>q.complete({...payload,model:'qwen-cloud'}),/不自动替换/);assert.equal(calls,0);
});
test('concurrent local requests are refused and server close aborts pending request',async()=>{
 const q=new LocalQwen({post:(_path,_body,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('cancelled')),{once:true}))});const pending=q.complete(payload);await assert.rejects(()=>q.complete(payload),/正在处理/);q.close();await assert.rejects(()=>pending,/cancelled/);assert.equal(q.busy,false);
});
test('local HTTP transport fixes loopback and refuses redirects without exposing body',async()=>{
 let captured,calls=0;const request=(options,callback)=>{captured=options;calls++;const req=new EventEmitter();req.destroy=()=>{};req.end=()=>queueMicrotask(()=>{const res=new EventEmitter();res.statusCode=302;callback(res);res.emit('data',Buffer.from('PRIVATE_DIAGNOSTIC'));res.emit('end');});return req;};
 await assert.rejects(()=>localOllamaJSON('/api/chat',payload,{request}),e=>!e.message.includes('PRIVATE'));assert.equal(captured.hostname,'127.0.0.1');assert.equal(captured.port,11434);assert.equal(calls,1);await assert.rejects(()=>localOllamaJSON('/other',payload,{request}),/路径无效/);assert.equal(calls,1);
});

test('external stop aborts local inference and an ignored abort cannot produce accepted text',async()=>{
 let resolve;const signal=new AbortController();const q=new LocalQwen({post:async path=>path==='/api/show'?info:new Promise(r=>resolve=r)});const pending=q.complete(payload,{signal:signal.signal});await new Promise(r=>setImmediate(r));signal.abort();resolve(output);await assert.rejects(()=>pending,/已停止/);assert.equal(q.busy,false);
});

test('short and long local requests retain complete input with bounded context and batch memory',async()=>{
 for(const [length,context] of [[50,2048],[1500,4096],[4000,8192]]){
  const original=[{role:'system',content:'SAFETY_DO_NOT_DROP'},{role:'user',content:'a'.repeat(length)}];let request;
  const q=new LocalQwen({post:async(path,body)=>{if(path==='/api/show')return info;request=body;return output;}});
  await q.complete({model:QWEN_MODEL,messages:original});
  assert.deepEqual(request.messages,original);assert.equal(request.options.num_ctx,context);assert.equal(request.options.num_batch,64);
  assert.ok(Buffer.byteLength(JSON.stringify(original))+512+256<=context);
 }
});
test('JSON-object output is explicit and text callers retain their original response mode',async()=>{
 const requests=[];const q=new LocalQwen({post:async(path,body)=>{if(path==='/api/show')return info;requests.push(body);return output;}});
 await q.complete({...payload,response_format:{type:'json_object'}});assert.equal(requests[0].format,'json');
 await q.complete(payload);assert.equal(Object.hasOwn(requests[1],'format'),false);
 await assert.rejects(()=>q.complete({...payload,response_format:{type:'unsupported'}}),/输出格式/);assert.equal(requests.length,2);
});

test('text teacher pins its own local family, disables thinking and preserves ordinary model identity',async()=>{
 const calls=[],teacher=new LocalTextTeacher({post:async(path,body)=>{calls.push({path,body});return path==='/api/show'?{details:{format:'gguf',family:'qwen3'}}:{...output,model:QWEN_TEACHER_MODEL};}});
 await teacher.complete({...payload,model:QWEN_TEACHER_MODEL});assert.equal(calls[1].body.model,'qwen3:8b');assert.equal(calls[1].body.think,false);assert.equal(calls[1].body.keep_alive,0);assert.equal(new LocalQwen().available(),QWEN_MODEL);await assert.rejects(()=>teacher.complete(payload),/不自动替换/);
});
test('teacher refuses a cloud alias or wrong architecture before generating',async()=>{
 for(const info of [{remote_host:'cloud',details:{format:'gguf',family:'qwen3'}},{details:{format:'gguf',family:'qwen3vl'}}]){let calls=0;const teacher=new LocalTextTeacher({post:async()=>{calls++;return info;}});await assert.rejects(()=>teacher.complete({...payload,model:QWEN_TEACHER_MODEL}),/本机千问权重/);assert.equal(calls,1);}
});
