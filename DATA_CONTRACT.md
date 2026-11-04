# RestockPilot 数据与补货规则契约

版本：`schema_version=2`，`rules_version=replenishment-v2`。适用于当前固定 CSV 数据入口，供后续上传工作台和业务工具共同复用。原始销售、库存和供应商 CSV 不被计算过程改写。

## 输入和口径确认

CSV 支持 UTF-8、UTF-8 BOM 和 GB18030，表头使用标准英文字段。三表不得为空，列名不得重复，数据行宽度必须与表头一致。字符串两端空白会被去除。

| 文件 | 必填字段 | 数值与唯一性约束 |
|---|---|---|
| sales.csv | date, sku, sold_qty | 日期为有效 YYYY-MM-DD；同一 SKU、日期只有一条日汇总；销量有限且非负，可有折算件的小数 |
| inventory.csv | sku, name, category, available_qty, inbound_qty, inbound_eta | SKU 唯一；可用和在途库存为非负整数；在途大于 0 时必须填写非负整数 ETA |
| suppliers.csv | sku, supplier_id, unit_cost_cents, lead_days, pack_size, min_order_qty | 每个 SKU 一个供应商记录；单价和包装为正整数，交货天数及起订量为非负整数 |

数值输入上限为 10^12；不接受 NaN、Infinity、负数或非法文本。库存、金额、包装和天数不做字符串小数或布尔值到整数的隐式转换。销售 SKU 必须存在于库存表；库存与供应商 SKU 集合必须一致。库存中没有销量的商品按缺失日期处理，不静默丢掉。

数据提供方通过 `POST /tasks` 的 `data_contract` 明确确认以下口径；省略此字段时读取数据目录的 `contract.json`。请求中的确认优先，不与文件中的确认隐式合并。

```json
{
  "quantity_unit": "piece",
  "available_is_net": true,
  "cost_includes_tax_shipping": true,
  "inbound_eta_is_days": true,
  "missing_sales": "reject",
  "window_end": "2026-10-28"
}
```

- `quantity_unit=piece`：销售、库存、在途和供应商包装都已统一为件；销量可为折算件，实际采购为整数件。
- `available_is_net=true`：可用库存已经扣除预占。
- `cost_includes_tax_shipping=true`：单价为含税含运的整数分到手价。
- `inbound_eta_is_days=true`：`inbound_eta` 表示相对本次规划时点的到货天数；0 表示立即可用，不是日期字符串。可用库存是在这一规划时点的数量。
- `missing_sales=reject` 为默认值。缺失日期不是零销量，必须补齐数据或显式选择 `zero`。
- `window_end` 可以省略；默认采用整份销售数据中的最新日期。历史演示数据不依赖系统当天日期。

仓库自带 `data/contract.json` 只声明演示数据的既定口径，不是对用户未来上传数据的自动确认。未提供完整确认时进入 `NEEDS_INPUT`，不能生成或批准方案。

## 28 天窗口和数据快照

窗口包含截止日及其前 27 个自然日，所有 SKU 使用同一窗口。按日期取数，不按 CSV 行顺序取末尾记录。窗口前的记录不参与日均，但仍保留在快照中；明确截止日之后出现销售记录会报错，防止无意遗漏新数据。

默认要求每个库存 SKU 在窗口内有 28 条日记录。选择 `missing_sales=zero` 后才补缺失日为零，同时记录补零天数，并将该 SKU 标为低置信度。显式写出的零销量不会被计为缺失日。

`DatasetSnapshot` 包含确认口径、窗口起止日期，以及规范排序后的三张完整输入表。对其规范 JSON 计算 SHA256，得到 `dataset_hash`；`dataset_id` 为 `ds-` 加该 hash。行重排不改变结果或 hash，输入值及确认口径变化会改变 hash。

