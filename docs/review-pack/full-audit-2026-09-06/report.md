# Pico 全链路审查：2026-09-06

> 当前状态：F01–F16 已完成修复和正式回归，见 [修复验收](/Users/yankai/Documents/Course/Agents/pico-main/docs/review-pack/full-audit-2026-09-06/resolution.md)。以下保留修复前证据；源码行号对应当时的审查位置。

本报告检查当前未提交工作区，而非仅检查 HEAD。参考提交为 `9a417f7a04ba52c81c98d1dc782aa90a68da7095`。

## 结果与覆盖范围

共确认 16 项：4 项 P1、11 项 P2、1 项 P3。每项都有独立本地复现，原始输出见 [results.jsonl](/Users/yankai/Documents/Course/Agents/pico-main/docs/review-pack/full-audit-2026-09-06/results.jsonl)。旧复现脚本已清理，当前回归与问题映射见 [修复验收](/Users/yankai/Documents/Course/Agents/pico-main/docs/review-pack/full-audit-2026-09-06/resolution.md)。没有使用真实凭据或外部 Provider；重定向测试仅使用两个本机 HTTP 服务和合成 token。

覆盖 `pico/` 全部生产模块及相邻的 `applications/coding.py`：共 50 个 Python 文件、11,332 行；包括启动配置、Provider 传输/解析、主循环、Context/WorkingState/压缩、文件与搜索工具、日志/投影/恢复、完成验证、Child 与 Git 交付。既有回归：276 passed、5 skipped；lint、compileall 通过。七个学习演示均实际执行通过。

F01、F14、F15 在旧复盘中已有未解决说明，本次确认仍然存在。其余是本轮确认的缺陷或边界不一致。本报告不把此前已经修复的问题重复列作新问题。

审查只新增本目录的报告和诊断材料，没有修改运行时代码。诊断材料未接入 CI；其中断言描述当前缺陷，修复后应转换为相应的通过性回归测试。

## 完整清单

| ID | 优先级 | 问题 | 历史复现编号 |
|---|---|---|---|
| F01 | P1 | [跨源重定向携带 Provider 凭据](/Users/yankai/Documents/Course/Agents/pico-main/pico/providers/clients.py:121) | `redirect_auth` |
| F02 | P1 | [非完成态 Provider 响应仍会执行工具](/Users/yankai/Documents/Course/Agents/pico-main/pico/providers/clients.py:399) | `nonterminal_response` |
| F03 | P1 | [脱敏改写路径事实并污染已落盘日志](/Users/yankai/Documents/Course/Agents/pico-main/pico/tool_runtime.py:691) | `redaction_journal` |
| F04 | P1 | [保存的文件前像与修改版本可能不一致](/Users/yankai/Documents/Course/Agents/pico-main/pico/tool_runtime.py:275) | `preimage_race` |
| F05 | P2 | [Git 模式下不可读文件仍被当作可完整观察](/Users/yankai/Documents/Course/Agents/pico-main/pico/verification.py:62) | `unreadable_git_snapshot` |
| F06 | P2 | [重复 call_id 被接受后，中断恢复错误关联旧调用](/Users/yankai/Documents/Course/Agents/pico-main/pico/run_lifecycle.py:120) | `duplicate_call_id` |
| F07 | P2 | [工具预算没有按 ask/resume 请求重置](/Users/yankai/Documents/Course/Agents/pico-main/pico/agent_loop.py:115) | `tool_budget_resume` |
| F08 | P2 | [--resume latest 会选到已结束的陈旧会话指针](/Users/yankai/Documents/Course/Agents/pico-main/pico/session_store.py:104) | `latest_session` |
| F09 | P2 | [持久化 WorkingState 被固定 300-token 上限截断](/Users/yankai/Documents/Course/Agents/pico-main/pico/context_manager.py:13) | `working_state_clip` |
| F10 | P2 | [深层 Python 语法树可使默认 RepoMap 阻止整个任务](/Users/yankai/Documents/Course/Agents/pico-main/pico/repo_map.py:573) | `deep_repo_map` |
| F11 | P2 | [回退搜索达到文件收集上限后只搜索第一个文件](/Users/yankai/Documents/Course/Agents/pico-main/pico/tools.py:526) | `fallback_scan` |
| F12 | P2 | [回退正则超过搜索期限后仍报告成功](/Users/yankai/Documents/Course/Agents/pico-main/pico/tools.py:509) | `fallback_regex_deadline` |
| F13 | P2 | [UTF-8 产物分页可能成功返回却不前进](/Users/yankai/Documents/Course/Agents/pico-main/pico/artifacts.py:205) | `artifact_page_progress` |
| F14 | P2 | [两个不相交的 Child 补丁无法顺序交付](/Users/yankai/Documents/Course/Agents/pico-main/pico/subagents/integration.py:161) | `two_children` |
| F15 | P2 | [自动 Git 交付遗漏用户暂存重命名的源路径](/Users/yankai/Documents/Course/Agents/pico-main/applications/coding.py:52) | `staged_rename_delivery` |
| F16 | P3 | [run show/events 与普通启动的工作区根解析不一致](/Users/yankai/Documents/Course/Agents/pico-main/pico/run_cli.py:20) | `run_cli_subdirectory` |

