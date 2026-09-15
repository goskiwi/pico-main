# Pico

Pico 是一个面向可信本地仓库的 Coding Agent Runtime。模型负责决定下一步和主动运行测试，Runtime 负责上下文、工具执行、持久化、恢复和完成边界。

```text
User request
    ↓
Pico → AgentLoop → ModelClient
          │
          ├── PromptBuilder：构造 System Prompt 和按时间排列的 Messages
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
PICO_MODEL_CONTEXT_WINDOW=1000000
PICO_MODEL_INPUT_LIMIT=
PICO_OPENAI_REASONING_EFFORT=none
```

当前生产接入只实现 OpenAI-compatible **Responses API**，不是 Chat
Completions、Anthropic `/messages` 或 OpenAI Agents SDK。更换 Base URL 的
前提是服务端真正兼容 Responses 的流式事件、函数调用和函数结果格式；Pico
内部以 `ModelMessage`、`AssistantTurn`、`ModelAction`、`ToolCall` 和 `ToolOutcome` 隔离 Provider 数据结构；只有 Provider Adapter 使用 Responses Input Item。

CLI 只提供 `--cwd`、`--resume`、`--mode`、`--model` 和 `--trace`。内部预算由 `PicoConfig` 管理；模型温度和请求超时通过环境变量配置。

`pico/config.py` 定义 Runtime 配置，`pico/env.py` 只负责加载项目环境变量。

## 核心对象

- `Pico`：组装模型、工作区、Session、工具和运行依赖。
- `Session`：只保存会话 ID、工作区归属和当前 `active_run_id`。
- `RunLog`：一个任务一次 Run，按顺序追加用户请求、模型调用、工具阶段、失败和终态。
- `RunProjection`：用同一套规则消费实时事件和历史事件，得到当前 Run 状态。
- `PromptBuilder`：构造 System Prompt，并从 RunLog 恢复按时间排列的 User、Assistant 和 Tool Messages。
- `ToolRuntime`：统一处理工具 Schema、模式权限、写入范围、审批、执行阶段和结果。

Session 不保存完整对话：

```text
.pico/sessions/<session-id>/
├── session.json                 # id / workspace_root / active_run_id
└── runs/<run-id>/
    ├── events.jsonl             # 当前任务的完整执行历史
    └── artifacts/               # 大工具输出
```

一个 Session 可以容纳多个 Run；`active_run_id` 是唯一恢复指针，并在首个 Run Event 之前持久化。指针存在但 Event 尚未写入时视为空启动并清除，不扫描或猜测 orphan Run。已完成 Run 之间不继承对话历史，后续任务只从当前 Workspace 重新观察代码事实；只有恢复同一个未完成 Run 时才继续原任务上下文。

长 Run 在 `ready_for_model` 稳定边界保存可重建 Checkpoint。Checkpoint 直接保存 RunProjection、ContextState 和日志游标；ContextState 只包含滚动摘要与摘要后的近期完整事件。恢复优先读取
这些状态和 Checkpoint 后的 Event 尾部；Checkpoint 缺失或损坏时从
当前格式的完整 RunLog 重建。旧 Run 格式不迁移、不兼容。

## 上下文与压缩

`PicoConfig.context_limit_tokens` 是可选的 Runtime Combined Context 上限；模型 Adapter 负责声明实际窗口。`Pico.effective_context_limit_tokens` 取两者中存在的较小值。`Pico.effective_input_limit_tokens` 再取 `Context Window - max_output_tokens` 与 Provider 独立 Input Limit 的较小值；System Prompt 和 Messages 的预算从中继续扣除 Tool Schema。`max_output_tokens` 默认为 32,000，`recent_history_tokens` 为 20,000。摘要请求使用同一 Input Limit。

CLI 可通过 `PICO_CONTEXT_LIMIT` 设置 Pico Combined Context 上限，通过 `PICO_MODEL_CONTEXT_WINDOW` 声明模型窗口；Provider 另有更小输入上限时，通过 `PICO_MODEL_INPUT_LIMIT` 声明。模型能力由用户依据 Provider 文档配置，不自动推断；更换模型时需同步更新。

例如当前 DeepSeek-V4-Flash 可配置 `PICO_MODEL_CONTEXT_WINDOW=1000000`；只有希望 Pico 主动采用更小窗口时才设置 `PICO_CONTEXT_LIMIT`。旧的 Context、Reserve 和 Summary 配置字段均已移除，不提供别名。

