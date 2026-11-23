import {parseAttribution} from './intent-attribution.mjs';import {parseChoice} from './intent-choice-gate.mjs';import {parseIntent} from './temporary-intent.mjs';
export function gateCombined(source,intentResponse,attributionResponse,choiceResponse){
 if(parseAttribution(attributionResponse)!=='self_current')return{act:'unclear',quote:null};
 if(parseChoice(source,choiceResponse).target!=='human_conversation_choice')return{act:'unclear',quote:null};
 return parseIntent(source,intentResponse);
}
