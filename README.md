# Pico

Pico 是一个轻量、本地、单协议的多 Agent Coding Runtime。模型只负责通过
OpenAI-compatible Responses 原生 function calling 提出下一步动作；上下文、权限、
工具执行、恢复、验证和持久化均由 Runtime 持有。

项目主动不做多 Provider、XML 工具协议、Skills、MCP、分布式 Worker 和旧状态兼容。

## 从这里开始

唯一权威学习导航见 [`docs/learning-path.md`](docs/learning-path.md)，并保留原始 Day 1～7 顺序。
Day 1 从真实 CLI 请求进入；第一遍只学习 Core：Runtime、AgentLoop、ToolRuntime、RunLog、
RunProjection 与 CompletionController。Project Memory 和 RepoMap 始终默认启用，但到 Day 5
再深入；Semantic Compaction 属于 Context Pressure，Subagents 属于 Orchestration Appendix，
Triage/Evals 属于 Applications。不要为了学习顺序关闭功能或新增开关。

## 当前三条执行路径

### 1. CLI 启动与恢复

```text
pico CLI
  -> build_agent() 构造 Pico、默认 Project Memory 与 RepoMap
  -> Pico.__init__ 通过 load_resumable_run 读取 Session.active_run_id；无指针时查找孤立的未完成 Run
  -> 新 Run：RunLifecycle 先写 user_message + TaskContract，再写 Session 指针
  -> 恢复 Run：RunStore.load_run 单次读取并校验 Run Log v15、终态 Artifact，重放全部 Fact
  -> 若尾部存在未完成 Tool Call：RunLog.reconcile_interrupted() 追加确定性结果
  -> 写 run_started / run_resumed，重置 Provider action session
  -> AgentLoop
```

同一 `Pico` 请求若因未处理异常退出，会先用 durable Run Log 替换可能存在提交歧义的内存快照；若该读取暂时失败，`ActiveRunState.reload_required` 保持为真，下一次 `ask()` 必须先重试读取，不能直接沿用旧 Projection。首次 Session 指针写失败时，恢复请求也会在写任何 `run_resumed` Fact 或调用 Provider 前修复指针。

### 2. 普通工具轮

```text
AgentLoop 请求 Provider
  -> Responses 输出恰好一个 function_call
  -> 解析为 ModelAction.tool
  -> 先写 assistant_tool_call Fact
  -> ToolRuntime：Registry / Surface / Schema / Policy / Approval、协议、重复调用、影响范围与 preimage
  -> tool_execution.py 私有纯函数：Preview、脱敏、Drift/Diff/Transition 与结果分类
  -> 先 fsync tool_started Fact
  -> ToolContext 向当前 Tool Runner 提供 Workspace、Store、Sandbox 与 Run 身份
  -> Tool Runner 返回 ToolRunnerResult
  -> ToolRuntime 使用纯函数结果归一化为 ToolOutcome
  -> fsync tool_result Fact，并更新 RunProjection / RunEvidence
  -> 将有界 ToolOutcome 回写 Provider
```

### 3. 最终提交

```text
ModelAction.final
  -> CompletionController 检查 TaskContract、Subagent、Evidence 与 Verifier
  -> 未通过：写 model_instruction + completion_blocked，继续 Provider
  -> 通过：RunLifecycle 生成最终净 Diff receipt
  -> 写 assistant_final Fact，并由 RunProjection 投影为终态
  -> 清空 Session.active_run_id 与 ExecutionContext

受控停止
  -> RunLifecycle 写 run_stopped + final_diff receipt
  -> 若 Workspace 已漂移，receipt 明确记录 unavailable_reason=workspace_drift
```

这里固定使用以下术语：

- **Fact**：已接受并持久化到 `events.jsonl` 的 Run Event，例如 Tool Call、
  `tool_started`、Tool Result、Verification 与终态。Workspace、Memory Card 和
  Artifact 内容仍由各自 Store 持有，不叫 Run Fact。
