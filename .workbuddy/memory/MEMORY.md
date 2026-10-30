# RestockPilot 项目长期记忆

## 2026-10-30 计划调整

- 后续执行以仓库根目录 [ENGINEERING_PLAN.md](../../ENGINEERING_PLAN.md) 为准，替代旧的 Step 4/5 排序。
- 保留后台 Codex CLI，不抽取源码或重写 Agent 循环；产品用户只使用 RestockPilot 工作台，不操作 Codex 界面或终端。
- 工作台、上传、版本差异与清单报告导出是必交项，与业务工具接入并行推进。
- 当前本机普通回归 47 项通过，8 项默认跳过（6 项浏览器、2 项启动集成）；另行启用的启动测试 13 项通过。此前浏览器验收为正常 E2E 1 项通过、故障注入 5 项失败。
- 2026-10-30 真实 intern-s2 后端链路单次通过，任务 7af1cc26，34 项断言；预算 5000→3000 元，方案 4903.08→2576.04 元，2 笔申请，旧批准失效和重复执行拒绝均正常。Planner/Reviser/Executor 472.5/8.1/94.9 秒，业务工具调用均为 0；Planner 解释偏差及耗时仍待修正。证据在本地 outputs/real-chain-20261030-134716/，不作为产品 UI/MCP 或故障验收完成的依据。
- 根目录 .env 自动加载，已有环境变量优先；统一入口为项目解释器运行 start.py，支持 --check、status、--offline，Ctrl+C 统一停止本次服务。使用测试密钥验证了真实代理与业务服务启动，未调用上游模型。
- 旧 Step 3 记录表示原开发环境的后端阶段验收，不表示产品闭环完成。三阶段称为工作流，不包装未经验证的多智能体协作。

## 赛事
- 第三届兴智杯 · 大模型智能体关键能力技术创新赛（信通院主办、上海AI实验室独家支持）
- 方向③开放赋能智能体；报名已完成；**交件截止 2026-11-05 23:59:59**
- 交付物：ZIP（技术文档 PDF + 代码 + 模型文件）+ ≤5min ≥720P 视频链接 + 应用体验链接
- 主办方邮箱：aiac@caict.ac.cn；API 提额：changshuai@pjlab.org.cn（备注"兴智杯+队名+联系方式"）

## 方案（采纳 A 同学 GPT 方案骨架 + 5 条改进）
- 作品：店铺补货与采购申请 Agent；单店单仓 30–100 SKU 三份 CSV；规则纯函数计算（ceil(d×(L+R+S))），模型只管语义决策
- 架构：Streamlit 界面 + FastAPI 后端（状态机 NEW→ANALYZING→NEEDS_REVIEW→APPROVED→EXECUTING→COMPLETED）+ Codex harness Agent + MCP 业务工具 + Playwright 固定适配器 + 自建演示后台
- 硬规则：approve_plan 不给 Agent；审批绑定 plan_hash，修改即失效；external_ref=plan_id+version+supplier_id 防重；超时先查后交
- 双通道一致性校验、Policy schema、SKU 置信分层已有初版；故障注入与审计实现尚未完成验收，详见当前计划。
- Planner/Reviser/Executor 为三阶段工作流；实际录单由受控适配器执行。

## 接入链路（已全部实测通过 2026-10-29）
- Codex 0.165（npm @openai/codex，位于托管 node workspace）→ LiteLLM 4000 → 端砚 discovery-api.intern-ai.org.cn/v1 的 intern-s2
- 备用：chat.intern-ai.org.cn/api/v1 的 intern-s2-preview（挑战杯 key，RPM 30）
- 端砚配额：墨点 1000 万 token，RPM 50（生成）/200（决策），TPM 2M
- 端砚 /v1/responses 不支持；Codex WireApi 仅 Responses → LiteLLM 必选
- 6 个坑及解法见 agent-integration/SETUP.md 踩坑记录表

## 进度（2026-10-29）
- Step 2 骨架 ✅（规则引擎/状态机/SQLite/runner/FastAPI，14 测试）
- Step 3 ✅ 全部验收：25 单测 + Chromium E2E 幂等两遍 + 真实进程全链路（COMPLETED，防重入 409）+ **真实 Agent 三阶段**（Planner 169s 高质量摘要 / Reviser 56s 输出合法 policy JSON / Executor 真实录单 2/2）
- 新坑已记录：codex.cmd 长 prompt 走 argv 触发 cmd.exe 8191 上限 → runner 改 stdin；沙箱代理劫持本地 httpx → trust_env=False / --no-proxy-server（详见 SETUP.md 踩坑表 8 条）
- key 在 .env（gitignored）

## 剩余 P1 风险
- RPM 50 限流（codex 循环密集调用）；max_tokens 8192 截断；danger-full-access 无沙箱

## 下一步
- M0：固定运行环境、完成迁移验证，修正日期窗口、在途抵扣和预算分配口径。
- M1/M2 并行：工作台、上传、修改差异与报告；真实业务工具、任务进度和批准权限边界。
- M3：修复故障注入、结果未知时的回查、部分成功恢复、执行互斥与审计完整性。
- M4：固定产品场景与真实模型验收、异机复现、技术文档、视频、体验链接及提交包。
- 安全：两把 key 聊天明文暴露过（端砚 key 2026-10-29 又暴露一次），11/5 提交前必须吊销换新
