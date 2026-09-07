# Per-tool 并发调度

Pico 接受 Responses 在一个模型响应中返回的多个 Function Call。Runtime 不再维护“纯读取
Batch”特例，也不要求模型负责构造可执行批次。每个工具在唯一 Registry 中声明执行能力：

```text
默认                         exclusive
list_files/read_file/search  parallel
read_artifact                parallel
```

ToolRuntime 按模型顺序把调用切成连续 parallel 段和 exclusive 屏障。parallel 段最多同时运行
`PicoConfig.max_parallel_tools` 个 Runner，默认 4；更多调用自动分波。exclusive 调用独占执行。
模型请求前解析的同一个 `ResolvedToolSurface` 同时约束 Provider、Parser 与调度器；每个调用
再独立完成 Schema、Approval、Effect 与工具预算准入，因此一个调用被拒绝
不会取消合法兄弟调用。

这指的是已被 Provider 解析接受的调用序列。缺少调用身份、非法 JSON 或未声明工具的响应，
会在 Provider 层整体判为 invalid，进入纠错流程。

```text
read A ─┐
read B ─┴─ parallel segment
             ↓ barrier
edit C       exclusive
             ↓ barrier
read D ─┐
read E ─┴─ parallel segment
```

每个响应统一使用 `assistant_tool_calls` 保存一个或多个原始有序调用。每个 Call 都有自己的
`tool_started/tool_result` 事务。主线程在 Runner
开始前写 Started，在并行执行后按模型原序写 Result。RunProjection 拒绝跨越尚未结束执行段的
Started；恢复保留已有 Result，逐个核对 Started 调用的副作用，并把尚未 Started 的调用闭合为
not-started，绝不重放 Runner。

旧 `assistant_tool_call`、`assistant_tool_batch`、`ModelAction.tool_batch`、`pending_batch_id`、
`batchable_observation` 和固定批次长度已删除。旧事件直接作为不支持的 Run Log kind 拒绝，
没有兼容或迁移分支。

工具完成后，Provider client 根据真实的 `function_call_output`、Call ID、instructions 与 tools
计算下一次 continuation；缺少 Provider usage 时计算完整本地请求。达到 Context high watermark 时，在下一次模型
请求前重建 Provider session 并从 RunLog 压缩；不等待下一轮已经越界后再处理，也不对尚未发生
的工具输出做最坏情况猜测。

## 验证

- 确定性回归结果见 [最终审查与修复验证](final-review-2026-09-07.md)。
- 七个 Day 1–7 演示通过。
- 历史真实 CLI 修复：模型在首轮返回 5 个 `read_file`，随后独占 `edit_file`；可见和隐藏验证通过。
- 历史真实 Child：Parent 的 5 个读取按并发上限执行，Child Worktree 修改、验证和 Parent 集成通过。
- 历史真实 Compaction：12 份证据各读取一次，完成 Provider session 轮换与语义压缩；
  WorkingState 保留、单次修改、可见与隐藏验证全部通过。

之前保存的真实运行证据（各文件记录实际测试 commit）位于 `artifacts/real-system.json`、
`artifacts/real-child.json` 和 `artifacts/real-compaction.json`。
这些工件证明各自记录的提交；当前代码修复后没有重跑外部 LLM，也没有改写历史结果。
