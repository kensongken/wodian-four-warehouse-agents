export const BOUNDARY_CASES=[
 {id:'recipient-switch',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','我自己的先不买，改成给妈妈挑生日礼物。','妈妈平时很少戴首饰，想简单一点，不用下单。']},
 {id:'category-switch',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','耳钉先不选了，重新挑一条手链，日常戴。','先聊手链的样子，不谈购买资料。']},
 {id:'unclear-then-stop',turns:['我想买一对耳钉，请先帮我确认下单需要哪些资料。','先换个吧。','今天还是别选了，先到这。']}
];
export const BOUNDARY_GATES={totalTurns:9,required:'All three seeds pending; recipient/category switches release pending, ambiguous change retains pending with clarification, explicit stop closes.',criticalViolations:0,scope:'Three repeated known purchase seeds plus six new follow-up turns, single native sample, not independent accuracy or owner-likeness acceptance.'};
