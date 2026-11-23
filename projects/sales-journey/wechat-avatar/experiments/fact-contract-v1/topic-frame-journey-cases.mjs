// Frozen synthetic fixtures: contexts are customer messages, not model-generated conversation.
export const JOURNEYS=[
 {id:'gift-to-self',turns:[
  {message:'想给妈妈买条项链，平时戴。',topic:'gift',intent:'unspecified'},
  {message:'算了，妈妈的下次再看，这次给我自己选戒指。',topic:'selection',intent:'unspecified'},
  {message:'今天上班太累了，脑子转不动。',topic:'chat',intent:'unspecified'},
  {message:'不过还是接着帮我选，想要简单一点的戒指。',topic:'selection',intent:'continue'}]},
 {id:'repair-to-return',turns:[
  {message:'我这个手链扣子坏了，想问怎么修。',topic:'repair',intent:'unspecified'},
  {message:'先不修了，我想了解退换的规则。',topic:'return',intent:'unspecified'},
  {message:'不是要买新的，我问的是退换需要什么材料。',topic:'return',intent:'unspecified'},
  {message:'退换规则了解了，再说说平时应该怎么保养手链。',topic:'care',intent:'unspecified'}]},
 {id:'pause-and-resume',turns:[
  {message:'最近家里事情多，想找人聊聊。',topic:'chat',intent:'unspecified'},
  {message:'首饰今天不看了，别推荐了。',topic:'selection',intent:'pause'},
  {message:'刚才说不看了，现在改主意了，继续挑一条手链。',topic:'selection',intent:'continue'},
  {message:'朋友说她不买了，不用管她，我还想看手链。',topic:'selection',intent:'unspecified'}]},
 {id:'meta-and-mixed',turns:[
  {message:'假如客户说不想买了，你一般会怎么接话？',topic:'unknown',intent:'unspecified'},
  {message:'我是在测试你的说话方式，不是在说我要停止选购。',topic:'unknown',intent:'unspecified'},
  {message:'换个实际问题：这枚旧戒指能改款吗，还是回收更合适？',topic:'unknown',intent:'unspecified'},
  {message:'先不谈回收，单独说说改款一般要了解哪些情况。',topic:'remodel',intent:'unspecified'}]}
];