- **Projection**：`RunProjection` 对 Fact 的内存归约结果，可重建 Task、Evidence、
  Metrics、单 Pending Call 与 Final Diff receipt；Live 只调用 `apply_event`；持久 Run
  由 `RunStore.load_run` 返回同一次读取的 Events 与 Projection，`RunStore.replay` 委托它
  并只返回 Projection；已有完整 Event 序列才使用 `replay_events`。Projection 不是第二份
  持久化状态。
- **Evidence**：`RunEvidence` 从 Tool Result 和 Verification Fact 派生的 Observation、
  Side Effect、RunChangeSet 与验证记录。
- **Completion**：`CompletionController` 基于 TaskContract、Evidence 和当前 Workspace
  状态作出的“允许最终提交或继续工作”决策；只有随后写入终态 Fact 才算 Run 已结束。
- **Tool runtime**：当前公开边界是 `ToolRuntime`；它通过 `tool_execution.py` 的私有纯函数、
  helpers、`ToolContext` 和具体 Tool Runner 完成一次工具事务，不持有第二份持久状态。

## Runtime 对象边界

`Pico` 只保留八个顶层组件，不再把配置、当前 Run、Workspace 缓存和工具状态
平铺成几十个属性：

```text
Pico
  model_client   模型通信
  config         不变式、资源限额和工具策略
  workspace      路径边界与按路径内容状态
  session        Session 身份与 active_run_id 指针
  run            当前或最近一次 Run 的可变状态
  dependencies   Store、Sandbox、RepoMap 和 Subagents
  tools          工具 Registry、模型 Surface、准入与执行
  prompt         稳定 instructions、动态 ContextManager 和相关记忆选择
```

程序化构造必须显式使用 `PicoConfig`；旧的平铺构造参数和
`current_task_state`、`run_tool()` 等转发属性不再保留：

```python
from pathlib import Path

from pico import Pico, PicoConfig, SessionStore, WorkspaceContext

repo = Path("/path/to/repo")
agent = Pico(
    model_client=model_client,
    workspace=WorkspaceContext.build(repo),
    session_store=SessionStore(repo / ".pico" / "sessions"),
    config=PicoConfig(approval_policy="auto", max_tool_executions=40),
)

outcome = agent.ask(
    "inspect the failing test",
    task_kind="read_only",
    requires_workspace_change=False,
    requires_verification=False,
)
answer = outcome.answer
state = agent.run.task
```

`ask()` 只返回终态 `RunOutcome`：包含 `run_id`、`status`、`answer`、
`stop_reason`、`final_diff` 和一份独立的 Metrics 快照。它是调用方视图，不写回
Run Log；需要审计时仍以 `RunStore.replay(outcome.run_id)` 为事实源。

单次请求的控制逻辑也按职责分开：`AgentLoop` 只编排模型轮次、工具轮次和
Provider 续接；`RunLifecycle` 负责创建/恢复 Run 和 Run Log 终态；
`CompletionController` 负责 TaskContract、Subagent、Verifier 与 Completion Gate。

当前工具轮已经收口到 `ToolRuntime` 公开边界：`AgentLoop` 只提交 `ToolCall` 并消费
`ToolOutcome`，工具协议、准入、执行和 Fact 写入由 `ToolRuntime` 协调，纯值计算下沉到
私有 `tool_execution.py`。最终提交仍由 `CompletionController` 判断、
`RunLifecycle` 持久化。

核心实现：

