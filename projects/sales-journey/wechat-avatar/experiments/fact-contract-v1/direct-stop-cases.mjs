export const CASES=[
 {message:'今天还是别选了，先到这。',scope:['end_consultation'],action:['close'],knownRegression:true},
 {message:'不用再给我推荐，今天的咨询就结束了。',scope:['end_consultation'],action:['close']},
 {message:'我现在不想挑了，你也别继续问。',scope:['end_consultation'],action:['close']},
 {message:'我不是不看了，只是这件不要，其他的继续看看。',scope:['exclude_item'],action:['not_close']},
 {message:'我先不付钱，想继续了解日常怎么搭。',scope:['defer_order'],action:['not_close']},
 {message:'不用替我买，我现在只想比较不同的设计。',scope:['defer_order'],action:['not_close']},
 {message:'日常搭衬衫，首饰的轮廓要怎么比较？',scope:['unclear'],action:['compare_daily','compare_detail','compare_outline']},
 {message:'耳堵掉了，想了解补配前需要核实哪些情况。',scope:['unclear'],action:['repair_preserve','repair_receipt','repair_limits']}
];
export const GATES={required:8,total:8,explicitStopQuestions:0,scope:'Stop-guard targeted test only: one known regression and seven new prompts. Not full dialogue, original architecture gate or owner likeness acceptance.'};
