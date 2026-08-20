# Pico

Pico 是一个轻量、本地、单协议的多 Agent Coding Runtime。模型只负责通过
OpenAI-compatible Responses 原生 function calling 提出下一步动作；上下文、权限、
工具执行、恢复、验证和持久化均由 Runtime 持有。

项目主动不做多 Provider、XML 工具协议、Skills、MCP、分布式 Worker 和旧状态兼容。

## Runtime 主链路

```text
User request
  -> Run Journal v3
  -> RepoMap + separated Working/Catalog/Retrieved Memory
  -> Token-budgeted prompt projection
  -> Responses function_call -> ModelAction
  -> Registry / Surface / Schema / Policy / Approval
  -> Docker tool execution or revision-bound atomic mutation
  -> fsynced tool_started -> ToolOutcome/tool_result + optional Policy Hook
  -> Journal projections: Context / TaskState / Evidence / Report
  -> structured runtime verification -> Completion Gate
```

## Runtime 对象边界

`Pico` 只保留九个顶层组件，不再把配置、当前 Run、Workspace 缓存和工具状态
平铺成几十个属性：

```text
Pico
  model_client   模型通信
  config         不变式、限额和功能开关
  workspace      路径边界、工作区快照和内容指纹
  session        Session 数据与当前任务目标
  run            当前或最近一次 Run 的可变状态
  services       Store、Sandbox、RepoMap、Hooks 和 Subagents
  tools          工具 Registry、模型 Surface、准入与执行
  recovery       active_run_id 与 Journal tail 恢复
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
    config=PicoConfig(approval_policy="auto", max_steps=40),
)

answer = agent.ask("inspect the failing test")
state = agent.run.task_state
outcome = agent.tools.run("read_file", {"path": "README.md"})
```

单次请求的控制逻辑也按职责分开：`AgentLoop` 只编排模型轮次、工具轮次和
Provider 续接；`RunLifecycle` 负责创建/恢复 Run 和 Journal 终态；
`CompletionController` 负责语法、Subagent、Verifier 与 Completion Gate。

核心实现：

- 原生 function calling：Pydantic 参数模型生成 strict function schema；Responses output items 与匹配的 function output 在任务内连续回放，一次响应只接受一个函数调用，最终回答也通过 `submit_final`。
- 单一事实源：每个 Run 只有一个 strict、连续 sequence、append-only、fsynced 的 `journal.jsonl`。User、Tool Call、`tool_started`、Tool Result、Verification、Compaction 与终态均写入同一序列；Context、TaskState、Evidence 和 Report 都是确定性投影。
- 长上下文治理：Prompt 使用配置的模型 Context Window；最近一次 Provider `total_tokens` 加上其后新增 Tool Result 的本地估算决定 rotation 与 Compaction，本地 tokenizer 负责 section 预算和 cut point。总上下文越过 `context window - reserve tokens` 时，Compaction 按 token 保留近期完整 Tool Call/Result 单元，并以结构化 `Goal / Constraints / Progress / Decisions / Next Steps / Critical Context` 摘要替换更早前缀；语义摘要失败时退化为包含原始目标的 Runtime Facts。Provider 明确报告 context overflow 时只执行一次 compact-and-retry。原始 Journal Entry 不删除。
- Workspace 新鲜度：工具 runner 以结构化结果声明精确影响路径；普通读取不扫描全仓，写入后只失效 Runtime Workspace revision。Completion/Verifier 才对完整 Workspace 内容做强制指纹扫描并绑定验证证据。
- RepoMap：基于 tree-sitter 构建 Python symbol/reference graph，以 lexical + personalized PageRank 在 Token 预算内返回任务相关签名。
- 分层记忆：Session Working Memory 只保存当前任务目标；Project Memory 以 Markdown Card 为唯一事实源。受限的 `MEMORY.md` 目录常驻上下文，语义选择的 Card 正文使用独立的高优先级预算逐张完整装入，装不下的 Card 不会被静默截成半张。文件事实始终从 Workspace、搜索和 Run Journal 获取。
- 安全工具：路径锚定 Workspace；重复调用检测；写入必须携带 `read_file` 返回的 SHA-256 revision，并通过 fsync + atomic replace 提交。
- 最小 Shell 环境：未配置时使用默认白名单；显式空白名单保持为空，仅由 Shell 边界补充运行必需的 `PWD`/`PATH`。
- 大输出：模型直接获得一次 12 KiB 预览（shell 为尾部 16 KiB）；完整、脱敏输出写入不可变 artifact，截断结果可通过当前 run 限定的 `read_artifact` 按 8 KiB 字节页继续读取。
- 隔离执行：`run_shell` 强制进入临时 Docker 容器；inspect/verify Profile 均禁网、只读 rootfs 与 Workspace，并施加 cap-drop、进程/CPU/内存/输出限制和整轮 deadline。
- 崩溃恢复：Session 只保存 `active_run_id`。副作用前先 fsync `tool_started` 及精确潜在影响路径/before revision；结果完成后 fsync `tool_result`。恢复时若二者不配对，只检查声明路径并生成 interrupted error/partial，绝不盲目重放。最后一条未完成 JSONL 尾巴可截断，中间损坏直接报错。
- 策略扩展：核心循环默认持续到模型提交最终答案、deadline、取消或错误；不内置“若干步未编辑”等任务猜测。宿主可显式传入 `before_tool_call`、`after_tool_result` 和 `should_stop_after_turn` hook，hook 只能进一步限制或提供指导，不能改写工具事实或绕过安全校验。
- 恢复配置：模型、预算、超时、Verifier、Hooks、工具表面和权限策略均使用续跑时的当前配置，不冻结为 Resume identity。
- 完成证据：观察、修改和结构化 verifier 结果写入 Evidence Ledger；变更 Python 先做 AST 校验，Workspace 变更需通过绑定当前内容指纹的 Runtime verifier，未解决 partial/unknown 状态禁止成功结束。

