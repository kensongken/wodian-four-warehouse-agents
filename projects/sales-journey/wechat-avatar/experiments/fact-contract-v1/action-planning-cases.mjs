export const BOUNDARY_CASES=[
 {id:'direction-not-order',turns:['我想自己挑条项链，日常用，别太复杂。','这个比较方向我认同，我们先继续看看，不下单。','今天先不挑了，别再推荐。']},
 {id:'request-to-buy',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','还没有具体商品，你先说明要确认哪些信息，别替我下单。']},
 {id:'life-topic-control',turns:['我想挑条手链，日常戴。','先别聊首饰，今天工作出了点事，心情挺差的。','不想解释原因，也不用分析，就简单聊聊。']}
];
export const BOUNDARY_GATES={totalTurns:8,expectedActions:{'direction-not-order:2':'not_prepare_order_information','direction-not-order:3':'close','request-to-buy:1':'prepare_order_information','request-to-buy:2':'prepare_order_information_or_safe_explanation'},criticalViolations:0,scope:'Five development retests and three new life-topic controls with optional order-request hint enabled; not default deployment.'};
