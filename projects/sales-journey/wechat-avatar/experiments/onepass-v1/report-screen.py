"""Render the posthoc full-matrix text-model screen; no inference or scoring calls."""
from pathlib import Path
import json, hashlib, html, statistics, sys
out = Path(sys.argv[1])
base_out = Path(sys.argv[2])
r = json.loads((out / 'results.json').read_text())
base = json.loads((base_out / 'results.json').read_text())
review = json.loads((out / 'review.json').read_text())
base_review = json.loads((base_out / 'review.json').read_text())
assert r['status'] == 'completed_not_business_acceptance'
assert len(r['rows']) == len({x['id'] for x in r['rows']}) == 24
assert r['baseResultSha256'] == hashlib.sha256((base_out / 'results.json').read_bytes()).hexdigest()
assert review['rawResultsSha256'] == hashlib.sha256((out / 'results.json').read_bytes()).hexdigest()
ratings = {x['id']: x for x in review['rows']}
base_ratings = {x['id']: x for x in base_review['rows']}
assert set(ratings) == {x['id'] for x in r['rows']}

def good(row, indexed):
    v = indexed[row['id']]
    return row['status'] == 'generated' and v['assessable'] and all(x is True for x in v['satisfied']) and v['forbiddenAdded'] is False

def metrics(rows, indexed):
    for row in rows:
        assert len(indexed[row['id']]['satisfied']) == len(row['mustAddress'])
    return {'planned': 24, 'generated': sum(x['status'] == 'generated' for x in rows),
            'protocolFailures': sum(x.get('failureClass') == 'protocol' for x in rows),
            'guardOrOtherFailures': sum(x.get('failureClass') == 'guard_or_other' for x in rows),
            'transportFailures': sum(x.get('failureClass') == 'transport_or_timeout' for x in rows),
            'qualifiedDeveloperReview': sum(good(x, indexed) for x in rows),
            'deliveredForbiddenAdded': sum(indexed[x['id']]['forbiddenAdded'] is True for x in rows if x['status'] == 'generated'),
            'medianAttemptSeconds': round(statistics.median(x['elapsedMs'] / 1000 for x in rows), 2),
            'modelCalls': sum(x['modelCalls'] for x in rows)}
before = [x for x in base['rows'] if x['arm'] == 'onepass']
summary = {'scope': r['scope'], 'posthocScreen': True, 'freshHeldout': False,
           'sameOldCasesAndSeeds': True, 'modelAndCompatibilityConfigDiffer': True,
           'arms': {'onepass4b': metrics(before, base_ratings), 'onepass8b': metrics(r['rows'], ratings)},
           'accuracy': None, 'ownerLikeness': None, 'ownerRatingsAssigned': 0,
           'independentReviewers': 0, 'realWechatCalls': 0, 'baseResultsUnchanged': True,
           'decision': review['decision'], 'first8BAttemptSeconds': round(r['rows'][0]['elapsedMs'] / 1000, 2),
           'pairedBothDelivered': {'total': 0, 'bothQualified': 0, 'only4BQualified': 0, 'only8BQualified': 0, 'neitherQualified': 0}}
for row in r['rows']:
    old = next(x for x in before if x['caseId'] == row['caseId'] and x['repeat'] == row['repeat'])
    assert old['materialHash'] == row['materialHash'] and old['seed'] == row['seed']
    if old['status'] == row['status'] == 'generated':
        summary['pairedBothDelivered']['total'] += 1
        flags = [good(old, base_ratings), good(row, ratings)]
        key = 'bothQualified' if all(flags) else 'only4BQualified' if flags[0] else 'only8BQualified' if flags[1] else 'neitherQualified'
        summary['pairedBothDelivered'][key] += 1
