# Pico

Pico 是一个面向可信本地仓库的 Coding Agent Runtime。模型负责决定下一步和主动运行测试，Runtime 负责上下文、工具执行、持久化、恢复和完成边界。

```text
User request
    ↓
Pico → AgentLoop → ModelClient
          │
          ├── PromptBuilder：组装任务、历史、仓库规则和工作区状态
          ├── ToolRuntime：校验并执行工具
          └── RunLog：追加执行事实并更新 RunProjection
```

项目不包含 RepoMap、Subagent、Child Worktree 或 Patch Integration。

第一次读代码请从 [面试阅读主线](READING.md) 开始。按一次任务的调用顺序阅读，再根据追问进入恢复和文件安全细节。

## 快速开始

```bash
uv sync
uv run pico --cwd /path/to/repo --mode ask "解释项目入口"
uv run pico --cwd /path/to/repo --mode code "修复这个问题"
uv run pico --cwd /path/to/repo --resume latest "继续任务"
```

模型配置通过环境变量提供：

```dotenv
PICO_OPENAI_API_KEY=your-key
PICO_OPENAI_API_BASE=https://api.deepseek.com
PICO_OPENAI_MODEL=deepseek-v4-flash
PICO_OPENAI_REASONING_EFFORT=none
```

CLI 只提供 `--cwd`、`--resume`、`--mode`、`--model` 和 `--trace`。内部预算由 `PicoConfig` 管理；模型温度和请求超时通过环境变量配置。

`pico/config.py` 定义 Runtime 配置，`pico/env.py` 只负责加载项目环境变量。

## 核心对象

- `Pico`：组装模型、工作区、Session、工具和运行依赖。
- `Session`：只保存会话 ID、工作区归属和当前 `active_run_id`。
- `RunLog`：一个任务一次 Run，按顺序追加用户请求、模型调用、工具阶段、失败和终态。
- `RunProjection`：用同一套规则消费实时事件和历史事件，得到当前 Run 状态。
- `PromptBuilder`：在模型窗口预算内组装当前任务、历史、规则和工作区信息。
- `ToolRuntime`：统一处理工具 Schema、模式权限、写入范围、审批、执行阶段和结果。

Session 不保存完整对话：

```text
.pico/sessions/<session-id>/
├── session.json                 # id / workspace_root / active_run_id
└── runs/<run-id>/
    ├── events.jsonl             # 当前任务的完整执行历史
    └── artifacts/               # 大工具输出和 Runtime 反馈
```

一个 Session 可以连续运行多个任务；`active_run_id` 只在任务未完成时指向需要恢复的 Run。

长 Run 在没有 Pending Intent 的稳定边界保存可重建 Checkpoint。Checkpoint 只保存一次 Run 身份与日志游标，以及恢复所需的最小状态和有效 History。恢复优先读取
这些状态和 Checkpoint 后的 Event 尾部；Checkpoint 缺失或损坏时从
当前格式的完整 RunLog 重建。旧 Run 格式不迁移、不兼容。

## 上下文与压缩

`context_budget_tokens` 是 Pico 的运行预算，不是模型的实际窗口。默认预算为 272,000 Token，预留 32,000，因而在约 240,000 Token 时触发压缩；近期历史保留预算为 20,000，摘要输出上限为 16,000。上限不会预先占用或产生对应数量的计费 Token。

CLI 可通过 `PICO_CONTEXT_BUDGET` 设置运行预算，通过 `PICO_MODEL_CONTEXT_WINDOW` 声明当前模型支持的窗口；设置后启动时校验预算不能超过窗口。模型窗口由用户依据 Provider 文档配置，不自动推断。未配置模型窗口时，只校验 Pico 内部预算关系，不能保证适配任意模型。更换模型时需同步更新窗口配置。

例如当前 DeepSeek-V4-Flash 可配置 `PICO_MODEL_CONTEXT_WINDOW=1000000`、`PICO_CONTEXT_BUDGET=272000`。旧配置字段 `provider_context_limit_tokens` 已移除，不提供别名。

Pico 使用 Pi 风格的滚动摘要，不要求模型维护第二套任务笔记：

```text
完整 RunLog
→ 上一次摘要 + 新增的较早历史
→ 新的结构化摘要
→ 摘要 + 近期完整交互 + 最新用户请求 + 当前真实状态
```

摘要固定保存 Goal、Constraints & Preferences、Progress、Key Decisions、Next Steps 和 Critical Context。较新的用户纠正覆盖冲突的旧摘要；只有实际工具结果可以证明执行事实。工具调用与结果按完整事务保留，旧的大结果在摘要输入中裁剪，原始 RunLog 和 Artifact 不被摘要改写。当前 Workspace 和根 `AGENTS.md` 每次由 Runtime 获取；不发现或加载嵌套规则。摘要是有损历史上下文，不能作为权限或执行事实。

