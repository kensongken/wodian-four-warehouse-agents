"""Render the complete workflow pilot without any inference or automatic quality scoring."""
from pathlib import Path
import json, html, hashlib, statistics, sys
out = Path(sys.argv[1])
r = json.loads((out / 'results.json').read_text())
review = json.loads((out / 'review.json').read_text())
assert r['status'] == 'completed_not_business_acceptance'
assert len(r['rows']) == len({x['id'] for x in r['rows']}) == 48
assert review['rawResultsSha256'] == hashlib.sha256((out / 'results.json').read_bytes()).hexdigest()
assert review['type'] == 'developer_unblinded_workflow_review_not_owner_acceptance'
ratings = {x['id']: x for x in review['rows']}
assert set(ratings) == {x['id'] for x in r['rows']}

def qualified(row):
    v = ratings[row['id']]
    return row['status'] == 'generated' and v['assessable'] and all(x is True for x in v['satisfied']) and v['forbiddenAdded'] is False

summary = {'scope': r['scope'], 'plannedReplies': 48, 'arms': {},
           'accuracy': None, 'ownerLikeness': None, 'ownerRatingsAssigned': 0,
           'independentReviewers': 0, 'realWechatCalls': 0,
           'manualRelevantFactSelection': True, 'decision': review['decision'],
           'pairedCompletion': {'both': 0, 'onepassOnly': 0, 'layeredOnly': 0, 'neither': 0},
           'amongBothDelivered': {'bothQualified': 0, 'onepassOnlyQualified': 0, 'layeredOnlyQualified': 0, 'neitherQualified': 0}}
for arm in ['onepass', 'layered']:
    rows = [x for x in r['rows'] if x['arm'] == arm]
    delivered = [x for x in rows if x['status'] == 'generated']
    summary['arms'][arm] = {
        'planned': 24, 'generated': len(delivered),
        'protocolFailures': sum(x.get('failureClass') == 'protocol' for x in rows),
        'guardOrOtherFailures': sum(x.get('failureClass') == 'guard_or_other' for x in rows),
        'transportFailures': sum(x.get('failureClass') == 'transport_or_timeout' for x in rows),
        'modelCalls': sum(x['modelCalls'] for x in rows),
        'medianAttemptSeconds': round(statistics.median(x['elapsedMs'] / 1000 for x in rows), 2),
        'qualifiedDeveloperReview': sum(qualified(x) for x in rows),
        'deliveredMissingRequired': sum(any(v is False for v in ratings[x['id']]['satisfied']) for x in delivered),
        'deliveredForbiddenAdded': sum(ratings[x['id']]['forbiddenAdded'] is True for x in delivered),
        'candidateForbiddenIncludingBlocked': sum(ratings[x['id']]['forbiddenAdded'] is True for x in rows),
    }
case_ids = list(dict.fromkeys(x['caseId'] for x in r['rows']))
for case_id in case_ids:
    for repeat in [1, 2]:
        pair = {x['arm']: x for x in r['rows'] if x['caseId'] == case_id and x['repeat'] == repeat}
        a, b = pair['onepass'], pair['layered']
        assert a['materialHash'] == b['materialHash']
        flags = [a['status'] == 'generated', b['status'] == 'generated']
        label = 'both' if all(flags) else 'onepassOnly' if flags[0] else 'layeredOnly' if flags[1] else 'neither'
        summary['pairedCompletion'][label] += 1
        if all(flags):
            good = [qualified(a), qualified(b)]
            label = 'bothQualified' if all(good) else 'onepassOnlyQualified' if good[0] else 'layeredOnlyQualified' if good[1] else 'neitherQualified'
            summary['amongBothDelivered'][label] += 1
