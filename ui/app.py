"""RestockPilot single-user workbench. All business changes go through the API."""
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_environment
import httpx
import streamlit as st

load_environment()
BASE = os.getenv("RESTOCK_API_URL", "http://127.0.0.1:8100").rstrip("/")
STATES = {"NEW":"等待处理","ANALYZING":"正在生成方案","NEEDS_INPUT":"需要补齐资料",
          "NEEDS_REVIEW":"等待复核","APPROVED":"已批准，等待执行","EXECUTING":"正在创建申请",
          "COMPLETED":"采购申请已完成","PARTIAL":"部分申请完成","FAILED":"操作未完成",
          "NEEDS_RECONCILIATION":"需要人工核对"}


def call(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    if path.endswith("/approve"):
        headers["X-Restock-Approval"] = os.getenv("RESTOCK_APPROVAL_TOKEN", "")
    try:
        with httpx.Client(base_url=BASE, timeout=15, trust_env=False) as client:
            response = client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "操作失败")
            except ValueError:
                detail = "服务返回了无法识别的结果"
            st.error(str(detail))
            return None
        if "/artifacts/" in path:
            return response.content
        return response.json()
    except httpx.HTTPError:
        st.error("暂时无法连接服务，请检查启动窗口，然后刷新页面。")
        return None


def money(cents):
    return f"¥{cents / 100:,.2f}"


def show_problem(problem):
    confirmations={
        'confirm quantity_unit=piece across sales, inventory and supplier packaging':'请确认三份数据的数量与包装单位都已统一为件。',
        'confirm available_is_net=true':'请确认可用库存已经扣除预占。',
        'confirm cost_includes_tax_shipping=true':'请确认单价包含税费和运费，金额单位为分。',
        'confirm inbound_eta_is_days=true':'请确认在途到货时间是距规划时点的天数。',
        'invalid 28-day window_end':'销售窗口截止日无效，请使用 YYYY-MM-DD 格式。',
        'sales dates exceed confirmed window_end':'销售记录包含截止日之后的日期，请核对截止日或修正资料。',
        'excluded_skus contains unknown SKU':'排除商品中有资料里不存在的 SKU，请重新选择。',
        'priority_categories contains unknown category':'优先类别中有资料里不存在的类别，请重新选择。'}
    st.error(confirmations.get(problem, '资料需要修正：'+problem))


def policy_fields(prefix, default, categories, skus):
    a,b,c = st.columns(3)
    budget=a.number_input("采购预算（元）",min_value=0.01,value=default["budget_cents"]/100,
                          step=100.0,key=prefix+"budget")
    cover=b.number_input("覆盖天数",min_value=7,max_value=90,value=default["cover_days"],key=prefix+"cover")
    safety=c.number_input("安全天数",min_value=0,max_value=30,value=default["safety_days"],key=prefix+"safety")
    priority=st.multiselect("优先类别",categories,default=[x for x in default["priority_categories"] if x in categories],key=prefix+"priority")
    excluded=st.multiselect("排除商品",skus,default=[x for x in default["excluded_skus"] if x in skus],key=prefix+"excluded")
    return {"budget_cents":int(Decimal(str(budget))*100),"cover_days":int(cover),"safety_days":int(safety),
            "priority_categories":priority,"excluded_skus":excluded}


def open_task(task_id):
    st.query_params["task"] = task_id
    st.rerun()


st.set_page_config(page_title="RestockPilot · 补货工作台",page_icon="📦",layout="wide")
st.markdown("""<style>
.stApp{background:#f5f7f6} h1,h2,h3{color:#163d36}
[data-testid="stMetric"]{background:white;border:1px solid #dce6e2;border-radius:12px;padding:16px}
[data-testid="stSidebar"]{background:#eaf1ee}
[data-testid="stToolbar"]{display:none}
.block-container{padding-top:2.5rem;max-width:1400px}
</style>""",unsafe_allow_html=True)
st.title("RestockPilot")
st.caption("店铺补货与采购申请工作台 · 从资料到经过核对的申请单")
health=call("GET","/health")
if health is None:
    st.stop()
offline=health.get("agent_mode")=="off"
if offline:
    st.info("离线演示模式：规则计算与浏览器录单可用，未启用模型。请用参数表单修改方案。")
st.caption("上传资料  →  生成方案  →  修改与复核  →  人工批准  →  申请与交付")

