import {assess,extractNeeds,mandatoryTopics,normalize} from './policy.mjs';

// This pilot only asks preference questions. No merchandise facts or promises.
export function discoveryPlan(text,customer={},settings={}) {
  if(typeof text!=='string'||text.length>100||mandatoryTopics(text).length)return null;
  const n=normalize(text);
  const decision=assess(text,customer,settings);
  const combined=/^(?:我想|我|想|想要|我要)?(?:送给妈妈|送妈妈|送给朋友|送朋友|送礼|自己戴|自己佩戴|自用)[，,。]*(?:我)?(?:喜欢|想要|要)?(?:素一点的?|简约(?:素雅)?|有颜色的?)?[呀呢吧。！!？?]*$/u.test(n);
  const preference=/^(?:我|我想|想|要)?(?:喜欢|偏爱|偏好|选)?(?:小巧(?:一点|一些)?|存在感强(?:一点|一些)?|日常(?:戴|佩戴)?|聚会(?:戴|搭配)?)(?:的|款式)?[呀呢吧。！!？?]*$/u.test(n);
  const closing=/^(再见|拜拜|先这样|没有了)[呀呢吧。！!？?]*$/u.test(n);
  if(decision.risk!=='low'&&!combined&&!preference&&!closing)return null;
  if(decision.risk==='low'&&!['礼貌接待','礼貌回应','需求了解','偏好澄清'].includes(decision.intent))return null;
  const needs=[...(customer.needs||[]),...(customer.candidates||[]),...extractNeeds(text)];
  const has=kind=>needs.some(v=>v.kind===kind);
  let replies;
  if(closing)replies=['好的，您后续有新的佩戴喜好，可以继续告诉我。'];
  else if(decision.intent==='礼貌回应')replies=['不客气，您还有想补充的佩戴喜好吗？'];
  else if(!has('用途'))replies=['您好，您是想自己日常佩戴，还是挑一份礼物？','您好，想先了解一下，您是自己戴还是送礼呢？'];
  else if(!has('风格'))replies=['了解，您更喜欢简约素雅，还是有些颜色的款式？','明白，您平时偏爱素一点的，还是有颜色的款式？'];
  else if(has('款式')&&!has('佩戴感'))replies=['了解，您平时更喜欢小巧一些，还是存在感强一些的款式？'];
  else if(has('款式')&&has('佩戴感')&&!has('场景'))replies=['您主要是日常佩戴，还是在聚会时搭配呢？'];
  else if(has('款式')&&has('佩戴感')&&has('场景'))replies=['了解，您的用途、风格和佩戴偏好都已记录为待核对信息。您还有想补充的喜好吗？'];
  else replies=['了解，您更想先看看手链，还是手串？','明白，您想先了解手链，还是手串这类款式？'];
  return {topic:'discovery',replies,intent:'需求了解',risk:'low',reasons:[],reply:'',nextAction:'自动追问佩戴偏好，需求候选保留待核对。'};
}
