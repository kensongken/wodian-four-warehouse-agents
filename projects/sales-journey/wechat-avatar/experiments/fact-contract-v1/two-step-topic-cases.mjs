// Frozen before sampling; synthetic cases, not independently blinded customer evidence.
export const TWO_STEP_GATES={minimumCorrect:14,total:16,maximumFalsePause:0,maximumInvalid:0,allExplicitPausesRequired:true,mustBeatBaseline:true};
export const TWO_STEP_CASES=[
 {id:'change-recipient',context:['给老公挑条链子。'],message:'他的先搁着，我自己想买对耳钉。',expected:'selection'},
 {id:'change-service',context:['旧耳环想换个造型。'],message:'换造型先算了，我想先问日常清洁要注意什么。',expected:'care'},
 {id:'change-return',context:['这个戒指有点松，考虑改圈。'],message:'圈先别改了，先说说退货需要哪些条件。',expected:'return'},
 {id:'change-category',context:['帮我挑条项链。'],message:'项链就不选了，给我看看日常戴的戒指。',expected:'selection'},
 {id:'end-shopping',context:['还想看别的款吗？'],message:'都不看了，我今天不买，你先忙。',expected:'pause'},
 {id:'explicit-pause',context:['可以再挑挑手镯。'],message:'选购的事今天到这吧，不用继续推荐。',expected:'pause'},
 {id:'resume-after-break',context:['先不选了，等我忙完。'],message:'忙完了，接着看看手镯的款式吧。',expected:'selection'},
 {id:'not-friends-intent',context:['朋友也想看首饰吗？'],message:'朋友没兴趣了，不过我还想看看耳钉。',expected:'selection'},
 {id:'ordinary-chat',context:['最近怎么样？'],message:'昨晚孩子一直咳嗽，折腾得我没睡好。',expected:'chat'},
 {id:'chat-to-gift',context:['最近陪孩子挺累的。'],message:'对了，下周是我爸生日，想给他挑个礼物。',expected:'gift'},
 {id:'conditional-reply',context:[],message:'假设有人说这也太贵了，你通常怎么接这句话？',expected:'unknown'},
 {id:'tone-test',context:['我们在演练服务。'],message:'我说不买只是演的，你帮我看看刚才那句回复会不会太生硬。',expected:'unknown'},
 {id:'unresolved-alternatives',context:['有条断了的老项链。'],message:'到底拿去维修还是干脆回收，我还在纠结。',expected:'unknown'},
 {id:'resolve-alternatives',context:['维修还是回收还没想好。'],message:'决定留着戴，不回收了，先聊维修怎么评估。',expected:'repair'},
 {id:'aftercare-to-remodel',context:['这条手链怎么保养我知道了。'],message:'还有个旧吊坠，我想保留原石换个造型。',expected:'remodel'},
 {id:'recycle-confirmed',context:['旧镯子要不要留着？'],message:'不打算修也不再戴了，现在只想了解回收流程。',expected:'recycle'}
];
