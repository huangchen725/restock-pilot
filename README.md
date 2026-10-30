# RestockPilot — 兴智杯大模型智能体关键能力技术创新赛

第三届「兴智杯」全国人工智能创新应用大赛 · 技术创新主题赛 · 方向③开放赋能智能体。
作品：店铺补货与采购申请 Agent。目标流程为上传资料、生成及修改方案、人工批准、采购申请录入、结果核对与交付。

## 工程计划与当前进度

后续开发与验收以 [工程执行计划](ENGINEERING_PLAN.md) 为准（2026-10-30 更新）。
开发 Agent 接手本项目时先阅读根目录 [AGENTS.md](AGENTS.md)。
保留 Codex CLI 作为后台运行时，用户全程使用 RestockPilot 工作台；不要求打开
Codex 桌面界面或操作终端。当前阶段不抽取底座源码、不重写 Agent 循环。

M1 产品工作台已接通三份 CSV 上传、口径检查、方案展示、修改差异、人工批准、
演示采购录单与回读，以及 CSV/HTML 导出。长任务在后台运行，页面可刷新恢复同一任务。
业务 MCP 尚未接入，三阶段调用不代表已经实现多智能体协作。

M0 业务规则与数据契约已完成本轮开发，详见 [DATA_CONTRACT.md](DATA_CONTRACT.md)：
日期窗口、口径确认、合格在途抵扣、预算部分采购、风险并列展示及持久化快照修订。
新方案采用 schema 2；旧方案保持读取、批准指纹和执行兼容，没有快照的旧方案需明确应用新数据或新建任务后修订。

本轮工作台真实浏览器正常流程已通过：上传、修正口径、生成、改预算、查看差异、
重新批准、录单、刷新和下载；另用 30 SKU 样例通过独立冒烟核对。
新旧方案浏览器正常流程 **2 项通过**；原有故障注入 **5 项仍失败**，留在 M3。
统一启动 **13 项通过**，包含工作台和代理。普通回归 **138 项通过、10 项默认跳过**
（工作台浏览器 1 项、执行浏览器 7 项、启动集成 2 项；均已另行显式运行）。
本轮未调用真实模型；自然语言采用模拟输出测试，真实语义、耗时及工作台模型闭环仍需 M2/M4 复验。
现有演示服务未重启；本地证据在 `outputs/m1-workbench/`，不提交 Git。

以下为 M0 规则更新前的真实模型验收记录，不能代替新版模型输出复验：

2026-10-30 迁移环境真实书生后端链路已通过一次验证（任务 `7af1cc26`，34 项断言）：
预算从 5,000 元改为 3,000 元，方案总额从 4,903.08 元变为 2,576.04 元；旧批准失效，
重新批准后真实 Chromium 创建 2 笔申请，独立回读一致，重复执行返回 409 且无新增单据。
Planner/Reviser/Executor 均由 `intern-s2` 实际响应，耗时分别为 472.5/8.1/94.9 秒。
Planner 摘要有预算分配与在途抵扣口径的解释偏差，需修正；这次通过的是后端正常流程，
不等于产品界面、业务 MCP、故障恢复或重复稳定性验收通过。三阶段均未调用业务工具。
本地脱敏证据保存在 `outputs/real-chain-20261030-134716/`（不提交 Git）。

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

在项目根目录执行以下 PowerShell 命令。Linux/macOS 使用 `.venv/bin/python`。

```powershell
# 首次准备依赖；已有 .venv 时跳过创建
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

# 首次创建配置；不覆盖已经填写的 .env
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑根目录 `.env`，填写 `INTERN_API_KEY`。真实模式还需要安装并能找到 Codex CLI。
之后每次只需运行：

```powershell
.\.venv\Scripts\python.exe start.py
```

入口自动读取 `.env`，检查依赖、密钥是否填写和端口占用，再依次启动 LiteLLM
代理（4000）、演示采购后台（8200）、主服务（8100）和工作台（8501）。
全部就绪后在浏览器打开 **http://127.0.0.1:8501**，此后通过工作台完成业务操作。
保持这一个窗口运行，按 **Ctrl+C** 会统一停止本次启动的服务及其子进程；启动失败
也会清理已经启动的部分。其他已运行进程不会被接管或停止，子服务不弹出终端。
所有服务仅监听 `127.0.0.1`。演示采购后台为 http://127.0.0.1:8200，开发者接口说明为
http://127.0.0.1:8100/docs；日常使用无需手写请求。

```powershell
# 只检查配置、依赖和端口，不启动服务或调用模型
.\.venv\Scripts\python.exe start.py --check

# 查询本地服务健康状态
.\.venv\Scripts\python.exe start.py status

