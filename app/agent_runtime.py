"""Mandatory model-tool gate and short-lived broker around the retained CLI.

This is capability isolation, not an OS account/container boundary: all model
replies are validated before Codex sees them, and only scoped business MCP calls
are allowed. Read-only Codex settings add defense in depth. Neither child gets
the approval token, database configuration or upstream API key.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 4 * 1024 * 1024
PREFIX = 'mcp__restock__'


def restrict_request(payload, allowed):
    payload = dict(payload)
    tools = []
    for tool in payload.get('tools', []):
        if tool.get('type') == 'function' and tool.get('name') in allowed:
            tools.append(tool)
        elif tool.get('type') == 'namespace' and tool.get('name') == 'mcp__restock':
            for child in tool.get('tools', []):
                name = PREFIX + child.get('name', '')
                if child.get('type') == 'function' and name in allowed:
                    tools.append({**child, 'name': name})
    payload['tools'] = tools
    # LiteLLM's Chat Completions upstream uses flat function names. Preserve the
    # CLI's namespace routing separately; it is never controlled by the model.
    payload = json.loads(json.dumps(payload))
    def flatten(value):
        if isinstance(value, dict):
            if value.get('type') == 'function_call' and value.get('namespace') == 'mcp__restock':
                value['name'] = PREFIX + value['name']
                del value['namespace']
            for child in value.values(): flatten(child)
        elif isinstance(value, list):
            for child in value: flatten(child)
    flatten(payload.get('input', []))
    choice = payload.get('tool_choice')
    if isinstance(choice, dict) and choice.get('name') not in allowed:
        raise ValueError('请求包含未授权工具选择')
    return payload


def validate_model_output(content, content_type, allowed):
    """Buffer and check the entire reply; never release an unchecked tool call."""
    if len(content) > MAX_BYTES:
        raise ValueError('模型输出超过上限')
    def inspect(value):
        if isinstance(value, dict):
            kind = value.get('type', '')
            if kind == 'function_call':
                if value.get('name') not in allowed or value.get('namespace'):
                    raise ValueError('模型请求了未授权的工具')
            elif isinstance(kind, str) and (kind.endswith('_call') or kind == 'tool_call'):
                raise ValueError('禁止通用命令、文件、网络及其他工具调用')
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    if 'text/event-stream' in content_type:
        seen = False
        for line in content.decode('utf-8').splitlines():
            if line.startswith('data:'):
                value = line[5:].strip()
                if value and value != '[DONE]':
                    inspect(json.loads(value)); seen = True
        if not seen:
            raise ValueError('模型未返回有效事件')
    else:
        inspect(json.loads(content))


def namespace_reply(content, content_type, mapping):
    """Restore only namespaces advertised by the trusted CLI on this request."""
    def convert(value):
        if isinstance(value, dict):
            if value.get('type') == 'function_call' and value.get('name') in mapping:
                value['namespace'], value['name'] = mapping[value['name']]
            for child in value.values(): convert(child)
        elif isinstance(value, list):
            for child in value: convert(child)
        return value
    if 'text/event-stream' in content_type:
        lines = []
        for line in content.decode('utf-8').splitlines():
            if line.startswith('data:') and line[5:].strip() not in ('', '[DONE]'):
                line = 'data: ' + json.dumps(convert(json.loads(line[5:].strip())))
            lines.append(line)
        return ('\n'.join(lines)+'\n\n').encode()
    return json.dumps(convert(json.loads(content))).encode()


class AgentRuntime:
    def __init__(self, session, upstream_url=None, upstream_key=None):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.allowed = {PREFIX + t for t in session.allowed} if session else set()
        self.upstream = (upstream_url or os.getenv('RESTOCK_LITELLM_URL', 'http://127.0.0.1:4000')).rstrip('/')
        parsed = urlparse(self.upstream)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/v1'):
            raise ValueError('模型网关必须指向本地 LiteLLM')
        self.key = upstream_key if upstream_key is not None else os.getenv('INTERN_API_KEY', '')
        self.client = httpx.Client(trust_env=False, timeout=float(os.getenv('RESTOCK_AGENT_TIMEOUT_S', '600')))
        self.closed = False

    def __enter__(self):
        runtime = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value, content_type='application/json'):
                data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self):
                return not runtime.closed and secrets.compare_digest(
                    self.headers.get('Authorization', ''), 'Bearer ' + runtime.token)

            def do_GET(self):
                if not self.authorized():
                    return self.reply(403, {'error': '授权无效'})
                if self.path == '/v1/models':
                    return self.reply(200, {'data': [{'id': 'intern-s2', 'object': 'model'}], 'object': 'list'})
                self.reply(404, {'error': '不支持的入口'})

            def do_POST(self):
                if not self.authorized():
                    return self.reply(403, {'error': '授权无效'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BYTES:
                        raise ValueError('请求大小不合法')
                    payload = json.loads(self.rfile.read(size))
                    if self.path == '/tools':
                        if runtime.session is None or set(payload) != {'tool', 'arguments'}:
                            raise ValueError('没有业务工具授权')
                        return self.reply(200, runtime.session.call(payload['tool'], payload['arguments']))
                    if self.path != '/v1/responses':
                        return self.reply(404, {'error': '不支持的入口'})
                    mapping = {PREFIX + t['name']: ('mcp__restock', t['name'])
                        for namespace in payload.get('tools', [])
                        if namespace.get('type') == 'namespace' and namespace.get('name') == 'mcp__restock'
                        for t in namespace.get('tools', []) if PREFIX + t.get('name','') in runtime.allowed}
                    payload = restrict_request(payload, runtime.allowed)
                    payload['model'] = 'intern-s2'
                    endpoint = runtime.upstream + ('/responses' if runtime.upstream.endswith('/v1') else '/v1/responses')
                    with runtime.client.stream('POST', endpoint, json=payload,
                            headers={'Authorization': 'Bearer ' + runtime.key}) as response:
                        if response.status_code >= 400:
                            return self.reply(502, {'error': {'message': '书生模型网关请求失败', 'type': 'upstream_error'}})
                        chunks = bytearray()
                        for chunk in response.iter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > MAX_BYTES:
                                raise ValueError('模型输出超过上限')
                        content_type = response.headers.get('content-type', 'application/json')
                    validate_model_output(bytes(chunks), content_type, runtime.allowed)
                    return self.reply(200, namespace_reply(bytes(chunks), content_type, mapping), content_type)
                except (ValueError, TypeError, KeyError) as exc:
                    if runtime.session:
                        runtime.session._audit('runtime_guard', {}, error=str(exc))
                    self.reply(400, {'error': {'message': str(exc), 'type': 'boundary_rejection'}})
                except httpx.HTTPError:
                    self.reply(502, {'error': {'message': '模型网关不可达或超时', 'type': 'upstream_error'}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.temp = tempfile.TemporaryDirectory(prefix='restock-agent-')
        base = Path(self.temp.name)
        self.workdir, self.home = base / 'work', base / 'codex'
        self.workdir.mkdir(); self.home.mkdir()
        q = json.dumps  # JSON string quoting is TOML-compatible for these values.
        config = f'''model = "intern-s2"
model_provider = "restock_guard"
model_context_window = 262144
sandbox_mode = "read-only"
approval_policy = "never"
project_doc_max_bytes = 0
web_search = "disabled"
[features]
shell_tool = false
view_image = false
browser_use = false
computer_use = false
apps = false
plugins = false
code_mode = false
[model_providers.restock_guard]
name = "RestockPilot scoped gateway"
base_url = {q(self.url + '/v1')}
wire_api = "responses"
env_key = "RESTOCK_MODEL_TOKEN"
request_max_retries = 0
stream_max_retries = 0
'''
        if self.session:
            config += f'''[mcp_servers.restock]
command = {q(sys.executable)}
args = ["-I", {q(str(ROOT / 'app/mcp_server.py'))}]
cwd = {q(str(self.workdir))}
required = true
env_vars = ["RESTOCK_AGENT_URL", "RESTOCK_AGENT_TOKEN"]
'''
        (self.home / 'config.toml').write_text(config, encoding='utf-8')
        self.env = {'RESTOCK_MODEL_TOKEN': self.token, 'RESTOCK_AGENT_URL': self.url,
                    'RESTOCK_AGENT_TOKEN': self.token}
        return self

    def __exit__(self, *args):
        self.closed = True
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=2)
        self.client.close()
        self.temp.cleanup()
