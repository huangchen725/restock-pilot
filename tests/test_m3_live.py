"""Real Chromium + actual local portal verification of remaining M3 blockers."""
import asyncio
import json
import threading
from pathlib import Path
import os
import pytest
from tests.test_m3_safety import env
from app import main, db
from app.procurement_portal import create_portal_app
from tests.test_fault_injection import FaultInjector, _start_portal, _free_port

pytestmark=pytest.mark.skipif(os.getenv('RUN_PLAYWRIGHT_E2E')!='1',reason='opt-in real M3 Chromium')
OUT=Path(__file__).resolve().parents[1]/'outputs'/'m3-remediation'

def evidence(name,value):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

class DeferredFirstPost:
    def __init__(self,app):
        self.app=app; self.accepted=threading.Event(); self.release=threading.Event(); self.committed=threading.Event(); self.error=None; self.posts=0
    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['method']=='POST' and scope['path']=='/purchase-orders':
            self.posts+=1
            if self.posts==1:
                messages=[]
                while True:
                    message=await receive(); messages.append(message)
                    if not message.get('more_body'):break
                self.accepted.set()
                async def commit_later():
                    try:
                        while not self.release.is_set():await asyncio.sleep(.05)
                        idx=0
                        async def replay():
                            nonlocal idx
                            if idx<len(messages):
                                msg=messages[idx]; idx+=1; return msg
                            return {'type':'http.disconnect'}
                        async def sink(message):pass
                        await self.app(scope,replay,sink)
                    except Exception as exc:self.error=str(exc)
                    finally:self.committed.set()
                asyncio.create_task(commit_later())
                while hasattr(self,'response_gate') and not self.response_gate.is_set():await asyncio.sleep(.02)
                await send({'type':'http.response.start','status':502,'headers':[(b'content-type',b'text/plain')]})
                await send({'type':'http.response.body','body':b'accepted but persistence deliberately deferred'})
                return
        await self.app(scope,receive,send)

def test_live_unknown_revise_cannot_create_duplicate_purchase(env,tmp_path):
    e=env; injector=DeferredFirstPost(create_portal_app(tmp_path/'portal.db'))
    url,portal,server,thread=_start_portal(tmp_path,injector)
    e.monkeypatch.setenv('PROCUREMENT_PORTAL_URL',url);e.monkeypatch.setenv('PROCUREMENT_TIMEOUT_MS','1200')
    try:
        first=e.client.post(f'/tasks/{e.tid}/execute')
        assert first.status_code==200 and first.json()['state']=='NEEDS_RECONCILIATION',first.text
        assert injector.accepted.is_set() and portal.state.store.list()==[]
        revision=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
        result={'original_execution_state':first.json()['state'],'revision_http':revision.status_code}
        if revision.status_code==200:
            assert e.client.post(f'/tasks/{e.tid}/approve').status_code==200
            injector.release.set(); assert injector.committed.wait(5) and injector.error is None
            assert len(portal.state.store.list())==1
            second=e.client.post(f'/tasks/{e.tid}/execute')
            orders=portal.state.store.list()
            result.update({'second_execution_http':second.status_code,'second_execution_state':second.json().get('state'),'orders':orders,
                           'total_qty':sum(l['order_qty'] for o in orders for l in o['lines']),
                           'total_cents':sum(o['total_cost_cents'] for o in orders),'original_target_qty':48,'task_budget_cents':4800})
        evidence('live-unknown-revision',result)
        assert revision.status_code==409,result
        injector.release.set();assert injector.committed.wait(5) and injector.error is None
        recovered=e.client.post(f'/tasks/{e.tid}/recover')
        assert recovered.status_code==200 and recovered.json()['state']=='COMPLETED',recovered.text
        assert len(portal.state.store.list())==1 and portal.state.store.list()[0]['lines'][0]['order_qty']==18
        assert e.client.get(f'/tasks/{e.tid}/summary').json()['pending_items']==[]
        assert e.client.get(f'/tasks/{e.tid}/audit-trail').json()['replay_complete'] is True
    finally:
        injector.release.set(); injector.committed.wait(5); server.should_exit=True;thread.join(timeout=5)

