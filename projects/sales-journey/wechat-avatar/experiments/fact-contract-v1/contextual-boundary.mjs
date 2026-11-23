import {focusRequest,parseFocus,focusedRouteRequest,parseFocusedRoute} from './two-step-topic.mjs';
const topicForAction={compare_daily:'selection',compare_detail:'selection',compare_outline:'selection',gift_habits:'gift',repair_preserve:'repair',repair_receipt:'repair',repair_limits:'repair',care_limits:'care',return_limits:'return',recycle_limits:'recycle',remodel_goal:'remodel',remodel_limits:'remodel'};
// Previous context is a program-rendered discussion, never evidence of stock, policy or business capability.
export async function contextualUnderstanding({model,message,history,previousAction,completeFocus,completeRoute}){
 const first=focusRequest(model,message);first.messages[1].content=JSON.stringify({recentConversation:history,currentMessage:message});
 if(Buffer.byteLength(JSON.stringify(first.messages),'utf8')>6800)throw Error('Invalid contextual focus size');
 const focus=parseFocus(message,await completeFocus(first));let projectedTopic={chat:'chat',stop:'pause',meta:'unknown',unclear:'unknown'}[focus.kind];
 if(focus.kind==='request'){
  const next=focusedRouteRequest(model,focus),topic=topicForAction[previousAction];
  next.messages[0].content+=' 如果本轮是在追问刚才说的方向、可行性或时间，可参考previousDiscussion确定指代。当前明确换对象、换任务或转闲聊时，以本轮为准。previousDiscussion只证明刚才聊了什么，不证明有商品、能力或已办理。';
  next.messages[1].content=JSON.stringify({currentRequestQuote:focus.quote,previousDiscussion:topic?{topic,actionId:previousAction,reply:history.at(-1)?.role==='assistant'?history.at(-1).content:null}:null});
  if(Buffer.byteLength(JSON.stringify(next.messages),'utf8')>6800)throw Error('Invalid contextual route size');
  projectedTopic=parseFocusedRoute(await completeRoute(next));
 }
 return{focus,projectedTopic,requiresHumanReview:true,semanticVerified:false,sent:false,profileChanged:false};
}
