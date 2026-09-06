# Pico 16 项问题的最终修复计划

本计划覆盖 [完整审查报告](/Users/yankai/Documents/Course/Agents/pico-main/docs/review-pack/full-audit-2026-09-06/report.md) 的 F01–F16。方案已实施，完整结果见 [修复验收](/Users/yankai/Documents/Course/Agents/pico-main/docs/review-pack/full-audit-2026-09-06/resolution.md)。下文保留原实施计划。

## 对照其他 coding agent 后的取舍

| 已核对的实现 | 可参考的行为 | Pico 的决策 |
|---|---|---|
| [Pi 主循环](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/agent/src/agent-loop.ts#L215) | 模型出错或中止时结束该轮；因长度限制截断的工具调用不执行 | Provider 动作必须经过完整响应状态判断 |
| [Gemini CLI ToolResult](https://github.com/google-gemini/gemini-cli/blob/85aca163f6c73ac6ce380b5447359146b8adcae4/packages/core/src/tools/tools.ts#L742) | 模型内容、界面显示、错误信息有独立字段 | 内部机器事实与脱敏展示分开；这不是声称 Gemini 使用了 Pico 的日志模型 |
| [Pi grep](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/src/core/tools/grep.ts#L119)、[OpenCode grep](https://github.com/anomalyco/opencode/blob/7c2199d84a5830f70a8250731a42ff958145b4d6/packages/opencode/src/tool/grep.ts#L25) | 默认搜索使用 ripgrep，并表达截断/错误 | 搜索只保留一个 rg 引擎，删除 Python 正则回退 |
| [Aider RepoMap](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L134) | 捕获索引的 RecursionError 并禁用 RepoMap | RepoMap 使用迭代遍历，辅助索引失败可降级，不阻止主任务 |
| [Claude Code worktrees](https://code.claude.com/docs/en/worktrees) | 通过工作树隔离任务，保留有改动的工作供后续处理 | 保留 Child 隔离，把组合、验证、写回作为交付流程处理 |
| [HTTPX 重定向处理](https://github.com/encode/httpx/blob/master/httpx/_client.py) | 对跨源重定向的 Authorization 做特殊处理 | Pico 拒绝跨源 Provider 重定向，避免同时转交凭据和请求正文；同源重定向仍受原请求期限约束 |

这些参考证明的是各个具体机制，不代表它们对 Pico 的 16 个场景都有完整保证。Aider 的 get_dirty_files 同样使用 name-only，因此 F15 直接依据 [Git diff 的机器可读格式](https://git-scm.com/docs/git-diff)设计，不照抄该 helper。

## 明确的范围与约束

- 按五个改动包实施，最后统一验收 F01–F16，不逐项交付零散分支。
- 复用 Run Log、事件序号、现有 revision/preimage、Git Worktree 和真实验证命令。
- 删除被替代的实现与对应旧测试假设，不保留兼容开关、旧接口包装或双轨判断。
- 不新增通用状态机、额外 hash 协议、冻结 contract、baseline 或发布 gate。
- 保留权限和写范围约束、未知副作用阻断、前像保护、日志完整性、原子产物发布和命令清理上限。
- 搜索有一项明确的环境要求变化：需要 rg。缺少 rg 时 search 返回可理解的能力错误，不执行 Python 正则回退；CI 和安装说明明确该依赖。
- 现有损坏日志不靠猜测内容迁移；新写入必须合法，旧非法记录明确拒绝并保留审计材料。

## 实施顺序

### 第一组：Provider 边界（F01、F02）

**修改范围：** `pico/providers/clients.py`。

1. 统一解析 scheme、host、有效端口。拒绝跨源重定向及协议降级，不向新源发送 Authorization 或原始请求正文。同源重定向继续遵守原请求的总期限。
2. 只把确认 completed 的 Responses 输出转成可执行动作。in_progress、queued、cancelled、failed、缺失终态及 SSE 提前结束均不能执行其中的工具。
3. 保留现有上下文溢出和传输失败区分；协议错误明确返回，不能通过默认分支假定成功。

**验收：** 两个本地源模拟跳转，目标源不得收到请求；同源跳转正常。所有非完成态均无文件副作用，完整响应仍能执行。SSE 无终止事件也覆盖。

### 第二组：内部事实与工具事务（F03、F04、F05、F06）

**修改范围：** `tool_runtime.py`、`tool_execution.py`、`mutations.py`、`run_log.py`、`run_projection.py`、`run_lifecycle.py`、`verification.py`。

1. 路径、revision、call ID、变更关系等内部机器事实保留精确值。自由文本及模型/界面展示继续脱敏；不对内部结构化事实做无差别字符串替换。
2. 日志追加先用与 replay 相同的状态转换语义构造并验证待提交状态，验证成功后才落盘和发布。不能在事件落盘之后才发现 WorkingState/RunEvidence 无法接受它。内存状态与持久化失败继续使用现有 reload/replay 处理，不猜测追加是否成功。
3. 修改操作的 before bytes、before revision、preimage 和 tool_started 来自同一份经核对的数据。仍在提交点检查实际 revision。每次事务需要的前像可通过已有 artifact 字段保存，Run 的最初前像继续用于最终 Diff。
4. Git 文件观察无法读取内容时返回 RepositorySnapshotError；unavailable 是无法观察，不作为可比较的文件内容版本。
5. 调用身份按明确的工具事务定位。现有事件序号定位事务，同一在途批次内拒绝重复 ID；恢复只查当前未结束事务的 started/result，不能把跨事务复用的 Provider ID 关联到旧记录。

**验收：** 合成密码与文件名相同仍能写入和回放，展示继续脱敏；非法事实不得污染日志；外部编辑交错时前像与 before revision 一致；Git 不可读文件不能漏报；重复 ID 的新事务未启动时恢复为 not_started，不误认旧副作用。

### 第三组：请求、恢复和当前上下文（F07、F08、F09、F16）

**修改范围：** `agent_loop.py`、`run_lifecycle.py`、`tool_runtime.py`、`session_store.py`、`context_manager.py`、`prompt_builder.py`、`run_cli.py`。

1. 每次 ask/resume 单独计算工具预算，Run 累计次数只用于统计。单调用、批调用和模型工具表面读取同一个剩余预算。恢复补记过去的执行不算本次新执行。
2. --resume latest 按 Run 的真实终态选择候选。清理陈旧索引后继续寻找未完成 Run，不把空状态当作恢复成功。
3. WorkingState 纳入必须保留的当前上下文，删除固定 300-token 尾部裁剪。优先缩减历史和 RepoMap；整个模型窗口仍不足时明确报容量错误，保留原状态。
4. run show/events 与普通入口统一使用 Workspace 的根目录解析。

**验收：** 新请求拿到新的工具预算，批次不能超额；陈旧最新指针不会遮住旧活跃任务；重启/Provider 重置后完整状态仍可见；从仓库子目录能查询实际 Run。

### 第四组：检索、索引和分页（F10、F11、F12、F13）

**修改范围：** `repo_map.py`、`tools.py`、`artifacts.py`，搜索依赖说明及 CI 配置。

1. AST 遍历改成显式栈，单文件解析失败有诊断并降级，不让 RepoMap 阻断主循环。
2. 删除 Python 文件枚举/正则回退，search 统一调用 rg。范围、截断、退出码、超时结果只维护一套语义，并接入当前请求剩余时间与取消信号。
3. 缺少 rg 时返回明确错误，说明如何提供依赖；不伪造空结果。
4. UTF-8 分页要求 max_bytes 至少为 4，接口与底层读取同步校验；成功的非末尾页面必须推进 offset。

**验收：** 深层及损坏语法文件不再使任务在模型请求前崩溃；rg 搜索能命中非首文件，病态回溯模式不会进入 Python re；缺依赖、截断、取消、超时区分正确；中文、emoji、ASCII 混合分页能走到末尾，小于 4 的请求明确拒绝。

### 第五组：组合交付与用户 Git 状态（F14、F15）

**修改范围：** `subagents/integration.py`、`subagents/runner.py`、`subagents/worktree.py`、`subagents/tools.py`、相关恢复逻辑，以及 `applications/coding.py`。

1. 保留原始 Git 基点和 Child 补丁，但后续集成以本 Run 已接纳的当前父状态为起点。把“父工作区全局干净”替换为对已记录变化、HEAD/index 和无关外部变化的具体核对。
2. 在临时工作树中构造“基点 + 已接纳父修改 + 新 Child 补丁”的候选。使用 Git 的补丁/三方合并能力处理可合并变化；冲突只留在候选环境中，不写回父工作区。
3. 验证组合后的候选。通过后只交付本次新增差异；写回前再次核对父状态。用户暂存区保持独立。
4. 正常集成、丢失结果后的恢复、显式重试共享候选构造和应用确认语义。确认依据具体事务的前后事实；当前最终代码仍单独验证，不让历史 integrated 标记替代检查。
5. 继续完整支持 Git 忽略文件的已记录修改、无修改 Child 和交付失败时保留产物。
6. 自动 Git 交付使用 NUL 分隔的 name-status 数据，rename/copy 的源和目标都参与 dirty 路径判断。

**已做的可行性实验：** 在临时 Git 仓库里，先接纳 A，再把 B 应用到包含 A 的候选中，两文件组合通过实际 Python 验证；同一行的冲突返回非零并仅修改候选。父工作区保持原状。这只验证方案机制，不表示 Pico 的集成修复已经完成。

**验收：** 两份不相交补丁连续集成；同文件可合并改动和真实冲突；集成前后中断与再次恢复；集成后再修改同文件；无关外部漂移、用户 index 修改、Git 忽略文件；暂存 rename 的两端均受保护。

## 统一验收与交付

1. 把现有 16 项诊断复现转换为正式回归测试；行为收敛项改写为新契约测试，不保留旧实现帮助旧测试通过。
2. 对高风险路径补失败注入与交错执行：结果落盘前后、文件提交前后、前像采集窗口、组合集成和恢复。
3. 使用真实临时文件、真实 Git、真实 Python 验证命令和本地 HTTP 服务。FakeModelClient 仅替代模型输出，不模拟文件写回和验证成功。
4. 运行全量 pytest、ruff、compileall、diff 检查和七个学习演示；原有权限、原子发布、超时清理和未知副作用测试保留。
5. 一次性复查全部改动，更新 16 项状态和限制说明，集中交付完整结果。

## 代码量估算

预计生产代码净增约 150–300 行，测试净增约 500–800 行。单一 rg 引擎会删除一批旧实现；主要新增工作集中于 F03/F04 的事务一致性和 F14 的组合恢复。该范围是估算，不是验收条件，最终以实际 diff 为准。

预计不需要新增持久化协议或通用框架。当前已完成的修复与用户未提交变更都保留。
