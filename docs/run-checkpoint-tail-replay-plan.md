# Run Checkpoint 与尾部重放改造计划

## 文档状态

- 状态：已实现；性能结果见 `docs/performance-baseline.md`。
- 目标：解决超长 Run 在进程重启后需要完整读取和重放 `events.jsonl`，导致恢复时间与内存占用随事件数量线性增长的问题。
- 约束：完整 Event Log 继续作为唯一事实源；Checkpoint 只作为可丢弃、可重建的恢复加速缓存。

## 1. 当前问题

当前恢复路径：

```text
读取完整 events.jsonl
→ 解析全部 RunEvent
→ 从第一个事件开始 replay
→ 重建 RunProjection
→ 从全部事件构建 RunHistory
```

恢复成本近似为：

```text
状态恢复成本 = O(全部事件数)
History 恢复成本 = O(全部事件数)
```

已有机制不能完全解决该问题：

- Artifact 外置只减少单个 Event 的体积，不减少事件数量。
- Context Compaction 只减少模型输入，不减少恢复时需要读取的原始日志。
- `max_model_requests_per_attempt` 只限制单次执行 Attempt；同一个 Run 经过多次 Resume 后仍可能持续增长。

## 2. 具体失败场景

一个 Run 经历多次 Resume，累计数十万甚至更多事件后进程崩溃。再次启动时，Runtime 必须完整读取日志、解析 JSON、重放 Projection，并构建 History，可能出现：

- Resume 延迟显著增长。
- 峰值内存随日志大小增长。
- 即使模型有效上下文已被压缩，本地恢复仍然很慢。
- 极端情况下，恢复所需资源超过宿主机可用资源。

该场景是引入 Checkpoint 的直接依据。Git、版本号、主键、事务、唯一约束、类型和普通功能测试只能保证数据身份与局部正确性，不能降低完整读取和 O(N) Replay 的时间与空间成本。

## 3. 目标架构

```text
                    ┌─────────────────────────────┐
                    │ events.jsonl                │
                    │ 完整、追加式、唯一事实源      │
                    └──────────────┬──────────────┘
                                   │ 推导
                                   ▼
                    ┌─────────────────────────────┐
                    │ checkpoint.json             │
                    │ Task + CompactedContext      │
                    │ Recent Events + Cursor       │
                    └──────────────┬──────────────┘
                                   │
                Resume             ▼
          ┌──────────────────────────────────────┐
          │ 加载 Checkpoint                     │
          │ → 从 byte_offset 读取 Event 尾部     │
          │ → 只 Replay last_sequence 之后的事件 │
          └──────────────────────────────────────┘
```

恢复规则：

```text
Checkpoint 可用
→ 加载 Projection 与 History 基线
→ 读取并重放尾部事件

Checkpoint 缺失、损坏或落后
→ 使用同一 Event Replay 逻辑恢复当前状态
→ 若已处于 ready_for_model，原子重建 Checkpoint
→ 若仍在模型／工具阶段，先由 Tail 状态完成中断结算
```

## 4. Checkpoint 数据结构

当前结构：

```json
{
  "run_id": "run_001",
  "session_id": "session_001",
  "last_sequence": 10000,
  "event_log_offset": 8388608,
  "run_state": {
    "contract": {},
    "status": "running",
    "metrics": {},
    "failure": null
  },
  "context_state": {
    "compacted": null,
    "recent_events": []
  }
}
```

三类状态必须区分：

- `run_state`：任务 Contract、运行状态、Metrics 与当前 Failure。
- `context_state`：一个 CompactedContext 加摘要后的近期精确 Event。
- `last_sequence` 与 `event_log_offset`：Checkpoint 覆盖的 Event sequence 与日志字节位置。

Checkpoint 只在 `ready_for_model` 写入；模型请求、Assistant Turn 或工具执行中的阶段必须位于 Event Log Tail，不能被 Checkpoint 游标吞掉。

不新增内容哈希或第二套事实权威。Checkpoint 使用原子写入，并通过精确字段、Run ID、Session ID、sequence、offset 和尾部事件连续性判断是否可用；任一校验失败时都从同一 Event Log 重建 Checkpoint，不维护旧格式兼容分支。

