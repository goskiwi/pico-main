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
- `max_agent_turns` 只限制单次 `ask()`；同一个 Run 经过多次 Resume 后仍可能持续增长。

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
                    │ Projection + 有效 History    │
                    │ last_sequence + byte_offset │
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
→ 使用同一 Event Replay 逻辑重建 Checkpoint
→ 原子替换后继续统一恢复流程
```

## 4. Checkpoint 数据结构

建议结构：

```json
{
  "run_id": "run_001",
  "session_id": "session_001",
  "last_sequence": 10000,
  "event_log_offset": 8388608,
  "projection": {
    "contract": {},
    "evidence": {},
    "metrics": {},
    "status": "running",
    "stop_reason": "",
    "final_answer": "",
    "pending": null,
    "runtime_feedback": null,
    "final_diff": null
  },
  "history": {
    "summary": "...",
    "retained_transactions": []
  }
}
```

两类状态必须区分：

- `projection`：精确、可执行的 Runtime 状态，必须与完整 Replay 结果一致。
- `history`：提供给模型的有效历史，可以包含有损摘要，但必须保留任务连续性和完整工具事务。

不新增内容哈希或第二套事实权威。Checkpoint 使用原子写入，并通过 schema、Run ID、Session ID、sequence、offset 和尾部事件连续性判断是否可用；任一校验失败时都从同一 Event Log 重建 Checkpoint，不维护旧格式兼容分支。

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

为 `RunProjection` 增加完整的 Checkpoint 序列化与恢复能力，覆盖：

- `TaskContract`
- `RunEvidence`
- `RunMetrics`
- `PendingToolCall`
- `RuntimeFeedback`
- `FinalDiff`
- `last_sequence`

持久化顺序：

```text
1. Event 追加并 fsync
2. 内存 Projection 推进
3. 原子写入 Checkpoint
```

如果第三步中断，旧 Checkpoint 仍然有效，下次恢复只需多重放一段尾部 Event。

### 阶段四：RunLog 支持基线与尾部

当前新事件 sequence 使用 `_events` 长度计算。只加载尾部后，需要改为：

```python
sequence = self.projection.last_sequence + 1
```

恢复后的 RunLog 持有：

```text
Checkpoint Projection 基线
+ Checkpoint History 基线
+ 尾部 Events
```

需要检查和调整：

- `RunLog.append()`
- `RunLog._from_events()`
- `RunLog.history()`
- `RunLog.pending_tool_intent()`
- 所有默认假设 `RunLog.events` 是完整历史的调用方

完整审计和导出仍由 RunStore 提供全量读取接口，不要求活动 RunLog 常驻全部旧事件。

### 阶段五：History Checkpoint

仅保存 Projection 不能解决 PromptBuilder 构建 History 时的全量读取问题。History Checkpoint 应保存：

- 最近一次有效摘要。
- 摘要后保留的完整工具事务。
- 最新用户指导。
- 当前待处理 Runtime 反馈。

恢复后的有效 History：

```text
History Checkpoint
+ Checkpoint 之后的 Event 尾部
```

Checkpoint 边界不能拆开：

```text
tool_intent
→ tool_settlement
```

因此优先只在没有 Pending Tool 的稳定边界保存 Checkpoint。

### 阶段六：触发策略

触发条件：

```text
当前没有 Pending Tool，并且刚完成 Compaction，或距离上次 Checkpoint
新增 10,000 个事件或 2 MiB 日志。
```

自然触发点：

- 成功提交 Compaction 后立即保存。
- 工具事务完成且达到阈值时保存。
- 用户主动暂停或退出前尝试保存。

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
→ 重建 Projection 与 History Checkpoint
→ 原子替换损坏或落后的 Checkpoint
→ 回到 Checkpoint + Tail 的统一恢复流程
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
- Status / Stop Reason / Final Answer
- Pending Tool
- Evidence / Change Set / Uncertain Effects
- Metrics
- Runtime Feedback
- Final Diff
- Last Sequence

### 7.2 History 等价

对同一状态构建模型输入，确保两条恢复路径都保留：

- 原始任务目标。
- 最新用户指令。
- Runtime 反馈。
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
8. `tool_exchange` 提交前中断。
9. `tool_intent` 后中断。
10. `tool_settlement` 后中断。
11. Compaction 后立即中断。
12. 新 Run 初始 Checkpoint 创建过程中断。

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
Projection Checkpoint
+ History Checkpoint
+ event_log_offset 尾部读取
+ Checkpoint 重建
+ Projection / History 等价测试
+ 中断故障测试
+ 恢复性能实测
```

Event Log 分段、历史 UI 分页和已完成 Run 归档不属于本次修改，后续只有在独立测量证明仍有瓶颈时再立项；它们不作为当前方案的第二套实现。

## 10. 面试表述

> 当前实现将完整 Run Event Log 作为事实源，大工具输出通过 Artifact 外置，模型上下文通过 Compaction 控制；但完整 Replay 的恢复成本仍是 O(N)。改造后统一使用 Projection/History Checkpoint，并记录 `last_sequence` 和 Event Log `byte_offset`，恢复时只重放尾部。Checkpoint 是 Event Log 的派生加速状态；缺失、损坏或落后时由同一 Event Replay 逻辑重建，不维护旧格式兼容路径或双写方案。
