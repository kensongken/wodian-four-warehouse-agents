import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {intentRequest,parseIntent,INTENT_SCHEMA} from './temporary-intent.mjs';import {attributionRequest,parseAttribution,gateIntent,ATTRIBUTION_SCHEMA} from './intent-attribution.mjs';
const plan=JSON.parse(readFileSync(new URL('./intent-comparison-cases.json',import.meta.url)));
const dir=new URL('./evidence/intent-comparison-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const hashes=Object.fromEntries(['intent-comparison-cases.json','temporary-intent.mjs','intent-attribution.mjs','intent-comparison.mjs'].map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')]));
const clients=[INTENT_SCHEMA,ATTRIBUTION_SCHEMA].map(format=>new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format,options:{...body.options,num_ctx:4096}}:body,options)}));
const result={createdAt:new Date().toISOString(),model:clients[0].modelName,plan,hashes,method:'Paired comparison: B gates exactly the same native intent output used by A. Two native calls per case, order alternates. No retries; both raw outputs saved before parsing. B latency sums both calls (conservative; runtime could skip intent call when gated). No production change.',rows:[]};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
function score(rows,key){const errors=rows.filter(r=>!r[key]?.parsed),correct=rows.filter(r=>r[key]?.parsed?.act===r.expected),falseResume=rows.filter(r=>r.expected!=='continue_selection'&&r[key]?.parsed?.act==='continue_selection'),missedPause=rows.filter(r=>['pause_sales','change_topic'].includes(r.expected)&&r[key]?.parsed?.act!==r.expected),falsePause=rows.filter(r=>!['pause_sales','change_topic'].includes(r.expected)&&['pause_sales','change_topic'].includes(r[key]?.parsed?.act));const times=rows.map(r=>r[key]?.wallMs).filter(Number.isFinite).sort((a,b)=>a-b);return{total:rows.length,correct:correct.length,invalid:errors.length,falseResume:falseResume.length,missedPause:missedPause.length,falsePause:falsePause.length,medianMs:times[Math.floor(times.length/2)]??null,failedIds:rows.filter(r=>r[key]?.parsed?.act!==r.expected).map(r=>r.id)};}
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==clients[0].modelName))throw Error('Another model is resident; stopped without disturbing it');
 for(const [index,c]of plan.cases.entries()){
  const row={...c};result.rows.push(row);save();
  for(const which of index%2?['attribution','intent']:['intent','attribution']){const n=which==='intent'?0:1,started=Date.now();try{row[which]={raw:await clients[n].complete((n?attributionRequest:intentRequest)(clients[n].modelName,c.text))};}catch(error){row[which]={error:error.message};}row[which].wallMs=Date.now()-started;save();}
  row.A={wallMs:row.intent.wallMs};row.B={wallMs:row.intent.wallMs+row.attribution.wallMs};
  try{row.A.parsed=parseIntent(c.text,row.intent.raw);}catch(error){row.A.error=error.message;}
  try{row.context=parseAttribution(row.attribution.raw);row.B.parsed=gateIntent(c.text,row.intent.raw,row.attribution.raw);}catch(error){row.B.error=error.message;}
  save();process.stdout.write(JSON.stringify({id:c.id,expected:c.expected,context:row.context,A:row.A.parsed?.act||'invalid',B:row.B.parsed?.act||'invalid'})+'\n');
 }
 result.summary={};for(const split of ['development','new','all']){const rows=result.rows.filter(r=>split==='all'||r.split===split);result.summary[split]={A:score(rows,'A'),B:score(rows,'B')};}
 const n=result.summary.new;result.candidateMeetsExploratoryGate=n.B.correct===n.B.total&&n.B.falseResume===0&&n.B.missedPause===0&&n.B.correct>n.A.correct;save();process.stdout.write(JSON.stringify({summary:result.summary,candidateMeetsExploratoryGate:result.candidateMeetsExploratoryGate,evidence:dir.href})+'\n');
}catch(error){result.runError=error.message;save();throw error;}finally{clients.forEach(c=>c.close());}
