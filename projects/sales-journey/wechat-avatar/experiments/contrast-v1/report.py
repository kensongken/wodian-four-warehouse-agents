from pathlib import Path
import json,html,statistics,hashlib,sys
out=Path(sys.argv[1]);r=json.loads((out/'results.json').read_text());assert r.get('status')=='completed_not_business_acceptance';assert len(r['rows'])==33
E=lambda x:html.escape(str(x))
summary={'scope':'one unblinded exploratory run per arm; not statistical, owner or production acceptance','arms':{},'decision':'No production promotion based on this pilot','promptfooCLIExecuted':False,'originalWorkbenchChanged':False}
for arm in 'ABC':
 rows=[x for x in r['rows']if x['arm']==arm];non=[x for x in rows if not x.get('factControl')];attempts=[x for x in non if x['status']not in ['not_run_after_branch_failure','not_run_resource_busy']];dur=[x['elapsedMs']/1000 for x in attempts];phases=[t.get('response',{}).get('performance')for x in non for t in x.get('trace',[])];phases=[x for x in phases if x];summary['arms'][arm]={'plannedMessages':len(rows),'factControls':sum(bool(x.get('factControl'))for x in rows),'modelTurnsGenerated':sum(x['status']=='generated'for x in non),'modelTurnsPlanned':len(non),'errors':sum(x['status']=='error'for x in non),'branchNotRun':sum(x['status']=='not_run_after_branch_failure'for x in non),'resourceNotRun':sum(x['status']=='not_run_resource_busy'for x in rows),'modelCalls':sum(x.get('modelCalls',0)for x in rows),'medianAttemptedTurnSeconds':round(statistics.median(dur),2)if dur else None,'meanRecordedModelLoadSecondsPerCall':round(statistics.mean(p['modelLoadMs']/1000 for p in phases),2)if phases else None,'qualityScore':None,'ownerLikenessScore':None}
review=json.loads((out/'review.json').read_text()) if (out/'review.json').exists() else None
summary['reviewFile']='review.json' if review else None
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>店主分身 A/B/C 探索对照</title><style>body{font:16px/1.7 system-ui;color:#20392e;max-width:1250px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%;font-size:14px;table-layout:fixed}td,th{border:1px solid #cbd7d0;padding:12px;vertical-align:top;word-wrap:break-word}summary{cursor:pointer}pre{white-space:pre-wrap;font-size:12px}.note{background:#f3f6f2;padding:18px}</style><h1>店主分身 A/B/C 探索对照</h1><p>2026年11月3日 · A为v61+4B；B为同4B分层流程；C为同分层流程+8B文本模型。现有操作台保持v61。</p><div class="note"><b>这不是生产验收，也不是准确率排名。</b><p>每组11条预定消息，其中1条核准事实控制与其他组共用确定性查找；剩余10条才涉及模型回复。单次运行，无统计显著性、店主盲评或真实微信发送。生成数只表示协议/当前机械检查通过，不表示正确、专业或像本人。</p></div><h2>执行结果</h2><table><tr><th>方案</th><th>模型消息生成/预定</th><th>错误/分支未运行</th><th>模型调用数</th><th>尝试消息耗时中位数</th></tr>'''
if review:
 marker='<h2>执行结果</h2>'
 findings='<h2>本轮决策</h2><p><b>'+E(review['decision'])+'</b></p><p>'+E(review['nextStep'])+'</p><p>以下为开发者非盲审阅，不是独立CTO验收或店主认可。原始回答全部保留，可逐条核查。</p>'
 page=page.replace(marker,findings+marker)
for arm,m in summary['arms'].items():page+='<tr>'+''.join('<td>'+E(x)+'</td>' for x in [arm,f"{m['modelTurnsGenerated']}/{m['modelTurnsPlanned']}",f"{m['errors']}/{m['branchNotRun']}",m['modelCalls'],str(m['medianAttemptedTurnSeconds'])+'秒'])+'</tr>'
page+='</table><p>耗时包含成功与失败的尝试，未运行不计为0秒。不含每场景创建隔离账本的准备时间；事实控制另列。按A/B/C顺序运行，冷启动、缓存、上下文配置和宿主机负载不同，因此不推断模型的固有速度优劣。分阶段加载/读入/生成耗时见原始JSON。</p><h2>协议与局限</h2><p>同一fixture提供相同原始资料和客户话语；A按旧检索选资料，B/C提供分开的资料与状态、内容和口吻两次生成，且没有复制A全部词法检查。这是整套流程比较，不能单独归因于两阶段或框架。B/C引用ID错误、JSON协议失败、无依据动作均记录，不用固定话术补成功。</p><p>A完成后因16GB内存与约4GB可用磁盘调整执行器资源协议并恢复，保留所有完成场景、不择优重跑；可能中断的一次在途请求未计为成功，总额外计算耗时未知。运行C前等待4B缓存自然释放，等待时间'+E(r.get('resourceWaitMs',0))+'毫秒；未强行卸载或下载模型。源码哈希、模型digest、执行器变更epochs详见results.json。</p><p>Promptfoo未安装或执行；仅提供只读回放适配器及配置，并完成接口单测。实际模型由独立Node运行器调用，不使用云端评分，不把回放耗时当推理耗时。模型打分和本人相似度均未在本报告中伪造。</p><h2>同题实际回复</h2>'
keys=[]
for row in r['rows']:
 key=(row['scenario'],row['turn']);
 if key not in keys:keys.append(key)
for scenario,turn in keys:
 rows=[x for x in r['rows']if x['scenario']==scenario and x['turn']==turn];q=rows[0]['question'];page+='<h3>'+E(scenario)+' · '+str(turn+1)+'</h3><p>客户：'+E(q)+'</p><p>评审标准：'+E(rows[0].get('rubric',''))+'</p><table><tr><th>A v61＋4B</th><th>B 分层＋4B</th><th>C 分层＋8B</th></tr><tr>'
 for arm in 'ABC':
  x=next(x for x in rows if x['arm']==arm);page+='<td>'+E(x.get('reply')or('未交付：'+x.get('error','未运行')))+'</td>'
 page+='</tr></table>'
 for x in rows:
  if x.get('trace'):
   page+='<details><summary>'+E(x['arm'])+' 原始阶段输出</summary>'
   for t in x['trace']:page+='<p>'+E(t['stage'])+'</p><pre>'+E(t.get('response',{}).get('choices',t.get('error','')))+'</pre>'
   page+='</details>'
if review:
 page+='<h2>失败定位与服务复核</h2><p>A的1条未交付是安全拦截；B的4条未交付是资料引用协议错误。二者不能合并解释为相同原因的故障，也不能用生成率充当准确率。</p>'
 for finding in review['findings']:
  page+='<p><b>'+E(finding['arm']+' / '+finding['scenario']+' / '+finding['stage'])+'</b>：'+E(finding['observation'])+'</p>'
page+='<h2>下一决策</h2><p>不因开源框架或较大模型而自动替换主流程。先按任务、事实/动作、边界、多轮记忆、内容到口吻的语义保持逐条复核，再确定是否值得做消融和重复实验。保留A基线，B/C任何候选均需完整新留出题、多轮测试与店主盲评，才能讨论放行。</p><p>下一轮计划见 next-experiment.json：只比较“全部核准资料”与“相关核准资料”。12条新问题、两组、两次重复，共48次预定回复；尚未执行，不计为本轮成绩。先验证资料是否把回答带偏，不追加场景关键词补丁。</p></html>'
(out/'对照实验报告.html').write_text(page)
print(json.dumps(summary,ensure_ascii=False))
