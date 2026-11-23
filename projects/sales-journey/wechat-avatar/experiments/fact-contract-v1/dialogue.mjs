import {randomUUID} from 'node:crypto';
import {receive} from './conversation.mjs';
const kinds=['order','inventory','delivery_today','owner_presence'];
const labels={order:'订单进度',inventory:'有没有货',delivery_today:'今天能否拿到',owner_presence:'店主是否在店'};
const keys=(v,allowed)=>v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).every(k=>allowed.includes(k));
const validTopics=t=>Array.isArray(t)&&t.length>0&&t.length<=4&&new Set(t).size===t.length&&t.every(k=>kinds.includes(k));

// Actor IDs must come from authenticated server context, never an HTTP/model body.
export class ClarifyingDialogue {
 #scope;#options;#pending=null;#busy=false;
 constructor({scope,ledger,model,complete,resolveBinding,lookupEvidence,clock=Date.now,ttlMs=300000}) {
  if(!scope||!['storeId','customerId','conversationId'].every(k=>typeof scope[k]==='string'&&scope[k]))throw Error('Invalid scope');
  if(!Number.isFinite(ttlMs)||ttlMs<=0)throw Error('Invalid expiry');
  this.#scope=Object.freeze({...scope});this.#options={ledger,model,complete,resolveBinding,lookupEvidence,clock,ttlMs};
 }
 async turn(actor,event) {
  if(!actor||!['storeId','customerId','conversationId'].every(k=>actor[k]===this.#scope[k]))throw Error('Forbidden conversation');
  if(this.#busy)throw Error('Conversation busy');
  if(!keys(event,['type','message','token','topics'])||!['message','confirm','cancel'].includes(event.type))throw Error('Invalid event');
  if(event.type==='message'&&(!keys(event,['type','message'])||typeof event.message!=='string'||!event.message.trim()||event.message.length>800))throw Error('Invalid message event');
  if(event.type==='confirm'&&(!keys(event,['type','token','topics'])||!validTopics(event.topics)))throw Error('Invalid confirmation');
  if(event.type==='cancel'&&!keys(event,['type']))throw Error('Invalid cancel');
  event=structuredClone(event);
  // Explicit numbered menu replies only; this is a control protocol, not NLP.
  if(event.type==='message'&&this.#pending&&/^[1-4]$/.test(event.message.trim())) {
   event={type:'confirm',token:this.#pending.token,topics:[kinds[Number(event.message.trim())-1]]};
  }
  this.#busy=true;
  try {
   if(event.type==='cancel'){this.#pending=null;return{reply:'好，先不继续这个问题。你想换个话题也可以。',choices:[],sent:false,source:'clarification_cancelled'};}
   if(event.type==='confirm') {
    if(!this.#pending||event.token!==this.#pending.token||this.#pending.expiresAt<=this.#options.clock())throw Error('Stale or expired confirmation');
   }
   // Any accepted new turn consumes the previous choice before asynchronous work.
   this.#pending=null;
   const binding=await this.#options.resolveBinding(this.#scope);
   if(!binding||binding.storeId!==this.#scope.storeId||binding.customerId!==this.#scope.customerId)throw Error('Binding scope mismatch');
   const snapshot=Object.freeze({storeId:binding.storeId,customerId:binding.customerId,subjects:Object.freeze({...binding.subjects})});
   const confirmed=event.type==='confirm';
   const topics=confirmed?[...event.topics]:null;
   const result=await receive({message:confirmed?'客户通过受信会话选项确认问题方向':event.message,
    model:this.#options.model,ledger:this.#options.ledger,binding:snapshot,
    complete:confirmed?async()=>({choices:[{message:{content:JSON.stringify({topics,socialAct:null})}}]}):this.#options.complete,
    lookupEvidence:this.#options.lookupEvidence});
   // No confidence inferred from wording: always offer a voluntary correction path.
   const pending={token:randomUUID(),expiresAt:this.#options.clock()+this.#options.ttlMs};
   this.#pending=pending;
   const ambiguous=!result.route||(!result.route.topics.length&&result.route.socialAct==='clarify');
   return {...result,reply:ambiguous?'你想了解哪方面？可以回复：1 订单进度，2 有没有货，3 今天能否拿到，4 店主是否在店。也可以继续补充。':result.reply,
    choices:kinds.map((kind,i)=>({kind,label:labels[kind],textSelection:String(i+1)})),clarification:{...pending,required:ambiguous},
    confirmedByCustomer:confirmed,modelRoutingCalled:!confirmed};
  } finally {this.#busy=false;}
 }
}
