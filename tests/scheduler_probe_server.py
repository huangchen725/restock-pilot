"""Subprocess-only crash probe. No product configuration or business fixtures."""
import os,time
from pathlib import Path
import uvicorn
from app import main


def run():
    original=main._dispatch
    def dispatch(task,phase,work,prefer,publishes_plan=False):
        if phase!='自动检查补货':return original(task,phase,work,prefer,publishes_plan)
        point=os.environ['RESTOCK_TEST_CRASH_POINT']
        def held_work():
            if point=='before':hold()
            value=work()
            if point=='after':hold()
            return value
        return original(task,phase,held_work,prefer,publishes_plan)
    def hold():
        Path(os.environ['RESTOCK_TEST_REACHED']).write_text('reached',encoding='utf-8')
        # Parent forcibly stops this owned API process. Finite fallback avoids a stranded probe.
        end=time.monotonic()+90
        while time.monotonic()<end:time.sleep(.1)
        raise RuntimeError('crash probe parent did not stop owned process')
    main._dispatch=dispatch
    uvicorn.run(main.app,host='127.0.0.1',port=int(os.environ['RESTOCK_TEST_API_PORT']),log_level='warning')


if __name__=='__main__':run()