(out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
E = lambda x: html.escape(str(x))
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>店主分身：追加8B文本模型筛查</title><style>body{font:16px/1.75 system-ui;color:#20392e;max-width:1260px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}td,th{border:1px solid #cbd7d0;padding:12px;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}.notice{background:#f1f5f0;padding:18px}</style><h1>店主分身：追加8B文本模型筛查</h1><p>2026年11月3日 · 一次生成流程和内容完全保持，只换本机模型配置；工作台仍为v61。</p><div class="notice"><b>看到4B失败后追加，不能当成预先设计的第三组或新留出题。</b><p>完整沿用一次生成的12题、两次重复、资料、口吻、标准及随机参数，共24次追加调用；没有重跑或修改4B。模型由4B视觉指令模型换为已安装的8B文本模型，文本模型兼容配置关闭内部思考模式。结构/配置不同，不能把差异只归因于参数量。</p><p>相关资料由人工提前选择，仅有模拟地址和时间。开发者非盲复核，无独立验收、店主盲评、真实微信发送或生产替换。</p></div>'''
page += '<h2>本轮决策</h2><p><b>' + E(review['decision']) + '</b></p>'
for finding in review['findings']:
    page += '<p>' + E(finding) + '</p>'
page += '<h2>交付与局部要求分开看</h2><table><tr><th>一次生成配置</th><th>生成/预定</th><th>格式失败</th><th>拦截/其他</th><th>局部要求达标</th><th>已交付的禁止新增</th></tr>'
for key, title in [('onepass4b', '4B，原结果未修改'), ('onepass8b', '8B，追加筛查')]:
    m = summary['arms'][key]
    values = [title, f"{m['generated']}/24", m['protocolFailures'], m['guardOrOtherFailures'], f"{m['qualifiedDeveloperReview']}/24", m['deliveredForbiddenAdded']]
    page += '<tr>' + ''.join('<td>' + E(x) + '</td>' for x in values) + '</tr>'
page += '</table><p>局部要求达标沿用原题的全部必须项、禁止项及完整交付要求，不是总体准确率、专业程度、自然度或本人评分。部分回答直接回应了客户所问，却未完整复述额外细节，逐条说明，不把这类缺项等同于编造订单。</p><p>8B首条加载和生成共' + E(summary['first8BAttemptSeconds']) + '秒，尝试耗时中位数' + E(summary['arms']['onepass8b']['medianAttemptSeconds']) + '秒。两组加载时机、缓存和模型配置不同，不作为固有速度排名。</p><h2>全部同题对照</h2>'
for case_id in dict.fromkeys(x['caseId'] for x in r['rows']):
    rows = [x for x in r['rows'] if x['caseId'] == case_id]
    first = rows[0]
    page += '<h3>' + E(case_id) + '</h3><p>客户原话：' + E(' → '.join(first['customerStatements'])) + '</p><p>冻结要求：' + E('；'.join(first['mustAddress'])) + '</p><table><tr><th>重复</th><th>原4B一次生成</th><th>追加8B一次生成</th></tr>'
    for row in rows:
        old = next(x for x in before if x['caseId'] == case_id and x['repeat'] == row['repeat'])
        page += '<tr><td>' + E(row['repeat']) + '</td>'
        for candidate, indexed in [(old, base_ratings), (row, ratings)]:
            text = candidate.get('reply') if candidate['status'] == 'generated' else '未交付：' + candidate.get('error', '')
            page += '<td>' + E(text)
            if candidate.get('candidateReply') and candidate['status'] != 'generated':
                page += '<p>被拦候选：' + E(candidate['candidateReply']) + '</p>'
            page += '<p>复核：' + E(indexed[candidate['id']]['note']) + '</p></td>'
        page += '</tr>'
    page += '</table>'
    for row in rows:
        page += '<details><summary>' + E(row['id']) + ' 原始输出与实际参数</summary><pre>' + E(json.dumps(row['trace'], ensure_ascii=False, indent=2)) + '</pre></details>'
page += '<h2>接下来的开发边界</h2><p>需要把业务状态的依据落实到程序与资料：订单进度依赖订单状态记录，库存依赖库存资料，本人位置依赖明确授权的实时信息。缺失则保持未知，不能由语言模型为了像店主而补出结果。此结构化约束尚未实现，不把计划写成已完成。当前模型和流程都未放行。</p></html>'
(out / '追加8B模型筛查报告.html').write_text(page, encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
