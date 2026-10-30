# RestockPilot — 兴智杯大模型智能体关键能力技术创新赛

第三届「兴智杯」全国人工智能创新应用大赛 · 技术创新主题赛 · 方向③开放赋能智能体。
作品：店铺补货与采购申请 Agent。目标流程为上传资料、生成及修改方案、人工批准、采购申请录入、结果核对与交付。

## 工程计划与当前进度

后续开发与验收以 [工程执行计划](ENGINEERING_PLAN.md) 为准（2026-10-30 更新）。
开发 Agent 接手本项目时先阅读根目录 [AGENTS.md](AGENTS.md)。
保留 Codex CLI 作为后台运行时，用户全程使用 RestockPilot 工作台；不要求打开
Codex 桌面界面或操作终端。当前阶段不抽取底座源码、不重写 Agent 循环。

后端已有规则引擎、审批、录单和回读链路；产品工作台、上传、差异展示和导出仍待实现。
业务 MCP 尚未接入，三阶段调用不代表已经实现多智能体协作。

本机核对：启动适配后普通回归 **36 项通过、6 项浏览器测试跳过**；此前单独启用
浏览器测试时，正常录单与幂等 **1 项通过**，故障注入 **5 项失败**。账本完整性
检查仍需修复；迁移环境的真实书生完整链路尚未复验。

执行顺序：运行与业务契约 → 产品工作台和 Agent 工具并行 → 异常恢复与审计 →
真实模型验收和比赛交付。工作台是必交项，详细分工、内部日期和完成标准见计划。

## 截止时间

**2026-11-05 23:59:59 报名 + 作品提交（ZIP + ≤5 分钟视频链接 + 应用体验链接）**，只报名不交件无效。

## 历史验收记录（2026-10-29 原开发环境）

以下保留原环境的阶段记录；当前迁移状态及产品完成度以上节为准。

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

- 在项目根目录创建 `.venv`，使用同一个虚拟环境安装依赖、运行服务和测试。
- 测试：`.\.venv\Scripts\python.exe -m pytest tests/ -v`
- 服务：`.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8100`
- 样例数据：`.\.venv\Scripts\python.exe scripts/generate_data.py`（30 SKU，stable/volatile/zerodays/
  newitem 四种销量模式，覆盖置信分层测试）

## 快速启动

以下命令使用 PowerShell，在项目根目录执行。Linux/macOS 使用 `.venv/bin/python`
及对应 shell 的环境变量语法。

```powershell
# 1. 首次准备环境（已创建 .venv 时跳过第一行）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

# 2. 确认当前终端能够找到 Codex CLI
codex --version
# 特殊安装位置可选配；只填程序位置，不附带 exec 等参数：
# $env:RESTOCK_CODEX_BIN = '.\node_modules\.bin\codex.cmd'

# 3. 代理终端：注入密钥后启动，保持运行
$env:INTERN_API_KEY = '端砚key'
.\.venv\Scripts\litellm.exe --config agent-integration/litellm-config.yaml --port 4000

# 4. 采购后台终端（另开终端，进入项目根目录）
.\.venv\Scripts\python.exe -m uvicorn app.procurement_portal:app --port 8200

# 5. 主服务终端（另开终端，进入项目根目录）
$env:INTERN_API_KEY = '端砚key'
$env:RESTOCK_AGENT_MODE = 'required'
$env:PROCUREMENT_PORTAL_URL = 'http://127.0.0.1:8200'
# 如果需要 RESTOCK_CODEX_BIN，也在这个终端设置
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8100
```

runner 每次启动时优先读取 `RESTOCK_CODEX_BIN`，未设置或留空时从主服务进程的
`PATH` 查找 `codex`。支持程序名、绝对路径和项目根目录下的相对路径；Windows
通过 `PATHEXT` 查找 `.exe` / `.cmd` 等可执行文件。显式配置无效时直接报错，
不会静默回退到其他安装版本。

参考 `.env.example` 配置环境变量。仅把值写入 `.env` 不会自动加载到 runner；
请通过启动终端或进程管理工具注入。项目专用 `CODEX_HOME` 由 runner 仅在子进程中
设置为 `agent-integration/`，不需要修改个人环境。

真实浏览器验收（自动启动隔离采购后台，重复执行两次验证不生成重复单）：

```powershell
$env:RUN_PLAYWRIGHT_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_playwright_e2e.py -q
```

双服务全链路冒烟（off 模式，不需要 LLM key）：

```powershell
# 终端 1：采购后台 8200；终端 2：主服务 8100（RESTOCK_AGENT_MODE=off
# PROCUREMENT_PORTAL_URL=http://127.0.0.1:8200），然后：
.\.venv\Scripts\python.exe scripts/run_smoke_e2e.py
```

> ⚠️ 沙箱代理坑：本机环境存在 `HTTP_PROXY` 指向沙箱代理，httpx 默认读它，
> 会对本地服务产生诡异的 404/串响应。所有指向本地服务的 httpx 调用必须
> `trust_env=False`；Playwright 启动参数已带 `--no-proxy-server`。

## 目录结构

| 路径 | 内容 |
|---|---|
| `app/` | models / rules / state_machine / db / validation / runner / prompts / main；Step 3 新增 `execution.py`（Playwright 固定适配器）与 `procurement_portal.py`（三页采购后台） |
| `tests/` | 规则、审批 hash、runner、幂等、回读、批准版本与审计测试；Chromium E2E 和故障注入需显式启用 |
| `ENGINEERING_PLAN.md` | 当前工程决策、M0–M4 任务、分工、排期与验收标准 |
| `data/` | 30 SKU 样例三件套 CSV |
| `scripts/` | generate_data.py（30 SKU 样例）+ run_smoke_e2e.py（双服务全链路冒烟，含 portal 落单核对） |
| `agent-integration/` | Codex config.toml / LiteLLM 配置 / SETUP.md 操作手册（含 6 条踩坑记录） |
| `day1-test/` | Day 1 验收产物（JSONL 事件流 + result.json） |
| `test_intern_function_calling.py` | 书生端点工具调用实测脚本（换端点/key 复用） |
| `codex-main.tar.gz` | Codex CLI 源码包（参考用，需研究时解压） |

## 当前接入约定

- 现有链路使用 Codex 的 Responses 接口，经 LiteLLM 转换到端砚 Chat Completions；保留这条已在原环境验证的接入路径。
- 原环境记录端砚目标模型不支持 `/v1/responses`；固定版本及端点后的迁移复验按工程计划执行。
- 当前配置使用 `danger-full-access`，来源于原环境的启动排障。这不构成产品权限隔离；独立工作目录、受限业务工具与人工批准边界是 M2 的待验收事项。
- TOML 顶层键必须写在任何 `[table]` 段之前，否则静默失效

## 安全注意

两把 API key（挑战杯 + 端砚）都曾在聊天中明文暴露——**11/5 提交前在控制台吊销重生成**；真实 key 只放 `.env`（已 gitignore）。
