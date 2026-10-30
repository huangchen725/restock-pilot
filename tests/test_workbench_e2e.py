"""Real workbench + API + procurement portal, isolated data, no model requests."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright, expect

from start import Service, Supervisor
from tests.test_data_contract import write_dataset

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(os.getenv('RUN_WORKBENCH_E2E') != '1', reason='opt-in real workbench browser')
def test_upload_revise_approve_execute_refresh_and_download(tmp_path):
    ports = [free_port() for _ in range(3)]
    portal, api, ui = [f'http://127.0.0.1:{p}' for p in ports]
    env = {**os.environ, 'RESTOCK_AGENT_MODE':'off', 'RESTOCK_APPROVAL_TOKEN':'local-ui-test-token',
           'INTERN_API_KEY':'unused-offline-test-key', 'RESTOCK_API_URL':api,
           'PROCUREMENT_PORTAL_URL':portal, 'RESTOCK_DB_PATH':str(tmp_path/'main.db'),
           'PROCUREMENT_DB_PATH':str(tmp_path/'portal.db'), 'PYTHONIOENCODING':'utf-8'}
    specs = [Service(name,port,[sys.executable,'-m','uvicorn',module,'--host','127.0.0.1','--port',str(port)],'/health')
             for name,port,module in [('portal',ports[0],'app.procurement_portal:app'),('api',ports[1],'app.main:app')]]
    specs.append(Service('ui',ports[2],[sys.executable,'-m','streamlit','run',str(ROOT/'ui/app.py'),
        '--server.address','127.0.0.1','--server.port',str(ports[2]),'--server.headless','true',
        '--browser.gatherUsageStats','false'],'/_stcore/health'))
    supervisor = Supervisor(specs,env,tmp_path/'logs',timeout=40)
    write_dataset(tmp_path)
    try:
        supervisor.start()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
            page=browser.new_page(viewport={'width':1440,'height':1100},accept_downloads=True)
            page.set_default_timeout(20000)
            try:
                page.goto(ui)
                expect(page.get_by_text('离线演示模式：',exact=False)).to_be_visible()
                uploads=page.locator('input[type=file]')
                for index,name in enumerate(('sales','inventory','suppliers')):
                    uploads.nth(index).set_input_files(str(tmp_path/(name+'.csv')))
                # Missing confirmations are actionable; the same files can be corrected.
                page.get_by_role('button',name='检查并保存数据').click()
                expect(page.get_by_text('先上传三份资料，或选择示例数据。')).to_be_visible()
                for label in ('三份数据已统一为件','可用库存已扣除预占','单价为含税含运的整数分','在途到货时间填写的是距规划时点的天数'):
                    page.get_by_text(label,exact=True).click()
                    expect(page.get_by_role('checkbox',name=label,exact=True)).to_be_checked()
                page.get_by_role('button',name='检查并保存数据').click()
                expect(page.get_by_text('数据已就绪 · 1 个商品',exact=False)).to_be_visible()
                page.get_by_role('spinbutton',name='采购预算（元）',exact=True).fill('100')
                page.get_by_role('spinbutton',name='覆盖天数',exact=True).fill('7')
                page.get_by_role('spinbutton',name='安全天数',exact=True).fill('2')
                page.get_by_role('button',name='生成补货方案').click()
                expect(page.get_by_role('heading',name='人工确认',exact=True)).to_be_visible()
                expect(page.get_by_text('¥48.00',exact=True)).to_be_visible()
                task=page.url.split('task=')[1].split('&')[0]
                page.get_by_text('我已核对 v1 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案').click()
                expect(page.get_by_role('button',name='创建采购申请')).to_be_visible()
                page.get_by_text('调整参数',exact=True).click()
                page.get_by_role('spinbutton',name='采购预算（元）',exact=True).fill('18')
                page.get_by_role('button',name='按参数重新计算').click()
                expect(page.get_by_role('heading',name='与上一版本比较')).to_be_visible()
                expect(page.get_by_text('¥18.00',exact=True)).to_be_visible()
                expect(page.get_by_text('总额变化：¥-30.00',exact=True)).to_be_visible()
                expect(page.get_by_role('button',name='创建采购申请')).to_have_count(0)
                page.get_by_text('我已核对 v2 的数量、金额和风险提示',exact=True).click()
                page.get_by_role('button',name='批准当前方案').click()
                page.get_by_role('button',name='创建采购申请').click()
                expect(page.get_by_text('申请已创建，回读核对一致。',exact=True)).to_be_visible(timeout=45000)
                expect(page.get_by_role('button',name='下载完整报告 HTML')).to_be_visible()
                # Refresh (new session) must restore the task, version and receipts.
                page.reload()
                expect(page.get_by_text('申请已创建，回读核对一致。',exact=True)).to_be_visible()
                for label,suffix in [('下载补货清单 CSV','csv'),('下载完整报告 HTML','html')]:
                    with page.expect_download() as pending:
                        page.get_by_role('button',name=label).click()
                    target=tmp_path/f'delivered.{suffix}'
                    pending.value.save_as(target)
                    text=target.read_text(encoding='utf-8-sig')
                    assert '1800' in text and task in text
                    if suffix=='html': assert 'SUBMITTED' in text and 'PO-' in text
                with httpx.Client(trust_env=False) as client:
                    orders=client.get(portal+'/api/purchase-orders').json()
                    assert len(orders)==1
                    order=orders[0]
                    assert order['external_ref']==f'{task}-v2-S'
                    assert order['supplier_id']=='S' and order['status']=='SUBMITTED'
                    assert order['total_cost_cents']==1800
                    assert [(i['sku'],i['order_qty'],i['unit_cost_cents']) for i in order['lines']]==[('A',18,100)]
                    assert client.post(api+f'/tasks/{task}/execute').status_code==409
                    assert len(client.get(portal+'/api/purchase-orders').json())==1
                evidence=ROOT/'outputs'/'m1-workbench'
                evidence.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(evidence/'completed.png'),full_page=True)
                page.get_by_role('heading',name='RestockPilot',exact=True).scroll_into_view_if_needed()
                page.screenshot(path=str(evidence/'overview.png'),full_page=True)
                (evidence/'report.html').write_bytes((tmp_path/'delivered.html').read_bytes())
                assert page.locator('[data-testid="stException"]').count()==0
                # Existing developer smoke entry remains usable with strict approvals.
                # It uses the bundled 30-SKU sample and verifies only its own orders.
                smoke=subprocess.run([sys.executable,str(ROOT/'scripts/run_smoke_e2e.py'),
                    '--base',api,'--portal',portal],env=env,cwd=ROOT,capture_output=True,
                    text=True,encoding='utf-8',timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                assert smoke.returncode==0,smoke.stdout+smoke.stderr
                (evidence/'smoke.txt').write_text(smoke.stdout,encoding='utf-8')
            except Exception:
                page.screenshot(path=str(tmp_path/'failure.png'),full_page=True)
                raise
            finally:
                browser.close()
    finally:
        supervisor.stop()