with st.sidebar:
    st.header("任务记录")
    history=call("GET","/tasks") or {"tasks":[]}
    tasks=history['tasks']
    labels={t['task_id']:f"{t['task_id']} · {STATES.get(t['state'],t['state'])}" for t in tasks}
    options=[""]+list(labels)
    current=st.query_params.get("task", "")
    picked=st.selectbox("选择任务",options,index=options.index(current) if current in options else 0,
                        format_func=lambda x:labels.get(x,"新建任务"))
    if st.button("打开任务",disabled=not picked,use_container_width=True):
        open_task(picked)
    if st.button("新建补货任务",use_container_width=True):
        st.query_params.pop("task",None); st.rerun()
    st.divider()
    st.caption("仅创建采购申请，不表示付款或到货。演示后台不会联系真实供应商。")

with st.expander("1 · 上传与检查资料",expanded=not st.query_params.get("task")):
    if st.button("使用示例数据"):
        result=call("POST","/datasets/example")
        if result:
            st.query_params['dataset']=result['dataset_id']; st.rerun()
    with st.form("upload"):
        columns=st.columns(3)
        sales=columns[0].file_uploader("销售记录 sales.csv",type=['csv'],key="sales")
        inventory=columns[1].file_uploader("库存资料 inventory.csv",type=['csv'],key="inventory")
        suppliers=columns[2].file_uploader("供应商资料 suppliers.csv",type=['csv'],key="suppliers")
        st.caption("请确认数据口径；有疑问时先修正资料，不要猜测单位或价格。")
        units=st.checkbox("三份数据已统一为件")
        net=st.checkbox("可用库存已扣除预占")
        landed=st.checkbox("单价为含税含运的整数分")
        eta=st.checkbox("在途到货时间填写的是距规划时点的天数")
        missing=st.checkbox("确认缺失销售日期代表零销量，允许补零",value=False)
        end=st.text_input("销售窗口截止日（可留空使用最新销售日）",placeholder="YYYY-MM-DD")
        if st.form_submit_button("检查并保存数据",type="primary"):
            if not all([sales,inventory,suppliers]):
                st.error("请上传完整的三份 CSV。")
            else:
                contract={"quantity_unit":"piece" if units else None,"available_is_net":net,
                    "cost_includes_tax_shipping":landed,"inbound_eta_is_days":eta,
                    "missing_sales":"zero" if missing else "reject","window_end":end or None}
                files={name:(name+'.csv',file.getvalue(),'text/csv') for name,file in
                       (("sales",sales),("inventory",inventory),("suppliers",suppliers))}
                result=call("POST","/datasets",files=files,data={"contract":json.dumps(contract)})
                if result and result.get("dataset_id"):
                    st.query_params['dataset']=result['dataset_id']; st.rerun()
                elif result:
                    for issue in result.get("data_problems",[]): show_problem(issue)
    st.caption("修正 CSV 或确认项后可以重新上传。上传不会自动覆盖当前任务；需要在任务中明确应用新数据。")

dataset=None
if st.query_params.get('dataset'):
    dataset=call('GET','/datasets/'+st.query_params['dataset'])
if dataset:
    st.success(f"数据已就绪 · {dataset['sku_count']} 个商品 · {dataset['window_start']} 至 {dataset['window_end']}")

DEFAULT={"budget_cents":500000,"cover_days":30,"safety_days":7,"priority_categories":[],"excluded_skus":[]}
if not st.query_params.get('task'):
    st.subheader("2 · 本次补货要求")
    if not dataset:
        st.info("先上传三份资料，或选择示例数据。")
        st.stop()
    with st.form('create'):
        policy=policy_fields('create-',DEFAULT,dataset['categories'],dataset['skus'])
        user_request=st.text_area("补充要求（可选）",disabled=offline,placeholder="例如：预算改为3000元，优先保障饮料。")
        st.caption("支持预算、覆盖天数、安全天数、优先类别和排除商品。")
        if st.form_submit_button("生成补货方案",type="primary"):
            result=call('POST','/tasks',json={'dataset_id':dataset['dataset_id'],'policy':policy,'user_request':user_request})
            if result: open_task(result['task_id'])
    st.stop()