快照与方案一起保存在现有 `plans.plan_json` 中。M1 另以增量建表方式将上传快照保存在 `datasets`，不删除或重建数据库。默认修订只使用方案持久化的快照；后台 CSV 被修改甚至删除，不会影响已有任务的重算。

上传不自动覆盖任务。尚未执行的任务可显式提交新的 `dataset_id`，生成 v+1 并撤销旧批准；旧版本保留原数据与 hash。执行后不允许用此入口换数据重买，部分成功和未知结果须按 M3 恢复规则处理。相同内容与口径对应同一数据身份；不同内容或口径产生新身份。

## 补货数量和预算

定义：`d` 为窗口 28 天日均有效销量，`L` 为交货天数，`R` 为覆盖天数，`S` 为安全天数。规划窗口长度 `H=L+R+S`。

1. `target_qty = ceil(d × H)`。
2. ETA 已知且满足 `0 ≤ ETA ≤ H` 时，在途数量计入 `eligible_inbound_qty`；否则本次不抵扣。
3. `gap = max(0, target_qty − available_qty − eligible_inbound_qty)`。
4. 缺口为 0 时原始建议采购量为 0；否则把 `max(gap, min_order_qty)` 向上取整至 `pack_size` 的倍数，得到 `original_order_qty`。
5. 全部商品共用一个预算，按是否属于优先类别、预计缺货的完整天数、SKU 排序。优先类别列表表示同一优先层，不表示类别之间的先后顺序。
6. 可负担数量为 `floor(剩余预算 / 单价 / 包装量) × 包装量`。最终 `order_qty` 取可负担数量与原始建议量的较小值；若低于起订量则为 0。
7. 分配后扣减预算，记录 `unmet_qty=max(0,gap−order_qty)`，即仍未满足的净需求，不把额外整包量计为需求。

目标库存的向上取整使用精确分数，避免二进制小数误差多买一件；数量和金额分配使用整数运算。展示用日均保留四位小数，不用于重新计算数量。

手算例：日均 4 件，L=3、R=7、S=2，无库存和在途，目标及缺口均为 48 件。单价 100 分、包装 6 件、起订 12 件、预算 1800 分时，采购 18 件，总额 1800 分，未满足 30 件。预算 1199 分时不足起订，采购 0 件。

## 风险和输出字段

到货前缺货独立于最终净缺口、预算和置信度计算。检查当前库存能否撑到本次采购交货日；若已有在途更早到达，分别检查在途到达前，以及加入在途后能否撑到交货日。库存恰好撑到到货时点不记为提前缺货。即使最终 gap=0，也可能在货物到达前缺货。

| 层次 | 新增字段 | 含义 |
|---|---|---|
| Plan | schema_version, rules_version | 数据结构及规则版本 |
| Plan | dataset_id, dataset_hash, dataset_snapshot | 数据身份、指纹与重算依据 |
| Plan | budget_remaining_cents | 实际采购分配后的余额 |
| Plan | partial_skus, skipped, excluded_skus | 部分采购、预算完全跳过、用户主动排除，分别记录 |
| PlanItem | horizon_days, eligible_inbound_qty | 规划时长与实际抵扣在途 |
| PlanItem | original_order_qty, order_qty, unmet_qty | 分配前建议、最终数量、剩余净缺口 |
| PlanItem | stockout_before_arrival, missing_sales_days, warnings | 到货前风险、补零天数、可并列展示的风险代码 |

`warnings` 可同时包含 `low_confidence`、`missing_sales_filled`、`inbound_not_deducted`、`stockout_before_arrival`、`budget_partial`、`budget_skipped`。`reason` 提供合并的人类可读说明，后一个问题不会覆盖前一个。

销量 CV 小于 0.3 为高置信，0.3 至 0.7 为中置信，大于 0.7 为低置信；零销量日超过一半或使用缺失日补零时也标为低置信。这是人工复核提示，不是统计预测置信区间。

## API 和修订约定