## 多 Agent 协作

Parent 通过三个原生工具编排独立 Pico Child：

- `delegate_tasks`：提交显式依赖 DAG；无依赖任务最多三个并行执行。
- `continue_task`：使用原 Child Session、工作记忆和运行摘要继续任务。
- `apply_task_patches`：由独立 `PatchIntegrator` 在临时 Integration Worktree 中按拓扑顺序检查并应用 Patch，验证通过且 Parent HEAD/工作区未变化后再写回原工作区。

Explore Child 共享同一源码快照但只有只读工具。Implement Child 使用独立 Git
Worktree、独立 Model Client、Session、Run Journal 和 Artifact namespace；
写操作在 ToolExecutor 准入阶段受精确文件白名单限制，任务结束后再以实际 Git Diff
复核一次。无依赖且写文件重叠的实现任务会在规划阶段被拒绝；有依赖的实现任务会把
上游 Patch 作为临时基线后继续工作。

Parent 只接收 Child 的摘要、Run Journal receipt 和 Patch 引用，不复制 Child 的工具历史。
相同 Parent Run 中已完成的 Task ID 会复用现有结果；失败任务只阻断依赖它的下游分支。
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
uv run pico --max-steps 40
uv run pico --max-new-tokens 1024
uv run pico --provider-context-limit 64000
uv run pico --provider-context-limit 64000 --compaction-reserve-tokens 16384 --compaction-keep-recent-tokens 20000
uv run pico --verify-command "python -m pytest -q"
uv run pico --resume latest
uv run pico journal stats <run_id> --cwd /path/to/repo
uv run pico journal replay <run_id> --cwd /path/to/repo
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
    cards/<type>_<name>.md
  runs/<run_id>/
    journal.jsonl
    artifacts/*
    subtasks.json
    subagents/<task_id>/
      sessions/*
      runs/*
      patch-*.diff
```

所有 schema 都严格校验；当前版本不会迁移旧 XML、`events.jsonl`、`context.jsonl`、Checkpoint 或旧 Session，
也不会在初始化时自动删除这些旧数据。

## 评测

```bash
uv run python scripts/run_evaluations.py
uv run pytest -q
uv run ruff check pico tests scripts
```

评测分为 native Harness regression、Context governance、Markdown Project Memory 和
RepoMap。它们衡量 Runtime 机制，不冒充真实模型能力指标。

当前随仓库发布的证据：

| 层级 | 结果 | 说明 |
|---|---:|---|
| Python tests | 全部通过 | Runtime contracts、恢复、安全、上下文与工具边界 |
| Native Harness | 5/5 | edit、recovery、safety、governance；失败时脚本非零退出 |
| Runtime policy | 全部通过 | 单一恢复建议、hook 边界、结构化 verifier 与事件重放 |
| Five-repository fixture preflight v2 | 5/5 | 每题均 fail-before/pass-after；绑定官方修复提交、fixture/verifier/patch digest 与 Docker image ID |
| Five-repository Real OSS suite v2 | 5/5 | clean commit `61207f4`；统一 40 步；五题均为第 1 次尝试 |
| Official upstream public tests | 25/25 | 官方 test-only patch；pre-fix 基线、reference patch、Agent patch 三组验证；禁网只读 Docker |

旧的 10/12/14 步差异化结果已删除。Real OSS v2 使用统一 40 工具步预算；没有任务失败后的选择性重跑，本次五题均为第 1 次尝试且没有基础设施重试。完整结果见 `artifacts/real-oss-suite-v2.{json,md}`。

五仓库冻结任务集可以从精确上游 commit 重新物化并运行：

```bash
uv run python scripts/materialize_real_oss.py --replace
docker build -f docker/real-oss-suite.Dockerfile -t pico/real-oss-suite:latest .
uv run python scripts/run_real_oss_suite.py --model gpt-5.6-luna
docker build -f docker/official-public-tests.Dockerfile -t pico/official-public-tests:latest .
uv run python scripts/run_official_public_tests.py
```

Real OSS runner 强制要求 clean worktree，并把 Runtime snapshot、manifest/verifier/reference-patch digest、fixture tree digest、Docker image ID 和统一工具预算写入证据。Preflight 必须证明每个 hidden verifier 在 pre-fix fixture 上失败，并在应用绑定上游修复提交的 reference patch 后通过。完整五题模型运行不复用旧结果，也不选择性重跑任务失败。

官方公开测试 runner 使用冻结的上游 test-only patch 和完整 fix SHA：pre-fix、官方 reference patch、Agent patch 使用同一测试节点。四题官方测试在 pre-fix 上失败；Jinja 官方测试在 pre-fix 上也会通过，因此明确标为非区分性，独立 hidden verifier 负责覆盖 async parent 无参数 overlay 的遗漏。结果见 `artifacts/official-public-tests-v1.{json,md}`。

面试展示顺序见 [`docs/review-pack/interview-demo.md`](docs/review-pack/interview-demo.md)。

项目定位是本地单用户 Agent Runtime，不是模型网关、多租户平台或通用工作流引擎。
