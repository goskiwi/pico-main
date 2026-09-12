# Pico

Pico 是一个面向可信本地仓库的 Coding Agent Runtime。模型负责决定下一步，Runtime 负责上下文、工具执行、持久化、恢复和最终验收。

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

CLI 只提供 `--cwd`、`--resume`、`--mode`、`--model` 和 `--trace`。内部预算由 `PicoConfig` 管理，项目验收由 Runtime 自动发现；模型温度和请求超时通过环境变量配置。

`pico/config.py` 定义 Runtime 配置，`pico/env.py` 只负责加载项目环境变量。

## 核心对象

- `Pico`：组装模型、工作区、Session、工具和运行依赖。
- `Session`：只保存会话 ID、工作区归属和当前 `active_run_id`。
- `RunLog`：一个任务一次 Run，按顺序追加用户请求、模型调用、工具阶段、验证和终态。
- `RunProjection`：用同一套规则消费实时事件和历史事件，得到当前 Run 状态。
- `PromptBuilder`：在模型窗口预算内组装当前任务、历史、规则和工作区信息。
- `ToolRuntime`：统一处理工具 Schema、模式权限、写入范围、审批、执行阶段和结果。

Session 不保存完整对话：

```text
.pico/sessions/<session-id>/
├── session.json                 # id / workspace_root / active_run_id
└── runs/<run-id>/
    ├── events.jsonl             # 当前任务的完整执行历史
    └── artifacts/               # 大工具输出、修改前像和最终 Diff
```

一个 Session 可以连续运行多个任务；`active_run_id` 只在任务未完成时指向需要恢复的 Run。

长 Run 在没有 Pending Intent 的稳定边界保存可重建 Checkpoint。恢复优先读取
Projection、有效 History 和 Checkpoint 后的 Event 尾部；Checkpoint 缺失或损坏时从
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

摘要固定保存 Goal、Constraints & Preferences、Progress、Key Decisions、Next Steps 和 Critical Context。较新的用户纠正覆盖冲突的旧摘要；只有工具与验证结果可以证明工作完成。工具调用与结果按完整事务保留，旧的大结果在摘要输入中裁剪，原始 RunLog 和 Artifact 不被摘要改写。当前 Workspace、根 `AGENTS.md` 和验证状态每次由 Runtime 获取；不发现或加载嵌套规则。摘要是有损历史上下文，不能作为权限或执行事实。

## 一次任务

1. CLI 创建或加载 Session，通过唯一的 `Pico(..., session=session)` 入口组装 Runtime。
2. `Pico.ask()` 创建新 Run，恢复时继续 Session 指向的未完成 Run。
3. `PromptBuilder` 从 RunLog 投影出模型需要的上下文。
4. 模型返回工具调用或 `submit_final`。
5. `ToolRuntime` 把只读与执行前拒绝保存为单条 Exchange；潜在副作用先提交 Intent，执行后提交 Settlement。
6. 中断恢复只检查未完成 Intent 的当前状态，不自动重放副作用操作。
7. `CompletionController` 检查副作用和验证结果，再允许 Runtime 完成任务。

## 工具与安全边界

主要工具包括 `list_files`、`read_file`、`read_artifact`、`search`、`run_shell`、`write_file`、`edit_file`、`verify` 和 `submit_final`。每轮只接受一个调用；有固定验收命令时才开放无参数 `verify`。

`verify` 由模型主动触发中途检查；任务契约独立记录是否要求验收，`submit_final` 在要求验收时强制重新验证，即使工作区没有净修改。中途入口不能保证模型一定及时调用。

`RunEvidence` 只维护文件变化、不确定副作用、最新验证和最后修改序号。完整读取、修改和验证历史保存在 RunLog 中；模型和工具用量统计继续保留在 Metrics 中。

- Ask 模式只读。
- Code 模式中的命令和文件修改需要审批。
- Auto 模式允许受限文件修改，但不开放通用命令。
- 文件路径必须位于 Workspace 内，`.git` 和 `.pico` 不对模型开放。
- `edit_file` 只接收路径和替换内容；Runtime 内部绑定本轮读取到的 Revision，写入前再次检查，并通过临时文件原子替换。
- 修改前像和 Intent 先落盘；中断后根据记录与当前文件状态判断未修改、已修改或未知，不盲目重放。
- 模型的最终回答不是完成证明；Runtime 独立执行配置的验证并生成最终 Diff。
- 模型结果明确区分完成、截断、服务失败和协议错误；截断调用不会执行。工具失败按工具名、完整参数、错误码和错误详情比较，忽略参数键顺序及调用 ID；连续三次相同失败提示改变策略，第四次停止。不同失败或成功工具会结束当前连续计数，交替循环由总轮数和时间限制兜底。
- Shell 与验收命令使用 POSIX 非阻塞管道和 `selectors` 等待输出，同时检查取消和截止时间；只保留固定大小的前缀并报告丢弃字节。超时会清理进程组，输出收集有固定清理期限，结束时不无限等待 EOF；主动脱离进程组的后代不保证被终止。模型请求通过异步任务取消结束网络等待。

## 阅读顺序

只按下面顺序阅读即可：

1. `pico/runtime.py`
2. `pico/agent_loop.py`
3. `pico/run_lifecycle.py`
4. `pico/tool_runtime.py`
5. `pico/run_log.py` 与 `pico/run_projection.py`
6. `pico/prompt_builder.py`

Provider 使用 `AsyncOpenAI` 调用兼容 Responses API 的服务，对外仍提供同步调用。每次请求拥有独立的异步事件循环和 HTTP 客户端；取消或截止时间到达时取消请求任务，等待响应流与客户端清理。消息回放状态保留在 Pico Adapter 中，不依赖 HTTP 连接复用。同步入口不应直接在已有 asyncio 事件循环的线程里调用。命令执行、Artifact 和底层 Git 状态采集第一次阅读主链时可以跳过。
