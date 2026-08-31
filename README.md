# Pico

Pico 是一个轻量、本地、单协议的 Coding Agent Runtime。模型每轮只通过
OpenAI-compatible Responses function calling 提出一个动作；Runtime 掌握工具准入、
副作用、持久化、恢复、验证和最终完成权。CLI 还提供一次一个的显式 Child 委派。

Pico 面向用户已经信任的本地仓库。模型没有通用 `run_shell`；只有用户通过
`--verify-command` 配置的固定命令会在 Workspace 中以当前用户权限本机执行。文件工具的
路径约束不能限制该命令访问其他目录、网络或子进程。未知仓库、未知 PR 或其他不可信代码
必须放到 Pico 外部的 CI、VM 或容器中运行。

## 快速开始

需要 Python 3.10+ 和 uv；Runtime 不依赖 Docker。

```bash
uv sync

uv run pico \
  --verify-command "python -m pytest -q" \
  --cwd /path/to/trusted/repo \
  "Fix calculator.add so the existing test passes"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

用户只提交自然语言目标。新 Run 创建前，Runtime 使用隔离的结构化分类调用生成
`read_only / modify / modify_optional` Intent，并据此持久化 TaskContract；分类不授予工具权限。
Resume 直接复用原 Contract，不重新分类。`ask()` 返回结构化 `RunOutcome`，持久事实仍以
`RunStore.replay(run_id)` 为准。

## 核心运行链

```text
User request -> hidden TaskIntentClassifier -> TaskContract
  -> AgentLoop: ModelAction(tool / invalid / final)
  -> ToolRuntime: admission -> tool_started -> Runner -> tool_result
  -> RunLog: append-only, sequenced, fsynced Facts
  -> RunProjection: Task + Evidence + Metrics + one Pending Call
  -> CompletionController
  -> Runtime Verification + Final Diff + RunOutcome
```

面试主线只需要五点：

1. **单一事实源**：每个 Run 只有一个 Run Log；Live 与 Replay 共用 `RunProjection.apply_event`。
2. **可恢复工具事务**：`assistant_tool_call -> tool_started -> tool_result`；副作用前先 fsync，
   崩溃后检查真实路径状态，不盲目重放未知操作。
3. **Runtime 拥有执行权**：模型只提出 ToolCall；ToolRuntime 使用持久化的 name/args 完成
   Schema、TaskContract、写范围与 Approval 检查。AgentLoop 接受并持久化 Call，ToolRuntime
   持久化执行事务的 Started/Result。
4. **Revision-bound 原子修改**：`edit_file` 携带读取时 Revision，提交点再次复验后原子替换，
   外部修改不会被静默覆盖。
5. **证据驱动完成**：模型调用 `submit_final` 后，Runtime 检查净变化与不确定副作用，运行用户
   固定 Verification，并生成真实 Final Diff；模型不自行运行 verifier 或生成 Final Diff。

关键因果 payload（Task、Tool Call/Started/Result、Verification 与终态）使用严格 Schema；
Telemetry、Compaction 等观测 payload 保持可扩展。事件 envelope、sequence 和 event ID 仍会
在持久化读取与 Replay 时校验。

## 真实 CLI 工具面

CLI 默认注册以下十个原生工具。支持 `allowed_tools` 的 Provider 可以保持完整 Schema 稳定并
动态限制名称；其他 Provider 直接接收当前允许的较窄 Schema。

| 工具 | modify | read_only | 作用 |
|---|:---:|:---:|---|
| `list_files` | ✓ | ✓ | 列出 Workspace 文件 |
| `read_file` | ✓ | ✓ | 按行读取并返回 Revision |
| `read_artifact` | ✓ | ✓ | 分页读取当前 Run 的大输出 |
| `search` | ✓ | ✓ | 有界搜索 Workspace |
| `write_file` | ✓ | — | 只创建不存在的文件 |
| `edit_file` | ✓ | — | 按 Revision 修改已有文件 |
| `update_working_state` | ✓ | ✓ | 增量维护当前 Run 的约束、决定和下一步 |
| `delegate` | ✓ | ✓ | 同步运行一个 Child；read-only Parent 只能使用 `explore` |
| `integrate_child` | ✓ | — | 显式验证并集成一个 Implement Child Patch |
| `submit_final` | ✓ | ✓ | 请求 Runtime 进行最终完成检查 |

CLI 的 `build_agent()` 默认安装 Child runner，因此真实 CLI 请求会携带 `delegate` 与
`integrate_child`。程序化 `Pico(...)` 默认是单 Agent；只有显式传入
`subagent_model_client_factory` 才加载 Child 工具。交互模式使用 `/state` 查看当前 Run 的
WorkingState。

## Child 委派边界

`delegate` 一次只运行一个同步 Child，角色为 `explore` 或 `implement`；Child 没有
`delegate`，不能嵌套委派。这里没有 DAG、批量请求、后台队列或并行 worker。

- Explore 直接读取 Parent Workspace，但工具表面只读，不创建 Worktree。
- Implement 要求 Git 仓库根、clean Parent、可解析的 HEAD、非空 `allowed_write_paths` 和固定
  Verification 命令；它始终在独立 Worktree 中运行，只返回摘要和不可变 Patch receipt。
- Implement 不自动 merge。Parent 必须调用 `integrate_child(child_id)`；Runtime 复验 base，
  在临时 Worktree 应用 Patch并运行 Verification，确认 Patch 与 changed paths 未漂移后才写回。
- 未集成的 Implement Child 会阻止 Parent 完成。

Child Run Log、Session、Artifact 和 Patch 位于 Parent Run 的 `subagents/<child_id>/` 下。
已完成 Implement 的 receipt 与 integration 状态可从 Parent Run Log 恢复，并在重启后继续
`integrate_child`；运行中的 Child 执行不会跨 CLI 恢复或重新调度。

## 状态与信任边界

```text
.pico/
  sessions/<session_id>.json
  runs/<run_id>/
    events.jsonl
    artifacts/*
    subagents/<child_id>/{sessions,runs,patch.diff}
```

Session 只保存 `active_run_id`。恢复会重放 Run Log、修复末尾未完成的 Tool 事务，并把新的
resume 请求作为 `user_guidance` Fact 持久化。文件工具不能访问 `.git/` 或 `.pico/`；固定
Verification 则拥有当前用户的宿主权限，不能被描述为 Sandbox。

## 阅读与演示

- [15 分钟与七天阅读路径](docs/learning-path.md)
- [Runtime 架构与真实执行路径](docs/architecture/agent-runtime.md)
- [状态所有权](docs/architecture/state-ownership.md)
- [面试讲解与 Demo](docs/review-pack/interview-demo.md)
- [恢复/简历表述](docs/resume-project.md)
- [确定性 Runtime 评测](docs/metrics/runtime-evaluation.md)

五分钟现场只运行：

```bash
uv run python scripts/day7_runtime_capstone.py
```

追问 Crash Recovery、Completion 或 Child delegation 时再运行 Day 6。完整回归与机制评测不在
现场展开：

```bash
uv run pytest -q
uv run ruff check pico applications tests scripts evals
uv run python scripts/run_evaluations.py
```

## Extensions

[Project Memory](extensions/project_memory/README.md) 是未安装、未加载、无 Core 开关的参考扩展。
导入 `pico` 不会创建 Memory 目录、注入 Prompt 内容或注册 Memory 工具。

Pico 不做多 Provider、XML 工具协议、Skills、MCP、多租户、远程 Worker、分布式调度或旧状态迁移。
