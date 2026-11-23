"""Read-only rendering of a completed, frozen style experiment; no inference or grading calls."""
from pathlib import Path
import html
import hashlib
import json
import statistics
import sys

out = Path(sys.argv[1])
r = json.loads((out / 'results.json').read_text())
review = json.loads((out / 'review.json').read_text())
assert review['rawResultsSha256'] == hashlib.sha256((out / 'results.json').read_bytes()).hexdigest()
assert r['status'] == 'completed_not_business_acceptance'
assert len(r['rows']) == len({x['id'] for x in r['rows']}) == 48
rows = [x for x in r['rows'] if x['arm'] == 'rewrite']
ratings = {x['id']: x for x in review['rows']}
assert set(ratings) == {x['id'] for x in rows}
assert review['type'] == 'developer_unblinded_semantic_review_not_owner_acceptance'
summary = {
    'scope': r['scope'], 'contentSource': r['contentSource'],
    'plannedOutputs': 48, 'deterministicReferenceCopies': 24,
    'plannedRewrites': 24, 'recordedModelCalls': sum(x['modelCalls'] for x in rows),
    'candidateReturned': sum(isinstance(x.get('candidateReply'), str) for x in rows),
    'mechanicallyAccepted': sum(x['status'] == 'generated' for x in rows),
    'protocolFailures': sum(x.get('failureClass') == 'protocol' for x in rows),
    'guardOrOtherFailures': sum(x.get('failureClass') == 'guard_or_other' for x in rows),
    'transportFailures': sum(x.get('failureClass') == 'transport_or_timeout' for x in rows),
    'semanticReviewed': sum(x['assessable'] for x in ratings.values()),
    'lostRequiredMeaning': sum(any(v is False for v in x['retained']) for x in ratings.values()),
    'forbiddenMeaningAdded': sum(x['forbiddenAdded'] is True for x in ratings.values()),
    'fullyConservedAndDelivered': 0, 'mechanicallyAcceptedButSemanticFail': 0,
    'accuracy': None, 'ownerLikeness': None, 'ownerRatingsAssigned': 0,
    'independentReviewers': 0, 'realWechatCalls': 0,
    'medianRewriteAttemptSeconds': round(statistics.median(x['elapsedMs'] / 1000 for x in rows), 2),
    'firstModelAttempt': {'id': rows[0]['id'], 'elapsedSeconds': round(rows[0]['elapsedMs'] / 1000, 2)},
    'decision': review['decision'],
}
for row in rows:
    v = ratings[row['id']]
    assert len(v['retained']) == len(row['mustRetain'])
    good = v['assessable'] and all(x is True for x in v['retained']) and v['forbiddenAdded'] is False
    if row['status'] == 'generated':
        summary['fullyConservedAndDelivered'] += good
        summary['mechanicallyAcceptedButSemanticFail'] += not good