## 5. 实施阶段

### 阶段一：测量当前恢复成本

在不改变恢复行为的前提下，构建不同规模的 Run：

```text
1,000 Events
10,000 Events
100,000 Events
```

记录：

- `events.jsonl` 字节数。
- 文件读取耗时。
- JSON 解析耗时。
- Projection Replay 耗时。
- History 构建耗时。
- 峰值内存。
- 实际 Replay 的事件数量。

根据测量结果确定 Checkpoint 的事件数或字节数间隔，不预先硬编码没有证据的阈值。

### 阶段二：RunStore 支持尾部读取

新增类似接口：

```python
read_events_from(
    run_id,
    *,
    byte_offset,
    expected_sequence,
)
```

行为：

```text
seek 到 byte_offset
→ 逐行读取后续 Event
→ 第一条 Event 必须等于 expected_sequence
→ 后续 sequence 必须连续
```

调整 `_append_event()`，使其能够返回事件持久化后的文件位置，供 Checkpoint 记录 `event_log_offset`。

### 阶段三：Projection Checkpoint

新增：

```text
pico/run_checkpoint.py
```

职责：

```python
write_run_checkpoint(...)
read_run_checkpoint(...)
```

为 `RunProjection` 增加稳定任务状态的 Checkpoint 序列化与恢复能力，覆盖：

- `TaskContract`
- `RunMetrics`
- 连续失败状态
- `status=running`

Run ID、Session ID 与日志位置都保存在顶层。Checkpoint 只允许在 `running + ready_for_model` 且没有 Active Tool Turn 或 Pending Tool 时生成。

持久化顺序：

```text
1. Event 追加并 fsync
2. 内存 Projection 推进
3. 原子写入 Checkpoint
```

如果第三步中断，旧 Checkpoint 仍然有效，下次恢复只需多重放一段尾部 Event。

### 阶段四：RunLog 支持 Projection 基线与尾部重放

新事件 sequence 使用 Projection 游标：

```python
sequence = self.projection.last_sequence + 1
```

恢复时临时读取：

```text
Checkpoint RunProjection
+ Checkpoint ContextState
+ 尾部 Events
```

尾部 Event 顺序推进 Projection 与 Context 后即可释放，不在 RunLog 内保留第二份
Event Tail。完整审计统一通过 RunStore 读取 `events.jsonl`。

需要检查和调整：

- `RunLog.append()`
- `RunLog._from_events()`
- `RunLog.history()`
- 所有需要完整审计事实的调用方

完整审计和导出仍由 RunStore 提供全量读取接口，不要求活动 RunLog 常驻全部旧事件。

### 阶段五：ContextState Checkpoint

仅保存 Projection 不能解决 PromptBuilder 构建 History 时的全量读取问题。ContextState 应保存：

- 最近一次有效摘要。
- 摘要后保留的完整工具事务。

用户补充指令只存在于 RunLog。尚未被 Compaction 覆盖的原文全部投影到可信用户区域，较早内容随其他历史一起进入摘要。已提交 Summary 必须保留；必要压缩失败或必保内容超预算时，Context 构建明确失败，不降级为缺少约束的 Prompt。

恢复后的有效 History：

```text
ContextState
+ Checkpoint 之后的 Event 尾部
```

Checkpoint 与 Compaction 边界不能拆开：

```text
assistant_turn
→ tool_result（每个 Tool Call 各一个）
```

副作用调用中间还包含 `tool_started`。只有当前 Assistant Turn 的全部 Tool Call 都已有 Tool Result 时，才允许保存 Checkpoint。

### 阶段六：触发策略

触发条件：

```text
当前处于 ready_for_model，并且刚完成 Compaction，或距离上次 Checkpoint
新增 10,000 个事件或 2 MiB 日志。
```

自然触发点：

- 成功提交 Compaction 后立即保存。
- 完整 Assistant Tool Turn 结算且达到阈值时保存。
- Run 初始化与中断结算完成后按阈值保存。

已经 `completed` 或 `stopped` 的 Run 不需要为了 Resume 强制生成新 Checkpoint；除非还要优化历史浏览和审计查询。

## 6. 新恢复流程