Pico 使用 Pi 风格的滚动摘要，不要求模型维护第二套任务笔记：

```text
完整 RunLog
→ 上一次摘要 + 新增的较早历史
→ 新的结构化摘要
→ 摘要 + 近期用户指导与完整工具事务 + 当前真实状态
```

`TaskContract.goal` 是唯一目标来源。CompactedContext 保存 Constraints、Progress、Key Decisions、Next Steps 和 Critical Context；公开 Assistant 文字与完整 Tool Call 先作为 `assistant_turn` 持久化，隐藏推理不保存。尚未被 Compaction 覆盖的用户补充全部以可信原文提供，较早补充进入约束和进度摘要。摘要输入沿用 Pi 的简单边界：User、Assistant、Tool Call Arguments、状态、失败、路径和 metadata 完整提供，只把较早 Tool Result 的 `content` 截到2,000字符；存在 Artifact 时保留可读取的 `artifact_id`。Runtime 另行确定生成 `read_files` 与 `modified_files`，修改路径优先于只读路径；Shell 不做虚假路径归因。必保内容仍放不下时 Compaction 明确失败，不提交残缺摘要。

`TaskContract` 还固定 Run 创建时的 `mode`、`allowed_tools` 和 `write_scope`。恢复时当前 Runtime 配置只能与这份授权取更严格的交集，不能扩大旧 Run 权限。Runtime 的有效权限进入 System Prompt；AGENTS.md、Environment Context、原始用户请求、Compacted Summary 和摘要后的消息按真实时间顺序进入 `ModelPrompt.messages`。恢复时 Provider Adapter 将同一组 Messages 转换成原生 Responses Message、Function Call 和 Function Call Output，不把历史降级成一段文本。

Runtime 只维护一个当前 Context 用量：首次构造或恢复时来自本地完整请求估算，模型响应后由 Provider Usage 加新增 Tool Result 估算覆盖。达到高水位时先 Reset，随后同一个计数由重建 Prompt 的本地估算覆盖并重新判断；重建值仍达到 Input Limit 才 Compaction。Provider 明确返回 Context Overflow 时直接强制尝试摘要。任一时刻只有一个当前值，不比较两份快照。

Provider 提供 `output_tokens_details.reasoning_tokens` 时，Pico 将其作为可选 Usage 明细写入 Assistant Turn、RunMetrics 和 Trace。Reasoning Token 已包含在 `output_tokens` 中，Context 计算仍只使用总 `output_tokens`，不会重复相加。

Environment Context 是轻量启动快照：Git short status 在 Workspace 层最多保留 2,000 字符并明确标记截断，PromptBuilder 不再重复设置 Token 上限；如果整个模型输入仍放不下，Environment Message 可以省略，模型按需通过工具重新观察当前状态。

System Prompt 只保存稳定规则和有效 Run 权限。当前失败纠正不进入 System Prompt：正常工具失败由对应 Tool Result 承载；恢复或 Context Reset 时需要重建的纠正作为最后一条 Developer Message 提供，因此不会提高成长期规则或破坏消息时间关系。

## 一次任务

1. CLI 创建或加载 Session，通过唯一的 `Pico(..., session=session)` 入口组装 Runtime。
2. `Pico.ask()` 创建新 Run，恢复时继续 Session 指向的未完成 Run。
3. `PromptBuilder` 从 RunLog 构造 System Prompt 和按时间排列的 Messages。
4. Runtime 先记录完整 `assistant_turn`，其中包含公开文字与有序 Tool Call。
5. 所有工具统一返回 `tool_result`；潜在副作用在执行前额外提交 `tool_started`。
6. 中断恢复根据 Assistant Turn、已完成结果和 Started 状态区分 completed、started 与 not_started，不盲目重放。
7. `submit_final` 形成 final Assistant Turn；Runtime 确认任务未取消后记录终态。

Run 表示可跨进程恢复的持久任务；每次首次执行或 Resume 是一个 Attempt。模型请求数量和执行期限按 Attempt 限制，崩溃前已经持久化的工具事实仍属于同一个 Run。

## 工具与安全边界

主要工具包括 `list_files`、`read_file`、`read_artifact`、`search`、`run_shell`、`write_file`、`edit_file` 和 `submit_final`。模型每轮可以返回最多八个彼此独立的调用，Runtime 按模型顺序串行执行并一次返回全部结果；`submit_final` 必须独占一轮。模型通过 `run_shell` 主动运行测试、构建、lint、类型检查和复现命令，并根据结果继续修复；`submit_final` 不会偷偷执行额外命令。

