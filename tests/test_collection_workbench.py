"""Real source-management workbench; synthetic observation input, no model/CUA claim."""
import csv
import io
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright, expect

from start import Service, Supervisor
from tests.test_data_contract import tables
from tests.test_workbench_e2e import free_port

ROOT = Path(__file__).resolve().parents[1]


def click_button(page, label):
    target = page.get_by_role('button', name=label, exact=True)
    # Streamlit can briefly retain the old keyed widget during a full rerender.
    # Wait for one current actionable control; never choose an arbitrary match.
    expect(target).to_have_count(1)
    expect(target).to_be_enabled()
    target.click()


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E') != '1', reason='opt-in real workbench browser')
@pytest.mark.parametrize('save_seconds', [1, 60])
def test_source_settings_scheduled_csv_download_and_old_approval_block(tmp_path, save_seconds):
    api_port, ui_port, portal_port = free_port(), free_port(), free_port()
    api, ui = f'http://127.0.0.1:{api_port}', f'http://127.0.0.1:{ui_port}'
    portal = f'http://127.0.0.1:{portal_port}'
    token = 'isolated-source-workbench-token'
    env = {**os.environ, 'RESTOCK_AGENT_MODE': 'off', 'RESTOCK_APPROVAL_TOKEN': token,
           'INTERN_API_KEY': 'unused-offline-key', 'RESTOCK_API_URL': api,
           'RESTOCK_DB_PATH': str(tmp_path/'main.db'), 'RESTOCK_COLLECTION_DIR': str(tmp_path/'collections'),
           'PROCUREMENT_PORTAL_URL': portal, 'PROCUREMENT_DB_PATH': str(tmp_path/'portal.db'),
           'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    services = [Service('portal',portal_port,[sys.executable,'-m','uvicorn','app.procurement_portal:app','--host','127.0.0.1','--port',str(portal_port)],'/health'),
                Service('api',api_port,[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(api_port)],'/health'),
                Service('ui',ui_port,[sys.executable,'-m','streamlit','run',str(ROOT/'ui/app.py'),
                     '--server.address','127.0.0.1','--server.port',str(ui_port),'--server.headless','true',
                     '--browser.gatherUsageStats','false'],'/_stcore/health')]
    supervisor = Supervisor(services, env, tmp_path/'logs', timeout=40)
    try:
        supervisor.start()
        with sync_playwright() as pw, httpx.Client(base_url=api,trust_env=False,headers={'X-Restock-Approval':token},timeout=20) as http:
            browser = pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            page = browser.new_page(viewport={'width':1440,'height':1100},accept_downloads=True)
            page.set_default_timeout(20000)
            try:
                page.goto(ui)
                page.get_by_text('数据来源与定期保存',exact=True).click()
                page.get_by_role('textbox',name='来源名称',exact=True).fill('模拟页面来源')
                page.get_by_role('textbox',name='商户标识',exact=True).fill('m-test')
                page.get_by_role('textbox',name='仓库标识',exact=True).fill('w-test')
                for label,value in [('采集间隔（秒）','120'),('保存间隔（秒）',str(save_seconds)),('补货检查间隔（秒）','10800')]:
                    page.get_by_role('spinbutton',name=label,exact=True).fill(value)
                for label in ['来源数量和包装已统一为件','来源可用库存已扣除预占',
                              '来源采购成本是含税含运的整数分','来源在途时间是距规划时点的天数']:
                    page.get_by_text(label,exact=True).click()
                    expect(page.get_by_role('checkbox',name=label,exact=True)).to_be_checked()
                click_button(page, '保存数据来源')
                expect(page.get_by_text('暂无正式数据版本，尚未完整采集或未到保存周期。')).to_be_visible()
                sources = http.get('/data-sources').json()['sources']; assert len(sources)==1
                source = sources[0]; sid=source['source_id']
                assert source['config']['schedule']['collect_every_seconds']==120
                assert source['config']['schedule']['save_every_seconds']==save_seconds
                assert source['config']['schedule']['check_every_seconds']==10800
                sales,inventory,suppliers=tables()
                now=datetime.now(timezone.utc).isoformat()
                payload={'batch_id':'synthetic-1','merchant_id':'m-test','warehouse_id':'w-test',
                         'observed_at':now,'source_as_of':now,'complete':True,
                         'row_counts':{'sales':28,'inventory':1,'suppliers':1},
                         'sales':sales,'inventory':inventory,'suppliers':suppliers}
                r=http.post(f'/data-sources/{sid}/observations',json=payload); assert r.status_code==200,r.text
                if save_seconds == 60:
                    assert http.get(f'/data-sources/{sid}/publications').json()['publications']==[]
                deadline=time.monotonic()+85
                while time.monotonic()<deadline:
                    versions=http.get(f'/data-sources/{sid}/publications').json()['publications']
                    if versions: break
                    time.sleep(.5)
                assert len(versions)==1
                assert http.get('/tasks').json()['tasks']==[]
                click_button(page, '刷新数据进度')
                click_button(page, '读取本版文件')
                with page.expect_download() as download:
                    click_button(page, '下载 sales.csv')
                path=tmp_path/'sales.csv'; download.value.save_as(path)
                rows=list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8'))))
                assert len(rows)==28 and sum(int(r['sold_qty']) for r in rows)==112
                click_button(page, '选择本版用于补货')
                page.get_by_role('spinbutton',name='采购预算（元）',exact=True).fill('100')
                page.get_by_role('spinbutton',name='覆盖天数',exact=True).fill('7')
                page.get_by_role('spinbutton',name='安全天数',exact=True).fill('2')
                click_button(page, '生成补货方案')
                expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible(timeout=20000)
                task=page.url.split('task=')[1].split('&')[0]
                page.get_by_text('我已核对 v1 的数量、金额和风险提示',exact=True).click()
                expect(page.get_by_role('checkbox',name='我已核对 v1 的数量、金额和风险提示',exact=True)).to_be_checked()
                click_button(page, '批准当前方案')
                expect(page.get_by_role('button',name='创建采购申请',exact=True)).to_be_visible()
                payload['batch_id']='synthetic-2'
                payload['observed_at']=payload['source_as_of']=datetime.now(timezone.utc).isoformat()
                payload['inventory'][0]['available_qty']='12'
                assert http.post(f'/data-sources/{sid}/observations',json=payload).status_code==200
                expect(page.get_by_text('来源数据需复核：',exact=False)).to_be_visible(timeout=10000)
                state=http.get(f'/tasks/{task}').json()
                assert state['state']=='NEEDS_REVIEW' and state['approved_plan_hash'] is None
                assert http.post(f'/tasks/{task}/execute').status_code==409
                assert http.get(f'/tasks/{task}/executions').json()['records']==[]
                assert page.locator('[data-testid="stException"]').count()==0
                evidence=ROOT/'outputs/m5-1-workbench'; evidence.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(evidence/'source-change-block.png'),full_page=True)
                deadline=time.monotonic()+85
                while time.monotonic()<deadline:
                    if len(http.get(f'/data-sources/{sid}/publications').json()['publications'])==2: break
                    time.sleep(.5)
                versions = http.get(f'/data-sources/{sid}/publications').json()['publications']
                assert len(versions)==2
                latest = http.get(f'/data-sources/{sid}').json()['state']['latest_dataset_id']
                click_button(page, '刷新数据进度')
                expect(page.get_by_role('combobox',name='正式 CSV 版本',exact=True)).to_match_aria_snapshot('- combobox "正式 CSV 版本": 版本 2 · 销售截至 2026-10-28')
                click_button(page, '选择本版用于补货')
                expect(page).to_have_url(re.compile('dataset='+latest))
                click_button(page, '应用新数据并重新生成')
                expect(page.get_by_role('heading',name='与上一版本比较')).to_be_visible()
                expect(page.get_by_text('¥36.00',exact=True)).to_be_visible()
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                click_button(page, '批准当前方案')
                click_button(page, '创建采购申请')
                expect(page.get_by_text('申请已创建，回读核对一致。',exact=True)).to_be_visible(timeout=45000)
                with httpx.Client(trust_env=False) as independent:
                    orders=independent.get(portal+'/api/purchase-orders').json()
                assert len(orders)==1 and orders[0]['total_cost_cents']==3600
                assert [(line['sku'],line['order_qty'],line['unit_cost_cents']) for line in orders[0]['lines']]==[('A',36,100)]
                assert http.post(f'/tasks/{task}/execute').status_code==409
                page.screenshot(path=str(evidence/'source-completed.png'),full_page=True)

            except Exception:
                page.screenshot(path=str(tmp_path/'failure.png'),full_page=True)
                raise
            finally: browser.close()
    finally: supervisor.stop()
