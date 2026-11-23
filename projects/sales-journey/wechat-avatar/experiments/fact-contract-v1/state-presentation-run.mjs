import {mkdirSync,readFileSync,writeFileSync,renameSync,statfsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';import {createHash} from 'node:crypto';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';
import {StateServicePlanner,SERVICE_STATE_SCHEMA,parseServiceState,presentStateRequest} from './state-service-planner.mjs';
import {actionSchema} from './service-boundary.mjs';import {STOP_SCOPE_SCHEMA} from './stop-scope.mjs';import {CASES,GATES} from './state-presentation-cases.mjs';
const dir=new URL('./evidence/state-presentation-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
let active,format,lastCall=false;
const client=new LocalQwen({timeout:120000,keepAlive:()=>lastCall?0:'120s',post:(p,b,o)=>{const payload=p==='/api/chat'?{...b,format,options:{...b.options,num_ctx:8192,seed:42}}:b;if(p==='/api/chat')active.nativeRequest=payload;return localOllamaJSON(p,payload,o);}});
const source='evidence/state-comparison-2026-11-10T13-58-00.208Z/results.json';const sourceText=readFileSync(new URL('./'+source,import.meta.url));const sourceHash=createHash('sha256').update(sourceText).digest('hex');const diagnosticRows=JSON.parse(sourceText).rows.filter(r=>r.arm==='B'&&r.error==='Invalid state evidence');if(diagnosticRows.length!==4)throw Error('Expected four frozen diagnostic contexts');
const files=['state-service-planner.mjs','service-boundary.mjs','stop-scope.mjs','state-presentation-cases.mjs','state-presentation-run.mjs'];
const d={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),model:client.modelName,gates:GATES,method:'Same local4B/context8192/seed42/temp0.2, unchanged schema/system/task mapping/stop guard. A packed context, B actual conversation messages with latest user turn last. Same information, different representation. Four original failed contexts paired diagnosis; then nine fresh scripted turns per arm. Alternate order, own replies, one sample, no retries. No default rollout or natural-wording acceptance',hashes:Object.fromEntries(files.map(n=>[n,createHash('sha256').update(readFileSync(new URL('./'+n,import.meta.url))).digest('hex')])),diagnosticSource:{source,sha256:sourceHash},diagnostics:[],rows:[],status:'running',productionAccepted:false};
const save=()=>{const tmp=new URL('results.pending.json',dir);writeFileSync(tmp,JSON.stringify(d,null,2));renameSync(tmp,new URL('results.json',dir));};save();
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident; do not preempt');
 for(const[i,r]of diagnosticRows.entries())for(const arm of (i%2?['B','A']:['A','B'])){
  const original=r.calls.find(c=>c.stage==='service_state').request;
  const request=presentStateRequest(original,arm==='A'?'packed':'messages');
  const disk=statfsSync(dir);if(disk.bavail*disk.bsize<512*1024*1024)throw Error('Storage below512MiB');
  const record={journey:r.journey,turn:r.turn,arm,currentMessage:r.message,request};d.diagnostics.push(record);active=record;format=SERVICE_STATE_SCHEMA;save();const start=Date.now();
  try{record.raw=await client.complete(request);record.parsed=parseServiceState(r.message,record.raw);record.currentQuoteValid=true;}catch(e){record.error=e.message;record.currentQuoteValid=false;}record.elapsedMs=Date.now()-start;save();console.log(JSON.stringify({phase:'diagnosis',journey:r.journey,arm,quoteValid:record.currentQuoteValid,need:record.parsed?.need,error:record.error}));
 }
 for(const[j,c]of CASES.entries()){
  const planners={},failed={A:false,B:false};let row;
  const complete=async(stage,schema,request)=>{const disk=statfsSync(dir);if(disk.bavail*disk.bsize<512*1024*1024)throw Error('Storage below512MiB');format=schema;const call={stage,request};active=call;row.calls.push(call);save();try{call.raw=await client.complete(request);return call.raw;}finally{save();}};
  planners.A=new StateServicePlanner({model:client.modelName,presentation:'packed',completeStopScope:q=>complete('stop_scope',STOP_SCOPE_SCHEMA,q),completeState:q=>complete('service_state',SERVICE_STATE_SCHEMA,q)});
  planners.B=new StateServicePlanner({model:client.modelName,presentation:'messages',completeStopScope:q=>complete('stop_scope',STOP_SCOPE_SCHEMA,q),completeState:q=>complete('service_state',SERVICE_STATE_SCHEMA,q)});
  for(const[i,t]of c.turns.entries())for(const arm of (j%2?['B','A']:['A','B'])){
   lastCall=j===CASES.length-1&&i===c.turns.length-1&&arm==='B';
   row={journey:c.id,arm,turn:i+1,...t,calls:[]};d.rows.push(row);const start=Date.now();
   if(failed[arm]){row.skipped='Prior arm turn failed';row.correct=false;save();continue;}
   try{row.output=await planners[arm].turn({message:t.message});row.correct=t.expected.includes(row.output.actionId);}catch(e){row.error=e.message;row.correct=false;failed[arm]=true;}
   row.elapsedMs=Date.now()-start;save();console.log(JSON.stringify({journey:c.id,arm,turn:i+1,action:row.output?.actionId,correct:row.correct,error:row.error}));
  }
 }
 d.status='completed';d.summary={};
 for(const arm of ['A','B']){const rows=d.rows.filter(r=>r.arm===arm);d.summary[arm]={correct:rows.filter(r=>r.correct).length,total:rows.length,missedExplicitStop:rows.filter(r=>r.explicitStop&&r.output?.actionId!=='close').length,prematureClose:rows.filter(r=>r.continueRequired&&r.output?.actionId==='close').length,errors:rows.filter(r=>r.error).length,skipped:rows.filter(r=>r.skipped).length};}
 const b=d.summary.B;d.gatePassed=b.correct>=GATES.minimumCandidateCorrect&&b.correct-d.summary.A.correct>=GATES.minimumGain&&b.missedExplicitStop===0&&b.prematureClose===0&&b.errors===0&&b.skipped===0&&d.diagnostics.filter(r=>r.arm==='B'&&r.currentQuoteValid).length===4;save();console.log(JSON.stringify({evidence:dir.pathname,summary:d.summary,passed:d.gatePassed}));
}catch(e){d.status='interrupted';d.error=e.message;save();console.log(JSON.stringify({error:d.error}));}finally{client.close();}
