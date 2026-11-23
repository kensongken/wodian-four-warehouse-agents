import {id,Problem} from './engine.mjs';
import {discoveryPlan} from './official-kf-discovery.mjs';
import {assess,extractNeeds} from './policy.mjs';

// Called only by the server-side, account-bound official receiver. Browser
// messages cannot claim this provenance or supply an official receipt key.
export class OfficialKfWorkbench {
  constructor(engine){this.engine=engine;}
  incoming({actorId,binding,key,message,discovery=false}){
    if(!/^[a-f0-9]{64}$/.test(binding)||!/^[a-f0-9]{64}$/.test(key)||message.msgtype!=='text'||typeof message.text?.content!=='string'||!message.text.content.trim()||message.text.content.length>2000)
      throw new Problem('官方客服消息不能同步到客户档案，接待已停止。',409);
    return this.engine.store.transact(s=>{
      const owner=s.users.find(u=>u.id===actorId&&u.active&&u.role==='owner');if(!owner)throw new Problem('接待负责人已失效。',403);
      const prior=s.messages.find(m=>m.officialReceiptKey===key&&m.direction==='in');
      if(prior){if(prior.text!==message.text.content||prior.provenance.binding!==binding)throw new Problem('官方消息标识与内容冲突。',409);return {customerId:prior.customerId,messageId:prior.id,customerVersion:s.customers.find(c=>c.id===prior.customerId)?.version};}
      let c=s.customers.find(c=>!c.deletedAt&&c.officialKf?.binding===binding);
      if(!c){c={id:id(),alias:'本人微信测试 · '+binding.slice(-6),assigneeId:owner.id,synthetic:true,ownerTest:true,stage:'discover',result:'ongoing',needs:[],candidates:[],products:[],version:1,locked:false,lockReason:'',optedOut:false,binding:null,officialKf:{binding,ownerTest:true},createdAt:this.engine.clock(),updatedAt:this.engine.clock()};s.customers.push(c);this.engine.audit(s,owner,'建立官方客服本人测试档案',c.id);}
      if(c.assigneeId!==owner.id)throw new Problem('本人测试档案已转交，先由老板核对归属。',409);
      this.engine.cancel(s,c.id,'官方客服有新消息');
      const incoming={id:id(),customerId:c.id,direction:'in',text:message.text.content,source:'official_kf_test',officialReceiptKey:key,at:message.send_time*1000,provenance:{kind:'official_kf_owner_test',binding,receivedAt:this.engine.clock()}};
      s.messages.push(incoming);c.version++;c.updatedAt=this.engine.clock();
      const evidence=discovery?s.messages.filter(m=>m.customerId===c.id&&m.direction==='in'&&m.source==='official_kf_test').slice(-12):[incoming];
      for(const message of evidence)for(const need of extractNeeds(message.text))if(![...c.needs,...c.candidates].some(n=>n.messageId===message.id&&n.kind===need.kind))c.candidates.push({...need,id:id(),messageId:message.id});
      const plan=discovery?discoveryPlan(incoming.text,c,s.settings):null;
      const decision=plan||assess(incoming.text,c,s.settings);
      const draft={id:id(),customerId:c.id,messageId:incoming.id,customerVersion:c.version,text:decision.reply,...decision,status:'draft',revision:1,policyVersion:s.settings.policyVersion,personaId:s.settings.personaId,at:this.engine.clock(),source:'official_kf_rule_suggestion',materialVersions:[...c.products]};
      s.drafts.push(draft);
      if(decision.risk==='human'){c.locked=true;c.lockReason=decision.reasons.join('、');s.officialKf??={receipts:[]};s.officialKf.humanLocked=true;this.remind(s,c,c.lockReason);}
      this.engine.audit(s,owner,'官方客服本人消息入档',c.id,{messageId:incoming.id});
      return {customerId:c.id,messageId:incoming.id,customerVersion:c.version};
    });
  }
  resume(actor,binding){
    this.engine.owner(actor);this.engine.store.transact(s=>{
      const c=s.customers.find(c=>!c.deletedAt&&c.officialKf?.binding===binding);if(!c)return;
      if(c.assigneeId!==actor.id)throw new Problem('客户已转交，不能恢复原测试接待。',409);
      c.locked=false;c.lockReason='';c.version++;this.engine.audit(s,actor,'老板解除本人官方测试人工锁',c.id);
    });
  }
  remind(s,c,reason){
    if(!s.reminders.some(r=>r.customerId===c.id&&r.type==='handoff'&&r.status==='open'))s.reminders.push({id:id(),customerId:c.id,type:'handoff',text:'查看官方客服原话，由负责人核实后接续。',reason,status:'open',due:this.engine.clock(),at:this.engine.clock(),version:1});
  }
  outcome({key,status,reason,text}){
    return this.engine.store.transact(s=>{
      const incoming=s.messages.find(m=>m.officialReceiptKey===key&&m.direction==='in');if(!incoming)return;
      const c=s.customers.find(c=>c.id===incoming.customerId&&!c.deletedAt);if(!c)return;
      if(status==='api_accepted'){
        if(typeof text!=='string'||!text)throw new Problem('官方受理回复正文缺失。',409);
        const prior=s.messages.find(m=>m.officialReceiptKey===key&&m.direction==='out');if(prior){if(prior.text!==text)throw new Problem('官方受理回复内容冲突。',409);return;}
        if(!s.messages.some(m=>m.officialReceiptKey===key&&m.direction==='out'))s.messages.push({id:id(),customerId:c.id,direction:'out',text,source:'official_kf_api_accepted',officialReceiptKey:key,at:this.engine.clock(),delivery:'api_accepted',provenance:{kind:'official_kf_owner_test',phoneVisibleConfirmed:false}});
        const draft=s.drafts.find(d=>d.messageId===incoming.id);if(draft){draft.text=text;draft.status='official_api_accepted';draft.source='official_kf_model_accepted';draft.revision++;delete draft.approval;}
      }else if(['human','unknown'].includes(status)){c.locked=true;c.lockReason=reason||'官方客服需人工处理';this.remind(s,c,c.lockReason);}
      c.updatedAt=this.engine.clock();
    });
  }
}
