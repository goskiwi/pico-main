# Pico

Pico 是一个轻量、本地、单协议的 Coding Agent Runtime。模型每轮通过
OpenAI-compatible Responses function calling 提出一个或多个工具调用；Runtime 掌握工具准入、
副作用、持久化、恢复、验证和最终完成权。CLI 还提供一次一个的显式 Child 委派。

Pico 面向用户已经信任的本地仓库。Code 模式允许模型申请一个需要 Approval 的诊断型
`run_command`；Ask/Auto 不暴露通用 Shell。CLI 自动发现 `tests/test_*.py` 并使用当前 Python
解释器运行 Pytest；特殊项目可用 `--verify-command` 覆盖。命令在 Workspace
中以当前用户权限执行，不是 Sandbox，文件工具的路径约束不能限制其访问其他目录、网络或
子进程。Pico 检测 Repository 可见净变化：Git 仓库使用 Diff、Untracked state 与 HEAD，非 Git
Workspace 使用有界 metadata snapshot；它不追踪 ignored 文件或外部系统副作用。未知仓库、
未知 PR 或其他不可信代码必须放到 Pico 外部的 CI、VM 或容器中运行。
CLI 注入终端 Approval handler；程序化 `Pico.create/resume(...)` 未提供 handler 时，Code 模式的 risky Tool
默认拒绝且不会读取 stdin。

## 快速开始

需要 Python 3.10+ 和 uv；Runtime 不依赖 Docker。

```bash
uv sync

uv run pico \
  --mode code \
  --cwd /path/to/trusted/repo \
  "Fix calculator.add so the existing test passes"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

默认后端要求 `PICO_OPENAI_API_KEY`；有意连接无认证的本机兼容端点时，显式传入
`--base-url http://127.0.0.1:PORT/v1`。
Provider 失败会区分传输错误与 HTTP／Responses 错误，保留底层异常类型、重试次数、状态码
及服务端错误信息；真实评测记录沿用项目的脱敏边界。

用户提交自然语言目标，并显式选择 `ask / code / auto` 模式；默认是 `code`。Runtime 不调用
隐藏分类模型。TaskContract 只保存原始目标、创建时的最大写能力、最低验证要求和写路径范围；
Resume 可以收窄写能力但不能扩大原 Contract，当前启动配置的 Verification 可以把完成要求
收紧。`ask()` 返回结构化 `RunOutcome`，持久事实仍以
`RunStore.replay(run_id)` 为准。

所有相对工具路径以 Workspace 根目录为基准，`.` 不是启动子目录。Workspace 中的
`startup_directory` 单独表示启动位置。
Workspace 摘要显示实际 Root，并把 Git 状态标为上下文构建时的快照：干净时只显示
分支和 clean，有改动才列路径；冲突、查询失败和路径列表截断会明确提示。
不向模型平铺零值计数和增删行统计，底层观测字段与安全检查保持不变。

CLI 从仓库根目录到启动目录加载 `AGENTS.md`；模型访问其他路径时沿其祖先目录按需加载，
不扫描整个仓库。每份规则明确限定目录子树，深层规则只在自身子树内优先。
新规则或规则内容变化时，当前模型调用组记为尚未执行，重建上下文后由模型重新决策，
不会先写入再补规则。重建时从原始调用日志重新发现访问路径，无额外规则状态存储。
规则沿用 32 KiB 总上限，放入 user input 的独立 `repository_instructions`，
不把它提升为 system policy，也不混入普通不可信
仓库 Context。当前用户任务冲突时优先；Mode、工具权限、路径和完成规则仍由 Runtime 代码决定。

每个新 Prompt snapshot 还现场构造一个 `WorkspaceObservation`：明确区分 Git、普通目录和
Git 不可用，表达 branch、detached HEAD 或 unborn branch，并按状态展示有界改动路径与异常提示。
普通 Tool continuation 复用 Provider session，
不把同一个快照反复追加到上下文；RepoMap 再按当前任务和已观察路径提供代码入口。