(out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
E = lambda x: html.escape(str(x))
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>店主分身：一次生成与两步生成对照</title><style>
body{font:16px/1.75 system-ui;color:#20392e;max-width:1260px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:14px}td,th{border:1px solid #cbd7d0;padding:12px;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}.notice{background:#f1f5f0;padding:18px}
</style><h1>店主分身：一次生成与两步生成对照</h1><p>2026年11月3日 · 同一本机千问4B、同一资料与本人表达；主工作台仍为v61。</p><div class="notice"><b>比较整套生成方式，不拿“能输出”代替“会接待”。</b><p>12个新场景，每组各重复两次，共48次预定回复。一次生成同时处理接待内容与口吻；两步生成先写内容计划，再改店主口吻。两组输出格式与指令组织不同，因此不是只验证少一次调用的单一因素。</p><p>相关资料由人工提前选择，仅有两条模拟门店资料，未验证自动检索器或真实资料。多轮题给相同客户历史原话，不依赖本次之前的生成结果。原问题和评分要求均在运行前冻结，无择优重跑。</p><p>逐条语义判断由开发者非盲完成，不是独立验收、店主本人认可或总体准确率；没有真实微信发送。</p></div>'''
page += '<h2>本轮决策</h2><p><b>' + E(review['decision']) + '</b></p>'
for finding in review['findings']:
    page += '<p>' + E(finding) + '</p>'
page += '<h2>交付与失败分开看</h2><p>格式/引用栏错误像填表时填错编号：程序不会把它当成已回复，但不能因此断言文字内容完全不懂。安全拦截另外列出。</p><table><tr><th>方式</th><th>生成/预定</th><th>格式或引用栏填错</th><th>拦截/其他</th><th>连接/超时</th><th>耗时中位数</th></tr>'
for arm, title in [('onepass', '一次生成'), ('layered', '两步生成')]:
    m = summary['arms'][arm]
    values = [title, f"{m['generated']}/24", m['protocolFailures'], m['guardOrOtherFailures'], m['transportFailures'], str(m['medianAttemptSeconds']) + '秒']
    page += '<tr>' + ''.join('<td>' + E(x) + '</td>' for x in values) + '</tr>'
page += '</table><p>耗时包含成功与失败尝试。格式错误可能提前结束，不能把早失败说成效率提升；首条冷启动和宿主机负载也会影响耗时，不作通用性能排名。</p>'
page += '<h2>开发者非盲业务复核</h2><p>本轮局部要求达标=完整交付、运行前写明的必须项全部满足、没有禁止新增内容。原始候选被拦截时仍保留复核，但不计为已交付达标。以下不是总体准确率或本人相似度。</p><table><tr><th>方式</th><th>本轮局部要求达标/预定</th><th>已交付但缺必须信息</th><th>已交付且新增禁止内容</th><th>含被拦截候选的禁止新增</th></tr>'
for arm, title in [('onepass', '一次生成'), ('layered', '两步生成')]:
    m = summary['arms'][arm]
    values = [title, f"{m['qualifiedDeveloperReview']}/24", m['deliveredMissingRequired'], m['deliveredForbiddenAdded'], m['candidateForbiddenIncludingBlocked']]
    page += '<tr>' + ''.join('<td>' + E(x) + '</td>' for x in values) + '</tr>'
page += '</table><p>两组都交付的同题对数及其中语义结果见summary.json；不得只比较一次生成能答而两步生成格式失败的题来宣称理解能力胜出。</p><h2>全部同题记录</h2>'
for case_id in case_ids:
    rows = [x for x in r['rows'] if x['caseId'] == case_id]
    first = rows[0]
    page += '<h3>' + E(case_id) + '</h3><p>客户原话：' + E(' → '.join(first['customerStatements'])) + '</p><p>必须回应：' + E('；'.join(first['mustAddress'])) + '</p><p>禁止新增：' + E('；'.join(first['forbiddenAdds'])) + '</p><table><tr><th>重复</th><th>一次生成</th><th>两步生成</th></tr>'
    for repeat in [1, 2]:
        page += '<tr><td>' + str(repeat) + '</td>'
        for arm in ['onepass', 'layered']:
            row = next(x for x in rows if x['arm'] == arm and x['repeat'] == repeat)
            v = ratings[row['id']]
            text = row.get('reply') if row['status'] == 'generated' else '未交付：' + ('格式或引用栏填写不合格' if row.get('failureClass') == 'protocol' else row.get('error', ''))
            page += '<td>' + E(text)
            if row.get('candidateReply') and row['status'] != 'generated':
                page += '<p>被拦候选：' + E(row['candidateReply']) + '</p>'
            page += '<p>复核：' + E(v['note']) + '</p></td>'
        page += '</tr>'
    page += '</table>'
    for row in rows:
        page += '<details><summary>' + E(row['id']) + ' 原始阶段与实际参数</summary><pre>' + E(json.dumps(row['trace'], ensure_ascii=False, indent=2)) + '</pre></details>'
page += '<h2>边界</h2><p>只验证本轮模型与两套候选流程；不是所有一次生成或分层方法的结论。没有真实政策、商品库存或订单工具，不能用模型推测补成事实。完整旧题回归、独立新题、连续接待和店主盲评均未在本报告中伪造完成；未安装新版本。</p></html>'
(out / '一次生成对照报告.html').write_text(page, encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
