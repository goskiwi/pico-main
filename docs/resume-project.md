# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function calling 统一模型决策、工具准入、执行结果回写、恢复、验证和持久化。

## 单一 Run Log

将 TaskContract、恢复 `user_guidance`、单 Tool Call 或纯 Observation Batch、逐 Call `tool_started/tool_result`、Verification、Compaction 和终态写入同一 strict append-only Run Log。实时执行与恢复共用一个 RunProjection，重建 Task、Evidence、Metrics、Pending Call IDs 与最终 Diff receipt。

## Crash Resume

Session 保存会话 ID、Workspace 归属和 `active_run_id`，不保存对话历史。恢复输入先作为 `user_guidance` Fact 持久化。Active Run 只按 call ID 取回 Run Log 中的原始 ToolCall；副作用前 fsync `tool_started`，记录精确潜在影响路径及 before revision；完成后 fsync `tool_result`。恢复时分析 Run Log tail，对未完成工具按路径 revision 生成 not-started、error、partial 或 unknown 结果，绝不盲目重放非幂等副作用。

首个 User Event 保存 Runtime-owned TaskContract。用户显式选择 Ask、Code 或 Auto；Runtime 不调用隐藏分类模型。Contract 保存 Goal、创建时的最大写能力、写入范围和变更验证要求。Resume 可以收窄但不能扩大原 Contract；Auto Approval 必须由本次启动明确选择。WorkingState 继续使用原有 add/remove 增量协议。

## 长上下文治理

稳定角色、执行、工具、WorkingState 与完成规则进入 Responses `instructions`；首轮动态 `input` 依次包含 Runtime task policy、root→CWD 的独立 `repository_instructions`、Task Request 和非空的有界不可信 Context。AGENTS.md 属于可覆盖的项目指令，不是 system policy，也不参加 History Compaction；当前用户请求冲突时优先。恢复输入从持久 `user_guidance` Fact 投影为最后的 latest request；RepoMap 由 Goal、当前请求、WorkingState 和本 Run 已观察/修改路径共同排序，History 位于当前 WorkingState 之前，因此重建 Session 时当前进度不会被原始任务中的步骤重新覆盖。空 Repository Instructions、RepoMap、WorkingState 和 History 不渲染，Function Schema 只进入原生 `tools`；每轮直接发送当前 TaskContract 与工具预算允许的 Schema，final-only 边界缩成 `submit_final` 并重建 Provider Session，不维护 Host capability、Prompt Cache Key 或动态 Provider Overhead。Prompt build 保持只读。Compaction 在 build 前显式准备，以完整 Tool Call/Result 批次为边界；独立模型 Session 与持久 Summary 始终只包含历史 Progress 与 Critical Context。七类 Effective Recovery Context 只是教学组合视图。Summary 失败、无法缩短或最终 Wire 编码放不下时不提交事件，使用近期完整事务的有界投影继续。

## 工具安全

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；`write_file` 只创建新文件，`edit_file` 使用 expected revision 修改已有文件。提交点再次复验 revision，冲突驱动模型重新读取和修复；未找到返回相近当前代码，多处匹配返回行号，三类失败都给出下一次 `read_file` 参数。`run_command` 只在 Code 模式用于用户审批的本机诊断；Auto 不暴露通用 Shell。它和 CLI 自动发现或显式覆盖的 Runtime Verification 共用 HEAD、staged/unstaged diff、非忽略 untracked revision 组成的 Repository 净状态观察。Git 可见变化因缺少可信 Run-start preimage 而形成 `unknown` 并阻止完成；ignored、Workspace 外、网络与后台副作用不在保证内。每次结构化修改保存匹配 before revision 的事务前像，Run 保留首次前像用于最终 Diff，成功终态生成真实净 Unified Diff；若外部漂移，成功提交被阻止，而取消/重置可以省略无法可信生成的 final_diff 后受控收尾。命令执行仅适用于可信仓库，不可信代码必须使用外部 CI、VM 或容器隔离。

文件读取、搜索和文本修改不设固定的整文件大小门槛；读取保留行数与输出字节预算，搜索保留时间、结果数与输出预算，截断会明确反馈。读取按块扫描并计算全文 revision，因此读取少量行仍有全文扫描成本。Preimage 从源文件分块复制原始字节，沿用已有 Artifact 描述与完整性校验，备份未成功不进入修改；不会反复将全文解码、编码来保存备份。文本替换与 Diff 仍处理完整文本，大文件的内存和耗时是已知边界，不宣称恒定内存或任意规模都能快速完成。