- 原生 function calling：稳定 Runtime 规则进入 Responses `instructions`，动态 Workspace、TaskContract、WorkingState、Memory、RepoMap、History 与当前请求进入 `input`，严格 Function Schema 只进入 `tools`。一次响应只接受一个函数调用；Provider、Run Protocol 和 Projection 都只保存一个 `pending_call_id`。
- Run 流程事实源：每个 Run 只有一个 strict、连续 sequence、append-only、fsynced 的 `events.jsonl`。首个 User Event 保存 Runtime-owned `TaskContract`；实时执行与恢复共用一个 `RunProjection`，统一重建 Task、Evidence、Metrics 和 Pending Call。终态只额外保存最终 `final_diff` receipt，不保存第二份终态报告。
- 长上下文治理：Prompt 使用配置的 Context Window 和固定 section caps，History 获得剩余预算。显式 `prepare_compaction` 在只读 Prompt build 前运行；独立模型 Session 只总结历史 Progress 与 Critical Context，不复述 TaskContract 或 WorkingState。OpenAI-compatible Adapter 将结构化 Context Overflow 归一化为 `ProviderContextOverflow`；AgentLoop 只对该类型重建一次 Session/Prompt，连续第二次直接抛出，不再匹配错误字符串。Provider、结构或长度失败时不提交 Compaction Event，Runtime 使用近期完整 Tool 事务的有界投影继续。
- Workspace 新鲜度：文件工具返回真实 Unified Diff 和 before/after path transitions。每个路径只在本 Run 第一次修改前保存完整 preimage；`RunChangeSet` 计算最终净变化，因此 `A -> B -> A` 不算完成所需的修改。外部漂移会阻止成功提交；用户取消或重置仍可受控停止，并以 `unavailable_reason=workspace_drift` 明确说明此时无法生成可信 Final Diff。Verifier 只保存命令结果及其 mutation/path states，current/stale 在查询时派生。
- RepoMap：基于 tree-sitter 构建 Python symbol/reference graph，以 lexical + personalized PageRank 在 Token 预算内返回任务相关签名。
- 分层状态：`TaskContract` 保存目标、任务类型、写入范围与完成要求，模型不能修改；WorkingState 继续使用原有 add/remove 增量协议，只保存约束、已确认决定和下一步。Project Memory 与 RepoMap 保持默认开启。Memory Catalog 在初始化、store、forget 或显式 refresh 时重建，Prompt build 不写磁盘。
- 安全工具：路径锚定 Workspace，通用文件工具不能访问 `.git/` 或 `.pico/`；Manual 模式只允许观察工具，mutation 必须属于 Active Run。父级 `delegate_tasks` 的 Implement scope 与 `apply_task_patches` 的计划路径同样必须落在 TaskContract/当前 Config 的有效写入交集内，并在 Approval 与集成前拒绝越界。`FailureInfo.recovery` 是唯一持久化纠错事实，模型可见 correction action 由它派生。Repeat cache 是进程内避免连续盲目重放相同 `partial/unknown` 调用的辅助；持久安全仍由 create-only、Revision、原子 Memory/Workspace 写入和 Patch 状态负责。`write_file` 只创建 absent 文件，`edit_file` 只修改 existing 文件并携带 `read_file` Revision；提交点再次复验 Revision 后原子替换。
- 最小 Shell 环境：未配置时使用默认白名单；显式空白名单保持为空，仅由 Shell 边界补充运行必需的 `PWD`/`PATH`。
- 大输出：模型直接获得一次 12 KiB 预览（shell 为尾部 16 KiB）；完整、脱敏输出写入不可变 artifact，截断结果可通过当前 run 限定的 `read_artifact` 按 8 KiB 字节页继续读取。
- 隔离执行：`run_shell` 在临时 Docker 容器内通过 POSIX Shell 执行，支持 `&&`、管道、重定向、环境变量与 `cd`；inspect/verify Profile 均禁网、只读 rootfs 与 Workspace，并施加 cap-drop、进程/CPU/内存/输出限制和整轮 deadline。
- 崩溃恢复：Session 只保存 `active_run_id`。副作用前先 fsync `tool_started` 及精确潜在影响路径/before revision；结果完成后 fsync `tool_result`。恢复时若二者不配对，只检查声明路径并生成 interrupted error/partial，绝不盲目重放。最后一条未完成 JSONL 尾巴可截断，中间损坏直接报错。
- 恢复配置：模型、预算、超时、Verifier、工具表面和权限策略均使用续跑时的当前配置，不冻结为 Resume identity。
- 完成证据：`CompletionController` 是唯一完成决策者，依次检查 Subagent、TaskContract、未修复 unknown/partial、Workspace drift、当前 Verification，以及验证后仍未解决的 Effect。未修复的不确定副作用不会触发无意义的 verifier；修复后的 Workspace partial 必须验证当前代码，修复后的 Project Memory partial 不运行 Workspace verifier。`RunEvidence` 只提供 repair/effect/verification 事实查询。没有通用 Python AST 隐式 Gate，也不会自动猜验证命令。