完整执行事实保存在 RunLog 中；当前任务、Assistant Turn、Pending Tool、连续失败状态和 Metrics 由 RunProjection 重建。模型纠错提示由失败状态临时生成。Checkpoint 只在 `ready_for_model` 稳定边界生成，直接保存 RunProjection、ContextState 和日志游标；崩溃中的阶段由 Tail Replay 恢复。状态查询只读取这两个现成视图，不维护第三份状态。

- Ask 模式只读。
- Code 模式中的命令和文件修改需要审批。
- Auto 模式允许受限文件修改并开放通用命令；由于 Pico 没有 Shell 沙箱，`run_shell` 仍然要求用户审批。
- `run_shell` 可以产生正常的测试、构建和 snapshot 输出；审批是这类主机副作用的授权边界。Runtime 不归因 Shell 修改的具体路径，因此其结果标记为 `untracked`，而不是声称没有副作用。模型应优先用文件工具修改源码，以获得精确替换、原子写入和可恢复记录。
- 文件路径必须位于 Workspace 内，`.git`、`.pico` 和真实 `.env` 文件不对模型开放；`.env.example`、`.env.sample` 仍可读取。
- `edit_file` 在执行时读取当前文件，只替换唯一匹配的 `old_text`，并通过同目录临时文件原子替换。它不把模型早先读取的整文件 Revision 作为局部修改的写入条件。
- 执行意图和修改前状态标识先落盘；中断后根据记录与当前文件状态判断未修改、已修改或未知，不盲目重放。
- 模型负责选择并运行相关测试，Runtime 不把普通测试结果冒充为自然语言任务的独立验收；恢复出的不确定副作用作为事实反馈模型，由模型观察当前工作区后继续，不自动重放原工具。
- 模型结果明确区分完成、截断、服务失败和协议错误；截断调用不会执行。工具失败按工具名、完整参数、错误码和错误详情比较，忽略参数键顺序及调用 ID；连续第三次相同工具失败把固定 `retry_instruction` 附在对应 Tool Result 中，不重建 Context，第四次停止。当前多工具批次若随后取得成功，失败 streak 与尚未发送的提醒一起取消。不同失败或成功工具会结束当前连续计数，交替循环由总轮数和时间限制兜底。
- Shell 命令是非交互式执行，默认 stdin 为 EOF；`timeout_seconds` 默认为 120，允许 1～600 秒且始终受当前 Attempt 剩余期限约束。POSIX 非阻塞管道和 `selectors` 持续排空输出；总内存上限为 1 MiB，stdout/stderr 各保留固定大小的 Head 与 Tail，并报告中间省略字节。超时会先终止再强制清理整个进程组，输出收集有固定清理期限，结束时不无限等待 EOF；主动脱离进程组的后代不保证被终止。模型请求通过异步任务取消结束网络等待。

## 阅读顺序

只按下面顺序阅读即可：

1. `pico/runtime.py`
2. `pico/agent_loop.py`
3. `pico/run_lifecycle.py`
4. `pico/tool_runtime.py`
5. `pico/run_log.py` 与 `pico/run_projection.py`
6. `pico/prompt_builder.py`

Provider 使用 `AsyncOpenAI` 调用兼容 Responses API 的服务，对外仍提供同步调用。一个 Model Client 在 Session 内复用 `asyncio.Runner`、SDK Client 和 HTTP 连接池；每次请求只创建独立任务与响应流。取消或截止时间到达时仅取消当前任务，后续请求继续复用可用连接；CLI 退出时统一关闭 Client。消息回放状态保留在 Pico Adapter 中。同步入口不应直接在已有 asyncio 事件循环的线程里调用。当前没有第二个生产 Provider Adapter，因此项目不宣称已经完成多厂商协议适配。命令执行、Artifact 和底层 Git 状态采集第一次阅读主链时可以跳过。

## 文件修改与结果展示

Pico 保留单次编辑 Diff、内部修改路径和前后状态标识，不生成跨 Run 恢复的累计净 Diff，不保存完整文件前像，也不提供历史撤销。工具输出及日志中的编辑片段仍会落盘，但不是整文件备份。未完成操作只在恢复时对相关路径重新观察；普通完成不扫描或锁定整个 Workspace。

旧版含前像或 final_diff 的事件／Checkpoint 不提供迁移和兼容。已有历史文件不自动删除；请使用新 Session。旧运行目录仍可由用户自行保留或清理。