# 不需要密钥、CLI 或 LiteLLM，启动工作台、主服务与演示后台
.\.venv\Scripts\python.exe start.py --offline
```

离线服务的状态使用 `start.py status --offline` 查询。`RESTOCK_AGENT_MODE=off`
也会启用离线模式；此模式不提供真实模型生成和自然语言修订。启动与状态检查只访问
本地健康/模型列表接口，不能证明书生密钥有效或完整任务通过。离线模式可用参数表单
完成规则计算与演示录单，页面明确标记未启用模型。浏览器安装是录单前提。

统一入口已验证并固定 LiteLLM 1.106.0、Streamlit 1.66.1。其余依赖的完整锁定仍按工程计划推进。

配置规则：

- 统一入口和直接导入 `app` 的服务都读取项目根目录 `.env`，不受终端当前目录影响。
- 已有进程环境变量优先于 `.env`；编辑配置后需重启。密钥不会出现在启动参数或控制台中。
- 数据库路径使用 `RESTOCK_DB_PATH`、`PROCUREMENT_DB_PATH`，相对路径以项目根目录为基准。
- 诊断日志写到被 Git 忽略的 `outputs/launcher/`，写入前遮蔽配置中的密钥与认证信息。
- `RESTOCK_CODEX_BIN` 可指定程序名或路径；未设置时查 PATH。项目相对路径也可使用，
  显式配置错误时不会静默换版本。项目 `CODEX_HOME` 仅在 CLI 子进程中设置。
- 统一入口固定管理本地 4000/8100/8200/8501 端口；`PROCUREMENT_PORTAL_URL` 保持
  `http://127.0.0.1:8200`。自定义外部后台属于手动部署场景。
- 统一入口自动为工作台和 API 提供相同的临时批准凭证，不需要用户手动配置。
  只有分开启动 UI/API 或运行独立冒烟脚本时，才需在被忽略的 `.env` 中配置相同的
  `RESTOCK_APPROVAL_TOKEN` 随机长值并重启。批准凭证不会注入 CLI 子进程；当前尚无
  用户登录和操作系统权限隔离，不能把本地工作台直接公开部署。
- 后台队列只支持一个 API 进程，不要使用多个 worker。服务重启可恢复查看，执行中断
  会标为待核对；不会自动重新录单。部分成功和未知结果的恢复仍在 M3。

### 工作台操作

1. 上传销售、库存、供应商三份标准 CSV（每份最多 5 MB），确认单位、库存、单价与在途口径，
   或点击“使用示例数据”。发现缺项时修正后重新检查。
2. 填写预算、天数、优先类别和排除商品。真实模式还可填写自然语言要求；最终以展示的生效参数为准。
3. 查看数量、金额、风险和未满足需求。修改产生新版本，页面显示与前一版差异；历史版本只读。
4. 勾选“我已核对”并批准当前版本，再点击“创建采购申请”。修改或明确应用新数据会撤销旧批准；
   单独上传不会覆盖已有任务。过期页面的批准会被拒绝。
5. 查看各供应商申请单号与回读结果，下载补货 CSV 和 HTML 报告。保存当前页面地址，或从任务记录返回。

申请创建不代表付款或到货。异常时请先核对已有申请，不要另建任务重复采购。

真实浏览器验收（自动启动隔离采购后台，重复执行两次验证不生成重复单）：

```powershell
$env:RUN_PLAYWRIGHT_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_playwright_e2e.py -q
Remove-Item Env:RUN_PLAYWRIGHT_E2E
```

统一启动的集成测试使用隔离端口、临时数据库和测试密钥，不发送模型推理请求，也不接管已有服务：

```powershell
$env:RUN_STARTUP_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_startup.py -q
Remove-Item Env:RUN_STARTUP_E2E
```

工作台真实浏览器验收（自动启动隔离服务，模型明确关闭；上传、修改、批准、执行、刷新和两种下载）：

```powershell
$env:RUN_WORKBENCH_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_workbench_e2e.py -q
Remove-Item Env:RUN_WORKBENCH_E2E
```

如果配置了 `PLAYWRIGHT_BROWSERS_PATH`，安装与验收需使用同一路径。普通回归默认跳过浏览器及启动集成，
skipped 不能计作验收通过。故障注入需按 AGENTS.md 单独显式运行，当前仍有 5 项已知失败。

开发者独立冒烟（off 模式，不需要 LLM key；会创建演示申请）：

```powershell
# 先在本地 .env 配置 RESTOCK_APPROVAL_TOKEN 随机长值，再用 start.py --offline 启动。
# 在另一窗口运行；日常工作台用户无需这一步：
.\.venv\Scripts\python.exe scripts/run_smoke_e2e.py
```

> ⚠️ 沙箱代理坑：本机环境存在 `HTTP_PROXY` 指向沙箱代理，httpx 默认读它，
> 会对本地服务产生诡异的 404/串响应。所有指向本地服务的 httpx 调用必须
> `trust_env=False`；Playwright 启动参数已带 `--no-proxy-server`。

## 目录结构

| 路径 | 内容 |
|---|---|
| `app/` | models / rules / state_machine / db / validation / runner / prompts / main；execution（录单）、procurement_portal（演示后台）；M1 新增 datasets（上传快照）、jobs（后台任务）、reports（差异与导出） |
| `ui/app.py` | Streamlit 业务工作台，仅通过业务 API 读写，不直接访问数据库 |
| `tests/` | 规则、审批 hash、runner、幂等、回读、批准版本与审计测试；Chromium E2E 和故障注入需显式启用 |
| `start.py` | 自动读取 .env、统一启动、状态查询、Ctrl+C 停止与脱敏日志 |
| `DATA_CONTRACT.md` | 数据确认口径、计算公式、输出字段、修订协议和旧方案兼容 |
| `ENGINEERING_PLAN.md` | 当前工程决策、M0–M4 任务、分工、排期与验收标准 |
| `data/` | 30 SKU 样例三件套 CSV |
| `scripts/` | generate_data.py（30 SKU 样例）+ run_smoke_e2e.py（独立冒烟，严格批准并核对本任务全部单据） |
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
