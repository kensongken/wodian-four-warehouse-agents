export const CASES=[
 {message:'一处细节的设计是指哪种，我还没想好。',action:'compare_detail',direction:['unclear'],order:['no']},
 {message:'轮廓大小要怎么跟平时穿的衣服比较？',action:'compare_daily',direction:['unclear'],order:['no']},
 {message:'对，就沿着少一点装饰的方向继续聊。',action:'compare_detail',direction:['yes'],order:['no']},
 {message:'你说从穿搭比较这个办法不错，先这样看，不买。',action:'compare_daily',direction:['yes'],order:['no']},
 {message:'这个思路我认可，但今天先不下单。',action:'compare_detail',direction:['yes'],order:['no']},
 {message:'嗯，好像也行吧。',action:'compare_detail',direction:['yes','unclear'],order:['no','unclear']},
 {message:'那个，先这么着吧。',action:'compare_daily',direction:['unclear'],order:['no','unclear']},
 {message:'不要再推荐了，今天到此为止。',action:'compare_detail',direction:['no','unclear'],order:['no']},
 {message:'这个方向我不喜欢，想换个思路，但还不买。',action:'compare_detail',direction:['no'],order:['no']},
 {message:'今天没有购买打算，先停在这里。',action:'compare_daily',direction:['unclear'],order:['no']},
 {message:'这个挑选思路我认同，也想下单，请先说明还缺哪些商品信息。',action:'compare_detail',direction:['yes'],order:['yes']},
 {message:'我想买一条项链，请先告诉我下单还需要什么信息。',action:'compare_daily',direction:['unclear'],order:['yes']}
];
export const GATES={minimumJointCorrect:11,total:12,falseOrderRequests:0,scope:'Independent intent signals, not conversation reply, order confirmation or owner likeness.'};
