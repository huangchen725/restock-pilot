"""Opt-in real InternLM -> locked CLI -> read-only browser -> staged data -> CSV."""
import csv
import io
import json
import os
from pathlib import Path
import secrets
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright, expect
from tests.test_collection_workbench import click_button

from app import collection
from app.merchant_test_portal import build_portal_data
from start import Service, Supervisor, services
from tests.test_real_m2 import free_port
from tests.test_browser_collection import portal, config, assert_snapshot_matches_portal

# Explicit authorized test scope: 30 valid active SKUs. Bad fields remain tested separately;
# production never derives or silently changes this scope from validation failures.
SCOPE=['SKU-'+str(1000+i) for i in (1,2,3,4,5,6,8,9,10,12,13,15,16,17,18,20,22,24,25,26,27,29,30,31,32,33,34,36,37,39)]


@pytest.mark.skipif(os.getenv('RUN_REAL_MODEL_E2E')!='1',reason='explicit real model opt-in')
def test_real_scheduled_browser_collection_two_batches(tmp_path,portal):
    assert os.getenv('INTERN_API_KEY'), 'Real model credential is not configured'
    target,merchant_url=portal
    ports={name:free_port() for name in ('proxy','api','ui')}
    urls={name:f'http://127.0.0.1:{port}' for name,port in ports.items()}
    env={**os.environ,'RESTOCK_AGENT_MODE':'required','RESTOCK_LITELLM_URL':urls['proxy'],
         'RESTOCK_API_URL':urls['api'],'RESTOCK_DB_PATH':str(tmp_path/'business.db'),'RESTOCK_COLLECTION_DIR':str(tmp_path/'collections'),
         'RESTOCK_APPROVAL_TOKEN':secrets.token_urlsafe(32),'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    specs=[]
    for spec in services(False):
        if spec.name not in ports: continue
        cmd=list(spec.command); flag='--server.port' if spec.name=='ui' else '--port';cmd[cmd.index(flag)+1]=str(ports[spec.name])
        specs.append(Service(spec.name,ports[spec.name],cmd,spec.health_path))
    supervisor=Supervisor(specs,env,tmp_path/'logs',timeout=60)
    evidence={'model':'intern-s2','environment':'synthetic merchant HTML only','scope':SCOPE,
              'automatic_first_run':True,'runs':[],'passed':False}
    destination=Path('outputs/m5-collector-real')/tmp_path.parent.name;destination.mkdir(parents=True,exist_ok=True)
    def wait(client,sid,after=None):
        started=time.monotonic()
        while time.monotonic()-started<660:
            state=client.get('/data-sources/'+sid).json()
            job=state['state'].get('collection_job',{})
            if job.get('run_id')!=after and job.get('status') in ('succeeded','failed','interrupted'):
                runs=client.get('/data-sources/'+sid+'/collection-runs').json()['runs']
                if not any(r['run_id']==runs[0]['run_id'] for r in evidence['runs']): evidence['runs'].append(runs[0])
                assert job['status']=='succeeded',runs[0]
                if state['state']['latest_dataset_id']==state['state']['latest_observed_dataset_id'] and state['state']['latest_dataset_id']:
                    return state
            time.sleep(1)
        pytest.fail('Collector did not finish and publish within bounded timeout')
    pw=browser=None
    try:
        supervisor.start()
        pw=sync_playwright().start();browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
        page=browser.new_page(viewport={'width':1440,'height':1200},accept_downloads=True)
        page.set_default_timeout(20000)
        page.goto(urls['ui'])
        with httpx.Client(base_url=urls['api'],trust_env=False,timeout=20) as client:
            auth={'X-Restock-Approval':env['RESTOCK_APPROVAL_TOKEN']}
            page.get_by_text('数据来源与定期保存',exact=True).click()
            for label,value in [('来源名称','真实书生页面采集验收'),('商户标识','S01'),('仓库标识','W01'),
                                ('商户网页地址',merchant_url),('采集 SKU 范围（逗号分隔；留空为全部在售）',','.join(SCOPE))]:
                page.get_by_role('textbox',name=label,exact=True).fill(value)
            for label in ['连接模拟商户网页（自动采集）','来源数量和包装已统一为件','来源可用库存已扣除预占',
                          '来源采购成本是含税含运的整数分','来源在途时间是距规划时点的天数']:
                page.get_by_text(label,exact=True).click()
                expect(page.get_by_role('checkbox',name=label,exact=True)).to_be_checked()
            for label,value in [('保存间隔（秒）','1'),('数据有效时长（秒）','2678400')]:
                page.get_by_role('spinbutton',name=label,exact=True).fill(value)
            click_button(page,'保存数据来源')
            expect(page.get_by_role('button',name='立即采集网页',exact=True)).to_be_visible()
            sources=client.get('/data-sources').json()['sources'];assert len(sources)==1
            sid=sources[0]['source_id'];evidence['source_id']=sid
            assert sources[0]['config']['browser']['sku_scope']==SCOPE
            assert client.post('/data-sources/'+sid+'/collect').status_code==403
            first=wait(client,sid)
            def verify(state):
                did=state['state']['latest_dataset_id'];tables={}
                for name in ('sales','inventory','suppliers'):
                    r=client.get(f'/data-sources/{sid}/publications/{did}/{name}.csv')
                    assert r.status_code==200
                    tables[name]=list(csv.DictReader(io.StringIO(r.content.decode('utf-8'))))
                from app.validation import prepare_snapshot
                snapshot,errors=prepare_snapshot(tables['sales'],tables['inventory'],tables['suppliers'],config(merchant_url,SCOPE).contract)
                assert not errors
                assert_snapshot_matches_portal(snapshot,target,set(SCOPE))
                assert len(snapshot.sales)==840
                return {'dataset_id':did,'rows':{k:len(v) for k,v in tables.items()},'independent_match':True}
            evidence['first']=verify(first)
            click_button(page,'刷新数据进度')
            click_button(page,'读取本版文件')
            with page.expect_download() as download:
                click_button(page,'下载 sales.csv')
            download.value.save_as(tmp_path/'downloaded-sales.csv')
            assert len(list(csv.DictReader((tmp_path/'downloaded-sales.csv').open(encoding='utf-8'))))==840
            click_button(page,'暂停自动采集')
            expect(page.get_by_role('button',name='恢复自动采集',exact=True)).to_be_visible()
            assert client.post('/data-sources/'+sid+'/collect',headers=auth).status_code==409
            page.screenshot(path=str(destination/'workbench-paused.png'),full_page=True)
            target.data=build_portal_data(batch=2)
            click_button(page,'恢复自动采集')
            expect(page.get_by_role('button',name='暂停自动采集',exact=True)).to_be_visible()
            second=wait(client,sid,first['state']['collection_job']['run_id'])
            evidence['second']=verify(second)
            assert evidence['first']['dataset_id']!=evidence['second']['dataset_id']
            assert len(client.get('/data-sources/'+sid+'/publications').json()['publications'])==2
            assert target.write_log==[]
            assert client.get('/tasks').json()['tasks']==[]
            for run in evidence['runs']:
                successful=[e for e in run['events'] if not e['error']]
                assert any(e['tool']=='finish_source_collection' and e['result']['complete'] for e in successful)
                model=[e['result'] for e in successful if e['tool']=='model_response']
                assert model and model[0]['model']=='intern-s2' and model[0]['exit_code']==0
            click_button(page,'刷新数据进度')
            page.screenshot(path=str(destination/'workbench-complete.png'),full_page=True)
            bad=client.post('/data-sources',headers=auth,json=config(merchant_url,['SKU-1011']).model_dump())
            assert bad.status_code==200
            bad_sid=bad.json()['source_id'];started=time.monotonic()
            while time.monotonic()-started<660:
                bad_state=client.get('/data-sources/'+bad_sid).json()
                bad_job=bad_state['state'].get('collection_job',{})
                if bad_job.get('status') in ('failed','succeeded','interrupted'):break
                time.sleep(1)
            bad_runs=client.get('/data-sources/'+bad_sid+'/collection-runs').json()['runs']
            evidence['negative_run']=bad_runs[0] if bad_runs else None
            assert bad_job.get('status')=='failed',bad_runs
            assert any('成本缺失' in str(e.get('error')) for e in bad_runs[0]['events'])
            assert client.get('/data-sources/'+bad_sid+'/publications').json()['publications']==[]
            assert len(client.get('/data-sources/'+sid+'/publications').json()['publications'])==2
            assert target.write_log==[]
            evidence['writes']=target.write_log;evidence['passed']=True
    finally:
        if browser:browser.close()
        if pw:pw.stop()
        supervisor.stop()
        (destination/'result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
