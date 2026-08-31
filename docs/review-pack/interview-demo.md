# Pico 面试讲解与演示

学习顺序以 [`../learning-path.md`](../learning-path.md) 为准。下面的标签区分 Core、默认
上下文增强、Context Pressure、Orchestration Appendix 和 Applications，避免在开场平铺全部能力。

## 30 秒介绍 `[Core]`

Pico 不是聊天 UI，而是 Coding Model 外围的本地 Runtime。模型每轮只能提出一个
`ModelAction`；Runtime 负责 Context、工具准入、副作用、持久化、崩溃恢复、验证和
最终完成权。每个 Run 的过程事实只写入一条 append-only RunLog，实时与恢复共用一个
RunProjection，重建 TaskContract、增量 WorkingState、Evidence、Metrics 和单 Pending Call。

```text
User request
  -> hidden TaskIntentClassifier -> TaskContract
  -> ModelAction(tool / invalid / final)
  -> Tool transaction or Completion Gate
  -> RunLog
  -> RunProjection(Task / Evidence / Metrics / Pending)
```

## 3 分钟主链路 `[Core]`

### 0:00～0:30：模型与 Runtime 的边界

打开 `pico/contracts.py`：

- `ModelAction.tool` 只是申请调用工具；
- `ModelAction.final` 只是申请完成；
- `ToolRunnerResult` 是 Runner 直接结果；
- `ToolOutcome` 是 Runtime 审计后的事实。

### 0:30～1:00：小而明确的 AgentLoop

打开 `pico/agent_loop.py` 的 `run()`：

```text
tool    -> 执行工具并继续 Provider
invalid -> 反馈协议错误并重试
final   -> 交给 CompletionController
```

### 1:00～1:45：工具事务

打开 `pico/tool_runtime.py` 和 `pico/run_log.py`：

```text
assistant_tool_call
-> ToolRuntime 按 call_id 取回持久化 ToolCall，再完成准入与私有 helpers
-> fsynced tool_started + before-state paths
-> ToolContext-bound Tool Runner
-> tool_result + side-effect state
```

强调 `ToolRuntime` 是模型可见工具的唯一公开执行边界；调用方不能用相同 call ID 替换
持久化 name/args。AgentLoop 接受并持久化 `assistant_tool_call`，ToolRuntime 负责参数校验、
Approval、preimage 以及执行事务的 `tool_started/tool_result`。纯值计算下沉到私有
`tool_execution.py`，具体 Runner 只获得 `ToolContext` 中的受限能力。

`write_file` 只创建新文件；已有文件必须用带 `read_file` Revision 的 `edit_file`。内容先在同目录暂存并 fsync，atomic replace 提交点再次复验 Revision。外部编辑会形成显式冲突，不会被覆盖。

### 1:45～2:30：恢复与状态投影

Session 只保存 `active_run_id`。重启时 Runtime 重放 RunLog；未完成工具不会盲目重试，
而是比较声明路径的 before/current state，追加 `not_started`、`error`、`partial` 或
`unknown` ToolOutcome。每次 Resume 输入先写 `user_guidance` Fact，因此二次崩溃后仍能
重建当时交给模型的约束。Goal 属于首个 User Event 的 TaskContract；WorkingState 只保存
Constraints、Decisions 和 Next Steps，并继续使用 add/remove 增量 Tool 事务。
持久 Run ID 由 `RunStore.load_run` 单次读取并返回 Events + Projection；
`RunStore.replay` 只是 Projection-only 委托。Live 路径只对新 Fact 调用
`RunProjection.apply_event`，不会再暴露额外的 `from_events` 回放入口。

### 2:30～3:00：完成权

打开 `pico/completion_controller.py`：模型提交 `final` 后，Runtime 依次检查尚未集成的
Implement Child、TaskContract、未修复 partial/unknown、副作用后的 Workspace drift、当前
Verification，以及验证后仍未解决的 Effect。这个顺序避免在副作用仍不确定时运行无意义的
verifier。每条路径只保存第一次 preimage，终态只写最终 `final_diff` receipt。

## 5 分钟现场 Demo `[Core]`

```bash
uv run python scripts/day7_runtime_capstone.py
```

指出输出中的：

- `RunOutcome`：公开终态结果包含状态、答案、停止原因、Final Diff 和 Metrics；
- `Final Diff`：receipt 指向经过校验的真实 Diff Artifact；
- `working_state`：当前 Run 的约束、决定和已清空 Next Steps；
- `tool_transactions`：每个调用都有 Call/Started/Result；
- `outcome_matches_replay`：返回值与 durable Replay 的终态一致。

现场不运行完整 Evals 或 pytest；把通过结果作为预先准备的证据。只有面试官追问 Crash
Recovery、Completion 顺序或 Child integration 时，再运行 Day 6 的对应实验。

## 10 分钟深入讲解

### 1. 单一事实源 `[Core]`

`events.jsonl` 保存 User、Tool Call/Started/Result、Verification、Compaction 和终态。
Session 指针、Workspace 内容和 Artifact 各有独立且明确的所有权；
没有第二份可变 `task_state.json` 或 `context.jsonl`。

### 2. Context 治理 `[Context Pressure]`

