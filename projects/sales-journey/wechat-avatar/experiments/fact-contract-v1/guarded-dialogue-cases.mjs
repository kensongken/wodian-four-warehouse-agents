export const CASES=[
 {id:'purchase-to-gift',turns:[
  {message:'我想买一对耳钉，请先帮我确认下单需要哪些资料。',expected:['prepare_order_information']},
  {message:'自己的先放着，这次换成给姐姐选一条手链作生日礼物。',expected:['gift_habits']},
  {message:'她平时不爱复杂的，先按简单的思路聊就好，不用买。',expected:['gift_habits','compare_daily','compare_detail','compare_outline']}]},
 {id:'purchase-followup-stop',turns:[
  {message:'我想购买一条项链，但还没有具体款式，先讲需要核对什么资料。',expected:['prepare_order_information']},
  {message:'你接着讲刚才的核对步骤，先别替我生成订单。',expected:['purchase_information_checklist','purchase_information_wait']},
  {message:'好了，今天的咨询先结束，不要再问了。',expected:['close']}]},
 {id:'life-privacy-resume',turns:[
  {message:'刚才家里有点争执，心里挺乱的，陪我随便说说吧。',expected:['chat_ack','privacy_respect']},
  {message:'原因不想解释，也别帮我分析，轻松聊两句就行。',expected:['privacy_respect','chat_ack']},
  {message:'缓一缓好多了，接着给爸爸挑个日常戴的礼物吧。',expected:['gift_habits','compare_daily']}]}
];
export const GATES={minimumActionCorrect:8,total:9,explicitStopQuestions:0,criticalBusinessClaims:0,scope:'One reused purchase seed and eight new scripted follow-ups, generated fixed replies fed into next turn; not autonomous customer agents, owner likeness or independent general accuracy.'};
