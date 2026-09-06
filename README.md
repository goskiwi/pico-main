# Pico

Pico 是一个轻量、本地、单协议的 Coding Agent Runtime。模型每轮通过
OpenAI-compatible Responses function calling 提出一个动作或独立 Observation Batch；Runtime 掌握工具准入、
副作用、持久化、恢复、验证和最终完成权。CLI 还提供一次一个的显式 Child 委派。

Pico 面向用户已经信任的本地仓库。Code 模式允许模型申请一个需要 Approval 的诊断型
`run_command`；Ask/Auto 不暴露通用 Shell。CLI 自动发现 `tests/test_*.py` 并使用当前 Python
解释器运行 Pytest；特殊项目可用 `--verify-command` 覆盖。命令在 Workspace
中以当前用户权限执行，不是 Sandbox，文件工具的路径约束不能限制其访问其他目录、网络或
子进程。Pico 检测 Repository 可见净变化：Git 仓库使用 Diff、Untracked state 与 HEAD，非 Git
Workspace 使用有界 metadata snapshot；它不追踪 ignored 文件或外部系统副作用。未知仓库、
未知 PR 或其他不可信代码必须放到 Pico 外部的 CI、VM 或容器中运行。

## 快速开始

需要 Python 3.10+ 和 uv；Runtime 不依赖 Docker。

```bash
uv sync

uv run pico \
  --mode code \
  --cwd /path/to/trusted/repo \
  "Fix calculator.add so the existing test passes"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

默认后端要求 `PICO_OPENAI_API_KEY`；有意连接无认证的本机兼容端点时，显式传入
`--base-url http://127.0.0.1:PORT/v1`。
Provider 失败会区分传输错误与 HTTP／Responses 错误，保留底层异常类型、重试次数、状态码
及服务端错误信息；真实评测记录沿用项目的脱敏边界。

用户提交自然语言目标，并显式选择 `ask / code / auto` 模式；默认是 `code`。Runtime 不调用
隐藏分类模型。TaskContract 只保存原始目标、创建时的最大写能力、验证要求和写路径范围；
Resume 可以收窄但不能扩大原 Contract。`ask()` 返回结构化 `RunOutcome`，持久事实仍以
`RunStore.replay(run_id)` 为准。

CLI 从仓库根目录到启动目录依次加载适用的 `AGENTS.md`，以 32 KiB 总上限放入首个 user
input 的独立 `repository_instructions`，不把它提升为 system policy，也不混入普通不可信
仓库 Context。当前用户任务冲突时优先；Mode、工具权限、路径和完成规则仍由 Runtime 代码决定。

## 核心运行链

```text
User request + Ask/Code/Auto -> TaskContract
  -> AgentLoop: ModelAction(tool / invalid / final)
  -> ToolRuntime: admission -> tool_started -> Runner -> tool_result
  -> RunLog: append-only, sequenced, fsynced Facts
  -> RunProjection: Task + Evidence + Metrics + one Pending Tool transaction
  -> CompletionController
  -> Runtime Verification + Final Diff + RunOutcome
```

面试主线只需要六点：

1. **单一事实源**：每个 Run 只有一个 Run Log。实时调用 `RunLog.append`，内部依次执行
   `apply_event` 构造并验证待提交状态、存储追加、发布已验证状态；回放使用同一套转换。
2. **可恢复工具事务**：单调用使用 `assistant_tool_call`；多个纯 Observation 原子写成
   `assistant_tool_batch`。每个 Call 都有 Started/Result，崩溃后逐 Call 闭合且不盲目重放。
3. **副作用感知调度**：`list_files/read_file/search/read_artifact` 可组成最多四个调用的并行 Batch；
   任何执行、写入、状态、编排或完成动作必须独占一轮。Runner 可并行，RunLog/Projection/Artifact
   始终由主线程按模型原始顺序提交。
4. **Revision-bound 原子修改**：`edit_file` 携带读取时 Revision，提交点再次复验后原子替换，
   外部修改不会被静默覆盖。
5. **证据驱动完成**：模型调用 `submit_final` 后，Runtime 检查净变化与不确定副作用，运行用户
   固定 Verification，并生成真实 Final Diff；模型不自行运行 verifier 或生成 Final Diff。
6. **有界循环**：一次活跃 ask/resume 最多 32 个主 Agent Turn 和 600 秒；Child 与 Integration
   继承 Parent Deadline，不重新获得预算。

关键因果 payload（Task、Tool Call/Started/Result、Verification 与终态）使用严格 Schema；
Telemetry、Compaction 等观测 payload 保持可扩展。事件 envelope、sequence 和 event ID 仍会
在持久化读取与 Replay 时校验。

## 真实 CLI 工具面

CLI 默认注册以下十一个原生工具。每轮只把当前 Mode、TaskContract 和工具预算允许的 Schema 发送
给 Provider；ToolRuntime 在本机再次执行准入。

