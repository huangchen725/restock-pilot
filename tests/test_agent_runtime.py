"""Protocol-boundary tests, including opt-in real CLI with a local fake model.

No test here calls a real model. The fake Responses server is a fault injector,
not evidence about InternLM semantics or latency.
"""
import json
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app import db, runner
from app.agent_runtime import AgentRuntime, restrict_request, validate_model_output
from app.business_tools import ToolSession
from app.models import Policy, TaskState
from app.validation import prepare_snapshot
from tests.test_data_contract import tables, contract


@pytest.mark.parametrize('kind,name', [
    ('function_call','exec_command'), ('custom_tool_call','apply_patch'),
    ('function_call','mcp__other__read'), ('computer_call',''),
    ('function_call','read_file'), ('function_call','read_mcp_resource'),
    ('function_call','view_image'), ('web_search_call',''),
    ('function_call','mcp__restock__approve'), ('function_call','mcp__restock__build_plan')])
def test_unallowed_model_calls_are_blocked_before_forwarding(kind,name):
    payload={'type':kind,'name':name,'arguments':'{}'}
    allowed={'mcp__restock__inspect_dataset'}
    for data,mime in [(json.dumps({'output':[payload]}).encode(),'application/json'),
                      (('data: '+json.dumps({'type':'response.output_item.added','item':payload})+'\n\n').encode(),'text/event-stream')]:
        with pytest.raises(ValueError): validate_model_output(data,mime,allowed)


def test_request_and_child_environment_expose_only_capabilities(monkeypatch):
    monkeypatch.setenv('INTERN_API_KEY','upstream-secret')
    monkeypatch.setenv('RESTOCK_APPROVAL_TOKEN','approval-secret')
    monkeypatch.setenv('RESTOCK_DB_PATH','private.db')
    monkeypatch.setenv('PYTHONPATH','untrusted-code')
    env=runner.child_environment({'RESTOCK_AGENT_TOKEN':'stage-token'})
    assert not {'INTERN_API_KEY','RESTOCK_APPROVAL_TOKEN','RESTOCK_DB_PATH','PYTHONPATH'} & env.keys()
    assert env['RESTOCK_AGENT_TOKEN']=='stage-token'
    request=restrict_request({'tools':[{'type':'function','name':'exec_command'},
        {'type':'function','name':'mcp__restock__inspect_dataset'},{'type':'web_search'}]}, {'mcp__restock__inspect_dataset'})
    assert [t['name'] for t in request['tools']]==['mcp__restock__inspect_dataset']


def test_namespace_alias_cannot_smuggle_a_different_executor():
    allowed={'mcp__restock__inspect_dataset'}
    payload={'output':[{'type':'function_call','name':'mcp__restock__inspect_dataset','namespace':'functions'}]}
    with pytest.raises(ValueError): validate_model_output(json.dumps(payload).encode(),'application/json',allowed)
    request=restrict_request({'tools':[{'type':'namespace','name':'mcp__restock','tools':[
        {'type':'function','name':'inspect_dataset'},{'type':'function','name':'approve'}]},
        {'type':'namespace','name':'functions','tools':[{'type':'function','name':'exec_command'}]}]},allowed)
    assert [t['name'] for t in request['tools']]==['mcp__restock__inspect_dataset']


def test_broker_rejects_uncredentialed_calls_and_unknown_routes():
    with AgentRuntime(None,upstream_key='unused') as runtime:
        with httpx.Client(trust_env=False) as client:
            assert client.post(runtime.url+'/tools',json={}).status_code==403
            auth={'Authorization':'Bearer '+runtime.token}
            assert client.post(runtime.url+'/tasks/x/approve',json={},headers=auth).status_code==404
            assert client.post(runtime.url+'/tools',json={'tool':'build_plan','arguments':{}},headers=auth).status_code==400
        home=runtime.home
        config=(home/'config.toml').read_text(encoding='utf-8')
        assert 'danger-full-access' not in config and 'read-only' in config
        assert 'unused' not in config and 'E:/restock' not in config
    assert not home.exists()


