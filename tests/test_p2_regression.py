"""Permanent P2 counterexamples. Synthetic keys and isolated local services only."""
import json,os,subprocess,sys,threading,textwrap
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest,httpx,psutil
from app import db,main,runner
from app.agent_runtime import AgentRuntime
from app.business_tools import ToolSession,planner_review
from app.models import Policy,TaskState
from app.rules import build_plan
from app.validation import prepare_snapshot,merged_rows,snapshot_hash
from tests.test_data_contract import tables,contract
from start import redact,preflight,StartupError



@pytest.fixture
def sample():
    snap,errors=prepare_snapshot(*tables(),contract());assert not errors
    policy=Policy(budget_cents=1800,cover_days=7,safety_days=2)
    plan=build_plan('p2-review',1,snapshot_hash(snap),policy,merged_rows(snap),snap)
    return snap,policy,plan

@pytest.mark.parametrize('advice',['需要两件商品','需要１２件商品'])
def test_P2_1_original_quantity_examples_now_rejected(sample,advice):
    with pytest.raises(ValueError):planner_review(sample[2],advice)

def test_P2_1_original_currency_example_now_rejected():
    with pytest.raises(main.AgentStageError):main._check_summary_currency({'summary':'采购金额两美元'})

def test_P2_1_qualitative_advice_still_accepted(sample):
    assert '模型建议：建议人工复核缺货风险。' in planner_review(sample[2],'建议人工复核缺货风险。')

def test_P2_1_numeric_days_must_not_pass_nonnumeric_advice(sample):
    with pytest.raises(ValueError):planner_review(sample[2],'建议增加两天安全库存。')

def test_P2_1_formal_chinese_currency_must_not_bypass_check():
    with pytest.raises(main.AgentStageError):main._check_summary_currency({'summary':'采购金额壹佰美元'})


