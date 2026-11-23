import {readFileSync,writeFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {LocalQwen} from '../../lib/qwen-model.mjs';
import {EvidenceLedger} from './contract.mjs';import {receive} from './conversation.mjs';
const path=new URL('./evidence/routing-run.json',import.meta.url);
try{readFileSync(path);throw Error('Existing run; not overwriting');}catch(e){if(e.code!=='ENOENT')throw e;}
const ps=await fetch('http://127.0.0.1:11434/api/ps').then(r=>r.json());
if(ps.models.some(m=>m.name!=='qwen3-vl:4b-instruct'))throw Error('Other model resident; not unloading');
const tags=await fetch('http://127.0.0.1:11434/api/tags').then(r=>r.json());
const digest='ee4b975b58c17ce268cd19d40db35d5edc64603035d2ffc1fee1968eb0947f7b';
if(!tags.models.some(m=>m.name==='qwen3-vl:4b-instruct'&&m.digest===digest))throw Error('Model changed');
const cases=JSON.parse(readFileSync(new URL('./route-cases.json',import.meta.url)));
const client=new LocalQwen({keepAlive:'120s',timeout:120000});
const result={scope:'12 developer-authored synthetic routing cases, not heldout or production acceptance',sha:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),model:client.modelName,digest,rows:[],realWechatCalls:0,originalAccountRead:false};
const binding={storeId:'demo',customerId:'demo',subjects:{order:'order-demo',inventory:'product-demo',delivery_today:'product-demo',owner_presence:'owner-demo'}};
try{for(const item of cases){
 const start=Date.now();const output=await receive({message:item.message,model:client.modelName,complete:p=>client.complete(p),ledger:new EvidenceLedger(),binding,lookupEvidence:async()=>null});
 const matches=!!output.route&&JSON.stringify([...output.route.topics].sort())===JSON.stringify([...item.expected].sort())&&(item.expected.length||output.route.socialAct===item.socialAct);
 result.rows.push({...item,output,matches:!!matches,elapsedMs:Date.now()-start});writeFileSync(path,JSON.stringify(result,null,2));console.log(JSON.stringify({n:result.rows.length,matches:!!matches,route:output.route,reply:output.reply}));
}}finally{client.close();}
result.completed=true;result.exactRouteMatches=result.rows.filter(r=>r.matches).length;writeFileSync(path,JSON.stringify(result,null,2));
