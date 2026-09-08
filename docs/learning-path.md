# Pico 七天学习路径

这是 Pico 的唯一权威学习导航。架构事实以
[`agent-runtime.md`](architecture/agent-runtime.md) 和
[`state-ownership.md`](architecture/state-ownership.md) 为准；本文只决定第一次应该按什么
顺序阅读，不改变任何 Runtime 默认行为。

RepoMap 从 `Pico` 初始化开始默认启用；第一遍先把它当成“非空才进入 Prompt”的有界
仓库导航，到 Day 5 再阅读内部实现。Semantic Compaction 和 Subagents 也都不是
理解一次普通单 Agent 请求的前置条件。

## 15 分钟核心路径

先运行 `scripts/day7_runtime_capstone.py`，然后只读一个 owner、一个 caller 和一个结果：

1. `PicoConfig.mode` → `RunLifecycle._task_contract()`：显式 Ask/Code/Auto 决定最大能力，不调用隐藏分类模型。
2. `AgentLoop.run()`：只看 Tool、Invalid、Final 三个分支以及 Agent Turn/Deadline 停止条件。
3. `ToolRuntime.resolve_surface()` → `AgentLoop._handle_tool_turn()` →
   `ToolRuntime.execute_pending_group(surface)`：同一个 Surface 负责 Provider Schema、解析与执行；
   Tool Call group 由 Loop 接受并持久化，工具默认独占，明确标记的读取工具形成有界并行段。
4. `RunLog.append()`、`RunProjection.check_event()` 与 `replay_events()`，以及 `ToolRuntime.reconcile_interrupted()`：理解
   Pending Tool transactions、Tool Call group 与 Crash recovery；历史筛选和压缩计划在 Day 5 阅读 `RunHistory`。
5. `WorkspaceMutationService.write/edit()` 与 ToolRuntime post-observation：理解原子 create-only、
   Revision 提交点复验，以及 Runtime 如何从真实 before/after 生成 transition。
6. `ResolvedVerificationPolicy`、`CompletionController.assess()`、
   `RunLifecycle.run_completion_verification()` 与 `CompletionController.assess_verification()`：
   理解持久最低要求与当前 verifier 如何合并成同一轮不可替换的完成策略，以及 Repository
   observation 为什么与验证命令共享 ExecutionContext。

到这里已经能完成面试主线。只有被追问 Child delegation 时才运行 Day 6；RepoMap、Provider
Adapter 细节和 Semantic Compaction 都留到后续章节。

## 六个核心 Ownership 文件

先记住下面六个文件分别拥有什么。其他文件会出现在真实调用链中，但只是协议、数据或
实现接缝，不另起一套核心 Ownership。

| 文件 | 核心 Ownership |
|---|---|
| [`pico/runtime.py`](../pico/runtime.py) | `Pico` 组合根、八个顶层组件以及 `ask()` 入口 |
| [`pico/agent_loop.py`](../pico/agent_loop.py) | 模型轮、工具轮、Provider 续接和停止条件的控制流 |
| [`pico/tool_runtime.py`](../pico/tool_runtime.py) | 模型可见工具的唯一公开执行边界，以及 Call/Started/Result 因果顺序 |
| [`pico/run_log.py`](../pico/run_log.py) | Run Fact 的严格协议与追加；历史视图在 `history.py`，恢复协调在 Lifecycle |
| [`pico/run_projection.py`](../pico/run_projection.py) | 从 Fact 重建 Task、Evidence、Metrics、Pending Call IDs 和终态 receipt |
| [`pico/completion_controller.py`](../pico/completion_controller.py) | 是否允许模型最终提交的 Runtime 决策 |

阅读 Trace 时还会经过 `cli.py`、`run_lifecycle.py`、Prompt/Provider、`ToolContext`、具体
Runner、Evidence 和 Verification。它们是六个 Ownership 之间的真实接缝，不冒充第七个
核心状态源。

## 从一个真实 CLI 请求开始

