# Pico 架构主线

## 设计范围

Pico 运行在可信本地仓库中，只有一个进程写一个 Session。完成的交互保存在 `messages.jsonl`，当前执行状态保存在 `session.json`，由 SessionStore 统一提交和恢复。

Session 拥有：

- 原始用户消息与模型交互；
- 滚动摘要、摘要覆盖位置、模型观察位置及最新用户请求位置；
- 工具调用的 `pending/running/finished` 阶段和结果；
- 当前任务最大写范围及验证要求；
- 修改收据、未确认副作用和最近验证记录。

Trace 用于观察；Artifact 保存过大的工具结果、文件前像和最终 Diff。旧聊天不会被搬迁到 Artifact。

## 主调用链

```text
CLI.build_agent
└── Pico.create / Pico.resume
    └── Pico.ask
        └── AgentLoop.run
            ├── _prepare_model_input → ContextManager.build
            ├── _request_action → model.complete_action
            ├── _handle_tool_calls → ToolRuntime.execute_group
            ├── _handle_completion_request → CompletionController.check
            └── _finish_run → ArtifactStore / RunOutcome
```

`AgentLoop` 是唯一循环，但不实现文件修改、摘要或验证细节。

`WorkspaceObservation` 只保存仓库类型、HEAD、clean/dirty 状态、Git short status 文件列表和列表截断标记。单次修改 Diff 由编辑工具返回，最终任务 Diff 根据前像和修改收据生成并落盘；Workspace 不维护重复的增删行数或文件数量统计。

`delegate(task)` 仅提供同步只读分析，实现在 `pico/delegate.py`。子任务有独立 Session，共享父任务的截止时间，轮数由 Runtime 固定，不具备修改或嵌套委派权限。

## 修改事务

```text
模型提交 edit_file
→ ToolRuntime 校验参数、路径、写范围和审批
→ Runtime 绑定内部已观察版本（来自读取结果或成功修改收据）
→ Session 保存 running
→ WorkspaceMutationService 锁定并再次检查 Revision
→ ArtifactStore 保存原始前像
→ Session 保存 prepared receipt
→ 计算替换结果并在写前保存预期 after_revision
→ 临时文件完整写入并原子替换
→ 观察实际磁盘版本，识别写后漂移
→ Session 保存结果和 applied receipt
```

Revision 解决的具体问题是：模型读取文件后，用户或其他进程又修改了同一文件。Git 状态和最终测试不会自动把修改绑定到模型实际读取的字节，因此写入点仍需复验。

模型参数不含 Revision，也不使用 read_id。恢复或新请求清空内存中的可编辑观察，要求重新读取；不自动采纳当前磁盘版本放行旧修改。

## 中途验证与最终验收

Runtime 通过验证模块发现项目验收；`VerificationService` 负责固定命令的执行、审批、前后状态观察、失败反馈和完成前复查。无参数 `verify` 可在修改过程中调用，`CompletionController` 在最终提交时再次使用同一服务。新建 Ask 和只读 Delegate 不运行项目验收；已有写任务用 Ask 恢复时，不能绕过遗留验收义务。验证通过后继续修改会把结果标记为 stale。较大输出保留首尾，全文落盘供 `read_artifact` 获取。策略提示只列出验证是否可用、是否必需；实际执行记录可以包含已运行的命令。

`run_shell` 用于普通开发中模型选择的测试和复现，仍遵守原有 code 模式审批和受限写范围禁用规则。一次拒绝只拒绝当前工具或验证调用，不保存隐式拒绝规则；后续相同请求重新审批。`verify` 只执行用户或评测配置的命令，不接受模型传命令。没有配置独立验收且没有强制验收要求时，完成不宣称独立验收通过；此前已要求的强制验收不能通过移除配置绕过。没有自动测试调度或额外阶段状态机。

## 上下文

最新用户请求保留原文；近期消息保持原顺序。只有进入过一次成功模型请求的旧交互才可摘要；一个工具调用组及其结果作为整体保留或摘要，不拆开配对。旧的成功读取结果正文先确定性裁剪，命令、验证失败、修改结果和不确定副作用不做这一裁剪。摘要以新对话纠正旧说法，仍然可能有损。

Context 的顺序是：

```text
旧执行历史摘要（非权威）
→ 完整 Transcript 的回读路径
→ 项目级长期记忆
→ 最新请求原文（若已被摘要覆盖则补回）
→ 未覆盖的完整历史
→ Runtime 验证/副作用状态
```

每轮主模型请求使用最新 Session 组装输入，Provider 是无状态传输层，不维护第二套 `_action_input`。用户消息、助手消息、工具调用和工具结果分别使用 Responses 原生角色与类型；历史不被包装成单条用户 JSON。摘要请求使用独立客户端，共享用量统计；摘要按 `Current Goal / User Messages & Corrections / Progress / Files & Symbols / Errors & Verification / Current Work & Next Step / Artifacts & Uncertainty` 组织。候选必须缩小整包输入才允许提交。

压缩只更新 `summary` 与 `summary_end`，不删除历史。保存失败时恢复内存旧值。`observed` 标记进入过成功请求的历史末尾，并不代表模型理解了内容。

完成的交互按行写入 Transcript；未完成批次留在快照。先同步新增记录，再原子提交 `transcript_bytes/history_count` 及执行状态。恢复检查已提交记录完整性，截去超出提交位置的尾部，包括残缺尾行；未完成操作仍按前像和收据检查，绝不重放。每次保存不重写旧聊天，但恢复会加载完整历史到内存。这是单机面试版本的明确范围。

`read_file/search` 仅允许额外访问当前会话的准确 Transcript 路径；普通目录扫描不会开放 `.pico`，写入规则不变。历史读取只提供旧证据，不更新文件修改版本或消除未知副作用。

## 完成权

`submit_final` 只是模型申请完成。Runtime 依次检查：

模型可见的权限清单与实际发送的工具 Schema 使用同一份名称集合，包含 `submit_final`；避免把完成入口误报为不可用。

1. 中断操作是否已经观察；
2. 修改后的任务是否配置验证；
3. 固定验证命令是否通过且未改变工作区；
4. 保存验证结果后，相关文件是否仍保持同一 Revision。

失败会作为 Runtime feedback 进入下一轮，模型继续修复。模型文本本身不能把任务标记为完成。

文件状态保存在 Session 中；外部修改需通过实际读取重新观察。最终验证保存后还会复查仓库状态与文件内容。无法确认的外部命令影响保留在最终回答中，局部观察不代表外部系统已被审计。
