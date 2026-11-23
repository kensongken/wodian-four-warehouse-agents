import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';import {LocalQwen,LocalTextTeacher,localOllamaJSON} from '../../lib/qwen-model.mjs';import {parseProbe,PROBE_SCHEMA} from './understanding-probe.mjs';import {scopeProbeRequest} from './understanding-scope.mjs';
const known=JSON.parse(readFileSync(new URL('./understanding-scope-cases.json',import.meta.url))),holdout=JSON.parse(readFileSync(new URL('./understanding-model-holdout.json',import.meta.url)));
const cases=[...known.cases.map(c=>({...c,split:'known'})),...holdout.cases.map(c=>({...c,split:'new'}))];
const dir=new URL('./evidence/understanding-model-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const hashes=Object.fromEntries(['understanding-scope-cases.json','understanding-model-holdout.json','understanding-model-run.mjs','understanding-probe.mjs','understanding-scope.mjs','../../lib/qwen-model.mjs'].map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')]));
const options={keepAlive:'120s',timeout:120000,post:(path,body,opts)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:PROBE_SCHEMA,options:{...body.options,num_ctx:4096}}:body,opts)};
const clients={A:new LocalQwen(options),B:new LocalTextTeacher(options)};
const result={createdAt:new Date().toISOString(),models:{A:clients.A.modelName,B:clients.B.modelName},hashes,plan:{known:12,new:8,cases},method:'Same frozen scope-aware question, material, native JSON schema, temperature/context/output limits. A vision-instruct 4B, B text 8B with existing wrapper think:false; architectures/families differ, so effects cannot be attributed to parameter count alone. Fixed batches A then B avoid repeated model swapping. One sample/case/model, no retries or downloads. First request load and subsequent response latency reported separately; not a deployment SLA. No state changes or production switch.',rows:cases.map(c=>({...c,A:{},B:{}})),runtime:{}};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
function score(rows,key){return{total:rows.length,correct:rows.filter(r=>r[key].answer===r.expected).length,invalid:rows.filter(r=>typeof r[key].answer!=='boolean').length,falsePositive:rows.filter(r=>!r.expected&&r[key].answer===true).length,falseNegative:rows.filter(r=>r.expected&&r[key].answer===false).length,failedIds:rows.filter(r=>r[key].answer!==r.expected).map(r=>r.id)};}
async function ps(){const r=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error('Cannot inspect model runtime');return r.json();}
try{
 const initial=await ps();result.runtime.initial=initial;save();if(initial.models.length)throw Error('A model is already resident; stopped rather than disrupting other inference');
 const tags=await fetch('http://127.0.0.1:11434/api/tags',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(Object.values(clients).some(c=>!tags.models.some(m=>m.name===c.modelName)))throw Error('Required model not installed; no download attempted');
 for(const key of ['A','B']){
  const other=(await ps()).models.filter(m=>!Object.values(clients).some(c=>m.name===c.modelName));if(other.length)throw Error('An unrelated model is resident; stopped');
  for(const [index,row]of result.rows.entries()){
   const started=Date.now();try{row[key].raw=await clients[key].complete(scopeProbeRequest(clients[key].modelName,row.text));save();row[key].answer=parseProbe(row[key].raw,true);}catch(error){row[key].error=error.message;}row[key].wallMs=Date.now()-started;save();
   if(index===0){result.runtime[key]={firstResponseMs:row[key].wallMs,firstLoadMs:row[key].raw?.performance?.modelLoadMs??null,resident:await ps()};save();}
   process.stdout.write(JSON.stringify({model:key,id:row.id,expected:row.expected,answer:row[key].answer??'invalid',wallMs:row[key].wallMs})+'\n');
  }
  const subsequent=result.rows.slice(1).filter(r=>typeof r[key].answer==='boolean').map(r=>r[key].wallMs).sort((a,b)=>a-b);result.runtime[key].subsequentMedianMs=subsequent.length?subsequent[Math.floor(subsequent.length/2)]:null;save();
 }
 result.summary={};for(const split of ['known','new','all']){const rows=result.rows.filter(r=>split==='all'||r.split===split);result.summary[split]={A:score(rows,'A'),B:score(rows,'B'),regressions:rows.filter(r=>r.A.answer===r.expected&&r.B.answer!==r.expected).map(r=>r.id)};}
 result.productionAccepted=false;save();process.stdout.write(JSON.stringify({summary:result.summary,runtime:result.runtime,evidence:dir.href})+'\n');
}catch(error){result.runError=error.message;save();throw error;}finally{Object.values(clients).forEach(c=>c.close());}
