# Pico

Pico 是一个轻量、本地、单协议的多 Agent Coding Runtime。模型只负责通过
OpenAI-compatible Responses 原生 function calling 提出下一步动作；上下文、权限、
工具执行、恢复、验证和持久化均由 Runtime 持有。

Pico 面向用户已经信任的本地仓库。模型没有通用 `run_shell` 工具；只有用户显式配置的
固定 Verification 命令会在 Workspace 中以当前用户权限本机执行。该命令不受文件工具的
路径约束保护，也没有文件系统、进程或网络隔离。未知仓库、未知 PR 或其他不可信代码必须
放到 Pico 外部的 CI、VM 或容器中运行。

项目主动不做多 Provider、XML 工具协议、Skills、MCP、分布式 Worker 和旧状态兼容。

## 从这里开始

唯一权威学习导航见 [`docs/learning-path.md`](docs/learning-path.md)，并保留原始 Day 1～7 顺序。
Day 1 从真实 CLI 请求进入；第一遍只学习 Core：Runtime、AgentLoop、ToolRuntime、RunLog、
RunProjection 与 CompletionController。RepoMap 默认启用但到 Day 5 再深入；Semantic
Compaction 属于 Context Pressure，Subagents 属于 Orchestration Appendix，
Triage/Evals 属于 Applications。不要为了学习顺序关闭功能或新增开关。

## 当前三条执行路径

### 1. CLI 启动与恢复

```text
pico CLI
  -> build_agent() 构造 Pico 与默认 RepoMap
  -> Pico.__init__ 通过 load_resumable_run 读取 Session.active_run_id；无指针时查找孤立的未完成 Run
  -> 新 Run：RunLifecycle 先写 user_message + TaskContract，再写 Session 指针
  -> 恢复 Run：RunStore.load_run 单次读取并校验 Run Log v15、终态 Artifact，重放全部 Fact
  -> 若尾部存在未完成 Tool Call：RunLog.reconcile_interrupted() 追加确定性结果
  -> 恢复输入先写 user_guidance Fact，再写 run_resumed；新 Run 写 run_started
  -> 重置 Provider action session
  -> AgentLoop
```

同一 `Pico` 请求若因未处理异常退出，会先用 durable Run Log 替换可能存在提交歧义的内存快照；若该读取暂时失败，`ActiveRunState.reload_required` 保持为真，下一次 `ask()` 必须先重试读取，不能直接沿用旧 Projection。首次 Session 指针写失败时，恢复请求也会在写任何 `run_resumed` Fact 或调用 Provider 前修复指针。

### 2. 普通工具轮

