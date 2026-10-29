# RestockPilot 项目长期记忆

## 赛事
- 第三届兴智杯 · 大模型智能体关键能力技术创新赛（信通院主办、上海AI实验室独家支持）
- 方向③开放赋能智能体；报名已完成；**交件截止 2026-11-05 23:59:59**
- 交付物：ZIP（技术文档 PDF + 代码 + 模型文件）+ ≤5min ≥720P 视频链接 + 应用体验链接
- 主办方邮箱：aiac@caict.ac.cn；API 提额：changshuai@pjlab.org.cn（备注"兴智杯+队名+联系方式"）

## 方案（采纳 A 同学 GPT 方案骨架 + 5 条改进）
- 作品：店铺补货与采购申请 Agent；单店单仓 30–100 SKU 三份 CSV；规则纯函数计算（ceil(d×(L+R+S))），模型只管语义决策
- 架构：Streamlit 界面 + FastAPI 后端（状态机 NEW→ANALYZING→NEEDS_REVIEW→APPROVED→EXECUTING→COMPLETED）+ Codex harness Agent + MCP 业务工具 + Playwright 固定适配器 + 自建演示后台
- 硬规则：approve_plan 不给 Agent；审批绑定 plan_hash，修改即失效；external_ref=plan_id+version+supplier_id 防重；超时先查后交
- 5 条改进待落地：双通道一致性校验、Policy DSL、故障注入评测、执行回放审计账本、SKU 置信分层
- 三阶段包装为 Planner/Reviser/Executor 专职子智能体（多智能体叙事）

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
- Step 4：故障注入评测 + 执行回放审计账本
- Step 5：技术文档 PDF / ≤5min 视频 / 应用体验链接部署（Streamlit 界面可选项）
- 安全：两把 key 聊天明文暴露过（端砚 key 2026-10-29 又暴露一次），11/5 提交前必须吊销换新
