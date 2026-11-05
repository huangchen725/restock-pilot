"""Offline test process: scripted tool selection, real page reading and production API."""
import argparse
import uvicorn
from app import collector,main


def scripted(session):
    session.call('inspect_source',{'source_id':session.sid})
    for section in ('sales','inventory','suppliers','inbound'):
        session.call('read_source_section',{'source_id':session.sid,'section':section})
    session.call('finish_source_collection',{'source_id':session.sid})
    return {'model':None,'mode':'test-controller-not-real-model'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,required=True);args=parser.parse_args()
    Original=collector.CollectorWorker
    class TestWorker(Original):
        def __init__(self,conn,root,interval=2):super().__init__(conn,root,interval,model=scripted)
    collector.CollectorWorker=TestWorker
    uvicorn.run(main.app,host='127.0.0.1',port=args.port,log_level='warning')