```json
{
  "policy": {
    "budget_cents": 1800,
    "cover_days": 7,
    "safety_days": 2,
    "priority_categories": [],
    "excluded_skus": []
  }
}
```

上述请求可直接用于已配置口径文件的数据目录；直接使用其他口径时可同时提交 `data_contract`。工作台先上传并绑定 `dataset_id`，不能再用任务级 `data_contract` 覆盖该数据集口径。

创建、修订、执行默认立即返回 HTTP 202 和 `task_id`；通过 `GET /tasks/{id}` 查询 `state`、`operation.busy/phase/error/result`。JSON 字段或 Policy 类型不合法仍同步返回 422。数据或口径问题会持久化为 `state=NEEDS_INPUT` 及 `operation.result.data_problems`，不调用模型、不保存方案；未知类别或排除 SKU 也阻止计算。脚本可显式设置 `Prefer: wait` 获得同步结果与原有错误状态码。

M1 接口约定：

| 操作 | 请求及响应 |
|---|---|
| 上传 | `POST /datasets`，multipart 字段 sales/inventory/suppliers 为三份 CSV，contract 为 DataContract JSON 字符串；每份最多 5 MB。成功返回 READY、dataset_id/hash、窗口、商品数；问题返回 NEEDS_INPUT、data_problems，不生成数据版本 |
| 示例 | `POST /datasets/example`，将自带样例保存为不可变快照 |
| 创建 | `POST /tasks`，policy、dataset_id，可选 user_request；自然语言先转换并校验为受支持 Policy |
| 修订 | `POST /tasks/{id}/revise`，结构化 policy 或自然语言 comment，可选 dataset_id；policy 和 comment 不能同时提交 |
| 批准 | `POST /tasks/{id}/approve`，正文必须为页面所见 version、plan_hash；需 X-Restock-Approval 工作台凭证。缺凭证 403，过期版本/hash 409，处理期间 409 |
| 版本与差异 | `GET /tasks/{id}/plans`、`plans/{version}`、`diff?from_version=1&to_version=2` |
| 阶段及结果 | `GET /tasks/{id}/activity`、`executions`；状态、意见、摘要和回执持久化 |
| 下载 | `GET /tasks/{id}/artifacts/{version}/csv` 或 html；历史报告只含对应版本申请，不将任务当前状态等同于该版本已执行 |

M3 接口约定（2026-11-02 第二轮整改）：

| 能力 | 接口与状态约定 |
|---|---|
| 受控核对 | `POST /tasks/{id}/reconcile`；PARTIAL / NEEDS_RECONCILIATION / 有有效批准的 FAILED；按原批准 version/hash 查询实际后台，不创建申请；完整观测与核对事件原子持久化 |
| 受控恢复 | `POST /tasks/{id}/recover`；同上；在任务互斥下先重查批准并进入 EXECUTING，创建唯一执行尝试，再核对并只提交已证明未尝试提交的订单；使用原 external_ref |
| 人工重新决策修订 | `POST /tasks/{id}/revise`；PARTIAL / NEEDS_RECONCILIATION 由后端在互斥内重新核对，只有无 unknown/回读不一致、快照不变才转 ANALYZING；工具自身不拥有改变执行状态的权限 |
| 跨版本总览 | `GET /tasks/{id}/summary`；返回全部有效申请、当前未决执行事项、有效申请累计金额及是否有执行尝试；历史失败被后续合法回执解决后不再重复列为未决 |
| 审计 | `GET /tasks/{id}/audit-trail`；返回原事件、`replay_complete` 与 `issues`；按尝试及其正式方案和批准记录校验，不把历史 v1 事件与当前 v2 hash 比较 |

三个操作端点默认返回 202（`task_id/accepted`），通过 `GET /tasks/{id}` 的 operation.busy/phase/error/result 查询结果；`Prefer: wait` 为脚本兼容的同步入口，成功返回 200。操作互斥、错误状态、批准过期、未知结果、回读不一致、执行后换数据返回 409；不支持的要求、非法 Policy 或低于已创建金额的累计预算返回 422；持久化/运行失败返回 500 或相应阶段错误。不存在的任务为 404。不得把 202 当作采购成功。

