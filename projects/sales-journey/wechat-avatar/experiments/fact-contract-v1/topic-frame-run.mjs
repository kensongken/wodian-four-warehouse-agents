import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';import {execFileSync} from 'node:child_process';import {createHash} from 'node:crypto';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';import {topicRequest,parseTopic,TOPIC_SCHEMA} from './service-topics.mjs';import {frameRequest,parseFrame,FRAME_SCHEMA} from './topic-frame.mjs';
const cases=[
 {message:'今天路上堵了两个小时，回到家只想躺着。',expected:'chat',split:'known_failure'},
 {message:'下班脑袋嗡嗡的，回家只想坐一会儿。',expected:'chat'},
 {message:'今天不想动，累得不行。',expected:'chat'},
 {message:'家里的事先不想细讲，我们聊点别的吧。',expected:'chat'},
 {message:'最近孩子考试，我也跟着操心。',expected:'chat'},
 {message:'我不是不想买，只是今天有点累。',expected:'chat'},
 {message:'今天先不挑了，明天再说。',expected:'pause'},
 {message:'心烦，首饰今天先不买了。',expected:'pause'},
 {message:'别再推荐了，今天先不选。',expected:'pause'},
 {message:'刚才说先不买，现在我们接着挑戒指吧。',expected:'selection'},
 {message:'我有点累，不过你继续说这条的保养方法。',expected:'care'},
 {message:'朋友说她不想选了，我还是想看看手链。',expected:'selection'},
 {message:'如果我说今天不买，你会怎么回复？',expected:'unknown'},
 {message:'不是改款，耳堵丢了想补一个。',expected:'repair'},
 {message:'不是维修，我想了解退换规则。',expected:'return'},
 {message:'给姐姐挑结婚纪念日礼物，她喜欢简洁的。',expected:'gift'}
];
const dir=new URL('./evidence/topic-frame-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);let schema=TOPIC_SCHEMA;const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(p,b,o)=>localOllamaJSON(p,p==='/api/chat'?{...b,format:schema}:b,o)});
const result={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),cases,hashes:Object.fromEntries(['service-topics.mjs','topic-frame.mjs','topic-frame-run.mjs'].map(n=>[n,createHash('sha256').update(readFileSync(new URL('./'+n,import.meta.url))).digest('hex')])),method:'Same local model and settings. A=current one-axis topic including pause. B=content topic plus independent shopping intent, projected to same topic labels. One known failure and 15 developer-authored new synthetic cases; one sample per arm per case, alternating order, no retries. B changes schema and instructions together; cannot attribute outcome to either independently. Quotes only prove provenance. No runtime change or real customer actions.',rows:[],status:'running',productionAccepted:false};const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
try{const ps=await fetch('http://127.0.0.1:11434/api/ps',{signal:AbortSignal.timeout(5000)}).then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident');for(const[i,c]of cases.entries())for(const arm of i%2?['B','A']:['A','B']){const row={...c,arm};result.rows.push(row);schema=arm==='A'?TOPIC_SCHEMA:FRAME_SCHEMA;try{row.request=arm==='A'?topicRequest(client.modelName,c.message):frameRequest(client.modelName,c.message);row.raw=await client.complete(row.request);row.parsed=arm==='A'?parseTopic(c.message,row.raw):parseFrame(c.message,row.raw);row.actual=arm==='A'?row.parsed.topic:row.parsed.projectedTopic;row.correct=row.actual===c.expected;}catch(e){row.error=e.message;row.correct=false;}save();console.log(JSON.stringify({arm,message:c.message,expected:c.expected,actual:row.actual,error:row.error}));}result.status='completed';result.summary={};for(const arm of ['A','B']){const rows=result.rows.filter(x=>x.arm===arm);result.summary[arm]={correct:rows.filter(x=>x.correct).length,total:rows.length,falsePause:rows.filter(x=>x.actual==='pause'&&x.expected!=='pause').length,invalid:rows.filter(x=>x.error).length};}save();console.log(JSON.stringify({evidence:dir.href,summary:result.summary}));}finally{client.close();}
