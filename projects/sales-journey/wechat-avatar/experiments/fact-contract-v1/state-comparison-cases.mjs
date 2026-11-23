export const CASES=[
 {id:'purchase-information-switch',turns:[
  {message:'戒指还没定下来，购买之前通常有哪些信息要核实？先了解流程。',expected:['prepare_order_information']},
  {message:'接着把刚才需要核实的项目说清楚就行，我暂时不付款。',expected:['purchase_information_checklist','purchase_information_wait'],continueRequired:true},
  {message:'流程我知道了，换成帮弟弟挑毕业礼物吧，他喜欢朴素的。',expected:['gift_habits','compare_daily'],continueRequired:true}]},
 {id:'privacy-and-resume',turns:[
  {message:'最近带孩子累得够呛，想跟你随便聊聊，今天不谈买东西。',expected:['chat_ack','privacy_respect'],continueRequired:true},
  {message:'具体家里的事我不想讲，你也不用分析，我们随便聊两句就好。',expected:['privacy_respect','chat_ack'],continueRequired:true},
  {message:'现在心情好多了，给闺蜜选个生日礼物，她平常穿得很简单。',expected:['gift_habits','compare_daily'],continueRequired:true}]},
 {id:'service-switch-and-stop',turns:[
  {message:'手链扣子松了，去核实能不能修之前，东西先怎么放？',expected:['repair_preserve']},
  {message:'维修的先这样吧，另外我自己想选一件带点设计感的，别太花。',expected:['compare_detail','compare_outline','compare_daily'],continueRequired:true},
  {message:'今天这场咨询就到这吧，不用接着推荐了。',expected:['close'],explicitStop:true}]},
 {id:'daily-design-item-rejection',turns:[
  {message:'上班经常穿素色衣服，想先听听首饰怎么搭，不急着买。',expected:['compare_daily','compare_detail'],continueRequired:true},
  {message:'大面积装饰的不要，我还是想听听一点小设计怎么选。',expected:['compare_detail','compare_daily','compare_outline'],continueRequired:true},
  {message:'不用挑具体商品，就继续讲一处设计细节该怎么比较。',expected:['compare_detail','compare_daily'],continueRequired:true}]}
];
export const GATES={perArmMinimumCorrect:10,total:12,minimumGainToRecommendCandidate:2,missedExplicitStop:0,prematureClose:0,errors:0,scope:'12 newly frozen development turns, four three-turn journeys, same local 4B model. Not independent general accuracy, natural wording, owner likeness or real WeChat acceptance.'};
