export const SUPPORT_OPTIONS=[{concern:'fit',label:'怕不适合'},{concern:'compare',label:'两个风格方向拿不准'},{concern:'consider',label:'想再考虑一下'}];
export function supportReply(concern,needs,tone='plain'){
 const prefixes={plain:'',friend:'咱们慢慢看。',warm:'好呀。',professional:'我们先把顾虑理清。'};
 if(!Object.hasOwn(prefixes,tone)||!SUPPORT_OPTIONS.some(c=>c.concern===concern))throw Error('Invalid support reply');
 if(concern==='consider')return prefixes[tone]+'没事，先考虑，不用现在决定。我们先停在这里，想继续看时再说就好。';
 const wearer={self:'给自己选',mother:'给妈妈选',friend:'给朋友选',other:'给收礼人选'}[needs.recipient]||'选首饰';
 const style={simple:'你已经选了偏简洁的方向，我们先保留这一点。',bold:'你已经选了偏有设计感的方向，我们先保留这一点。',unsure:'风格暂时不确定也没关系。'}[needs.style]||'';
 return prefixes[tone]+(concern==='fit'?`你是想${wearer}，担心不合适很正常，先不用急着定。${style}可以先按常穿的衣服和佩戴场合判断。你更想先调整用途，还是风格？`:`两个方向拿不准也没关系。${style}我们可以先比较简洁和有设计感这两个风格方向，看看哪种更贴近日常穿搭，再决定是否调整。`);
}