@st.fragment(run_every=2)
def task_panel():
    task_id=st.query_params['task']
    state=call('GET',f'/tasks/{task_id}')
    if not state: return
    operation=state['operation']; busy=bool(operation['busy'])
    st.subheader(f"任务 {task_id}")
    st.write("**"+STATES.get(state['state'],state['state'])+"**")
    if busy: st.info(operation['phase']+"，页面会自动更新，可稍后通过任务记录返回。")
    if operation.get('error'): st.error(operation['error'])
    for problem in (operation.get('result') or {}).get('data_problems',[]): show_problem(problem)
    plan=state.get('latest_plan')
    editable=state['state'] in ('NEEDS_INPUT','NEEDS_REVIEW','APPROVED') and not busy
    if dataset and editable and (not plan or dataset['dataset_id']!=plan.get('dataset_id')):
        st.warning("选中了不同的数据版本，应用后旧批准立即失效。")
        if st.button("应用新数据并重新生成",key='replace-data'):
            result=call('POST',f'/tasks/{task_id}/revise',json={'dataset_id':dataset['dataset_id']})
            if result: st.rerun()
    if not plan:
        if dataset and editable:
            with st.form('repair-input'):
                fixed=policy_fields('repair-',(operation.get('request') or {}).get('policy') or DEFAULT,
                                    dataset['categories'],dataset['skus'])
                if st.form_submit_button("补齐资料并生成方案"):
                    if call('POST',f'/tasks/{task_id}/revise',json={'dataset_id':dataset['dataset_id'],'policy':fixed}): st.rerun()
        return
    versions=call('GET',f'/tasks/{task_id}/plans') or {'versions':[]}
    nums=[v['version'] for v in versions['versions']]
    selected=st.selectbox("查看方案版本",nums,index=len(nums)-1,key=f'versions-{task_id}-{len(nums)}')
    view=call('GET',f'/tasks/{task_id}/plans/{selected}')
    if not view: return
    shown=view['plan']; is_latest=selected==plan['version']
    if not is_latest: st.info("正在查看历史版本，仅供查阅和导出。")
    cols=st.columns(4)
    cols[0].metric("采购总额",money(shown['total_cost_cents']))
    cols[1].metric("剩余预算",money(shown.get('budget_remaining_cents') or 0))
    cols[2].metric("采购商品数",sum(i['order_qty']>0 for i in shown['items']))
    cols[3].metric("低置信商品",sum(i['confidence']=='low' for i in shown['items']))
    st.caption(f"方案 v{selected} · 预算 {money(shown['policy']['budget_cents'])} · 覆盖 {shown['policy']['cover_days']} 天 · 安全 {shown['policy']['safety_days']} 天")
    st.caption("优先类别："+(', '.join(shown['policy']['priority_categories']) or '无'))
    if shown.get('dataset_snapshot'):
        source=shown['dataset_snapshot']
        st.caption(f"销售窗口：{source['window_start']} 至 {source['window_end']} · 数量单位：件 · 单价含税含运 · 库存已扣预占")
    st.dataframe([{'SKU':i['sku'],'商品':i['name'],'供应商':i['supplier_id'],'采购数量':i['order_qty'],
        '单价（元）':i['unit_cost_cents']/100,'金额（元）':i['line_total_cents']/100,
        '未满足数量':i['unmet_qty'],'置信度':{'low':'低，需复核','medium':'中','high':'高'}.get(i['confidence'],i['confidence']),
        '提示':i['reason']} for i in shown['items']],hide_index=True,use_container_width=True)
    st.caption("主动排除："+(', '.join(shown['excluded_skus']) or '无')+"；预算跳过："+(', '.join(shown['skipped']) or '无'))
    if selected>1:
        st.subheader("与上一版本比较")
        diff=call('GET',f'/tasks/{task_id}/diff',params={'from_version':selected-1,'to_version':selected})
        if diff:
            st.write(f"总额变化：{money(diff['total_delta_cents'])}")
            if diff['dataset_changed']: st.warning("本次修改更换了数据版本。")
            labels={'budget_cents':'预算（分）','cover_days':'覆盖天数','safety_days':'安全天数','priority_categories':'优先类别','excluded_skus':'排除商品'}
            for field,change in diff['policy_changes'].items(): st.write(f"{labels[field]}：{change['before']} → {change['after']}")
            if diff['skipped_before']!=diff['skipped_after']:
                st.write("预算跳过项："+(', '.join(diff['skipped_before']) or '无')+" → "+(', '.join(diff['skipped_after']) or '无'))
            st.dataframe([{'SKU':i['sku'],'商品':i['name'],'原数量':i['before_qty'],'新数量':i['after_qty'],
                '数量变化':i['qty_delta'],'金额变化（元）':i['cost_delta_cents']/100,'说明':i['reason']} for i in diff['items']],hide_index=True,use_container_width=True)
    if editable and is_latest:
        st.subheader("修改方案")
        snapshot=plan.get('dataset_snapshot')
        if snapshot:
            categories=sorted({r['category'] for r in snapshot['inventory']}); skus=[r['sku'] for r in snapshot['inventory']]
            with st.expander("调整参数"):
                with st.form('revise-params-'+str(selected)):
                    updated=policy_fields(f'revise-{task_id}-{selected}-',plan['policy'],categories,skus)
                    if st.form_submit_button("按参数重新计算"):
                        result=call('POST',f'/tasks/{task_id}/revise',json={'policy':updated})
                        if result: st.rerun()
            if not offline:
                with st.form('revise-text-'+str(selected)):
                    comment=st.text_area("用一句话说明修改意见")
                    if st.form_submit_button("提交修改意见"):
                        result=call('POST',f'/tasks/{task_id}/revise',json={'comment':comment})
                        if result: st.rerun()
    if state['state']=='NEEDS_REVIEW' and not busy and is_latest:
        st.subheader("人工确认")
        confirmed=st.checkbox(f"我已核对 v{selected} 的数量、金额和风险提示",key='approve-'+view['plan_hash'])
        if st.button("批准当前方案",type='primary',disabled=not confirmed):
            result=call('POST',f'/tasks/{task_id}/approve',json={'version':selected,'plan_hash':view['plan_hash']})
            if result: st.rerun()
    if state['state']=='APPROVED' and not busy and is_latest:
        st.success("已批准此版本，可以创建演示采购申请。")
        if st.button("创建采购申请",type='primary'):
            if call('POST',f'/tasks/{task_id}/execute'): st.rerun()
    records=call('GET',f'/tasks/{task_id}/executions') or {'records':[]}
    current=[r for r in records['records'] if r['version']==selected]
    if current:
        st.subheader("采购申请与核对结果")
        for record in current:
            receipt=record['receipt']; problems=receipt.get('problems') or []
            st.write(f"申请单 {receipt.get('order_id') or '未获得单号'} · {receipt.get('supplier_id','')}")
            if receipt.get('receipt') and not problems: st.success("申请已创建，回读核对一致。")
            else: st.warning("申请未完成或需要核对："+str(problems or receipt.get('error','未知结果')))
    if state['state'] in ('PARTIAL','NEEDS_RECONCILIATION','FAILED'):
        st.warning("请核对已存在的申请；不要通过新建任务重复采购。异常恢复入口将在后续阶段完善。")
    activity=call('GET',f'/tasks/{task_id}/activity') or {'events':[]}
    with st.expander("阶段记录与分析摘要"):
        for event in activity['events']:
            payload=event['payload']
            if payload.get('summary'):
                label={'planner':'方案复核','reviser':'方案修订','executor':'执行总结','requirements':'需求理解'}.get(event['kind'],'阶段记录')
                st.caption(f"{event['created_at']} UTC · v{payload.get('version','')} · {label}")
                summary=payload['summary']
                if event['kind'] in ('reviser','requirements'):
                    try: summary=json.loads(summary).get('explanation',summary)
                    except (ValueError,AttributeError): pass
                st.text(summary)
            elif event['kind']=='approved': st.write(f"v{payload['version']} 已人工批准 · {event['created_at']} UTC")
    if not busy:
        st.subheader("下载交付物")
        a,b=st.columns(2)
        csv_data=call('GET',f'/tasks/{task_id}/artifacts/{selected}/csv')
        html_data=call('GET',f'/tasks/{task_id}/artifacts/{selected}/html')
        if csv_data is not None: a.download_button("下载补货清单 CSV",csv_data,file_name=f'restock-{task_id}-v{selected}.csv',mime='text/csv')
        if html_data is not None: b.download_button("下载完整报告 HTML",html_data,file_name=f'restock-{task_id}-v{selected}.html',mime='text/html')


task_panel()