## 逐项证据与修复方向

### F01 · P1 · 跨源重定向携带 Provider 凭据

位置：[pico/providers/clients.py:121](/Users/yankai/Documents/Course/Agents/pico-main/pico/providers/clients.py:121)。

**实测：** 两个不同端口的本机 HTTP 服务模拟不同源。源 A 返回 302 后，源 B 收到了 Authorization: Bearer synthetic-audit-token。没有使用真实凭据，也没有访问外网。

**影响：** 配置给一个 Provider 的凭据可被重定向目标接收。

**修复方向：** 按 scheme/host/port 判断源；跨源重定向不转发 Authorization，同源重定向仍按需要支持。


### F02 · P1 · 非完成态 Provider 响应仍会执行工具

位置：[pico/providers/clients.py:399](/Users/yankai/Documents/Course/Agents/pico-main/pico/providers/clients.py:399)。

**实测：** 分别返回 status=in_progress、cancelled、queued，并带一个形状正确的 write_file 调用。三种情况下文件都被创建，后续 Run 都进入 completed。

**影响：** 尚未完成或已取消的响应被当作可执行指令。当前只特判 incomplete，缺少完成态准入。

**修复方向：** 仅接受确认完成的响应作为动作；非终态、取消态和缺失终止事件返回明确协议错误/无效动作，不执行其中工具。


### F03 · P1 · 脱敏改写路径事实并污染已落盘日志

位置：[pico/tool_runtime.py:691](/Users/yankai/Documents/Course/Agents/pico-main/pico/tool_runtime.py:691)。

**实测：** 仅设置合成 DB_PASSWORD=admin，再使用内置 write_file 创建 admin.py。affected_paths 保留 admin.py，structured.path_transitions.path 却变成 <redacted>.py。文件已写入，错误事件也已落盘；随后加载和回放都报 workspace effect lacks path transitions。

**影响：** 常见文件名与短密码值重合即可导致 Run 无法恢复。RunLog 在完整投影约束检查前持久化结果，使异常永久化。

**修复方向：** 把模型展示脱敏与内部路径/版本事实分开；在落盘前校验结果对 WorkingState/RunEvidence 的完整约束，拒绝无法回放的事件。


### F04 · P1 · 保存的文件前像与修改版本可能不一致

位置：[pico/tool_runtime.py:275](/Users/yankai/Documents/Course/Agents/pico-main/pico/tool_runtime.py:275)。

**实测：** 先读取 B；外部编辑器把文件暂时改为 A；Runtime 保存 A 的前像后，外部编辑器恢复 B；edit_file 按 B 的正确 revision 成功改成 C。日志 before_state 指向 B，前像内容却是 A，最终 Diff 错误地表示 A→C。

**影响：** 一个 Runtime 与外部编辑器交错操作即可造成错误的变更归属和最终 Diff，不需要两个进程共同写 Run Log。

**修复方向：** 让前像与被提交的 before revision 来自同一次受校验读取；提交前确认前像对应实际修改的版本，不能独立采集后直接拼接。


### F05 · P2 · Git 模式下不可读文件仍被当作可完整观察

位置：[pico/verification.py:62](/Users/yankai/Documents/Course/Agents/pico-main/pico/verification.py:62)。

**实测：** 创建未被 Git 忽略的 untracked 文件并设为 000。验证命令临时恢复权限、修改内容、再设回 000。前后观察都得到相同的 unavailable 标记，验证返回 passed、workspace_changes=[]，文件实际已改变。

**影响：** 上轮修复了非 Git 扫描失败路径，但 Git untracked 文件的不可读内容仍能漏报副作用。

**修复方向：** 无法确定文件内容状态时使用现有 RepositorySnapshotError；不能把两次相同的 unavailable 当作未变化。


### F06 · P2 · 重复 call_id 被接受后，中断恢复错误关联旧调用