## 上下文预算

Provider 报上下文超限时，Runtime 强制重建一次，并比较被拒绝请求与新请求的完整本地
输入估算（instructions、工具 Schema、实际回放消息）。只有新输入严格变小才发送重试；
没有缩减则明确报错，提示检查模型窗口／输出配置或减少必需上下文。缩减后仍被拒绝也停止，
保留未完成 Run。比较不采用 Provider usage 或扣除缓存 Token，不新增压缩策略；
本地估算变小并不保证 Provider 一定接受。

一次 Prompt 重建分为 `prepare → plan_compaction → build`：`prepare` 只采样一次
Workspace、RepoMap、规则与任务，并计算可用输入及 History 预算；压缩规划和最终构建共享
这些本次准备数据。Runtime 提交压缩事件后，`build` 重新投影历史，不再次采样仓库。
最后仍检查实际组装后的整包预算，诊断信息由独立 `_metadata` 方法生成。准备数据只在本次
调用链传递，不持久化、不作为跨请求缓存；正常 Provider 续接仍沿用已有 prompt snapshot。

执行资源只限制模型轮数、请求时间和并行度，并支持取消；主 Agent 和 Child 均不设置
独立的工具执行次数额度。一轮多工具调用不再按总次数截断，但权限和执行顺序检查不变。
累计工具执行数仍用于统计。旧 `--max-tool-executions` 参数已删除，不兼容。

`--trace` 的模型统计展示 `input`、`cached`、`output`。`input` 是包含缓存部分的总输入；
`cached` 来自响应的 `usage.input_tokens_details.cached_tokens`，缺失为 `unknown`，
明确返回零才显示 `0`。缓存部分仍占上下文，不能从上下文预算中扣除；这些统计不保证
所用中转服务支持缓存，也不等于本地测试已验证真实命中。

默认主请求输出上限 `--max-new-tokens 32000`，压缩预留
`--compaction-reserve-tokens 32000`，独立摘要请求输出上限
`--summary-max-output-tokens 16000`。这些是起始配置，不是所有模型的最优值；切换模型时，
需按模型实际能力设置输出上限和 `--provider-context-limit`（默认 272000）。

主请求与摘要请求分别计算输入、指令、工具 Schema 和输出预算，三个参数不叠加到同一次请求。
摘要输出上限还受主请求剩余 History 空间约束。摘要源保留语义事实；超预算时裁剪长字段，
标记省略并保留 Artifact 引用，不改写原始 RunLog。连记录元数据都放不下时，走已有的
显式降级路径，不发送超预算摘要请求。

`--compaction-keep-recent-tokens 20000` 是按完整工具响应组选择近期历史的目标预算，
不是保证保留最后 20000 Token，也不是保证完整文件。摘要生成后，只有摘要与实际保留历史
一起符合主请求 History 预算、且确实缩短历史，才提交压缩。以上 Token 数为本地估算，
不代表 Provider 的精确计费或上下文计算；真实摘要事实保真仍需长任务验证。

## 核心运行链

```text
User request + Ask/Code/Auto -> TaskContract
  -> AgentLoop: ModelAction(tool / invalid / final)
  -> ToolRuntime: admission -> tool_started -> Runner -> tool_result
  -> RunLog: append-only, sequenced, fsynced Facts
  -> RunProjection: Task + Evidence + Metrics + one Pending Tool transaction
  -> CompletionController: blocked / verification_required / allowed
  -> RunLifecycle: Runtime Verification + terminal Final Diff + RunOutcome
```

面试主线只需要六点：

1. **单一事实源**：每个 Run 只有一个 Run Log。实时调用 `RunLog.append`，内部依次执行
   `apply_event` 构造并验证待提交状态、存储追加、发布已验证状态；回放使用同一套转换。
2. **可恢复工具事务**：每次模型响应统一写成一个 `assistant_tool_calls`，其中包含一个或
   多个有序 Call。每个 Call 都有独立 Started/Result，崩溃后逐 Call 闭合且不盲目重放。
