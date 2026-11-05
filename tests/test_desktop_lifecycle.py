"""U02/U03/U04 regression contracts; isolated ports and processes only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading

import httpx
import psutil
import pytest
import start
from start import StartupError


def test_local_stop_requires_control_token_and_preserves_other_instances(tmp_path,monkeypatch):
    from launcher_control import LocalControl,request_control,read_instance
    monkeypatch.setenv('RESTOCK_INSTANCE_FILE',str(tmp_path/'instance.json'))
    stopping=threading.Event()
    control=LocalControl(stopping,'off')
    try:
        assert read_instance()['pid']==os.getpid()
        assert httpx.post(control.url+'/stop',trust_env=False).status_code==403
        assert not stopping.is_set()
        assert request_control()['state']=='starting'
        with pytest.raises(OSError): LocalControl(threading.Event(),'off')
        assert request_control('stop')['state']=='stopping'
        assert stopping.wait(2)
    finally: control.close()
    assert read_instance() is None


def test_missing_browser_rejected_before_start(tmp_path, monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(tmp_path))
    with pytest.raises(StartupError, match='运行环境'):
        start.preflight([], {**os.environ, 'PROCUREMENT_PORTAL_URL':'http://127.0.0.1:8200'}, True)


def test_stop_during_preflight_releases_probe(monkeypatch):
    from app.processes import ProcessScope
    original=ProcessScope.spawn; processes=[]
    def record(self,*args,**kwargs):
        process=original(self,*args,**kwargs); processes.append(process); return process
    monkeypatch.setattr(ProcessScope,'spawn',record)
    stop=threading.Event(); stop.set()
    with pytest.raises(StartupError,match='取消'):
        start.check_runtime_probe(dict(os.environ),False,stop)
    assert processes and all(p.poll() is not None for p in processes)


@pytest.mark.parametrize('setting', ['RESTOCK_API_URL', 'RESTOCK_LITELLM_URL'])
def test_mismatched_managed_service_url_is_rejected(setting):
    with pytest.raises(StartupError, match=setting):
        start.preflight([], {**os.environ, setting:'http://127.0.0.1:1',
                        'PROCUREMENT_PORTAL_URL':'http://127.0.0.1:8200'}, True)


@pytest.mark.skipif(os.name != 'nt' or os.getenv('RUN_STARTUP_E2E') != '1', reason='Windows process lifecycle integration')
def test_crashed_supervisor_leaves_no_service_or_grandchild(tmp_path):
    marker=tmp_path/'ready.json'
    code='''import json,os,sys
from pathlib import Path
from start import Supervisor
from tests.test_startup import stub_service
root=Path(sys.argv[1]); s=Supervisor([stub_service(root)],dict(os.environ),root/'logs',timeout=10)
s.start()
(root/'ready.json').write_text(json.dumps({'pid':os.getpid(),'port':s.specs[0].port}))
s.wait()
'''
    proc=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    root=psutil.Process(proc.pid); root.create_time(); owned=[root]
    outsider=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline=time.monotonic()+15
        while not marker.exists() and time.monotonic()<deadline:
            assert proc.poll() is None
            time.sleep(.1)
        info=json.loads(marker.read_text())
        owned += root.children(recursive=True)
        for p in owned: p.create_time()
        psutil.Process(info['pid']).kill()
        proc.wait(timeout=10)
        _,alive=psutil.wait_procs(owned,timeout=5)
        assert not alive
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{info['port']}/health",trust_env=False,timeout=1)
        assert outsider.poll() is None
    finally:
        for p in reversed(owned):
            try:
                if p.is_running(): p.kill()
            except psutil.NoSuchProcess: pass
        psutil.wait_procs(owned,timeout=5)
        outsider.terminate(); outsider.wait(timeout=5)


@pytest.fixture(autouse=True)
def isolated_control(tmp_path,monkeypatch):
    monkeypatch.setenv('RESTOCK_INSTANCE_FILE',str(tmp_path/'instance.json'))
    monkeypatch.setenv('RESTOCK_LIFECYCLE_LOCK',str(tmp_path/'service.lock'))


def test_stale_pid_record_does_not_block_setup_but_kernel_lock_does(tmp_path,monkeypatch):
    import shutil
    from launcher_control import LifecycleLock,read_instance
    project=tmp_path/'project';(project/'scripts').mkdir(parents=True);(project/'outputs').mkdir()
    root=Path(__file__).resolve().parents[1]
    shutil.copyfile(root/'scripts/setup.ps1',project/'scripts/setup.ps1')
    # Deliberate invalid manifest stops before network/install after checking the actual lock.
    (project/'bootstrap-lock.json').write_text(json.dumps({'schema':0,'python':{'version':'wrong','sha256':'0'*64}}))
    (project/'runtime-lock.json').write_text(json.dumps({'python':'3.12.14'}))
    raw=b'# keep exact bytes\r\nINTERN_API_KEY=synthetic-config\r\n'
    (project/'.env').write_bytes(raw)
    instance=project/'outputs/launcher-instance.json'
    instance.write_text(json.dumps({'pid':os.getpid(),'created':psutil.Process().create_time()-86400,
        'url':'http://127.0.0.1:1','token':'test-token','mode':'off'}))
    monkeypatch.setenv('RESTOCK_INSTANCE_FILE',str(instance))
    assert read_instance() is None
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    command=[str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(project/'scripts/setup.ps1'),'-NoPause']
    def run():
        r=subprocess.run(command,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode,(r.stdout+r.stderr).decode('utf-8',errors='replace')
    code,message=run()
    assert code==1 and 'Bootstrap manifest does not match' in message
    assert 'The project is running' not in message
    lock=LifecycleLock(project/'.restock/setup.lock')
    try:
        code,message=run()
        assert code==1 and 'The project is running' in message
    finally:lock.close()
    code,message=run()
    assert 'Bootstrap manifest does not match' in message
    assert (project/'.env').read_bytes()==raw


def test_setup_lock_release_after_abnormal_owner_exit(tmp_path):
    from launcher_control import LifecycleLock
    marker=tmp_path/'held'
    code="from launcher_control import LifecycleLock;from pathlib import Path;import sys,time,os; lock=LifecycleLock(Path(sys.argv[1]));Path(sys.argv[2]).write_text(str(os.getpid()));time.sleep(60)"
    from app.processes import ProcessScope
    scope=ProcessScope()
    proc=scope.spawn([sys.executable,'-c',code,str(tmp_path/'shared.lock'),str(marker)])
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.05)
        assert marker.exists()
        with pytest.raises(OSError):LifecycleLock(tmp_path/'shared.lock')
        psutil.Process(int(marker.read_text())).kill();proc.wait(5)
        lock=LifecycleLock(tmp_path/'shared.lock');lock.close()
    finally:
        scope.stop();scope.close();proc.wait(5)


def test_installer_gui_calls_only_existing_bootstrap_and_preserves_locks():
    root=Path(__file__).resolve().parents[1]
    script=(root/'scripts/install_window.ps1').read_text(encoding='utf-8-sig')
    assert 'setup.ps1' in script and '-NoPause' in script
    assert 'runtime-manifest' not in script and 'download_runtime' not in script


def test_launcher_credentials_never_reach_product_cli(monkeypatch):
    from app.runner import child_environment
    monkeypatch.setenv('RESTOCK_CONTROL_TOKEN','synthetic-control-secret')
    monkeypatch.setenv('RESTOCK_CONTROL_URL','http://127.0.0.1:1234')
    assert not {'RESTOCK_CONTROL_TOKEN','RESTOCK_CONTROL_URL'} & child_environment().keys()


@pytest.mark.skipif(os.name!='nt' or os.getenv('RUN_STARTUP_E2E')!='1',reason='actual Windows service descendants')
def test_stop_returns_only_after_recorded_service_descendants_exit(tmp_path):
    from start import Supervisor
    from tests.test_startup import stub_service
    for index in range(3):
        root=tmp_path/str(index);root.mkdir()
        supervisor=Supervisor([stub_service(root)],dict(os.environ),root/'logs',timeout=10)
        owned=[]
        try:
            supervisor.start()
            for _,_,owner,_ in supervisor.children:
                owned.extend([owner]+owner.children(recursive=True))
            for proc in owned:proc.create_time()
        finally:supervisor.stop()
        assert not [proc.pid for proc in owned if proc.is_running()]
