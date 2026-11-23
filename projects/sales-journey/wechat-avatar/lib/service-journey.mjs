import{rehearsalTopics}from'./conversation-understanding.mjs';
// A reversible local conversation aid, never a customer label, sale receipt, or satisfaction score.
export const SERVICE_JOURNEY_VERSION=1;
const stages={
 pause:['尊重暂停','确认这轮先到这里，给客户空间。','不追问，不追加购买理由，不安排主动联系。'],
 after_sales:['先解决使用或售后问题','接住具体问题，说明需要核对的信息；不继续推销。','政策和处理结果待核实，不声称已受理、转交、退款或修好。'],
 verify:['核对购买所需信息','先弄清客户问的具体商品或业务资料，核实后再谈购买。','没有核准资料不报价格、不保证库存、不声称已下单或安排服务。'],
 ready:['把购买前一步讲清楚','客户有意继续，优先说明还缺哪一项具体商品信息；不要重新问已经说过的偏好。','当前没有商品目录或订单执行结果；不能凭意向当成成交，不承诺发图、留货或付款。'],
 concern:['把顾虑讲明白','直接回答本轮顾虑，给一个客户能自己判断的办法；不是继续夸产品。','不反驳顾虑，不推测购买心理，不用稀缺或人情压力催决定。'],
 reassure:['先让客户放心问','明确不麻烦，让客户放心说清问题。','问多不代表想买；不要把安慰店主的责任交回客户。'],
 compare:['帮助缩小选择','结合已明确的用途和偏好，讲清一个选择差别及理由。','缺图片就说缺图；没有商品资料只讲方向，不装作已经找到款。'],
 clarify:['只补一个关键信息','优先补清影响选择的一项未知信息；已经说清的内容不重问。','不连续盘问，不凭空指定商品或客户习惯。'],
 service:['回应当前诉求','先回答这句话真正需要的帮助，再决定是否推进下一步。','不要把每次聊天都变成荐款，也不把结束当成失败。']
};
export function serviceJourney(question,{needs={},guidance=null,requiresVerification=false}={}){
 const topics=rehearsalTopics(question);let key='service';
 if(topics.includes('停止联系')||guidance?.key==='pause')key='pause';
 else if(topics.includes('售后与争议'))key='after_sales';
 else if(requiresVerification||topics.length)key='verify';
 else if(/想买|决定买|就买|怎么买|怎么下单|怎么购买|怎么付款|怎么付钱|要下单|下一步怎么买/u.test(question)&&!/(?:不想|不打算|不决定|先不|暂不|别催我)买/u.test(question))key='ready';
 else if(guidance?.key==='reassure')key='reassure';
 else if(guidance?.key==='deliberate'||/怕选错|担心|顾虑|怕不|怕买|怕.{0,5}闲置/u.test(question))key='concern';
 else if(['repair','choose','compare_shops','advance','update_preferences'].includes(guidance?.key)||/怎么选|怎么挑|选哪个|区别|区别在哪|具体.{0,4}说|讲清|说具体|帮我选/u.test(question))key='compare';
 else if(/第一次|初次|先了解/u.test(question)||!needs.category?.values?.length)key='clarify';
 const[label,objective,boundary]=stages[key];
 return{version:SERVICE_JOURNEY_VERSION,key,label,objective,boundary,source:'system_designed_local_service_aid',customerQuote:question,completion:'not_assessed',customerSatisfaction:null,saleConfirmed:false};
}
export function journeyPrompt(journey){return journey?{stage:journey.key,objective:journey.objective,boundary:journey.boundary}:null;}
