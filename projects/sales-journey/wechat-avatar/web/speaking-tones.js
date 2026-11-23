// System base tones are starting points, never owner samples or shop facts.
export const speakingTones=Object.freeze([
 {id:'friend',label:'朋友',description:'自然亲切，像面对面聊天；不假装已经认识客户。',instruction:'用朋友式的自然短句，亲切有分寸，不装熟、不调侃、不擅自改称呼。'},
 {id:'warm',label:'热情',description:'积极接话、主动帮选，保持少催买。',instruction:'积极热情地接话，主动给一个有用帮助；不堆感叹号、不夸外貌、不催买。'},
 {id:'normal',label:'平常',description:'平实、直接、不刻意热络。',instruction:'用平实直接的日常短句，不刻意热络、不写服务口号。'},
 {id:'professional',label:'专业',description:'先给依据和判断，再说下一步；不用术语撑场面。',instruction:'先给有依据的判断，再说一个具体理由或下一步；用大白话，不堆术语、不用无关细节装专业。'},
 {id:'gentle',label:'温柔',description:'耐心、语气柔和，尊重客户的犹豫。',instruction:'语气温和耐心，简短接住犹豫，不长篇安慰、不讲鸡汤、不替客户做决定。'}
]);
export const speakingTone=id=>speakingTones.find(t=>t.id===id)||null;
export function withSpeakingTone(request,toneId){
 const tone=speakingTone(toneId);if(!tone)return request;
 return{...request,messages:request.messages.map(m=>m.role==='system'?{...m,content:m.content+' 本轮基础口吻：'+tone.label+'。'+tone.instruction+' 基础口吻只作表达起点；已确认的本人表达优先用于措辞，先完成客户当前诉求，不照抄样本。商品事实、业务授权与少催买边界始终优先，不因切换口吻而改变。'}:m)};
}
