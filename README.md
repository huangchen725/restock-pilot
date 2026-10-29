# RestockPilot — 兴智杯大模型智能体关键能力技术创新赛

第三届「兴智杯」全国人工智能创新应用大赛 · 技术创新主题赛 · 方向③开放赋能智能体。
作品：店铺补货与采购申请 Agent（多智能体分工 + MCP 业务工具 + 审批状态机 + 独立校验层）。

## 截止时间

**2026-11-05 23:59:59 报名 + 作品提交（ZIP + ≤5 分钟视频链接 + 应用体验链接）**，只报名不交件无效。

## 当前状态（2026-10-29）

✅ 官网报名完成
✅ 书生·端砚 function calling 实测通过（T1/T2/T3）
✅ Codex→LiteLLM→端砚 全链路 Day 1 验收通过（含真实工具执行 + 文件写入）
✅ **Step 2 骨架完成**：规则引擎（纯函数+整数分）、状态机（含审批撤销路径）、
   SQLite 持久层（WAL）、数据校验（GBK 探测）、runner.py（codex exec 封装）、
   FastAPI 生命周期端点
✅ **Step 3 代码与单测完成**：main.py 已接 runner.run_codex_task（Planner/Reviser/
   Executor），新增 Playwright 录单适配器、采购后台三页、详情回读核对、
   external_ref 幂等与异常后先查后交；**25 项单测通过**
✅ **Step 3 验收全部完成（2026-10-29）**：
   - 离线链路：真实 Chromium E2E 两遍全绿（幂等 already_exists）；真实进程
     create→approve→execute→COMPLETED，回读零偏差；COMPLETED 重执行正确 409
   - **真实 Agent 三阶段冒烟通过**（RESTOCK_AGENT_MODE=required）：
     Planner 169s 产出高质量复核摘要（正确引用 3 行下单/19 SKU 跳过/low 置信度
     提示人工复核）；Reviser 56s 正确解析自然语言指令输出合法 policy JSON 生成
     v2 方案；Executor 真实录单 2/2 created
   - 期间修复：runner 长 prompt 走 argv 触发 cmd.exe 8191 上限（改 stdin）+
     stderr GBK 解码；采购后台补 GET /api/purchase-orders 列表端点；
     沙箱代理劫持本地 httpx（trust_env=False / --no-proxy-server）

## 开发环境

- Python：`C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （注意：裸 `python` 与 `pip` 指向不同解释器，跑服务/测试一律用完整路径）
- 测试：项目根目录跑 `<venv-python> -m pytest tests/ -v`
- 服务：`<venv-python> -m uvicorn app.main:app --port 8100`
- 样例数据：`python scripts/generate_data.py`（30 SKU，stable/volatile/zerodays/
  newitem 四种销量模式，覆盖置信分层测试）

## 快速启动

```bash
# 1. 配置环境变量（参考 .env.example，key 不要进 git）
export INTERN_API_KEY=端砚key

# 2. 启动 LiteLLM 代理（4000 端口，保持运行）
litellm --config agent-integration/litellm-config.yaml --port 4000

# 3. 安装 Chromium（首次）
"C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m playwright install chromium

# 4. 启动自建采购后台（8200，另开终端）
"C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn app.procurement_portal:app --port 8200

# 5. 启动主服务（8100，另开终端）
export RESTOCK_AGENT_MODE=required
export PROCUREMENT_PORTAL_URL=http://127.0.0.1:8200
"C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn app.main:app --port 8100

# 6. 跑 agent 任务（Bash；也可直接调用主服务 API）
export CODEX_HOME=$(pwd)/agent-integration
codex exec --json --skip-git-repo-check "任务描述" < /dev/null
```

真实浏览器验收（自动启动隔离采购后台，重复执行两次验证不生成重复单）：

```bash
RUN_PLAYWRIGHT_E2E=1 "C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest tests/test_playwright_e2e.py -q
```

双服务全链路冒烟（off 模式，不需要 LLM key）：

```bash
# 终端 1：采购后台 8200；终端 2：主服务 8100（RESTOCK_AGENT_MODE=off
# PROCUREMENT_PORTAL_URL=http://127.0.0.1:8200），然后：
"C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe" scripts/run_smoke_e2e.py
```

> ⚠️ 沙箱代理坑：本机环境存在 `HTTP_PROXY` 指向沙箱代理，httpx 默认读它，
> 会对本地服务产生诡异的 404/串响应。所有指向本地服务的 httpx 调用必须
> `trust_env=False`；Playwright 启动参数已带 `--no-proxy-server`。

## 目录结构

| 路径 | 内容 |
|---|---|
| `app/` | models / rules / state_machine / db / validation / runner / prompts / main；Step 3 新增 `execution.py`（Playwright 固定适配器）与 `procurement_portal.py`（三页采购后台） |
| `tests/` | 25 项单测：规则、审批 hash、真实 runner 接线、幂等、超时回查、回读不一致、批准版本绑定；另有默认跳过的 Chromium E2E |
| `data/` | 30 SKU 样例三件套 CSV |
| `scripts/` | generate_data.py（30 SKU 样例）+ run_smoke_e2e.py（双服务全链路冒烟，含 portal 落单核对） |
| `agent-integration/` | Codex config.toml / LiteLLM 配置 / SETUP.md 操作手册（含 6 条踩坑记录） |
| `day1-test/` | Day 1 验收产物（JSONL 事件流 + result.json） |
| `test_intern_function_calling.py` | 书生端点工具调用实测脚本（换端点/key 复用） |
| `codex-main.tar.gz` | Codex CLI 源码包（参考用，需研究时解压） |

## 关键架构事实

- 最新版 Codex 只说 Responses 协议（`WireApi` 枚举无 chat）→ **LiteLLM 转换层必选**
- 端砚只有 Chat Completions（`/v1/responses` 明确不支持）
- Windows 下 Codex 的 `workspace-write` 沙箱拦截所有 shell → 用 `danger-full-access`
- TOML 顶层键必须写在任何 `[table]` 段之前，否则静默失效

## 安全注意

两把 API key（挑战杯 + 端砚）都曾在聊天中明文暴露——**11/5 提交前在控制台吊销重生成**；真实 key 只放 `.env`（已 gitignore）。
