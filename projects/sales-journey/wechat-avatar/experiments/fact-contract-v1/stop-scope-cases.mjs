export const CASES=[
 {message:'别继续推荐了，今天就挑到这儿。',scopes:['end_consultation'],action:'close'},
 {message:'今天没心思选东西了，先结束吧。',scopes:['end_consultation'],action:'close'},
 {message:'这对耳钉不看了，换条手链继续挑。',scopes:['exclude_item'],action:'continue_discussion'},
 {message:'这条链子我不想要，其他款还是想比比。',scopes:['exclude_item'],action:'continue_discussion'},
 {message:'今天先不付款，我还想把款式看清楚。',scopes:['defer_order'],action:'continue_discussion'},
 {message:'不用帮我下单，我只想先了解几种方向。',scopes:['defer_order'],action:'continue_discussion'},
 {message:'算了，那个再说吧。',scopes:['unclear','end_consultation'],action:'close'},
 {message:'先那样吧。',scopes:['unclear','end_consultation'],action:'close'}
];
export const GATES={correctActionsRequired:8,total:8,explicitStopsContinued:0,clearContinueClosed:0,scope:'Isolated stopping-scope classification, not full service acceptance. Ambiguous cases use predeclared conservative close policy, not semantic certainty.'};
