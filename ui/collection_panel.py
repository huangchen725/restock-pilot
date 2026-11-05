"""Guided page connection and source management; all writes use the business API."""
import streamlit as st


def cadence_fields(prefix, schedule=None):
    defaults = schedule or {'collect_every_seconds':1800,'save_every_seconds':3600,'check_every_seconds':600,'max_age_seconds':2678400}
    labels={'collect_every_seconds':'采集间隔（秒）','save_every_seconds':'保存间隔（秒）',
            'check_every_seconds':'补货检查间隔（秒）','max_age_seconds':'数据有效时长（秒）'}
    tips={'collect_every_seconds':'多久重新读取一次网页。1800 秒是 30 分钟。',
          'save_every_seconds':'多久把完整的新数据保存成正式资料。同样的数据不会重复保存。3600 秒是 1 小时。',
          'check_every_seconds':'启用定期补货后，多久检查一次补货需求。不会自动批准采购。',
          'max_age_seconds':'从网页标明的数据日期算起，超过多久就需要重新确认数据。历史测试资料使用 31 天，实际店铺应另行设置。'}
    return {key:int(col.number_input(label,min_value=1,max_value=2678400,value=defaults[key],step=1,key=prefix+key,help=tips[key]))
            for col,(key,label) in zip(st.columns(4),labels.items())}


def normalized_contract():
    # This adapter verifies visible stock/cost/unit facts and normalizes output.
    # Missing records are still rejected, never relabelled as zero.
    return {'quantity_unit':'piece','available_is_net':True,'cost_includes_tax_shipping':True,
            'inbound_eta_is_days':True,'missing_sales':'reject'}


