import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {probeRequest,parseProbe,PROBE_SCHEMA} from './understanding-probe.mjs';import {scopeProbeRequest} from './understanding-scope.mjs';
const plan=JSON.parse(readFileSync(new URL('./understanding-scope-cases.json',import.meta.url))),dir=new URL('./evidence/understanding-scope-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const hashes=Object.fromEntries(['understanding-scope-cases.json','understanding-scope-run.mjs','understanding-scope.mjs','understanding-probe.mjs'].map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')]));
const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:PROBE_SCHEMA,options:{...body.options,num_ctx:4096}}:body,options)});
const result={createdAt:new Date().toISOString(),model:client.modelName,plan,hashes,method:'A frozen simple question vs B explicit negation-scope question. Same material/schema/model/options, one call per arm, order alternates, no retries, raw retained. Prompt-only comparison; diagnostic results not wired to state.',rows:[]};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
function score(rows,key){return{total:rows.length,correct:rows.filter(r=>r[key].answer===r.expected).length,invalid:rows.filter(r=>typeof r[key].answer!=='boolean').length,falsePositive:rows.filter(r=>!r.expected&&r[key].answer===true).length,falseNegative:rows.filter(r=>r.expected&&r[key].answer===false).length,failedIds:rows.filter(r=>r[key].answer!==r.expected).map(r=>r.id)};}
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Another model is resident; stopped without disturbing it');
 for(const[index,c]of plan.cases.entries()){
  const row={...c,A:{},B:{}};result.rows.push(row);save();
  for(const key of index%2?['B','A']:['A','B']){const started=Date.now();try{row[key].raw=await client.complete(key==='A'?probeRequest(client.modelName,c.text,true):scopeProbeRequest(client.modelName,c.text));save();row[key].answer=parseProbe(row[key].raw,true);}catch(error){row[key].error=error.message;}row[key].wallMs=Date.now()-started;save();}process.stdout.write(JSON.stringify({id:c.id,expected:c.expected,A:row.A.answer??'invalid',B:row.B.answer??'invalid'})+'\n');
 }
 result.summary={};for(const split of ['development','new','all']){const rows=result.rows.filter(r=>split==='all'||r.split===split);result.summary[split]={A:score(rows,'A'),B:score(rows,'B'),regressions:rows.filter(r=>r.A.answer===r.expected&&r.B.answer!==r.expected).map(r=>r.id)};}
 result.productionAccepted=false;save();process.stdout.write(JSON.stringify({summary:result.summary,evidence:dir.href})+'\n');
}catch(error){result.runError=error.message;save();throw error;}finally{client.close();}
