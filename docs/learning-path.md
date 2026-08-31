# Pico 七天学习路径

这是 Pico 的唯一权威学习导航。架构事实以
[`agent-runtime.md`](architecture/agent-runtime.md) 和
[`state-ownership.md`](architecture/state-ownership.md) 为准；本文只决定第一次应该按什么
顺序阅读，不改变任何 Runtime 默认行为。

RepoMap 从 `Pico` 初始化开始默认启用；第一遍先把它当成“非空才进入 Prompt”的有界
仓库导航，到 Day 5 再阅读内部实现。Semantic Compaction、Subagents 和 Triage 也都不是
理解一次普通单 Agent 请求的前置条件。

## 15 分钟核心路径

先运行 `scripts/day7_runtime_capstone.py`，然后只读一个 owner、一个 caller 和一个结果：

1. `TaskIntentClassifier.classify()`：自然语言只生成内部 Intent，不授予工具权限；Resume 跳过分类。
2. `AgentLoop.run()`：只看 Tool、Invalid、Final 三个分支。
3. `AgentLoop._handle_tool_action()` → `ToolRuntime.execute()`：Call 由 Loop 接受并持久化，
   Started/Result 由 ToolRuntime 持久化。
4. `RunLog` 的 `_RunProtocol`、`append()`、`reconcile_interrupted()` 与 `replay_events()`：理解
   单 Pending 与 Crash recovery；第一遍在 `compact()` 前停止，不读 History projection。
5. `WorkspaceMutationService.edit()`：理解 Revision 在原子提交点再次复验。
6. `CompletionController.assess()`：按未集成 Child、TaskContract、不确定副作用、Drift、
   Verification 的顺序理解 Runtime 完成权。

到这里已经能完成面试主线。只有被追问 Child delegation 时才运行 Day 6；RepoMap、Provider
cache、Semantic Compaction、Triage 和完整 Evals 都留到后续章节。

## 六个核心 Ownership 文件

先记住下面六个文件分别拥有什么。其他文件会出现在真实调用链中，但只是协议、数据或
实现接缝，不另起一套核心 Ownership。

| 文件 | 核心 Ownership |
|---|---|
| [`pico/runtime.py`](../pico/runtime.py) | `Pico` 组合根、八个顶层组件以及 `ask()` 入口 |
| [`pico/agent_loop.py`](../pico/agent_loop.py) | 模型轮、工具轮、Provider 续接和停止条件的控制流 |
| [`pico/tool_runtime.py`](../pico/tool_runtime.py) | 模型可见工具的唯一公开执行边界，以及 Call/Started/Result 因果顺序 |
| [`pico/run_log.py`](../pico/run_log.py) | Run Fact 的严格协议、追加、Compaction 与中断对账 |
| [`pico/run_projection.py`](../pico/run_projection.py) | 从 Fact 重建 Task、Evidence、Metrics、单 Pending Call 和终态 receipt |
| [`pico/completion_controller.py`](../pico/completion_controller.py) | 是否允许模型最终提交的 Runtime 决策 |

阅读 Trace 时还会经过 `cli.py`、`run_lifecycle.py`、Prompt/Provider、`ToolContext`、具体
Runner、Evidence 和 Verification。它们是六个 Ownership 之间的真实接缝，不冒充第七个
核心状态源。

## 从一个真实 CLI 请求开始

下面的参数都由当前 CLI 提供。它表示“必须产生修改，并且必须通过显式验证”这一种任务：

```bash
uv run pico \
  --verify-command "python -m pytest -q" \
  --cwd /path/to/repo \
  "Fix calculator.add so the existing addition test passes"
```

真实 Provider 需要模型配置。模型没有通用 `run_shell`；示例中的固定 Verification 命令会
以当前用户权限在本机运行，因此只应对可信仓库使用。未知代码应先放进外部 CI、VM 或容器。
只想看确定性本地演示时，运行：

```bash
uv run python scripts/day7_runtime_capstone.py
```