每次 `ask/resume` 的模型计数起点为 `AgentLoopState.starting_model_request_count`。
执行限制使用模型轮数、请求超时、取消和并行度，不再单独限制工具执行总次数。
工具累计次数仍从日志统计，用于观测；没有请求级工具计数起点或额度耗尽提示。

程序入口明确选择新建或恢复：普通启动调用 `Pico.create(..., session_store=store)`，
由它创建 Session 并装配 Runtime，不调用恢复查找，也不扫描空 Run 目录。
只有显式传入 `--resume <session_id>` 或 `--resume latest` 时，才加载 Session 并调用
`Pico.resume(..., session=store.load(session_id))`。旧 `Pico(...)` 构造方式不再支持。
运行中异常恢复仍保留首条事件已落盘但指针未发布时的孤立 Run 查找。会话通过
`session.id`、`session.active_run_id` 访问。`PicoConfig(...)` 构造时即完成校验与规范化，
修改使用 `dataclasses.replace(config, ...)`，不存在第二次 `build/normalized` 加工。

下面的参数都由当前 CLI 提供。CLI 会识别仓库中的 Python 测试并自动配置验证：

```bash
uv run pico \
  --mode code \
  --cwd /path/to/repo \
  "Fix calculator.add so the existing addition test passes"
```

真实 Provider 需要模型配置。Code 模式可以申请用户审批的诊断型 `run_command`；Ask/Auto
不暴露通用 Shell。自动发现或由 `--verify-command` 覆盖的 Verification
由 RunLifecycle 执行并持久化，CompletionController 只判断结果。它们都以当前用户权限在本机运行，因此只应对可信仓库使用。
未知代码应先放进外部 CI、VM 或容器。
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
| 3 | `pico/run_lifecycle.py: initialize -> _resume_or_create_run` | 新 Run 由显式 Mode 确定性生成 TaskContract；恢复 Run 不能扩大原能力，并在 Provider 前持久化 `user_guidance` |
| 4 | `pico/agent_loop.py: run -> _next_model_turn` | 每轮只处理 Tool、Invalid 或 Final 三种 ModelAction；一个 Tool Action 可带一个或多个有序 Call |
| 5 | `PromptBuilder -> OpenAICompatibleModelClient` | 固定规则进 `instructions`；root→CWD 的 `AGENTS.md` 作为独立 repository instructions；`WorkspaceObservation` 与 RepoMap 进入有界 Context；当前 Tool Surface 决定本轮 Schema，Prompt 展示当前有效验证要求 |
| 6 | `providers/clients.py: _parse_provider_turn -> complete_action` | 完整 Provider output 一次解析为 Action 与规范化 replay items；合法 Assistant preamble 与 Call 一起规范化，任一非法 sibling 或混合 `submit_final` 使整轮拒绝，合法多个 Call 保持原顺序进入分组调度 |
| 7 | `AgentLoop._handle_tool_turn` | 执行前把本次响应的一个或多个 Call 原子持久化为一个 `assistant_tool_calls` Fact |
| 8 | `ToolRuntime.execute_pending_group(surface)` | 每个 Call 只能从本轮 Surface 解析；连续 parallel-safe 调用有界并行，exclusive 调用形成屏障，Result 按原顺序落盘；无 Run 的人工观察走独立 manual Surface |
| 9 | `ToolExecutionPlan -> tool_started -> ToolContext -> concrete runner` | Runner 只消费已经持久化的执行计划和显式能力；ChildLaunch 在资源创建前归属 Parent，文件修改进入 mutations，Registry 不捕获 Parent Runtime |
| 10 | `RunLog.append -> apply_event 验证待提交状态 -> 存储追加 -> 发布状态` | 同一个新 Fact 如何在写盘前验证全部投影、持久化后发布 Pending、Metrics、WorkingState 和 Evidence |
| 11 | `CompletionController -> RunLifecycle Verification -> CompletionController -> RunLifecycle.finish_success` | TaskContract、净变化和已持久化验证事实如何决定完成，写入 `final_diff` 与 `assistant_final`，再从终态 Projection 返回非持久化 `RunOutcome` |

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
  -> ToolRuntime.reconcile_interrupted
  -> append user_guidance
  -> run_resumed