## 一次任务

1. CLI 创建或加载 Session，通过唯一的 `Pico(..., session=session)` 入口组装 Runtime。
2. `Pico.ask()` 创建新 Run，恢复时继续 Session 指向的未完成 Run。
3. `PromptBuilder` 从 RunLog 投影出模型需要的上下文。
4. 模型返回工具调用或 `submit_final`。
5. `ToolRuntime` 把只读与执行前拒绝保存为单条 Exchange；潜在副作用先提交 Intent，执行后提交 Settlement。
6. 中断恢复只检查未完成 Intent 的当前状态，不自动重放副作用操作。
7. `CompletionController` 检查任务边界和已跟踪文件的工作区漂移，再接受模型声明的完成结果。

## 工具与安全边界

主要工具包括 `list_files`、`read_file`、`read_artifact`、`search`、`run_shell`、`write_file`、`edit_file` 和 `submit_final`。模型每轮可以返回最多八个彼此独立的调用，Runtime 按模型顺序串行执行并一次返回全部结果；`submit_final` 必须独占一轮。模型通过 `run_shell` 主动运行测试、构建、lint、类型检查和复现命令，并根据结果继续修复；`submit_final` 不会偷偷执行额外命令。

`RunEvidence` 只维护文件变化和不确定副作用。完整工具历史保存在 RunLog 中；模型和工具用量统计保留在 Metrics 中。

- Ask 模式只读。
- Code 模式中的命令和文件修改需要审批。
- Auto 模式允许受限文件修改并开放通用命令；由于 Pico 没有 Shell 沙箱，`run_shell` 仍然要求用户审批。
- `run_shell` 可以产生正常的测试、构建和 snapshot 输出；审批是这类主机副作用的授权边界。模型应优先用文件工具修改源码，以保留读取版本检查和原子替换。
- 文件路径必须位于 Workspace 内，`.git` 和 `.pico` 不对模型开放。
- `edit_file` 只接收路径和替换内容；Runtime 内部绑定本轮读取到的 Revision，写入前再次检查，并通过临时文件原子替换。
- 执行意图和修改前版本标识先落盘；中断后根据记录与当前文件状态判断未修改、已修改或未知，不盲目重放。
- 模型负责选择并运行相关测试，Runtime 不把普通测试结果冒充为自然语言任务的独立验收；恢复出的不确定副作用作为事实反馈模型，由模型观察当前工作区后继续，不自动重放原工具。
- 模型结果明确区分完成、截断、服务失败和协议错误；截断调用不会执行。工具失败按工具名、完整参数、错误码和错误详情比较，忽略参数键顺序及调用 ID；连续三次相同失败提示改变策略，第四次停止。不同失败或成功工具会结束当前连续计数，交替循环由总轮数和时间限制兜底。
- Shell 命令是非交互式执行，默认 stdin 为 EOF；`timeout_seconds` 默认为 120，允许 1～600 秒且始终受 Run 剩余期限约束。POSIX 非阻塞管道和 `selectors` 持续排空输出；总内存上限为 1 MiB，stdout/stderr 各保留固定大小的 Head 与 Tail，并报告中间省略字节。超时会清理进程组，输出收集有固定清理期限，结束时不无限等待 EOF；主动脱离进程组的后代不保证被终止。模型请求通过异步任务取消结束网络等待。

## 阅读顺序

只按下面顺序阅读即可：

1. `pico/runtime.py`
2. `pico/agent_loop.py`
3. `pico/run_lifecycle.py`
4. `pico/tool_runtime.py`
5. `pico/run_log.py` 与 `pico/run_projection.py`
6. `pico/prompt_builder.py`

Provider 使用 `AsyncOpenAI` 调用兼容 Responses API 的服务，对外仍提供同步调用。一个 Model Client 在 Session 内复用 `asyncio.Runner`、SDK Client 和 HTTP 连接池；每次请求只创建独立任务与响应流。取消或截止时间到达时仅取消当前任务，后续请求继续复用可用连接；CLI 退出时统一关闭 Client。消息回放状态保留在 Pico Adapter 中。同步入口不应直接在已有 asyncio 事件循环的线程里调用。命令执行、Artifact 和底层 Git 状态采集第一次阅读主链时可以跳过。

## 文件版本与结果展示

Pico 保留单次编辑 Diff、内部修改路径和版本标识，不生成跨 Run 恢复的累计净 Diff，不保存完整文件前像，也不提供历史撤销。工具输出及日志中的编辑片段仍会落盘，但不是整文件备份。未完成操作通过 Intent 与当前 Revision 结算；完成前只检查任务边界和已跟踪文件的工作区漂移。

旧版含前像或 final_diff 的事件／Checkpoint 不提供迁移和兼容。已有历史文件不自动删除；请使用新 Session。旧运行目录仍可由用户自行保留或清理。
