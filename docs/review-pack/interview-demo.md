# Pico 面试讲解与演示

## 30 秒介绍

Pico 不是聊天 UI，而是 Coding Model 外围的本地 Runtime。模型每轮只能提出一个
`ModelAction`；Runtime 负责 Context、工具准入、副作用、持久化、崩溃恢复、验证和
最终完成权。每个 Run 的过程事实只写入一条 append-only RunLog，实时与恢复共用一个
RunProjection，重建 TaskContract、增量 WorkingState、Evidence、Metrics 和单 Pending Call。

```text
User request
  -> ModelAction(tool / invalid / final)
  -> Tool transaction or Completion Gate
  -> RunLog
  -> RunProjection(Task / Evidence / Metrics / Pending)
```

## 3 分钟主链路

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

打开 `pico/tool_executor.py` 和 `pico/run_log.py`：

```text
assistant_tool_call
-> fsynced tool_started + before-state paths
-> Tool Runner
-> tool_result + side-effect state
```

`write_file` 只创建新文件；已有文件必须用带 `read_file` Revision 的 `edit_file`。内容先在同目录暂存并 fsync，atomic replace 提交点再次复验 Revision。外部编辑会形成显式冲突，不会被覆盖。

### 1:45～2:30：恢复与状态投影

Session 只保存 `active_run_id`。重启时 Runtime 重放 RunLog；未完成工具不会盲目重试，
而是比较声明路径的 before/current state，追加 `not_started`、`error`、`partial` 或
`unknown` ToolOutcome。Goal 属于首个 User Event 的 TaskContract；WorkingState 只保存
Constraints、Decisions 和 Next Steps，并继续使用 add/remove 增量 Tool 事务。

### 2:30～3:00：完成权

打开 `pico/completion_controller.py`：模型提交 `final` 后，Runtime 按 TaskContract 检查 Observation、最终净 RunChangeSet 和明确要求的 Verification，再检查 partial/unknown 副作用与未应用 Child Patch。每条路径只保存第一次 preimage，终态只写最终 `final_diff` receipt。

## 5 分钟现场 Demo

### 1. 运行确定性演示

```bash
uv run python scripts/demo_runtime.py
```

指出输出中的：

- `memory_recalls`：Project Memory 通过显式 Tool Call/Result Recall；
- `working_state`：当前 Run 的约束、决定和已清空 Next Steps；
- `tool_transactions`：每个调用都有 Call/Started/Result；
- `evidence_effects`：Patch 的精确路径和副作用；
- `completion`：Runtime 终态；
- `pending_call_id: null`：没有悬空工具事务。

### 2. 运行机制评测

```bash
uv run python scripts/run_evaluations.py
```

说明这些是确定性 Runtime 评测，不是模型能力分数：

- Native Harness 真实运行 Pico、工具和外部 verifier；
- Context v5 真实触发 ContextManager/RunLog Compaction；
- Project Memory v2 真实运行 `memory_store -> memory_recall -> final`；
- RepoMap v1 验证 tree-sitter 图、任务命中和 Token Budget。

### 3. 可选回归证明

```bash
uv run pytest -q
uv run ruff check pico tests scripts
```

## 10 分钟深入讲解

### 1. 单一事实源

`events.jsonl` 保存 User、Tool Call/Started/Result、Verification、Compaction 和终态。
Session 指针、Workspace 内容、Project Memory Card 和 Artifact 各有独立且明确的所有权；
没有第二份可变 `task_state.json` 或 `context.jsonl`。

### 2. Context 治理

固定 Runtime policy 进入 `instructions`；动态 Workspace、TaskContract、Memory Catalog、RepoMap、WorkingState、History 和 Current Request 进入 `input`。Prompt build 只读；Compaction 在 build 前准备，只总结历史 Progress/Critical Context，失败时使用近期完整事务的 bounded fallback。

### 3. WorkingState 与 Project Memory

TaskContract 保存 Goal；WorkingState 属于当前 Run，只保存 Constraints、Decisions 和 Next Steps；成功的增量 `update_working_state` Tool 事务是事实来源。Project Memory 属于跨 Session 项目知识；
Catalog 常驻 Prompt，主模型按描述显式调用 `memory_recall`，Card 作为不可信历史数据
返回，不能改变工具权限或代替 Workspace 当前事实。

### 4. RepoMap

RepoMap 使用 tree-sitter 提取 Python Symbol 和静态 Call/Import/Inheritance/Test Edges，
再用 lexical personalization + PageRank 在 Token Budget 内输出任务相关签名。当前
确定性评测验证任务命中、图关系和预算边界，不宣称通用模型收益。

### 5. 多 Agent 附录

Parent 可以委派 Explore/Implement DAG。Explore Child 只读；Implement Child 在独立 Git
Worktree 中执行并返回 Patch receipt。PatchIntegrator 在独立 Integration Worktree 按依赖
应用、验证，并在 Parent HEAD 未变化时才写回。调度同步且进程内，不宣称分布式 Worker
或跨进程 Child 调度恢复。

## 常见追问

### 为什么不用普通聊天历史？

聊天历史不能可靠表达副作用和事务。RunLog 区分 Tool Call、开始、结果、影响路径和终态，
可以在崩溃后确定性重放。

### 为什么不用 Checkpoint 快照？

快照会与 Tool 结果、Evidence 和运行统计形成多份可变状态。Pico 保存原始事件并按需投影；
Compaction 只改变模型 Context，不删除审计事实。

### 为什么模型没有最终完成权？

模型可能在没有真实修改、验证失败或副作用未知时声称完成。Completion Gate 使用
TaskContract 和 Runtime 观察到的当前 Workspace 证据决定是否接受 `final`。

### WorkingState 与 Project Memory 有什么区别？

WorkingState 是当前 Run 的计划白板；Project Memory 是跨 Session 的显式稳定知识。
前者不能授予权限，后者不能代替当前文件事实。

### 为什么 Memory 显式 Recall，而 RepoMap 自动注入？

Memory Catalog 已提供语义标题，模型能判断是否需要正文；显式 Recall 让调用和成本进入
正常 RunLog。RepoMap 是有界的小型仓库导航投影，因此由Runtime自动注入。

### 多 Agent 为什么需要 Worktree？

共享工作区会让 Child 互相覆盖并污染 Parent。Worktree 让每个实现拥有独立 Diff，组合
Patch 在写回 Parent 前可以单独验证。

### 这是生产系统吗？

不是。它是本地、单用户、单 Responses 协议的 Runtime。多租户、远程 Worker、后台
Mailbox、跨进程 Child 调度恢复和通用 MCP/Skills 都明确不在范围内。

## 证据边界

- 当前确定性证据：pytest、Native Harness、Context v5、Project Memory v2、RepoMap v1。
- 当前真实模型证据：三个 Triage 案例；必须同时说明
  它们绑定的 Runtime commit/fixture/model，不宣称是当前未提交工作树的实时成绩。
- Docker 隔离、可选网络和模型后端仍是环境信任边界。