def event_stream(item, index):
    rid='resp_test_'+str(index)
    response={'id':rid,'object':'response','created_at':int(time.time()),'model':'intern-s2',
              'status':'in_progress','output':[]}
    events=[{'type':'response.created','response':response},
            {'type':'response.output_item.added','output_index':0,'item':item},
            {'type':'response.output_item.done','output_index':0,'item':item},
            {'type':'response.completed','response':{**response,'status':'completed','output':[item],
                'usage':{'input_tokens':100,'output_tokens':30,'total_tokens':130}}}]
    return ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()


@pytest.mark.skipif(os.getenv('RUN_AGENT_BOUNDARY_E2E')!='1',reason='opt-in real Codex, local fake model only')
@pytest.mark.parametrize('malicious',[False,True])
def test_real_cli_scoped_mcp_and_hostile_model_output(tmp_path,malicious,monkeypatch):
    conn=db.init_db(tmp_path/'business.db')
    snapshot,errors=prepare_snapshot(*tables(),contract()); assert not errors
    policy=Policy(budget_cents=1800,cover_days=7,safety_days=2)
    db.upsert_task(conn,'scope-test',TaskState.ANALYZING)
    db.claim_task(conn,'scope-test','测试')
    session=ToolSession(conn,'scope-test','planner',snapshot,fixed_policy=policy)
    observed=[]
    raw_tools=[]
    from app import agent_runtime
    original=agent_runtime.restrict_request
    def capture_tools(payload,allowed):
        raw_tools.append(payload.get('tools', []))
        return original(payload,allowed)
    monkeypatch.setattr(agent_runtime,'restrict_request',capture_tools)
    marker=tmp_path/'must-not-exist.txt'
    class FakeModel(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append(request)
            index=len(observed)
            if malicious:
                item={'type':'function_call','id':'fc_bad','call_id':'call_bad','name':'exec_command',
                      'arguments':json.dumps({'cmd':f'echo bypass > "{marker}"'})}
            elif index<=2:
                tool='inspect_dataset' if index==1 else 'build_plan'
                args={'task_id':'scope-test'}
                if index==2: args.update(dataset_id=session.dataset_id,policy=policy.model_dump())
                item={'type':'function_call','id':f'fc_{index}','call_id':f'call_{index}',
                      'name':'mcp__restock__'+tool,'arguments':json.dumps(args)}
            else:
                item={'type':'message','id':'msg_done','role':'assistant','status':'completed',
                      'content':[{'type':'output_text','text':'本地协议测试完成','annotations':[]}]}
            data=event_stream(item,index)
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),FakeModel)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        with session.activate(), AgentRuntime(session,upstream_url=f'http://127.0.0.1:{server.server_port}',upstream_key='fake-upstream') as runtime:
            result=runner.run_codex_task('仅使用本次业务工具完成检查与计算，不运行命令。',str(runtime.workdir),
                codex_home=str(runtime.home),child_env=runtime.env,timeout_s=45,
                executable=runner.resolve_codex_bin(str(Path(__file__).resolve().parents[1])))
            evidence=Path(__file__).resolve().parents[1]/'outputs/m2-remediation'
            evidence.mkdir(parents=True,exist_ok=True)
            (evidence/('hostile-cli.json' if malicious else 'allowed-cli.json')).write_text(json.dumps({
                'exit_code':result.exit_code,'timed_out':result.timed_out,'stderr':result.stderr,
                'events':[e.raw for e in result.events],
                'cli_tools':raw_tools,
                'advertised_tools':[[t.get('name',t['type']) for t in req.get('tools',[])] for req in observed]},ensure_ascii=False,indent=2),encoding='utf-8')
            assert observed, result.stderr
            assert all({t['name'] for t in req['tools']} <= runtime.allowed for req in observed)
            assert not marker.exists()
            if malicious:
                assert result.exit_code!=0
                assert not result.timed_out and len(observed)==1
                assert any('boundary_rejection' in json.dumps(e.raw) for e in result.events)
                assert not result.tool_calls
                assert session.draft is None and db.list_plan_versions(conn,'scope-test')==[]
            else:
                assert result.exit_code==0, result.stderr
                assert not result.timed_out
                plan,_=session.commit(policy)
                assert plan.total_cost_cents==1800 and plan.items[0].order_qty==18
                calls=[e['payload']['tool'] for e in db.list_activity(conn,'scope-test') if e['kind']=='business_tool']
                assert calls==['inspect_dataset','build_plan']
                assert {t['tool'] for t in result.tool_calls}=={'inspect_dataset','build_plan'}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2); conn.close()
