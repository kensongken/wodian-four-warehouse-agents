import {Problem} from './engine.mjs';
// Whole customer utterances only; never inherit model-made summaries or client-supplied notes.
export function conversationContext(trials,profileId,previousTrialId){
 if(previousTrialId!=null&&typeof previousTrialId!=='string')throw new Problem('请选择有效的上一轮试聊。');
 const chain=[],seen=new Set();let previous=previousTrialId,olderUnavailable=false;
 while(previous&&chain.length<10){
  if(seen.has(previous))throw new Problem('试聊记录关联异常，请开始新的试聊。',409);seen.add(previous);
  const prior=trials.find(t=>t.id===previous);
  if(!prior||prior.profileId!==profileId||prior.blocked){if(chain.length<3)throw new Problem('上一轮已过期、被拦截或口吻已更新，请开始新的试聊。',409);olderUnavailable=true;break;}
  chain.push(prior);previous=prior.previousTrialId;
 }
 const history=chain.slice(0,3).reverse().map(t=>({question:t.question,reply:t.reply}));
 if(Buffer.byteLength(JSON.stringify(history),'utf8')>1800)throw new Problem('连续试聊内容过长，请开始新的试聊；本轮没有生成或发送消息。',409);
 const older=chain.slice(3).reverse(),declarative=older.filter(t=>!/[?？]|是不是|不知道|不确定|吗[。！!]*$/u.test(t.question));
 // Prioritize whole customer quotes about wearer, category and latest preference;
 // no inferred slot values or model summaries are persisted.
 const anchors=[
  declarative.findLast(t=>/给自己|自己戴|自己佩戴|自用|自戴|送妈妈|送母亲|送给|送人|送礼/u.test(t.question)),
  declarative.findLast(t=>/手链|手镯|项链|戒指|耳饰|耳环/u.test(t.question)),
  declarative.findLast(t=>/上班|通勤|日常|周末|聚会|细一点|宽一点|简约|显眼|喜欢|换成|改成|改主意/u.test(t.question))
 ].filter(Boolean);
 const selected=new Map();let bytes=2;
 for(const t of [...anchors,...older.toReversed()]){if(selected.has(t.id))continue;const note={trialId:t.id,question:t.question};const size=Buffer.byteLength(JSON.stringify(note),'utf8')+1;if(selected.size>=4||bytes+size>900)continue;selected.set(t.id,note);bytes+=size;}
 const notes=older.filter(t=>selected.has(t.id)).map(t=>selected.get(t.id));
 return{history,notes,olderSkipped:Math.max(0,chain.length-3)-notes.length,olderUnavailable,chainLimitReached:chain.length===10&&!!previous};
}
