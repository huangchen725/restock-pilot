"""Source settings and formal CSV versions; all writes use the business API."""
import streamlit as st


def cadence_fields(prefix, schedule=None):
    defaults = schedule or {'collect_every_seconds': 1800, 'save_every_seconds': 3600,
                            'check_every_seconds': 86400, 'max_age_seconds': 5400}
    labels = {'collect_every_seconds': '采集间隔（秒）', 'save_every_seconds': '保存间隔（秒）',
              'check_every_seconds': '补货检查间隔（秒）', 'max_age_seconds': '数据有效时长（秒）'}
    values = {}
    for column, (key, label) in zip(st.columns(4), labels.items()):
        seconds = column.number_input(label, min_value=1, max_value=2678400,
                                      value=defaults[key], step=1, key=prefix + key)
        values[key] = int(seconds)
    return values


def render(call):
    with st.expander('数据来源与定期保存', expanded=False):
        st.caption('读取结果先整理暂存，到保存周期才形成正式 CSV；补货检查按另设频率进行。频率单位为秒：1800 秒为半小时，3600 秒为一小时。')
        st.info('可连接模拟商户网页，后台自动采集并定期保存。在“自动补货与周期预算”另行启用检查后，会生成或更新待审方案，采购仍须人工批准。')
        with st.form('new-data-source'):
            name = st.text_input('来源名称', key='source-name')
            a, b = st.columns(2)
            merchant = a.text_input('商户标识', key='source-merchant')
            warehouse = b.text_input('仓库标识', key='source-warehouse')
            environment = st.selectbox('数据环境', ['测试数据', '真实店铺'], key='source-environment')
            connect_browser = st.checkbox('连接模拟商户网页（自动采集）', key='source-browser')
            page_url = st.text_input('商户网页地址', value='http://127.0.0.1:8030', key='source-page-url')
            merchant_name = st.text_input('页面商户全称', value='示例便利（模拟）有限公司', key='source-merchant-name')
            scope_text = st.text_input('采集 SKU 范围（逗号分隔；留空为全部在售）', key='source-sku-scope')
            credentials = st.selectbox('网页登录配置', ['测试页默认演示账号', '本地 .env 中的商户账号'], key='source-login-profile')
            st.caption('当前适配器只支持本机模拟页面。测试数据日期较早时，可显式调大有效时长；源数据时间不会被伪造。缺项商品会阻止整批发布，不会自动剔除。')
            schedule = cadence_fields('new-source-')
            st.caption('以下默认频率仅为可修改的初始设置；选择“真实店铺”不代表真实接入已验收。')
            units = st.checkbox('来源数量和包装已统一为件', key='source-units')
            net = st.checkbox('来源可用库存已扣除预占', key='source-net')
            cost = st.checkbox('来源采购成本是含税含运的整数分', key='source-cost')
            eta = st.checkbox('来源在途时间是距规划时点的天数', key='source-eta')
            zero = st.checkbox('来源缺失销量日期已确认代表零销量', key='source-zero')
            if st.form_submit_button('保存数据来源'):
                result = call('POST', '/data-sources', json={
                    'name': name, 'merchant_id': merchant, 'warehouse_id': warehouse,
                    'environment': 'test' if environment == '测试数据' else 'real', 'schedule': schedule,
                    'browser': {'base_url':page_url,'merchant_name':merchant_name,
                                'sku_scope':[v.strip() for v in scope_text.replace('，',',').split(',') if v.strip()],
                                'credential_profile':'demo' if credentials=='测试页默认演示账号' else 'environment'} if connect_browser else None,
                    'contract': {'quantity_unit': 'piece' if units else None, 'available_is_net': net,
                                 'cost_includes_tax_shipping': cost, 'inbound_eta_is_days': eta,
                                 'missing_sales': 'zero' if zero else 'reject'}})
                if result:
                    st.session_state['selected-source'] = result['source_id']
                    st.rerun()
        data = call('GET', '/data-sources') or {'sources': []}
        if not data['sources']:
            st.caption('尚未配置数据来源。')
            return
        sources = {item['source_id']: item for item in data['sources']}
        selected = st.selectbox('查看数据来源', list(sources), format_func=lambda sid: sources[sid]['config']['name'],
                                key='selected-source')
        source = sources[selected]; config = source['config']; state = source['state']
        st.write(f"商户：{config['merchant_id']} · 仓库：{config['warehouse_id']} · " +
                 ('测试数据' if config['environment'] == 'test' else '真实来源（验收另计）'))
        with st.form('source-settings-' + selected):
            schedule = cadence_fields('edit-' + selected, config['schedule'])
            if st.form_submit_button('更新频率设置'):
                if call('PUT', f'/data-sources/{selected}/settings', json=schedule): st.rerun()
        if config.get('browser'):
            st.caption('采集范围：' + (', '.join(config['browser']['sku_scope']) or '全部在售商品'))
            st.write('下次采集：' + state['next_collect_at'])
            enabled = config['browser']['enabled']
            if st.button('暂停自动采集' if enabled else '恢复自动采集', key='toggle-'+selected):
                if call('PUT', f'/data-sources/{selected}/collection-enabled', json={'enabled':not enabled}): st.rerun()
            if st.button('立即采集网页', key='collect-'+selected, disabled=not enabled):
                if call('POST', f'/data-sources/{selected}/collect'): st.rerun()
            job = state.get('collection_job', {})
            st.write('采集状态：' + {'queued':'排队中','running':'正在读取网页','succeeded':'读取校验成功','failed':'失败','interrupted':'已中断'}.get(job.get('status'),'等待首次采集'))
            records = call('GET', f'/data-sources/{selected}/collection-runs')
            if records and records['runs']:
                with st.expander('最近采集记录'):
                    st.json(records['runs'][0])
        st.write('最后成功采集：' + (state['last_success_at'] or '尚无'))
        st.write('源数据时间：' + (state['source_as_of'] or '尚无'))
        st.write('下次保存检查：' + state['next_save_at'])
        if not source['data_status']['ready']: st.caption(source['data_status']['reason'])
        if state['last_problem']: st.error(state['last_problem'])
        if state['last_publish_error']: st.error(state['last_publish_error'])
        if state['pending_snapshot']:
            st.warning('已有更新的数据待保存；绑定旧数据的方案需重新复核。')
        if st.button('刷新数据进度', key='refresh-source-' + selected): st.rerun()
        versions = (call('GET', f'/data-sources/{selected}/publications') or {}).get('publications', [])
        if not versions:
            st.caption('暂无正式数据版本，尚未完整采集或未到保存周期。')
            return
        indexes = list(range(len(versions)))
        index = st.selectbox('正式 CSV 版本', indexes, index=next((i for i,v in enumerate(versions) if v['dataset_id']==state['latest_dataset_id']), len(versions)-1),
                             format_func=lambda i: f"版本 {i+1} · 销售截至 {versions[i]['window_end']}",
                             key='source-version-' + selected + '-' + str(state['latest_dataset_id']))
        manifest = versions[index]; did = manifest['dataset_id']
        st.caption('该版本形成于 ' + manifest['published_at'] + '；再次检查未变化时不会重复生成文件。')
        if st.button('读取本版文件', key='load-csv-' + selected + did):
            files = {name: call('GET', f'/data-sources/{selected}/publications/{did}/{name}')
                     for name in ['sales.csv', 'inventory.csv', 'suppliers.csv', 'manifest.json']}
            if all(value is not None for value in files.values()):
                st.session_state['csv-downloads'] = (selected, did, files)
        cached = st.session_state.get('csv-downloads')
        if cached and cached[:2] == (selected, did):
            for name, contents in cached[2].items():
                st.download_button('下载 ' + name, contents, file_name=name,
                                   mime='application/json' if name.endswith('.json') else 'text/csv',
                                   key='download-' + selected + did + name)
        if st.button('选择本版用于补货', key='use-source-' + selected + did,
                     disabled=not source['data_status']['ready'] or did != state['latest_dataset_id']):
            st.session_state['_select_source_dataset'] = (selected, did)
            st.rerun()