def capture_guard(tmp_path,sample,call):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            body=json.dumps({'output':[call]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    conn=db.init_db(tmp_path/'guard.db');db.upsert_task(conn,'guard',TaskState.ANALYZING);db.claim_task(conn,'guard','review')
    try:
        with ToolSession(conn,'guard','planner',sample[0],fixed_policy=sample[1]).activate() as session:
            with AgentRuntime(session,upstream_url=f'http://127.0.0.1:{server.server_port}',upstream_key='unused-local-key') as runtime:
                r=httpx.post(runtime.url+'/v1/responses',headers={'Authorization':'Bearer '+runtime.model_token},json={'input':[],'tools':[],'stream':False},trust_env=False,timeout=4)
                assert r.status_code==400
        events=[x['payload'] for x in db.list_activity(conn,'guard') if x['payload'].get('tool')=='runtime_guard']
        assert len(events)==1
        return events[0]
    finally:
        conn.close();server.shutdown();server.server_close();thread.join(timeout=2)

@pytest.mark.parametrize('kind,field',[('function_call','arguments'),('custom_tool_call','input')])
def test_P2_2_audit_preserves_short_input_with_commas(tmp_path,sample,kind,field):
    value='{"command":"echo first,second","flag":true}'
    event=capture_guard(tmp_path,sample,{'type':kind,'name':'exec_command' if kind=='function_call' else 'apply_patch','call_id':'review-call',field:value})
    assert event['arguments'].get(field)==value, {'field':field,'stored':event['arguments'].get(field),'expected_length':len(value)}

def test_P2_2_gateway_audit_redacts_synthetic_configured_secret(tmp_path,sample,monkeypatch):
    sentinel='review-only-secret-93efb71'
    monkeypatch.setenv('RESTOCK_REVIEW_API_KEY',sentinel)
    event=capture_guard(tmp_path,sample,{'type':'custom_tool_call','name':'apply_patch','call_id':'redaction-review','input':sentinel})
    contains=sentinel in json.dumps(event,ensure_ascii=False)
    assert contains is False, 'configured synthetic secret remained in persistent runtime_guard event'

@pytest.mark.parametrize('value',['1234567','abc123'])
def test_P2_7_short_secret_examples_now_redacted(value):
    assert value not in redact('secret='+value,{'RESTOCK_APPROVAL_TOKEN':value})

@pytest.mark.parametrize('value',['0','1'])
def test_P2_7_secret_field_is_not_misclassified_as_feature_flag(value):
    assert '[REDACTED]' in redact('secret='+value,{'RESTOCK_APPROVAL_TOKEN':value})

def test_P2_7_actual_boolean_helper_flag_is_unchanged():
    assert redact('version=1',{'CODEBUDDY_API_KEY_HELPER_DISABLED':'1'})=='version=1'


def test_P2_8_exact_pinned_litellm_version_is_enforced(monkeypatch):
    import importlib.metadata
    original=importlib.metadata.version
    monkeypatch.setattr(importlib.metadata,'version',lambda name:'1.106.999' if name=='litellm' else original(name))
    with pytest.raises(StartupError):preflight([],{},offline=True)


def run_stub(tmp_path,code,timeout=6):
    script=tmp_path/'stub.py';script.write_text(code,encoding='utf-8')
    launcher=tmp_path/'stub.cmd';launcher.write_text('@echo off\n"%LOCAL_TEST_PYTHON%" "%LOCAL_TEST_SCRIPT%" "%LOCAL_TEST_RECORD%"\n',encoding='ascii')
    record=tmp_path/'owned-child.json'
    result=runner.run_codex_task('local review stub',str(tmp_path),timeout_s=timeout,executable=str(launcher),
        child_env={'LOCAL_TEST_PYTHON':sys.executable,'LOCAL_TEST_SCRIPT':str(script),'LOCAL_TEST_RECORD':str(record)})
    return result,record


def test_P2_5_parent_exit_really_terminates_its_owned_child(tmp_path):
    code='''import subprocess,sys,time,json,psutil
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(45)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
identity=psutil.Process(p.pid)
Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid,'created':identity.create_time()}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'parent completed'}}),flush=True)
'''
    owner=None;record=tmp_path/'owned-child.json'
    try:
        result,record=run_stub(tmp_path,code)
        data=json.loads(record.read_text());alive=False
        try:
            candidate=psutil.Process(data['pid'])
            if candidate.create_time()==data['created']:owner=candidate;alive=owner.is_running()
        except psutil.NoSuchProcess:pass
        (tmp_path/'orphan-result.json').write_text(json.dumps({'runner_exit':result.exit_code,'runner_timed_out':result.timed_out,'owned_child_survived':alive,'cleanup_scope':'only recorded PID plus create_time'},indent=2),encoding='utf-8')
        assert alive is False, {'runner_exit':result.exit_code,'child_survived':alive}
    finally:
        if owner is None and record.exists():
            data=json.loads(record.read_text())
            try:
                candidate=psutil.Process(data['pid'])
                if candidate.create_time()==data['created']:owner=candidate
            except psutil.NoSuchProcess:pass
        if owner is not None:
            try:owner.kill();owner.wait(timeout=5)
            except psutil.NoSuchProcess:pass


def test_P2_5_oversized_physical_line_cannot_become_a_valid_tail_event(tmp_path):
    code='''import sys,json
payload=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'tail-from-invalid-line'}}).encode()
sys.stdout.buffer.write(b'x'*(1024*1024)+payload+b'\\n');sys.stdout.buffer.flush()
'''
    result,_=run_stub(tmp_path,code)
    assert result.exit_code!=0 or result.final_text!='tail-from-invalid-line', {'exit':result.exit_code,'final_text':result.final_text}


def test_P2_5_stream_total_limit_still_rejects_overflow(tmp_path):
    result,_=run_stub(tmp_path,"import sys,time\nsys.stdout.buffer.write((b'x'*1023+b'\\n')*9216);sys.stdout.buffer.flush()\ntime.sleep(1)\n")
    assert result.exit_code!=0 and 'exceeded 8 MB' in result.stderr

@pytest.mark.parametrize('advice', ['提高安全库存至叁天', '建议增加半箱', '建议买三打', '建议追加②件', '建议补货Ⅻ件', '建议增加两周覆盖'])
def test_P2_1_numeric_expression_classes_rejected(sample, advice):
    with pytest.raises(ValueError):
        planner_review(sample[2], advice)

@pytest.mark.parametrize('summary', ['金额USD 100', '总额$100', '金额壹佰美分', '金额１００美元'])
def test_P2_1_currency_prefix_and_suffix_rejected(summary):
    with pytest.raises(main.AgentStageError):
        main._check_summary_currency({'summary': summary})

@pytest.mark.parametrize('kind,field',[('function_call','arguments'),('custom_tool_call','input')])
@pytest.mark.parametrize('value', ['{"items":[{"text":"a,b}c\\\"d"}]}', {'nested':['a,b}',{'flag':True}]}])
def test_P2_2_nested_and_escaped_input_is_preserved(tmp_path,sample,kind,field,value):
    event=capture_guard(tmp_path,sample,{'type':kind,'name':'exec_command','call_id':'structured',field:value})
    assert event['arguments'][field]==value
    assert event['operation_id'] and event['stage']=='planner'


def test_P2_2_long_input_has_bounded_preview_and_length(tmp_path,sample):
    value='中文,'*4000
    event=capture_guard(tmp_path,sample,{'type':'custom_tool_call','name':'apply_patch','call_id':'long','input':value})
    args=event['arguments'];meta=args['input_metadata']
    assert meta['truncated'] and meta['original_bytes']==len(json.dumps(value,ensure_ascii=False).encode())
    assert len(args['input'].encode())<=4096


def test_P2_2_unknown_credential_field_is_redacted(tmp_path,sample):
    value=json.dumps({'access_token':'synthetic-unconfigured-secret','command':'echo safe'})
    event=capture_guard(tmp_path,sample,{'type':'custom_tool_call','name':'apply_patch','call_id':'nested-secret','input':value})
    assert 'synthetic-unconfigured-secret' not in json.dumps(event)
    assert json.loads(event['arguments']['input'])['command']=='echo safe'


@pytest.mark.parametrize('value',['0','1','1234567','abc123'])
def test_P2_7_supervisor_persisted_logs_redact_short_credentials(tmp_path,value):
    import io
    from start import Supervisor
    path=tmp_path/'redacted.log'
    supervisor=Supervisor([],{'RESTOCK_APPROVAL_TOKEN':value},tmp_path)
    supervisor._log(io.BytesIO(f'secret={value} repeated={value}\n'.encode()),path)
    assert path.read_text()=='secret=[REDACTED] repeated=[REDACTED]\n'


def test_P2_7_nonsecret_json_and_boolean_switch_preserved():
    text='{"amount":100,"version":1,"flag":0}'
    assert redact(text,{'CODEBUDDY_API_KEY_HELPER_DISABLED':'1'})==text


@pytest.mark.parametrize('length',[65535,65536,65537,1024*1024-1,1024*1024,1024*1024+1])
def test_P2_5_physical_line_boundaries(tmp_path,length):
    code=f"import sys,json\np=json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'ok'}}}}).encode()\nsys.stdout.buffer.write(p+b' '*({length}-len(p)-1)+b'\\n');sys.stdout.buffer.flush()\n"
    result,_=run_stub(tmp_path,code)
    if length<=1024*1024:
        assert result.exit_code==0 and result.final_text=='ok', result.stderr
    else:
        assert result.exit_code!=0 and 'line exceeded' in result.stderr


def test_P2_5_cleanup_failure_is_not_success(tmp_path,monkeypatch):
    original=runner.ProcessScope.stop
    def fail_after_cleanup(self):
        original(self)
        raise OSError('synthetic cleanup failure')
    monkeypatch.setattr(runner.ProcessScope,'stop',fail_after_cleanup)
    result,_=run_stub(tmp_path,"print('done')")
    assert result.exit_code!=0 and 'cleanup failed' in result.stderr


def test_P2_5_job_assignment_failure_does_not_run_unowned_code(tmp_path,monkeypatch):
    from app.processes import WindowsJob
    marker=tmp_path/'must-not-exist'
    monkeypatch.setattr(WindowsJob,'assign',lambda *args: (_ for _ in ()).throw(OSError('assignment denied')))
    result,_=run_stub(tmp_path,f"from pathlib import Path;Path({str(marker)!r}).touch()")
    assert result.exit_code==127 and not marker.exists()

@pytest.mark.parametrize('advice',['建议统一复核口径。','建议逐一检查缺货风险。','建议进一步复核库存与到货信息是否一致。'])
def test_P2_1_qualitative_compounds_are_not_quantities(sample,advice):
    assert advice in planner_review(sample[2],advice)


@pytest.mark.parametrize('advice',['建议统一追加两件。','建议进一步增购叁箱。','建议逐一补充1件。'])
def test_P2_1_qualitative_words_do_not_exempt_quantities(sample,advice):
    with pytest.raises(ValueError):planner_review(sample[2],advice)


def test_P2_1_policy_identifier_is_not_a_currency_claim():
    main._check_summary_currency({'summary':json.dumps({'policy':{'excluded_skus':['USD-001']},'explanation':'按要求排除商品'})})
    with pytest.raises(main.AgentStageError):
        main._check_summary_currency({'summary':json.dumps({'policy':{},'explanation':'采购金额壹佰美元'})})

def test_P2_1_real_overlong_advice_remains_rejected(sample):
    raw=(Path(__file__).parent/'fixtures/planner-rejected-182.txt').read_text(encoding='utf-8')
    assert len(raw.strip())==182
    with pytest.raises(ValueError,match='长度 182'):planner_review(sample[2],raw)


@pytest.mark.parametrize('length,allowed',[(180,True),(181,False)])
def test_P2_1_hard_length_boundary(sample,length,allowed):
    advice=('请复核库存风险。'*30)[:length]
    assert len(advice)==length
    if allowed:assert advice in planner_review(sample[2],advice)
    else:
        with pytest.raises(ValueError):planner_review(sample[2],advice)
