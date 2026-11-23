import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {probeRequest,parseProbe,PROBE_SCHEMA} from './understanding-probe.mjs';
const plan=JSON.parse(readFileSync(new URL('./understanding-probe-cases.json',import.meta.url))),dir=new URL('./evidence/understanding-probe-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const hashes=Object.fromEntries(['understanding-probe-cases.json','understanding-probe.mjs','understanding-probe-run.mjs'].map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')]));
const clients=[false,true].map(structured=>new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,...(structured?{format:PROBE_SCHEMA}:{}),options:{...body.options,num_ctx:4096}}:body,options)}));
const result={createdAt:new Date().toISOString(),model:clients[0].modelName,plan,hashes,method:'16 identical yes/no comprehension questions, plain A versus native-schema JSON B. Output request and grammar differ; underlying question and model/options unchanged. Order alternates, one run, no retries. All raw results saved. Pure diagnostic, no state projection or release approval.',rows:[]};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
function score(rows,key){return{total:rows.length,correct:rows.filter(r=>r[key].answer===r.expected).length,invalid:rows.filter(r=>typeof r[key].answer!=='boolean').length,falsePositive:rows.filter(r=>!r.expected&&r[key].answer===true).length,falseNegative:rows.filter(r=>r.expected&&r[key].answer===false).length,failedIds:rows.filter(r=>r[key].answer!==r.expected).map(r=>r.id)};}
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==clients[0].modelName))throw Error('Another model is resident; stopped without disturbing it');
 for(const [index,c]of plan.cases.entries()){
  const row={...c,A:{},B:{}};result.rows.push(row);save();
  for(const key of index%2?['B','A']:['A','B']){const n=key==='A'?0:1,started=Date.now();try{row[key].raw=await clients[n].complete(probeRequest(clients[n].modelName,c.text,!!n));save();row[key].answer=parseProbe(row[key].raw,!!n);}catch(error){row[key].error=error.message;}row[key].wallMs=Date.now()-started;save();}
  process.stdout.write(JSON.stringify({id:c.id,expected:c.expected,A:row.A.answer??'invalid',B:row.B.answer??'invalid'})+'\n');
 }
 result.summary={};for(const split of ['development','new','all']){const rows=result.rows.filter(r=>split==='all'||r.split===split);result.summary[split]={A:score(rows,'A'),B:score(rows,'B')};}
 result.dimensions=Object.fromEntries(['speaker','hypothesis','output_control'].map(d=>{const rows=result.rows.filter(r=>r.split==='new'&&r.dimension===d);return[d,{A:score(rows,'A'),B:score(rows,'B')}];}));
 result.pairs=Object.fromEntries([...new Set(result.rows.filter(r=>r.pair).map(r=>r.pair))].map(pair=>{const rows=result.rows.filter(r=>r.pair===pair);return[pair,{A:rows.every(r=>r.A.answer===r.expected),B:rows.every(r=>r.B.answer===r.expected)}];}));
 result.productionAccepted=false;save();process.stdout.write(JSON.stringify({summary:result.summary,dimensions:result.dimensions,pairs:result.pairs,evidence:dir.href})+'\n');
}catch(error){result.runError=error.message;save();throw error;}finally{clients.forEach(c=>c.close());}