固定角色、执行、Tool 协议、WorkingState 和完成规则进入 `instructions`。首轮动态 `input`
只包含 Runtime task policy、非空的有界不可信 Context 和 Task Request；仅 Resume 且请求改变时
才追加 latest request。空 RepoMap/WorkingState/History 不渲染，普通 Function Call 续接
只追加 Call/Output。原生 Function Schema 只在 `tools`；支持 `allowed_tools` 的 Provider 在
普通阶段获得稳定完整 Schema 和动态允许名称，final-only 边界则物理缩成 `submit_final` 并
重建 Provider Session。Runtime 的 Token 预算按实际 wire tools 计算。稳定
instructions hash 用作受支持 Provider 的 `prompt_cache_key`，并记录 cached/uncached
Token 指标。

Prompt build 只读；Compaction 在 build 前准备，独立 LLM 的输出与持久 Summary 始终只有
`Progress`、`Critical Context` 两段，失败时使用近期完整事务的 bounded fallback。演示中可把
TaskContract Goal、WorkingState Constraints/Decisions/Next Steps、这两段 Summary 与
RunEvidence 组合成七类 Effective Recovery Context。七类只是逐项标来源的教学/观测视图，
不是七段 LLM 输出，不是第二状态，也不参与 Completion Gate。

Provider Context Overflow 由 Adapter 归一化为一个类型；AgentLoop 只允许一次重建重试，不通过厂商错误字符串猜测控制流。

### 3. WorkingState `[Core]`

TaskContract 保存 Goal；WorkingState 属于当前 Run，只保存 Constraints、Decisions 和 Next Steps；
成功的增量 `update_working_state` Tool 事务是事实来源。它不能改变工具权限，也不能代替
Workspace 当前事实。

### 4. RepoMap `[默认上下文增强]`

RepoMap 使用 tree-sitter 提取 Python Symbol 和静态 Call/Import/Inheritance/Test Edges，
再用 lexical personalization + PageRank 在 Token Budget 内输出任务相关签名。当前
确定性评测验证任务命中、图关系和预算边界，不宣称通用模型收益。

### 5. 多 Agent 附录 `[Orchestration Appendix]`

Parent 每次用 `delegate` 创建一个 Explore 或 Implement Child。Explore 只读且不建
Worktree；Implement 必须声明允许写路径并始终在独立 Git Worktree 中执行。Child 不能再次
委派。Implement 完成后不会自动 merge，Parent 必须用 `integrate_child` 按 child ID 显式
集成；Runtime 复验 delegation base，在临时 Worktree 应用 Patch、运行固定 Verification，
成功后才写回 Parent。这里没有 DAG、批量委派、后台队列或并行 worker。

## 应用层追问 `[Applications]`

只有在 Core 和默认上下文增强讲清后，再用 `applications/triage` 说明 Pico 如何被薄应用层
复用；Triage 不拥有第二套 Agent Loop、ToolRuntime、RunLog 或 Completion。

## 常见追问

### 为什么不用普通聊天历史？

聊天历史不能可靠表达副作用和事务。RunLog 区分 Tool Call、开始、结果、影响路径和终态，
可以在崩溃后确定性重放。

### 为什么不用 Checkpoint 快照？

快照会与 Tool 结果、Evidence 和运行统计形成多份可变状态。Pico 保存原始事件并按需投影；
Compaction 只改变模型 Context，不删除审计事实。

### 七类恢复信息是否等于七段 Compaction Summary？

不等于。Semantic Summarizer 只生成 `Progress` 与 `Critical Context`。Goal 来自 TaskContract，
Constraints/Decisions/Next Steps 来自 WorkingState，Execution Evidence 来自 RunEvidence；演示只是
把这些既有来源并排展示。它不会被持久化或再次发送成七段 Prompt，也不替代
CompletionController。

### 为什么模型没有最终完成权？

模型可能在没有真实修改、验证失败或副作用未知时声称完成。Completion Gate 使用
TaskContract 和 Runtime 观察到的当前 Workspace 证据决定是否接受 `final`。

### Verification 是否被 Workspace 路径约束保护？

不是。模型没有通用 Shell 工具，但用户显式配置的固定 Verification 会在 Workspace 中以
当前用户权限本机运行。文件工具的路径检查不能限制该进程访问其他目录、网络或子进程。
因此 Pico 只运行可信仓库；未知仓库或 PR 应交给外部 CI、VM 或容器。

### WorkingState 能授予权限吗？

不能。WorkingState 是当前 Run 的计划白板；TaskContract 和 Runtime policy 才拥有任务类型、
写入范围与验证要求，Workspace 当前事实仍必须通过工具观察。

### 多 Agent 为什么需要 Worktree？

Implement Child 不能在委派阶段直接污染 Parent。独立 Worktree 让 Runtime 先生成不可变
Patch receipt；`integrate_child` 再在临时 Worktree 复验 base、应用 Patch 和运行 Verification，
成功后才显式写回 Parent。

### 这是生产系统吗？

不是。它是本地、单用户、单 Responses 协议的 Runtime。已完成 Implement receipt 可以恢复并
继续显式集成，但运行中的 Child 不会跨进程恢复。多租户、远程 Worker、后台 Mailbox 和通用
MCP/Skills 都明确不在范围内。

## 证据边界

- 当前确定性证据：pytest、Native Harness、Context v5、RepoMap v1。
- 当前保留的真实模型证据是受控的 Compaction Artifact；旧 Docker 边界产生的三个 Triage
  Artifact 已退役并删除，不能作为当前 Runtime 成绩。
- 固定 Verification 使用当前用户的本机权限；可信仓库是明确前提，未知代码的隔离由外部 CI、VM 或容器提供。
- 模型后端仍是外部信任边界。
