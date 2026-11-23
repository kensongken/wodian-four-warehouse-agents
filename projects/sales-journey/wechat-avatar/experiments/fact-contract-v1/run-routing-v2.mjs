import {readFileSync,writeFileSync} from 'node:fs';import {createHash} from 'node:crypto';import {execFileSync} from 'node:child_process';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {receive,routingRequest} from './conversation.mjs';import {routingRequestV2,ROUTE_SCHEMA} from './routing-v2.mjs';import {EvidenceLedger} from './contract.mjs';
const out=new URL('./evidence/routing-comparison.json',import.meta.url);
try{readFileSync(out);throw Error('Existing results, no rerun');}catch(e){if(e.code!=='ENOENT')throw e;}
const ps=await fetch('http://127.0.0.1:11434/api/ps').then(r=>r.json());if(ps.models.some(m=>m.name!=='qwen3-vl:4b-instruct'))throw Error('Other model resident');
const tags=await fetch('http://127.0.0.1:11434/api/tags').then(r=>r.json());const digest='ee4b975b58c17ce268cd19d40db35d5edc64603035d2ffc1fee1968eb0947f7b';if(!tags.models.some(m=>m.name==='qwen3-vl:4b-instruct'&&m.digest===digest))throw Error('Model changed');
const hash=x=>createHash('sha256').update(x).digest('hex');const files=['conversation.mjs','routing-v2.mjs','route-cases.json','routing-new-cases.json','run-routing-v2.mjs'];const hashes=Object.fromEntries(files.map(f=>[f,hash(readFileSync(new URL('./'+f,import.meta.url)))]));
const cases=['route-cases.json','routing-new-cases.json'].flatMap((f,i)=>JSON.parse(readFileSync(new URL('./'+f,import.meta.url))).map(c=>({...c,cohort:i?'new':'regression'})));
const result={scope:'combined schema+instruction candidate, 12 known regression +12 developer-authored new cases; one run, not independent acceptance',sha:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),hashes,digest,rows:[],realWechatCalls:0,workbenchChanged:false};
for(let i=0;i<cases.length;i++)for(const arm of (i%2?['v2','v1']:['v1','v2'])){
 const item=cases[i];const transport=[];const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:async(path,body,options)=>{
  if(path==='/api/chat'){body={...body,format:arm==='v2'?ROUTE_SCHEMA:'json',options:{num_ctx:4096,num_predict:512,num_batch:64,temperature:0.2,seed:20261103+i}};transport.push(body);}return localOllamaJSON(path,body,options);
 }});
 let output;try{output=await receive({message:item.message,model:client.modelName,requestBuilder:arm==='v2'?routingRequestV2:routingRequest,complete:p=>client.complete(p),ledger:new EvidenceLedger(),binding:{storeId:'demo',customerId:'demo',subjects:{order:'o',inventory:'p',delivery_today:'p',owner_presence:'boss'}},lookupEvidence:async()=>null});}finally{client.close();}
 const matches=!!output.route&&JSON.stringify([...output.route.topics].sort())===JSON.stringify([...item.expected].sort())&&(item.expected.length>0||output.route.socialAct===item.socialAct);
 result.rows.push({...item,arm,matches,output,transport});writeFileSync(out,JSON.stringify(result,null,2));console.log(JSON.stringify({n:result.rows.length,arm,matches,cohort:item.cohort,route:output.route}));
}
for(const[f,h]of Object.entries(hashes))if(hash(readFileSync(new URL('./'+f,import.meta.url)))!==h)throw Error('Source changed');result.completed=true;writeFileSync(out,JSON.stringify(result,null,2));