3. **按工具能力调度**：工具默认独占，只有显式标记的读取工具可并行；混合调用按原顺序切成
   并行段和独占段。并行上限只控制同时运行数，不限制一个响应中的调用数。Runner 可并行，
   RunLog/Projection/Artifact 始终由主线程按模型原始顺序提交。
4. **可观察的原子修改**：`write_file` 以 hard-link 原子 create-only；`edit_file` 携带读取时
   Revision，提交点再次复验后原子替换。ToolRuntime 对声明路径观察真实 before/after 并生成
   transition，检测到 Revision 冲突时保留当前文件并要求重新读取。
5. **证据驱动完成**：每次 `submit_final` 解析一份不可替换的 Verification policy；Runtime
   检查净变化与不确定副作用，运行该 policy 的固定命令，并生成真实
   Final Diff。恢复时新增 verifier 会收紧旧 Run，删除 Contract 要求的 verifier 则关闭失败。
6. **有界循环**：一次活跃 ask/resume 最多 32 个主 Agent Turn 和 600 秒；Child 与 Integration
   继承 Parent Deadline，不重新获得预算。

关键因果 payload（Task、Tool Call/Started/Result、Verification 与终态）使用严格 Schema；
Telemetry、Compaction 等观测 payload 保持可扩展。事件 envelope、sequence 和 event ID 仍会
在持久化读取与 Replay 时校验。

## 真实 CLI 工具面

CLI 默认注册以下十一个原生工具。每轮只把当前 Mode、TaskContract 和工具权限允许的 Schema 发送
给 Provider；ToolRuntime 在本机再次执行准入。

程序化 `Pico.create/resume(..., check_runner=...)` 可选安装 `run_check`：在明确配置的隔离执行器中
运行临时 Python／pytest 复现，Code／Auto 可用，Ask 不暴露。普通 CLI 默认不安装此工具。
执行器由调用方提供，接口与信任边界见 [复现检查说明](docs/review-pack/isolated-checks.md)。

前四个只读工具显式声明为 parallel；其余工具默认 exclusive。同一响应可以混合多种工具，
Runtime 对每个调用独立准入，并在读写边界间建立顺序屏障；一个调用被拒绝不会取消合法兄弟调用。
`--max-parallel-tools` 只限制同时运行的 parallel Runner 数量（默认 4）；更多调用自动分波，
不会因为超过并发数而拒绝整个响应。

| 工具 | Ask | Code | Auto | 作用 |
|---|:---:|:---:|:---:|---|
| `list_files` | ✓ | ✓ | ✓ | 列出 Workspace 文件 |
| `read_file` | ✓ | ✓ | ✓ | 按行读取并返回 Revision |
| `read_artifact` | ✓ | ✓ | ✓ | 分页读取当前 Run 的大输出 |
| `search` | ✓ | ✓ | ✓ | 有界搜索 Workspace |
| `run_command` | — | 询问 | — | 可信本机诊断；Auto 不暴露通用 Shell |
| `write_file` | — | 询问 | 自动 | 只创建不存在的文件 |
| `edit_file` | — | 询问 | 自动 | 按 Revision 修改已有文件 |
| `update_working_state` | ✓ | ✓ | ✓ | 增量维护当前 Run 的约束、决定和下一步 |
| `delegate` | — | ✓ | ✓ | 同步运行一个受限 Child |
| `integrate_child` | — | 询问 | 自动 | 显式验证并集成 Implement Child Patch |
| `submit_final` | ✓ | ✓ | ✓ | 请求 Runtime 进行最终完成检查 |

CLI 的 `build_agent()` 默认安装 Child runner，因此真实 CLI 请求会携带 `delegate` 与
`integrate_child`。程序化 `Pico.create/resume(...)` 默认是单 Agent；只有显式传入
`subagent_model_client_factory` 才加载 Child 工具。交互模式使用 `/state` 查看当前 Run 的
WorkingState。

