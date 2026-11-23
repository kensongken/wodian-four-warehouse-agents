// Experimental natural drafts, never messages or verified merchant capabilities.
import {twoStepTopic} from './two-step-topic.mjs';
import {faqIntents} from '../../lib/knowledge-library.mjs';
export const RECEPTION_SCHEMA={type:'object',additionalProperties:false,required:['reply'],properties:{reply:{type:'string'}}};
const voice=['你可以先试试','你平时穿戴是怎样的','没事，你先考虑，但是真的很适合你','可以看看这个款式'];
export class TwoStepReception{
 #history=[];#model;#focus;#route;#reply;
 constructor({model,completeFocus,completeRoute,completeReply}){this.#model=model;this.#focus=completeFocus;this.#route=completeRoute;this.#reply=completeReply;}
 async turn({message}){
  if(typeof message!=='string'||!message.trim()||message.length>800)throw Error('Invalid reception message');
  let understanding=null,response,modelCalls=0;
  const facts=faqIntents(message),labels={address:'门店地址',hours:'营业时间与能否到店',try_on:'试戴安排',name:'门店名称'};
  if(facts.length){response={choices:[{message:{content:JSON.stringify({reply:`${facts.map(k=>labels[k]).join('、')}目前没有核准资料，需要先核实，暂时不能准确答复。`})}}]};}
  else{
   understanding=await twoStepTopic({model:this.#model,message,context:this.#history.filter(m=>m.role==='user').map(m=>m.content).slice(-4),completeFocus:r=>{modelCalls++;return this.#focus(r);},completeRoute:r=>{modelCalls++;return this.#route(r);}});
   const instructions='你是珠宝店的AI接待分身，只在本机演练。只输出JSON字段reply，中文1至3句。像店主平常发微信：热情、直接、简短，少催买，称呼你。先接住本轮实际意思，再给一个有用的选择或服务下一步；没有真正缺的信息就不追问，最多问一个。不重复问前文已知条件。前文店主回复可能有错，客户最新纠正优先，不把旧回复当商品事实。\n没有核准商品、图片、价格、库存、门店能力、售后政策或本人生活资料。不声称找到款或查过系统，不保证适合、喜欢、能修、可退或已办理。预算不是商品价格；涉及具体费用、尺寸处理或工期须核实，不给临时固定首饰等冒险建议。不能因为想促成销售就编造事实。\n客户说停就自然收住，不追加推荐或问题；说家事先接家事，不分析隐私，不编造自己的经历。客户重新开始选购就接最新需求。方向未定时帮客户理清取舍，不替她决定。理解结果是模型建议，不是已确认的客户事实；结合原话和双方前文判断，不机械复述分类词或内部流程。\n以下是本人原话，仅供学习短句和直接的感觉，不是本轮答案或门店能力证明；其中“适合”与“看看这个款式”不能当已验证结论或实有商品：'+JSON.stringify(voice)+'。';
   const messages=[{role:'system',content:instructions},...this.#history,{role:'system',content:'本轮机器理解建议（可能有误，不执行动作）：'+JSON.stringify({kind:understanding.focus.kind,quote:understanding.focus.quote,topic:understanding.projectedTopic})},{role:'user',content:message}];
   if(Buffer.byteLength(JSON.stringify(messages),'utf8')>6800)throw Error('Invalid reception context size');
   modelCalls++;response=await this.#reply({model:this.#model,response_format:{type:'json_object'},messages});
  }
  const v=JSON.parse(response.choices?.[0]?.message?.content);
  if(!v||Array.isArray(v)||Object.keys(v).join(',')!=='reply'||typeof v.reply!=='string'||!v.reply.trim()||v.reply.length>500)throw Error('Invalid reception reply');
  const reply=v.reply.trim();this.#history=[...this.#history,{role:'user',content:message},{role:'assistant',content:reply}].slice(-6);
  return{reply,understanding,modelCalls,simulation:true,sent:false,profileChanged:false,businessFactsVerified:false,ownerVoiceVerified:false,historyPersisted:false,responseMode:facts.length?'fact_unknown':'two_step_natural_draft'};
 }
}
