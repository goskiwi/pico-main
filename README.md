# Pico

Pico 是一个轻量、本地、单协议的多 Agent Coding Runtime。模型只负责通过
OpenAI-compatible Responses 原生 function calling 提出下一步动作；上下文、权限、
工具执行、恢复、验证和持久化均由 Runtime 持有。

项目主动不做多 Provider、XML 工具协议、Skills、MCP、分布式 Worker 和旧状态兼容。

## Runtime 主链路

```text
User request
  -> Run Log v2
  -> RepoMap + Run WorkingState + Project Memory Catalog
  -> Token-budgeted prompt projection
  -> Responses function_call -> ModelAction
  -> Registry / Surface / Schema / Policy / Approval
  -> Docker tool execution or revision-bound atomic mutation
  -> fsynced tool_started -> ToolOutcome/tool_result
  -> Run Log projections: WorkingState / Context / TaskState / Evidence / stats
  -> structured runtime verification -> Completion Gate
```

## Runtime 对象边界

`Pico` 只保留九个顶层组件，不再把配置、当前 Run、Workspace 缓存和工具状态
平铺成几十个属性：

```text
Pico
  model_client   模型通信
  config         不变式、资源限额和工具策略
  workspace      路径边界、工作区快照和内容指纹
  session        Session 身份与 active_run_id 指针
  run            当前或最近一次 Run 的可变状态
  dependencies   Store、Sandbox、RepoMap 和 Subagents
  tools          工具 Registry、模型 Surface、准入与执行
  recovery       active_run_id 与 Run Log tail 恢复
  prompt         Prefix、ContextManager 和相关记忆选择
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

answer = agent.ask("inspect the failing test")
state = agent.run.task_state
outcome = agent.tools.run("read_file", {"path": "README.md"})
```

单次请求的控制逻辑也按职责分开：`AgentLoop` 只编排模型轮次、工具轮次和
Provider 续接；`RunLifecycle` 负责创建/恢复 Run 和 Run Log 终态；
`CompletionController` 负责语法、Subagent、Verifier 与 Completion Gate。

核心实现：

- 原生 function calling：Pydantic 参数模型生成 strict function schema；Responses output items 与匹配的 function output 在任务内连续回放，一次响应只接受一个函数调用，最终回答也通过 `submit_final`。
- Run 流程事实源：每个 Run 只有一个 strict、连续 sequence、append-only、fsynced 的 `events.jsonl`。User、Tool Call、`tool_started`、Tool Result、Verification、Compaction 与终态均写入同一序列；WorkingState、TaskState、RunEvidence 和运行统计由同一事件 reducer 实时投影并可重建。Workspace、Project Memory 和 Artifact 分别持有其当前内容事实。
- 长上下文治理：Prompt 使用配置的模型 Context Window；预算包含各 section、序列化 Tool Schema，并在 fresh Provider usage 返回后纳入已观测的协议开销。总上下文越过 `context window - reserve tokens` 时，Compaction 按 token 保留近期完整 Tool Call/Result 事务，更早事务的工具名、参数、状态和结果摘要会继续留在确定性投影中。Provider 明确报告 context overflow 时只执行一次 compact-and-retry；原始 Run events 不删除。
- Workspace 新鲜度：工具 runner 以结构化结果声明精确影响路径；普通读取不扫描全仓，写入后只失效 Runtime Workspace revision。Completion/Verifier 才对完整 Workspace 内容做强制指纹扫描并绑定验证证据。
- RepoMap：基于 tree-sitter 构建 Python symbol/reference graph，以 lexical + personalized PageRank 在 Token 预算内返回任务相关签名。
- 分层状态：Run WorkingState 由 `user_message` 和 `update_working_state` Tool 事务投影，保存目标、约束、已确认决定和下一步；Session 只保存 `active_run_id`。跨任务 Project Memory 以 Markdown Card 为唯一事实源，写入来源由 Run ID 和 Tool Call ID 定位；生成的 `MEMORY.md` Catalog 会从 Card 自愈并有界地常驻上下文。主模型按可见描述显式调用 `memory_recall`，Runtime 在独立预算内返回最多五张完整 Card，所有 Recall Call/Result 都进入正常 RunLog 与模型计量。文件事实始终从 Workspace、搜索和 Run Log 获取。
- 安全工具：路径锚定 Workspace，通用文件工具不能访问 `.git/` 或 `.pico/`；重复调用检测；写入必须携带 `read_file` 返回的 SHA-256 revision，并通过 fsync + atomic replace 提交。
- 最小 Shell 环境：未配置时使用默认白名单；显式空白名单保持为空，仅由 Shell 边界补充运行必需的 `PWD`/`PATH`。
- 大输出：模型直接获得一次 12 KiB 预览（shell 为尾部 16 KiB）；完整、脱敏输出写入不可变 artifact，截断结果可通过当前 run 限定的 `read_artifact` 按 8 KiB 字节页继续读取。
- 隔离执行：`run_shell` 强制进入临时 Docker 容器；inspect/verify Profile 均禁网、只读 rootfs 与 Workspace，并施加 cap-drop、进程/CPU/内存/输出限制和整轮 deadline。
- 崩溃恢复：Session 只保存 `active_run_id`。副作用前先 fsync `tool_started` 及精确潜在影响路径/before revision；结果完成后 fsync `tool_result`。恢复时若二者不配对，只检查声明路径并生成 interrupted error/partial，绝不盲目重放。最后一条未完成 JSONL 尾巴可截断，中间损坏直接报错。
- 恢复配置：模型、预算、超时、Verifier、工具表面和权限策略均使用续跑时的当前配置，不冻结为 Resume identity。
- 完成证据：观察、修改和结构化 verifier 结果写入 RunEvidence；变更 Python 先做 AST 校验，Workspace 变更需通过绑定当前内容指纹的 Runtime verifier，未解决 partial/unknown 状态禁止成功结束。

