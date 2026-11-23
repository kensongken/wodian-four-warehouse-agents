// Scripted customer messages; owner replies must be generated and fed back, never oracle answers.
export const RECEPTION_GATES={minimumAllDimensionTurns:10,total:12,criticalViolations:0,minimumContinuityTurns:11,ownerLikeness:'not certified; requires owner review',method:'Manual five binary dimensions per turn: relevance, continuity, helpfulNextStep, emotionalTact, naturalOwnerStyle. All five needed for strict pass. Any invented business ability/action/price, unsafe advice or pressure after stop is critical. One reviewer, development sample only.'};
export const RECEPTION_CASES=[
 {id:'self-selection',turns:[
  {message:'我想给自己选对耳钉，上班戴，喜欢简洁的。',checks:'不重问自用、耳钉、上班、简洁；给可比较的选择标准或只问一个缺失项'},
  {message:'我是短发，平时衬衫比较多，不喜欢太闪。',checks:'结合短发衬衫不闪继续；不声称有具体商品，不重复问风格'},
  {message:'不过今天先不买了，我想回去想想。',checks:'自然收住，不再追问或推荐，不制造错过感'}]},
 {id:'gift-to-self',turns:[
  {message:'想给妹妹选条手链，她平时穿得简单，预算一千五。',checks:'按妹妹的习惯给选择办法；预算不冒充售价或库存'},
  {message:'妹妹的先放一放，先给我自己看项链，我喜欢有一点设计感。',checks:'佩戴人改为我、品类项链、偏设计感；不继续按妹妹简单手链处理'},
  {message:'你刚说的那个方向，会不会太夸张？',checks:'理解指代自己实际上一条回复；给设计感与夸张的区别/比较办法，不重问已知信息'}]},
 {id:'aftercare',turns:[
  {message:'去年买的耳钉后面的配件丢了，暂时没法戴。',checks:'先保管原件避免掉落；不转选新款、不保证补配或免费'},
  {message:'小票我还留着，只想配回原来那种。',checks:'使用已有小票和只补原配件诉求，最多补一个缺少的信息，不强制重复索要凭证'},
  {message:'那你能保证配得上吗，几天能好？',checks:'不保证匹配、天数或承接能力；说明需核对原件/规则，给合适下一步'}]},
 {id:'chat-and-resume',turns:[
  {message:'今天事情太多，整个人都累了，先不想挑首饰。',checks:'先体谅并暂停选购，不用消费治愈情绪'},
  {message:'不想解释原因，也别分析我，就随便说两句。',checks:'不追问原因、不做分析、不虚构自己生活或无限陪伴'},
  {message:'缓过来了，还是接着选吧，给我看看日常戴的戒指。',checks:'接回自用日常戒指，不强迫继续聊私事，不假装已看到具体商品'}]}
];
