import {SERVICE_TOPICS} from './service-topics.mjs';
export function twoStepProposal(result){
 const topic=result.projectedTopic,[label,suggestedScenario]=SERVICE_TOPICS[topic];
 return{...result,topic,label:result.focus.kind==='meta'?'讨论接话方式，非实际选购':result.focus.kind==='unclear'?'需求未定，需要澄清':label,quote:topic==='unknown'?null:result.focus.quote,suggestedScenario,simulation:true};
}
