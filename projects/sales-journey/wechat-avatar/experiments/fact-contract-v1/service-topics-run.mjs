import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {createHash} from 'node:crypto';import {execFileSync} from 'node:child_process';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {topicRequest,parseTopic,TOPIC_SCHEMA} from './service-topics.mjs';
const cases=[
 {message:'这条玉戴腻了，想重新换个样子。',expected:'remodel'},
 {message:'妈妈留下的项链想保留，只把戴法改一改。',expected:'remodel'},
 {message:'手镯的扣子断了，你看接下来该怎么处理？',expected:'repair'},
 {message:'不是想改款，我只想把丢的耳堵配回来。',context:['我想换个样子'],expected:'repair'},
 {message:'这条平时怎么清洁保养比较好？',expected:'care'},
 {message:'昨天买的，不是想维修，我想了解能不能退。',expected:'return'},
 {message:'不想重新设计了，能回收换成钱吗？',context:['我有一条玉想改款'],expected:'recycle'},
 {message:'朋友快过生日，想挑份首饰给她。',expected:'gift'},
 {message:'给自己上班戴，想选细一点的手链。',expected:'selection'},
 {message:'我家孩子这周考试，我比他还紧张。',expected:'chat'},
 {message:'今天先不选了，也不用再推荐。',expected:'pause'},
 {message:'既想知道怎么改款，也想了解回收，两个都想问。',expected:'unknown'}
];
const dir=new URL('./evidence/service-topics-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);const r={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),sourceHash:createHash('sha256').update(readFileSync(new URL('./service-topics.mjs',import.meta.url))).digest('hex'),cases,rows:[],method:'Twelve pre-frozen synthetic development cases, single actual local-model sample, no retry or model switch. Context is customer text, not confirmed profile. Exact quotes verify provenance only. This test does not prove automatic service routing or general accuracy.',productionAccepted:false,status:'running'};const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(r,null,2));save();
const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(p,b,o)=>localOllamaJSON(p,p==='/api/chat'?{...b,format:TOPIC_SCHEMA}:b,o)});
try{const ps=await fetch('http://127.0.0.1:11434/api/ps').then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident');for(const c of cases){const row={...c};r.rows.push(row);try{row.request=topicRequest(client.modelName,c.message,c.context);row.raw=await client.complete(row.request);row.proposal=parseTopic(c.message,row.raw);row.correct=row.proposal.topic===c.expected;}catch(e){row.error=e.message;row.correct=false;}save();console.log(JSON.stringify({message:c.message,expected:c.expected,actual:row.proposal?.topic,error:row.error}));}r.status='completed';r.summary={correct:r.rows.filter(x=>x.correct).length,total:cases.length,invalid:r.rows.filter(x=>x.error).length};save();console.log(JSON.stringify({summary:r.summary,evidence:dir.href}));}finally{client.close();}
