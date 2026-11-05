"""U05/U06/U07: user recovery and data selection through the real workbench."""
import os
from pathlib import Path
import socket
import sys
import httpx
import psutil
import pytest
from playwright.sync_api import sync_playwright,expect
from start import Service,Supervisor,services
from tests.test_data_contract import tables,write_dataset

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E')!='1',reason='explicit workbench browser')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]


@pytest.fixture
def workbench(tmp_path):
    ports={name:free_port() for name in ('portal','api','ui')}
    urls={name:f'http://127.0.0.1:{port}' for name,port in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'off','RESTOCK_APPROVAL_TOKEN':'journey-ui-test',
         'INTERN_API_KEY':'unused-test','RESTOCK_API_URL':urls['api'],'PROCUREMENT_PORTAL_URL':urls['portal'],
         'RESTOCK_DB_PATH':str(tmp_path/'business.db'),'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'),
         'PYTHONIOENCODING':'utf-8'}
    specs=[]
    for spec in services(True):
        command=list(spec.command); flag='--server.port' if spec.name=='ui' else '--port'
        command[command.index(flag)+1]=str(ports[spec.name])
        specs.append(Service(spec.name,ports[spec.name],command,spec.health_path))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=40)
    replacements=[]
    try:
        supervisor.start()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            page=browser.new_page(viewport={'width':1440,'height':1100},accept_downloads=True)
            page.set_default_timeout(20000)
            try: yield page,urls,supervisor,env,specs,replacements
            finally: browser.close()
    finally:
        for item in replacements: item.stop()
        supervisor.stop()


def upload(page,path):
    for i,name in enumerate(('sales','inventory','suppliers')):
        remove=page.get_by_role('button',name='Remove '+name+'.csv',exact=True)
        if remove.count():
            remove.click()
            expect(remove).to_have_count(0)
            page.locator('input[type=file]').nth(i).set_input_files([])
        page.locator('input[type=file]').nth(i).set_input_files(str(path/(name+'.csv')))
        expect(page.get_by_role('button',name='Remove '+name+'.csv',exact=True)).to_be_visible()
    for label in ('三份数据已统一为件','可用库存已扣除预占','单价为含税含运的整数分','在途到货时间填写的是距规划时点的天数'):
        checkbox=page.get_by_role('checkbox',name=label,exact=True)
        if not checkbox.is_checked(): page.get_by_text(label,exact=True).click()
    page.get_by_role('button',name='检查并保存数据').click()


def test_rejected_replacement_blocks_generation_until_corrected(workbench,tmp_path):
    page,urls,*_=workbench
    page.goto(urls['ui']); page.get_by_role('button',name='使用示例数据').click()
    expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
    broken=tables(); broken[1][0]['available_qty']='-1'; write_dataset(tmp_path,broken)
    upload(page,tmp_path)
    expect(page.get_by_text('库存资料第 2 行',exact=False)).to_be_visible()
    expect(page.get_by_role('button',name='生成补货方案')).to_have_count(0)
    expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_have_count(0)
    assert 'dataset=' not in page.url
    assert httpx.get(urls['api']+'/tasks',trust_env=False).json()['tasks']==[]
    write_dataset(tmp_path)
    upload(page,tmp_path)
    expect(page.get_by_text('数据已就绪 · 1 个商品',exact=False)).to_be_visible()
    with page.expect_download() as download: page.get_by_role('button',name='下载三份 CSV 模板').click()
    target=tmp_path/'templates.zip'; download.value.save_as(target)
    import zipfile
    assert {'sales.csv','inventory.csv','suppliers.csv','填写说明.txt'}==set(zipfile.ZipFile(target).namelist())
    page.get_by_role('button',name='生成补货方案').click()
    expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible()
    task=page.url.split('task=')[1].split('&')[0]
    assert len(httpx.get(urls['api']+'/tasks/'+task,trust_env=False).json()['latest_plan']['items'])==1




def test_idle_form_keeps_user_edit_while_background_watcher_polls(workbench):
    page,urls,*_=workbench
    page.goto(urls['ui']);page.get_by_role('button',name='使用示例数据').click()
    expect(page.get_by_text('数据已就绪 · 30 个商品',exact=False)).to_be_visible()
    page.get_by_role('button',name='生成补货方案',exact=True).click()
    expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible(timeout=20000)
    page.get_by_text('调整参数',exact=True).click()
    field=page.get_by_role('spinbutton',name='覆盖天数',exact=True)
    expect(field).to_have_count(1)
    expect(field).to_be_enabled()
    field.fill('17')
    page.wait_for_timeout(4500)
    expect(field).to_have_value('17')
    assert page.locator('[data-testid="stException"]').count()==0