**未知结果**：历史 `submission_state=unknown`、已落账的 submit_intent 后无可靠结果、旧执行记录不足、查询失败，均保持未知。一次空列表不能解除未知。只有实际查询到业务状态和全部明细一致的申请才能解除；持续未知或回读不一致显示待人工核对，不能强制改为未提交、换版本或换数据重买。新协议只有确认未发出提交（或提交意图之前中断）才允许受控恢复。明确未提交的失败、真实有效申请和未知结果分开表示。

**有效覆盖**：从同一 execution_records / execution_events 按原 external_ref 取最新可信观测，独立按原正式方案验证回执。`created/recovered_after_error/already_exists` 三种成功状态均可计入，但须具备非空 order_id、SUBMITTED 业务状态，且 supplier、external_ref、SKU、数量、单价和总额一致。按 order_id/SKU 去重；同一订单不会因多次查询重复扣量。撤销、错误状态、回读不一致不是有效覆盖，同时阻断新的采购版本，不能简单忽略后重买。

**固定数据与累计预算**：一旦任务有执行尝试，就禁止切换不同数据快照，包括全部尚未提交的失败任务。修订沿用原快照，扣除已有效创建的数量和金额；累计预算不得低于已创建金额。例：旧版已购 A 48 件、4800 分，累计预算改为 8000 分时，新版至多使用余下 3200 分，而非重新取得 8000 分。新版本没有执行过时，相同参数复用版本；已执行的旧版本即使参数不变，也必须重新计算剩余需求并重新批准，不能直接复用原采购清单。

**MOQ**：剩余需求仍按供应商起订量与包装取整，可能超过剩余缺口，但不得突破扣除已创建金额后的预算；报告保留这一规则依据。

**方案事务**：正式 Plan、阶段事件和 operation.result 同一事务提交，提交后的必要持久化失败整体回滚，不删除任何先前正式版本。尚未执行的方案修订失败后，旧批准失效、原方案仍可重新复核；已执行任务的失败修订若没有发布新版本，保留原批准和执行状态供安全核对/恢复。只有新版本成功发布才撤销该原批准。

**执行与审计事务**：正常执行和恢复均有唯一 attempt_id，开始声明完整订单集合及正式 version/hash，查重、提交意图、回读和结果按顺序记录；每个回执与对应事件在同一事务落账。回调写账失败必须抛出，任务进入待核对，不得继续后续供应商。采购执行或恢复期间 API 进程中断后保留批准、释放互斥并进入 NEEDS_RECONCILIATION；恢复先查原参考号。旧协议或进程崩溃缺失的历史事件不被补造，即使实际申请后来核对一致、任务恢复完成，历史审计仍可为不完整，UI 和 HTML 报告明确展示缺口。

人工批准凭证由统一启动入口注入工作台和 API，不注入后台 CLI。M2 另通过临时模型网关限制可执行工具、通过操作会话绑定任务/阶段/数据；CLI/MCP 不继承数据库配置或上游密钥。边界是工具能力隔离，不是独立 OS 用户或容器。当前队列限定单 API 进程；启动时把上次执行中断标为 NEEDS_RECONCILIATION，不自动重新提交采购。

首版 Policy 只支持预算、覆盖天数、安全天数、优先类别和排除 SKU。预算必须为正整数分，覆盖天数为 7–90，安全天数为 0–30。不能直接修改数量、价格、供应商或设置单 SKU 最低数量。

Reviser 必须返回 `policy`、`explanation`、`unsupported_requests` 三个字段。识别到不支持或不明确的要求时返回 `policy=null` 和非空问题列表；整个修订被拒绝，不部分应用。缺字段、非法 Policy 或问题列表非空均记录为修订错误（`Prefer: wait` 时返回 422），不产生 v+1；旧批准按已有修订语义撤销，仍需重新复核。未执行当前版本时，相同数据和参数的修订复用原版本；已经执行过的版本按上方恢复契约重新计算剩余需求。