Day 7 是只使用 Core Tool transaction 的 Capstone。

## 一次请求的逐步调用 Trace

第一遍只回答每一步的“输入、动作、输出”，不要同时展开所有辅助模块。

| 步骤 | 当前调用 | 第一遍要看懂的内容 |
|---:|---|---|
| 1 | `pico/cli.py: main -> build_agent` | CLI 只提交自然语言与 Runtime 配置，不要求用户填写 TaskContract |
| 2 | `pico/runtime.py: Pico.__init__ -> ask` | 默认构造 RepoMap、ToolRuntime、Prompt，加载可恢复 Run，并进入 AgentLoop |
| 3 | `pico/run_lifecycle.py: initialize -> _resume_or_create_run` | 新 Run 先用隔离结构化分类生成 TaskContract；恢复 Run 复用原 Contract，并在 Provider 前持久化 `user_guidance` |
| 4 | `pico/agent_loop.py: run -> _next_model_turn` | 每轮只处理 Tool、Invalid 或 Final 三种 ModelAction |
| 5 | `PromptBuilder -> ContextManager -> OpenAICompatibleModelClient` | 固定规则进 `instructions`；最小 Runtime policy、按需上下文和任务请求进首轮 `input`；原生 Schema 进 `tools`，支持时用 `allowed_tools` 动态限名 |
| 6 | `providers/clients.py: _action_from_response -> complete_action` | 只有恰好一个带 `call_id` 的 Function Call 才形成 Provider Pending Call |
| 7 | `AgentLoop._handle_tool_action` | Tool 执行前先持久化 `assistant_tool_call` Fact |
| 8 | `ToolRuntime.execute` | 按 call ID 取回持久化 ToolCall，完成准入、Approval、影响路径、Preimage、`tool_started`、Runner、ToolOutcome、`tool_result` |
| 9 | `ToolContext -> tools.tool_edit_file -> mutations` | Runner 只获得受限能力；Revision 在提交点复验后原子替换 |
| 10 | `RunLog.append -> RunProjection.apply_event` | 同一个新 Fact 如何同时推进 Pending、Metrics、WorkingState 和 Evidence |
| 11 | `CompletionController -> Verification -> RunLifecycle.finish_success` | TaskContract、净变化和当前验证如何决定完成，写入 `final_diff` 与 `assistant_final`，再从终态 Projection 返回非持久化 `RunOutcome` |

恢复是步骤 3 的侧支，建议理解一次正常 Tool 事务后再读。构造期只加载并安装 dormant
Run；真正的中断对账发生在下一次 `ask()` 初始化时：

```text
Pico.__init__
  -> load_resumable_run
  -> RunStore.load_run
  -> replay_events
  -> RunProjection.apply_event
  -> install dormant ActiveRunState

Pico.ask
  -> RunLifecycle.initialize
  -> RunLog.reconcile_interrupted
  -> append user_guidance
  -> run_resumed
```

`load_run` 从同一次持久化读取返回 Events 与 Projection；`RunStore.replay` 是只返回
Projection 的委托。Live 路径只调用 `apply_event`。

## 五层知识结构

| 层级 | 内容 | 第一次是否必学 |
|---|---|---|
| Core | 上述 CLI Trace、TaskContract、增量 WorkingState、单 Pending、工具安全、恢复、Completion 与 Final Diff | 是 |
| 默认上下文增强 | 非空 RepoMap 自动提供有界仓库导航 | 功能始终启用；空投影不发送；内部实现第二遍再学 |
| Context Pressure | Token Budget、Provider Session Rotation、Semantic Compaction、失败后的事务级 Fallback | 仅超长任务需要 |
| Orchestration Appendix | 单个 Explore/Implement Child、Git Worktree、显式 `integrate_child` | 单 Agent Core 完成后选学 |
| Applications | Triage Workflow、Report、真实 Fixture 与 Evals | 最后学习 |

