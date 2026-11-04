"""Real locked CLI, local Responses fault injector, and real external Chromium."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from app import collector, collection, db, runner
from app.agent_runtime import AgentRuntime
from tests.test_agent_runtime import event_stream
from tests.test_browser_collection import portal, source_db, assert_snapshot_matches_portal

pytestmark=pytest.mark.skipif(os.getenv('RUN_AGENT_BOUNDARY_E2E')!='1',reason='real CLI boundary opt-in')


@pytest.mark.parametrize('attack',[None,'foreign_tool','after_finish'])
def test_cli_browser_allowed_and_hostile_output(tmp_path,portal,attack):
    target,url=portal; conn,sid=source_db(tmp_path,url)
    requests=[]
    calls=[('inspect_source',{}),('read_source_section',{'section':'inventory'}),
           ('read_source_product',{'sku':'SKU-1014'}),
           ('read_source_section',{'section':'sales'}),('read_source_section',{'section':'suppliers'}),
           ('read_source_section',{'section':'inbound'}),('finish_source_collection',{})]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload); index=len(requests)
            if (attack=='foreign_tool' and index==4) or (attack=='after_finish' and index==8):
                item={'type':'function_call','id':'bad','call_id':'bad','name':'exec_command','arguments':'{"cmd":"echo forbidden"}'}
            elif index<=len(calls):
                name,params=calls[index-1]
                item={'type':'function_call','id':f'fc{index}','call_id':f'c{index}',
                    'name':'mcp__restock__'+name,'arguments':json.dumps({'source_id':sid,**params})}
            else:
                item={'type':'message','id':'done','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':'{"collection_complete":true}','annotations':[]}]}
            data=event_stream(item,index)
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def fake_model(session):
        with AgentRuntime(session,upstream_url=f'http://127.0.0.1:{server.server_port}',upstream_key='test-only') as runtime:
            result=runner.run_codex_task('只使用授权工具读取模拟网页。',str(runtime.workdir),timeout_s=100,
                codex_home=str(runtime.home),child_env=runtime.env,
                executable=runner.resolve_codex_bin(str(Path(__file__).resolve().parents[1])))
        session._audit('test_model_response',{},result={'exit_code':result.exit_code,'timed_out':result.timed_out,
            'events':[e.raw for e in result.events],'stderr':result.stderr})
        assert not result.timed_out
        if result.exit_code: raise ValueError('模型输出越权被拒绝')
        assert result.final_text.strip()=='{"collection_complete":true}'
        return {'model':'local-fault-injector','mode':'real-cli-test'}
    try:
        run=collector.execute(conn,tmp_path/'csv',sid,collector.claim(conn,sid),fake_model)
        assert requests
        assert all({t['name'] for t in req.get('tools',[])}<={'mcp__restock__'+t for t in collector.TOOLS} for req in requests)
        assert target.write_log==[]
        if attack:
            assert run['status']=='failed',run
            assert any(e['tool']=='runtime_guard' and e['error'] for e in run['events'])
            assert db.get_source(conn,sid)['state']['latest_observed_dataset_id'] is None
        else:
            assert run['status']=='succeeded',run
            snapshot=collection.DatasetSnapshot.model_validate(db.get_source(conn,sid)['state']['pending_snapshot'])
            assert_snapshot_matches_portal(snapshot,target,{'SKU-1001','SKU-1003','SKU-1004'})
            success=[e for e in run['events'] if e['result'] is not None and not e['error']]
            assert any(e['tool']=='finish_source_collection' for e in success)
            assert any(e['tool']=='read_source_product' and '999' in e['result']['remark'] for e in success)
        assert not db.list_publications(conn,sid)
        evidence=Path('outputs/m5-collector-cli'); evidence.mkdir(exist_ok=True)
        (evidence/(str(attack)+'.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        server.shutdown();server.server_close();thread.join(2);conn.close()
