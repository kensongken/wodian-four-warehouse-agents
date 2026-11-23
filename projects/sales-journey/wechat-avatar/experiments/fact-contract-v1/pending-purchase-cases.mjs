export const BOUNDARY_CASES=[
 {id:'direction-not-order',turns:['我想自己挑条项链，日常用，别太复杂。','这个比较方向我认同，我们先继续看看，不下单。','今天先不挑了，别再推荐。']},
 {id:'request-to-buy',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','还没有具体商品，你先说明要确认哪些信息，别替我下单。']},
 {id:'purchase-to-life',turns:['我要买一条手链，先说清楚准备哪些购买资料。','先放下买东西的事，今天工作不顺，想聊聊。','还是回到首饰，先比较适合日常穿搭的方向，不要下单。']}
];
export const BOUNDARY_GATES={totalTurns:8,expectedActions:{'direction-not-order:2':'not_prepare_order_information','direction-not-order:3':'close','request-to-buy:1':'prepare_order_information','request-to-buy:2':'purchase_information_checklist'},criticalViolations:0,scope:'Five development retests and three new life-topic controls with optional order-request hint enabled; not default deployment.'};