Semantic Compaction 不是每轮执行：必须已有 Run Log、当前没有 Pending Call，并且本地估算与
Provider 报告的 Context Token 较大者超过
`total_budget - max(max_new_tokens, compaction_reserve_tokens)`，才进入准备分支；失败时只使用
完整 Tool 事务组成的 bounded fallback。

Subagents 在 CLI 中默认提供，因为 `build_agent()` 会安装 Child Model Client Factory；直接使用
`Pico(...)` API 时只有显式传入该 Factory 才启用。无论是否启用，普通单 Agent Core 都不依赖
Subagent 实现。

## 保留原始七天顺序

### Day 1：从 CLI 到 AgentLoop

- 从上面的真实 CLI 命令进入 `cli.py`，沿步骤 1～4 阅读。
- 运行 `scripts/day1_runtime_walkthrough.py`：它使用真实
  `build_arg_parser -> build_agent -> ask` 路径，只用 `FakeModelClient` 替换网络 Provider；
  依次展示 CLI 任务要求、八个顶层组件、全新 Session 的恢复探测和完整 `RunOutcome`。
- 确认 `RunOutcome` 是终态 Projection 的非持久化返回快照；Run Log 中没有第二种
  `run_outcome` Fact。
- RepoMap 此时只需要知道“默认存在”，不要打开其实现。

完成标准：能用 30 秒讲清用户请求如何进入 `AgentLoop`。

### Day 2：State、Fact 与 Projection

- 阅读 TaskContract、`pico/working_state.py` 的六字段 add/remove WorkingState、RunLog v15 和
  RunProjection；交互 CLI 使用 `/state` 查看这一当前 Run 投影。
- 运行 `scripts/day2_state_walkthrough.py` 的三段实验：
  1. 查看原始 v15 Fact，并比较 Live、`load_run` 与 `RunStore.replay` 的完整 Projection；
  2. Replay 合法 Event 前缀，观察单 Pending Call 在 Call、Started、Result 之间的变化；
  3. 用新 `Pico` 加载无副作用的中断调用，在下一次 `ask()` 自动对账且不盲目重放 Runner。
- `reload_required` 是未处理异常后的进程内缓存可信度标记。Day 6 先学习真实 Crash Resume
  和 Active Reset；需要故障注入细节时，再阅读 `tests/test_resume_runtime.py` 中的 ambiguous
  append / transient reload 回归。

完成标准：能解释 Fact 与 Projection 的区别，以及为什么不保存第二份 Task 快照。

### Day 3：Prompt 与 Provider

- 阅读步骤 5～6：`instructions`、`input`、`tools` 三个通道和 Function Call Output 回写。
- 区分 `declared_tools / allowed_tool_names / wire_tools`：支持 `allowed_tools` 的 Provider
  在普通阶段接收稳定完整 native schemas，只动态改变允许名称；final-only 边界会物理缩成
  `submit_final` 并重建 Session。Prompt Token 预算按真实 wire surface 计算。
- 首轮动态 Input 只保留 Runtime policy、非空的有界 Context 与 Task Request；普通 Tool 续接
  只追加 Call/Output，不重发另一份 Workspace/History。观察 `prompt_cache_key`、
  `cached_tokens` 与 uncached input，而不是只凭请求相似猜缓存命中。
- 理解 Provider Adapter 如何把结构化 Context Overflow 转成唯一的
  `ProviderContextOverflow`；AgentLoop 不读取厂商错误文案，只允许一次重建重试。
- 运行 `scripts/day3_prompt_provider_walkthrough.py` 的四段实验：最小三通道与稳定 Tool
  Surface、单 Pending 续接与多 Call 拒绝、Incomplete 伪 Final 拒绝、Typed Context Overflow
  的一次重建重试。
- 只确认非空 RepoMap 会进入首轮 Context，不在今天学习图算法。

完成标准：能画出一次 Function Call 及其 Output 的 Provider 会话。

### Day 4：ToolRuntime 与一次安全 Edit

