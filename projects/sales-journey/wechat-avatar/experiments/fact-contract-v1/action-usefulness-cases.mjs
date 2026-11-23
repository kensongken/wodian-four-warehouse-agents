export const BOUNDARY_CASES=[
 {id:'direction-not-order',turns:['我想自己挑条项链，日常用，别太复杂。','这个比较方向我认同，我们先继续看看，不下单。','今天先不挑了，别再推荐。']},
 {id:'request-to-buy',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','还没有具体商品，你先说明要确认哪些信息，别替我下单。']}
];
export const BOUNDARY_GATES={totalTurns:5,expectedActions:{'direction-not-order:2':'not_prepare_order_information','direction-not-order:3':'close','request-to-buy:1':'prepare_order_information','request-to-buy:2':'prepare_order_information_or_safe_explanation'},criticalViolations:0,scope:'New scripted service-action test with optional order-request hint enabled; not default deployment.'};