```text
AgentLoop 请求 Provider
  -> Responses 输出恰好一个 function_call
  -> 解析为 ModelAction.tool
  -> 先写 assistant_tool_call Fact
  -> ToolRuntime 按 call_id 取回持久化 ToolCall，再做 Registry / Surface / Schema / Policy / Approval
  -> 使用该持久调用规划影响范围与 preimage，再写 tool_started
  -> tool_execution.py 私有纯函数：Preview、脱敏、Drift/Diff/Transition 与结果分类
  -> 先 fsync tool_started Fact
  -> ToolContext 向当前 Tool Runner 提供 Workspace、Store 与 Run 身份
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

- **Fact**：已接受并持久化到 `events.jsonl` 的 Run Event，例如 Tool Call、恢复时的
  `user_guidance`、`tool_started`、Tool Result、Verification 与终态。Workspace 和 Artifact
  内容仍由各自 Store 持有，不叫 Run Fact。
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
  dependencies   Store、Artifacts、CommandRunner、RepoMap 和 Subagents
  tools          工具 Registry、模型 Surface、准入与执行
  prompt         稳定 instructions 与动态 ContextManager
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
`CompletionController` 负责 TaskContract、Child receipt、Verifier 与 Completion Gate。

当前工具轮已经收口到 `ToolRuntime` 公开边界：`AgentLoop` 只提交 `ToolCall` 并消费
`ToolOutcome`，工具协议、准入、执行和 Fact 写入由 `ToolRuntime` 协调，纯值计算下沉到
私有 `tool_execution.py`。最终提交仍由 `CompletionController` 判断、
`RunLifecycle` 持久化。

核心实现：

- 原生 function calling：稳定角色/执行/Tool/WorkingState/完成规则进入 Responses `instructions`；首轮动态 `input` 只发送 Runtime task policy、非空的有界不可信 Context 与 Task Request。恢复输入先作为 `user_guidance` Fact 持久化，再从该 Fact 投影为不可裁剪的 latest request；普通工具续接只追加原生 Call/Output。空 RepoMap、WorkingState、History 不渲染。严格 Function Schema 只进入 `tools`。支持 `allowed_tools` 的 Provider 在普通执行阶段获得稳定完整 Schema 和动态允许名称；进入 final-only 边界时，wire schema 也物理缩成 `submit_final` 并重建 Provider Session。解析层和 ToolRuntime 都复验允许名称，一次响应只接受一个函数调用。
- Run 流程事实源：每个 Run 只有一个 strict、连续 sequence、append-only、fsynced 的 `events.jsonl`。首个 User Event 保存 Runtime-owned `TaskContract`；实时执行与恢复共用一个 `RunProjection`，统一重建 Task、Evidence、Metrics 和 Pending Call。终态只额外保存最终 `final_diff` receipt，不保存第二份终态报告。
- 长上下文治理：Prompt 使用配置的 Context Window 和固定 section caps，按真实 `wire_tools` 计入 Schema Token，History 获得剩余预算。显式 `prepare_compaction` 在只读 Prompt build 前运行；独立模型 Session 读取经过转义的规范 Event payload，完整保留 ToolOutcome 事实，持久 Summary 始终只有 `Progress`、`Critical Context` 两段，不复述 TaskContract 或 WorkingState。只有替换前后的最终 escaped History Wire 确实缩小时才提交。所谓七类 Effective Recovery Context 只是把 TaskContract Goal、WorkingState Constraints/Decisions/Next Steps、这两段 Summary 与 RunEvidence 并排展示的教学/观测视图；它不是七段 LLM 输出、第二状态或 Completion 依据。Provider、结构或长度失败时不提交 Compaction Event，Runtime 使用近期完整 Tool 事务的有界投影继续。结构化 Context Overflow 只允许一次 Session/Prompt 重建重试。
- KV Cache：稳定 instructions hash 在已验证支持的 Backend 上作为 `prompt_cache_key`；每轮 `turn_metrics.completion_metadata` 记录 `input_tokens`、`cached_tokens` 与 uncached input。Provider capability 按明确 Host 配置，不在生产请求中自动探测，也不保留旧 Prompt/Tool 协议兼容分支。
- Workspace 新鲜度：文件工具返回真实 Unified Diff 和 before/after path transitions。每个路径只在本 Run 第一次修改前保存完整 preimage；`RunChangeSet` 计算最终净变化，因此 `A -> B -> A` 不算完成所需的修改。外部漂移会阻止成功提交；用户取消或重置仍可受控停止，并以 `unavailable_reason=workspace_drift` 明确说明此时无法生成可信 Final Diff。Verifier 只保存命令结果及其 mutation/path states，current/stale 在查询时派生。
- RepoMap：基于 tree-sitter 构建 Python symbol/reference graph，以 lexical + personalized PageRank 在 Token 预算内返回任务相关签名。
- 分层状态：`TaskContract` 保存目标、任务类型、写入范围与完成要求，模型不能修改；WorkingState 继续使用原有 add/remove 增量协议，只保存约束、已确认决定和下一步。RepoMap 保持默认开启，Prompt build 不写磁盘。
- 安全工具：路径锚定 Workspace，通用文件工具不能访问 `.git/` 或 `.pico/`；Manual 模式只允许观察工具，mutation 必须属于 Active Run。`delegate` 的 implement 写范围必须落在 TaskContract 与当前 Config 的有效交集内，`integrate_child` 在写回前再次复核 Child receipt 与路径。`FailureInfo.recovery` 是唯一持久化纠错事实，模型可见 correction action 由它派生。Repeat cache 是进程内避免连续盲目重放相同 `partial/unknown` 调用的辅助；持久安全仍由 create-only、Revision、原子 Workspace 写入和 Child integration 状态负责。`write_file` 只创建 absent 文件，`edit_file` 只修改 existing 文件并携带 `read_file` Revision；提交点再次复验 Revision 后原子替换。
- 无通用 Shell：模型可见工具不包含任意命令执行入口。文件读取、搜索、修改与 Subagent 操作都通过各自的结构化 Tool 进入同一 ToolRuntime。
- 大输出：模型直接获得一次 12 KiB 预览；完整、脱敏输出写入不可变 artifact，截断结果可通过当前 run 限定的 `read_artifact` 按 8 KiB 字节页继续读取。
- 本机验证：只有用户通过 `--verify-command` 明确给出的固定命令可以执行。Runtime 在最终提交边界运行该命令，并把结果绑定到当前 mutation/path states；模型不能生成或修改验证命令。命令拥有当前用户的本机文件、进程与网络权限，因此这不是 Sandbox。
- 崩溃恢复：Session 只保存 `active_run_id`。恢复时的新用户约束先写入 `user_guidance`；副作用前 fsync `tool_started`，保存从持久 ToolCall 确定性校验出的规范参数及精确潜在影响路径/before revision；结果完成后 fsync `tool_result`。恢复时若 Started/Result 不配对，只检查声明路径并生成 interrupted error/partial，绝不盲目重放。最后一条未完成 JSONL 尾巴可截断，中间损坏直接报错。
- 恢复配置：模型、预算、超时、Verifier、工具表面和权限策略均使用续跑时的当前配置，不冻结为 Resume identity。
- 完成证据：`CompletionController` 是唯一完成决策者，依次检查尚未集成的 implement Child、TaskContract、未修复 unknown/partial、Workspace drift、当前 Verification，以及验证后仍未解决的 Effect。未修复的不确定副作用不会触发无意义的 verifier；修复后的 Workspace partial 必须验证当前代码。`RunEvidence` 只提供 repair/effect/verification 事实查询。没有通用 Python AST 隐式 Gate，也不会自动猜验证命令。

## Extensions

[Project Memory](extensions/project_memory/README.md) 作为独立参考扩展保存在仓库中。它未安装、
未被 Pico 加载，也没有 Core 配置开关；导入 `pico` 不会创建 Memory 目录、注入 Prompt 内容或
注册 Memory 工具。

## 多 Agent 协作

Parent 通过两个原生工具一次处理一个独立 Pico Child：

- `delegate`：创建一个 `explore` 或 `implement` Child，并同步返回单个 receipt。
- `integrate_child`：按 `child_id` 显式验证并集成一个已完成的 implement Child。

Explore Child 直接读取 Parent Workspace，但工具表面严格只读，不创建 Worktree。Implement
Child 必须声明非空 `allowed_write_paths`，始终运行在从当前 Git base 创建的独立 Worktree，
并拥有独立 Model Client、Session、Run Log 和 Artifact namespace。两类 Child 都没有
`delegate`，不能嵌套创建下一层 Agent。

委派完成不等于修改已进入 Parent。Implement Child 只返回 base、实际 changed paths、Patch
digest 与摘要 receipt；Parent 必须随后调用 `integrate_child`。集成先复验 Parent 仍位于同一
base，再在临时 Worktree 应用 Patch、运行用户固定 Verification，全部通过后才写回 Parent。
没有自动 merge，也没有 batch、依赖图、后台任务或并行 worker。

Parent 只接收 Child 摘要和 receipt，不复制 Child 工具历史。Child Run Log 与 Patch 作为
审计工件保留，但当前实现不恢复跨 CLI 的 Child 编排或集成事务，也不做分布式调度。

## 安装与运行

需要 Python 3.10+ 和 uv；Runtime 不依赖 Docker。

```bash
uv sync
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
交互模式使用 `/state` 查看当前 Run 的 WorkingState；它不读取或加载任何跨 Run 扩展状态。