`read_file` 将 LF/CRLF 统一展示为 LF；`edit_file` 按同样的换行语义匹配唯一文本块，
新增换行沿用修改位置的格式，块外字节保持不变。Revision 和前像始终对应原始文件字节。

## Child 委派边界

`delegate` 一次只运行一个同步 Child，角色为 `explore` 或 `implement`；Child 没有
`delegate`，不能嵌套委派。这里没有 DAG、批量请求、后台队列或并行 worker。

- Explore 直接读取 Parent Workspace，但工具表面只读，不创建 Worktree。
- Implement 要求 Git 仓库根、无未归属变更的 Parent、可解析的 HEAD、非空 `allowed_write_paths` 和固定
  Verification 命令；它始终在独立 Worktree 中运行，返回摘要，有实际修改时才附带不可变 Patch receipt。
  修改范围来自 RunChangeSet，包含明确修改的 Git 忽略文件；Patch 持久化后立即清理 Worktree，失败和
  取消也走相同的有界资源结算。
  Child 可以使用 `read_artifact` 读取自己 Run 的完整工具输出。
- Implement 不自动 merge。Parent 调用 `integrate_child(child_id)`；Runtime 在临时 Worktree 组合已接纳的父修改和新 Patch，
  使用 Git 三方应用并验证组合结果，确认父状态未漂移后再按 revision 写回本次差异。
- 未集成的 Implement Child 会阻止 Parent 完成。

Child 身份、base、verifier 和计划 Worktree 路径在任何 Child 资源创建前进入 Parent 的
`tool_started.operation`；Child Run Log、Session、Artifact 和 Patch 位于 Parent Run 的
`subagents/<child_id>/` 下。若 Parent 在最终 delegate Result 前中断，恢复会以同一个 Child ID
记录 `child_interrupted` 并清理计划 Worktree；不会接纳或重跑这次同步 Child。
已完成 Implement 的 receipt 与 integration 状态可从 Parent Run Log 恢复，并在重启后继续
`integrate_child`；运行中的 Child 执行不会跨 CLI 恢复或重新调度。

## 状态与信任边界

修改目标在参数准备阶段只生成一次计划，文件校验和 Runner 使用计划中的实际目标。
范围检查统一在审批前完成；经过人工审批后重新核对目标和当前权限，目标或权限变化则
拒绝旧操作。审批回调签名为 `(tool_name, arguments, plan)`，终端展示实际目标与操作；
Child 复验不重新生成 ID/worktree 计划，合入仍保留验证后、发布前的内容校验。
工具名称未变但有效权限变化时也会刷新 prompt，写入范围为空时收窄为只读工具集合。

用户修改已被 Run 跟踪的文件后，旧 revision 或未确认的漂移仍会拦截写入。模型通过
`read_file` 重新观察后，读结果会标记 `external_change_observed`；缺失文件也可通过该工具
确认删除。Evidence 将这些版本变化单独记入 `external_changes`，不加入 Agent 工具副作用
列表，并使旧验证失效。重新读取不授予新权限，也不自动合并或回滚用户内容。
如果文件或父目录被符号链接重定向，读取新目标不能确认原路径的漂移；需用户恢复原目标后继续。
最终 Diff 是受跟踪文件从首次修改前到最终状态的工作区差异，不是逐行作者证明：有已观察
外部变化时，回执的 `external_paths`、Diff 文本和 CLI 都明确标注，旧回执结构不兼容。
未观察的再次变化仍会阻止完成；Child 对 Git 基准和索引变化的限制保留。

WorkingState 是模型维护的可修订任务笔记，不是执行事实或权限来源。简单任务可不使用；
多步骤任务规划、用户要求变化、新证据推翻方案、阶段完成时更新，不要求每轮调用。
`constraints` 只记用户明确要求，`decisions` 记采用的方案及简短依据，`next_steps` 只记未完成
工作；完成、取消或过期的项应移除。当前用户要求和新执行证据优先于旧笔记。
这些是模型使用指引，Runtime 仅校验更新结构并可靠保存，不保证模型始终及时更新或判断正确；
写权限和完成验证仍由 TaskContract、工具边界及 Evidence 决定。

