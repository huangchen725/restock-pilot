"""RestockPilot single-user workbench. All business changes go through the API."""
import json
import uuid
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_environment
import httpx
import streamlit as st
import pandas
import pyarrow
from app.user_messages import friendly_error

load_environment()
BASE = os.getenv("RESTOCK_API_URL", "http://127.0.0.1:8100").rstrip("/")
STATES = {"NEW":"等待处理","ANALYZING":"正在生成方案","NEEDS_INPUT":"需要补齐资料",
          "NEEDS_REVIEW":"等待复核","APPROVED":"已批准，等待执行","EXECUTING":"正在创建申请",
          "COMPLETED":"采购申请已完成","PARTIAL":"部分申请完成","FAILED":"操作未完成",
          "NEEDS_RECONCILIATION":"需要人工核对"}


def call(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    if path.rsplit("/",1)[-1] in ("approve","approve-and-execute","revoke-approval","retry","stop") or (path.startswith(("/data-sources","/replenishment-policies","/demo-site")) and method in ("POST", "PUT")):
        headers["X-Restock-Approval"] = os.getenv("RESTOCK_APPROVAL_TOKEN", "")
    try:
        with httpx.Client(base_url=BASE, timeout=15, trust_env=False) as client:
            response = client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "操作失败")
            except ValueError:
                detail = "服务返回了无法识别的结果"
            show_error(detail)
            return None
        if "/artifacts/" in path or "/publications/" in path or path=="/dataset-templates":
            return response.content
        return response.json()
    except httpx.HTTPError:
        st.error("暂时无法连接服务，请检查启动窗口，然后刷新页面。")
        return None


def money(cents):
    return f"¥{cents / 100:,.2f}"


def show_error(detail):
    message=friendly_error(detail)
    st.error(message)
    if message!=str(detail):
        with st.expander('查看诊断详情'): st.code(str(detail),language=None)


def show_problem(problem):
    confirmations={
        'confirm quantity_unit=piece across sales, inventory and supplier packaging':'请核实数量是否按单个商品填写，例如按瓶而不是按箱。',
        'confirm available_is_net=true':'请核实库存是否已减去留给现有订单的数量。',
        'confirm cost_includes_tax_shipping=true':'请确认单价包含税费和运费，金额单位为分。',
        'confirm inbound_eta_is_days=true':'请核实到货时间是否填写了还需几天，例如 3 天填写 3。',
        'invalid 28-day window_end':'销量统计截止日期无效，请按年-月-日填写，例如 2026-10-28。',
        'sales dates exceed confirmed window_end':'销售记录包含截止日之后的日期，请核对截止日或修正资料。',
        'excluded_skus contains unknown SKU':'排除商品中有资料里不存在的 SKU，请重新选择。',
        'priority_categories contains unknown category':'优先类别中有资料里不存在的类别，请重新选择。'}
    if problem in confirmations:st.error(confirmations[problem])
    else:show_error(problem)


def policy_fields(prefix, default, categories, skus,ready=True):
    a,b,c = st.columns(3)
    budget=a.number_input("采购预算（元）",min_value=0.01,value=default["budget_cents"]/100,
                          step=100.0,key=prefix+"budget",help="这次采购最多可以花多少钱，含税费和运费。")
    cover=b.number_input("覆盖天数",min_value=7,max_value=90,value=default["cover_days"],key=prefix+"cover",help="希望补货后能支撑多少天的销售。")
    safety=c.number_input("安全天数",min_value=0,max_value=30,value=default["safety_days"],key=prefix+"safety",help="在预计需求外，再准备几天的备用库存。")
    priority=st.multiselect("优先类别",categories,default=[x for x in default["priority_categories"] if x in categories],key=prefix+"priority",disabled=not ready,help='先读入资料，再选择优先补货的商品类别。')
    excluded=st.multiselect("排除商品",skus,default=[x for x in default["excluded_skus"] if x in skus],key=prefix+"excluded",disabled=not ready,help='先读入资料，再选择本次不需要采购的商品。')
    return {"budget_cents":int(Decimal(str(budget))*100),"cover_days":int(cover),"safety_days":int(safety),
            "priority_categories":priority,"excluded_skus":excluded}


