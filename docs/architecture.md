# Pico 架构主线

## 设计范围

Pico 运行在可信本地仓库中，只有一个进程写一个 Session。这个范围不需要 Event Sourcing；一个原子 Session 快照足以保存恢复所需事实。

Session 拥有：

- 原始用户消息与模型交互；
- 摘要正文及覆盖位置；
- 工具调用的 `pending/running/finished` 阶段和结果；
- 当前任务最大写范围及验证要求；
- 修改收据、未确认副作用和最近验证记录。

Trace 和 Artifact 都不是状态来源。Trace 用于观察，Artifact 保存过大的工具结果、文件前像和最终 Diff。

## 主调用链

```text
CLI.build_agent
└── Pico.create / Pico.resume
    └── Pico.ask
        └── AgentLoop.run
            ├── ContextManager.build
            ├── model.complete_action
            ├── ToolRuntime.execute_group
            └── CompletionController.check
```

`AgentLoop` 是唯一循环，但不实现文件修改、摘要或验证细节。

`delegate` 仅提供同步只读分析，实现在 `pico/delegate.py`。子任务有独立 Session，共享父任务的截止时间，不具备修改或嵌套委派权限。

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

`VerificationService` 统一执行固定验收命令、审批、前后状态观察、失败反馈数据和完成前复查。无参数 `verify` 工具可在修改过程中调用；`CompletionController` 在最终提交时再次调用同一服务。验证通过后继续修改会把结果标记为 stale。较大输出保留首尾，全文落盘供 `read_artifact` 获取。

`run_shell` 用于普通开发中模型选择的测试和复现，仍遵守原有 code 模式审批和受限写范围禁用规则。`verify` 只执行用户或评测配置的命令，不接受模型传命令。没有配置独立验收且没有强制验收要求时，完成不宣称独立验收通过；此前已要求的强制验收不能通过移除配置绕过。没有增加自动测试调度或额外阶段状态机。

## 上下文

当前请求始终保留原文。只有已经被一次成功模型请求观察、并且不属于近期完整交互的历史可以进入摘要。一个工具调用组及其结果作为整体保留或摘要，不拆开配对。

Context 的顺序是：

```text
旧历史摘要
→ 项目级长期记忆
→ 当前请求原文
→ 未覆盖的完整历史
→ Runtime 验证/副作用状态
```

每轮主模型请求使用最新 Session 组装输入，Provider 不延续旧输入缓存。用户消息、助手消息、工具调用和工具结果分别使用 Responses 原生角色与类型；历史不被包装成单条用户 JSON。摘要请求使用独立客户端，共享用量统计；摘要候选必须缩小输入并通过整包预算检查后才提交 `covered`。失败时保留原状态；如果原请求仍能放入窗口则继续。

## 完成权

`submit_final` 只是模型申请完成。Runtime 依次检查：

模型可见的权限清单与实际发送的工具 Schema 使用同一份名称集合，包含 `submit_final`；避免把完成入口误报为不可用。

1. 中断操作是否已经观察；
2. 修改后的任务是否配置验证；
3. 固定验证命令是否通过且未改变工作区；
4. 保存验证结果后，相关文件是否仍保持同一 Revision。

失败会作为 Runtime feedback 进入下一轮，模型继续修复。模型文本本身不能把任务标记为完成。

文件状态保存在 Session 中；外部修改需通过实际读取重新观察。最终验证保存后还会复查仓库状态与文件内容。无法确认的外部命令影响保留在最终回答中，局部观察不代表外部系统已被审计。
