import {readFileSync,writeFileSync} from 'node:fs';import {createHash} from 'node:crypto';
const definitions=[
 ['intent-comparison-2026-11-03T04-03-31.636Z/results.json','先分清人物和语境','真实模型对照；18道当轮新题','new'],
 ['intent-schema-2026-11-03T04-13-22.113Z/results.json','约束返回格式','真实模型对照；12道当轮新题','new'],
 ['intent-choice-2026-11-03T04-21-58.499Z/results.json','核对真实选择与输出指令','真实模型对照；12道当轮新题','new'],
 ['intent-combined-preflight.json','同时满足两项检查','历史结果回放；复用上一轮全部16题，无新模型调用','all'],
 ['intent-frame-2026-11-03T04-34-46.863Z/results.json','先说明谁、什么情境、想做什么','真实模型方案对照；8道当轮新题；A三次调用、B一次调用','new']
];
const entries=definitions.map(([file,title,method,split])=>{
 const bytes=readFileSync(new URL('./evidence/'+file,import.meta.url)),r=JSON.parse(bytes),summary=r.summary[split];
 return{title,method,source:file,sourceSha256:createHash('sha256').update(bytes).digest('hex'),createdAt:r.createdAt,A:summary.A,B:summary.B,passed:(r.candidateMeetsExploratoryGate??r.candidateMeetsPreflight)===true};
});
const diagnosticFile='understanding-probe-2026-11-03T04-42-20.654Z/results.json',diagnosticBytes=readFileSync(new URL('./evidence/'+diagnosticFile,import.meta.url)),probe=JSON.parse(diagnosticBytes);
const diagnostics=[{title:'基础理解：直接是/否与固定格式',source:diagnosticFile,sourceSha256:createHash('sha256').update(diagnosticBytes).digest('hex'),scope:'只测是否真实想继续选购，不改变客户状态，不代表完整接待通过。',summary:probe.summary.new,dimensions:probe.dimensions,pairs:probe.pairs}];
const bilingualFile='understanding-bilingual-2026-11-03T04-53-29.969Z/results.json',bilingualBytes=readFileSync(new URL('./evidence/'+bilingualFile,import.meta.url)),bilingual=JSON.parse(bilingualBytes);
diagnostics.push({title:'逐句等义的中英文理解诊断',source:bilingualFile,sourceSha256:createHash('sha256').update(bilingualBytes).digest('hex'),scope:'12对开发者翻译句，含2对历史题；没有独立双语审校。仅诊断，不改变客户状态。',labels:{A:'中文',B:'英文'},summary:{A:bilingual.summary.all.zh,B:bilingual.summary.all.en},note:'同对均正确 '+bilingual.summary.all.bothCorrect+'/12；答案不一致 '+bilingual.summary.all.disagree.length+'对；同对均错误 '+bilingual.summary.all.bothWrong.length+'对。'+bilingual.summary.new.zh.total+'对新题：中文'+bilingual.summary.new.zh.correct+'/'+bilingual.summary.new.zh.total+'、英文'+bilingual.summary.new.en.correct+'/'+bilingual.summary.new.en.total+'。不是整体准确率或上线验收。'});
const modifierFile='understanding-modifier-2026-11-03T04-58-45.099Z/results.json',modifierBytes=readFileSync(new URL('./evidence/'+modifierFile,import.meta.url)),modifier=JSON.parse(modifierBytes);
diagnostics.push({title:'不改变选购意愿的修饰语对照',source:modifierFile,sourceSha256:createHash('sha256').update(modifierBytes).digest('hex'),scope:'8组真实选购、4组否定对照。原句与修饰句意愿标签相同；一次采样，仅诊断。',labels:{A:'原句',B:'加修饰语'},summary:{A:modifier.summary.all.base,B:modifier.summary.all.modified},note:'从正确变错 '+modifier.summary.all.harmfulFlips.length+'组；真实选购修饰句 '+modifier.summary.positive.modified.correct+'/'+modifier.summary.positive.modified.total+'。未连接接待状态。'});
const scopeFile='understanding-scope-2026-11-03T05-00-23.835Z/results.json',scopeBytes=readFileSync(new URL('./evidence/'+scopeFile,import.meta.url)),scopeRun=JSON.parse(scopeBytes);
diagnostics.push({title:'否定作用范围的单次判断修正',source:scopeFile,sourceSha256:createHash('sha256').update(scopeBytes).digest('hex'),scope:'同题、同模型、同格式，每方案一次调用；仅替换问题说明。4道历史题和8道新题，不是接待上线验收。',labels:{A:'原问题',B:'否定范围修正'},summary:scopeRun.summary.all,note:'新题：原问题 '+scopeRun.summary.new.A.correct+'/'+scopeRun.summary.new.A.total+'，修正 '+scopeRun.summary.new.B.correct+'/'+scopeRun.summary.new.B.total+'；历史退步 '+scopeRun.summary.development.regressions.length+'题。修正会把英文输出指令误认成真实选购，候选不放行。'});
const modelFile='understanding-model-2026-11-03T05-08-50.705Z/results.json',modelBytes=readFileSync(new URL('./evidence/'+modelFile,import.meta.url)),modelRun=JSON.parse(modelBytes);
diagnostics.push({title:'本机4B与8B同题对照',source:modelFile,sourceSha256:createHash('sha256').update(modelBytes).digest('hex'),scope:'12道已知题加8道新题；同问题、同格式。模型家族不同，8B关闭思考；不能只归因于参数量。没有切换接待模型。',labels:{A:'4B',B:'8B'},summary:modelRun.summary.all,note:'8道新题两模型均8/8。后续请求响应中位数：4B '+(modelRun.runtime.A.subsequentMedianMs/1000).toFixed(2)+'秒，8B '+(modelRun.runtime.B.subsequentMedianMs/1000).toFixed(2)+'秒；均不含首条，非服务承诺。两模型各有一道不同的已知错误，不建议据此直接升级。'});
writeFileSync(new URL('./acceptance-summary.json',import.meta.url),JSON.stringify({generatedAt:new Date().toISOString(),scope:'仅本机模拟实验；不同题集不能横向排名或合并成准确率。没有本人相似度、真实微信、真实服务或销售转化验收。',entries,diagnostics},null,2));
