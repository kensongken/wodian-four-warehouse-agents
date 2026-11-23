import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {intentRequest} from './temporary-intent.mjs';import {attributionRequest,ATTRIBUTION_SCHEMA} from './intent-attribution.mjs';import {INTENT_SCHEMA_V2} from './intent-schema-v2.mjs';import {choiceRequest,CHOICE_SCHEMA} from './intent-choice-gate.mjs';import {gateCombined} from './intent-combined-gate.mjs';import {frameRequest,projectFrames,FRAME_SCHEMA} from './intent-frame.mjs';
const plan=JSON.parse(readFileSync(new URL('./intent-frame-cases.json',import.meta.url)));
const dir=new URL('./evidence/intent-frame-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const sources=['intent-frame-cases.json','intent-frame.mjs','intent-frame-comparison.mjs','temporary-intent.mjs','intent-attribution.mjs','intent-choice-gate.mjs','intent-schema-v2.mjs','intent-combined-gate.mjs'];
const hashes=Object.fromEntries(sources.map(name=>[name,createHash('sha256').update(readFileSync(new URL('./'+name,import.meta.url))).digest('hex')]));
const clients=[ATTRIBUTION_SCHEMA,CHOICE_SCHEMA,INTENT_SCHEMA_V2,FRAME_SCHEMA].map(format=>new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,options)=>localOllamaJSON(path,path==='/api/chat'?{...body,format,options:{...body.options,num_ctx:4096}}:body,options)}));
const result={createdAt:new Date().toISOString(),model:clients[0].modelName,plan,hashes,method:'A combined attribution+choice+intent (3 calls) vs B actor-context-action frame extraction (1 call). Same fixed model, different experimental architecture; not isolated one-factor comparison. Four calls/case, alternating order, no retries, raw saved before parsing. Sum of actual calls used as latency estimate.',rows:[]};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
function score(rows,key){const stops=a=>['pause_sales','change_topic'].includes(a),times=rows.map(r=>r[key].wallMs).sort((a,b)=>a-b);return{total:rows.length,correct:rows.filter(r=>r[key].parsed?.act===r.expected).length,invalid:rows.filter(r=>!r[key].parsed).length,falseResume:rows.filter(r=>r.expected!=='continue_selection'&&r[key].parsed?.act==='continue_selection').length,falsePause:rows.filter(r=>!stops(r.expected)&&stops(r[key].parsed?.act)).length,missedStop:rows.filter(r=>stops(r.expected)&&!stops(r[key].parsed?.act)).length,wrongStopType:rows.filter(r=>stops(r.expected)&&stops(r[key].parsed?.act)&&r.expected!==r[key].parsed.act).length,medianMs:times[Math.floor(times.length/2)],failedIds:rows.filter(r=>r[key].parsed?.act!==r.expected).map(r=>r.id)};}
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==clients[0].modelName))throw Error('Another model is resident; stopped without disturbing it');
 for(const [index,c]of plan.cases.entries()){
  const row={...c,attribution:{},choice:{},intent:{},frame:{},A:{},B:{}};result.rows.push(row);save();
  for(const key of index%2?['frame','intent','choice','attribution']:['attribution','choice','intent','frame']){const n={attribution:0,choice:1,intent:2,frame:3}[key],started=Date.now();try{row[key].raw=await clients[n].complete([attributionRequest,choiceRequest,intentRequest,frameRequest][n](clients[n].modelName,c.text));}catch(error){row[key].callError=error.message;}row[key].callMs=Date.now()-started;save();}
  row.A.wallMs=row.attribution.callMs+row.choice.callMs+row.intent.callMs;row.B.wallMs=row.frame.callMs;
  try{row.A.parsed=gateCombined(c.text,row.intent.raw,row.attribution.raw,row.choice.raw);}catch(error){row.A.error=error.message;}
  try{row.B.parsed=projectFrames(c.text,row.frame.raw);}catch(error){row.B.error=error.message;}
  save();process.stdout.write(JSON.stringify({id:c.id,expected:c.expected,A:row.A.parsed?.act||'invalid',B:row.B.parsed?.act||'invalid'})+'\n');
 }
 result.summary={};for(const split of ['development','new','all']){const rows=result.rows.filter(r=>split==='all'||r.split===split);result.summary[split]={A:score(rows,'A'),B:score(rows,'B')};}
 const b=result.summary.all.B;result.candidateMeetsExploratoryGate=b.correct===b.total&&b.falseResume===0&&b.missedStop===0;save();process.stdout.write(JSON.stringify({summary:result.summary,candidateMeetsExploratoryGate:result.candidateMeetsExploratoryGate,evidence:dir.href})+'\n');
}catch(error){result.runError=error.message;save();throw error;}finally{clients.forEach(c=>c.close());}