MCP build_plan / revise_plan 只计算候选，不写 plans。初始生成必须先 inspect_dataset，且不能改动已经确认的结构化 Policy；自然语言修订的最终完整 Policy 必须与工具候选一致。后端提交时再次检查 ANALYZING 状态、操作互斥、原版本/hash；任何失败都不发布候选。PARTIAL / NEEDS_RECONCILIATION 不能通过工具直接变回待批准；须由后端在互斥内完成上方核对与固定快照校验，才允许候选计算，新版本扣除已有有效申请覆盖及金额。每次工具调用按任务保存阶段、操作标识、绑定的数据集、输入、结果及拒绝原因。

Planner 的预算、金额、商品数量和风险交集核对由程序生成，明确标注来源；模型只提供简短非数值建议，原始建议单独保留。金额单位始终是人民币分，展示元时除以 100；到货前缺货风险总数与其中被预算跳过的数量分别统计。

这个协议及拒绝路径已通过模拟模型故障注入；真实书生的支持范围内预算修订已经验证。所有自然语言不支持项的语义识别覆盖仍属 M4 固定场景，不能仅凭 schema 宣称模型永远不会误解。

## 旧方案兼容和验收范围

缺少 `schema_version` 的存量 JSON 按版本 1 加载，审批 hash 只包含旧有字段；新增默认字段不会改变其既有指纹。新方案按版本 2 将快照、规则版本和计算证据一并纳入 hash。

旧方案仍可读取、核验和按原批准执行或恢复。没有历史数据快照时，不从现在的 CSV 猜测原始依据；直接修订被拒绝（同步模式 409）。尚未执行的旧方案可上传并显式应用新数据、重新批准；已有执行尝试的旧方案只能按原批准核对/恢复，不能换数据重买。数据库与旧记录不被清空或自动重算。

验收覆盖日期乱序、缺失与重复、编码、数值边界、口径确认、在途时点、预算整包与起订量、版本持久化、旧 hash 兼容，以及新旧方案的真实 Chromium 录单与防重。M1 验证工作台上传到下载、旧批准失效、非阻塞响应、执行互斥、数据库重开及导出安全。M2 整改增加真实 CLI 协议边界、工具拒绝、候选回滚和真实书生预算修订；均使用隔离服务和数据库，详见工程计划。2026-11-02 第二轮 P0/P1 最终综合回归 270 passed / 0 failed / 2 skipped，包含新增 M3 59 项、真实工作台恢复、真实进程中断及跨版本累计金额核对；详见工程计划，真实模型与 M4 验收单列。


<a id="m5-collection"></a>
## M5-1 采集结果、本地暂存与正式 CSV（2026-11-04）

本节是现有不可变快照的新增来源入口，不改变已有 CSV 手动上传或补货数值规则。网页采集器已接入；以下结构化批次由后端将可见页面按固定映射转换后提交，不是要求商户后台提供 JSON/CSV 接口，更不是允许产品 Agent 绕过页面读取测试数据库。

### 来源与三个独立周期

`POST /data-sources` 创建来源，字段为 `name`、`merchant_id`、`warehouse_id`、`environment`（`test`/`real`）、已确认的 `contract` 和 `schedule`。来源商户/仓库/口径创建后固定，修改接口只调整周期；选择 `real` 不代表真实来源已验收。

`schedule` 必须分别提供整数秒 `collect_every_seconds`、`save_every_seconds`、`check_every_seconds` 和 `max_age_seconds`，范围为 1–2678400。前三项分别是采集、正式保存、补货检查周期；最后一项是源数据最大年龄。采集和保存周期分别运行；补货检查周期只持久化设置，触发器属于 M5-2。采集结束后至少等待一个采集间隔再启动下一轮，避免长任务完成后连续抢跑；单来源不并发。修改单项不会重置其他周期或丢失暂存内容。