def open_task(task_id):
    # P1-13：form 内的 st.query_params 设置不生效——改用 session_state 标记，
    # 在脚本顶部（form 外）统一设置 query_params 并跳转。
    st.session_state['_open_task'] = task_id
    st.rerun()


st.set_page_config(page_title="RestockPilot · 补货工作台",page_icon="📦",layout="wide")
# Apply source selection at script entry, before task fragments capture dataset state.
if st.session_state.get('_select_source_dataset'):
    source_id, dataset_id = st.session_state.pop('_select_source_dataset')
    st.query_params['source'] = source_id
    st.query_params['dataset'] = dataset_id
    st.session_state.pop('upload_rejected',None)
    st.rerun()

# P1-13：在脚本顶部（form 外）处理任务跳转，确保 query_params 设置生效。
if st.session_state.get('_open_task'):
    st.query_params['task'] = st.session_state.pop('_open_task')
    st.rerun()
st.markdown("""<style>
.stApp{background:#f5f7f6} h1,h2,h3{color:#163d36}
[data-testid="stMetric"]{background:white;border:1px solid #dce6e2;border-radius:12px;padding:16px}
[data-testid="stSidebar"]{background:#eaf1ee}
[data-testid="stToolbarActions"],.stAppDeployButton{display:none}
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
st.caption("选择取数方式  →  准备资料  →  设置补货要求  →  复核与批准  →  申请与交付")

active_id=st.query_params.get('task')
active_state=call('GET','/tasks/'+active_id) if active_id else None
polling=bool(active_state and active_state['operation']['busy'])

def background_identity(state):
    if not state:return None
    operation=state.get('operation') or {}
    return json.dumps({'state':state['state'],'plan':state.get('latest_plan_hash'),
                       'approval':state.get('approved_plan_hash'),'data':state.get('data_status'),'intent':state.get('execution_intent'),'automation':state.get('automation'),
                       'busy':operation.get('busy'),'error':operation.get('error')},sort_keys=True)


@st.fragment(run_every=2 if active_id and not polling else None)
def watch_background_changes():
    if not active_id or polling:return
    # Poll only this small watcher; do not rebuild idle forms on every timer tick.
    latest=call('GET','/tasks/'+active_id)
    if latest and background_identity(latest)!=background_identity(active_state):st.rerun()


watch_background_changes()

from ui.replenishment_panel import render as replenishment_controls, inbox as replenishment_inbox


@st.fragment
def sidebar_content():
    st.header("任务记录")
    history=call("GET","/tasks") or {"tasks":[]}
    tasks=history['tasks']
    from datetime import datetime,timezone
    def when(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone().strftime('%m-%d %H:%M')
    labels={t['task_id']:f"{when(t['created_at'])} · {t['task_id'][:6]} · "+
            ('正在处理' if t['task_id']==active_id and polling else STATES.get(t['state'],t['state'])) for t in tasks}
    options=[""]+list(labels)
    current=st.query_params.get("task", "")
    picked=st.selectbox("选择任务",options,index=options.index(current) if current in options else 0,
                        format_func=lambda x:labels.get(x,"新建任务"))
    if st.button("打开任务",disabled=not picked,use_container_width=True):
        open_task(picked)
    if st.button("新建补货任务",use_container_width=True):
        st.query_params.pop("task",None); st.rerun()
    if st.button('刷新任务记录',use_container_width=True): st.rerun()
    st.divider()
    st.caption("仅创建采购申请，不表示付款或到货。演示后台不会联系真实供应商。")
    st.caption('关闭浏览器标签不会停止服务；任务会继续处理。')
    system=call('GET','/system/status') or {}
    if system.get('can_stop'):
        stop_ok=st.checkbox('确认停止服务（进行中的处理会中断）',key='confirm-stop')
        if st.button('停止所有服务',disabled=not stop_ok,use_container_width=True):
            result=call('POST','/system/stop')
            if result:
                st.session_state['stopped']=True
                st.success(result['message'])
                st.stop()

with st.sidebar: sidebar_content()
if st.session_state.get('stopped'):
    st.success('已请求停止服务。请等待启动窗口显示关闭完成后，再关闭页面。')
    st.stop()


from ui.data_entry import render as data_entry
if st.query_params.get('task'):
    with st.expander('更换资料或准备新任务',expanded=False):
        entry,dataset=data_entry(call,show_problem,offline)
else:
    entry,dataset=data_entry(call,show_problem,offline)
    if entry=='web':
        st.subheader('2 · 设置补货方式')
        auto=st.radio('取得资料后怎么补货',['定期检查补货','只生成这次方案'],horizontal=True,key='replenishment-choice')
        if auto=='定期检查补货':
            replenishment_controls(call)
            replenishment_inbox(call)
            st.stop()
replenishment_inbox(call)

DEFAULT={"budget_cents":500000,"cover_days":30,"safety_days":7,"priority_categories":[],"excluded_skus":[]}
if not st.query_params.get('task'):
    st.subheader("2 · 本次补货要求")
    if not dataset:
        st.info('可以先准备下面的要求。资料就绪后才能生成；商品类别和商品选择会随资料出现。')
    with st.form('create'):
        policy=policy_fields('create-',DEFAULT,dataset['categories'] if dataset else [],dataset['skus'] if dataset else [],ready=bool(dataset))
        user_request=st.text_area("补充要求（可选）",disabled=offline,placeholder="例如：预算改为3000元，优先保障饮料。")
        st.caption("支持预算、覆盖天数、安全天数、优先类别和排除商品。")
        if st.form_submit_button("生成补货方案",type="primary",disabled=not dataset):
            result=call('POST','/tasks',json={'dataset_id':dataset['dataset_id'],'source_id':st.query_params.get('source'), 'policy':policy,'user_request':user_request})
            if result: open_task(result['task_id'])
    st.stop()


@st.fragment(run_every=2 if polling else None)
def task_panel():
    if st.session_state.get('upload_rejected'):
        st.info('更换的表格未通过检查。请修正上传，或在上方明确选择继续使用原方案。')
        return
    task_id=st.query_params['task']
    state=call('GET',f'/tasks/{task_id}')
    if not state: return
    operation=state['operation']; busy=bool(operation['busy'])
    automation=state.get('automation')
    if automation and automation.get('reason'):st.info('自动补货：'+automation['reason'])
    if polling and not busy:st.rerun()
    summary=call('GET',f'/tasks/{task_id}/summary') or {}
    blocked=any(r['status'] in ('unknown','mismatch') for r in summary.get('pending_items',[]))
    st.subheader(f"任务 {task_id}")
    st.write("**"+STATES.get(state['state'],state['state'])+"**")
    if busy: st.info(operation['phase']+"，页面会自动更新，可稍后通过任务记录返回。")
    if operation.get('error'): show_error(operation['error'])
    data_status = state.get('data_status')
    data_ready = not data_status or data_status['ready']
    if data_status and not data_status['ready']: st.warning('来源数据需复核：' + data_status['reason'])
    for problem in (operation.get('result') or {}).get('data_problems',[]): show_problem(problem)
    plan=state.get('latest_plan')
    # P1-9：PARTIAL/NEEDS_RECONCILIATION 允许人工重新决策修订（revise 端点已支持，
    # NEEDS_RECONCILIATION 修订前会先受控核对，unknown 拒绝）。
    editable=state['state'] in ('NEEDS_INPUT','NEEDS_REVIEW','APPROVED','PARTIAL','NEEDS_RECONCILIATION') and not busy and not blocked
    if summary.get('has_execution'):
        st.caption('此任务已有执行记录，后续修订沿用原数据；已创建申请会从需求和累计预算中扣除。')
    if blocked:
        st.warning('存在结果未知或回读不一致的申请。请先核对，当前不能生成新的采购版本。')
    if dataset and editable and not summary.get('has_execution') and (not plan or dataset['dataset_id']!=plan.get('dataset_id')):
        st.warning("选中了不同的数据版本，应用后旧批准立即失效。")
        if st.button("应用新数据并重新生成",key='replace-data'):
            result=call('POST',f'/tasks/{task_id}/revise',json={'dataset_id':dataset['dataset_id']})
            if result: st.rerun()
    if not plan:
        if state['state']=='FAILED' and not busy:
            original=operation.get('request') or {}
            retry_dataset=dataset or (call('GET','/datasets/'+original['dataset_id']) if original.get('dataset_id') else None)
            st.info('本次没有生成可批准方案，也没有创建采购申请。可修正参数或资料后重新生成。')
            if retry_dataset and not st.session_state.get('upload_rejected'):
                with st.form('retry-generation'):
                    fixed=policy_fields('retry-',original.get('policy') or DEFAULT,retry_dataset['categories'],retry_dataset['skus'])
                    retry_text=st.text_area('补充要求（可选）',value=original.get('user_request',''),disabled=offline)
                    if st.form_submit_button('重新生成方案'):
                        if call('POST',f'/tasks/{task_id}/retry',json={'dataset_id':retry_dataset['dataset_id'],'policy':fixed,
                                'source_id':original.get('source_id'),'user_request':'' if offline else retry_text}): st.rerun()
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
    cycle=shown.get('automation_context')
    if cycle:
        st.write('预算周期与已有采购')
        st.caption(cycle['period_start']+' 至 '+cycle['period_end']+'（不含终点）')
        st.write('已有采购占用 '+money(cycle['coverage']['occupied_cents'])+'；本方案新增 '+money(shown['total_cost_cents'])+'。')
        st.caption('显示生成本版本时的核验结果；批准和执行前重新核验。恢复原申请仍占用原批准周期额度。')
        if cycle['coverage']['orders']:
            with st.expander('已有采购与需求抵扣明细'):
                st.dataframe([{'采购系统':f['identity'][0],'单号':f['identity'][1],'SKU':f['identity'][2],
                               '周期占用（元）':f['charged_cents']/100,'额外抵扣数量':f['extra_covered_qty']}
                              for f in cycle['coverage']['orders']],hide_index=True)
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
        st.caption('批准并执行只针对当前版本；系统随后自动录入、回读并生成报告。排队超过所选有效期将重新待审，已开始的采购按实际结果核对。')
        approval_minutes=st.number_input('开始执行的批准有效期（分钟）',min_value=1,max_value=1440,value=30,step=1)
        request_key=f'approval-request-{task_id}-{view["plan_hash"]}-{approval_minutes}'
        if request_key not in st.session_state:st.session_state[request_key]=uuid.uuid4().hex
        if st.button('批准并执行',type='primary',disabled=not confirmed or not data_ready or bool(automation and not automation['ready'])):
            result=call('POST',f'/tasks/{task_id}/approve-and-execute',json={'version':selected,'plan_hash':view['plan_hash'],
                        'request_id':st.session_state[request_key],'expires_in_seconds':int(approval_minutes)*60})
            if result:st.session_state.pop(request_key,None);st.rerun()
        if st.button("批准当前方案",disabled=not confirmed or not data_ready or bool(automation and not automation['ready'])):
            result=call('POST',f'/tasks/{task_id}/approve',json={'version':selected,'plan_hash':view['plan_hash']})
            if result: st.rerun()
    intent=state.get('execution_intent')
    if intent:
        st.caption('连续执行：'+{'queued':'已排队','running':'正在执行','succeeded':'已完成',
                   'needs_reconciliation':'需要核对','cancelled':'已撤销或失效','expired':'批准已过期'}.get(intent['status'],intent['status']))
        if intent.get('error'):st.warning(intent['error'])
    if state['state']=='APPROVED' and not busy and is_latest:
        st.success("已批准此版本，可以创建演示采购申请。")
        if st.button('撤销尚未执行的批准'):
            if call('POST',f'/tasks/{task_id}/revoke-approval'):st.rerun()
        if st.button("创建采购申请",type='primary',disabled=not data_ready or bool(automation and not automation['ready'])):
            if call('POST',f'/tasks/{task_id}/execute'): st.rerun()
    current=[r for r in summary.get('valid_orders',[]) if r['version']==selected]
    if current:
        st.subheader("采购申请与核对结果")
        for order in current:
            st.write(f"申请单 {order['order_id']} · {order['supplier_id']}")
            st.success("申请已创建，回读核对一致。")
    if summary.get('valid_orders') or summary.get('pending_items'):
        with st.expander("当前任务整体申请总览（跨版本）",expanded=bool(summary.get('pending_items'))):
            if summary.get('valid_orders'):
                st.write("有效申请（所有版本合计）：")
                for o in summary['valid_orders']:
                    st.write(f"  · {o.get('order_id') or '未获得单号'} · {o.get('supplier_id','')} · v{o.get('version')} · {o.get('total_cost_cents',0)} 分")
            if summary.get('pending_items'):
                st.warning("未完成事项：")
                outcome_labels={'unknown':'结果未知','mismatch':'回读不一致','not_submitted':'确定未提交'}
                for p in summary['pending_items']:
                    st.write(f"  · {p.get('external_ref')} · {p.get('supplier_id','')} · {outcome_labels.get(p.get('status'),p.get('status'))} · {p.get('problems') or p.get('error','结果未知')}")
    if summary.get('has_execution') and not busy:
        audit=call('GET',f'/tasks/{task_id}/audit-trail') or {}
        if audit and not audit.get('replay_complete'):
            st.warning('执行记录尚不完整：'+'；'.join(audit.get('issues',[])))
    check_result=(operation.get('result') or {}).get('reconciliation')
    if check_result and not busy:
        st.subheader("最新核对结果")
        labels={'created':'申请存在且一致','not_submitted':'确定未提交','unknown':'结果未知','mismatch':'回读不一致'}
        for row in check_result:
            st.write(f"{row['supplier_id']}：{labels.get(row['state'],row['state'])}")
            if row.get('problems'): st.warning('；'.join(row['problems']))
    if state['state'] in ('PARTIAL','NEEDS_RECONCILIATION','FAILED') and state.get('approved_version') is not None:
        st.warning("请核对已存在的申请；不要通过新建任务重复采购。")
        if st.button("核对申请状态（受控核对）", key=f'reconcile-{task_id}-{selected}',disabled=busy):
            if call('POST',f'/tasks/{task_id}/reconcile'): st.rerun()
        confirmed_recovery=st.checkbox("确认仅恢复已确定未提交的申请",key=f'recover-confirm-{task_id}',disabled=busy)
        st.caption('恢复会先按原参考号核对；结果未知的申请不会再次提交，已存在的申请保留。')
        if st.button("恢复未完成的申请（受控恢复）",key=f'recover-{task_id}-{selected}',disabled=busy or not confirmed_recovery):
            if call('POST',f'/tasks/{task_id}/recover'): st.rerun()
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
                if payload.get("model_original"):
                    with st.expander("模型原始说明（仅供诊断）"):
                        st.text(payload["model_original"])
            elif event['kind']=='approved': st.write(f"v{payload['version']} 已人工批准 · {event['created_at']} UTC")
    if not busy or state['state'] in ('COMPLETED','PARTIAL','FAILED','NEEDS_RECONCILIATION'):
        st.subheader("下载交付物")
        a,b=st.columns(2)
        csv_data=call('GET',f'/tasks/{task_id}/artifacts/{selected}/csv')
        html_data=call('GET',f'/tasks/{task_id}/artifacts/{selected}/html')
        if csv_data is not None: a.download_button("下载补货清单 CSV",csv_data,file_name=f'restock-{task_id}-v{selected}.csv',mime='text/csv')
        if html_data is not None: b.download_button("下载完整报告 HTML",html_data,file_name=f'restock-{task_id}-v{selected}.html',mime='text/html')


task_panel()
