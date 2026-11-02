"""Independent acceptance oracle: never imports the production summary renderer."""
import re


def assert_planner_summaries(events, plans):
    summaries = [e['payload'] for e in events if e['kind']=='planner' and e['payload'].get('summary')]
    assert summaries, 'Missing planner summary'
    for event in summaries:
        version = event['version']
        assert version in plans, 'Unknown summary version'
        plan = plans[version]
        assert plan['version'] == version, 'Summary/plan version mismatch'
        text = event['summary']
        assert '人民币' in text and '模型建议：' in text
        for label, cents in [('全局预算',plan['policy']['budget_cents']),
                ('方案金额',plan['total_cost_cents']),('余额',plan['budget_remaining_cents'])]:
            assert f'{label} {cents//100}.{cents%100:02d} 元' in text, label
        risk = {i['sku'] for i in plan['items'] if i.get('stockout_before_arrival')}
        expected = [len(risk),len(risk.intersection(plan['skipped']))]
        match = re.search(r'预计到货前缺货风险 (\d+) 种，其中被预算跳过 (\d+) 种',text)
        assert match and [int(x) for x in match.groups()] == expected, 'Risk subset mismatch'
        low = re.search(r'低置信商品 (\d+) 种',text)
        assert low and int(low[1]) == sum(i.get('confidence')=='low' for i in plan['items'])
        counts = re.search(r'采购 (\d+) 种商品，预算跳过 (\d+) 种',text)
        assert counts and [int(x) for x in counts.groups()] == [sum(i['order_qty']>0 for i in plan['items']),len(plan['skipped'])]
        assert 0 < len(text.split('模型建议：',1)[1].strip()) <= 180