工具结果区分实际正文、结构化信息和失败说明：文件读取的路径／revision／范围只在元数据
中提供；修改正文只保留 diff，命令正文只保留 stdout/stderr。修改前后 revision 仍保存在
内部回执中，模型视图只返回当前回执的新 revision，无变化的成功编辑同样保留它。
`path_transitions` 和备份引用留在日志用于恢复，不整包发送给模型；失败、部分成功和未知
副作用仍保留必要诊断。仅无消费者的 `changed`、固定替换次数、diff 字节数及检查代码回显
已移除，累计执行统计和净改动集合不变。

长输出先截断正文；必要的版本、分页、状态信息保留。其他元数据需要省略时显示
`metadata_omitted` 并提供完整 Artifact；必要元数据本身超限会明确报错，不静默丢弃。
Child 失败说明只持久化在 `failure.detail`，Child 投影由该字段重建。取消要求明确继续，
恢复产生的已闭合调用要求重新评估，不默认指示等待。历史摘要保留路径、读取范围、退出状态
和 Child 交接结果，但不重新塞入备份和 revision 等执行标识。

ToolOutcome 的 `content` 只承载实际输出，Runtime 失败解释只放在 `failure.detail`；
纯失败没有输出时，模型消息省略 `content`，但日志仍保留空字符串字段。恢复建议只使用
`failure.recovery`，不再派生或发送 `correction_action`。未进入执行的旧调用会被闭合，
模型应根据当前任务、权限和输入重新决定是否发起新调用，不要求空等，也不会自动重放。
调用结果、执行阶段和副作用状态仍分别保留，带失败的真实部分输出不会因去重而丢弃。

文件工具的执行细节：`read_file` 在分块读取时响应取消和截止时间，整文件 revision 的
计算仍保留；`list_files` 用 `offset`、`limit` 分页，返回 `next_offset`，目录变化时应从零
重新列出，不保证跨页快照隔离。`read_artifact` 只缓存最近一份通过完整性校验的字节内容，
翻页时复用；内容文件的身份、大小、修改时间或描述信息变化后重新读取并校验。
缓存不写入日志，不取代 Artifact 的 hash／长度校验，也不会把未经校验的新页直接返回。

`edit_file` 在现有 Workspace 写锁内读取一次原文，用同一份字节校验预期 revision、
检查 Run 漂移、保存 Preimage 并持久化 `tool_started`，随后匹配和计算替换；提交前仍重新
校验磁盘版本，提交后观察实际状态。备份或开始日志失败不写目标文件，写入后中断仍由日志
恢复处理。这是减少重复读取，不是跨进程原子 CAS；普通写入与 Child 合入流程不变。

工具注册时绑定各自需要的固定依赖（路径、修改服务、Artifact、执行器等）；每次调用的
`ToolContext` 仅携带 `run_id`、`tool_call_id`、`execution_context`、当前 `working_state`
和 `execution_plan`。校验、计划、执行使用同一套绑定，WorkingState 则每次从最新投影获取。

脱敏是已知环境密钥值的文本替换，不是通用秘密检测：工具结果、验证输出和对外 Tool
Artifact 经过脱敏；用户原话、工具参数、原始文件备份及最终 Diff 不保证脱敏。路径和
revision 等机器字段保留原值，Trace 也会展示路径，因此不能把整个 `.pico` 目录或 Trace
当成可直接公开的安全材料。分享前仍需检查。短密钥值可能误替换普通文本，当前算法不理解
代码语义。Artifact 分页会在脱敏后计算输出大小，工具出口的统一脱敏同样保留。

```text
.pico/
  sessions/<session_id>/
    session.json
    runs/<run_id>/
      events.jsonl
      artifacts/*
      subagents/<child_id>/
        patch.diff
        sessions/<child_id>/
          session.json
          runs/<child_run_id>/{events.jsonl,artifacts/}
```

