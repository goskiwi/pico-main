# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function calling 统一模型决策、工具准入、执行结果回写、恢复、验证和持久化。

## 单一 Run Log

将 TaskContract、恢复 `user_guidance`、Tool Call、`tool_started`、Tool Result、Verification、Compaction 和终态写入同一 strict append-only Run Log。实时执行与恢复共用一个 RunProjection，重建 Task、Evidence、Metrics、单 Pending Call 与最终 Diff receipt。

## Crash Resume

Session 只保存 `active_run_id`。恢复输入先作为 `user_guidance` Fact 持久化。Active Run 只按 call ID 取回 Run Log 中的原始 ToolCall；副作用前 fsync `tool_started`，记录精确潜在影响路径及 before revision；完成后 fsync `tool_result`。恢复时分析 Run Log tail，对未完成工具按路径 revision 生成 not-started、error、partial 或 unknown 结果，绝不盲目重放非幂等副作用。

首个 User Event 保存 Runtime-owned TaskContract。新 Run 由隔离的结构化 IntentClassifier 从自然语言派生 Contract；用户不填写协议字段。Goal、任务类型、写入范围和完成要求不能被 WorkingState 修改；恢复直接复用原 Contract，不重新分类。WorkingState 继续使用原有 add/remove 增量协议。

## 长上下文治理

稳定角色、执行、工具、WorkingState 与完成规则进入 Responses `instructions`；首轮动态 `input` 只包含 Runtime task policy、非空的有界不可信 Context 与 Task Request。恢复输入从持久 `user_guidance` Fact 投影为 latest request；空 RepoMap、WorkingState 和 History 不渲染，Function Schema 只进入原生 `tools`；普通阶段在受支持 Backend 上以 `allowed_tools` 动态收窄名称，final-only 边界则把 Wire Schema 物理缩成 `submit_final` 并重建 Provider Session。Prompt build 保持只读。Compaction 在 build 前显式准备，以完整 Tool Call/Result 批次为边界；独立模型 Session 与持久 Summary 始终只包含历史 Progress 与 Critical Context。七类 Effective Recovery Context 只是教学组合视图。Summary 失败、无法缩短或最终 Wire 编码放不下时不提交事件，使用近期完整事务的有界投影继续。

## 工具安全

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；`write_file` 只创建新文件，`edit_file` 使用 expected revision 修改已有文件。提交点再次复验 revision，冲突驱动模型重新读取和修复。每个路径只保存第一次修改前的 preimage，成功终态生成真实净 Unified Diff；若外部漂移，成功提交被阻止，而取消/重置以明确的 unavailable receipt 受控收尾。模型没有通用 Shell 工具；用户固定 Verification 在 Workspace 中以当前用户权限本机执行，仅适用于可信仓库。不可信代码必须使用外部 CI、VM 或容器隔离。

## 多 Agent

Parent 使用单个 `delegate` 创建一个 Explore 或 Implement Child，再用 `integrate_child` 按 Child ID 显式集成。Explore 只读且不创建 Worktree；Implement 必须声明精确写路径并始终在独立 Git Worktree 中运行。Child 具有独立 Session、Run Log 与 Artifact namespace，且不能嵌套委派。Implement 不自动 merge；集成时复验 Parent base，在临时 Worktree 应用 Patch 并运行固定 Verification，全部通过后才写回 Parent。

## 评测与审计

结构化 verifier 绑定最后一次 workspace mutation sequence 与 changed-path states；freshness 使用时派生，不持久化可变标签。Completion Gate 按 TaskContract 检查 Observation、最终净变化和明确要求的 Verification；`A -> B -> A` 不算净变化。失败验证、未知副作用或尚未显式集成的 Implement Child 不能被报告为成功。
