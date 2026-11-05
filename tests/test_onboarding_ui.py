"""UX-01..07: user-visible entry points on actual isolated Streamlit/Chromium."""
import os
from pathlib import Path
import pytest
from playwright.sync_api import expect
from tests.test_usability_ui import workbench

pytestmark=pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='actual onboarding UI')


def test_sidebar_can_reopen_repeatedly_and_on_narrow_screen(workbench):
    page,urls,*_=workbench;page.goto(urls['ui'])
    for _ in range(3):
        page.get_by_test_id('stSidebar').hover()
        page.get_by_role('button',name='Collapse sidebar',exact=True).click()
        expect(page.get_by_role('button',name='Expand sidebar',exact=True)).to_be_visible(timeout=2500)
        page.get_by_role('button',name='Expand sidebar',exact=True).click()
        expect(page.get_by_role('heading',name='任务记录',exact=True)).to_be_visible()
    page.set_viewport_size({'width':600,'height':900})
    expand=page.get_by_role('button',name='Expand sidebar',exact=True)
    # Streamlit collapses automatically after the mobile breakpoint transition.
    expect(page.get_by_test_id('stSidebar')).to_have_attribute('data-collapsed','true')
    expect(expand).to_be_visible();expand.click()
    page.get_by_role('button',name='Collapse sidebar',exact=True).click()
    expect(expand).to_be_visible();expand.click()
    expect(page.get_by_role('button',name='刷新任务记录',exact=True)).to_be_visible()


def test_three_entry_routes_are_separate_and_empty_data_only_blocks_generation(workbench):
    page,urls,*_=workbench;page.goto(urls['ui'])
    expect(page.get_by_role('radiogroup',name='资料获取方式',exact=True)).to_be_visible(timeout=2500)
    expect(page.get_by_role('spinbutton',name='采购预算（元）',exact=True)).to_be_enabled()
    expect(page.get_by_role('button',name='生成补货方案',exact=True)).to_be_disabled()
    page.get_by_text('自动采集网页',exact=True).click()
    expect(page.get_by_role('button',name='启动测试页面',exact=True)).to_be_visible()
    expect(page.locator('input[type=file]')).to_have_count(0)
    expect(page.get_by_role('button',name='使用示例数据',exact=True)).to_have_count(0)
    page.get_by_text('上传表格',exact=True).click()
    expect(page.locator('input[type=file]')).to_have_count(3)
    expect(page.get_by_role('button',name='启动测试页面',exact=True)).to_have_count(0)
    page.get_by_role('button',name='检查并保存数据',exact=True).click()
    expect(page.get_by_text('请上传完整的三份表格：销售、库存和供应商资料。',exact=True)).to_be_visible()
    expect(page.get_by_role('button',name='生成补货方案',exact=True)).to_be_disabled()
    page.get_by_text('示例体验',exact=True).click()
    expect(page.get_by_role('button',name='使用示例数据',exact=True)).to_be_visible()
    expect(page.locator('input[type=file]')).to_have_count(0)
    page.get_by_role('button',name='使用示例数据',exact=True).click()
    expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
    page.get_by_text('上传表格',exact=True).click()
    expect(page.get_by_role('button',name='生成补货方案',exact=True)).to_be_disabled()
    import re
    expect(page).not_to_have_url(re.compile(r'[?&]dataset='))
    expect(page).to_have_url(re.compile(r'[?&]entry=upload(?:&|$)'))


@pytest.mark.parametrize('ending',['normal','api-crash'])
def test_demo_page_starts_from_workbench_reuses_process_and_exits_with_app(workbench,ending):
    import httpx,psutil,socket
    page,urls,supervisor,env,*_=workbench
    page.goto(urls['ui']);page.get_by_text('自动采集网页',exact=True).click()
    page.get_by_role('button',name='启动测试页面',exact=True).click()
    expect(page.get_by_role('link',name='打开测试商户后台',exact=True)).to_be_visible()
    with httpx.Client(base_url=urls['api'],trust_env=False) as client:
        status=client.get('/demo-site').json();assert status['running']
        assert client.post('/demo-site/start').status_code==403
        repeat=client.post('/demo-site/start',headers={'X-Restock-Approval':env['RESTOCK_APPROVAL_TOKEN']})
        assert repeat.status_code==200 and repeat.json()['pid']==status['pid']
    with page.expect_popup() as popup:page.get_by_role('link',name='打开测试商户后台',exact=True).click()
    merchant=popup.value
    expect(merchant.get_by_role('button',name='登录',exact=True)).to_be_visible()
    merchant.close()
    expect(page.get_by_role('button',name='连接测试页面并开始读取',exact=True)).to_be_disabled()
    if ending=='normal':supervisor.stop()
    else:
        import time
        api_owner=next(owner for spec,_,owner,_ in supervisor.children if spec.name=='api')
        demo_parent=psutil.Process(status['pid']).parent()
        assert demo_parent.pid in {p.pid for p in [api_owner]+api_owner.children(recursive=True)}
        demo_parent.kill();demo_parent.wait(timeout=5)
        deadline=time.monotonic()+5
        while psutil.pid_exists(status['pid']) and time.monotonic()<deadline:time.sleep(.05)
    assert not psutil.pid_exists(status['pid'])
    with socket.socket() as sock:sock.bind(('127.0.0.1',int(env['RESTOCK_DEMO_PORT'])))


def test_failed_table_replacement_keeps_original_task_but_blocks_approval(workbench,tmp_path):
    import httpx
    from tests.ui_actions import choose_entry
    from tests.test_usability_ui import upload
    from tests.test_data_contract import tables,write_dataset
    page,urls,*_=workbench;page.goto(urls['ui']);choose_entry(page,'示例体验')
    page.get_by_role('button',name='使用示例数据',exact=True).click()
    page.get_by_role('button',name='生成补货方案',exact=True).click()
    expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible()
    tid=page.url.split('task=')[1].split('&')[0]
    old=httpx.get(urls['api']+'/tasks/'+tid,trust_env=False).json()['latest_plan_hash']
    broken=tables();broken[1][0]['available_qty']='-1';write_dataset(tmp_path,broken)
    upload(page,tmp_path)
    expect(page.get_by_text('库存资料第 2 行',exact=False)).to_be_visible()
    expect(page.get_by_role('button',name='批准当前方案',exact=True)).to_have_count(0)
    assert httpx.get(urls['api']+'/tasks/'+tid,trust_env=False).json()['latest_plan_hash']==old
    page.get_by_role('button',name='继续查看并使用原方案',exact=True).click()
    expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible()
    assert httpx.get(urls['portal']+'/api/purchase-orders',trust_env=False).json()==[]


def test_requirements_entered_before_data_survive_route_selection(workbench):
    from tests.ui_actions import choose_entry
    page,urls,*_=workbench;page.goto(urls['ui'])
    budget=page.get_by_role('spinbutton',name='采购预算（元）',exact=True)
    budget.fill('1300');budget.press('Tab')
    choose_entry(page,'示例体验')
    page.get_by_role('button',name='使用示例数据',exact=True).click()
    expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
    expect(budget).to_have_value('1300.00')


def test_example_entry_full_revision_approval_orders_and_exports(tmp_path):
    from tests.workbench_flow import run_workbench_flow
    run_workbench_flow(tmp_path,'off',tmp_path/'evidence',continuous=True,entry='example')
