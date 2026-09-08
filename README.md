# Pico

Pico 是一个轻量、本地、单协议的 Coding Agent Runtime。模型每轮通过
OpenAI-compatible Responses function calling 提出一个或多个工具调用；Runtime 掌握工具准入、
副作用、持久化、恢复、验证和最终完成权。CLI 还提供一次一个的显式 Child 委派。

Pico 面向用户已经信任的本地仓库。Code 模式允许模型申请一个需要 Approval 的诊断型
`run_command`；Ask/Auto 不暴露通用 Shell。CLI 自动发现 `tests/test_*.py` 并使用当前 Python
解释器运行 Pytest；特殊项目可用 `--verify-command` 覆盖。命令在 Workspace
中以当前用户权限执行，不是 Sandbox，文件工具的路径约束不能限制其访问其他目录、网络或
子进程。Pico 检测 Repository 可见净变化：Git 仓库使用 Diff、Untracked state 与 HEAD，非 Git
Workspace 使用有界 metadata snapshot；它不追踪 ignored 文件或外部系统副作用。未知仓库、
未知 PR 或其他不可信代码必须放到 Pico 外部的 CI、VM 或容器中运行。
CLI 注入终端 Approval handler；程序化 `Pico(...)` 未提供 handler 时，Code 模式的 risky Tool
默认拒绝且不会读取 stdin。

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
隐藏分类模型。TaskContract 只保存原始目标、创建时的最大写能力、最低验证要求和写路径范围；
Resume 可以收窄写能力但不能扩大原 Contract，当前启动配置的 Verification 可以把完成要求
收紧。`ask()` 返回结构化 `RunOutcome`，持久事实仍以
`RunStore.replay(run_id)` 为准。

CLI 从仓库根目录到启动目录依次加载适用的 `AGENTS.md`，以 32 KiB 总上限放入首个 user
input 的独立 `repository_instructions`，不把它提升为 system policy，也不混入普通不可信
仓库 Context。当前用户任务冲突时优先；Mode、工具权限、路径和完成规则仍由 Runtime 代码决定。

每个新 Prompt snapshot 还现场构造一个 `WorkspaceObservation`：明确区分 Git、普通目录和
Git 不可用，表达 branch、detached HEAD 或 unborn branch，并给出 staged、unstaged、untracked、
conflicted 数量、总 Diff 行数及有界路径列表。普通 Tool continuation 复用 Provider session，
不把同一个快照反复追加到上下文；RepoMap 再按当前任务和已观察路径提供代码入口。

## 核心运行链

```text
User request + Ask/Code/Auto -> TaskContract
  -> AgentLoop: ModelAction(tool / invalid / final)
  -> ToolRuntime: admission -> tool_started -> Runner -> tool_result
  -> RunLog: append-only, sequenced, fsynced Facts
  -> RunProjection: Task + Evidence + Metrics + one Pending Tool transaction
  -> CompletionController: blocked / verification_required / allowed
  -> RunLifecycle: Runtime Verification + terminal Final Diff + RunOutcome
```

面试主线只需要六点：

1. **单一事实源**：每个 Run 只有一个 Run Log。实时调用 `RunLog.append`，内部依次执行
   `apply_event` 构造并验证待提交状态、存储追加、发布已验证状态；回放使用同一套转换。
2. **可恢复工具事务**：每次模型响应统一写成一个 `assistant_tool_calls`，其中包含一个或
   多个有序 Call。每个 Call 都有独立 Started/Result，崩溃后逐 Call 闭合且不盲目重放。
3. **按工具能力调度**：工具默认独占，只有显式标记的读取工具可并行；混合调用按原顺序切成
   并行段和独占段。并行上限只控制同时运行数，不限制一个响应中的调用数。Runner 可并行，
   RunLog/Projection/Artifact 始终由主线程按模型原始顺序提交。
4. **可观察的原子修改**：`write_file` 以 hard-link 原子 create-only；`edit_file` 携带读取时
   Revision，提交点再次复验后原子替换。ToolRuntime 对声明路径观察真实 before/after 并生成
   transition，检测到 Revision 冲突时保留当前文件并要求重新读取。
5. **证据驱动完成**：每次 `submit_final` 解析一份不可替换的 Verification policy；Runtime
   检查净变化与不确定副作用，运行该 policy 的固定命令，并生成真实
   Final Diff。恢复时新增 verifier 会收紧旧 Run，删除 Contract 要求的 verifier 则关闭失败。
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

