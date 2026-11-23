// New synthetic cases and gates frozen before inference. Not blind external evaluation.
export const ACCEPTANCE={minimumCorrect:14,total:16,maximumFalsePause:0,maximumInvalid:0,allExplicitPausesRequired:true,mustBeatBaseline:true};
export const SCOPED_CASES=[
 {id:'recipient-change',context:['给女儿挑生日耳环。'],message:'女儿那份先放放，我先给自己选个吊坠。',expected:'selection'},
 {id:'task-change',context:['我的耳环有点变形，想维修。'],message:'维修先别弄，改问一下能不能退换。',expected:'return'},
 {id:'category-change',context:['给我看看戒指。'],message:'戒指不考虑了，换成手链看看。',expected:'selection'},
 {id:'service-change',context:['想把旧项链改成手链。'],message:'改造先不做，先了解怎么清洗保养。',expected:'care'},
 {id:'clear-stop',context:['我想挑个日常戴的戒指。'],message:'今天所有款都先不看了，别给我推荐了。',expected:'pause'},
 {id:'stop-after-care',context:['这条项链怎么保养？'],message:'保养方法我记住了，今天也不选首饰了。',expected:'pause'},
 {id:'resume',context:['今天别推荐了。'],message:'现在有空了，我们继续挑刚才的耳环。',expected:'selection'},
 {id:'reported-stop',context:['你想看什么首饰？'],message:'姐姐说她不买，不是我，我想接着挑吊坠。',expected:'selection'},
 {id:'fatigue',context:['平时喜欢哪种戒指？'],message:'今天连开了三个会，真想早点回家睡觉。',expected:'chat'},
 {id:'privacy',context:['最近家里有什么事？'],message:'家里的情况不想说，我们聊点轻松的。',expected:'chat'},
 {id:'hypothetical',context:[],message:'要是顾客说太贵不买了，你会怎么回答？',expected:'unknown'},
 {id:'review-tone',context:['我不是在买东西。'],message:'刚才那句只是给你演练，帮我评价这种接话口吻。',expected:'unknown'},
 {id:'two-services',context:['我有一个旧金镯。'],message:'是修好继续戴，还是直接回收？我还没有想好。',expected:'unknown'},
 {id:'resolve-two-services',context:['是修好继续戴，还是直接回收？我还没有想好。'],message:'想好了，不回收，先看看维修需要了解些什么。',expected:'repair'},
 {id:'gift-after-chat',context:['最近工作有点烦。'],message:'说点开心的，我想给爱人选个纪念日礼物。',expected:'gift'},
 {id:'care-after-return',context:['我刚才问了退换条件。'],message:'现在不问退换，单说这枚戒指的保养方法。',expected:'care'}
];
