"""Pure version comparisons and escaped, downloadable business reports."""
import csv
import io
import json
from html import escape


def plan_diff(before, after):
    a, b = ({i.sku:i for i in p.items} for p in (before, after))
    rows = []
    for sku in sorted(set(a)|set(b)):
        old, new = a.get(sku), b.get(sku)
        qty_a, qty_b = (old.order_qty if old else 0), (new.order_qty if new else 0)
        cost_a, cost_b = (old.line_total_cents if old else 0), (new.line_total_cents if new else 0)
        if old != new:
            rows.append({"sku":sku,"name":(new or old).name,"before_qty":qty_a,"after_qty":qty_b,
                         "qty_delta":qty_b-qty_a,"before_cost_cents":cost_a,"after_cost_cents":cost_b,
                         "cost_delta_cents":cost_b-cost_a,"reason":new.reason if new else "已排除"})
    pa, pb = before.policy.model_dump(), after.policy.model_dump()
    return {"from_version":before.version,"to_version":after.version,
            "dataset_changed":before.dataset_hash!=after.dataset_hash,
            "policy_changes":{k:{"before":pa[k],"after":pb[k]} for k in pa if pa[k]!=pb[k]},
            "total_delta_cents":after.total_cost_cents-before.total_cost_cents,
            "skipped_before":before.skipped,"skipped_after":after.skipped,"items":rows}


def csv_cell(value):
    text = str(value)
    return "'"+text if text.lstrip().startswith(("=","+","-","@","\t","\r")) or text.startswith(("\t","\r","\n")) else text


def export_csv(plan):
    output=io.StringIO(newline="")
    writer=csv.writer(output)
    writer.writerow(["方案编号","版本","数据版本","SKU","商品","供应商","采购数量","单价（分）","金额（分）","未满足数量","复核提示"])
    for item in plan.items:
        writer.writerow([csv_cell(v) for v in (plan.plan_id,plan.version,plan.dataset_hash,item.sku,item.name,
            item.supplier_id,item.order_qty,item.unit_cost_cents,item.line_total_cents,item.unmet_qty,item.reason)])
    return output.getvalue().encode("utf-8-sig")


def export_html(plan, task_state, events, records, plan_hash):
    e=lambda x:escape(str(x), quote=True)
    rows="".join("<tr>"+"".join(f"<td>{e(v)}</td>" for v in (i.sku,i.name,i.supplier_id,i.order_qty,i.unit_cost_cents,
        i.line_total_cents,i.unmet_qty,i.reason))+"</tr>" for i in plan.items)
    receipts=[r for r in records if r['version']==plan.version]
    relevant=[event for event in events if event['payload'].get('version')==plan.version or event['kind']=='created']
    def blocks(values):
        return "".join("<pre>"+e(json.dumps(v,ensure_ascii=False,indent=2))+"</pre>" for v in values) or "<p>暂无记录</p>"
    contract=plan.dataset_snapshot.contract.model_dump() if plan.dataset_snapshot else {"说明":"旧方案无数据快照"}
    state_labels={'NEW':'等待处理','ANALYZING':'生成方案中','NEEDS_REVIEW':'等待复核','APPROVED':'已批准',
                  'EXECUTING':'正在创建申请','COMPLETED':'采购申请已完成','PARTIAL':'部分完成',
                  'NEEDS_INPUT':'待补齐资料','NEEDS_RECONCILIATION':'待人工核对','FAILED':'操作未完成'}
    summary=[]
    for record in receipts:
        receipt=record['receipt']
        ok=receipt.get('receipt') is not None and not receipt.get('problems')
        summary.append(f"<li>供应商 {e(receipt.get('supplier_id',''))} · 申请单 {e(receipt.get('order_id') or '未获得单号')} · "
                       f"{'回读核对一致' if ok else '未完成或需要人工核对'}"
                       f"{e(receipt.get('problems') or receipt.get('error') or '')}</li>")
    approvals=[ev for ev in relevant if ev['kind']=='approved']
    approval_text='；'.join(e(ev['created_at'])+' UTC 人工批准 v'+str(plan.version) for ev in approvals) or '本版本暂无批准记录'
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>RestockPilot 补货报告</title>
<style>body{{font:16px/1.65 sans-serif;max-width:1100px;margin:40px auto;color:#183332;padding:20px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd8d6;padding:8px;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f6f5;padding:14px}}</style>
<h1>RestockPilot 补货与采购申请报告</h1><p>方案 {e(plan.plan_id)} · 版本 {plan.version} · 任务当前状态 {e(state_labels.get(task_state,task_state))}</p>
<p>数据版本：{e(plan.dataset_id or plan.dataset_hash)}<br>规则版本：{e(plan.rules_version)}<br>数据指纹：{e(plan.dataset_hash)}<br>方案指纹：{e(plan_hash)}</p><p>金额单位为分。采购申请已创建不表示付款或到货。执行记录只展示本版本；任务当前状态不表示历史版本也已执行。</p>
<h2>方案与计算口径</h2><p>合计 {plan.total_cost_cents} 分，剩余预算 {e(plan.budget_remaining_cents)} 分。</p>
{blocks([plan.policy.model_dump(),contract])}<p>目标库存=ceil(日均销量×交货及覆盖安全天数)。缺口扣除可用库存和窗口内合格在途；全局预算按优先级分配，采购满足整包和起订量。</p>
<table><tr><th>SKU</th><th>商品</th><th>供应商</th><th>采购数量</th><th>单价</th><th>金额</th><th>未满足</th><th>提示</th></tr>{rows}</table>
<p>主动排除：{e(', '.join(plan.excluded_skus) or '无')}；预算跳过：{e(', '.join(plan.skipped) or '无')}</p>
<h2>批准与阶段记录</h2><p>{approval_text}。历史批准记录不代表当前仍有执行权限。</p>
<details><summary>查看需求、修改与阶段记录</summary>{blocks(relevant)}</details>
<h2>采购申请与回读核对</h2>{'<ul>'+''.join(summary)+'</ul>' if summary else '<p>本版本暂无申请回执，不能据此认定执行成功。</p>'}
<details><summary>查看申请明细与核对证据</summary>{blocks(receipts)}</details></html>'''