### 采集批次

`POST /data-sources/{source_id}/observations` 接收以下字段：

| 字段 | 约束 |
|---|---|
| `batch_id` | 来源内唯一的幂等标识，1–100 位字母/数字/下划线/短横线；相同 ID 内容不同拒绝 |
| `merchant_id` / `warehouse_id` | 必须与来源配置完全对应，不能混入其他店/仓 |
| `observed_at` / `source_as_of` | 带时区的 ISO 8601；源数据时间 ≤ 采集时间 ≤ 接收时刻，不能把本次读取时间冒充源数据更新时刻 |
| `complete` | 必须明确为 true；声明只是可信采集器的完整性输入，不能代替浏览器分页实测 |
| `row_counts` | 恰好含 sales/inventory/suppliers，分别与实际行数一致 |
| `sales` / `inventory` / `suppliers` | 三张完整表，沿用本文件现有字段和单位；本入口不接受额外字段，值为字符串 |

程序统一排序、去除外围空白，并规范化数值写法（如 `004`/`4`、`4.000`/`4`），不舍入销量精度。正常数据仍按原 `prepare_snapshot` 规则校验：缺日期、未知库存/成本、未确认口径等不能靠默认值补齐。本入口按完整批次接收，不把不同时间的零散表默默拼接；页面适配器先读取并核验全部分页，四类页面数据时间必须一致；模型不得提交替代数据。

有效批次在数据库内暂存，尚未成为可消费的正式 dataset。重复批次不刷新时效；内容不变的新采集只更新观测/来源时间和轻量记录，不重复生成正式 CSV。旧批次不覆盖较新数据；新的无效采集阻止后续使用，上一份正式版本仍保留。

### 正式发布与恢复

默认目录：`outputs/collections/<source_id>/<dataset_id>/`，可由 `RESTOCK_COLLECTION_DIR` 修改存储根目录。每份正式包包括：

- `sales.csv`、`inventory.csv`、`suppliers.csv`：UTF-8，固定字段顺序、完整业务表。
- `manifest.json`：schema、source/merchant/warehouse/environment、dataset 身份、采集批次、采集/源数据/发布时间、窗口、口径和每份 CSV 的 SHA256/字节数/行数。

`dataset_id/hash` 沿用现有规范快照；数据库快照作为同一身份的计算缓存，正式 CSV 的字节指纹与清单独立核对。任务/批准/执行账本继续留在原数据库，不靠 CSV 替代事务状态。任务引用的是不可变版本，禁止用一个可随时覆盖的 `latest.csv` 作为批准依据。

服务约每 5 秒检查已到期来源的保存周期；这不是浏览器采集频率。完整文件先写入独立暂存目录，再整体改名为正式目录；数据库数据集、发布索引和来源指针在同一事务生效。若文件已落盘但事务失败，下个周期校验同一包及原采集记录后登记；不覆盖旧包。未登记/半写入目录不能被下载入口当作正式版本。失败可见且有下一次检查时间；无内容变化不再写一份正式数据。

### 接口与使用边界

| 入口 | 行为 |
|---|---|
| `GET /data-sources`、`GET /data-sources/{id}` | 来源、周期、暂存/时间/错误及数据可用状态 |
| `PUT /data-sources/{id}/settings` | 更新独立周期，保留来源身份与数据 |
| `POST /data-sources/{id}/publish` | 检查并执行已到期的保存；未到周期返回 409，不绕过频率 |
| `GET /data-sources/{id}/publications` | 正式版本清单，按内容去重，内容恢复到旧值可复用旧版本 |
| `GET /data-sources/{id}/publications/{dataset_id}/{filename}` | 下载上述四个固定文件之一；路径、来源及完整包指纹全部核验 |