Session 保存会话 ID、Workspace 归属和 `active_run_id`，不保存对话历史。
恢复逻辑仅用于 `Pico.resume()` 和运行中的异常恢复；`Pico.create()` 自己创建新 Session，
不调用恢复、不扫描 Run 目录。旧 `Pico(...)` 入口已移除。恢复时，有指针则直接加载
本 Session 的 Run；无指针时只扫描本 Session 的 runs，恢复首条事件落盘但指针尚未发布的
崩溃窗口。`--resume latest` 遍历 Session，但不重复全仓扫描 Run。旧平铺布局不兼容、不迁移。
查看运行必须指定归属：`pico run show RUN_ID --session SESSION_ID --cwd /path/to/repo`。
恢复会重放 Run Log、修复末尾未完成的 Tool 事务，并把新的
resume 请求作为 `user_guidance` Fact 持久化。文件工具不能访问 `.git/` 或 `.pico/`；固定
`run_command` 和 Verification 都拥有当前用户的宿主权限，不能被描述为 Sandbox。
两者共用 Repository 净状态观察：HEAD、staged/unstaged diff 与非忽略 untracked revision。
`none` 只表示没有观察到该范围内的变化，不表示整台机器没有副作用。

## 阅读与演示

- [15 分钟与七天阅读路径](docs/learning-path.md)
- [Runtime 架构与真实执行路径](docs/architecture/agent-runtime.md)
- [状态所有权](docs/architecture/state-ownership.md)
- [面试讲解与 Demo](docs/review-pack/interview-demo.md)
- [恢复/简历表述](docs/resume-project.md)

五分钟现场只运行：

```bash
uv run python scripts/day7_runtime_capstone.py
```

该演示只预设模型动作；文件修改、pytest 和 RunLog 回放实际执行，并显示修改前失败、修改后通过。

追问 Crash Recovery、Completion 或 Child delegation 时再运行 Day 6。完整回归与机制评测不在
现场展开：

```bash
uv run pytest -q
uv run ruff check pico applications tests scripts
```

运行时加 `--trace` 可以在终端实时查看请求、工具、压缩、恢复及验证过程：

```bash
uv run pico --trace --cwd /path/to/repo "修复测试"
```

Trace 写入 stderr 并立即刷新，默认关闭；不展开 Prompt、文件正文或最终回答。
并行工具结果按 RunLog 提交顺序显示，不代表工作线程的实际结束顺序。
恢复会话时只打印本次新增事件。Trace 不写入第二份持久化状态。

精简回归只保留 155 项面试 Core：AgentLoop、Completion、Context/Compaction、Provider
传输与协议、RunLog/Projection，以及路径安全。CLI 包装、评测脚本、自动 Git 交付、Child
附录和重复生产边界矩阵不再维护独立测试；Day 1–7 与保留的真实 LLM 报告用于演示这些外围路径。
外部仓库题库、Docker 判分、成绩统计及旧审查复现脚本已移除。
默认回归不调用 LLM，也不代表当前代码通过真实模型验收；历史报告保留为运行记录。

真实评测默认创建独立的临时工作区，完整路径写入报告。显式传入 `--workspace` 或
`--workspace-root` 时，实际的场景目录必须尚不存在；脚本不会清空已有目录。
代码正确性按真实验证结果判定；Ask 的指定证据和压缩场景的完整读取属于场景断言，
不构成普通 Runtime 任务的必读文件或读取次数门槛。

## Scope

Pico 不做 Project Memory、Triage、全量 OSS 排行榜评测、多 Provider、XML 工具协议、Skills、
MCP、多租户、远程 Worker、分布式调度或旧状态迁移。这些外围实现不在当前面试分支中。

搜索工具依赖系统安装的 ripgrep（`rg`），没有 Python 正则回退。缺少 rg 时仅搜索返回明确的能力错误。
