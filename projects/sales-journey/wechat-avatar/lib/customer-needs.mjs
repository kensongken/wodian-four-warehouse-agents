// Literal, branch-scoped intent evidence. Never shop facts or permanent customer traits.
const dimensions={
 wearer:{label:'佩戴对象',terms:['给我自己','给自己','自己戴','自己佩戴','自用','自戴','送妈妈','送母亲','给妈妈','给母亲','给我妈','送给妈妈','送给母亲','送给朋友','送朋友','送礼','送人']},
 category:{label:'品类',terms:['手链','手镯','项链','戒指','耳饰','耳环']},
 occasion:{label:'场景',terms:['上班','通勤','日常','周末','聚会']},
 style:{label:'风格重点',terms:['简单','简约','低调','显眼','设计感','宽一点','细一点','花哨','复杂']}
};
const clauses=text=>text.split(/[，,。；;！？!?]/u);
const negative=prefix=>/不(?:再|太)?(?:看|选|考虑|想要|要|想|在|用于|喜欢|爱)|不是|别.{0,3}(?:绕到|选)|(?:不|不太|别|别太|不要太)$/u.test(prefix);
export function customerNeedState(statements){
 const fields={};
 for(const [index,statement]of statements.entries()){
  const quote=typeof statement==='string'?statement:statement.question;
  if(typeof quote!=='string')continue;
  const source=typeof statement==='string'?{type:'branch_quote',index}:statement.source||{type:'branch_quote',index};
  if(/[?？]|是不是|不知道|不确定|怎么|如何|哪种|吗[。！!]*$/u.test(quote)){
   // Only a separate, explicit first-person preference clause in an already confirmed self-use branch.
   const self=fields.wearer?.values.some(v=>/给(?:我)?自己|自己戴|自己佩戴|自用|自戴/u.test(v));
   const said=self&&quote.match(/(?:^|[，,；;])(?:但|不过|而)?我(?:自己)?(?:就|更)?(?:喜欢|偏爱)([^，,。！？!?；;]{1,30})(?=[，,。！？!?；;]|$)/u);
   if(said&&!/还是|或者|吗|不|别|没/u.test(said[1])){const values=dimensions.style.terms.filter(t=>said[1].includes(t));if(values.length){const field=fields.style??={label:dimensions.style.label,values:[],excluded:[]};field.values=values;field.quote=quote;field.source=source;field.excluded=field.excluded.filter(e=>!values.includes(e.value));}}
   continue;
  }
  for(const [key,definition]of Object.entries(dimensions)){
   const yes=new Set(),no=new Set();
   for(const clause of clauses(quote))for(const term of definition.terms){let start=0,at;while((at=clause.indexOf(term,start))!==-1){(negative(clause.slice(Math.max(0,at-8),at))?no:yes).add(term);start=at+term.length;}}
   if(!yes.size&&!no.size)continue;
   if(yes.size>1&&/还是|或者/u.test(quote)&&!no.size)continue;
   const field=fields[key]??={label:definition.label,values:[],excluded:[]};
   if(no.size){field.excluded=field.excluded.filter(x=>!no.has(x.value));for(const value of no)field.excluded.push({value,quote,source});field.values=field.values.filter(x=>!no.has(x));}
   if(yes.size){field.values=[...yes];field.quote=quote;field.source=source;field.excluded=field.excluded.filter(x=>!yes.has(x.value));}
  }
 }
 return{source:'literal_current_branch_quotes',fields};
}
export function customerNeedSummary(state){
 return Object.fromEntries(Object.entries(state.fields).map(([key,field])=>[key,{values:field.values,excluded:field.excluded.map(x=>x.value)}]));
}
export function customerNeedIssues(state,reply){
 const issues=[];
 for(const field of Object.values(state.fields))for(const excluded of field.excluded){
  const stale=clauses(reply).some(clause=>{const at=clause.indexOf(excluded.value);if(at===-1)return false;const prefix=clause.slice(Math.max(0,at-10),at);return!negative(prefix)&&!/原来|原先|以前|之前|先前|原本/u.test(prefix);});
  if(stale)issues.push('客户已明确排除“'+excluded.value+'”，不要把它写成当前需求；按本轮需求核对表与最新原话回答。');
 }
 return issues;
}
