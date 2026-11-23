export const BOUNDARY_CASES=[
 {id:'design-reference',turns:['给我自己挑条项链，日常戴但想有点设计细节。','你刚说的这种方向，会不会显得太抢眼？']},
 {id:'repair-reference',turns:['我以前买的耳钉少了耳堵，想了解补配。','接着刚才那个事，能保证合适吗，要多久？']},
 {id:'pause-and-change',turns:['今天不想买了，先停一下。','改主意了，想给妈妈选个生日礼物。']}
];
export const BOUNDARY_GATES={totalTurns:6,criticalViolations:0,noModelFreeText:true,referentTurns:'design-reference:2 must stay selection; repair-reference:2 must stay repair',pause:'pause-and-change:1 close',change:'pause-and-change:2 gift',scope:'new scripted native dialogue, single sample; no general accuracy claim'};
