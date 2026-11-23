import {mandatoryTopics,normalize} from './policy.mjs';
// Only for local rehearsal intent routing. Shared outbound policy remains strict.
export function rehearsalTopics(question){
 const text=normalize(question);
 if(/(?:可以|能|支持|无条件|给我|帮我).{0,4}退(?:货|款|钱|回|换|的|吗|[，。!?]|$)/u.test(text))return [...new Set([...mandatoryTopics(text),'售后与争议'])];
 if(/退|换|赔|维修|发货|到货|寄|邮|送到/u.test(text))return mandatoryTopics(text);
 // Remove only the duration word within a literal fear-of-nonuse expression.
 // Other durations and every other business-risk trigger remain intact.
 const routed=text.replace(/(?:过|戴了?|戴不了|没戴|用了?)几天(?=(?:估计|可能|恐怕|大概|就|又|便|会|自己|我){0,5}(?:懒得戴|不想戴|不戴|不想用|不用|闲置|放(?:进)?抽屉))/gu,s=>s.replace('几天','一阵'));
 return mandatoryTopics(routed);
}
export function selectionFocus(needs={}){
 const values=needs.style?.values||[],excluded=needs.style?.excluded||[],noticeable=values.includes('显眼');
 const basis={values:[...values],excluded:[...excluded]};
 if(noticeable)return{key:'noticeable',source:'system_method_from_literal_preferences',basis,instruction:'客户明确想显眼。先解释整体轮廓能不能让人注意，再看细节多少；想显眼不等于要更重，也不等于把细节堆满。不要把目标改成不抢眼或越素越好。'+(excluded.some(v=>['复杂','花哨'].includes(v))?'客户又明确不要复杂或花哨，可以靠清楚的轮廓保留存在感，把细节收住；讲清这两个方面怎样兼顾。':'')+'只解释通用观察办法，不假装见过具体商品。'};
 if(values.includes('设计感'))return{key:'distinctive',source:'system_method_from_literal_preferences',basis,instruction:'客户想有设计感，不能直接理解成要夸张显眼。解释局部有没有特点、整体细节是否太满这两个观察重点，给出怎样取舍的理由。'+(excluded.includes('显眼')||values.includes('低调')?'客户也明确要低调或不要显眼，保留整体克制，不主动劝她突出抢眼。':'')+'只说通用判断办法，不编造商品造型、材质或库存。'};
 if(values.some(v=>['简单','简约','低调'].includes(v)))return{key:'understated',source:'system_method_from_literal_preferences',basis,instruction:'客户明确喜欢简单或低调。重点说明细节不要太满、与她已经说过的穿衣场景是否协调；不要反过来催她选夸张显眼的方向。没说过的衣服和佩戴习惯不假设。只解释一个可观察的差别和理由。'};
 return null;
}
