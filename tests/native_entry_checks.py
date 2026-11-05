"""Opt-in checks of an isolated installed fixture's Stop VBS and workbench stop."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import httpx
import psutil
from playwright.sync_api import sync_playwright, expect
from app.processes import ProcessScope

ROOT=Path(__file__).resolve().parents[1]


def check(project,out):
    project=Path(project).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    if not project.is_relative_to(ROOT/'outputs'):
        raise ValueError('Only a disposable fixture under repository outputs may be used')
    if 'synthetic-install-key' not in (project/'.env').read_text(encoding='utf-8-sig'):
        raise ValueError('Missing explicit synthetic installer fixture configuration')
    env={k:v for k,v in os.environ.items() if not k.startswith(('RESTOCK_','INTERN_','CODEX_','RUN_','OPENAI_','PYTHON'))}
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',RESTOCK_AGENT_MODE='off')
    records=[]
    def instance():
        try:return json.loads((project/'outputs/launcher-instance.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):return None
    for method in ('stop-vbs','workbench-stop'):
        for port in (4000,8100,8200,8501):
            with socket.socket() as sock:
                assert sock.connect_ex(('127.0.0.1',port))!=0, 'Do not adopt any existing service'
        scope=ProcessScope();helper_scope=ProcessScope();proc=None
        with (out/(method+'.log')).open('w',encoding='utf-8') as log:
            try:
                proc=scope.spawn([str(project/'.venv/Scripts/python.exe'),str(project/'start.py')],cwd=project,env=env,
                    stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
                started=time.monotonic()
                while time.monotonic()-started<90:
                    assert proc.poll() is None
                    info=instance()
                    if info:
                        try:
                            state=httpx.get(info['url']+'/status',headers={'X-Restock-Control':info['token']},trust_env=False,timeout=1).json()
                            if state.get('state')=='ready':break
                        except (httpx.HTTPError,ValueError):pass
                    time.sleep(.2)
                else:raise AssertionError('Launcher did not become ready')
                owner=psutil.Process(proc.pid)
                owned=[{'pid':p.pid,'created':p.create_time(),'name':p.name(),'parent':p.ppid()} for p in [owner]+owner.children(recursive=True)]
                harness_consoles={p['pid'] for p in owned if p['name'].lower()=='conhost.exe' and p['parent']==proc.pid}
                assert httpx.post('http://127.0.0.1:8100/system/stop',trust_env=False).status_code==403
                if method=='stop-vbs':
                    helper=helper_scope.spawn([str(Path(os.environ['SystemRoot'])/'System32/wscript.exe'),
                        str(project/'关闭 RestockPilot.vbs')],cwd=project,env=env)
                    helper.wait(10)
                else:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(headless=True,args=['--no-proxy-server'])
                        try:
                            page=browser.new_page();page.goto('http://127.0.0.1:8501')
                            label='确认停止服务（进行中的处理会中断）'
                            page.get_by_text(label,exact=True).click()
                            expect(page.get_by_role('checkbox',name=label,exact=True)).to_be_checked()
                            page.get_by_role('button',name='停止所有服务',exact=True).click()
                            # Keep the browser connected until Streamlit confirms backend acceptance.
                            expect(page.get_by_text('正在停止本次服务。',exact=False)).to_be_visible(timeout=10000)
                        finally:browser.close()
                proc.wait(30)
                deadline=time.monotonic()+10
                while instance() and time.monotonic()<deadline:time.sleep(.1)
                assert instance() is None
                remaining=[]
                for item in owned:
                    try:
                        p=psutil.Process(item['pid'])
                        if p.is_running() and abs(p.create_time()-item['created'])<.01:remaining.append(p.pid)
                    except psutil.Error:pass
                ports={}
                for port in (4000,8100,8200,8501):
                    with socket.socket() as sock:ports[str(port)]=sock.connect_ex(('127.0.0.1',port))!=0
                (out/(method+'-observed.json')).write_text(json.dumps({'remaining_owned':remaining,'ports':ports,'owned':owned,'root_exit':proc.returncode},indent=2),encoding='utf-8')
                assert not (set(remaining)-harness_consoles) and all(ports.values())
                # The harness owns the hidden console for its root Python launcher.
                # Like desktop.poll(), release that owner only after product services exited.
                scope.stop();scope.close()
                still_alive=[]
                for item in owned:
                    try:
                        process=psutil.Process(item['pid'])
                        if process.is_running() and abs(process.create_time()-item['created'])<.01:still_alive.append(process.pid)
                    except psutil.Error:pass
                assert not still_alive
                records.append({'method':method,'passed':True,'owned_count':len(owned),'remaining_owned':still_alive,'harness_console_before_owner_cleanup':remaining,
                                'ports_released':ports,'unauthorized_api_stop':403,'root_exit':proc.returncode})
            finally:
                scope.stop();scope.close();helper_scope.stop();helper_scope.close()
                if proc:proc.wait(5)
            (out/'result.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    return records


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--project',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(check(args.project,args.output),ensure_ascii=False,indent=2))
