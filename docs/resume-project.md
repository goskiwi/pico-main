# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function calling 统一模型决策、工具准入、执行结果回写、恢复、验证和持久化。

## 单一 Run Log

将 User、Tool Call、`tool_started`、Tool Result、Verification、Compaction 和终态写入同一 strict append-only Run Log。Context、TaskState、Evidence、运行统计与 `pico run show` 均由 Run Log 确定性投影，避免多份持久状态之间的同步和事务问题。

## Crash Resume

Session 只保存 `active_run_id`。副作用前 fsync `tool_started`，记录精确潜在影响路径及 before revision；完成后 fsync `tool_result`。恢复时分析 Run Log tail，对未完成工具按路径 revision 生成 not-started、error、partial 或 unknown 结果，绝不盲目重放非幂等副作用。

## 长上下文治理

以配置的模型 Context Window 组装 RepoMap、WorkingState、Project Memory Catalog、完整活动 Run Log 和当前请求。预算先扣除序列化 Tool Schema；首次 fresh Provider usage 返回后，Runtime 还会记录实际 input 与已知 prompt/schema 之间的协议开销，供后续 fresh prompt 预留。最近一次 fresh Provider usage 是总上下文基准，其后的 Tool Result 使用本地 tokenizer 估算；超过保留输出空间后的阈值才触发 Compaction。Compaction 以完整 Tool Call/Result 批次为边界，使用独立模型 Session 生成 Goal、Constraints、Progress、Key Decisions、Next Steps 和 Critical Context 六段式语义投影；WorkingState 与 Tool Result 仍是权威来源，Summary 失败或不够短时回退确定性事务摘要。Provider 明确报告 context overflow 时只 compact-and-retry 一次。原始 Run events 不删除。

## 工具安全

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；write/edit 先将完整内容写入同目录临时文件并 fsync，在 `os.replace` 提交点复验 expected revision，冲突以结构化 expected/actual revision 驱动模型重新读取和修复。模型命令进入禁网、只读 Workspace 的 Docker Profile，并共享整轮 deadline/cancellation token。

## 多 Agent

Parent 基于显式依赖 DAG 调度 Child；Explore 只读，Implement 在独立 Git Worktree 中按精确路径授权。Child 具有独立 Session、Run Log 与 Artifact namespace；Patch 在临时 Integration Worktree 验证后写回 Parent。

## 评测与审计

Run Log `turn_metrics` 每轮保留 Provider continuation、Token 和延迟证据；只有新建或旋转后的 Prompt 保存完整 section projection，复用轮次只保存稳定引用，避免重复遥测。结构化 verifier 绑定 Run Log 中最后一次 workspace mutation sequence，无需全仓内容扫描。Completion Gate 阻止失败验证或未知副作用被报告为成功。大输出保存为当前 Run 范围内的 Artifact。
