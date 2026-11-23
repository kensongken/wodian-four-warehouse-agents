import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';import {execFileSync} from 'node:child_process';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {NaturalReception,NATURAL_SCHEMA} from './natural-reception.mjs';import {notebookRequest} from './conversation-notebook.mjs';import {cases} from './conversation-notebook-cases.mjs';
const dir=new URL('./evidence/conversation-notebook-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const result={createdAt:new Date().toISOString(),head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),status:'running',method:'A=current natural preview. B=same pipeline, same model and settings, plus a pre-authored oracle customer card with source quotes. Each arm generates its own conversation history. Five four-turn scenarios; single samples, no retries. AB/BA order alternates across scenarios; first-load latency is not a model-speed comparison. Oracle cards are not automatic extraction and not production customer state. Four synthetic scenarios and one observed reproduction with synthetic continuation. No native model judge; later CTO self-review against frozen per-turn checks.',hashes:Object.fromEntries(['natural-reception.mjs','conversation-notebook.mjs','conversation-notebook-cases.mjs','conversation-notebook-run.mjs','../../lib/qwen-model.mjs'].map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')])),cases,rows:[],nativeAttempts:0,externalSends:0,productionAccepted:false,semanticReviewStatus:'pending'};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2)+'\n');save();
const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:NATURAL_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());result.initialResidency=ps;
 if(!Array.isArray(ps.models)||ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident; no unloading or switching authorized');
 result.model=client.modelName;save();
 for(const[index,c]of cases.entries())for(const arm of index%2?['B','A']:['A','B']){
  let card;const statements=[];
  const conversation=new NaturalReception({model:client.modelName,complete:async request=>{
   const payload=arm==='B'?notebookRequest(request,card,statements):request;result.nativeAttempts++;row.request=payload;save();return client.complete(payload);
  }});let row;
  for(const[i,t]of c.turns.entries()){
   card=t.card;statements.push(t.message);row={caseId:c.id,arm,turn:i+1,message:t.message,checks:t.checks,oracleProvided:arm==='B',oracle:arm==='B'?card:null};result.rows.push(row);const started=Date.now();
   try{row.result=await conversation.turn({message:t.message,tone:'warm'});}catch(e){row.error=e.message;}
   row.wallMs=Date.now()-started;save();console.log(JSON.stringify({caseId:c.id,arm,turn:i+1,reply:row.result?.reply??null,error:row.error??null,wallMs:row.wallMs}));
  }
 }
 result.status='completed';save();console.log(JSON.stringify({evidence:dir.href,rows:result.rows.length,nativeAttempts:result.nativeAttempts,review:'pending',productionAccepted:false}));
}catch(e){result.status='failed';result.error=e.message;save();throw e;}finally{client.close();}