```text
1. 读取 checkpoint.json
2. 校验精确字段、run_id、session_id
3. 校验 last_sequence 和 event_log_offset 范围
4. 恢复 Projection 和 History 基线
5. 从 byte_offset 读取 Event 尾部
6. 要求第一条尾部 Event.sequence = last_sequence + 1
7. 顺序应用尾部 Event
8. 返回可继续追加的 RunLog
```

任一步失败：

```text
使用统一 Event Replay 逻辑读取 Event Log
→ 重建 Task、CompactedContext、Recent Events 与执行阶段
→ 仅在 ready_for_model 时原子替换 Checkpoint
→ 中间阶段先按 Replay 结果结算，再在下一个稳定边界保存
```

Checkpoint 只加速正常恢复，不成为恢复成功的前置条件或 Gate。新实现一次性切换到新的 Run 存储契约，不读取旧版 Run 数据，也不提供旧格式迁移逻辑。

## 7. 测试计划

### 7.1 Projection 等价

对同一份完整 Event Log 比较：

```text
完整 Replay 的 RunProjection
==
Checkpoint + Tail Replay 的 RunProjection
```

比较：

- Contract
- 隐含恢复状态：Running / No Pending Tool
- Metrics
- Last Sequence

### 7.2 History 等价

对同一状态构建模型输入，确保两条恢复路径都保留：

- 原始任务目标。
- 近期用户指令原文以及较早指令的摘要。
- 由连续失败状态临时生成的模型纠错提示。
- 最近完整工具事务。
- 已提交的摘要。
- 当前 Workspace 事实。

不要求 JSON 或 Prompt 字节完全相同，但模型有效上下文的语义和事务边界必须一致。

### 7.3 中断与损坏场景

覆盖：

1. Event 已写，Checkpoint 未写。
2. Checkpoint 临时文件写到一半。
3. Checkpoint 落后多个 Event。
4. Checkpoint JSON 损坏。
5. Checkpoint Run ID 或 Session ID 错误。
6. Checkpoint byte offset 越界。
7. 尾部第一条 sequence 不连续。
8. `model_requested` 后中断。
9. `assistant_turn` 后中断。
10. `tool_started` 后中断。
11. 多工具批次部分 `tool_result` 后中断。
12. Compaction 后立即中断。
13. 新 Run 初始 Checkpoint 创建过程中断。

### 7.4 性能验证

在相同 Event Log 数据上比较：

- 完整 Replay 恢复耗时与峰值内存。
- Checkpoint + Tail Replay 恢复耗时与峰值内存。
- 实际读取字节数。
- 实际 Replay 事件数。

性能结论必须来自实际执行和测量，不能只以前置检查或理论复杂度替代。

## 8. 预计文件范围

新增：

```text
pico/run_checkpoint.py
```

修改：

```text
pico/run_store.py
pico/run_log.py
pico/run_projection.py
pico/history.py
pico/run_lifecycle.py
```

仅在测量证明需要可配置阈值时修改：

```text
pico/config.py
```

测试覆盖 Checkpoint 序列化、尾部 Replay、History 等价、中断故障矩阵和恢复性能基准。

## 9. 单次实施范围

本次改造一次完成以下能力，不保留旧版读取路径、不做双写，也不拆成两版共存：

```text
RunProjection
+ ContextState
+ event_log_offset 尾部读取
+ Checkpoint 重建
+ Projection / History 等价测试
+ 中断故障测试
+ 恢复性能实测
```

Event Log 分段、历史 UI 分页和已完成 Run 归档不属于本次修改，后续只有在独立测量证明仍有瓶颈时再立项；它们不作为当前方案的第二套实现。

## 10. 面试表述

> 当前实现将完整 Run Event Log 作为事实源，大工具输出通过 Artifact 外置，模型上下文通过 Compaction 控制；但完整 Replay 的恢复成本仍是 O(N)。改造后 Checkpoint 直接保存 RunProjection、ContextState、`last_sequence` 和 Event Log `byte_offset`，恢复时只重放尾部。Checkpoint 是 Event Log 的派生加速状态；缺失、损坏或落后时由同一 Event Replay 逻辑重建，不维护旧格式兼容路径或双写方案。