位置：[pico/run_lifecycle.py:120](/Users/yankai/Documents/Course/Agents/pico-main/pico/run_lifecycle.py:120)。

**实测：** 第一个 same 调用已经完成；第二个同 ID 调用只持久化了请求，还未开始。日志接受了它。恢复时从整个 Run 找到第一次的 tool_started，错误认定第二次已执行，最终报 executed tool_result requires tool_started。

**影响：** 第三方 Provider 或程序化调用者重用 ID 后，合法落盘的 Run 可能无法继续。

**修复方向：** 明确 call_id 的唯一性范围，在接纳新调用时执行主键约束；恢复只对当前未结束的事务查找 started/result。


### F07 · P2 · 工具预算没有按 ask/resume 请求重置

位置：[pico/agent_loop.py:115](/Users/yankai/Documents/Course/Agents/pico-main/pico/agent_loop.py:115)。

**实测：** --max-tool-executions=1，前一请求执行一个工具后发生模型异常。新的 resume 请求第一轮就只暴露 submit_final；继续调用工具直接停止，原因为 tool_execution_limit。

**影响：** CLI 宣称 per request，实际比较整个 Run 的累计工具次数；模型轮预算按请求计数，工具预算却不同。

**修复方向：** 统一预算作用域；按已公布的 per-request 语义记录请求起点，并同步单调用、批调用与工具表面判断。


### F08 · P2 · --resume latest 会选到已结束的陈旧会话指针

位置：[pico/session_store.py:104](/Users/yankai/Documents/Course/Agents/pico-main/pico/session_store.py:104)。

**实测：** 旧会话有未完成任务；新会话已结束，但模拟在清除 active_run_id 前中断。--resume latest 选中新会话，加载后清空它的状态，得到 resumed_goal=None，没有恢复旧任务。

**影响：** 用户以为在继续最近未完成任务，实际可能开始一个失去原目标的新 Run。

**修复方向：** 选择 latest 时核对 Run 的真实终态；清理陈旧指针后继续寻找未完成候选。


### F09 · P2 · 持久化 WorkingState 被固定 300-token 上限截断

位置：[pico/context_manager.py:13](/Users/yankai/Documents/Course/Agents/pico-main/pico/context_manager.py:13)。

**实测：** 通过真实 update_working_state 保存多条合法约束和 NEXT_STEP_MUST_SURVIVE。状态里仍有该步骤，构造的 Prompt 却没有；与此同时还有约 270,247 个输入 token 未用。成功的状态更新已从历史显示中移除。

**影响：** Provider 重置或恢复后，模型可能无法看到自己已经保存的约束和下一步；并非整个窗口不足。

**修复方向：** 将需要完整恢复的 WorkingState 作为完整当前状态分配预算，避免与已过滤的历史一起丢失；明确总容量而非无条件裁掉尾部。


### F10 · P2 · 深层 Python 语法树可使默认 RepoMap 阻止整个任务

位置：[pico/repo_map.py:573](/Users/yankai/Documents/Course/Agents/pico-main/pico/repo_map.py:573)。

**实测：** 一个约 3 KB、包含 1,500 层嵌套的 broken.py，在要求修复语法错误时触发 RecursionError；模型请求次数为 0。

**影响：** 辅助索引先于模型崩溃，Agent 无法读取并修复导致索引失败的文件。

**修复方向：** 避免递归遍历依赖 Python 调用栈；索引单文件失败应有明确降级，不应中止整个主任务。


### F11 · P2 · 回退搜索达到文件收集上限后只搜索第一个文件

位置：[pico/tools.py:526](/Users/yankai/Documents/Course/Agents/pico-main/pico/tools.py:526)。

**实测：** 把测试中的文件上限设为 2，needle 在收集范围内的第二个文件。收集阶段设 limited=True 后，搜索循环处理完第一个文件就退出，返回 0 匹配。生产默认上限为 5,000，走同一逻辑。

**影响：** 没有 rg 的大仓库可能漏掉已经纳入本次搜索范围的绝大多数文件。

**修复方向：** 区分文件枚举截断与匹配/输出预算耗尽，继续搜索已经收集的文件。


### F12 · P2 · 回退正则超过搜索期限后仍报告成功

位置：[pico/tools.py:509](/Users/yankai/Documents/Course/Agents/pico-main/pico/tools.py:509)。

**实测：** (a+)+$ 搜索 24 个 a 后接 ! 的文本。测试期限设为 0.01 秒，实际约 0.68 秒，返回 status=success、timed_out=False。