(out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
E = lambda x: html.escape(str(x))
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>店主分身：口吻改写保真对照</title><style>
body{font:16px/1.75 system-ui;color:#20392e;max-width:1260px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}td,th{border:1px solid #cbd7d0;padding:12px;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}.notice{background:#f1f5f0;padding:18px}
</style><h1>店主分身：口吻改写保真对照</h1><p>2026年11月3日 · 同一本机千问4B、同一四句本人表达、原有改写指令；主工作台保持v61。</p><div class="notice"><b>固定“说什么”，只测改成店主口吻后会不会变意思。</b><p>12段新内容，各重复两次。原文组直接复制，改写组调用模型：48条输出记录、24次预定模型调用。原文由开发者提前准备，不是模型自动生成；直接复制的保真是构造结果，不能宣布它的准确率100%或自动接待已经可用。</p><p>需要保留的意思和禁止添加的内容在运行前冻结。改写只看原文与口吻样本，不看评分标准。开发者非盲复核，没有店主盲评、独立验收或真实发送，也未给本人相似度打分。</p></div>
'''
page += '<h2>本轮决策</h2><p><b>' + E(review['decision']) + '</b></p>'
for finding in review['findings']:
    page += '<p>' + E(finding) + '</p>'
page += '<h2>执行情况</h2><table><tr><th>组别</th><th>预定输出</th><th>模型调用</th><th>完整输出且通过现有机械检查</th></tr><tr><td>原文直接复制参照</td><td>24</td><td>0</td><td>24（只说明原样复制）</td></tr><tr><td>口吻改写</td><td>24</td><td>' + E(summary['recordedModelCalls']) + '</td><td>' + E(summary['mechanicallyAccepted']) + '</td></tr></table>'
page += '<p>现有机械检查像格式和权限检查，并不能读懂全部意思；“通过检查”不能当作“答对”。改写尝试耗时中位数为' + E(summary['medianRewriteAttemptSeconds']) + '秒，包括成功和失败，首条冷启动另记；不把零调用的复制组与模型推理作速度排名。</p>'
metrics = [
    ('丢失必须保留的意思', summary['lostRequiredMeaning'], '按预先写好的语义要求复核，不要求逐字重复；含被机械拦截的候选。'),
    ('新增明确禁止的内容', summary['forbiddenMeaningAdded'], '例如编库存、本人状态、执行动作或据模拟资料邀请真实到店；可与上一行重叠。'),
    ('完整输出且本轮保真达标', summary['fullyConservedAndDelivered'], '必须保留的信息全部在、没有禁止添加内容，并通过现有机械检查；分母固定24。'),
    ('机械检查放行但语义未达标', summary['mechanicallyAcceptedButSemanticFail'], '现有检查没拦住丢信息或改意思的回复。'),
]
page += '<h2>开发者非盲复核</h2><table><tr><th>指标</th><th>条数</th><th>含义</th></tr>'
for title, count, meaning in metrics:
    page += '<tr><td>' + E(title) + '</td><td>' + E(count) + '/24</td><td>' + E(meaning) + '</td></tr>'
page += '</table><p>这是局部保真指标，不是总体准确率、服务满意度、自然度或店主本人认可。两次重复和开发者出题/复核的局限仍在，不能生产放行。</p><h2>逐题原文与改写</h2>'
for case_id in dict.fromkeys(x['caseId'] for x in r['rows']):
    group = [x for x in rows if x['caseId'] == case_id]
    first = group[0]
    page += '<h3>' + E(case_id) + '</h3><p>客户原话：' + E(' → '.join(first['customerStatements'])) + '</p><p>运行前要求保留：' + E('；'.join(first['mustRetain'])) + '</p><p>禁止新增：' + E('；'.join(first['forbiddenAdds'])) + '</p><table><tr><th>重复</th><th>原文直接使用</th><th>口吻改写候选及复核</th></tr>'
    for row in group:
        v = ratings[row['id']]
        candidate = row.get('candidateReply', '没有可评估的完整候选')
        status = '机械检查放行' if row['status'] == 'generated' else '未交付：' + row.get('error', '')
        page += '<tr><td>' + E(row['repeat']) + '</td><td>' + E(row['referenceContent']) + '</td><td>' + E(candidate) + '<p>' + E(status) + '</p><p>复核：' + E(v['note']) + '</p></td></tr>'
    page += '</table>'
    for row in group:
        page += '<details><summary>' + E(row['id']) + ' 原始输出与实际参数</summary><pre>' + E(json.dumps(row['trace'], ensure_ascii=False, indent=2)) + '</pre></details>'
page += '<h2>后续验证边界</h2><p>本轮只测试当前模型和当前口吻改写步骤，不证明所有改写方法都无效。原文由人工准备，不能据此断言移除改写后自动接待就达标。原始记录、模型摘要、冻结源码与实际参数均留档；无账户读取、微信发送、训练或新版安装。下一改动由证据决定，不用针对单句补关键词的方式抬高这轮成绩。</p></html>'
(out / '口吻改写保真报告.html').write_text(page, encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
