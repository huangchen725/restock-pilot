"""Human-started local demo page using the existing owned-process supervisor."""
import os,secrets,sys,threading
from pathlib import Path
from app.demo_profile import profile

ROOT=Path(__file__).resolve().parents[1]


class DemoSite:
    def __init__(self):
        self.port=int(os.getenv('RESTOCK_DEMO_PORT','8030'))
        if not 1<=self.port<=65535:raise ValueError('测试页面端口无效')
        self.lock=threading.RLock();self.supervisor=None
        self.instance=secrets.token_urlsafe(24)

    def status(self):
        with self.lock:
            supervisor=self.supervisor
            running=bool(supervisor and supervisor.children and supervisor.children[0][1].poll() is None)
            if running:
                from start import is_healthy
                running=is_healthy(supervisor.specs[0],supervisor.env)
            return {'running':running,'url':f'http://127.0.0.1:{self.port}',
                    'pid':supervisor.children[0][1].pid if running else None,
                    'profile':profile(f'http://127.0.0.1:{self.port}')}

    def start(self):
        with self.lock:
            if self.status()['running']:return self.status()
            self.stop()
            from start import Service,Supervisor,check_port
            check_port(self.port) # Never adopt or stop another process using the port.
            env={**os.environ,'RESTOCK_DEMO_INSTANCE':self.instance,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
            for key in ('INTERN_API_KEY','RESTOCK_APPROVAL_TOKEN','RESTOCK_CONTROL_TOKEN'):
                env.pop(key,None)
            spec=Service('merchant-demo',self.port,[sys.executable,'-m','app.demo_site_server','--port',str(self.port)],'/restock-demo-health')
            logs=Path(os.getenv('RESTOCK_DEMO_LOG_DIR',str(ROOT/'outputs/demo-page')))
            supervisor=Supervisor([spec],env,logs,timeout=10)
            try:supervisor.start()
            except BaseException:
                supervisor.stop();raise
            self.supervisor=supervisor
            return self.status()

    def stop(self):
        with self.lock:
            if self.supervisor:
                self.supervisor.stop();self.supervisor=None
