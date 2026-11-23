export const CASES=[
 {id:'information-to-gift',turns:[
  {message:'我想先弄明白买耳饰要核对哪些资料，现在还没有看中的款。',expected:['prepare_order_information']},
  {message:'继续讲需要核对的项目就行，先不替我买。',expected:['purchase_information_checklist','purchase_information_wait'],continueRequired:true},
  {message:'自己的先不挑了，改为给舅妈选件生日礼物，她喜欢低调的。',expected:['gift_habits','compare_daily'],continueRequired:true}]},
 {id:'life-privacy-back-to-service',turns:[
  {message:'最近加班挺累的，先和我闲聊两句，买东西的事等会儿说。',expected:['chat_ack','privacy_respect'],continueRequired:true},
  {message:'工作的细节不想解释，也不用分析我，陪我轻松说会儿话就好。',expected:['privacy_respect','chat_ack'],continueRequired:true},
  {message:'换个话题，我项链配件松了，找人核实维修前先怎么保管？',expected:['repair_preserve'],continueRequired:true}]},
 {id:'repair-design-stop',turns:[
  {message:'耳钉配件不见了，确认能否补配之前，原来的东西先怎么放好？',expected:['repair_preserve']},
  {message:'补配先不聊，转回来给自己选日常戴、有一点设计细节的。',expected:['compare_daily','compare_detail','compare_outline'],continueRequired:true},
  {message:'好了，今天不继续咨询，别再给我推荐了。',expected:['close'],explicitStop:true}]},
 {id:'design-care-stop',turns:[
  {message:'素色衣服配首饰，想有一点小设计但不要装饰堆太多，怎么考虑？',expected:['compare_daily','compare_detail','compare_outline']},
  {message:'搭配的先放一放。家里那件材质没弄清楚，清洗之前应该核对什么？',expected:['care_limits'],continueRequired:true},
  {message:'先结束吧，今天不用再追问我了。',expected:['close'],explicitStop:true}]}
];
export const GATES={minimumCandidateCorrect:10,total:12,minimumGain:2,errors:0,skipped:0,prematureClose:0,uncompletedExplicitStop:0,scope:'12 fresh fixed development turns per arm. No per-case retries, owner-likeness, general-accuracy or live WeChat claim.'};
