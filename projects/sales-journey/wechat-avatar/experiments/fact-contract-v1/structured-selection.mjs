import {randomUUID} from 'node:crypto';import {isSimpleAcknowledgement} from './guided-reception.mjs';
import {SUPPORT_OPTIONS,supportReply} from './service-support.mjs';
const OPTIONS={
 recipient:[['self','给自己选'],['mother','送妈妈'],['friend','送朋友'],['other','送其他人']],
 category:[['bracelet','手链'],['necklace','项链'],['earrings','耳环'],['ring','戒指'],['undecided','品类还没定']],
 occasion:[['daily','日常戴'],['work','上班戴'],['party','聚会戴'],['birthday','生日礼物']],
 budget:[[500,'预算500元以内'],[1000,'预算1000元以内'],[1500,'预算1500元以内'],['private','预算先不说']],
 style:[['simple','偏简洁'],['bold','有设计感'],['unsure','风格还不确定']]
};
const EDIT_LABELS={recipient:'调整给谁选',category:'调整品类',occasion:'调整用途',budget:'调整预算',style:'调整风格'};
export class StructuredSelection{
 #guide;#intent;#reception;#clock;#pending=null;#review=null;#tone='plain';#support=null;#textEdit=null;
 constructor({guide,intent,reception,clock=Date.now}){this.#guide=guide;this.#intent=intent;this.#reception=reception;this.#clock=clock;}
 message(text){
  if(typeof text!=='string'||!text.trim()||text.length>800)throw Error('Invalid selection message');
  if(this.#pending&&isSimpleAcknowledgement(text))return this.result();
  if(this.#review&&isSimpleAcknowledgement(text)){const response=this.#reception.message(text);if(response.responseMode==='acknowledgement'){const menu=this.result();return{...menu,reply:response.reply,selection:{...menu.selection,nextField:null,reply:response.reply},responseMode:'acknowledgement',stateChanged:false};}}
  this.#pending=null;this.#review=null;this.#support=null;this.#textEdit=null;return this.#reception.message(text);
 }
 chooseIntent(event){const response=this.#reception.choose(event);this.#pending=null;this.#review=null;this.#support=null;this.#textEdit=null;if(event.action==='continue_selection'){this.#advance();return this.result();}return response;}
 beginTextEdit(token){this.#validateReview(token);this.cancel();this.#reception.message('模拟客户核对需求修改');this.#textEdit=JSON.stringify(this.#intent.get());return this.result();}
 assertTextEdit(){if(!this.#textEdit||this.#textEdit!==JSON.stringify(this.#intent.get()))throw Error('Stale text edit context');}
 completeTextEdit(){this.assertTextEdit();const q=this.#reception.message('模拟客户确认修改并继续');return this.chooseIntent({type:'choice',token:q.clarification.token,action:'continue_selection'});}
 get tone(){return this.#tone;}
 setTone(tone){this.#guide.preview(tone);this.#tone=tone;}
 #ticket(field,editing=false){return{token:randomUUID(),field,editing,revision:this.#guide.preview().revision,intent:JSON.stringify(this.#intent.get()),expiresAt:this.#clock()+300000};}
 #advance(){const plan=this.#guide.preview();this.#review=null;this.#support=null;if(plan.nextField&&OPTIONS[plan.nextField])this.#pending=this.#ticket(plan.nextField);else if(Object.keys(OPTIONS).every(k=>plan.needs[k]!==null))this.#review=this.#ticket(null);}
 #validate(token){const p=this.#pending;if(!p||p.token!==token||p.expiresAt<=this.#clock()||p.revision!==this.#guide.preview().revision||p.intent!==JSON.stringify(this.#intent.get())||this.#intent.get().paused)throw Error('Stale selection question');return p;}
 #validateReview(token){const p=this.#review;if(!p||p.token!==token||p.expiresAt<=this.#clock()||p.revision!==this.#guide.preview().revision||p.intent!==JSON.stringify(this.#intent.get())||this.#intent.get().paused)throw Error('Stale selection review');return p;}
 editStart(event){
  if(!event||event.type!=='edit_start'||Object.keys(event).some(k=>!['type','token','field'].includes(k))||!Object.hasOwn(OPTIONS,event.field))throw Error('Invalid edit request');
  this.#validateReview(event.token);this.#review=null;this.#support=null;this.#pending=this.#ticket(event.field,true);return this.result();
 }
 support(event){
  if(!event||event.type!=='support_choice'||Object.keys(event).some(k=>!['type','token','concern'].includes(k))||!SUPPORT_OPTIONS.some(c=>c.concern===event.concern))throw Error('Invalid service concern');
  this.#validateReview(event.token);
  const receipt={source:'explicit_simulated_customer_concern',concern:event.concern,needsChanged:false,revision:this.#guide.preview().revision};
  if(event.concern==='consider'){const response=this.interrupt({type:'service_interrupt',token:event.token,action:'pause_sales'}),reply=supportReply('consider',response.selection.needs,this.#tone);return{...response,reply,selection:{...response.selection,reply},responseMode:'service_support',supportReceipt:receipt};}
  this.#review=this.#ticket(null);this.#support=event.concern;return{...this.result(),supportReceipt:receipt};
 }
 chooseNeed(event){
  if(!event||event.type!=='need_choice'||Object.keys(event).some(k=>!['type','token','field','value'].includes(k)))throw Error('Invalid need choice');
  const p=this.#validate(event.token);if(event.field!==p.field||!OPTIONS[p.field].some(([value])=>value===event.value))throw Error('Invalid need value');
  const previousValue=this.#guide.preview().needs[p.field],plan=this.#guide.update({expectedRevision:p.revision,confirmed:true,patch:{[p.field]:event.value}});this.#pending=null;this.#advance();
  const result=this.result(),changed=previousValue!==event.value;
  if(p.editing&&p.field==='recipient'&&changed){const note='已更换佩戴者。预算、品类和用途暂沿用原条件，风格重新确认；其他条件也可以再调整。';result.reply=note+result.reply;result.selection={...result.selection,reply:result.reply};}
  return{...result,needChoiceReceipt:{source:'explicit_simulated_customer_selection',field:p.field,previousValue,value:event.value,changed,revision:plan.revision}};
 }
 interrupt(event){
  if(!event||event.type!=='service_interrupt'||Object.keys(event).some(k=>!['type','token','action'].includes(k))||!['pause_sales','change_topic'].includes(event.action))throw Error('Invalid selection interruption');
  if(this.#review?.token===event.token)this.#validateReview(event.token);else this.#validate(event.token);this.#pending=null;this.#review=null;this.#support=null;
  // The UI choice was validated above. Reuse the existing token-protected intent path.
  const q=this.#reception.message('模拟客户点选中止选款');return this.#reception.choose({type:'choice',token:q.clarification.token,action:event.action});
 }
 cancel(){if(this.#pending||this.#review)this.#intent.control('pause_sales');this.#pending=null;this.#review=null;this.#support=null;this.#textEdit=null;return this.#reception.cancel();}
 clearNeedQuestion(){if(this.#pending||this.#review||this.#textEdit)this.cancel();}
 protect(plan){
  if(this.#textEdit){try{this.assertTextEdit();}catch{return{...plan,nextField:null,awaitingClarification:true,reply:'这次修改确认已失效，请重新整理后核对。'};}return{...plan,nextField:null,awaitingClarification:true,reply:'先核对这次想改的内容；确认前原需求保持不变。'};}
  if(!this.#pending){if(this.#support){try{this.#validateReview(this.#review?.token);}catch{return{...plan,nextField:null,awaitingClarification:true,reply:'资料已变化或确认已过期，请重新确认这次想看的方向。'};}return{...plan,nextField:null,reply:supportReply(this.#support,plan.needs,plan.tone)};}return plan;}
  try{this.#validate(this.#pending.token);}catch{return{...plan,nextField:null,awaitingClarification:true,reply:'刚才的选款问题已过期或资料有变化。请重新发一条消息，我们再确认。'};}
  return{...plan,nextField:this.#pending.field,awaitingNeed:true,reply:this.#pending.editing?this.#guide.questionFor(this.#pending.field,plan.tone):plan.reply};
 }
 result(){const base=this.#reception.result(),selection=this.protect(this.#reception.protect(this.#intent.protect(this.#guide.preview(this.#tone))));if(!this.#pending){if(this.#review)return{...base,selection,reply:selection.reply,review:{token:this.#review.token,expiresAt:this.#review.expiresAt},choices:[...SUPPORT_OPTIONS.map(c=>({kind:'support',...c})),...Object.entries(EDIT_LABELS).map(([field,label])=>({kind:'edit',field,label})),{kind:'interrupt',action:'change_topic',label:'先聊点别的'},{kind:'interrupt',action:'pause_sales',label:'今天先不选了'}],responseMode:this.#support?'service_support':'selection_complete'};return{...base,selection,reply:selection.reply};}const p=this.#pending;return{...base,selection,reply:selection.reply,clarification:{token:p.token,expiresAt:p.expiresAt,kind:'need'},choices:[...OPTIONS[p.field].map(([value,label])=>({kind:'need',field:p.field,value,label})),{kind:'interrupt',action:'change_topic',label:'先聊点别的'},{kind:'interrupt',action:'pause_sales',label:'今天先不选了'}],responseMode:'structured_need'};}
}
