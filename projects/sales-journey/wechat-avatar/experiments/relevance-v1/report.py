"""Render a completed paired run; never call a model or silently fill quality ratings."""
import html
import json
import statistics
import sys
from pathlib import Path

out = Path(sys.argv[1])
raw = json.loads((out / 'results.json').read_text())
assert raw['status'] == 'completed_not_business_acceptance'
assert len(raw['rows']) == len({x['id'] for x in raw['rows']}) == 48
review = json.loads((out / 'review.json').read_text()) if (out / 'review.json').exists() else None
ratings = {x['id']: x for x in review['rows']} if review else {}
if review:
    assert set(ratings) == {x['id'] for x in raw['rows']}
    assert review['type'] == 'developer_unblinded_not_owner_acceptance'

summary = {
    'scope': raw['designScope'], 'baselineSha': raw['baselineSha'],
    'plannedReplies': 48, 'ownerRatingsAssigned': 0, 'realWechatCalls': 0,
    'independentReviewers': 0, 'accuracy': None, 'ownerLikeness': None,
    'humanPreselectedRelevance': True, 'automatedRetrieverTested': False,
    'pairedCompletion': {'both': 0, 'allOnly': 0, 'relevantOnly': 0, 'neither': 0},
    'firstAttemptIncludingColdStart': {'id': raw['rows'][0]['id'], 'elapsedSeconds': round(raw['rows'][0]['elapsedMs'] / 1000, 2)},
    'arms': {}, 'reviewType': review['type'] if review else None,
}
for arm in ['all', 'relevant']:
    rows = [x for x in raw['rows'] if x['arm'] == arm]
    generated = [x for x in rows if x['status'] == 'generated']
    phases = [t['response']['performance'] for x in rows for t in x['trace']
              if t.get('response', {}).get('performance')]
    metrics = {
        'planned': 24, 'delivered': len(generated),
        'protocolFailures': sum(x.get('failureClass') == 'protocol' for x in rows),
        'transportFailures': sum(x.get('failureClass') == 'transport_or_timeout' for x in rows),
        'guardOrOtherFailures': sum(x.get('failureClass') == 'guard_or_other' for x in rows),
        'modelCalls': sum(x['modelCalls'] for x in rows),
        'medianAttemptSeconds': round(statistics.median(x['elapsedMs'] / 1000 for x in rows), 2),
        'meanModelLoadSeconds': round(statistics.mean(x['modelLoadMs'] / 1000 for x in phases), 2) if phases else None,
        'deliveredAndOnTaskDeveloperReview': sum(ratings[x['id']]['onTask'] is True for x in generated) if review else None,
        'unsupportedFinalRepliesDeveloperReview': sum(ratings[x['id']]['supportedFinal'] is False for x in generated) if review else None,
        'finalSupportUncertainDeveloperReview': sum(ratings[x['id']]['supportedFinal'] is None for x in generated) if review else None,
        'simulationLabelMissingDeveloperReview': sum(ratings[x['id']]['simulationLabelPreserved'] is False for x in generated) if review else None,
        'semanticDriftDeveloperReview': sum(ratings[x['id']]['meaningPreserved'] is False for x in generated) if review else None,
    }
    summary['arms'][arm] = metrics
case_ids = list(dict.fromkeys(x['caseId'] for x in raw['rows']))
for case_id in case_ids:
    for repeat in [1, 2]:
        pair = [x for x in raw['rows'] if x['caseId'] == case_id and x['repeat'] == repeat]
        assert len(pair) == 2
        flags = {x['arm']: x['status'] == 'generated' for x in pair}
        label = 'both' if all(flags.values()) else 'allOnly' if flags['all'] else 'relevantOnly' if flags['relevant'] else 'neither'
        summary['pairedCompletion'][label] += 1