前四个只读工具显式声明为 parallel；其余工具默认 exclusive。同一响应可以混合多种工具，
Runtime 对每个调用独立准入，并在读写边界间建立顺序屏障；一个调用被拒绝不会取消合法兄弟调用。
`--max-parallel-tools` 只限制同时运行的 parallel Runner 数量（默认 4）；更多调用自动分波，
不会因为超过并发数而拒绝整个响应。

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

`read_file` 将 LF/CRLF 统一展示为 LF；`edit_file` 按同样的换行语义匹配唯一文本块，
新增换行沿用修改位置的格式，块外字节保持不变。Revision 和前像始终对应原始文件字节。

## Child 委派边界

`delegate` 一次只运行一个同步 Child，角色为 `explore` 或 `implement`；Child 没有
`delegate`，不能嵌套委派。这里没有 DAG、批量请求、后台队列或并行 worker。

- Explore 直接读取 Parent Workspace，但工具表面只读，不创建 Worktree。
- Implement 要求 Git 仓库根、无未归属变更的 Parent、可解析的 HEAD、非空 `allowed_write_paths` 和固定
  Verification 命令；它始终在独立 Worktree 中运行，返回摘要，有实际修改时才附带不可变 Patch receipt。
  修改范围来自 RunChangeSet，包含明确修改的 Git 忽略文件；Patch 持久化后立即清理 Worktree，失败和
  取消也走相同的有界资源结算。
  Child 可以使用 `read_artifact` 读取自己 Run 的完整工具输出。
- Implement 不自动 merge。Parent 调用 `integrate_child(child_id)`；Runtime 在临时 Worktree 组合已接纳的父修改和新 Patch，
  使用 Git 三方应用并验证组合结果，确认父状态未漂移后再按 revision 写回本次差异。
- 未集成的 Implement Child 会阻止 Parent 完成。

Child 身份、base、verifier 和计划 Worktree 路径在任何 Child 资源创建前进入 Parent 的
`tool_started.operation`；Child Run Log、Session、Artifact 和 Patch 位于 Parent Run 的
`subagents/<child_id>/` 下。若 Parent 在最终 delegate Result 前中断，恢复会以同一个 Child ID
记录 `child_interrupted` 并清理计划 Worktree；不会接纳或重跑这次同步 Child。
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

该演示只预设模型动作；文件修改、pytest 和 RunLog 回放实际执行，并显示修改前失败、修改后通过。

追问 Crash Recovery、Completion 或 Child delegation 时再运行 Day 6。完整回归与机制评测不在
现场展开：

```bash
uv run pytest -q
uv run ruff check pico applications tests scripts
```

运行时加 `--trace` 可以在终端实时查看请求、工具、压缩、恢复及验证过程：

```bash
uv run pico --trace --cwd /path/to/repo "修复测试"
```

Trace 写入 stderr 并立即刷新，默认关闭；不展开 Prompt、文件正文或最终回答。
并行工具结果按 RunLog 提交顺序显示，不代表工作线程的实际结束顺序。
恢复会话时只打印本次新增事件。Trace 不写入第二份持久化状态。

精简回归只保留 155 项面试 Core：AgentLoop、Completion、Context/Compaction、Provider
传输与协议、RunLog/Projection，以及路径安全。CLI 包装、评测脚本、自动 Git 交付、Child
附录和重复生产边界矩阵不再维护独立测试；Day 1–7 与保留的真实 LLM 报告用于演示这些外围路径。
外部仓库题库、Docker 判分、成绩统计及旧审查复现脚本已移除。
默认回归不调用 LLM，也不代表当前代码通过真实模型验收；历史报告保留为运行记录。

真实评测默认创建独立的临时工作区，完整路径写入报告。显式传入 `--workspace` 或
`--workspace-root` 时，实际的场景目录必须尚不存在；脚本不会清空已有目录。
代码正确性按真实验证结果判定；Ask 的指定证据和压缩场景的完整读取属于场景断言，
不构成普通 Runtime 任务的必读文件或读取次数门槛。

## Scope

Pico 不做 Project Memory、Triage、全量 OSS 排行榜评测、多 Provider、XML 工具协议、Skills、
MCP、多租户、远程 Worker、分布式调度或旧状态迁移。这些外围实现不在当前面试分支中。

搜索工具依赖系统安装的 ripgrep（`rg`），没有 Python 正则回退。缺少 rg 时仅搜索返回明确的能力错误。