def test_live_recover_after_connection_refusal_completes(env,tmp_path):
    e=env; injector=FaultInjector(create_portal_app(tmp_path/'portal.db'))
    url,portal,server,thread=_start_portal(tmp_path,injector)
    e.monkeypatch.setenv('PROCUREMENT_PORTAL_URL',f'http://127.0.0.1:{_free_port()}')
    try:
        first=e.client.post(f'/tasks/{e.tid}/execute'); assert first.status_code==200 and first.json()['state']=='FAILED'
        e.monkeypatch.setenv('PROCUREMENT_PORTAL_URL',url)
        recovered=e.client.post(f'/tasks/{e.tid}/recover')
        result={'first_state':first.json()['state'],'recovery_http':recovered.status_code,'recovery_body':recovered.json(),
                'task_state':db.get_task(e.conn,e.tid)['state'],'actual_orders':portal.state.store.list()}
        evidence('live-recover-state',result)
        assert recovered.status_code==200 and recovered.json()['state']=='COMPLETED',result
        assert len(portal.state.store.list())==1
        assert e.client.get(f'/tasks/{e.tid}/audit-trail').json()['replay_complete'] is True
    finally:server.should_exit=True;thread.join(timeout=5)

def test_live_ledger_write_failure_cannot_report_completed(env,tmp_path):
    e=env; injector=FaultInjector(create_portal_app(tmp_path/'portal.db'))
    url,portal,server,thread=_start_portal(tmp_path,injector)
    e.monkeypatch.setenv('PROCUREMENT_PORTAL_URL',url)
    original=db.append_execution_event
    def fail(conn,tid,event_type,ref,payload):
        if event_type=='order_result': raise RuntimeError('injected audit event storage failure')
        return original(conn,tid,event_type,ref,payload)
    e.monkeypatch.setattr(db,'append_execution_event',fail)
    try:
        r=e.client.post(f'/tasks/{e.tid}/execute')
        result={'http':r.status_code,'state':r.json().get('state'),'actual_orders':portal.state.store.list(),
                'event_types':[x['event_type'] for x in db.list_execution_events(e.conn,e.tid)],
                'activity_kinds':[x['kind'] for x in db.list_activity(e.conn,e.tid)]}
        evidence('live-ledger-failure',result)
        assert r.status_code==500 and db.get_task(e.conn,e.tid)['state']=='NEEDS_RECONCILIATION',result
        assert len(portal.state.store.list())==1 and db.list_execution_records(e.conn,e.tid)==[]
        assert e.client.get(f'/tasks/{e.tid}/audit-trail').json()['replay_complete'] is False
    finally:server.should_exit=True;thread.join(timeout=5)

def test_live_readback_mismatch_must_block_new_purchase_version(env,tmp_path):
    e=env; injector=FaultInjector(create_portal_app(tmp_path/'portal.db'));injector.tamper_total_delta=1
    url,portal,server,thread=_start_portal(tmp_path,injector)
    e.monkeypatch.setenv('PROCUREMENT_PORTAL_URL',url)
    try:
        first=e.client.post(f'/tasks/{e.tid}/execute')
        assert first.status_code==200 and first.json()['state']=='NEEDS_RECONCILIATION'
        assert len(portal.state.store.list())==1 and injector.injected
        r=e.client.post(f'/tasks/{e.tid}/revise',json={'policy':{**e.policy,'budget_cents':4800}})
        result={'original_state':first.json()['state'],'revision_http':r.status_code,'injections':injector.injected.copy()}
        if r.status_code==200:
            assert e.client.post(f'/tasks/{e.tid}/approve').status_code==200
            injector.tamper_total_delta=0
            second=e.client.post(f'/tasks/{e.tid}/execute'); orders=portal.state.store.list()
            result.update({'second_execution_http':second.status_code,'state':second.json().get('state'),'orders':orders,
                           'total_qty':sum(l['order_qty'] for o in orders for l in o['lines']),'total_cents':sum(o['total_cost_cents'] for o in orders)})
        evidence('live-mismatch-revision',result)
        assert r.status_code==409,result
        injector.tamper_total_delta=0
        recovered=e.client.post(f'/tasks/{e.tid}/recover')
        assert recovered.status_code==200 and recovered.json()['state']=='COMPLETED',recovered.text
        assert len(portal.state.store.list())==1
        assert e.client.get(f'/tasks/{e.tid}/summary').json()['pending_items']==[]
    finally:server.should_exit=True;thread.join(timeout=5)
