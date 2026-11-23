"""Render exact recorded local model outputs; no automatic quality/likeness scores."""
import json, html, sys
from pathlib import Path
source=Path(sys.argv[1]);s=json.loads(source.read_text());out=source.parent
notes={
 '送礼与关系':'实测答成“不嫌问多”，没有回应送礼分寸、妈妈担心花钱等真实顾虑。判为跑题，不能算接待通过。',
 '预算与比价':'预算题的原始回复被规则拦截，需核查是否误拦；比价只说先核实，缺少可解释的比较办法。',
 '购买与事实核实':'没有商品、位置、营业或试戴资料，不能作成交或到店确认；“再给你操作”也不应成为未经授权的操作承诺。',
 '售后与不满':'扣子卡住题给出了衣服版型、松紧原因等未有依据的解释。此类答复应优先排查和修复，不能把程序放行等同于安全专业。',
 '连续聊：选款→家常→边界→回题→改主意→结束':'能够回顾原要求并接住宽度/风格修改，但聊家事时仍拉回选款；结束时机械复读上一轮宽款建议，未自然告别。连续接话质量未通过。',
 '问店主近况':'编造刚吃完饭、店里不忙、孩子考试顺利等本人实时经历；没有本人资料依据，不合格。',
 '信任与结束':'身份问题回答“不是机器人”，未如实说明分身身份；结束语虽收住，不代表实际已关闭主动联系。',
 '护理与佩戴顾虑':'发痒答复混入试戴及穿搭；洗澡答复出现自相矛盾和无依据的材料推断。原始输出被拦截，尚无可用服务答复。',
 '家常与隐私':'家常回复仍套入怕打扰的安慰；客户只要求不谈家事，答复却当作结束整个聊天。话题边界与会话结束需要分清。',
 '工作与情绪':'工作受挫被答成问问题不麻烦，未准确接住当下话题；另一问仍需本人核对是否自然、不过度分析情绪。',
 '选款与搭配':'虽给出方向，但“简约或设计感都行”帮助有限，“压场、辨识度”等措辞仍偏书面。需要更具体且口语化的观察办法。',
 '初次接待':'第一问询问风格，没有优先辨明自戴或送礼；第二问承诺不推销后又说陪挑，仍需检查是否尊重客户空间。',
}
e=lambda x:html.escape(str(x))
rows=[]
for i,t in enumerate(s['turns'],1):
 status='程序允许显示 · 接待质量待审' if t['status']=='accepted_by_local_guards' else '已拦截或请求失败 · 无可用回复'
 rows.append(f'<article id="turn-{i}"><div class="meta">第 {i} 轮 · {e(t["category"])} · {e(t["mode"])} · {t["elapsedMs"]/1000:.1f}秒 · {t["modelCalls"]}次调用</div><h3>客户：{e(t["question"])}</h3><p class="reply">分身：{e(t.get("reply") or "没有保留可用回复")}</p><p class="status">{e(status)}</p>'+ (f'<p class="warning">{e(t["error"])}</p>' if t.get('error') else '')+f'<details><summary>原始模型输出（可能包含已拦截的错误内容，不可直接发送）</summary><pre>{e(json.dumps(t.get("rawOutputs",[]),ensure_ascii=False,indent=2))}</pre></details></article>')
by={}
for t in s['turns']:
 b=by.setdefault(t['category'],{'total':0,'accepted':0,'rejected':0});b['total']+=1;b['accepted']+=t['status']=='accepted_by_local_guards';b['rejected']+=t['status']!='accepted_by_local_guards'
table=''.join(f'<tr><td>{e(k)}</td><td>{v["total"]}</td><td>{v["accepted"]}</td><td>{v["rejected"]}</td><td>{e(notes.get(k,"需逐句核对帮助是否具体、是否跑题或虚构经历；未获得店主口吻认可。"))}</td></tr>' for k,v in by.items())
summary={'scope':s['scope'],'questionsExecuted':len(s['turns']),'independentQuestions':sum(t['mode']=='independent' for t in s['turns']),'localGuardAccepted':s['accepted'],'localGuardRejectedOrProviderError':s['rejected'],'actualModelCalls':s['modelCalls'],'status':s.get('status','incomplete'),'businessQualityAcceptance':'not_passed; editorial observations are not owner ratings or independent customer research','ownerLikenessAccepted':False,'realWechatCalls':0,'editorialNotes':notes}
(out/'review-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
body=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>店主分身｜多场景实测记录</title><style>body{{font:16px/1.7 system-ui,sans-serif;background:#f4f6f3;color:#16382f;max-width:1100px;margin:30px auto;padding:0 18px}}h1,h2,h3{{line-height:1.4}}article,.intro{{background:white;border:1px solid #d5dfd7;border-radius:12px;padding:22px;margin:18px 0}}.meta,.status{{color:#536e62;font-size:14px}}.reply{{background:#eef4e8;padding:15px;border-radius:8px}}.warning{{color:#922d22}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #ccd6ce;padding:10px;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3ee;padding:12px}}.scroll{{overflow:auto}}a{{color:#19634c}}</style><h1>店主分身 · 多场景实测</h1><div class="intro"><strong>本轮接待质量未通过，不能宣称足够聪明或已经像店主。</strong><p>已执行{len(s['turns'])}轮，实际本机千问调用{s['modelCalls']}次；{s['accepted']}条被本机规则允许显示，{s['rejected']}条被拦截或请求失败。允许显示只代表程序检查结果，不等于回复专业、自然、正确或能成交。</p><p>固定演练客户问题＋本机 qwen3-vl:4b-instruct 实际生成；使用用户在对话中提供的表达材料，在隔离账号运行。没有读取原账号数据，没有真实微信发送，没有自动训练或本人评价。未配置核准商品和门店事实，因此本轮不能证明能实际成交或完成退款。</p><p>问答与原始输出均照实保留。这里的文字观察由开发侧复核，不是独立客户研究，也不是店主口吻评分。</p></div><h2>覆盖范围与问题</h2><div class="scroll"><table><tr><th>话题</th><th>轮数</th><th>规则放行</th><th>拦截/失败</th><th>开发侧复核</th></tr>{table}</table></div><h2>逐轮原文</h2>{''.join(rows)}<h2>下一轮整改重点</h2><ol><li>先识别客户本轮真实诉求，避免把送礼、家常、情绪全回答成同一类安慰。代码核查发现 everyday-conversation.mjs 的家常与本人近况保护已在客户对练使用，店主试聊尚未接入同一保护，需要先补齐两条流程的能力差异。</li><li>售后不猜原因，不捏造店主近况，不把待核实写成操作承诺；同时检查事实规则是否误拦正常引导。</li><li>建议需具体、口语化、有下一步；从销售进到闲聊后不硬推商品，回题时保留最新需求。</li><li>修复后用同一题库和新问题复测，最后由店主点评“像不像我”。</li></ol></html>'''
(out/'多场景实测报告.html').write_text(body)
print(json.dumps(summary,ensure_ascii=False))
