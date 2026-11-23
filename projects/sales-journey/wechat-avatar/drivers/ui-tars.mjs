import { NativeBridge } from '../lib/native-bridge.mjs';
import { hash } from '../lib/policy.mjs';

function exactly(nodes,predicate,reason){const found=nodes.filter(predicate);if(found.length!==1)throw new Error(reason);return found[0];}
function inside(point,box,bounds){return point.x>=box.x-bounds.x && point.x<=box.x+box.width-bounds.x && point.y>=box.y-bounds.y && point.y<=box.y+box.height-bounds.y;}

export function inspectFrame(raw,profile) {
  if(!raw||!Array.isArray(raw.nodes)||raw.humanActive!==false)throw new Error('HUMAN_OR_WINDOW_UNCERTAIN');
  const identity=exactly(raw.nodes,(n)=>n.identifier===profile.identityIdentifier && (n.value===profile.identityValue||n.title===profile.identityValue),'IDENTITY_NOT_UNIQUE');
  const input=exactly(raw.nodes,(n)=>n.identifier===profile.inputIdentifier && ['AXTextArea','AXTextField'].includes(n.role),'INPUT_NOT_UNIQUE');
  const send=exactly(raw.nodes,(n)=>n.identifier===profile.sendIdentifier && n.role==='AXButton' && (n.title==='发送'||n.description==='发送'),'SEND_BUTTON_NOT_UNIQUE');
  const nodes=raw.nodes.filter((n)=>n.identifier && n.identifier.startsWith(profile.messagePrefix));
  if(!nodes.length||new Set(nodes.map((n)=>n.identifier)).size!==nodes.length)throw new Error('STABLE_MESSAGE_IDS_REQUIRED');
  const messages=nodes.map((n)=>{
    const direction=n.description===profile.incomingDescription?'in':n.description===profile.outgoingDescription?'out':null;
    if(!direction||typeof n.value!=='string'||!n.value.trim()||n.value.length>2000)throw new Error('MESSAGE_DIRECTION_OR_TEXT_UNCERTAIN');
    return {eventId:hash(`${profile.account}:${profile.conversation}:${n.identifier}`),customerId:profile.customerId,direction,text:n.value};
  });
  const latest=messages.filter((m)=>m.direction==='in').at(-1);if(!latest)throw new Error('LATEST_CUSTOMER_MESSAGE_REQUIRED');
  return {account:profile.account,conversation:profile.conversation,name:profile.name,inputEmpty:input.value==='',humanActive:false,latestCustomerText:latest.text,latestCustomerEventId:latest.eventId,messages,raw,input,send,identity};
}

export function createDriver({post,bridge=new NativeBridge(),clock=()=>Date.now()}={}) {
  let profile,frame,point,epoch,initialized=false;const seen=new Set();
  const load=async()=>{profile=await post('/api/device/profile',{});if(!profile)throw new Error('DESKTOP_PROFILE_REQUIRED');};
  const capture=async()=>{
    if(!profile)await load();
    const raw=await bridge.call('snapshot');
    if(epoch!==undefined&&raw.epoch!==epoch)throw new Error('HUMAN_INTERVENED_REARM_REQUIRED');
    epoch=raw.epoch;frame=inspectFrame(raw,profile);
    if(point && point.eventId!==frame.latestCustomerEventId){point=null;throw new Error('CUSTOMER_MESSAGE_CHANGED');}
    if(!initialized){for(const m of frame.messages)seen.add(m.eventId);initialized=true;}
    return {...frame,messages:frame.messages.filter((m)=>!seen.has(m.eventId))};
  };
  return {
    capture,
    accepted(message){seen.add(message.eventId);},
    async prepare(){
      if(!frame||!frame.inputEmpty)throw new Error('INPUT_NOT_EMPTY');
      const before=frame;
      const located=await post('/api/device/vision/locate',{image:before.raw.image,customerId:profile.customerId});
      const after=await capture();
      if(JSON.stringify(before.raw.bounds)!==JSON.stringify(after.raw.bounds)||before.latestCustomerEventId!==after.latestCustomerEventId||before.raw.windowID!==after.raw.windowID||!after.input.rect||!inside(located,after.input.rect,after.raw.bounds))throw new Error('GROUNDING_OR_WINDOW_CHANGED');
      point={...located,readyAt:clock(),eventId:after.latestCustomerEventId};
    },
    async execute(task){
      if(!frame||!point||clock()-point.readyAt>5000||clock()>=task.grantExpires||hash(task.text)!==task.hash||task.target.account!==profile.account||task.target.conversation!==profile.conversation||task.target.name!==profile.name||!frame.inputEmpty||clock()-frame.raw.at>2000)throw new Error('EXECUTION_CONTEXT_STALE');
      const result=await bridge.call('send',{frameId:frame.raw.frameId,text:task.text,textHash:task.hash,expires:task.grantExpires,x:point.x,y:point.y,inputNodeId:frame.input.nodeId,sendNodeId:frame.send.nodeId,identityNodeId:frame.identity.nodeId,identityValue:profile.identityValue,messagePrefix:profile.messagePrefix,outgoingDescription:profile.outgoingDescription});
      if(result.status==='observed_sent'&&result.outgoingId)seen.add(hash(`${profile.account}:${profile.conversation}:${result.outgoingId}`));
      point=null;return result;
    },
    async stop(){point=null;bridge.stop();},
    async close(){bridge.close();},
  };
}
