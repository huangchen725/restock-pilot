"""Three distinct data entry paths. No business calculations or direct database access."""
import json
import streamlit as st

ROUTES={'自动采集网页':'web','上传表格':'upload','示例体验':'example'}
UPLOAD_CHECKS=[
    ('数量按单个商品填写（例如按瓶，不按箱）','例如 1 箱有 12 瓶，销售、库存、包装和起订数量都按瓶数填写。'),
    ('库存已减去留给现有订单的数量','例如仓库有 100 件，其中 20 件留给已接订单，可用库存填 80。'),
    ('采购价按分填写，并包含税费和运费','12.50 元填写 1250。这里是采购成本，不是卖给顾客的售价。'),
    ('到货时间填写还需几天','这是已订未到商品的预计到货时间。例如距本次计划日期还需 3 天，填写 3。')]


def render(call,show_problem,offline=False):
    st.subheader('选择资料获取方式')
    st.caption('选一种方式即可。网页会自动整理资料；表格适合已有文件；示例适合先熟悉流程。')
    old=st.query_params.get('entry')
    if old not in ROUTES.values() and st.query_params.get('dataset'):old='web' if st.query_params.get('source') else 'upload'
    initial=next((i for i,x in enumerate(ROUTES.values()) if x==old),None)
    choice=st.radio('资料获取方式',list(ROUTES),index=initial,horizontal=True,key='entry-choice')
    route=ROUTES.get(choice)
    if route!=old:
        if route:st.query_params['entry']=route
        else:st.query_params.pop('entry',None)
        st.query_params.pop('dataset',None);st.query_params.pop('source',None)
        st.session_state.pop('upload_rejected',None)
        st.rerun()
    if route=='web':
        from ui.collection_panel import render as connection
        connection(call,offline)
    elif route=='example':
        st.subheader('1 · 使用现成的示例资料')
        st.write('已准备 30 种商品的销售、库存和供应商资料。点击后即可设置预算并体验补货，不用下载或填写表格。')
        st.caption('这是固定的演示资料，不连接网页、不自动更新。产生的采购申请也只进入演示后台。')
        if st.button('使用示例数据',type='primary'):
            result=call('POST','/datasets/example')
            if result:
                st.session_state.pop('upload_rejected',None);st.query_params.pop('source',None)
                st.query_params['dataset']=result['dataset_id'];st.rerun()
    elif route=='upload':upload(call,show_problem)
    else:st.info('先从上面选择一种资料获取方式。你也可以先在下面准备预算和补货要求。')
    if st.session_state.get('upload_rejected'):
        st.warning('本次上传未通过检查，尚未选用。请修正后重新检查。已有任务的原方案保留，但需明确选择后才能继续批准。')
        if st.query_params.get('task') and st.button('继续查看并使用原方案'):
            st.session_state.pop('upload_rejected',None);st.rerun()
    dataset=call('GET','/datasets/'+st.query_params['dataset']) if st.query_params.get('dataset') else None
    if dataset:st.success(f"数据已就绪 · {dataset['sku_count']} 个商品 · {dataset['window_start']} 至 {dataset['window_end']}")
    return route,dataset


def upload(call,show_problem):
    st.subheader('1 · 上传并检查三份表格')
    st.write('请提供同一家店、同一批商品的三份资料。CSV 是可用 Excel 打开的表格文件；网页采集路线不需要再做这一步。')
    st.markdown('| 资料 | 用来回答什么问题 |\n|---|---|\n| 销售记录 | 最近 28 天，每天每种商品卖了多少？ |\n| 库存资料 | 现在还有多少可用库存？已订未到的货有多少？ |\n| 供应商资料 | 向谁进货、采购价多少、多久到货、每包多少、最低订多少？ |')
    template=call('GET','/dataset-templates')
    if template is not None:st.download_button('下载填写样例（3 份 CSV）',template,file_name='restock-templates.zip',mime='application/zip')
    st.caption('下载包包含演示行和填写说明。请替换成你的资料、保留英文列名，再分别上传；每个文件最多 5 MB。')
    with st.expander('查看文件列名和填写例子'):
        st.markdown('销售：`date` 日期、`sku` 商品编号、`sold_qty` 当天销量。每种商品需要完整的 28 天记录。\n\n'
            '库存：`sku` 商品编号、`name` 名称、`category` 类别、`available_qty` 可用数量、`inbound_qty` 已订未到数量、`inbound_eta` 剩余到货天数。\n\n'
            '供应商：`sku` 商品编号、`supplier_id` 供应商编号、`unit_cost_cents` 采购价（分）、`lead_days` 交货天数、`pack_size` 每包商品数、`min_order_qty` 最低采购数。')
    with st.form('upload'):
        cols=st.columns(3)
        files=[col.file_uploader(label,type=['csv'],key=key) for col,label,key in zip(cols,
            ['销售记录（每天卖出多少）','库存资料（可用及已订未到）','供应商资料（采购价格和交货条件）'],['sales','inventory','suppliers'])]
        st.write('核对表格的填写方式')
        st.caption('这些是对表格内容的确认，勾选不会替你换算数据。下面每项都有例子；不确定时请先核实。')
        checks=[st.checkbox(label,help=tip) for label,tip in UPLOAD_CHECKS]
        with st.expander('统计日期和缺失记录怎么处理'):
            end=st.text_input('销量统计截止日期（可留空）',placeholder='例如 2026-10-28',help='以这一天为终点统计近 28 天销量。留空使用文件里最新的销售日期。')
            missing=st.checkbox('我确认：没记录的日期就是没有销售',value=False,help='可选项，默认不勾选。若可能是漏导出或漏记，请补齐数据，不能把它当作零销量。')
        if st.form_submit_button('检查并保存数据',type='primary'):
            st.query_params.pop('dataset',None);st.query_params.pop('source',None);st.session_state['upload_rejected']=True
            if not all(files):st.error('请上传完整的三份表格：销售、库存和供应商资料。')
            else:
                contract={'quantity_unit':'piece' if checks[0] else None,'available_is_net':checks[1],
                          'cost_includes_tax_shipping':checks[2],'inbound_eta_is_days':checks[3],
                          'missing_sales':'zero' if missing else 'reject','window_end':end or None}
                payload={n:(n+'.csv',f.getvalue(),'text/csv') for n,f in zip(('sales','inventory','suppliers'),files)}
                result=call('POST','/datasets',files=payload,data={'contract':json.dumps(contract)})
                if result and result.get('dataset_id'):
                    st.session_state.pop('upload_rejected',None);st.query_params['dataset']=result['dataset_id'];st.rerun()
                elif result:
                    for issue in result.get('data_problems',[]):show_problem(issue)
    st.caption('检查通过后才能生成方案。更换资料不会自动修改已经保存的任务，仍需在任务里明确应用。')