def render(call,offline=False):
    st.subheader('1 · 连接网页并读取资料')
    st.write('从我们提供的商户测试页面读取销售、库存、供应商和在途资料。系统整理后保存，无需再上传三份表格。')
    st.caption('当前提供测试后台连接；实际店铺平台需要单独适配。读取不会修改商户的商品、库存或订单。')
    method=st.radio('连接方式',['使用内置测试页面','连接已运行的测试页面'],horizontal=True,key='source-method')
    if method=='使用内置测试页面':
        status=call('GET','/demo-site')
        if status:
            profile=status['profile']
            if status['running']:
                st.success('测试页面已启动，可直接连接。')
                st.link_button('打开测试商户后台',status['url'])
                st.caption('如需手动查看，测试账号为 demo_reader，密码为 demo-pass-2026。')
            elif st.button('启动测试页面',type='primary'):
                if call('POST','/demo-site/start'):st.rerun()
            st.write('演示门店：城南店 · 仓库：城南店仓 · 读取范围：数据完整的 30 种演示商品')
            st.caption('测试后台还包含缺价格等故障样例。本入口明确选择下面的 30 种商品，不会在读取失败后悄悄删除商品。')
            with st.expander('查看本次商品范围和保存安排'):
                st.write('商品编号（SKU，用来区分具体商品规格）：'+', '.join(profile['sku_scope']))
                st.write('首次完整读取后立即保存。之后每 30 分钟读取，每 1 小时保存变化；启用定期补货后每 10 分钟检查。')
                st.caption(profile['history_notice'])
            with st.form('connect-managed-page'):
                name=st.text_input('给这份资料起个名字',value=profile['name'],help='只是方便你辨认的备注，不需要填写商家注册名称。')
                accepted=st.checkbox('我已确认使用这家演示门店和上述 30 种商品')
                if st.form_submit_button('连接测试页面并开始读取',disabled=offline or not status['running']):
                    if not accepted:st.warning('请先确认本次读取的演示门店和商品范围。')
                    else:
                        browser={'base_url':status['url'],'merchant_name':profile['merchant_name'],'sku_scope':profile['sku_scope'],'credential_profile':'demo'}
                        sources=(call('GET','/data-sources') or {}).get('sources',[])
                        same=next((x for x in sources if x['config'].get('browser') and x['config']['browser']['base_url']==status['url']
                            and x['config']['merchant_id']==profile['merchant_id'] and x['config']['warehouse_id']==profile['warehouse_id']
                            and set(x['config']['browser']['sku_scope'])==set(profile['sku_scope'])),None)
                        result=same or call('POST','/data-sources',json={'name':name,'merchant_id':profile['merchant_id'],
                            'warehouse_id':profile['warehouse_id'],'environment':'test','browser':browser,
                            'contract':normalized_contract(),'schedule':profile['schedule'],'publish_first_immediately':True})
                        if result:
                            if same and not same['config']['browser']['enabled']:
                                if not call('PUT','/data-sources/'+same['source_id']+'/collection-enabled',json={'enabled':True}):return
                            st.session_state['selected-source']=result['source_id'];st.rerun()
    else:
        with st.expander('连接已有测试页面（高级设置）',expanded=True):
            st.caption('填写你已启动的同一种测试后台。门店和仓库编号可在测试后台的门店列表查看；这些不是营业执照号码。')
            with st.form('new-data-source'):
                name=st.text_input('来源名称',value='我的测试页面',help='这条连接的备注名，可自行命名。')
                page_url=st.text_input('商户网页地址',value='http://127.0.0.1:8030',help='只支持本机测试页面地址。页面需要先启动。')
                merchant=st.text_input('门店编号',value='S01',help='例如城南店是 S01，应与测试页面的门店列表一致。')
                warehouse=st.text_input('仓库编号',value='W01',help='例如城南店仓是 W01，应与所选门店对应。')
                merchant_name=st.text_input('页面上的商家全称',value='示例便利（模拟）有限公司',help='照填页面显示的公司名称，用来核对是否连接了正确后台；不是连接备注名。')
                scope_text=st.text_input('需要读取的商品编号（SKU）',help='SKU 是商品规格编号，例如 SKU-1001。多个编号用逗号分隔；留空会读取全部在售商品，遇到缺价等问题会停止整批保存。')
                credentials=st.selectbox('登录方式',['测试页默认演示账号','已经配置的本地账号'],help='本地账号由启动配置提供，不在这里输入密码。')
                schedule=cadence_fields('new-source-')
                if st.form_submit_button('保存连接并开始读取',disabled=offline):
                    result=call('POST','/data-sources',json={'name':name,'merchant_id':merchant,'warehouse_id':warehouse,
                        'environment':'test','schedule':schedule,'publish_first_immediately':True,'contract':normalized_contract(),
                        'browser':{'base_url':page_url,'merchant_name':merchant_name,
                            'sku_scope':[v.strip() for v in scope_text.replace('，',',').split(',') if v.strip()],
                            'credential_profile':'demo' if credentials=='测试页默认演示账号' else 'environment'}})
                    if result:st.session_state['selected-source']=result['source_id'];st.rerun()
    if offline:st.info('当前是离线体验模式，可以启动和查看测试页面。自动读取网页需要在启动窗口启用真实模型；也可先选择“示例体验”。')
    st.caption('读取器会核对页面数量、可用库存和采购成本，并统一单位。缺记录不会自动当作零销量，缺价格不会自动补造。')
    manage(call)


@st.fragment(run_every=3)
def manage(call):
    data = call('GET', '/data-sources') or {'sources': []}
    if not data['sources']:
        st.caption('尚未配置数据来源。')
        return
    sources = {item['source_id']: item for item in data['sources']}
    selected = st.selectbox('查看数据来源', list(sources), format_func=lambda sid: sources[sid]['config']['name'],
                            key='selected-source')
    source = sources[selected]; config = source['config']; state = source['state']
    st.write(f"门店编号：{config['merchant_id']} · 仓库编号：{config['warehouse_id']} · " +
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
    if st.button('用这份资料继续', key='use-source-' + selected + did,
                 disabled=not source['data_status']['ready'] or did != state['latest_dataset_id']):
        st.session_state['_select_source_dataset'] = (selected, did)
        st.rerun()
