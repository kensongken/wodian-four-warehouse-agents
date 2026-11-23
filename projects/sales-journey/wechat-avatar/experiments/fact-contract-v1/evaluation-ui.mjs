const $=id=>document.getElementById(id);
try{
 const response=await fetch('/evaluation.json',{cache:'no-store'});if(!response.ok)throw Error('验收记录暂时不可读取，不能判断是否通过。');const data=await response.json();
 if(!Array.isArray(data.entries)||!data.entries.length)throw Error('验收记录不完整，不能判断是否通过。');
 $('evaluation-scope').textContent=data.scope;$('evaluation-time').textContent='快照生成时间：'+new Date(data.generatedAt).toLocaleString('zh-CN');
 if(data.conversationNotebook){
  const item=data.conversationNotebook,section=document.createElement('section');section.className='settings';section.id='notebook-comparison';
  const h=document.createElement('h2');h.textContent='连续对话：正确需求卡能不能解决接话问题？';section.append(h);
  for(const text of [item.method,item.limits,'结论：'+item.verdict]){const p=document.createElement('p');p.textContent=text;section.append(p);}
  const table=document.createElement('table');table.style.width='100%';
  for(const values of [['本轮逐句复核','A 当前方式','B 正确需求卡'],['两项检查均通过且无已发现事实问题',item.A.turnPassed+'/'+item.A.total,item.B.turnPassed+'/'+item.B.total],['含未核实商品或服务说法的轮数',item.A.unsupportedBusinessClaimTurns,item.B.unsupportedBusinessClaimTurns],['请求失败或无效返回',item.A.invalid,item.B.invalid]]){const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}table.append(tr);}section.append(table);
  for(const example of item.examples){const p=document.createElement('p');p.textContent=example;section.append(p);}
  if(item.serviceMethodsStudy){const p=document.createElement('p');p.textContent=item.serviceMethodsStudy;section.append(p);}
  if(item.followUp){const p=document.createElement('p');p.textContent=item.followUp;section.append(p);}
  const source=document.createElement('p');source.textContent='逐句检查与原始回复：'+item.source;section.append(source);$('evaluation-results').append(section);
 }
 for(const entry of data.entries){
  const section=document.createElement('section');section.className='settings';const heading=document.createElement('h2');heading.textContent=entry.title;section.append(heading);
  for(const text of [entry.method,'验收结论：'+(entry.passed?'达到当轮实验门槛，仍不是正式上线验收':'未达标，未接入正式接待')]){const p=document.createElement('p');p.textContent=text;section.append(p);}
  const table=document.createElement('table');table.style.width='100%';const head=document.createElement('tr');for(const label of ['指标','A 对照','B 候选']){const th=document.createElement('th');th.textContent=label;head.append(th);}table.append(head);
  for(const [label,key]of [['最终意愿正确','correct'],['错误恢复推荐','falseResume'],['无效返回','invalid']]){const tr=document.createElement('tr');for(const value of [label,...['A','B'].map(side=>key==='correct'?`${entry[side][key]}/${entry[side].total}`:String(entry[side][key]??entry[side].invalidFinal??'未记录'))]){const td=document.createElement('td');td.textContent=value;tr.append(td);}table.append(tr);}section.append(table);
  const p=document.createElement('p');p.textContent='证据文件：'+entry.source;section.append(p);$('evaluation-results').append(section);
 }
 if(!Array.isArray(data.diagnostics))throw Error('基础理解诊断记录缺失，不能判断是否通过。');
 for(const entry of data.diagnostics){
  const section=document.createElement('section');section.className='settings';const h=document.createElement('h2');h.textContent=entry.title;section.append(h);
  const scope=document.createElement('p');scope.textContent=entry.scope;section.append(scope);
  const labels=entry.labels||{A:'直接回答',B:'固定格式'};
  for(const [label,key]of [[entry.labels?'本组题答对':'当轮新题答对','correct'],['误认存在真实选购意愿','falsePositive'],['漏认真实选购意愿','falseNegative']]){const p=document.createElement('p');p.textContent=label+'：'+['A','B'].map(side=>labels[side]+' '+(key==='correct'?`${entry.summary[side][key]}/${entry.summary[side].total}`:entry.summary[side][key]+'次')).join('；');section.append(p);}
  if(entry.dimensions)for(const [dimension,label]of [['speaker','本人/转述'],['hypothesis','现实/假设'],['output_control','选购/输出要求']]){const p=document.createElement('p'),r=entry.dimensions[dimension];p.textContent=label+'：直接回答 '+r.A.correct+'/'+r.A.total+'；固定格式 '+r.B.correct+'/'+r.B.total;section.append(p);}
  if(entry.note){const p=document.createElement('p');p.textContent=entry.note;section.append(p);}
  const file=document.createElement('p');file.textContent='证据文件：'+entry.source;section.append(file);$('diagnostic-results').append(section);
 }
 $('evaluation-error').textContent='';
}catch(error){$('evaluation-results').replaceChildren();$('diagnostic-results').replaceChildren();$('evaluation-error').textContent=error.message||'验收记录读取失败，不能判断是否通过。';}
