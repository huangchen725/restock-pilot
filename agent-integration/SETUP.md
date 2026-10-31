# Codex + 书生·端砚 接入操作手册（Step 1–3）

> 以下是 2026-10-28/29 的历史排障记录，不是当前产品启动规程。
> 当前入口与验收见 [README](../README.md) 和 [ENGINEERING_PLAN](../ENGINEERING_PLAN.md)。
> M2 由后端动态创建临时 CODEX_HOME、短期工具凭证和模型工具协议网关；CLI 只读运行，
> MCP 只计算候选，不直接访问业务数据库。历史 danger-full-access、手动 CODEX_HOME 和
> 环境变量配置示例不能用于当前产品任务；不得按旧排障结论关闭新边界。

日期：2026-10-28 ｜ 距兴智杯交件截止（11/5 23:59）剩 8 天

## 已完成的验证（本机实测）

### 主链路：书生·端砚 TokenPlan（兴智杯正主，正式提交用这套）

- 端点 `https://discovery-api.intern-ai.org.cn/v1`，模型 `intern-s2`（端砚共 13 个模型）
- T1 单工具调用 PASS / T2 工具结果回填 PASS / T3 流式分片拼装 PASS
- **`/v1/responses` 探测**：明确返回 "intern-s2 does not support openai_responses protocol"
  → 端砚只有 Chat Completions 协议，**LiteLLM 转换层为必选项**（Codex 只说 Responses）
- 配额：墨点余额 1000 万 token；RPM 50（生成模型）/ 200（决策模型）；TPM 2M

### 备用链路：挑战杯端点（调试期兜底）

- 端点 `https://chat.intern-ai.org.cn/api/v1`，模型名 `intern-s2-preview`（实际 Intern-S2-Preview-35B）
- T1/T2/T3 三项同样全 PASS；注意该 key 在书生官网域名报 scope forbidden
- 挑战杯 key RPM 30 限流较紧，仅作备用

### 源码结论

- 最新版 Codex `WireApi` 枚举仅有 `Responses`（无 chat 选项）→ 无直连可能
- 源码位置：`codex-reference/`（codeload tarball，main 分支）
  - exec CLI：`codex-rs/exec/src/cli.rs`（`--json` 别名 `--experimental-json`，`--output-schema` 约束最终输出）
  - JSONL 事件输出：`codex-rs/exec/src/event_processor_with_jsonl_output.rs`
  - provider 配置语法：`codex-rs/cli/tests/doctor_enterprise_network.rs` 等测试用例

## 安装步骤

```powershell
# 1. 安装 Codex CLI（Node 22 已具备）
npm install -g @openai/codex

# 2. 安装 LiteLLM（含 proxy）
pip install "litellm[proxy]"

# 3. 设置环境变量（每次开新终端都要，建议写进 profile 或 .env）
$env:INTERN_API_KEY = "端砚key"
```

## 启动与验证（按顺序）

```powershell
# 4. 启动 LiteLLM 代理（保持运行）
litellm --config agent-integration\litellm-config.yaml --port 4000

# 5. 健康检查：应返回模型列表
curl http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $env:INTERN_API_KEY"

# 6. 项目专用 CODEX_HOME 指向本目录配置
$env:CODEX_HOME = Join-Path (Get-Location) "agent-integration"
# 这里从项目根目录手动运行 CLI；主服务的 runner 会自动为子进程设置此目录。
# （把 codex-config.toml 复制/改名为该目录下的 config.toml）

# 7. Day 1 验收：最小工具调用链（不是"能聊天"）
# 注意三个坑（均已踩过并验证）：
#   a) 必须加 --skip-git-repo-check（非 git 仓库目录会被拒）
#   b) 必须重定向 stdin（codex exec 会等输入：< /dev/null 或 NUL）
#   c) sandbox_mode 必须是 danger-full-access（Windows 的 workspace-write
#      沙箱拦截所有 powershell/cmd 调用，连 echo 都 blocked by policy）
codex exec --json --skip-git-repo-check "统计当前目录下 .py 文件的数量，然后把结果写入 result.json" < /dev/null
# 验收标准：JSONL 事件流中出现工具调用事件（exec_command/shell）→ 读取结果 → 最终输出可解析

# 8. 环境就绪后的常备组合（一次会话跑多个任务）
#    LiteLLM 后台进程：litellm --config agent-integration\litellm-config.yaml --port 4000
#    CODEX_HOME / INTERN_API_KEY 环境变量设置后即可反复 codex exec
```

