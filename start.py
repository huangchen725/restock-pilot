"""Single-console supervisor: python start.py [start|status] [--offline|--check]."""

import argparse
import importlib.util
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Service:
    name: str
    port: int
    command: list[str]
    health_path: str


class StartupError(RuntimeError):
    pass


def services(offline: bool) -> list[Service]:
    result = []
    if not offline:
        # Use the installed console entry point in this same Python environment.
        entry = (
            "from importlib.metadata import distribution; "
            "next(e for e in distribution('litellm').entry_points "
            "if e.group == 'console_scripts' and e.name == 'litellm').load()()"
        )
        result.append(Service("proxy", 4000, [sys.executable, "-c", entry,
            "--config", str(ROOT / "agent-integration/litellm-config.yaml"),
            "--host", "127.0.0.1", "--port", "4000"], "/v1/models"))
    for name, port, module in (
        ("portal", 8200, "app.procurement_portal:app"),
        ("api", 8100, "app.main:app"),
    ):
        result.append(Service(name, port, [sys.executable, "-m", "uvicorn",
            module, "--host", "127.0.0.1", "--port", str(port)], "/health"))
    result.append(Service("ui",8501,[sys.executable,"-m","streamlit","run",str(ROOT / "ui/app.py"),
        "--server.address","127.0.0.1","--server.port","8501","--server.headless","true",
        "--browser.gatherUsageStats","false"],"/_stcore/health"))
    return result


from app.redaction import redact