## 应用层 Git 交付

`CodingWorkflow` 要求配置 Runtime Verification；在 Core 成功终态后从 RunOutcome 得到净变化路径，并只对运行前未脏的这些路径创建一个 Git Commit。显式 pathspec 不带入用户其他 staged 内容；提交前再次复验 RunChangeSet，不运行 hooks，不 reset、不 push。跳过或失败作为应用层 `CodingResult` 返回，不改变 Core 的 Completion 和恢复语义。

## 多 Agent

Parent 使用单个 `delegate` 创建一个 Explore 或 Implement Child，再用 `integrate_child` 按 Child ID 显式集成。Explore 只读且不创建 Worktree；Implement 必须声明精确写路径并始终在独立 Git Worktree 中运行。Child 具有独立 Session、Run Log 与 Artifact namespace，且不能嵌套委派。Implement 不自动 merge；集成时复验 Parent base，在临时 Worktree 应用 Patch 并运行固定 Verification，全部通过后才写回 Parent。

## 评测与审计

结构化 verifier 绑定 command identity、最后一次 workspace mutation sequence 与 changed-path states，用于核对本次执行结果。每次需要验证的完成提交都实际执行 verifier，包括恢复后的提交；历史通过记录不替代新验证。Completion 不以观察次数或非空 Diff 判断成功；有净变化且 Contract 要求时运行 Verification，已追踪的 partial 始终要求当前状态验证。`A -> B -> A` 不算净变化。失败验证、未知副作用或尚未显式集成的 Implement Child 不能被报告为成功。Completion 证明证据充分且新鲜，不声称证明任意业务语义。

## 对象与调用边界

当前结构重构将身份、TaskContract、WorkingState 和终态字段直接归入 RunProjection，移除
TaskState、TaskLifecycle、RunIdentity 及独立的协议状态副本。RunLog 负责转换验证、落盘、发布
已验证的 Projection 状态；调用方不再手动组合 append 与 apply。Summary 中的 identity/task 分组
是展示格式，不是额外的运行时对象。

每个工具只在一处声明 Schema、权限、校验、Runner 与 effects。单次与批次共用参数准备、
Runner 调用和结果归类；批次保留整批准入与按原序记账。PromptBuilder 是唯一 Prompt 对象，
预算／组装使用无独立状态的函数，压缩计划由 RunLifecycle 提交。ChildState 从 Parent 事件
派生，Completion 不再依赖 Child 执行器是否安装；Runner 仅保留执行与 Worktree 资源。

跨源凭据转发、暂存重命名、多个 Child 顺序集成与中断应用确认已由全链路修复覆盖。
单 Run Log 仍采用单写者模型；没有引入通用多写者或硬实时执行框架。

工具表、单次与批量准入从当前 Config 派生，Context 的默认预算同样读取当前 Config，
不存在“Config 已更新但消费者仍用初始化副本”的行为。验证额外变更进入持久 Evidence，
不能靠再次提交绕过。Git 交付按命令禁用 hooks；Child Git 操作使用 Parent 剩余时间。
Provider 保留 urllib 代理与同源重定向，拒绝跨源跳转，连接后以同一请求 deadline 限制响应头和响应体，
持续小块返回不再无限延长请求。DNS、连接/TLS 由系统及套接字超时控制，普通 Python 计算
仍是协作式停止，不声称整条链路是硬实时系统。

RunHistory 只读选择和渲染历史、生成压缩计划；RunLog 验证并追加压缩事实；Lifecycle
负责恢复对账。当前代码回归不再包含历史 LLM JSON 成绩断言，历史报告仅保留为运行记录，
正式 LLM 报告仍要求干净提交。

SessionStore 直接创建或加载 Session；Pico 接收现成对象，不再包装裸字典。PicoConfig 构造即校验，修改走 dataclasses.replace。工具表只保存未绑定函数，单次调用与 Observation Batch 都显式传入本次 ToolContext，不通过回调寻找当前调用，也不重建整张工具表。Prompt 缓存只保留 Prompt 和工具名；详细预算诊断仍供 Day3/Day5 使用。CodingResult 只在 RunOutcome 外增加 Git 交付信息，变化路径统一读取 outcome.changed_paths。