summary['decision'] = review['decision'] if review else 'Awaiting qualitative review; no production promotion'
(out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
E = lambda value: html.escape(str(value))
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>店主分身：资料筛选对照实验</title><style>
body{font:16px/1.75 system-ui;color:#20392e;max-width:1260px;margin:32px auto;padding:0 20px}
table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}th,td{border:1px solid #cbd7d0;padding:12px;vertical-align:top;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.notice{padding:18px;background:#f1f5f0}summary{cursor:pointer}.error{color:#943d2b}
</style><h1>店主分身：资料筛选对照实验</h1><p>2026年11月3日 · 两组均使用同一本机千问4B、同一口吻和分层流程。现有工作台保持v61。</p>
<div class="notice"><b>只改变一件事：给全部资料，还是只给相关资料。</b><p>12条新问题 × 两组 × 两次重复，共48次预定回复。每对使用相同的随机参数，先后顺序平衡。相关资料由开发者运行前指定，还没有验证系统自己找资料的能力；这里只有两条模拟资料，不能推广为大规模门店资料库结论。</p><p>生成数不是准确率。无真实微信发送、店主盲评、独立评审或生产放行。多轮题都给相同的客户历史原话，不依赖前面生成的回复，尚未测试整段连续接待。</p></div>
'''
page += '<h2>本轮决策</h2><p><b>' + E(summary['decision']) + '</b></p>'
if review:
    for finding in review['findings']:
        page += '<p>' + E(finding) + '</p>'
page += '<h2>完整交付与失败</h2><p>格式或引用错误，就像填表时把引用资料的编号栏填错：即使文字里有合理内容，程序也不把它算成已回复。安全拦截另列，不能混成同一种故障。</p><table><tr><th>组别</th><th>生成/预定</th><th>格式或引用栏填错</th><th>连接或超时</th><th>拦截或其他</th><th>尝试耗时中位数</th></tr>'
for arm, title in [('all', '全部资料'), ('relevant', '仅相关资料')]:
    m = summary['arms'][arm]
    values = [title, f"{m['delivered']}/24", m['protocolFailures'], m['transportFailures'], m['guardOrOtherFailures'], f"{m['medianAttemptSeconds']}秒"]
    page += '<tr>' + ''.join('<td>' + E(x) + '</td>' for x in values) + '</tr>'
page += '</table><p>耗时包括失败尝试；协议失败可能提前结束，不能把更早失败当作效率改善。首条请求包含冷启动；缓存、请求长度和宿主机负载会影响本机耗时，不用来声称通用模型速度。</p>'
if review:
    page += '<h2>开发者逐条复核</h2><p>非盲审阅，不是店主认可或总体准确率。任务相关性：是否回应核心诉求或提出相关澄清，不等于完整业务达标；未交付单列。事实依据：最终回复是否添加明确未支持的事实或执行状态，歧义另列待复核。语义保持：口吻阶段是否改变内容阶段的关键判断、未知状态或提问。内容阶段已有的问题不能归咎于口吻阶段。</p><table><tr><th>组别</th><th>已交付且回应任务/全部预定</th><th>最终答复含无依据内容</th><th>事实含义待复核</th><th>内容到口吻发生关键漂移</th></tr>'
    for arm, title in [('all', '全部资料'), ('relevant', '仅相关资料')]:
        m = summary['arms'][arm]
        values = [title, f"{m['deliveredAndOnTaskDeveloperReview']}/24", m['unsupportedFinalRepliesDeveloperReview'], m['finalSupportUncertainDeveloperReview'], m['semanticDriftDeveloperReview']]
        page += '<tr>' + ''.join('<td>' + E(x) + '</td>' for x in values) + '</tr>'
    page += '</table>'
page += '<h2>全部同题记录</h2>'
for case_id in case_ids:
    rows = [x for x in raw['rows'] if x['caseId'] == case_id]
    page += '<h3>' + E(case_id) + '</h3><p>客户原话（按顺序）：' + E(' → '.join(rows[0]['customerStatements'])) + '</p><p>运行前评审标准：' + E(rows[0]['rubric']) + '</p><table><tr><th>重复</th><th>全部资料</th><th>仅相关资料</th></tr>'
    for repeat in [1, 2]:
        page += '<tr><td>' + str(repeat) + '</td>'
        for arm in ['all', 'relevant']:
            x = next(x for x in rows if x['arm'] == arm and x['repeat'] == repeat)
            value = x.get('reply') if x['status'] == 'generated' else ('未交付：引用资料的编号栏填写不合格' if x.get('failureClass') == 'protocol' else '未交付：' + x.get('error', ''))
            page += '<td>' + E(value)
            if review:
                page += '<p>复核：' + E(ratings[x['id']]['note']) + '</p>'
            page += '</td>'
        page += '</tr>'
    page += '</table>'
    for x in rows:
        page += '<details><summary>' + E(x['id']) + ' 阶段原始输出与实际参数</summary>'
        if x.get('error'):
            page += '<p>原始报错：' + E(x['error']) + '</p>'
        for stage in x['trace']:
            page += '<p>' + E(stage['stage']) + '</p><pre>' + E(json.dumps({
                'options': stage.get('transport', {}).get('body', {}).get('options'),
                'response': stage.get('response'), 'error': stage.get('error')}, ensure_ascii=False, indent=2)) + '</pre>'
        page += '</details>'
page += '<h2>边界与后续</h2><p>相比前一轮，两组同时采用固定资料ID和固定上下文/采样参数，不能把跨轮生成率变化归因于资料筛选。这里的成对差异仅发生于本轮资料列表。两次重复、开发者出题与非盲审阅仍不足以生产放行。原始资料全部为模拟事实，不会写回门店知识库。</p><p>实验源码、冻结哈希、顺序、模型digest与原始记录见同目录。48次预定回复均计入分母，不挑最好的一次。下一步应由本轮证据决定，不追加关键词补丁。</p></html>'
(out / '资料筛选对照报告.html').write_text(page, encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