def check_port(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise StartupError(f"端口 {port} 已被占用或无法监听；请先停止已有服务。") from exc


def preflight(specs: list[Service], env: dict[str, str], offline: bool, stopping=None) -> None:
    if not offline:
        key = env.get("INTERN_API_KEY", "").strip()
        if not key or key in {"在这里填端砚key", "你的端砚API密钥", "端砚key"}:
            raise StartupError("请在项目根目录 .env 中填写 INTERN_API_KEY，再重新启动。")
    ports={spec.name:spec.port for spec in specs}
    for name,port in (('RESTOCK_API_URL',ports.get('api',8100)),('RESTOCK_LITELLM_URL',ports.get('proxy',4000)),('PROCUREMENT_PORTAL_URL',ports.get('portal',8200))):
        expected=f'http://127.0.0.1:{port}'
        if env.get(name,expected).rstrip('/')!=expected:
            raise StartupError(name+' 与统一入口不一致，请在启动窗口恢复本地连接设置。')
    check_runtime_probe(env,not offline,stopping)
    missing = [name for name in ("uvicorn", "fastapi", "httpx", "psutil", "playwright", "streamlit")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise StartupError("缺少依赖：" + ", ".join(missing) +
                           "；请用当前解释器执行 -m pip install -r requirements.txt。")
    if not offline:
        try:
            entries = distribution("litellm").entry_points
        except PackageNotFoundError as exc:
            raise StartupError("缺少 LiteLLM 代理；请安装 requirements.txt 中的依赖。") from exc
        if not any(e.name == "litellm" and e.group == "console_scripts" for e in entries):
            raise StartupError("当前 LiteLLM 安装缺少代理启动入口。")
        from app.runner import resolve_codex_bin
        try:
            resolve_codex_bin(str(ROOT))
        except OSError as exc:
            raise StartupError(str(exc)) from exc
    for spec in specs:
        check_port(spec.port)


def wait_probe(proc,timeout,stopping=None):
    deadline=time.monotonic()+timeout
    while True:
        if stopping is not None and stopping.is_set(): raise StartupError('启动已取消，正在释放检查进程。')
        remaining=deadline-time.monotonic()
        if remaining<=0: raise subprocess.TimeoutExpired(proc.args,timeout)
        try: return proc.communicate(timeout=min(.2,remaining))
        except subprocess.TimeoutExpired: pass


def finish_probe(owner,proc):
    owner.stop(); owner.close()
    if proc:
        if proc.poll() is None: proc.kill()
        proc.wait(timeout=5)
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()


def check_workbench(spec,env,stopping):
    """Read-only page probe: health endpoints alone do not prove a usable UI."""
    from app.processes import ProcessScope
    probe='''import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-proxy-server'])
    try:
        page=browser.new_page()
        page.goto(sys.argv[1],timeout=30000)
        page.get_by_role('radiogroup',name='资料获取方式',exact=True).wait_for(timeout=30000)
        assert page.locator('[data-testid="stException"]').count()==0
    finally: browser.close()
'''
    owner=ProcessScope(); proc=None
    try:
        proc=owner.spawn([sys.executable,'-c',probe,f'http://127.0.0.1:{spec.port}'],cwd=ROOT,env=env,
                         stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        wait_probe(proc,40,stopping)
        if proc.returncode: raise StartupError('工作台页面未能完成准备。请查看诊断日志，或使用“安装 / 修复环境”。')
    except subprocess.TimeoutExpired as exc:
        raise StartupError('工作台页面准备超时，已取消本次启动。请安装或修复环境后重试。') from exc
    finally: finish_probe(owner,proc)


def check_runtime_probe(env,include_cli=True,stopping=None):
    from app.processes import ProcessScope
    scope=ProcessScope();proc=None
    script="import os,sys;from app.runtime_versions import check_runtime;check_runtime(dict(os.environ),include_cli=sys.argv[1]=='1')"
    try:
        proc=scope.spawn([sys.executable,'-c',script,'1' if include_cli else '0'],cwd=ROOT,env=env,
                         stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        output,error=wait_probe(proc,60,stopping)
        if proc.returncode:
            detail=redact(error.decode('utf-8',errors='replace'),env)[-1500:]
            raise StartupError('运行环境检查失败，请安装/修复环境或恢复本地连接设置。\n'+detail)
    except subprocess.TimeoutExpired as exc:
        raise StartupError('运行环境检查超时，已停止本次检查进程。') from exc
    finally: finish_probe(scope,proc)


def is_healthy(spec: Service, env: dict[str, str]) -> bool:
    import httpx
    headers = {"Authorization": "Bearer " + env.get("INTERN_API_KEY", "")} if spec.name == "proxy" else {}
    try:
        response = httpx.get(f"http://127.0.0.1:{spec.port}{spec.health_path}",
                             headers=headers, timeout=0.8, trust_env=False)
        if response.status_code != 200:
            return False
        if spec.name == "ui":
            return response.text.strip() == "ok"
        payload = response.json()
        if spec.name == "proxy":
            return any(item.get("id") == "intern-s2" for item in payload.get("data", []))
        if spec.name == "api":
            return payload.get("service") == "restock-pilot" and payload.get("status") == "ok"
        if spec.name == 'merchant-demo':
            return (payload.get('service')=='restock-merchant-demo' and payload.get('status')=='ok'
                    and secrets.compare_digest(payload.get('instance',''),env.get('RESTOCK_DEMO_INSTANCE','')))
        return payload.get("status") == "ok"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return False


class Supervisor:
    def __init__(self, specs: list[Service], env: dict[str, str], log_dir: Path,
                 timeout: float = 90):
        self.specs, self.env, self.log_dir, self.timeout = specs, env, log_dir, timeout
        self.children = []
        self.scopes = []
        self.stopping = threading.Event()

    def _log(self, pipe, path):
        # Raw child output is never forwarded to the terminal or written to disk.
        with path.open("a", encoding="utf-8") as output:
            for line in iter(pipe.readline, b""):
                output.write(redact(line.decode("utf-8", errors="replace"), self.env))
                output.flush()
        pipe.close()

    def start(self):
        import psutil
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for spec in self.specs:
            if self.stopping.is_set():
                raise StartupError("启动已取消。")
            # Recheck immediately before launch; never adopt or stop an existing server.
            check_port(spec.port)
            from app.processes import ProcessScope
            scope=ProcessScope();self.scopes.append(scope)
            proc=scope.spawn(spec.command,cwd=ROOT,env=self.env,stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            try:
                owner = psutil.Process(proc.pid)
                owner.create_time()  # Cache identity so later PID reuse is detectable.
            except psutil.NoSuchProcess:
                proc.stdout.close()
                raise StartupError(f"{spec.name} 启动后立即退出。")
            reader = threading.Thread(target=self._log,
                args=(proc.stdout, self.log_dir / f"{spec.name}.log"), daemon=True)
            self.children.append((spec, proc, owner, reader))
            reader.start()
            deadline = time.monotonic() + self.timeout
            while not is_healthy(spec, self.env):
                self._check_children()
                if self.stopping.wait(0.15):
                    raise StartupError("启动已取消。")
                if time.monotonic() >= deadline:
                    raise StartupError(f"{spec.name} 启动超时，请查看脱敏日志。")
            self._check_children()
            print(f"已就绪：{spec.name} http://127.0.0.1:{spec.port}", flush=True)

    def _check_children(self):
        for spec, proc, _, _ in self.children:
            if proc.poll() is not None:
                raise StartupError(f"{spec.name} 已退出（退出码 {proc.returncode}），请查看脱敏日志。")

    def wait(self):
        while not self.stopping.wait(0.5):
            self._check_children()

    def stop(self):
        import psutil
        self.stopping.set()
        # Record identities while the roots still exist. After Job termination,
        # a console helper can briefly outlive its root and no longer be found
        # through owner.children(). Stop is complete only when these exit too.
        owned={}
        for _,proc,owner,_ in self.children:
            try:
                for process in [owner]+owner.children(recursive=True):
                    owned[(process.pid,process.create_time())]=process
            except psutil.NoSuchProcess:
                pass
        for scope in reversed(self.scopes):
            try: scope.stop()
            finally: scope.close()
        self.scopes.clear()
        for _, proc, owner, reader in reversed(self.children):
            # Only this run's process identities and their descendants are eligible.
            try:
                if owner.is_running():
                    tree = owner.children(recursive=True) + [owner]
                    for child in reversed(tree):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs(tree, timeout=3)
                    for child in alive:
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    psutil.wait_procs(alive, timeout=3)
            except psutil.NoSuchProcess:
                pass
            finally:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                reader.join(timeout=2)
        _,alive=psutil.wait_procs(list(owned.values()),timeout=5)
        if alive:
            raise StartupError('本次后台进程尚未全部退出，请查看启动诊断。')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RestockPilot 统一启动；Ctrl+C 停止本次启动的服务。")
    parser.add_argument("action", nargs="?", choices=("start", "status", "stop"), default="start")
    parser.add_argument("--offline", action="store_true", help="关闭模型，仅启动 API 和演示后台")
    parser.add_argument("--check", action="store_true", help="只检查配置、依赖和端口，不启动")
    parser.add_argument('--open-browser',action='store_true')
    args = parser.parse_args(argv)
    if args.action=='stop':
        from launcher_control import request_control,read_instance
        try:
            print(request_control('stop')['message'],flush=True)
            deadline=time.monotonic()+20
            while read_instance() and time.monotonic()<deadline:time.sleep(.2)
            if read_instance():raise StartupError('停止仍在进行，请查看启动窗口。')
            return 0
        except Exception:
            print('停止未完成，请查看启动窗口。',file=sys.stderr);return 1

    try:
        from app.config import load_environment
        load_environment()
    except ImportError:
        print("请先用项目解释器执行 -m pip install -r requirements.txt。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env = os.environ.copy()
    mode = "off" if args.offline else env.get("RESTOCK_AGENT_MODE", "required").lower()
    if mode not in {"off", "required"}:
        print("RESTOCK_AGENT_MODE 只支持 required 或 off。", file=sys.stderr)
        return 1
    offline = mode == "off"
    env.update(RESTOCK_AGENT_MODE=mode, PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env["RESTOCK_APPROVAL_TOKEN"] = env.get("RESTOCK_APPROVAL_TOKEN") or secrets.token_urlsafe(32)
    specs = services(offline)
    supervisor = None
    control = None
    previous_handlers = {}
    try:
        if args.action == "status":
            ready = [(spec, is_healthy(spec, env)) for spec in specs]
            for spec, ok in ready:
                print(f"{spec.name}: {'就绪' if ok else '未就绪'}")
            return 0 if all(ok for _, ok in ready) else 1
        if args.check:
            preflight(specs, env, offline)
            print("配置、依赖与端口检查通过。此检查不验证模型密钥有效性或端到端任务。")
            return 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        supervisor = Supervisor(specs, env, ROOT / "outputs" / "launcher" / stamp)
        from launcher_control import LocalControl
        control=LocalControl(supervisor.stopping,mode)
        env.update(RESTOCK_CONTROL_URL=control.url,RESTOCK_CONTROL_TOKEN=control.token)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: supervisor.stopping.set())
        preflight(specs,env,offline,supervisor.stopping)
        supervisor.start()
        for spec in specs:
            if spec.name=='ui':check_workbench(spec,env,supervisor.stopping)
        control.state='ready';control.message='工作台已就绪'
        if args.open_browser:
            import webbrowser
            webbrowser.open('http://127.0.0.1:8501')
        print("服务已启动。模式：" + ("离线，未启用模型" if offline else "真实模型（密钥与推理尚未验收）"), flush=True)
        print("工作台：http://127.0.0.1:8501；采购演示：http://127.0.0.1:8200", flush=True)
        print(f"日志：{supervisor.log_dir}\n保持本窗口运行，按 Ctrl+C 统一停止。", flush=True)
        supervisor.wait()
        return 0
    except (StartupError, OSError, ImportError) as exc:
        print(redact(str(exc), env), file=sys.stderr)
        if supervisor:
            print(f"日志：{supervisor.log_dir}", file=sys.stderr)
        return 1
    finally:
        if supervisor:
            supervisor.stop()
        if control: control.close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
