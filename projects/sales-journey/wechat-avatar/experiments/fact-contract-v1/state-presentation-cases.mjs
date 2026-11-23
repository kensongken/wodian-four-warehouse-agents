export const CASES=[
 {id:'purchase-then-gift',turns:[
  {message:'想了解买手镯前要核实哪些东西，款式我还没挑好。',expected:['prepare_order_information']},
  {message:'那就把刚才的核对流程往下说，我只是问清楚，还不买。',expected:['purchase_information_checklist','purchase_information_wait'],continueRequired:true},
  {message:'明白啦，先换成给外婆挑寿礼，她平时喜欢简洁的。',expected:['gift_habits','compare_daily'],continueRequired:true}]},
 {id:'smalltalk-privacy-service',turns:[
  {message:'这两天忙搬家有点累，先陪我聊几句，首饰等会儿再说。',expected:['chat_ack','privacy_respect'],continueRequired:true},
  {message:'为什么搬家就不说了，也不用替我分析，随便说说轻松的。',expected:['privacy_respect','chat_ack'],continueRequired:true},
  {message:'现在说回首饰，我那条项链扣松了，核实维修前应该怎么保管？',expected:['repair_preserve'],continueRequired:true}]},
 {id:'repair-then-design-stop',turns:[
  {message:'耳环的配件掉了，补配之前原件先怎么保存合适？',expected:['repair_preserve']},
  {message:'补配的事先搁着，聊聊日常戴又有一点设计细节的款式吧。',expected:['compare_detail','compare_daily','compare_outline'],continueRequired:true},
  {message:'今天就先结束聊天吧，别再问我或者推荐了。',expected:['close'],explicitStop:true}]}
];
export const GATES={minimumCandidateCorrect:8,total:9,minimumGain:2,errors:0,skipped:0,prematureClose:0,uncompletedExplicitStop:0,diagnosticQuoteValid:4,scope:'4 reused failed-request contexts are diagnosis only;9 fresh scripted turns/arm are separate development holdout, not general accuracy or owner-voice acceptance.'};
