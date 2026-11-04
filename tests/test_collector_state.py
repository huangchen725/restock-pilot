"""Durable run identity must survive list pagination and late callbacks."""
import pytest
from app import collector, collection, db
from tests.test_browser_collection import config


def test_late_session_audit_preserves_run_identity_past_recent_twenty(tmp_path):
    conn=db.init_db(tmp_path/'jobs.db')
    sid=collection.create_source(conn,config('http://127.0.0.1:8030'))
    run=collector.claim(conn,sid)
    session=collector.CollectorSession(conn,sid,run,tmp_path/'csv')
    session.close()
    try:
        collector.recover(conn)
        for i in range(25):
            db.save_collection_run(conn,f'later-{i}',sid,{'run_id':f'later-{i}','source_id':sid,'status':'failed','events':[]})
        with pytest.raises(ValueError):
            session.call('inspect_source',{'source_id':sid})
        row=conn.execute('SELECT record_json FROM collection_runs WHERE run_id=?',(run,)).fetchone()[0]
        import json
        record=json.loads(row)
        assert record['run_id']==run and record['source_id']==sid and record['status']=='interrupted'
    finally:conn.close()


def test_late_failure_does_not_overwrite_user_interruption(tmp_path):
    conn=db.init_db(tmp_path/'jobs.db')
    sid=collection.create_source(conn,config('http://127.0.0.1:8030'))
    run=collector.claim(conn,sid)
    collector.set_enabled(conn,sid,False)
    collector._finish(conn,sid,run,'failed',error='late model output')
    assert db.collection_runs(conn,sid)[0]['status']=='interrupted'
    assert not db.get_source(conn,sid)['config']['browser']['enabled']
    conn.close()