Pico 不根据仓库文件猜验证命令。任务设置 `--require-verification` 时必须显式提供
`--verify-command`；Completion Gate 在最终提交时运行它并绑定当前 mutation/path states。
验证命令在 Workspace 中以启动 Pico 的当前用户权限本机运行。Workspace 路径检查只约束
Pico 的结构化文件工具，不能约束该进程读取其他目录、访问网络或启动子进程。因此这里只应
填写你已经审查并信任的仓库命令；不可信代码应交给外部 CI、VM 或容器。

`--max-new-tokens` 默认值为 `1024`。它传给 Responses API 的 `max_output_tokens`，统计 reasoning tokens、
可见文本和 function-call arguments 的总输出，而不只是最终展示文本。

## 状态与工件

```text
.pico/
  sessions/<session_id>.json
  runs/<run_id>/
    events.jsonl
    artifacts/*
    subagents/<child_id>/
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

评测分为 native Harness regression、Context governance 和 RepoMap。它们衡量 Runtime
机制，不冒充真实模型能力指标。
评测实现位于仓库顶层 `evals/`，不属于 `pyproject.toml` 打包的 `pico` Runtime。

当前确定性 Artifact 为 `harness-regression.json`、`context-governance.json`、
`repo-map.json` 与 `triage-evaluation.json`。

当前随仓库发布的证据：

| 层级 | 结果 | 说明 |
|---|---:|---|
| Python tests | 全部通过 | Runtime contracts、恢复、安全、上下文与工具边界 |
| Native Harness | 5/5 | edit、recovery、safety、governance；失败时脚本非零退出 |
| Context governance | 3/3 | 真实 ContextManager/RunLog Compaction；三个上下文规模下预算、事务、原事件与 WorkingState 均保持 |
| Real Compaction | 1/1 | 真实 `gpt-5.6-luna` 在受控窗口触发 Session Reset + Compaction，继续完成单次 Patch 与 Hidden Verifier；当前 runner 在 Prompt 精简后使用 28k，已发布 Artifact 记录其捕获时的 32k 配置 |
| RepoMap | 全部通过 | tree-sitter 图、任务命中与 Token 预算 |
| Pico Triage | 3/3 | 确定性 Case、失败证据、责任文件、Patch receipt 与 Verification 报告约束 |

真实 Compaction Artifact 见 `artifacts/real-compaction.json` 与
`artifacts/real-compaction.patch`。模型按顺序各读取一次 12 份证据，最大单次 Input 为
31,713 Token；Runtime 在估算下一轮达到 33,662 Token 时重建 Provider Session，将旧的
13 个事件摘要为 624 Token，并保留 7,732 Token 近期事务。624 Token Summary 只包含 Progress
与 Critical Context；Compaction 后 Goal 仍来自 TaskContract，Constraints、Decisions 与 Next
Steps 仍来自 WorkingState。模型只执行一次 Mutation，最终可见与停止后 Hidden Verifier
均通过。该测试通过降低Runtime配置窗口来验证真实LLM续接，不冒充自然消耗272k上下文，
也不代表已经验证跨进程Resume。

## Pico Triage

`applications/triage` 是直接建立在 Pico Runtime 上的 CI 故障诊断应用层。它没有第二套
Agent Loop、状态机、日志或 Patch 应用器：当前诊断计划复用 WorkingState，执行事实继续写入
Pico Run Log，修复与验证复用现有 Tool、implement Child Worktree、`integrate_child` 和 Completion Gate。

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

最终 `TriageReport` 要求诊断证据引用当前 Run 中真实完成的 Tool Call。原始失败来自用户提供的
CI Log；修改路径、Verification 和工具步数由 Run Log 与 RunEvidence 确定性派生，而不是由
模型自行声明。

旧的 Click、Packaging 和 urllib3 真实模型 Artifact 由已经退役的 Docker 执行边界生成，
现已从仓库删除，不作为当前本机 Verification Runtime 的证据。新的不可信仓库 Triage 证据
必须在外部 CI、VM 或容器中重新采集；只有用户已经信任的仓库才可直接使用本机 Verification。

面试展示顺序见 [`docs/review-pack/interview-demo.md`](docs/review-pack/interview-demo.md)。

项目定位是本地单用户 Agent Runtime，不是模型网关、多租户平台或通用工作流引擎。
