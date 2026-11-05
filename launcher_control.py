"""Authenticated loopback control for this project instance, never PID killing."""
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def lifecycle_lock_path():
    return Path(os.environ.get('RESTOCK_LIFECYCLE_LOCK', ROOT/'.restock/setup.lock'))


class LifecycleLock:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=None
        try:
            self.stream=path.open('a+b')
            if os.name=='nt':
                import msvcrt
                self.stream.seek(0);self.stream.write(b'0');self.stream.flush();self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise OSError('项目正在安装或运行，请使用现有启动窗口，或停止后再安装。') from exc

    def close(self):
        if self.stream:
            self.stream.close();self.stream=None


def instance_path():
    return Path(os.environ.get('RESTOCK_INSTANCE_FILE', ROOT/'outputs/launcher-instance.json'))


def read_instance():
    try:
        import psutil
    except ImportError:
        return None
    try:
        data=json.loads(instance_path().read_text(encoding='utf-8'))
        url=urlparse(data['url'])
        if (url.scheme!='http' or url.hostname!='127.0.0.1' or url.path or url.query or url.fragment
                or url.username or url.password or not url.port):
            return None
        process=psutil.Process(data['pid'])
        if not process.is_running() or abs(process.create_time()-data['created'])>.01:
            return None
        return data
    except (OSError, ValueError, KeyError, psutil.Error):
        return None


def request_control(action='status', instance=None):
    import httpx
    if action not in ('status','stop'): raise ValueError('不支持的控制操作')
    data=instance or read_instance()
    if not data:
        return {'running':False, 'message':'服务已停止。'}
    with httpx.Client(trust_env=False,timeout=3) as client:
        r=client.request('POST' if action=='stop' else 'GET',data['url']+'/'+action,
                         headers={'X-Restock-Control':data['token']})
        r.raise_for_status()
        return r.json()


class LocalControl:
    def __init__(self, stopping, mode):
        import psutil
        self.path=instance_path()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # Same kernel-held file as scripts/setup.ps1. Stale JSON/PID is never an install lock.
        self.lock=LifecycleLock(lifecycle_lock_path())
        self.token=secrets.token_urlsafe(32)
        self.state='starting'
        self.message='正在检查并启动服务'
        self.mode=mode
        control=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def _handle(self):
                if not secrets.compare_digest(self.headers.get('X-Restock-Control',''),control.token):
                    self.send_error(403); return
                if self.path not in ('/status','/stop') or (self.path=='/stop' and self.command!='POST'):
                    self.send_error(404); return
                payload={'running':True,'state':control.state,'message':control.message,'mode':mode}
                if self.path=='/stop':
                    control.state='stopping'; control.message='正在停止服务并释放后台进程'
                    payload.update(state='stopping',message=control.message)
                    # Return before the UI/API is closed so the user receives acknowledgement.
                    threading.Timer(.7,stopping.set).start()
                data=json.dumps(payload,ensure_ascii=False).encode('utf-8')
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            do_GET=_handle
            do_POST=_handle
        try:
            self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            self.server.daemon_threads=True
            self.url=f'http://127.0.0.1:{self.server.server_port}'
            self.data={'pid':os.getpid(),'created':psutil.Process().create_time(),
                       'url':self.url,'token':self.token,'mode':mode}
            temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.data),encoding='utf-8'); temp.replace(self.path)
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
            self.thread.start()
        except BaseException:
            self.lock.close()
            raise

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        try:
            current=json.loads(self.path.read_text(encoding='utf-8'))
            if current.get('token')==self.token: self.path.unlink()
        except (OSError,ValueError): pass
        self.lock.close()
