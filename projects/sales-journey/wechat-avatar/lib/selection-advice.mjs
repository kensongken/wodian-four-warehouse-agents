// Literal preference-to-catalog matching. Advice only: never orders or guaranteed fit.
const terms={简单:/简洁|简单|简约|细节少/u,简约:/简洁|简单|简约|细节少/u,低调:/低调|不抢眼/u,设计感:/设计感/u,宽一点:/较宽|轮廓宽|宽一些/u,细一点:/较细|细一些|细款/u,显眼:/显眼|存在感/u};
export function selectionAdvice(catalog,needs={}){
 const wanted=needs.style?.values||[],excluded=needs.style?.excluded||[];
 const candidates=catalog.map(p=>{const features=typeof p.features==='string'?p.features:'';return{sku:p.sku,features,matched:wanted.filter(t=>terms[t]?.test(features)),conflicts:excluded.filter(t=>terms[t]?.test(features)),available:Number.isFinite(p.stock)?p.stock>0:null};});
 const eligible=candidates.filter(p=>p.available!==false&&!p.conflicts.length&&p.matched.length),best=Math.max(0,...eligible.map(p=>p.matched.length)),leaders=eligible.filter(p=>p.matched.length===best);
 const winner=leaders.length===1&&leaders[0].matched.length===wanted.length?leaders[0]:null;
 return{source:'literal_branch_preferences_and_catalog_features',basis:'style_only_not_guaranteed_fit',recommendedSku:winner?winner.sku:null,decision:winner?'unique_style_match':'no_unique_style_match',wanted:[...wanted],excluded:[...excluded],candidates};
}
export function selectionReplyIssues(reply,advice,goal){
 if(!advice)return[];
 for(const p of advice.candidates){const l=p.sku.match(/(?:^|-)([AB])$/u)?.[1];if(!l||/较细|偏细|更细|细款|细一点/u.test(p.features))continue;const claim=new RegExp('(?:DEMO[- ]?)?'+l+'[^。；;]{0,16}(?:偏细|更细|细款|细一点)','u').exec(reply);if(claim&&!/不能说|无法判断|不代表|不一定/u.test(reply.slice(Math.max(0,claim.index-6),claim.index)))return['目录没有'+p.sku+'的宽细资料，不能把线条简洁说成整体偏细或更细。只引用它的原特点：'+p.features+'。'];}
 const useQuestion=/[?？]/u.test(reply)&&/(?:上班|通勤|日常).{0,10}(?:戴|用)|(?:戴|用).{0,10}(?:上班|通勤|日常)/u.test(reply);
 if(goal==='idle_concern'&&!useQuestion&&!/平时|习惯|机会|场合|频率|先不买|不急|暂|缓|先想|常戴/u.test(reply))return['客户怕闲置，本轮不重复推荐款式。问一个实际佩戴习惯，或给一个判断使用机会的小办法；不保证会戴、不需要为了销售劝买。'];
 if(goal!=='compare'||!advice.recommendedSku)return[];
 const sku=advice.recommendedSku,letter=sku.match(/(?:^|-)\s*([AB])$/u)?.[1];if(!letter)return[];
 const name='(?:DEMO[- ]?)?'+letter+'(?:款|手链)?';
 const choose=new RegExp('(?:先看|先选|建议|推荐|倾向|会选|选|看中)(?:你|就|先|更|我|可以|这|演练|款|\\s|，|,)*'+name+'|'+name+'(?:吧|更符合|更贴近)','u');
 const rejects=new RegExp('(?:不要|别|不|不建议)(?:先)?(?:选|看|推荐)?(?:DEMO[- ]?)?'+letter,'u');
 const other=advice.candidates.filter(p=>p.sku!==sku).some(p=>{const l=p.sku.match(/(?:^|-)([AB])$/u)?.[1];return l&&new RegExp('(?:DEMO[- ]?)?'+l+'(?:款|手链)?','u').test(reply);});
 const selected=advice.candidates.find(p=>p.sku===sku);const groundedReason=selected?.matched.some(t=>terms[t]?.test(reply));
 const startsWithChoice=new RegExp('^(?:嗯[，, ]*)?'+name,'u').test(reply);
 const implicitChoice=startsWithChoice;
 const wrongChoice=advice.candidates.filter(p=>p.sku!==sku).some(p=>{const l=p.sku.match(/(?:^|-)([AB])$/u)?.[1];return l&&new RegExp('(?:先看|先选|建议|推荐|倾向|会选|选)(?:你|就|先|更|我|可以|这|演练|款|\\s|，|,)*(?:DEMO[- ]?)?'+l+'|(?:DEMO[- ]?)?'+l+'(?:款)?吧','u').test(reply);});
 if((!choose.test(reply)&&!implicitChoice)||rejects.test(reply)||wrongChoice||!groundedReason||!other)return['客户要你帮取舍。按本轮明确风格与目录，先建议'+sku+'，说清一个匹配理由并对照另一款；只用目录原特点：'+advice.candidates.map(p=>p.sku+'：'+p.features).join('；')+'。简洁不代表更细。不要只介绍另一款特点；这是风格建议，不保证适合或催买。'];
 return[];
}
