import {readFileSync,writeFileSync} from 'node:fs';import {createHash} from 'node:crypto';import {execFileSync} from 'node:child_process';
import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {receive} from './conversation.mjs';import {routingRequestV2,ROUTE_SCHEMA} from './routing-v2.mjs';import {routingRequestV3,ROUTE_SCHEMA_V3,projectRoute} from './routing-v3.mjs';import {EvidenceLedger} from './contract.mjs';
const out=new URL('./evidence/routing-v3-comparison.json',import.meta.url);
try{readFileSync(out);throw Error('Existing result; no rerun');}catch(e){if(e.code!=='ENOENT')throw e;}
const ps=await fetch('http://127.0.0.1:11434/api/ps').then(r=>r.json());if(ps.models.some(m=>m.name!=='qwen3-vl:4b-instruct'))throw Error('Other model resident');
const digest='ee4b975b58c17ce268cd19d40db35d5edc64603035d2ffc1fee1968eb0947f7b';const tags=await fetch('http://127.0.0.1:11434/api/tags').then(r=>r.json());if(!tags.models.some(m=>m.name==='qwen3-vl:4b-instruct'&&m.digest===digest))throw Error('Model changed');
const hash=x=>createHash('sha256').update(x).digest('hex');const files=['contract.mjs','conversation.mjs','routing-v2.mjs','routing-v3.mjs','route-cases.json','routing-new-cases.json','routing-fresh-cases.json','run-routing-v3.mjs'];const hashes=Object.fromEntries(files.map(f=>[f,hash(readFileSync(new URL('./'+f,import.meta.url)))]));
const cases=['route-cases.json','routing-new-cases.json','routing-fresh-cases.json'].flatMap((f,i)=>JSON.parse(readFileSync(new URL('./'+f,import.meta.url))).map(c=>({...c,cohort:i===2?'fresh':'regression'})));
const result={scope:'v2 versus extractive v3, combined schema+prompt+projection changes;24 known and12 author-created fresh cases, not independent heldout',sha:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),hashes,digest,rows:[],realWechatCalls:0,workbenchChanged:false};
let transportFailures=0;
for(let i=0;i<cases.length;i++)for(const arm of (i%2?['v3','v2']:['v2','v3'])){
 const item=cases[i],transport=[];let nativeResponse;
 const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:async(path,body,options)=>{
  if(path==='/api/chat'){body={...body,format:arm==='v3'?ROUTE_SCHEMA_V3:ROUTE_SCHEMA,options:{num_ctx:4096,num_predict:512,num_batch:64,temperature:0.2,seed:20261104+i}};transport.push(body);}return localOllamaJSON(path,body,options);
 }});
 let output;try{output=await receive({message:item.message,model:client.modelName,requestBuilder:arm==='v3'?routingRequestV3:routingRequestV2,complete:async p=>{nativeResponse=await client.complete(p);return arm==='v3'?projectRoute(nativeResponse,item.message):nativeResponse;},ledger:new EvidenceLedger(),binding:{storeId:'demo',customerId:'demo',subjects:{order:'o',inventory:'p',delivery_today:'p',owner_presence:'boss'}},lookupEvidence:async()=>null});}finally{client.close();}
 const matches=!!output.route&&JSON.stringify([...output.route.topics].sort())===JSON.stringify([...item.expected].sort())&&(item.expected.length>0||output.route.socialAct===item.socialAct);
 result.rows.push({...item,arm,matches,output,transport,nativeResponse});writeFileSync(out,JSON.stringify(result,null,2));console.log(JSON.stringify({n:result.rows.length,arm,matches,cohort:item.cohort,route:output.route,error:output.routeError}));
 if(output.routeError?.includes('本机千问请求失败')&&++transportFailures>=3)throw Error('Repeated backend failures; preserve incomplete experiment');
}
for(const[f,h]of Object.entries(hashes))if(hash(readFileSync(new URL('./'+f,import.meta.url)))!==h)throw Error('Source changed');result.completed=true;writeFileSync(out,JSON.stringify(result,null,2));