## 多 Agent 协作

Parent 通过两个原生工具编排独立 Pico Child：

- `delegate_tasks`：提交显式依赖 DAG；无依赖任务最多三个并行执行。
- `apply_task_patches`：由独立 `PatchIntegrator` 在临时 Integration Worktree 中按拓扑顺序检查并应用 Patch，验证通过且 Parent HEAD/工作区未变化后再写回原工作区。

Explore Child 共享同一源码快照但只有只读工具，并以包含精确路径、行号和关键片段的
证据交接替代完整工具历史。Implement Child 使用独立 Git
Worktree、独立 Model Client、Session、Run Log 和 Artifact namespace；
写操作在 ToolRuntime 准入阶段受精确文件白名单限制，任务结束后再以实际 Git Diff
复核一次。无依赖且写文件重叠的实现任务会在规划阶段被拒绝；有依赖的实现任务会把
上游 Patch 作为临时基线后继续工作。

应用预计需要超过 3 次 Search/List/Read 时才派 Explore；一次委派不能混合 Explore 与
Implement。Parent 先消费 Explore 交接并合成规格，再单独派 Implement，成功后必须通过
`apply_task_patches` 集成。每个 Child 最多执行 12 个 Tool，避免同步父子流程无限扩散。

Parent 只接收 Child 的摘要、Run Log receipt 和 Patch 引用，不复制 Child 的工具历史。
相同 Parent Run 中已完成的 Task ID 会复用现有结果；失败任务只阻断依赖它的下游分支。
子任务 DAG 和应用状态只属于当前 Parent 进程；Child Run Log 和 Patch
作为审计工件保留，但不尝试跨 CLI 恢复调度或 Patch 集成事务。
当前实现刻意不做后台 Mailbox、跨 CLI 重启恢复 Child、多级递归 Agent 或分布式调度。

## 安装与运行

需要 Python 3.10+、uv；使用 `run_shell` 还需要 Docker。

