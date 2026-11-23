// System-authored customer prompts, never owner speech or store facts.
export const rehearsalScenarios=Object.freeze([
 {id:'first_visit',label:'初次接待',questions:['你好，我第一次来，也不知道自己适合什么。','我就随便逛逛，不想一进来就被推销。']},
 {id:'selection',label:'选款与搭配',questions:['我平时穿白衬衫上班，手链选什么感觉比较搭？','宽一点怕夸张，细一点又觉得没特点，怎么选？']},
 {id:'gift',label:'送礼与关系',questions:['想给妈妈挑个礼物，但怕她觉得我乱花钱。','送给刚认识的朋友，想表达心意又怕太贵重。']},
 {id:'budget',label:'预算与比价',questions:['我只有三百左右预算，会不会根本没得选？','网上看起来差不多的更便宜，你们为什么贵？']},
 {id:'hesitation',label:'犹豫与节奏',questions:['我怕买回去又放着不用，但看着确实喜欢。','我今天不买，你别一直劝我了。']},
 {id:'purchase',label:'购买与事实核实',questions:['就要刚才那个款，你直接给我下单吧。','你们在哪里，今晚几点关门？能直接过去试戴吗？']},
 {id:'aftercare',label:'售后与不满',questions:['才戴一天扣子就卡住了，我有点生气。','我不想修了，今天就给我退钱，可以吗？']},
 {id:'care',label:'护理与佩戴顾虑',questions:['戴着有点痒，还能继续戴吗？','我总忘记摘首饰，洗澡能一直戴着吗？']},
 {id:'mood',label:'工作与情绪',questions:['今天被领导说了一顿，好烦，只想随便聊聊。','最近总觉得自己不够好，买什么都没心情。']},
 {id:'family',label:'家常与隐私',questions:['孩子最近太闹了，每天忙完真的好累。','家里的事我不方便说，别再问这个了。']},
 {id:'owner_life',label:'问店主近况',questions:['你吃饭了吗，今天店里忙不忙？','你家孩子最近怎么样，考试考得好吗？']},
 {id:'trust_exit',label:'信任与结束',questions:['你是不是机器人？怎么说话总是差不多。','我先去接孩子了，有空再聊，先别给我发消息。']}
]);
export function rehearsalQuestion(id){for(const s of rehearsalScenarios){for(let i=0;i<s.questions.length;i++){if(id===`${s.id}:${i}`)return{category:s.label,question:s.questions[i]};}}return null;}
