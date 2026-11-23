import {randomUUID} from 'node:crypto';
const CHOICES=[{action:'continue_selection',label:'我想继续选款'},{action:'change_topic',label:'先聊点别的'},{action:'pause_sales',label:'今天先不看了'}];
const QUESTION='我确认一下，你这会儿是想继续看看款式，还是先聊点别的？不着急决定。';
const ACK=/^(谢谢你?|多谢|感谢|好的?|嗯|收到|明白了|知道了)[。！!～~]*$/u;
const THANKS=/^(谢谢你?|多谢|感谢)/u;
export const isSimpleAcknowledgement=text=>typeof text==='string'&&ACK.test(text.trim());
export class GuidedReception{
 #intent;#guide;#pending=null;#clock;#confirmed=null;
 constructor({intent,guide,clock=Date.now}){this.#intent=intent;this.#guide=guide;this.#clock=clock;}
 message(text){
  if(typeof text!=='string'||!text.trim()||text.length>800)throw Error('Invalid reception message');
  const normalized=text.trim();
  if(ACK.test(normalized)){
   if(this.#pending)return this.result();
   if(this.#confirmed===JSON.stringify(this.#intent.get())){
    const current=this.#intent.get(),reply=(THANKS.test(normalized)?'不客气。':'好。')+(current.paused?'按你的节奏来，需要时再聊。':'你慢慢看，有想问的就说。');
    const result=this.result();return{...result,reply,selection:{...result.selection,nextField:null,reply},responseMode:'acknowledgement',stateChanged:false};
   }
  }
  this.#confirmed=null;
  this.#pending={token:randomUUID(),expiresAt:this.#clock()+300000,revision:this.#guide.preview().revision,intent:JSON.stringify(this.#intent.get())};
  return this.result();
 }
 choose(event){
  if(!event||event.type!=='choice'||Object.keys(event).some(k=>!['type','token','action'].includes(k))||!CHOICES.some(c=>c.action===event.action))throw Error('Invalid reception choice');
  const p=this.#pending;
  if(!p||event.token!==p.token||p.expiresAt<=this.#clock()||p.revision!==this.#guide.preview().revision||p.intent!==JSON.stringify(this.#intent.get()))throw Error('Stale reception choice');
  this.#intent.confirmSimulatedChoice(event.action);this.#pending=null;this.#confirmed=JSON.stringify(this.#intent.get());return this.result();
 }
 cancel(){if(this.#pending)this.#intent.control('pause_sales');this.#pending=null;this.#confirmed=null;return this.result();}
 result(){const plan=this.protect(this.#intent.protect(this.#guide.preview()));return{selection:plan,reply:plan.reply,clarification:this.#pending?{token:this.#pending.token,expiresAt:this.#pending.expiresAt}:null,choices:this.#pending?CHOICES.map(c=>({...c})):[],simulation:true,sent:false,modelCalls:0};}
 protect(plan){
  if(!this.#pending)return plan;
  const stale=this.#pending.expiresAt<=this.#clock()||this.#pending.revision!==this.#guide.preview().revision||this.#pending.intent!==JSON.stringify(this.#intent.get());
  return{...plan,nextField:null,awaitingClarification:true,reply:stale?'刚才的确认已过期或信息有变化。你重新告诉我想继续看，还是先聊点别的，我再确认一次。':QUESTION};
 }
}