```bash
uv sync
docker build -f docker/sandbox.Dockerfile -t pico/sandbox:latest .
uv run pico --task-kind read_only --cwd /path/to/repo
uv run pico --task-kind modify --require-verification \
  --verify-command "python -m pytest -q" \
  "inspect the failing test and implement a verified fix"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

`right.codes` 是 Pico 预期使用的默认 OpenAI-compatible Gateway；如需连接其他
Responses Endpoint，必须显式覆盖 `PICO_OPENAI_API_BASE` 或传入 `--base-url`。

常用运行参数：

```bash
uv run pico --task-kind read_only --approval ask
uv run pico --task-kind modify --approval deny
uv run pico --task-kind read_only --run-timeout 600
uv run pico --task-kind read_only --max-tool-executions 40
uv run pico --task-kind read_only --max-new-tokens 1024
uv run pico --task-kind read_only --provider-context-limit 272000
uv run pico --task-kind read_only --provider-context-limit 272000 --compaction-reserve-tokens 16384 --compaction-keep-recent-tokens 20000
uv run pico --task-kind modify --require-verification --verify-command "python -m pytest -q"
uv run pico --task-kind modify --require-verification --verify-command "python -m pytest -q" --resume latest
uv run pico run show <run_id> --cwd /path/to/repo
uv run pico run events <run_id> --cwd /path/to/repo
```

Risky Tool 的 Approval Policy 语义为：`ask` 交互确认、`auto` 自动批准、`deny`
拒绝执行。`deny` 返回结构化 rejected ToolOutcome，不会启动对应 Tool Runner。

Pico 不根据仓库文件猜验证命令。任务设置 `--require-verification` 时必须显式提供
`--verify-command`；Completion Gate 在最终提交时运行它并绑定当前 mutation/path states。

`--max-new-tokens` 默认值为 `1024`。它传给 Responses API 的 `max_output_tokens`，统计 reasoning tokens、
可见文本和 function-call arguments 的总输出，而不只是最终展示文本。

## 状态与工件

```text
.pico/
  sessions/<session_id>.json
  memory/
    MEMORY.md
    cards/<memory_type>_<name>.md
  runs/<run_id>/
    events.jsonl
    artifacts/*
    subagents/<task_id>/
      sessions/*
      runs/*
      patch.diff
```

所有 schema 都严格校验；当前版本不会迁移旧 XML、旧版 Run 事件文件、`context.jsonl`、Checkpoint 或旧 Session，
也不会在初始化时自动删除这些旧数据。

## 评测

```bash
uv run python scripts/run_evaluations.py
uv run python scripts/run_triage_evaluation.py
uv run python scripts/run_real_compaction.py --base-url <openai-compatible-base>
uv run pytest -q
uv run ruff check pico applications tests scripts evals
```

评测分为 native Harness regression、Context governance、Markdown Project Memory 和
RepoMap。它们衡量 Runtime 机制，不冒充真实模型能力指标。
评测实现位于仓库顶层 `evals/`，不属于 `pyproject.toml` 打包的 `pico` Runtime。

当前确定性 Artifact 为 `harness-regression.json`、`context-governance.json`、
`project-memory.json`、`repo-map.json` 与 `triage-evaluation.json`。

当前随仓库发布的证据：

| 层级 | 结果 | 说明 |
|---|---:|---|
| Python tests | 全部通过 | Runtime contracts、恢复、安全、上下文与工具边界 |
| Native Harness | 5/5 | edit、recovery、safety、governance；失败时脚本非零退出 |
| Context governance | 3/3 | 真实 ContextManager/RunLog Compaction；三个上下文规模下预算、事务、原事件与 WorkingState 均保持 |
| Real Compaction | 1/1 | 真实 `gpt-5.6-luna` 在受控 32k 窗口触发 Session Reset + Compaction，继续完成单次 Patch 与 Hidden Verifier |
| Project Memory | 全部通过 | 真实 `memory_store -> memory_recall -> final` Tool 事务与不可信数据边界 |
| RepoMap | 全部通过 | tree-sitter 图、任务命中与 Token 预算 |
| Pico Triage | 3/3 | 真实失败命令复现、责任文件定位、Patch 与 Verification 闭环 |
| Real Click Triage | 1/1 | `gpt-5.6-luna`；可见测试与停止后 Hidden Verifier 均通过，只修改 `src/click/utils.py` |
| Real Packaging Triage | 1/1 | `gpt-5.6-luna`；6 个可见测试与 Hidden Verifier 均通过，Root Cause Top-1/Top-3 命中 |
| Real urllib3 Triage | 1/1 | `gpt-5.6-luna`；Explore → Parent 合成 → Implement → Patch 集成完成两文件修复，Hidden Verifier 通过 |

真实 Compaction Artifact 见 `artifacts/real-compaction.json` 与
`artifacts/real-compaction.patch`。模型按顺序各读取一次 12 份证据，最大单次 Input 为
31,713 Token；Runtime 在估算下一轮达到 33,662 Token 时重建 Provider Session，将旧的
13 个事件摘要为 624 Token，并保留 7,732 Token 近期事务。Compaction 后 Goal、Constraints、
Decisions 与 Next Steps 仍在，模型只执行一次 Mutation，最终可见与停止后 Hidden Verifier
均通过。该测试通过降低Runtime配置窗口来验证真实LLM续接，不冒充自然消耗272k上下文，
也不代表已经验证跨进程Resume。

## Pico Triage

`applications/triage` 是直接建立在 Pico Runtime 上的 CI 故障诊断应用层。它没有第二套
Agent Loop、状态机、日志或 Patch 应用器：当前诊断计划复用 WorkingState，执行事实继续写入
Pico Run Log，修复与验证复用现有 Tool、Subagent Worktree、PatchIntegrator 和 Completion Gate。

输入 Case 是一个严格 JSON 对象，包含仓库、失败版本、失败命令和不可信 CI 日志：

```json
{
  "incident_id": "login-timeout-ci",
  "repository_root": "/path/to/repo",
  "revision": "<commit>",
  "failing_command": "python -m pytest -q tests/test_login.py",
  "ci_log": "1 failed ...",
  "constraints": ["Do not change the database schema"]
}
```

运行：

```bash
uv run python scripts/run_triage.py case.json --output triage-report.json
```

最终 `TriageReport` 验证每条证据都引用当前 Run 中真实完成的 Tool Call；复现状态、修改路径、
Verification 和工具步数由 Run Log 与 RunEvidence 确定性派生，而不是由模型自行声明。

第一个真实模型案例复用冻结的 Click Pre-fix Fixture，将上游失败测试提交为干净的 Git
baseline，并在 Agent 停止后才注入 Hidden Verifier：

```bash
uv run python scripts/run_real_triage.py \
  --task click_empty_bytes_echo \
  --model gpt-5.6-luna
```

当前真实运行绑定 Runtime commit `599d9a8`，模型用 9 个 Tool Call 定位并修复
空 bytes 被错误替换为文本空字符串的问题；复现、可见测试、Hidden Verifier 和修改范围检查全部通过。
结构化证据见 `artifacts/triage-click-real.json`，实际 Patch 见
`artifacts/triage-click-real.patch`。

第二个真实案例 `packaging_non_string_version` 绑定 Runtime commit `b149260`。模型复现 6 个
非字符串版本失败，用 9 个 Tool Call 定位 `Version.__init__` 的字符串类型假设，只修改
`src/packaging/version.py`；可见测试、Hidden Verifier、Root Cause Top-1/Top-3 和范围检查
全部通过。证据见 `artifacts/triage-packaging-real.json` 与
`artifacts/triage-packaging-real.patch`。

第三个真实案例 `urllib3_port_zero` 绑定 Runtime commit `19d8793`，使用两阶段父子编排：
Explore Child 隔离源码调查，Parent 合成两文件实现规格，Implement Child 在独立 Worktree
完成修改，Parent 通过 `apply_task_patches` 集成。Parent 没有重复 Patch；两个 Child 与
Parent 均完成，16 个可见测试和停止后 Hidden Verifier 通过。证据见
`artifacts/triage-urllib3-real.json` 与 `artifacts/triage-urllib3-real.patch`。

最新真实运行分别统计 Parent、Children 和 Total，避免把 Child 成本遗漏在多 Agent 结果之外：

| Case | Parent Tools | Child Tools / Runs | Wall Time | Parent Uncached | Total Uncached |
|---|---:|---:|---:|---:|---:|
| Click | 9 | 0 / 0 | 97.5s | 20.4k | 20.4k |
| Packaging | 9 | 0 / 0 | 86.2s | 24.9k | 24.9k |
| urllib3 | 11 | 16 / 2 | 343.6s | 68.5k | 247.0k |

Packaging 相比旧证据从 12 个 Tool Call、113 秒降到 9 个、86 秒，Uncached Input 从
194.5k 降到 24.9k。urllib3 的当前多 Agent Run 将 Parent Tool 控制为 11 个，但两个
Child 使总 Tool 达到 27 个、Total Uncached 达到 247.0k，Wall Time 为 344 秒。该 Run
切换了 Provider 路由，不能作为严格的单/多 Agent 性能 A/B；它证明的是当前隔离、交接、
Worktree、Patch 集成和 Verifier 链路端到端可用，同时也如实显示父子型编排保护 Parent
上下文但不保证降低总成本或延迟。

三个真实Triage Fixture可以从精确上游Commit重新物化：

```bash
uv run python scripts/materialize_real_oss.py --replace
docker build -f docker/real-oss-suite.Dockerfile -t pico/real-oss-suite:latest .
docker build -f docker/official-public-tests.Dockerfile -t pico/official-public-tests:latest .
```

`run_real_triage.py`强制要求clean worktree，在Agent运行前证明公开回归测试失败，停止后再运行Hidden Verifier，并把Runtime commit、上游commit、Patch范围及Parent/Child/Total成本写入当前三份真实证据。

面试展示顺序见 [`docs/review-pack/interview-demo.md`](docs/review-pack/interview-demo.md)。

项目定位是本地单用户 Agent Runtime，不是模型网关、多租户平台或通用工作流引擎。
