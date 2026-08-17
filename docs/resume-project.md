# 简历项目表述（与当前代码一致）

## Agent Runtime

设计并实现本地 Coding Agent Runtime，基于 OpenAI-compatible Responses 原生 function
calling 统一模型决策、工具准入、执行结果回写、会话状态、Checkpoint 恢复和运行工件
落盘；以 Context、Tool、Memory、State、Evidence、Progress、Event 组成完整控制面。

## 长上下文治理

构建 Task-local append-only Context Ledger、分层上下文组装和 Token 共享预算，对
RepoMap、工作记忆、项目记忆、历史因果链和当前请求按优先级投影。上下文折叠以完整
tool call/result 批次为边界，并通过 generation、active digest 与 Workspace 指纹做
事务式提交，避免并发状态变化导致摘要覆盖新事实。

## 结构化记忆与 RepoMap

实现 Session Working Memory，将目标、最近文件和文件摘要绑定到精确内容 revision，
文件漂移后自动失效；实现以 Markdown card 为唯一事实源的 Project Memory，支持
provenance、版本、过期时间、显式记忆优先和受限文件选择。基于 tree-sitter 构建
Python symbol/reference graph，以 lexical + personalized PageRank 生成 Token 有界的
任务相关 RepoMap，减少盲目全仓读取。

## Checkpoint / Crash Resume

设计 Checkpoint v5 与 hash-chained Runtime Event Log：恢复时严格校验
Session/Checkpoint/Context schema、Runtime 配置、内容级 Workspace 指纹和 event
cursor/digest；进程若中断在工具执行期间，依据事件 receipt 回填结果，没有 terminal
receipt 时标记 unknown/partial，禁止盲目重放潜在副作用。

## 工具安全与执行治理

建立 Registry、Surface、Schema、Policy、Approval 五阶段准入；文件写入采用
revision-bound compare-and-swap 与 fsync/atomic replace，防止并发覆盖。模型请求的命令
强制进入禁网、只读 rootfs 与 Workspace、cap-drop 和资源限额的 Docker inspect/verify
Profile，并共享整轮 deadline / cancellation token；统一识别重复调用、路径逃逸、敏感
信息和部分成功。

## 评测与审计闭环

以 Evidence Ledger 记录观察、Workspace effect 和结构化 verifier 证据；ProgressGovernor
根据新证据、重复失败、Context generation 和测试 failure signature 决定 repair、verify、
replan 或 stop。Workspace 变更必须通过绑定当前内容指纹的 Runtime verifier，Completion
Gate 阻止失败验证或未知副作用被报告为成功。通过事件 replay/stats、report 和 digest
artifact 审计运行过程。

简历中不要宣称 Skills/MCP/子 Agent、多 Provider 或未经真实实验得到的 GAIA/HLE 指标；
这些不在当前实现范围内。
