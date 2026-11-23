import {mkdirSync,readFileSync,writeFileSync,renameSync,statfsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';import {createHash} from 'node:crypto';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';
import {TaskServicePlanner,TASK_SCHEMA} from './task-service-planner.mjs';import {StateServicePlanner,SERVICE_STATE_SCHEMA} from './state-service-planner.mjs';
import {actionSchema} from './service-boundary.mjs';import {STOP_SCOPE_SCHEMA} from './stop-scope.mjs';import {CASES,GATES} from './task-comparison-cases.mjs';
const dir=new URL('./evidence/task-comparison-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
let active,format,lastCall=false;
const client=new LocalQwen({timeout:120000,keepAlive:()=>lastCall?0:'120s',post:(p,b,o)=>{const payload=p==='/api/chat'?{...b,format,options:{...b.options,num_ctx:8192,seed:42}}:b;if(p==='/api/chat')active.nativeRequest=payload;return localOllamaJSON(p,payload,o);}});
const files=['state-service-planner.mjs','task-service-planner.mjs','service-boundary.mjs','stop-scope.mjs','task-comparison-cases.mjs','task-comparison-run.mjs'];
const d={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),model:client.modelName,gates:GATES,method:'Same local4B/context8192/seed42/temp0.2 and shared unchanged stop guard and program drafts. A previous packed state+model quotes, B only need/transition with program-bound exact current source and compact task state; continued purchase_info advances checklist. Bundled contract/state handling intervention, not a single-variable quote ablation. Four new fixed 3-turn customer scripts, own actual replies, alternating arm order, one sample, no retries. No default rollout, fact extraction or owner likeness claim',hashes:Object.fromEntries(files.map(n=>[n,createHash('sha256').update(readFileSync(new URL('./'+n,import.meta.url))).digest('hex')])),rows:[],status:'running',productionAccepted:false};
const save=()=>{const tmp=new URL('results.pending.json',dir);writeFileSync(tmp,JSON.stringify(d,null,2));renameSync(tmp,new URL('results.json',dir));};save();
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident; do not preempt');
 for(const[j,c]of CASES.entries()){
  const planners={},failed={A:false,B:false};let row;
  const complete=async(stage,schema,request)=>{const disk=statfsSync(dir);if(disk.bavail*disk.bsize<512*1024*1024)throw Error('Storage below512MiB');format=schema;const call={stage,request};active=call;row.calls.push(call);save();try{call.raw=await client.complete(request);return call.raw;}finally{save();}};
  planners.A=new StateServicePlanner({model:client.modelName,presentation:'packed',completeStopScope:q=>complete('stop_scope',STOP_SCOPE_SCHEMA,q),completeState:q=>complete('service_state',SERVICE_STATE_SCHEMA,q)});
  planners.B=new TaskServicePlanner({model:client.modelName,completeStopScope:q=>complete('stop_scope',STOP_SCOPE_SCHEMA,q),completeTask:q=>complete('service_task',TASK_SCHEMA,q)});
  for(const[i,t]of c.turns.entries())for(const arm of (j%2?['B','A']:['A','B'])){
   lastCall=j===CASES.length-1&&i===c.turns.length-1&&arm==='A';
   row={journey:c.id,arm,turn:i+1,...t,calls:[]};d.rows.push(row);const start=Date.now();
   if(failed[arm]){row.skipped='Prior arm turn failed';row.correct=false;save();continue;}
   try{row.output=await planners[arm].turn({message:t.message});row.correct=t.expected.includes(row.output.actionId);}catch(e){row.error=e.message;row.correct=false;failed[arm]=true;}
   row.elapsedMs=Date.now()-start;save();console.log(JSON.stringify({journey:c.id,arm,turn:i+1,action:row.output?.actionId,correct:row.correct,error:row.error}));
  }
 }
 d.status='completed';d.summary={};
 for(const arm of ['A','B']){const rows=d.rows.filter(r=>r.arm===arm);d.summary[arm]={correct:rows.filter(r=>r.correct).length,total:rows.length,missedExplicitStop:rows.filter(r=>r.explicitStop&&r.output?.actionId!=='close').length,prematureClose:rows.filter(r=>r.continueRequired&&r.output?.actionId==='close').length,errors:rows.filter(r=>r.error).length,skipped:rows.filter(r=>r.skipped).length};}
 const b=d.summary.B;d.gatePassed=b.correct>=GATES.minimumCandidateCorrect&&b.correct-d.summary.A.correct>=GATES.minimumGain&&b.missedExplicitStop===0&&b.prematureClose===0&&b.errors===0&&b.skipped===0;save();console.log(JSON.stringify({evidence:dir.pathname,summary:d.summary,passed:d.gatePassed}));
}catch(e){d.status='interrupted';d.error=e.message;save();console.log(JSON.stringify({error:d.error}));}finally{client.close();}