来源写接口要求现有 `X-Restock-Approval` 工作台凭证；目前这是人工/可信后端的管理入口，不是产品 Agent 工具。采集器通过后端绑定 source_id/run_id 的限范围会话接入，不把人工批准凭证交给模型。新增 inspect_source/read_source_section/read_source_product/finish_source_collection 四个受限 MCP 工具；Planner/Reviser/Executor 仍不可调用它们，采集阶段也不能调用原业务工具。通用 shell、原生 computer/browser call 与任意脚本继续拒绝。

`POST /tasks` 新增可选 `source_id`。使用来源发布的数据时必须明确绑定其 source，不能删除此参数绕过检查。绑定任务在候选发布、批准及执行开始时复核最新有效来源；新数据即使待保存也阻止使用旧版本。有效内容变化会撤销无执行尝试的旧批准；只读历史和核对继续可用。已有执行尝试的任务保持固定快照；数据不再有效时恢复写入也会阻止，不能换版本绕过未知订单。

本阶段不会在采集/保存后自动创建或修订补货任务。工作台的“选择本版用于补货”是用户明确选择，后续生成/修订与批准仍经过原流程；按补货检查周期自动触发属于 M5-2。

### 已实现的页面适配与采集授权

来源可选 `browser`：`adapter=merchant-test-v1`、`base_url`、`merchant_name`、`credential_profile=demo/environment`、`sku_scope`、`enabled`。只允许 `environment=test` 和 `http://127.0.0.1:端口`；不接受 URL 内账号、查询串、路径或外部站点。门店使用来源的 `merchant_id`；仓库和商户全称须与可见页面精确匹配。来源身份、范围和映射创建后固定；变更范围须建立新来源。

- `POST /data-sources/{id}/collect`：有凭证才可排队，202；已有任务或暂停则 409。
- `PUT /data-sources/{id}/collection-enabled`：严格布尔 `enabled`，暂停撤销排队/运行会话的提交权；恢复安排下一轮。
- `GET /data-sources/{id}/collection-runs`：最近 20 次的状态、模型时长、脱敏工具输入/结果/拒绝原因；后台按 run_id 精确查询完整历史，不受该展示分页影响。

浏览器为每轮独立无头 Chromium，无个人配置。只读取可见 DOM 文字及控件；关闭页面脚本和 Service Worker，网络层限定来源、只读 GET 路径及查询参数，POST 只在后端登录期间允许 `/login`。模型没有 URL、任意选择器、脚本、提交表单或凭证输入工具。截图仅供审计，不是模型视觉输入。页面接口、源码、隐藏属性、测试夹具和服务器状态均不是产品取数渠道；测试侧独立访问夹具作为核对标准。

映射版本 `merchant-test-v1`：按可见数据截止日读取完整 28 天净销量（成交减退款）；库存可用先核对现存减预占，再按页面标注盒/件换算；成本须明确含税含运且有值，元精确换算整数分。在途只计同仓“已订货/在途”，排除申请/已到货；未知状态、汇总不一致、逾期或多个到货日无法表达时拒绝。此历史模拟适配的 ETA 以 `source_as_of` 的业务日为参照并写入 manifest；真实平台必须另行确认规划时点，不能照搬该模拟假设。

正式清单增加 `collection` 来源说明：适配版本、明确 SKU 范围、run_id、数量口径与 ETA 参照。先成功读完四类页面并通过数值校验，再等待模型正常完成和后台授权复核后暂存；工具完成不等于已发布。模型拒绝、超时、越权、暂停或重启失效均不能生成新可用版本。读取中断保留审计，但不会把部分页面续接成完整批次；下一次从头读取。

时间比较统一 UTC，但清单保留源数据时区，窗口截止日按源业务日核对。来源功能加入前的手动任务不会因内容 hash 恰好一致被追溯绑定，只有显式 source_id 才受该来源后续变化约束。