**影响：** 单次正则匹配不可在现有循环中打断，且函数结束时没有再次检查期限。项目已说明协作式停止，本项至少属于超时结果误报。

**修复方向：** 匹配结束及返回前检查截止时间；如需要严格限制回溯成本，采用明确支持有界执行的搜索能力，而非把循环外检查当作硬超时。


### F13 · P2 · UTF-8 产物分页可能成功返回却不前进

位置：[pico/artifacts.py:205](/Users/yankai/Documents/Course/Agents/pico-main/pico/artifacts.py:205)。

**实测：** 读取内容为“中文”的产物，使用 schema 允许的 max_bytes=1。结果成功，但 offset=0、end_offset=0、has_more=True；按提示继续调用会重复同一页。

**影响：** 合法的小页大小会使调用者卡在空页循环。

**修复方向：** 无法容纳一个完整字符时应明确拒绝或保证前进；成功分页不能在还有数据时保持相同 offset。


### F14 · P2 · 两个不相交的 Child 补丁无法顺序交付

位置：[pico/subagents/integration.py:161](/Users/yankai/Documents/Course/Agents/pico-main/pico/subagents/integration.py:161)。

**实测：** 两个 Child 分别创建 first.py、second.py，均成功且路径不相交。第一份补丁集成成功；第二份因父工作区不干净被拒绝，最终完成状态为 subtasks_incomplete。

**影响：** 真实单 Child 恢复已修复，但正常的多个补丁组合仍受首次集成的 clean-base 条件限制。旧复盘中已列为未解决项。

**修复方向：** 为多个交付定义一致的基点与应用语义，允许已经由本 Run 接受的变化参与后续集成，并验证组合结果；保留对无关外部漂移的检查。


### F15 · P2 · 自动 Git 交付遗漏用户暂存重命名的源路径

位置：[applications/coding.py:52](/Users/yankai/Documents/Course/Agents/pico-main/applications/coding.py:52)。

**实测：** 用户已暂存 subject.txt→renamed.txt。dirty_before 只包含 renamed.txt，Pico 新建 subject.txt 后仍自动提交；用户的暂存重命名随后变成单独的新增 renamed.txt。

**影响：** 外围 CodingWorkflow 没识别到与用户操作重叠，改变了预先暂存变更的语义。旧复盘中已列为未解决项。

**修复方向：** 以能保留 rename 两端的 Git 状态格式计算占用路径，源路径和目标路径都参与冲突判断。


### F16 · P3 · run show/events 与普通启动的工作区根解析不一致

位置：[pico/run_cli.py:20](/Users/yankai/Documents/Course/Agents/pico-main/pico/run_cli.py:20)。

**实测：** 从仓库 src 子目录运行 Pico，Run 正确保存到仓库根 .pico/runs；使用相同 --cwd src 执行 run show，却报 Run Log not found。

**影响：** 子目录启动后的检查命令找不到实际存在的 Run，需要用户自行改传仓库根。

**修复方向：** 统一复用 Workspace.build 的根目录解析，或统一并明确两个入口的 cwd 语义。


## 修复顺序

1. 先处理 F01–F05：凭据边界、动作准入、日志可回放性、前像一致性和观察失败语义。
2. 再处理 F06–F10、F14–F15：恢复事务、请求预算、会话选择、当前状态可见性、索引降级以及组合交付。
3. 最后处理 F11–F13、F16：搜索和分页完整性、超时报告、CLI 一致性。

同组问题应一起修改相关实现与真实复现测试，再运行完整回归；不靠新增 hash、冻结 contract、baseline 或 gate 掩盖事实来源和生命周期不一致。

## 已知边界与未宣称的保证

- 单 Run Log 的多个并发写者不属于当前声明的支持范围；F04 是一个 Runtime 与外部编辑器的交错，不依赖多写者日志。
- 宿主 Shell/验证器仍按可信仓库模型运行；未把它们当成操作系统沙箱。
- 脱离进程组的后台进程、网络及工作区外副作用不在现有文件观察契约内。本轮未重新把该已知边界列作缺陷。
- 未进行真实模型在线评测、Docker 后端内部审计或断电级文件系统持久性测试。
- 本次对列明范围完成了一轮完整静态检查与候选复现；不能据此保证不存在其他潜在缺陷。

## 当前回归入口

旧复现脚本已移除。修复后的正式回归从项目根目录运行：

```bash
.venv/bin/pytest -q
```

该命令验证当前实现，不重新制造本报告中的旧缺陷。问题与保留测试的对应关系见修复验收文档。
