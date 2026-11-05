"""User controls and a read-only automatic task inbox; all writes use the API."""
from datetime import datetime
import streamlit as st

LABELS={'waiting':'等待检查','running':'正在生成/更新方案','observed':'观察草案已生成',
        'needs_review':'有待审方案','blocked':'需要处理','failed':'本次检查失败',
        'no_demand':'当前没有补货缺口','no_purchase':'当前规则或额度下没有可采购数量','interrupted':'重启后待补查'}


def render(call):
    with st.expander('自动补货与周期预算',expanded=False):
        st.caption('采集、保存和补货检查各自按配置频率运行。先观察草案，确认后进入待审模式；系统不会自动批准。')
        sources=(call('GET','/data-sources') or {}).get('sources',[])
        if not sources:
            st.info('先在“数据来源与定期保存”配置来源。');return
        policies=(call('GET','/replenishment-policies') or {}).get('policies',[])
        tasks=(call('GET','/tasks') or {}).get('tasks',[])
        with st.form('replenishment-config-create'):
            sid=st.selectbox('自动检查的数据来源',[s['source_id'] for s in sources],
                format_func=lambda k:next(s['config']['name'] for s in sources if s['source_id']==k),key='rp-source')
            anchor=st.text_input('周期起点（包含时区）',value=datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).isoformat())
            days=st.number_input('每个预算周期（天）',min_value=1,max_value=366,value=1,step=1)
            amount=st.number_input('每周期额度（元）',min_value=.01,value=5000.0,step=100.0)
            a,b=st.columns(2)
            cover=a.number_input('自动覆盖天数',min_value=7,max_value=90,value=30)
            safety=b.number_input('自动安全天数',min_value=0,max_value=30,value=7)
            priority=st.text_input('自动优先类别（逗号分隔）')
            excluded=st.text_input('自动排除商品（SKU，逗号分隔）')
            legacy=st.multiselect('明确属于此店仓的旧任务（其余保留原身份）',[t['task_id'] for t in tasks])
            reviewed=st.checkbox('我已核对旧任务归属，并确认恢复原申请仍占用原补货周期额度')
            st.caption('当前执行目标为独立的本机演示采购后台，不会写入商户网页。周期起点与长度保存后不可重置来重新领额度。')
            if st.form_submit_button('保存并开始观察'):
                if not reviewed:
                    st.warning('请先核对旧任务归属并勾选确认。')
                    return
                def split(text):return [v.strip() for v in text.replace('，',',').split(',') if v.strip()]
                from decimal import Decimal
                result=call('POST','/replenishment-policies',json={'source_id':sid,'anchor':anchor,
                    'period_seconds':int(days)*86400,'policy':{'budget_cents':int(Decimal(str(amount))*100),
                    'cover_days':int(cover),'safety_days':int(safety),'priority_categories':split(priority),'excluded_skus':split(excluded)},
                    'relationship':'separate','mode':'observe','enabled':True,'legacy_task_ids':legacy,
                    'legacy_tasks_reviewed':reviewed,'budget_basis':'approval_cycle'})
                if result:st.rerun()
        if policies:
            selected=st.selectbox('管理自动补货',[p['group_id'] for p in policies],key='rp-manage',
                                   format_func=lambda k:next(p['merchant_id']+' / '+p['warehouse_id'] for p in policies if p['group_id']==k))
            policy=next(p for p in policies if p['group_id']==selected)
            with st.form('rp-manage-'+selected):
                mode=st.selectbox('运行方式',['仅观察草案','生成待审方案'],index=0 if policy['config']['mode']=='observe' else 1)
                enabled=st.checkbox('启用自动补货检查',value=policy['config']['enabled'])
                future=st.number_input('下一预算周期额度（元）',min_value=.01,value=policy['config']['policy']['budget_cents']/100,step=100.0)
                st.caption('已有周期的额度保留；本项金额更新用于尚未建立的新周期。修改方案仍需重新批准。')
                if st.form_submit_button('更新自动补货设置'):
                    from decimal import Decimal
                    c={**policy['config'],'mode':'observe' if mode=='仅观察草案' else 'review','enabled':enabled,
                       'policy':{**policy['config']['policy'],'budget_cents':int(Decimal(str(future))*100)}}
                    if call('PUT','/replenishment-policies/'+selected,json=c):st.rerun()
            with st.expander('纠正尚未使用的本周期策略'):
                st.caption('仅限本周期没有正式方案、执行记录和采购占用。纠正后回到观察模式，并同步以后周期的默认策略；已有方案请从任务中修订。')
                current=policy.get('period')
                if current:
                    defaults=current['policy']
                    with st.form('rp-correct-'+selected+'-'+str(policy['revision'])):
                        amount=st.number_input('纠正周期额度（元）',min_value=.01,value=defaults['budget_cents']/100,step=100.0)
                        a,b=st.columns(2)
                        cover=a.number_input('纠正覆盖天数',min_value=7,max_value=90,value=defaults['cover_days'])
                        safety=b.number_input('纠正安全天数',min_value=0,max_value=30,value=defaults['safety_days'])
                        priority=st.text_input('纠正优先类别（逗号分隔）',value=','.join(defaults['priority_categories']))
                        excluded=st.text_input('纠正排除商品（逗号分隔）',value=','.join(defaults['excluded_skus']))
                        if st.form_submit_button('保存当期纠正并重新观察'):
                            from decimal import Decimal
                            split=lambda text:[v.strip() for v in text.replace('，',',').split(',') if v.strip()]
                            fixed={'budget_cents':int(Decimal(str(amount))*100),'cover_days':int(cover),'safety_days':int(safety),
                                   'priority_categories':split(priority),'excluded_skus':split(excluded)}
                            if call('POST','/replenishment-policies/'+selected+'/correct-current-policy',json={
                                    'expected_revision':policy['revision'],'period_start':current['start'],'policy':fixed}):st.rerun()
            if st.button('立即检查补货',key='rp-check-'+selected):
                if call('POST','/replenishment-policies/'+selected+'/check'):st.rerun()


@st.fragment(run_every=3)
def inbox(call):
    policies=(call('GET','/replenishment-policies') or {}).get('policies',[])
    if not policies:return
    st.subheader('自动补货待办')
    for p in policies:
        state=p['state'];label=LABELS.get(state['status'],state['status'])
        st.write(p['merchant_id']+' / '+p['warehouse_id']+' · '+('已暂停' if not p['config']['enabled'] else label))
        if state.get('error'):st.warning(state['error'])
        st.caption('下次补货检查：'+state['next_check_at'])
        period=p.get('period')
        if period:
            spent=state.get('occupied_cents',0);budget=period['policy']['budget_cents']
            st.caption(f'周期额度 {budget/100:.2f} 元 · 最近核验占用 {spent/100:.2f} 元；最终批准前会重新核验。')
        if state.get('task_id'):
            st.link_button('打开自动补货任务',url='/?task='+state['task_id'])
