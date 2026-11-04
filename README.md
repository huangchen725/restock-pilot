# RestockPilot — 兴智杯大模型智能体关键能力技术创新赛

第三届「兴智杯」全国人工智能创新应用大赛 · 技术创新主题赛 · 方向③开放赋能智能体。
作品：店铺补货与采购申请 Agent。目标流程为上传资料、生成及修改方案、人工批准、采购申请录入、结果核对与交付。

## 工程计划与当前进度

**2026-11-04 新机安装入口已完成：双击 `setup.cmd`，填写本地 `.env` 后双击 `start.cmd`。** 支持固定运行时下载校验、已有配置保留、重复执行和失败恢复。定向回归 179 项通过；独立安装场景及新环境内 51 项产品验证通过。详见 [安装与启动](#新机安装与启动)、[工程进度](ENGINEERING_PLAN.md#setup-closeout) 和 [安装验收索引](docs/verification/setup-20261104.json)。这是同机自动化验证，M4-13 异机与 M4-14 部署尚未完成；本轮未提交推送。

后续开发与验收以 [工程执行计划](ENGINEERING_PLAN.md) 为准（2026-11-03 M4-01 至 M4-12 关闭复核已对齐）。
开发 Agent 接手时先阅读 [AGENTS.md 的问题关闭协议](AGENTS.md#问题关闭协议)，修复依据 [P2 关闭矩阵](ENGINEERING_PLAN.md#p2-closeout) 保留原反例、相邻边界、正常控制及实际入口证据。
当前 [38 项审计修复台账](ENGINEERING_PLAN.md#audit-status)：37 项验证关闭；P2-12 按用户要求忽略、未验证。此前六项 P2 的修复与规范已由 `fcf1240` 交付；本轮 M4 状态另见下方，异机和比赛材料仍单列。
**M4 整体仍未完成；M4-01 至 M4-12 已关闭。** 本轮 `temporary@660706d` 加未提交修改，严格分层 478 passed / 0 failed / 0 skipped；20 类场景覆盖 59 个去重后实际执行节点。正式稳定性入口的 30 SKU 离线工作台 10/10，真实书生入口另验 1/1，均核对批准、实际单据及导出。M4-13 至 M4-17 按用户要求本轮不处理，原状态保留；没有提交或推送。详见 [当前 M4 修复计划](ENGINEERING_PLAN.md#m4-audit)、[关闭索引](docs/verification/m4-closeout-20261103.json)；[首次审计](docs/verification/m4-20261103.json) 保留历史失败，不代表当前状态。

保留 Codex CLI 作为后台运行时，用户全程使用 RestockPilot 工作台；不要求打开
Codex 桌面界面或操作终端。当前阶段不抽取底座源码、不重写 Agent 循环。

M1 产品工作台已接通三份 CSV 上传、口径检查、方案展示、修改差异、人工批准、
演示采购录单与回读，以及 CSV/HTML 导出。长任务在后台运行，页面可刷新恢复同一任务。
M2 四个业务 MCP 已接入：检查数据、计算初始候选、计算修订候选、读取执行结果。
工具授权绑定本次任务、阶段和数据版本；正式方案由后端在校验最终决策后提交。
三阶段调用不代表已经实现多智能体协作。

M0 业务规则与数据契约已完成本轮开发，详见 [DATA_CONTRACT.md](DATA_CONTRACT.md)：
日期窗口、口径确认、合格在途抵扣、预算部分采购、风险并列展示及持久化快照修订。
新方案采用 schema 2；旧方案保持读取、批准指纹和执行兼容，尚未执行且没有快照的旧方案需明确应用新数据后修订；有执行尝试时仅按原批准核对/恢复。

2026-10-31 的 M2 整改恢复了离线工作台，修复了数据误用、预算与阶段越界、拒绝修订遗留版本、
不变参数报错、MCP 记录遗漏和 stderr 阻塞。问题逐项记录在工程计划 R1–R15。
真实 CLI + 本地假模型验证了允许的 MCP 调用和恶意命令被拒绝；真实书生三阶段已完成
独立演示链路，包含生成、修订、批准失效、重新批准、浏览器录单、独立回读及防重。
最终综合回归 199 passed / 2 skipped（两项真实模型用例单独运行）；包含工作台、真实 CLI 边界、
新旧 schema 浏览器正常流程、修复后的 5 项故障注入和统一启动测试。最终真实书生 30 SKU 链路另验通过，
预算修订后实际创建两张申请，共 2999.11 元，独立回读全部明细一致，重复执行未新增单据。
Planner 金额和风险统计改为程序生成，模型只给简短建议；此前摘要错误及最终复验均保留记录。
详细计数、阶段耗时与范围见工程计划，证据位于 `outputs/m2-remediation/`。
M3 的全部 P0/P1 已在 2026-11-02 第二轮通过验收；本轮修复剩余六项 P2。最终综合回归 357 passed / 0 failed / 2 skipped，真实书生 1/30 SKU 另验 2 passed / 0 failed / 0 skipped；新安装环境启动与 CLI 边界 30 passed。真实用例包含联合修改五类参数、不支持混合请求整体拒绝、版本对应的摘要核对和独立采购回读。此前失败原样保留；版本指纹、入口和局限见 [验收索引](docs/verification/p2-20261103.json)，一次通过不代表 M4 连续稳定性达标。
未知结果和回读不一致会阻止新采购版本；执行后固定数据快照，修订按任务累计额度扣除已创建申请。
工作台提供受控核对与恢复，操作异步运行并可刷新查看；恢复始终使用原批准与参考号。
方案、阶段事件与结果原子发布；订单回执和事件原子落账，写账失败停止后续录单。
历史中断造成的账本缺口会明确显示，不会因后续订单核对成功就伪造完整历史。
现有演示服务未重启；验证使用独立端口和临时数据库。单次真实模型成功不代表重复稳定性达标。

M1 历史验收（2026-10-30）：普通回归 138 passed / 10 skipped，真实工作台上传到下载通过，
独立冒烟覆盖 30 SKU；当轮未调用真实模型。历史证据在 `outputs/m1-workbench/`。

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

**2026-11-10 23:59:59（北京时间）报名及作品提交截止。** 2026-11-03 已核对[官方赛道页面](http://www.aiinnovation.com.cn/web/#/trackDetail?id=160&ckey=5)的 id=160/ckey=5 数据；旧 11 月 5 日记录已过时。需准备技术 PDF（含 ≤5 分钟、≥720P 视频及体验链接）、代码和模型材料，并按官方要求打包命名；API-only 模型材料是否可豁免未明确，不能自行省略。

M3 专项验收入口（前提是按下文配置好 Chromium 路径）：

```powershell
$env:RUN_PLAYWRIGHT_E2E = '1'
$env:RUN_WORKBENCH_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_m3_safety.py tests/test_m3_acceptance.py tests/test_m3_live.py tests/test_m3_workbench.py -q
Remove-Item Env:RUN_PLAYWRIGHT_E2E, Env:RUN_WORKBENCH_E2E
```

这些用例使用隔离数据库、回环端口和自建采购后台，包含真实 Chromium、真实工作台和本次启动 API 的进程中断；模型明确关闭。真实书生、冻结稳定性和异机复现单列，不由这些结果代替。

## M4 验收入口

以下均在项目根目录使用同一 `.venv`。先按启动说明安装锁定 CLI、Chromium 并配置环境；相对 `RESTOCK_CODEX_BIN` 在项目根解析。验收不会重置用户数据库，输出保存在 `outputs/` 的独立目录。

```powershell
# 正式非真实模型分层验收；严格模式不接受任何 skipped
.\.venv\Scripts\python.exe scripts/run_acceptance.py --layers 1,2,3,4 --strict

# 30 SKU 真实工作台/浏览器；明确关闭模型的 10 轮稳定性
.\.venv\Scripts\python.exe scripts/run_stability.py --mode off --runs 10 --target-success 9

# 单独验证真实书生入口，需要项目 .env 的 INTERN_API_KEY，会消耗模型额度
.\.venv\Scripts\python.exe scripts/run_stability.py --mode required --runs 1 --target-success 1

# 实际交付 ZIP 或解压目录；发现问题始终失败，不需依赖 --strict
.\.venv\Scripts\python.exe scripts/check_delivery.py --path .\delivery.zip
# 只检查 Git 跟踪的源码候选；此结果不能代替实际交付包检查
.\.venv\Scripts\python.exe scripts/check_delivery.py --path . --tracked
```

分层为：1 规则/API 与验收控制器；2 真实浏览器/故障/重启；3 离线真实工作台；4 CLI 允许/恶意调用与启动；5 真实书生 API/工作台；6 新机安装（联网、独立目录）。默认 1–4 不运行第 6 层，需显式选择。每个测试节点只属于一层；脚本清除非本层开关，只有显式选择第 5 层才在分层验收中调用模型。第 5 层与 required 稳定性是不同入口，不能把运行其中一个写成全部真实模型测试已通过。

20 个产品场景由 [场景目录](tests/scenario_catalog.py) 映射到实际测试；复用节点只统计一次。正常任务与故障恢复经真实工作台/浏览器，规则和数据契约场景按其层级验收，不把规则测试说成 UI 验收。可显式设置 `RUN_PLAYWRIGHT_E2E=1`、`RUN_WORKBENCH_E2E=1` 后，通过 `python -m pytest tests/ -p tests.acceptance_plugin --restock-scenarios -q` 单独选择这 20 类场景。

稳定性脚本要求 `--mode`，保存源码/依赖指纹、逐轮 JUnit、脱敏日志、数据/方案 hash、人工批准、实际订单、CSV/HTML 及其 hash。未经批准写入、重复申请、金额不一致、跳过或证据不完整均直接阻断，不能用成功率抵消；只有证明确未产生采购申请的规划/修订普通超时可占失败额度。目录必须新建，不能覆盖原失败记录。off 与 required 分开统计，一轮 required 通过不等于十轮真实模型稳定性。

交付扫描器默认检查实际输入中的全部文件，包括 ZIP 成员、中文路径和运行目录；`--tracked` 明确只覆盖 Git 候选。退出码为 0=本次扫描未发现问题、1=发现敏感内容/路径、2=输入无效或扫描不完整；UTF-8/GBK 控制台均可读。当前保守限制为单文件 50 MiB、总量 256 MiB、10,000 个条目，超限报错，不能当通过；不支持加密 ZIP 或符号链接。可用 `--report <文件>` 保留清单、hash 和脱敏结果，父目录须已存在。扫描器通过并不代表异机、体验环境或比赛材料已交付。

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

新机安装专项验收入口：`.\.venv\Scripts\python.exe scripts/run_acceptance.py --layers 6 --strict`。它在不含 Python/Node 的 PATH 和新目录中调用真实安装入口，检查首次安装、模拟网络不可用时重复安装、错误配置拒绝/纠正后恢复及新环境产品流程；耗时和下载量较大。同机自动化记录仍不能代替 M4-13 的真实异机验收。

## 新机安装与启动

本入口适用于 Windows x64，需 Windows PowerShell 5.1 和系统 `tar.exe`（当前 Windows 10/11 环境已验证）。首次安装需要访问 GitHub 的 Astral Python 发行、Node.js、PyPI 和 Playwright 下载源。使用包含 `setup.cmd` 的 temporary 分支版本；无需预装 Python、Node、Codex 桌面客户端，也不需要管理员权限或全局 PATH 修改。

1. 将工程放在可写目录，双击 **`setup.cmd`**。它下载并校验固定版本 Python、Node，创建 `.venv`，安装锁定依赖、Codex CLI 和 Chromium，最后核对实际运行版本。
2. 安装提示完成后，在本地 `.env` 中填写 `INTERN_API_KEY`。安装不会验证真实密钥，也不会调用模型。
3. 双击 **`start.cmd`**，服务就绪后打开 **http://127.0.0.1:8501**。后续启动不需再次安装。

已有 `.env`（包括空文件、BOM、换行、注释及自定义字段）按原字节保留。缺失时才从 `.env.example` 创建。安装器将非敏感默认值写到 `.restock/environment.json`，优先级为 **已有进程环境变量 > `.env` > 安装默认值**。明确配置了错误 CLI 路径时会失败，修正该配置后重新安装；不会偷偷替换用户选择。

Python、Node、浏览器及下载缓存位于 `.restock/`，依赖与 CLI 位于 `.venv/`，均不进 Git 或源码交付包。不要复制另一台机器的 `.venv`；在新目录重新运行安装。已有虚拟环境的 Python 版本不符、未知残缺环境、运行目录链接等情况会明确拒绝并保留现场。

安装可重复运行：实际版本、依赖与浏览器检查通过才复用；失败后修正网络或配置再执行。两次安装不能同时进行。日志与逐阶段结果保存在 `outputs/setup/<本次编号>/`，失败记录不覆盖。安装只使用本次进程的执行策略，不修改系统策略；Windows 的 npm 入口明确使用 `npm.cmd`。

自动化验收或命令行使用：

```powershell
# 不在末尾等待按键；返回 0 表示环境准备与版本核验完成
.\setup.cmd -NoPause

# 可选的共享下载缓存；所有命中均重新核对 SHA-256
.\setup.cmd -NoPause -CacheDirectory .\outputs\setup-cache

# 可显式选择离线演示；仍需先安装
.\start.cmd --offline

# 环境预检；required 模式还要求已填写密钥，但不验证上游推理
.\start.cmd --check
```

固定来源及 SHA-256 见 [bootstrap-lock.json](bootstrap-lock.json)：Python 3.12.14 使用 Astral 的独立 Windows x64 发行，Node 为 24.19.0 官方 ZIP。产品 Python/CLI/Chromium 基线仍由 [runtime-lock.json](runtime-lock.json) 定义。已经自行准备好兼容环境的开发者也可以继续使用 `.\.venv\Scripts\python.exe start.py`；没有安装默认值文件时，原 `.env`/PATH 解析规则保持。

如果已有 `PLAYWRIGHT_BROWSERS_PATH`，安装与运行使用该位置；项目内缺少浏览器时自动安装，项目外的已有缓存只验证、不擅自写入，缺失时提示处理。安装成功只代表本地环境准备完成，不代表异机、真实模型或公开体验环境验收已完成。

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

# 不调用模型，也不需要密钥或 CLI；仍需安装完整锁定依赖和浏览器
.\.venv\Scripts\python.exe start.py --offline
```

离线服务的状态使用 `start.py status --offline` 查询。`RESTOCK_AGENT_MODE=off`
也会启用离线模式；此模式不提供真实模型生成和自然语言修订。启动与状态检查只访问
本地健康/模型列表接口，不能证明书生密钥有效或完整任务通过。离线模式可用参数表单
完成规则计算与演示录单，页面明确标记未启用模型。浏览器安装是录单前提。

`requirements.txt` 统一引入 [requirements.lock](requirements.lock)，锁定直接及传递依赖；[runtime-lock.json](runtime-lock.json) 固定 Python、平台、CLI 和 Chromium 组合。启动预检核对实际安装版本并启动受控浏览器检查版本，任何不符都清晰失败；不会静默换 CLI。浏览器默认无界面模式可使用 `playwright install chromium --only-shell`，需要可见浏览器时安装完整 Chromium。

配置规则：

- 统一入口和直接导入 `app` 的服务都读取项目根目录 `.env`，不受终端当前目录影响。
- 已有进程环境变量优先于 `.env`；编辑配置后需重启。密钥不会出现在启动参数或控制台中。
- 数据库路径使用 `RESTOCK_DB_PATH`、`PROCUREMENT_DB_PATH`，相对路径以项目根目录为基准。
- 诊断日志写到被 Git 忽略的 `outputs/launcher/`，写入前遮蔽配置中的密钥与认证信息。
- `RESTOCK_CODEX_BIN` 可指定程序名或路径；未设置且没有安装默认值时查 PATH。相对路径在项目根解析后才进入 Agent 临时目录，
  显式配置错误时不会静默换版本。项目 `CODEX_HOME` 仅在 CLI 子进程中设置。
- 统一入口固定管理本地 4000/8100/8200/8501 端口；`PROCUREMENT_PORTAL_URL` 保持
  `http://127.0.0.1:8200`。自定义外部后台属于手动部署场景。
- 统一入口自动为工作台和 API 提供相同的临时批准凭证，不需要用户手动配置。
  只有分开启动 UI/API 或运行独立冒烟脚本时，才需在被忽略的 `.env` 中配置相同的
  `RESTOCK_APPROVAL_TOKEN` 随机长值并重启。CLI/MCP 子进程不继承该凭证、业务数据库路径或模型密钥。
- 每次模型阶段自动生成临时 CODEX_HOME、工作目录和短期工具凭证；模型密钥留在后端。
  后端向 LiteLLM 转发请求，移除非业务工具，并在向 CLI 返回输出之前拒绝未经授权的工具调用。
  MCP 转发器只访问本次授权入口，不读取 .env 或 SQLite；计算结果是候选，不能自行发布方案。
  这属于程序强制的工具能力边界，**不是独立操作系统用户或容器隔离**；宿主机管理员、被篡改的
  CLI 及公网登录防护不在此范围，不能把本地工作台直接公开部署。
- 后台队列只支持一个 API 进程，不要使用多个 worker。服务重启可恢复查看，执行中断
  会标为待核对；不会自动重新录单。部分成功和未知结果可通过受控核对（`/reconcile`）
  与受控恢复（`/recover`）处理：核对确定订单状态后，只恢复确定未提交的订单，
  已创建的保留不重复，结果未知的需先人工核对；修订时新版本扣除已创建申请覆盖的需求。

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

真实 CLI 协议验收使用本地假模型，不消耗书生额度；需要 PATH 或 RESTOCK_CODEX_BIN 可找到 CLI：

```powershell
$env:RUN_AGENT_BOUNDARY_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_agent_runtime.py -q
Remove-Item Env:RUN_AGENT_BOUNDARY_E2E
```

真实书生验收（明确调用上游模型，自动启动独立代理/API/演示后台，含 1 SKU 手算和 30 SKU 样例）：

```powershell
$env:RUN_REAL_MODEL_E2E = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_real_m2.py -q
Remove-Item Env:RUN_REAL_MODEL_E2E
```

上游代理默认本地 4000。手动部署隔离服务可设置 RESTOCK_LITELLM_URL；业务 CLI 仍只能使用后端
生成的临时网关地址，不继承上游密钥。不要用旧 SETUP.md 中的宽权限手动 CLI 示例运行产品任务。

如果配置了 `PLAYWRIGHT_BROWSERS_PATH`，安装与验收需使用同一路径。普通回归默认跳过浏览器及启动集成，
skipped 不能计作验收通过。故障注入需按 AGENTS.md 显式运行；本轮原 9 项及新增 M3 真实 API、工作台、重启和跨版本验证已通过，范围以工程计划第二轮记录为准。

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
| `app/business_tools.py` | 任务授权、候选计算及后端校验提交，工具不直接发布方案 |
| `app/agent_runtime.py`、`app/mcp_server.py` | 模型工具协议检查、动态运行配置及仅可调用授权入口的 STDIO 转发器 |
| `tests/` | 规则、审批 hash、runner、幂等、回读、批准版本与审计测试；Chromium E2E 和故障注入需显式启用 |
| `start.py` | 自动读取 .env、统一启动、状态查询、Ctrl+C 停止与脱敏日志 |
| `DATA_CONTRACT.md` | 数据确认口径、计算公式、输出字段、修订协议和旧方案兼容 |
| `ENGINEERING_PLAN.md` | 当前工程决策、M0–M4 任务、分工、排期与验收标准 |
| `data/` | 30 SKU 样例三件套 CSV |
| `scripts/` | generate_data.py（30 SKU 样例）+ run_smoke_e2e.py（独立冒烟，严格批准并核对本任务全部单据） |
| `agent-integration/` | Codex config.toml / LiteLLM 配置 / SETUP.md 操作手册（含 8 条踩坑记录） |
| `day1-test/` | Day 1 验收产物（JSONL 事件流 + result.json） |
| `test_intern_function_calling.py` | 书生端点工具调用实测脚本（换端点/key 复用） |
| `codex-main.tar.gz` | Codex CLI 源码包（参考用，需研究时解压） |

## 当前接入约定

- 现有链路使用 Codex 的 Responses 接口，经 LiteLLM 转换到端砚 Chat Completions；保留这条已在原环境验证的接入路径。
- 原环境记录端砚目标模型不支持 `/v1/responses`；固定版本及端点后的迁移复验按工程计划执行。
- 产品使用只读 CLI 设置和强制业务工具协议检查，不以 `danger-full-access` 或 cwd 宣称隔离。agent-integration/config.toml 仅供参考，产品不会复制个人配置或静态机器路径。
- TOML 顶层键必须写在任何 `[table]` 段之前，否则静默失效

## 安全注意

两把 API key（挑战杯 + 端砚）都曾在聊天中明文暴露——**11/5 提交前在控制台吊销重生成**；真实 key 只放 `.env`（已 gitignore）。
