export const DIAGNOSTIC_MESSAGES=[
 '补配先不聊，转回来给自己选日常戴、有一点设计细节的。',
 '维修暂时搁着吧，我现在想看日常搭配，有一点小设计就好。',
 '先不说配件了，换成给我挑平时穿搭能用的简单设计。',
 '修的事等核实后再聊，眼下想先比较首饰的小设计怎么选。'
];
export const DIAGNOSTIC_EXPECTED=['daily','design','outline'];
export const CASES=[
 {id:'service-to-gift-long',turns:[
  {message:'胸针的配件松脱了，确认是否能修之前，原件先怎样保存？',expected:['repair_preserve']},
  {message:'那能不能修、费用和工期，现在是不是都得等核实才能说？',expected:['repair_limits'],continueRequired:true},
  {message:'维修等后面再核实，我改问给表妹送毕业礼物，她平常很少戴饰品。',expected:['gift_habits','compare_daily'],continueRequired:true},
  {message:'先结束今天的咨询，不用接着问了。',expected:['close'],explicitStop:true}]},
 {id:'purchase-life-selection-long',turns:[
  {message:'手链还没有选好，先告诉我购买前需要核对哪些资料。',expected:['prepare_order_information']},
  {message:'按刚才的流程往下说要核对的项目，我只是了解一下。',expected:['purchase_information_checklist','purchase_information_wait'],continueRequired:true},
  {message:'流程先聊到这，最近照顾老人有点累，陪我随便聊会儿。',expected:['chat_ack','privacy_respect'],continueRequired:true},
  {message:'好，换回我自己的搭配，平时穿衬衫，想有一点设计但别太繁复。',expected:['compare_daily','compare_detail','compare_outline'],continueRequired:true}]},
 {id:'care-privacy-gift-long',turns:[
  {message:'老首饰的材质还没确认，先想知道清洁前应该核实哪些信息。',expected:['care_limits']},
  {message:'保养先不说了，我今天心情有点乱，原因不想讲，也别分析，轻松聊就好。',expected:['privacy_respect','chat_ack'],continueRequired:true},
  {message:'现在想说点高兴的，给侄女挑成年礼，她喜欢清爽简单的感觉。',expected:['gift_habits','compare_daily'],continueRequired:true},
  {message:'今天就聊到这里吧，请别再推荐或者追问了。',expected:['close'],explicitStop:true}]}
];
export const GATES={minimumCandidateCorrect:11,total:12,minimumGain:1,diagnosticCorrect:4,errors:0,skipped:0,prematureClose:0,uncompletedExplicitStop:0,scope:'One known failure plus three paraphrases in reused history are diagnostic only;12 new scripted turns per arm, three4-turn journeys, same model. No general accuracy or owner likeness claim.'};
