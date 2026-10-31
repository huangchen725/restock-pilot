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

人工批准凭证由统一启动入口注入工作台和 API，不注入后台 CLI。M2 另通过临时模型网关限制可执行工具、通过操作会话绑定任务/阶段/数据；CLI/MCP 不继承数据库配置或上游密钥。边界是工具能力隔离，不是独立 OS 用户或容器。当前队列限定单 API 进程；启动时把上次执行中断标为 NEEDS_RECONCILIATION，不自动重新提交采购。

首版 Policy 只支持预算、覆盖天数、安全天数、优先类别和排除 SKU。预算必须为正整数分，覆盖天数为 7–90，安全天数为 0–30。不能直接修改数量、价格、供应商或设置单 SKU 最低数量。

Reviser 必须返回 `policy`、`explanation`、`unsupported_requests` 三个字段。识别到不支持或不明确的要求时返回 `policy=null` 和非空问题列表；整个修订被拒绝，不部分应用。缺字段、非法 Policy 或问题列表非空均记录为修订错误（`Prefer: wait` 时返回 422），不产生 v+1；旧批准按已有修订语义撤销，仍需重新复核。相同数据和参数的修订复用原版本并成功返回，不能强制要求 v+1。

MCP build_plan / revise_plan 只计算候选，不写 plans。初始生成必须先 inspect_dataset，且不能改动已经确认的结构化 Policy；自然语言修订的最终完整 Policy 必须与工具候选一致。后端提交时再次检查 ANALYZING 状态、操作互斥、原版本/hash；任何失败都不发布候选。PARTIAL / NEEDS_RECONCILIATION 不能通过这些工具变回待批准。每次工具调用按任务保存阶段、操作标识、绑定的数据集、输入、结果及拒绝原因。

Planner 的预算、金额、商品数量和风险交集核对由程序生成，明确标注来源；模型只提供简短非数值建议，原始建议单独保留。金额单位始终是人民币分，展示元时除以 100；到货前缺货风险总数与其中被预算跳过的数量分别统计。

这个协议及拒绝路径已通过模拟模型故障注入；真实书生的支持范围内预算修订已经验证。所有自然语言不支持项的语义识别覆盖仍属 M4 固定场景，不能仅凭 schema 宣称模型永远不会误解。

## 旧方案兼容和验收范围

缺少 `schema_version` 的存量 JSON 按版本 1 加载，审批 hash 只包含旧有字段；新增默认字段不会改变其既有指纹。新方案按版本 2 将快照、规则版本和计算证据一并纳入 hash。

旧方案仍可读取、核验和按原批准执行。旧方案没有历史数据快照时，不从现在的 CSV 猜测原始依据；直接修订被拒绝（同步模式 409）。用户须上传并显式应用新数据，或创建新任务；无论哪种方式均须重新批准。数据库与旧记录不被清空或自动重算。

验收覆盖日期乱序、缺失与重复、编码、数值边界、口径确认、在途时点、预算整包与起订量、版本持久化、旧 hash 兼容，以及新旧方案的真实 Chromium 录单与防重。M1 验证工作台上传到下载、旧批准失效、非阻塞响应、执行互斥、数据库重开及导出安全。M2 整改增加真实 CLI 协议边界、工具拒绝、候选回滚和真实书生预算修订；均使用隔离服务和数据库，详见工程计划。原有 5 项故障注入已修复并通过，延迟落库、未知结果/部分成功恢复及审计完整性仍属 M3 待完成项。