- 阅读步骤 7～10：ToolRuntime、私有 tool-execution helpers、ToolContext、文件 Runner 和 Mutation Service。
- 运行 `scripts/day4_tool_boundary_walkthrough.py`，跟踪 `alpha -> agent`，同时保留外部追加的
  `external` 内容。
- 对照输出解释 stale Revision、v15 ToolOutcome、Preimage、PathTransition、Unified Diff，以及
  Approval Deny 为什么只有 Call/Result 而没有 Started。

完成标准：能说明模型为什么不能直接写文件。

### Day 5：Context、RepoMap 与 Compaction（分三段）

运行 `scripts/day5_context_walkthrough.py`，按三个独立实验学习：

1. **默认上下文增强**：查看 RepoMap 如何在预算内提供任务相关仓库导航。
2. **Context Pressure / Fallback**：无 Semantic Summarizer 时不写 Compaction Fact，只保留一对
   完整 Call/Result；`tool_started` 仍只存在于 durable log。
3. **Context Pressure / Semantic Success**：注入确定性 Summarizer，比较物理原 Events、
   Compaction Fact 与模型可见的 RunLog History View；Summary 始终只有 `Progress` 与
   `Critical Context`，它不是第二个 `RunProjection`。

第三段最后会额外打印七类 **Effective Recovery Context**：Goal、Constraints & Preferences、
Progress、Key Decisions、Next Steps、Critical Context、Execution Evidence，并逐项标明来自
TaskContract、WorkingState、两段 Semantic Summary 或 RunEvidence。七类是教学/观测组合视图；
不是七段 LLM 摘要，不持久化为第二状态，也不参与 CompletionController 判断。

完成标准：能区分“默认上下文输入”和“只有压力下才发生的压缩路径”。

### Day 6：Completion、Recovery 与 Child 附录（仅追问）

- 先运行 `completion_experiment`：Evidence 只展示净变化和当前 Verification，是否允许完成只
  由 CompletionController 决定。
- 再运行真实 `recovery_experiment`：构造期安装 dormant ActiveRunState，下一次 `ask()` 才
  reconcile、修复 Partial、验证并返回 RunOutcome；Run 的创建与恢复都走生产 RunLifecycle，
  只有硬崩溃点的 Call/Started 与已观察文件副作用是合成夹具，原 Tool Call 不盲目重放。
- `active_reset_experiment` 展示 active Runner 先落 `tool_result`，随后才写 `run_stopped` 并
  清理状态。
- 最后的 `child_delegation_experiment` 属于 **Orchestration Appendix**：先运行只读 Explore，
  再运行强制 Worktree 与精确写范围的 Implement，最后由 Parent 使用 `integrate_child` 完成
  base 复验、临时 Worktree Verification 和显式写回。Core 第一遍可以跳过。

完成标准：能解释为什么模型说“完成”不等于 Runtime 接受完成。

### Day 7：Capstone 与面试表达

- 运行 `scripts/day7_runtime_capstone.py`，把前六天串成一条完整请求。
- 直接核对 `RunOutcome.to_dict()`、Final Diff Artifact、Metrics 与 `RunStore.replay()` 的终态
  一致性。
- 按 [`review-pack/interview-demo.md`](review-pack/interview-demo.md) 练习 30 秒、3 分钟和
  10 分钟三种表达。
- 最后再进入 **Applications**：`applications/triage/workflow.py`、`report.py` 与相关 Evals。

完成标准：不用枚举所有类，也能先讲清 Core；面试官追问时再进入 Enhancement、Pressure、
Appendix 或 Application。

## 第一遍可以跳过什么

- `repo_map.py` 的 Tree-sitter 图构建与 PageRank 细节；
- `compaction_summary.py` 的 Summary Schema；
- `pico/subagents/` 的 Child receipt、Worktree 和显式集成；
- `applications/`、`evals/` 与真实 OSS Fixture。

跳过这些实现不等于关闭功能。它们仍按当前 Runtime 默认与条件路径正常工作。