```

`load_run` 从同一次持久化读取返回拥有已恢复 Projection 的 RunLog；事件从 `log.events` 读取，
Projection 从 `log.projection` 取得。
`RunStore.replay` 是只返回 Projection 的委托，Runtime 直接安装加载得到的两个对象，不再重建和重复校验。
实时调用 `RunLog.append`，先通过 `RunProjection.apply_event` 构造并验证待提交状态，
存储成功后发布该状态；回放使用相同的转换。非法事实不会先落盘再报错。

## 五层知识结构

| 层级 | 内容 | 第一次是否必学 |
|---|---|---|
| Core | 上述 CLI Trace、TaskContract、增量 WorkingState、Pending Call group、按工具能力调度、工具安全、恢复、Completion 与 Final Diff | 是 |
| 默认上下文增强 | 非空 RepoMap 自动提供有界仓库导航 | 功能始终启用；空投影不发送；内部实现第二遍再学 |
| Context Pressure | Token Budget、Provider Session Rotation、Semantic Compaction、失败后的事务级 Fallback | 仅超长任务需要 |
| Orchestration Appendix | 单个 Explore/Implement Child、Git Worktree、显式 `integrate_child` | 单 Agent Core 完成后选学 |
| Application | Coding Workflow 在成功终态后的可选 Git Commit | 最后学习 |

Semantic Compaction 不是每轮执行：必须已有 Run Log、当前没有 Pending Call，并且新 Prompt
的本地实际组装量，或 Provider 已报告的 input 加已提交 replay output、实际 Tool Result Token 达到
`provider_context_limit_tokens - compaction_reserve_tokens`，才进入准备分支。这些都是本轮已经
产生的测量值，不使用 `max_new_tokens` 猜测尚未发生的输出；缺少 Provider usage 时依赖本地新
Prompt 计数和真实 typed overflow。失败时使用完整 Tool 事务组成的 bounded fallback；取消与
deadline 保持执行控制异常。已提交 Summary 仍是可选历史投影，切换到更小预算时可以省略，
不会阻塞 Run 恢复。

Subagents 在 CLI 中默认提供，因为 `build_agent()` 会安装 Child Model Client Factory；直接使用
`Pico.create/resume(...)` API 时只有显式传入该 Factory 才启用。无论是否启用，普通单 Agent Core 都不依赖
Subagent 实现。

## 保留原始七天顺序

### Day 1：从 CLI 到 AgentLoop

- 从上面的真实 CLI 命令进入 `cli.py`，沿步骤 1～4 阅读。
- 运行 `scripts/day1_runtime_walkthrough.py`：它使用真实
  `build_arg_parser -> build_agent -> ask` 路径，只用 `FakeModelClient` 替换网络 Provider；
  依次展示 CLI 任务要求、八个顶层组件、全新 Session 的恢复探测和完整 `RunOutcome`。
- 查看启动策略生成的 `TaskContract(goal, write_scope, verify_changes)`；写范围是
  `none/workspace/paths`。事件只有 Run/Session 身份，不再有独立 Task ID。
- 确认 `RunOutcome` 是终态 Projection 的非持久化返回快照；Run Log 中没有第二种
  `run_outcome` Fact。
- RepoMap 此时只需要知道“默认存在”，不要打开其实现。

完成标准：能用 30 秒讲清用户请求如何进入 `AgentLoop`。

### Day 2：State、Fact 与 Projection

- 阅读 TaskContract、`pico/working_state.py` 的六字段 add/remove WorkingState、RunLog 和
  RunProjection；交互 CLI 使用 `/state` 查看这一当前 Run 投影。
- 运行 `scripts/day2_state_walkthrough.py` 的三段实验：
  1. 查看原始 Fact，并比较 Live、`load_run` 与 `RunStore.replay` 的完整 Projection；
  2. Replay 单 Call 前缀，并观察 ordered Tool Call group 的 Pending Call IDs；
  3. 用新 `Pico` 加载无副作用的中断调用，在下一次 `ask()` 自动对账且不盲目重放 Runner。
- `PendingToolGroup` 自己管理调用、启动与结果游标；RunProjection 委托工具事件校验，
  批次完成后 group id 和剩余调用一起清空。
- 未处理异常后，Runtime 通过 `reload_current_run` 重新加载持久状态，处理“已经落盘但返回失败”的情况。
  Day 6 再通过 walkthrough 学习 Crash Resume 和 Active Reset；精简面试测试套件不保留完整恢复矩阵。

完成标准：能解释 Fact 与 Projection 的区别，以及为什么不保存第二份 Task 快照。

### Day 3：Prompt 与 Provider

- 阅读步骤 5～6：`instructions`、`input`、`tools` 三个通道和 Function Call Output 回写。
- 每轮解析当前 Mode、TaskContract 和工具权限允许的 native schemas；final submission 再冻结 Verification policy；
  final-only 边界缩成 `submit_final` 并重建 Session。Prompt Token 预算按这个真实表面计算。
- 首轮动态 Input 按 Runtime policy、Task Request、非空的有界 Context 排列；普通 Tool 续接
  只追加 Call/Output，不重发另一份 Workspace/History。
- 理解 Provider Adapter 如何把结构化 Context Overflow 转成唯一的
  `ProviderContextOverflow`；AgentLoop 不读取厂商错误文案，只允许一次重建重试。
- 运行 `scripts/day3_prompt_provider_walkthrough.py` 的四段实验：最小三通道与稳定 Tool
  Surface、单 Call 续接与 Tool Call group 聚合 Result、Incomplete 伪 Final 拒绝、Typed Context Overflow
  的一次重建重试。
- 只确认非空 RepoMap 会进入首轮 Context，不在今天学习图算法。
- 观察 Provider 计量实验：输入 usage 与已接受输出归 Provider 会话所有，调用方只提交
  待回写 results。该小实验使用 `len` 展示增量公式，不把字符数当作真实 token 统计。

完成标准：能画出一次 Function Call 及其 Output 的 Provider 会话。

### Day 4：ToolRuntime 与一次安全 Edit

- 阅读步骤 7～10：ToolRuntime、私有 tool-execution helpers、ToolContext、文件 Runner 和 Mutation Service。
- 运行 `scripts/day4_tool_boundary_walkthrough.py`，跟踪 `alpha -> agent`，同时保留外部追加的
  `external` 内容。
- 对照输出解释 stale Revision、ToolOutcome、Preimage、PathTransition、Unified Diff，以及
  为什么 Observation 可并行而 Edit/Approval 仍必须独占一轮。
- ToolContext 只保存 Run ID、Call ID、ExecutionContext、当前 WorkingState 和执行计划。
  路径解析、文件修改、Artifact、命令及子任务服务在工具注册时通过 `partial` 绑定给需要它们的
  校验、计划与执行回调；不把整个 Runtime 传给 Runner。WorkingState 每次调用重新读取，
  不捕获注册时的旧投影；执行期间的 ExecutionContext 随本次运行结束释放。

完成标准：能说明模型为什么不能直接写文件。

### Day 5：Context、RepoMap 与 Compaction

运行 `scripts/day5_context_walkthrough.py`，按四个实验学习：

1. **默认上下文增强**：查看 RepoMap 如何在预算内提供任务相关仓库导航。
2. **Context Pressure / Fallback**：无 Semantic Summarizer 时不写 Compaction Fact，只保留一对
   完整 Call/Result；`tool_started` 仍只存在于 durable log。
3. **Context Pressure / Semantic Success**：只预设摘要模型输出，实际执行语义投影、请求构建、
   Schema 解析和提交，比较物理原 Events、
   Compaction Fact 与模型可见的 RunLog History View；Summary 始终只有 `Progress` 与
   `Critical Context`，它不是第二个 `RunProjection`。摘要输入使用独立语义投影：RunLog 继续
   保存 Call ID、revision 和分页统计，Summarizer 接收实际结果内容与失败／副作用的语义投影。
   摘要请求独立计算输入、指令、Schema 和输出预算；长字段超预算时标记裁剪并保留 Artifact
   引用，原始日志不变。摘要输出上限由 `summary_max_output_tokens` 和主请求在保留近期历史后
   的剩余空间共同决定。提交前实际检查摘要加保留历史的总预算，不能只检查摘要自身。
4. **小预算历史投影**：旧摘要无法放入时省略它并显示 omitted 标记，日志和 WorkingState
   不变。History 由 `RunLog.history()` 构造，当前纠错提示 ID 直接来自 RunProjection。

第三段最后会额外打印七类 **Effective Recovery Context**：Goal、Constraints & Preferences、
Progress、Key Decisions、Next Steps、Critical Context、Execution Evidence，并逐项标明来自
TaskContract、WorkingState、两段 Semantic Summary 或 RunEvidence。七类是教学/观测组合视图；
不是七段 LLM 摘要，不持久化为第二状态，也不参与 CompletionController 判断。

完成标准：能区分“默认上下文输入”和“只有压力下才发生的压缩路径”。

### Day 6：Completion、Recovery 与 Child 附录（仅追问）

- 先运行 `completion_experiment`：Evidence 只展示净变化和当前 Verification，是否允许完成只
  由 CompletionController 决定。
- 再运行真实 `recovery_experiment`：构造期安装 dormant ActiveRunState，下一次 `ask()` 才
  reconcile、读取当前文件、运行真实验证并返回 RunOutcome，已经正确的内容无需再次修改。
  Run 的创建与恢复都走生产 RunLifecycle；只有硬崩溃点的 Call/Started 与已观察文件副作用
  是合成夹具，原 Tool Call 不盲目重放，历史 Partial 在完成后仍保留。
- 核对恢复前后的持久 `write_scope` 相同；夹具使用当前事件格式，不加载旧 Task ID 日志。
- `active_reset_experiment` 展示 active Runner 先落 `tool_result`，随后才写 `run_stopped` 并
  清理状态。
- 最后的 `child_delegation_experiment` 属于 **Orchestration Appendix**：先运行只读 Explore，
  再运行强制 Worktree 与精确写范围的 Implement，最后由 Parent 使用 `integrate_child` 完成
  base 复验、临时 Worktree Verification 和显式写回。Core 第一遍可以跳过。

完成标准：能解释为什么模型说“完成”不等于 Runtime 接受完成。

### Day 7：Capstone 与面试表达

- 运行 `scripts/day7_runtime_capstone.py`，把前六天串成一条完整请求。只有模型动作是预设的，
  文件工具和 pytest 实际执行；输出显示修改前测试失败和 Runtime 修改后验证通过。
- 脚本默认把实时 Trace 打到 stderr；观察请求、工具启动、结果提交、验证和终态。
  预设模型不报告 usage，因此 token 显示 None；工具结果按日志提交顺序显示。
- 观察代码与测试文件在一个 Tool Call group 的 parallel 段中并行读取，而 Edit、WorkingState 与
  `submit_final` 使用 exclusive 边界。
- 直接核对 `RunOutcome.to_dict()` 中的 changed paths、Final Diff、Metrics 与 `RunStore.replay()` 的终态
  一致性。
- 按 [`review-pack/interview-demo.md`](review-pack/interview-demo.md) 练习 30 秒、3 分钟和
  10 分钟三种表达。
- 最后再进入 **Application**：看 `applications/coding.py` 如何在终态后只提交本 Run 的干净路径；它不参与 Core 的恢复与完成判断。

完成标准：不用枚举所有类，也能先讲清 Core；面试官追问时再进入 Enhancement、Pressure、
Appendix 或 Application。

## 第一遍可以跳过什么

- `repo_map.py` 的 Tree-sitter 图构建与 PageRank 细节；
- `compaction_summary.py` 的 Summary Schema；
- `pico/subagents/` 的 Child receipt、Worktree 和显式集成；
- `applications/coding.py` 的终态 Git Commit 和真实 Compaction 评测脚本。

跳过这些实现不等于关闭功能。它们仍按当前 Runtime 默认与条件路径正常工作。