## Day 1 验收结果（2026-10-29 00:35，已通过）

- 任务：统计 .py 文件数量并写入 result.json
- 事件流：reasoning → **command_execution #1（数文件，exit 0，输出 "2"）** → reasoning
  → **command_execution #2（写文件，exit 0）** → 最终 message（答案正确）
- result.json = `{"py_file_count": 2}`，与实际一致（a.py + b.py）
- 结论：**Codex(Responses) → LiteLLM(桥接) → 端砚 intern-s2(Chat Completions) 的
  工具调用闭环全通，原 P0 风险（桥接保真度）关闭**

## 踩坑记录（按出现顺序）

| 坑 | 现象 | 解法 |
|---|---|---|
| TOML 顶层键位置 | sandbox/approval 写在 [table] 段之后会被解析进上一个 table，静默失效 | 顶层键全部放在文件头部、任何 [section] 之前 |
| Windows 沙箱拦截 | workspace-write 模式下所有 powershell/cmd 被 "blocked by policy" | `sandbox_mode = "danger-full-access"`（本地开发期） |
| git 仓库检查 | "Not inside a trusted directory" 拒绝运行 | 加 `--skip-git-repo-check` |
| stdin 挂起 | "Reading additional input from stdin..." 等待输入导致脚本卡死 | `< /dev/null` 重定向 |
| Model metadata 警告 | "Model metadata for intern-s2 not found" 降级为 fallback 元数据 | config 里显式 `model_context_window = 262144` |
| pip 装不上（环境侧） | pip 走 Windows 系统代理（注册表 127.0.0.1）但代理未开 → "versions: none" | 开本地代理后重试即可 |
| 命令行太长（Step 3 实测） | 长 prompt 走 argv 传 `codex.cmd exec` 时 exit=1，stderr 乱码（GBK "命令行太长"）；cmd.exe 对 .cmd 批处理有 8191 字符上限 | runner 改为 prompt 写临时文件、`codex exec -` 从 stdin 读入（见 app/runner.py） |
| 沙箱代理劫持本地 httpx（Step 3 实测） | 环境变量 HTTP_PROXY 指向沙箱代理，httpx 调本地服务出现"首请求成功、后续请求 404"的诡异现象，curl 正常 | 本地服务 httpx 一律 `trust_env=False`；Playwright 启动加 `--no-proxy-server` |

## 风险清单（更新）

| 级别 | 风险 | 对策 |
|---|---|---|
| ~~P0~~ | ~~LiteLLM Responses 桥接保真度~~ | **已关闭**：Day 1 验收通过 |
| P1 | 端砚 RPM 50（生成模型），codex exec 循环密集调用可能触发 | 备用挑战杯端点分流；如仍不够，端砚墨点额度内可申请提升或邮件 changshuai@pjlab.org.cn |
| P1 | max_tokens 上限 8192 硬截断 | LiteLLM 已配 max_tokens=8192；关注 finish_reason=length |
| P1 | danger-full-access 无沙箱防护 | 演示环境可接受；正式提交如需收紧，考虑 WSL 或限制工作目录 |
| P2 | 两把 key 均已在聊天中明文暴露 | 提交作品前在各自控制台吊销重生成 |

## 接入打通后的下一步

1. mock 骨架（FastAPI + SQLite + 状态机 + 工具层纯函数）
2. runner.py：封装 codex exec 子进程（启动/读 JSONL/超时终止/结果校验）
3. 三阶段 prompt 模板（Planner / Reviser / Executor）
