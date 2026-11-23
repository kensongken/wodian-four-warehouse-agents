const choices={recipient:['self','mother','friend','other'],category:['bracelet','necklace','earrings','ring','undecided'],occasion:['daily','work','party','birthday'],style:['simple','bold','unsure'],concern:['none','hesitant','no_buy']};
const labels={self:'自己',mother:'妈妈',friend:'朋友',other:'收礼人',bracelet:'手链',necklace:'项链',earrings:'耳环',ring:'戒指',undecided:'品类还没定',daily:'日常',work:'上班',party:'聚会',birthday:'生日礼物',simple:'简洁',bold:'有设计感',unsure:'风格还没定'};
export class SelectionGuide{
 #needs={recipient:null,category:null,occasion:null,budget:null,style:null,concern:'none'};#revision=0;
 static restore({needs,revision,tone}){
  if(!Number.isSafeInteger(revision)||revision<0||!needs||Array.isArray(needs)||Object.keys(needs).length!==5||!['recipient','category','occasion','budget','style'].every(k=>Object.hasOwn(needs,k)))throw Error('Invalid checkpoint');
  const guide=new SelectionGuide();guide.update({expectedRevision:0,confirmed:true,patch:needs,tone});guide.#revision=revision;return guide;
 }
 questionFor(field,tone='plain'){
  if(!['recipient','category','occasion','budget','style'].includes(field))throw Error('Invalid question field');
  const copy=new SelectionGuide();return copy.update({expectedRevision:0,confirmed:true,patch:{...this.#needs,[field]:null},tone}).reply;
 }
 update({expectedRevision,confirmed,patch,tone='plain'}){
  if(expectedRevision!==this.#revision)throw Error('Stale selection revision');
  if(confirmed!==true||!patch||typeof patch!=='object'||Array.isArray(patch)||Object.keys(patch).some(k=>!Object.hasOwn(this.#needs,k)))throw Error('Invalid confirmed selection');
  if(!['friend','warm','plain','professional'].includes(tone))throw Error('Invalid tone');
  for(const[k,v]of Object.entries(patch))if(v!==null&&(k==='budget'?!(v==='private'||Number.isSafeInteger(v)&&v>0&&v<=10000000):!choices[k].includes(v)))throw Error('Invalid selection field');
  if(Object.keys(patch).length===0)return this.preview(tone);
  const next={...this.#needs,...patch};
  // Style describes the wearer. Do not transfer one person's preference to another.
  if(Object.hasOwn(patch,'recipient')&&patch.recipient!==this.#needs.recipient&&!Object.hasOwn(patch,'style'))next.style=null;
  this.#needs=next;this.#revision++;
  return this.preview(tone);
 }
 preview(tone='plain'){
  if(!['friend','warm','plain','professional'].includes(tone))throw Error('Invalid tone');
  const n=this.#needs;const person=n.recipient==='self'?'你':n.recipient==='mother'?'妈妈':n.recipient==='friend'?'朋友':'收礼人';
  let nextField=null,content;
  if(n.concern==='no_buy')content='好，你慢慢看，今天不决定也可以。需要聊的时候再说。';
  else{
   nextField=['recipient','category','occasion','budget','style'].find(k=>n[k]===null)||null;
   const questions={recipient:'这次是给自己选，还是送给别人？',category:'你想先看手链、项链、耳环还是戒指？暂时没定也可以。',occasion:n.recipient==='self'?'你主要想在什么场合戴？':`这份首饰主要想让${person}在什么场合戴？`,budget:'有没有想控制的预算上限？暂时不想说也没关系。',style:n.recipient==='self'?'你平时穿戴是怎样的，更偏简洁还是有设计感？':`${person}平时更偏简洁，还是有设计感？不确定也可以。`};
   if(nextField)content=questions[nextField];
   else{
    const direction=n.style==='simple'?'可以先比较线条简洁、与实际常穿衣服容易协调的方向。':n.style==='bold'?'可以先比较轮廓或细节有特点的方向，再看会不会抢过整体穿搭。':'可以把简洁和有细节的两个方向放在一起比较，看更想保留哪种感觉。';
    content=`按${labels[n.recipient]}、${labels[n.occasion]}、${labels[n.category]}这个方向看。${direction}${n.budget==='private'?'预算先不展开。':`预算按你确认的${n.budget}元以内作为筛选条件。`}具体有没有对应商品、价格和库存，还需要真实商品资料核对。`;
   }
   if(n.concern==='hesitant'&&nextField===null)content='可以先比较线条和实际常穿衣服是否协调，不急着决定。'+content;
  }
  const prefix=n.concern==='no_buy'?'':{friend:'咱们慢慢挑。',warm:'好呀。',plain:'',professional:'我们先把选择方向理清。'}[tone];
  return {revision:this.#revision,needs:{...n},tone,nextField,reply:prefix+content,
   summary:Object.entries(n).filter(([k,v])=>v!==null&&k!=='concern').map(([k,v])=>k==='budget'?(v==='private'?'预算暂不透露':`预算上限${v}元`):labels[v]).join(' · '),
   source:'confirmed_synthetic_needs_and_bounded_service_plan',ownerVoiceLearned:false,productMatched:false,sent:false};
 }
}