## 多 Agent 协作

Parent 通过两个原生工具编排独立 Pico Child：

- `delegate_tasks`：提交显式依赖 DAG；无依赖任务最多三个并行执行。
- `apply_task_patches`：由独立 `PatchIntegrator` 在临时 Integration Worktree 中按拓扑顺序检查并应用 Patch，验证通过且 Parent HEAD/工作区未变化后再写回原工作区。

Explore Child 共享同一源码快照但只有只读工具。Implement Child 使用独立 Git
Worktree、独立 Model Client、Session、Run Log 和 Artifact namespace；
写操作在 ToolExecutor 准入阶段受精确文件白名单限制，任务结束后再以实际 Git Diff
复核一次。无依赖且写文件重叠的实现任务会在规划阶段被拒绝；有依赖的实现任务会把
上游 Patch 作为临时基线后继续工作。

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
uv run pico --cwd /path/to/repo
uv run pico "inspect the failing test and implement a verified fix"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://api.openai.com/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

常用运行参数：

```bash
uv run pico --approval ask
uv run pico --run-timeout 600
uv run pico --max-tool-executions 40
uv run pico --max-new-tokens 1024
uv run pico --provider-context-limit 272000
uv run pico --provider-context-limit 272000 --compaction-reserve-tokens 16384 --compaction-keep-recent-tokens 20000
uv run pico --verify-command "python -m pytest -q"
uv run pico --resume latest
uv run pico run show <run_id> --cwd /path/to/repo
uv run pico run events <run_id> --cwd /path/to/repo
```

`--verify-command ""` 可显式关闭自动 verifier。若未提供，Python/Node 项目会按仓库文件自动选择默认命令。

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
| Project Memory | 全部通过 | 真实 `memory_store -> memory_recall -> final` Tool 事务与不可信数据边界 |
| RepoMap | 全部通过 | tree-sitter 图、任务命中与 Token 预算；另有固定模型 AUTO/OFF 对照 |
| Pico Triage | 3/3 | 真实失败命令复现、责任文件定位、Patch 与 Verification 闭环 |
| Five-repository fixture preflight v2 | 5/5 | 每题均 fail-before/pass-after；绑定官方修复提交、fixture/verifier/patch digest 与 Docker image ID |
| Historical Real OSS suite v2 | 5/5 | 固定 Runtime commit `61207f4` 的历史模型证据；统一 40 步，五题均为第 1 次尝试 |
| Historical upstream public tests | 25/25 | 与 Real OSS v2 Patch 绑定的历史上游测试证据；禁网只读 Docker |

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
uv run python scripts/run_click_triage.py --model gpt-5.6-luna
```

旧的 10/12/14 步差异化结果已删除。Real OSS v2 使用统一 40 工具步预算；没有任务失败后的选择性重跑，本次五题均为第 1 次尝试且没有基础设施重试。它绑定历史 Runtime commit `61207f4`，不能冒充当前工作树的模型结果。完整结果见 `artifacts/real-oss-suite-v2.{json,md}`。

五仓库冻结任务集可以从精确上游 commit 重新物化并运行：

```bash
uv run python scripts/materialize_real_oss.py --replace
docker build -f docker/real-oss-suite.Dockerfile -t pico/real-oss-suite:latest .
uv run python scripts/run_real_oss_suite.py --model gpt-5.6-luna
docker build -f docker/official-public-tests.Dockerfile -t pico/official-public-tests:latest .
uv run python scripts/run_official_public_tests.py
```

Real OSS runner 强制要求 clean worktree，并把 Runtime commit、manifest/verifier/reference-patch digest、fixture tree digest、Docker image ID 和统一工具预算写入证据。Preflight 必须证明每个 hidden verifier 在 pre-fix fixture 上失败，并在应用绑定上游修复提交的 reference patch 后通过。完整五题模型运行不复用旧结果，也不选择性重跑任务失败。

官方公开测试 runner 使用冻结的上游 test-only patch 和完整 fix SHA：pre-fix、官方 reference patch、Agent patch 使用同一测试节点。四题官方测试在 pre-fix 上失败；Jinja 官方测试在 pre-fix 上也会通过，因此明确标为非区分性，独立 hidden verifier 负责覆盖 async parent 无参数 overlay 的遗漏。结果见 `artifacts/official-public-tests-v1.{json,md}`。

面试展示顺序见 [`docs/review-pack/interview-demo.md`](docs/review-pack/interview-demo.md)。

项目定位是本地单用户 Agent Runtime，不是模型网关、多租户平台或通用工作流引擎。