程序化 `Pico(..., check_runner=...)` 可选安装 `run_check`：在明确配置的隔离执行器中
运行临时 Python／pytest 复现，Code／Auto 可用，Ask 不暴露。普通 CLI 默认不安装此工具。
执行器由调用方提供，接口与信任边界见 [复现检查说明](docs/review-pack/isolated-checks.md)。

前四个只读工具可组成 Observation Batch；其余工具必须单独调用。Mixed Batch 在执行前整批
拒绝，但仍为每个 Call ID 写入一个 `rejected/not_started` Result。

| 工具 | Ask | Code | Auto | 作用 |
|---|:---:|:---:|:---:|---|
| `list_files` | ✓ | ✓ | ✓ | 列出 Workspace 文件 |
| `read_file` | ✓ | ✓ | ✓ | 按行读取并返回 Revision |
| `read_artifact` | ✓ | ✓ | ✓ | 分页读取当前 Run 的大输出 |
| `search` | ✓ | ✓ | ✓ | 有界搜索 Workspace |
| `run_command` | — | 询问 | — | 可信本机诊断；Auto 不暴露通用 Shell |
| `write_file` | — | 询问 | 自动 | 只创建不存在的文件 |
| `edit_file` | — | 询问 | 自动 | 按 Revision 修改已有文件 |
| `update_working_state` | ✓ | ✓ | ✓ | 增量维护当前 Run 的约束、决定和下一步 |
| `delegate` | — | ✓ | ✓ | 同步运行一个受限 Child |
| `integrate_child` | — | 询问 | 自动 | 显式验证并集成 Implement Child Patch |
| `submit_final` | ✓ | ✓ | ✓ | 请求 Runtime 进行最终完成检查 |

CLI 的 `build_agent()` 默认安装 Child runner，因此真实 CLI 请求会携带 `delegate` 与
`integrate_child`。程序化 `Pico(...)` 默认是单 Agent；只有显式传入
`subagent_model_client_factory` 才加载 Child 工具。交互模式使用 `/state` 查看当前 Run 的
WorkingState。

## Child 委派边界

`delegate` 一次只运行一个同步 Child，角色为 `explore` 或 `implement`；Child 没有
`delegate`，不能嵌套委派。这里没有 DAG、批量请求、后台队列或并行 worker。

- Explore 直接读取 Parent Workspace，但工具表面只读，不创建 Worktree。
- Implement 要求 Git 仓库根、无未归属变更的 Parent、可解析的 HEAD、非空 `allowed_write_paths` 和固定
  Verification 命令；它始终在独立 Worktree 中运行，返回摘要，有实际修改时才附带不可变 Patch receipt。
  修改范围来自 RunChangeSet，包含明确修改的 Git 忽略文件；交付失败会保留 Worktree 并报告路径。
- Implement 不自动 merge。Parent 调用 `integrate_child(child_id)`；Runtime 在临时 Worktree 组合已接纳的父修改和新 Patch，
  使用 Git 三方应用并验证组合结果，确认父状态未漂移后再按 revision 写回本次差异。
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

Session 保存会话 ID、Workspace 归属和 `active_run_id`，不保存对话历史。恢复会重放 Run Log、修复末尾未完成的 Tool 事务，并把新的
resume 请求作为 `user_guidance` Fact 持久化。文件工具不能访问 `.git/` 或 `.pico/`；固定
`run_command` 和 Verification 都拥有当前用户的宿主权限，不能被描述为 Sandbox。
两者共用 Repository 净状态观察：HEAD、staged/unstaged diff 与非忽略 untracked revision。
`none` 只表示没有观察到该范围内的变化，不表示整台机器没有副作用。

## 阅读与演示

- [15 分钟与七天阅读路径](docs/learning-path.md)
- [Runtime 架构与真实执行路径](docs/architecture/agent-runtime.md)
- [状态所有权](docs/architecture/state-ownership.md)
- [面试讲解与 Demo](docs/review-pack/interview-demo.md)
- [恢复/简历表述](docs/resume-project.md)

五分钟现场只运行：

```bash
uv run python scripts/day7_runtime_capstone.py
```

追问 Crash Recovery、Completion 或 Child delegation 时再运行 Day 6。完整回归与机制评测不在
现场展开：

```bash
uv run pytest -q
uv run ruff check pico applications tests scripts
```

测试按面试主线保留 Runtime 主链、恢复、工具安全、Provider 协议、上下文与 RepoMap、Child 集成和 Git 交付。
Day 1–7 与真实 LLM 场景用于演示和核实这些行为；压缩报告的断言用于保证面试证据准确。
外部仓库题库、Docker 判分、成绩统计及旧审查复现脚本已移除。
默认回归不调用 LLM，也不代表当前代码通过真实模型验收；历史报告保留为运行记录。

## Scope

Pico 不做 Project Memory、Triage、全量 OSS 排行榜评测、多 Provider、XML 工具协议、Skills、
MCP、多租户、远程 Worker、分布式调度或旧状态迁移。这些外围实现不在当前面试分支中。

搜索工具依赖系统安装的 ripgrep（`rg`），没有 Python 正则回退。缺少 rg 时仅搜索返回明确的能力错误。
