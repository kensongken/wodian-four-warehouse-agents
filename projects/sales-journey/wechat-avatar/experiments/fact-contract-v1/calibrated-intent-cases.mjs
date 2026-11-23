// Acceptable sets frozen before model calls. Ambiguity is not scored as a single unquestionable truth.
export const CASES=[
 {message:'这个设计思路会不会显得太张扬？',action:'compare_detail',relations:['followup'],intents:['discuss']},
 {message:'你说比较反光感，具体怎么看？',action:'compare_daily',relations:['followup'],intents:['discuss']},
 {message:'那要准备哪些东西才能核实能不能补配？',action:'repair_limits',relations:['followup'],intents:['discuss']},
 {message:'今天先别推荐，我不挑了。',action:'compare_detail',relations:['new_topic'],intents:['discuss']},
 {message:'换成给外婆选礼物，不给自己挑了。',action:'compare_daily',relations:['new_topic'],intents:['discuss']},
 {message:'不聊首饰了，我想说说今天家里的事。',action:'compare_detail',relations:['new_topic'],intents:['discuss']},
 {message:'嗯，就按你说的简洁方向继续比较。',action:'compare_daily',relations:['followup'],intents:['direction_accepted']},
 {message:'对，一处小细节就好，我们先沿这个思路看。',action:'compare_detail',relations:['followup'],intents:['direction_accepted']},
 {message:'行，那个再说吧。',action:'compare_detail',relations:['unclear','new_topic'],intents:['unclear','discuss']},
 {message:'好，那样也行。',action:'compare_detail',relations:['followup','unclear'],intents:['direction_accepted','unclear']},
 {message:'我要直接下单，你告诉我要补哪些商品和付款信息。',action:'compare_detail',relations:['followup'],intents:['order_requested']},
 {message:'帮我下单买一对耳钉，不是继续聊方向。',action:'compare_daily',relations:['followup','new_topic'],intents:['order_requested']}
];
export const GATES={minimumJointCorrect:11,total:12,falseOrderRequests:0,explicitStopsCorrect:true,scope:'Intent proposals only, never orders. No runtime default change. Not general accuracy or owner likeness.'};
