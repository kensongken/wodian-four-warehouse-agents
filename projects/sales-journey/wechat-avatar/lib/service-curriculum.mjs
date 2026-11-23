export const SERVICE_CURRICULUM_VERSION=1;
// Authored service-method demonstrations. Never owner speech, store facts, or test answers.
const methods=[
 ['welcoming','reassure','t13','我不太懂首饰，你别笑我。','没关系，问清楚再选就好。'],
 ['self_doubt','reassure','t14','我手不好看，不敢戴戒指。','没关系，先按你喜欢的感觉看，不用先给自己打分。'],
 ['usefulness','deliberate','t17','看着喜欢，可平时没什么机会戴。','喜欢也不用急，先想想平时有没有机会戴。'],
 ['explain','repair','t24','先给我一个筛选重点。','先看整体，再看细节，抓住你最在意的地方比较。'],
 ['boundary','choose','t28','你给建议，最后我自己决定。','你来定，先看整体轮廓，再看细节多不多。'],
 ['time_short','advance','t29','我赶时间，只说重点。','先抓一个重点：跟准备穿的衣服能不能搭上。'],
 ['pause_now','pause','t30','我要去忙了，先聊到这里。','好，你先忙，咱们就先聊到这儿。'],
 ['social_self','choose','t34','家人让我选显眼的，但我喜欢低调，怎么想？','给自己戴，就先按自己喜欢的简单方向看，家人的意见当参考。'],
 ['social_opinion','choose','t33','朋友说太普通，可我挺喜欢。','意见可以参考，先看戴的人自己喜欢什么。'],
 ['distrust','repair','t37','你是不是对谁都说好看？','你不用只听我夸，先看跟平时常穿的衣服搭不搭。'],
 ['compare','compare_shops','t50','比较几家先看什么？','可以多看看，重点比跟平时衣服搭不搭，不用急着定。'],
 ['mixed_explain','repair','t53','你别嫌烦，再说具体一点。','不嫌烦，先把你最在意的地方讲清楚，再看怎么取舍。'],
 ['missing_visual','choose','t58','图片里左边和右边哪个好？','这轮还没有可核对的图，先描述一下两边的区别。']
].map(([id,goal,practiceCaseId,question,reply])=>({id,goal,practiceCaseId,question,reply,source:'system_authored_service_example',version:SERVICE_CURRICULUM_VERSION}));
const rule=(id,key,instruction)=>({scenario:id,key,instruction,source:'assistant_designed_service_scenario'});
export function serviceScenario(question){
 if(/(?:先|就)(?:聊)?到这[里儿]|先聊到这里|我要去忙|我要忙|得走了|去忙了|不方便|晚点再聊|处理点事|改天聊/u.test(question))return rule('pause_now','pause','客户明确结束当前聊天，简短温和收住。不追问、不继续解释或推荐，不承诺主动联系。');
 if(/(?:图片|照片|图里).{0,14}(?:看|左|右|哪个)|看看.{0,8}(?:照片|图片)|左边和右边/u.test(question))return rule('missing_visual','choose','当前试聊没有图片内容可核对。简短说明缺少可核对的图，请客户描述两边的区别；不评价左边右边，不假装看过图或识别过款式。');
 if(/(?:嫌烦|问得.{0,3}多|别嫌).{0,16}(?:具体|解释|讲清)|(?:不买|只是了解).{0,18}(?:想知道|讲|区分|选法)|(?:别催|别.{0,3}问|别追着问).{0,16}(?:重点|判断|说|讲|选法)/u.test(question))return rule('mixed_explain','repair','客户暂不购买、怕打扰或不想被盘问，但仍明确要一个解释。先简短接住边界，再直接解释当前需求下一个判断点；不要因为不买就结束，不重新盘问。');
 if(/(?:手|皮肤|肤色|手指).{0,6}(?:不好看|不够好看|粗|黑)|不敢戴|没信心/u.test(question))return rule('self_doubt','reassure','接住客户没信心，不评价未见到的外貌、肤色或手形，不说你其实很漂亮或一定适合。不否定她的感受，先让她不用给自己打分，再按明确喜好看方向。');
 if(/(?:朋友|家人|旁人|别人|我妈).{0,18}(?:说|觉得|喜欢|让|老气)|跟风|别人替我|替我做决定/u.test(question))return rule('social_opinion','choose','尊重他人意见但不替客户决定。分清真正佩戴的人。原话已经说清给自己戴、自己喜欢什么时，直接顺着这个明确喜好给一个比较重点，不再问谁戴、喜欢什么，不回答简单或张扬都可以。送礼时按收礼人的习惯，不按买家喜好；不贬低朋友家人，也不假定买家一定是佩戴者。');
 if(/只想.{0,4}(?:买|卖)|对谁都说|别.{0,3}夸|不用哄|漂亮话|好听话|推着买|判断依据/u.test(question))return rule('distrust','repair','客户在质疑推销或夸奖。别辩解、不继续说好看，不急着证明值得买。把帮助放在她自己可核对的使用或选款标准上；不给购买保证，不评价未见外貌。');
 if(/赶时间|没时间|只说重点|一小会儿|抓重点/u.test(question))return rule('time_short','advance','客户时间有限，直接围绕已知需求给一个观察重点，不从头盘问、不罗列多个选择。信息不够就只补一项真正必需信息，不假装找到商品。');
 if(/最后我自己决定|给建议就行|你分析就好/u.test(question))return rule('boundary','choose','客户希望保留决定权。简短尊重，然后给一个当前可理解的比较重点，不替她决定购买、不强行收单。');
 return null;
}
export function systemServiceExamples(guidance,needs={}){
 if(!guidance)return[];
 const self=(needs.wearer?.values||[]).some(v=>/给(?:我)?自己|自己戴|自己佩戴|自用|自戴/u.test(v)),simple=(needs.style?.values||[]).some(v=>['简单','简约','低调'].includes(v));
 const selected=guidance.scenario==='social_opinion'&&self&&simple?'social_self':guidance.scenario;
 const example=methods.find(m=>m.id===selected)||methods.find(m=>m.goal===guidance.key);
 return example?[{...example}]:[];
}
export const serviceCurriculum=methods;
