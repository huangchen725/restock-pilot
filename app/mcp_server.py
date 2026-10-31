"""Standalone STDIO bridge: no app imports, dotenv or database access.

Run with Python -I. The backend authorizes each call; tools only calculate
candidates, never save or approve a business plan.
"""
import os
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer('restock-pilot')
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def call(tool, arguments):
    url = os.environ.get('RESTOCK_AGENT_URL', '')
    token = os.environ.get('RESTOCK_AGENT_TOKEN', '')
    if not url.startswith('http://127.0.0.1:') or not token:
        raise ValueError('必须由后端创建有范围和有效期的工具会话')
    with httpx.Client(trust_env=False, timeout=20) as client:
        response = client.post(url + '/tools', headers={'Authorization': 'Bearer ' + token},
                               json={'tool': tool, 'arguments': arguments})
    result = response.json()
    if response.status_code != 200:
        raise ValueError(result.get('error', '工具请求被拒绝'))
    return result


@server.tool(annotations=READ_ONLY, description='检查本次授权任务的数据快照、商品数和已确认口径。')
def inspect_dataset(task_id: str) -> dict:
    return call('inspect_dataset', {'task_id': task_id})


@server.tool(annotations=READ_ONLY, description='按已确认 Policy 计算候选方案，不保存、批准或执行采购。')
def build_plan(task_id: str, policy: dict, dataset_id: str | None = None) -> dict:
    return call('build_plan', {'task_id': task_id, 'policy': policy, 'dataset_id': dataset_id})


@server.tool(annotations=READ_ONLY, description='计算修订候选，完整提供五个 Policy 字段；最终决策校验后才由后端保存。')
def revise_plan(task_id: str, policy: dict, dataset_id: str | None = None) -> dict:
    return call('revise_plan', {'task_id': task_id, 'policy': policy, 'dataset_id': dataset_id})


@server.tool(annotations=READ_ONLY, description='只读本次任务已批准版本的实际采购回执。')
def get_execution_result(task_id: str) -> dict:
    return call('get_execution_result', {'task_id': task_id})


if __name__ == '__main__':
    server.run()
