# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function calling 统一模型决策、工具准入、执行结果回写、恢复、验证和持久化。

## 单一 Run Journal

将 User、Tool Call、`tool_started`、Tool Result、Verification、Compaction 和终态写入同一 strict append-only Journal。Context、TaskState、Evidence、Report 与 CLI stats 均由 Journal 确定性投影，避免多份持久状态之间的同步和事务问题。

## Crash Resume

Session 只保存 `active_run_id`。副作用前 fsync `tool_started`，记录精确潜在影响路径及 before revision；完成后 fsync `tool_result`。恢复时分析 Journal tail，对未完成工具按路径 revision 生成 not-started、error、partial 或 unknown 结果，绝不盲目重放非幂等副作用。

## 长上下文治理

以配置的模型 Context Window 组装 RepoMap、工作记忆、项目记忆、完整活动 Journal 和当前请求。最近一次 fresh Provider usage 是总上下文基准，其后的 Tool Result 使用本地 tokenizer 估算；超过保留输出空间后的阈值才触发 Compaction。本地 token 统计只负责 section 分配和 cut point；Compaction 以完整 Tool Call/Result 批次为边界，并保存包含目标与约束的结构化摘要和覆盖 Entry ID。Provider 明确报告 context overflow 时只 compact-and-retry 一次。原始 Journal Entry 不删除。

## 工具安全

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；写入采用 revision-bound compare-and-swap 与 fsync/atomic replace。模型命令进入禁网、只读 Workspace 的 Docker Profile，并共享整轮 deadline/cancellation token。

## 多 Agent

Parent 基于显式依赖 DAG 调度 Child；Explore 只读，Implement 在独立 Git Worktree 中按精确路径授权。Child 具有独立 Session、Run Journal 与 Artifact namespace；Patch 在临时 Integration Worktree 验证后写回 Parent。

## 评测与审计

Journal `turn_metrics` 保留 Provider continuation、Token 和延迟证据；结构化 verifier 绑定完整 Workspace 内容指纹。Completion Gate 阻止失败验证或未知副作用被报告为成功。大输出保存为当前 Run 范围内的 Artifact。
