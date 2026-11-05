"""User controls and a read-only automatic task inbox; all writes use the API."""
from datetime import datetime
import streamlit as st

LABELS={'waiting':'等待检查','running':'正在生成/更新方案','observed':'观察草案已生成',
        'needs_review':'有待审方案','blocked':'需要处理','failed':'本次检查失败',
        'no_demand':'当前没有补货缺口','no_purchase':'当前规则或额度下没有可采购数量','interrupted':'重启后待补查'}


def render(call):
    with st.expander('定期补货设置',expanded=True):
        st.caption('网页资料保存后，按设定时间检查是否缺货。先观察建议，确认设置合适后再生成待审方案；每次采购仍需你批准。')
        sources=(call('GET','/data-sources') or {}).get('sources',[])
        if not sources:
            st.info('先完成上方的网页连接，等资料保存好后，点击“用这份资料继续”。');return
        policies=(call('GET','/replenishment-policies') or {}).get('policies',[])
        tasks=(call('GET','/tasks') or {}).get('tasks',[])
        chosen=st.query_params.get('source')
        ready=[s for s in sources if s['data_status']['ready']]
        if not ready:
            st.info('正在等待完整资料。读取并保存成功后，点击上方“用这份资料继续”，再设置定期补货。')
        with st.form('replenishment-config-create'):
            sid=st.selectbox('自动检查的数据来源',[s['source_id'] for s in sources],
                index=next((i for i,s in enumerate(sources) if s['source_id']==chosen),0),
                format_func=lambda k:next(s['config']['name'] for s in sources if s['source_id']==k),key='rp-source')
            with st.expander('预算从哪天开始计算'):
                anchor=st.text_input('周期起点（包含时区）',value=datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),help='默认从今天零点开始。例如每 7 天一个预算周期，就按这个起点每 7 天重新计算一次额度。保存后不能随意改起点重复领取额度。')
            days=st.number_input('每个预算周期（天）',min_value=1,max_value=366,value=1,step=1,help='例如填写 7，表示每 7 天共用一笔采购预算。')
            amount=st.number_input('每周期额度（元）',min_value=.01,value=5000.0,step=100.0,help='这段时间里所有采购申请合计不能超过此金额。')
            a,b=st.columns(2)
            cover=a.number_input('自动覆盖天数',min_value=7,max_value=90,value=30,help='希望补货后能支撑多少天的销售。')
            safety=b.number_input('自动安全天数',min_value=0,max_value=30,value=7,help='在预计需求外，多留几天的备用库存。')
            priority=st.text_input('自动优先类别（逗号分隔）')
            excluded=st.text_input('自动排除商品（SKU，逗号分隔）')
            legacy=[];reviewed=True
            if tasks:
                with st.expander('以前创建的任务是否属于这家店'):
                    st.caption('只勾选确实属于这家店和这个仓库的任务，避免同一笔采购重复计费。拿不准的任务不要选。')
                    legacy=st.multiselect('需要关联的旧任务',[t['task_id'] for t in tasks])
                    reviewed=st.checkbox('我已核对旧任务；重新尝试申请仍计入原来的预算周期')
            st.caption('当前执行目标为独立的本机演示采购后台，不会写入商户网页。周期起点与长度保存后不可重置来重新领额度。')
            confirmed=st.checkbox('我确认按以上周期和预算检查补货；采购前仍由我批准')
            if st.form_submit_button('保存并开始观察',disabled=not any(s['source_id']==sid and s['data_status']['ready'] for s in sources)):
                if not reviewed or not confirmed:
                    st.warning('请核对补货设置并勾选确认；如果存在旧任务，还需核对归属。')
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
