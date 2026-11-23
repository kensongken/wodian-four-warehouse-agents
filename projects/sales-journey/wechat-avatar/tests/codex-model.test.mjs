import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {existsSync} from 'node:fs';
import {CodexModel,codexArgs} from '../lib/codex-model.mjs';
const payload={model:'gpt-5.6-sol',messages:[{role:'user',content:'【合成】你好'}]};
const event=(text='您好')=>JSON.stringify({type:'item.completed',item:{type:'agent_message',text}})+'\n'+JSON.stringify({type:'turn.completed'})+'\n';
function fixture(run){let child,call;const model=new CodexModel({timeout:100,locate:()=>'/official/codex',spawnProcess:(binary,args,options)=>{
 call={binary,args,options};child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.stdin=new PassThrough();child.kills=0;child.kill=()=>{child.kills++;queueMicrotask(()=>child.emit('close',null));};queueMicrotask(()=>run?.(child));return child;
}});return{model,child:()=>child,call:()=>call};}
test('Codex invocation pins GPT-5.6, disables action tools and has no shell or prompt arguments',async()=>{
 const f=fixture(c=>{c.stdout.write(event());c.emit('close',0);});const r=await f.model.complete(payload);assert.equal(r.choices[0].message.content,'您好');
 const {args,options}=f.call();assert.equal(args[args.indexOf('-m')+1],'gpt-5.6-sol');assert.ok(args.includes('--ignore-user-config'));assert.ok(args.includes('--ephemeral'));assert.ok(args.includes('features.multi_agent=false'));assert.ok(args.includes('features.shell_tool=false'));assert.ok(args.includes('features.plugins=false'));assert.equal(options.shell,false);assert.ok(!JSON.stringify(args).includes('合成'));assert.equal(existsSync(options.cwd),false);
});
test('UTF8 text split between process chunks is preserved',async()=>{
 const f=fixture(c=>{const b=Buffer.from(event('您好，先聊聊您的喜好。'));for(const byte of b)c.stdout.write(Buffer.from([byte]));c.emit('close',0);});assert.equal((await f.model.complete(payload)).choices[0].message.content,'您好，先聊聊您的喜好。');
});
test('model family mismatch is rejected without spawning process',async()=>{
 let called=0;const c=new CodexModel({locate:()=>'/official/codex',spawnProcess:()=>called++});await assert.rejects(()=>c.complete({...payload,model:'gpt-6-astra'}),/GPT-5.6/);assert.equal(called,0);assert.throws(()=>codexArgs('gpt-6-sol','/tmp'),/GPT-5.6/);
});
test('incomplete output and provider errors never become a successful draft or expose diagnostics',async()=>{
 for(const mode of ['incomplete','provider']){
 const f=fixture(c=>{c.stderr.write('PRIVATE_ACCOUNT_DIAGNOSTIC');c.stdout.write(mode==='provider'?JSON.stringify({type:'error',message:'SECRET_PROVIDER_MESSAGE'})+'\n':JSON.stringify({type:'item.completed',item:{type:'agent_message',text:'不完整文字'}})+'\n');c.emit('close',1);});
 await assert.rejects(()=>f.model.complete(payload),e=>!e.message.includes('SECRET')&&!e.message.includes('PRIVATE'));assert.equal(existsSync(f.call().options.cwd),false);
 }
});
test('timeout kills child and concurrent calls do not spawn another process',async()=>{
 const f=fixture();f.model.timeout=15;const pending=f.model.complete(payload);await assert.rejects(()=>f.model.complete(payload),/正在处理/);await assert.rejects(()=>pending,/超时/);assert.ok(f.child().kills>=1);
});
test('tool attempt is rejected and killed instead of being accepted as text',async()=>{
 const f=fixture(c=>{c.stdout.write(JSON.stringify({type:'item.started',item:{type:'command_execution',command:'do not execute'}})+'\n');});await assert.rejects(()=>f.model.complete(payload),/结果已拒绝/);assert.ok(f.child().kills>=1);
});
test('output size cap rejects oversized process output',async()=>{
 const f=fixture(c=>c.stdout.write(Buffer.alloc(256001)));await assert.rejects(()=>f.model.complete(payload),/超出上限/);
});
