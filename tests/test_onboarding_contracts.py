"""UX source setup, first publication, and owned demo-server contracts."""
from datetime import timedelta
from tests.test_step3_api import client
from tests.test_collection import clock,config,batch,auth,observe,publish


def test_first_publication_is_immediate_only_when_explicitly_selected(client,clock):
    c=config();c['publish_first_immediately']=True
    r=client.post('/data-sources',json=c,headers=auth());assert r.status_code==200,r.text
    sid=r.json()['source_id'];assert observe(client,sid,batch(clock)).status_code==200
    assert publish(client,sid).status_code==200
    clock[0]+=timedelta(seconds=1)
    assert observe(client,sid,batch(clock,'changed','10')).status_code==200
    assert publish(client,sid).status_code==409
    clock[0]+=timedelta(seconds=60)
    assert publish(client,sid).status_code==200


def test_demo_start_requires_human_authorization(client):
    r=client.post('/demo-site/start',headers={'X-Restock-Approval':''})
    assert r.status_code==403,r.text


def test_demo_start_conflict_returns_actionable_error_without_adopting_port(client,monkeypatch,tmp_path):
    import socket
    from app import main
    from app.demo_site import DemoSite
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1',0));foreign.listen()
        port=foreign.getsockname()[1]
        monkeypatch.setenv('RESTOCK_DEMO_PORT',str(port))
        monkeypatch.setenv('RESTOCK_DEMO_LOG_DIR',str(tmp_path/'demo-logs'))
        manager=DemoSite();monkeypatch.setattr(main.app.state,'demo_site',manager,raising=False)
        r=client.post('/demo-site/start',headers=auth())
        assert r.status_code==409 and '测试页面未能启动' in r.json()['detail'] and '占用' in r.json()['detail']
        assert manager.supervisor is None and foreign.getsockname()[1]==port
