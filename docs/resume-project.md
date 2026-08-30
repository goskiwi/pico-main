# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function calling 统一模型决策、工具准入、执行结果回写、恢复、验证和持久化。

## 单一 Run Log

将 TaskContract、Tool Call、`tool_started`、Tool Result、Verification、Compaction 和终态写入同一 strict append-only Run Log。实时执行与恢复共用一个 RunProjection，重建 Task、Evidence、Metrics、单 Pending Call 与最终 Diff receipt。

## Crash Resume

Session 只保存 `active_run_id`。副作用前 fsync `tool_started`，记录精确潜在影响路径及 before revision；完成后 fsync `tool_result`。恢复时分析 Run Log tail，对未完成工具按路径 revision 生成 not-started、error、partial 或 unknown 结果，绝不盲目重放非幂等副作用。

首个 User Event 保存 Runtime-owned TaskContract。Goal、任务类型、写入范围和完成要求不能被 WorkingState 修改；恢复请求必须保持相同要求。WorkingState 继续使用原有 add/remove 增量协议。

## 长上下文治理

稳定 Runtime 规则进入 Responses `instructions`；Workspace、TaskContract、WorkingState、RepoMap、Project Memory、History 与当前请求进入 `input`；Function Schema 只进入 `tools`。Prompt build 保持只读。Compaction 在 build 前显式准备，以完整 Tool Call/Result 批次为边界；独立模型 Session 只总结历史 Progress 与 Critical Context。Summary 失败或不够短时不提交事件，使用近期完整事务的有界投影继续。

## 工具安全

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；`write_file` 只创建新文件，`edit_file` 使用 expected revision 修改已有文件。提交点再次复验 revision，冲突驱动模型重新读取和修复。每个路径只保存第一次修改前的 preimage，成功终态生成真实净 Unified Diff；若外部漂移，成功提交被阻止，而取消/重置以明确的 unavailable receipt 受控收尾。模型命令进入禁网、只读 Workspace 的 Docker Profile。

## 多 Agent

Parent 基于显式依赖 DAG 调度 Child；Explore 只读，Implement 在独立 Git Worktree 中按精确路径授权。Child 具有独立 Session、Run Log 与 Artifact namespace；Patch 在临时 Integration Worktree 验证后写回 Parent。

## 评测与审计

结构化 verifier 绑定最后一次 workspace mutation sequence 与 changed-path states；freshness 使用时派生，不持久化可变标签。Completion Gate 按 TaskContract 检查 Observation、最终净变化和明确要求的 Verification；`A -> B -> A` 不算净变化。失败验证、未知副作用或未应用 Child Patch 不能被报告为成功。
