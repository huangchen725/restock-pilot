"""Owned demo process: actual start, idempotency, conflicts, failure and cleanup."""
import os,socket
from concurrent.futures import ThreadPoolExecutor
import httpx,psutil,pytest
from app.demo_site import DemoSite
from start import StartupError
from tests.test_usability_ui import free_port

@pytest.fixture
def demo(monkeypatch,tmp_path):
    monkeypatch.setenv('RESTOCK_DEMO_PORT',str(free_port()))
    monkeypatch.setenv('RESTOCK_DEMO_LOG_DIR',str(tmp_path/'logs'))
    site=DemoSite()
    try:yield site
    finally:site.stop()


def assert_released(site,pid):
    assert not psutil.pid_exists(pid)
    with socket.socket() as s:s.bind(('127.0.0.1',site.port))


def test_owned_demo_concurrent_start_stop_restart(demo):
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=list(pool.map(lambda _:demo.start(),range(2)))
    assert a['running'] and b['running'] and a['pid']==b['pid']
    assert len(a['profile']['sku_scope'])==30
    with httpx.Client(trust_env=False,follow_redirects=True) as c:
        r=c.get(a['url']);assert r.status_code==200 and '登录' in r.text
        assert c.get(a['url']+'/api/sales').status_code==404
    demo.stop();assert_released(demo,a['pid'])
    c=demo.start();assert c['running'] and c['pid']!=a['pid']
    demo.stop();assert_released(demo,c['pid'])


def test_foreign_port_is_never_adopted_or_closed(demo):
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1',demo.port));foreign.listen()
        with pytest.raises(StartupError,match='占用'):demo.start()
        assert not demo.status()['running'] and demo.supervisor is None
        assert foreign.getsockname()[1]==demo.port
    result=demo.start();assert result['running']


def test_failed_health_check_cleans_child_and_can_retry(demo,monkeypatch):
    import start
    original=start.Supervisor
    spawned=[]
    class FailingHealth(original):
        def __init__(self,*args,**kwargs):
            kwargs['timeout']=.6;super().__init__(*args,**kwargs);spawned.append(self)
    original_health=start.is_healthy
    monkeypatch.setattr(start,'Supervisor',FailingHealth)
    monkeypatch.setattr(start,'is_healthy',lambda *a,**k:False)
    with pytest.raises(StartupError):demo.start()
    assert spawned and spawned[0].children
    for _,child,_,_ in spawned[0].children:assert_released(demo,child.pid)
    assert demo.supervisor is None
    monkeypatch.setattr(start,'Supervisor',original)
    monkeypatch.setattr(start,'is_healthy',original_health)
    assert demo.start()['running']
