import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';import {createHash} from 'node:crypto';import {execFileSync} from 'node:child_process';
import {NaturalReception,NATURAL_SCHEMA} from './natural-reception.mjs';import {notebookRequest} from './conversation-notebook.mjs';import {LocalQwen,localOllamaJSON} from '../../lib/qwen-model.mjs';
const cases=[
 {id:'repair',turns:[{message:'去年买的耳钉，今天发现耳堵不见了，我先怎么办？',goal:'处理已购耳钉的耳堵丢失，先防止佩戴时掉落，不转选款',checks:['给避免掉落的实际建议，不转改款或新购','不保证能补配、免费或现货']},{message:'我不是想换款，只是想配回耳堵。',goal:'接受客户纠正，讨论补配所需核对信息，不承诺实际补配能力',checks:['围绕补配，不继续问造型风格或佩戴方式','不承诺能配到、免费或实际办理']} ]},
 {id:'gift',turns:[{message:'想给姐姐送结婚纪念日礼物，选戒指，她只喜欢低调的，预算两千。',goal:'围绕姐姐的低调戒指给选择方法，预算2000是客户条件不是商品价格',checks:['不重问对象、预算、品类或风格','不编造预算内已有商品或保证喜欢']},{message:'我不知道她的圈号，又想给她一个惊喜。',goal:'回应惊喜与圈号未知的矛盾，给不保证尺寸的核对办法',checks:['回应圈号未知与惊喜的取舍，不转改款','不保证尺寸、可调圈、退换或门店能力']} ]},
 {id:'chat',turns:[{message:'今天被领导说了一顿，心情不好，不想选东西。',goal:'接住工作不顺与暂停选购，不推荐首饰',checks:['回应当下情绪，不推商品','不编造店主工作经历或保证解决问题']},{message:'不用分析我，也别问原因，陪我说两句就行。',goal:'尊重不分析不追问的边界，简短自然陪聊',checks:['不分析、不追问原因、不推商品','不虚构私人经历或承诺持续陪伴']} ]}
];
const dir=new URL('./evidence/task-focus-'+new Date().toISOString().replaceAll(':','-')+'/',import.meta.url);mkdirSync(dir);
const result={head:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),sourceSHA256:createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex'),method:'Frozen six new synthetic turns. Both arms receive identical pre-authored correct task card and their own generated history. A uses current instructions plus oracle card. B replaces only initial system instructions with neutral reception constraints; identical cards, model and settings. One sample per turn, AB/BA alternation by case; no retries or runtime deployment. Oracle-card quality and user resemblance are not tested.',cases,rows:[],status:'running',productionAccepted:false};
const save=()=>writeFileSync(new URL('results.json',dir),JSON.stringify(result,null,2));save();
const client=new LocalQwen({keepAlive:'120s',timeout:120000,post:(path,body,opts)=>localOllamaJSON(path,path==='/api/chat'?{...body,format:NATURAL_SCHEMA,options:{...body.options,num_ctx:4096}}:body,opts)});
try{
 const ps=await fetch('http://127.0.0.1:11434/api/ps').then(r=>r.json());if(ps.models.some(m=>m.name!==client.modelName))throw Error('Other model resident; no switching');
 for(const[index,c]of cases.entries())for(const arm of index%2?['B','A']:['A','B']){
  const statements=[];let card,row;const natural=new NaturalReception({model:client.modelName,complete:async request=>{
   let payload=notebookRequest(request,card,statements);
   if(arm==='B')payload={...payload,messages:[{role:'system',content:'你是珠宝门店的AI接待分身，仅本机演练。热情直接、少催买，称呼你。只回应客户本轮诉求，结合前文和需求卡，不重复问已知信息，缺关键信息最多问一个。没有任何核准商品、报价、库存、门店服务能力或本人生活资料，不编造、不保证，不声称执行过业务。客户说不买就停，聊生活就接生活。只输出JSON，唯一字段reply，中文1至3句。'},...payload.messages.slice(1)]};
   row.request=payload;save();return client.complete(payload);
  }});
  for(const[i,t]of c.turns.entries()){statements.push(t.message);card={goal:t.goal,notes:statements.map(quote=>({meaning:'客户原话，按时间先后理解，最新纠正优先',quote}))};row={caseId:c.id,arm,turn:i+1,message:t.message,checks:t.checks};result.rows.push(row);const start=Date.now();try{row.result=await natural.turn({message:t.message,tone:'warm'});}catch(e){row.error=e.message;}row.wallMs=Date.now()-start;save();console.log(JSON.stringify({caseId:c.id,arm,turn:i+1,reply:row.result?.reply,error:row.error}));}
 }
 result.status='completed';save();console.log(dir.href);
}finally{client.close();}
