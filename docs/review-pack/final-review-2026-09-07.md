# 面试项目最终审查：2026-09-07

审查对象是当前未提交工作区，参考 HEAD 为 `2e03e4b`，包括审查开始时已经存在的 19 个修改文件。初次审查只新增报告；随后按用户要求直接修正实现并补充正式回归。已有修改和历史评测工件均保留，未创建 Git commit。

**状态：R1–R5 已在原实现中修复；后续全项目复审另确认并修正了 Q1–Q3，见下节。Day 7 已删除模拟验证器，使用真实 pytest。项目继续保持面试范围，没有新增状态协议或架构层。** 以下保留各轮复现证据，不把一次回归通过表述成不存在其他缺陷。

## 最终收口复验（当前工作区）

本轮在不提交当前 dirty worktree 的前提下，将当前源码复制到不含 `.git`、`.env*`、历史
Artifacts、缓存和虚拟环境的一次性目录，并创建干净测试提交
`eb03abca5e1ab733058a676fee1d9071b71f8426`。Provider Key 只由原项目环境加载器注入子进程，
没有打印、复制到快照或写入报告。

收口前确认并修正三项 Context 语义：Compaction Summary 是当前预算下可选的派生历史，不能
阻塞较小上下文配置恢复；取消与 deadline 不得包装成摘要失败；下一轮 Context 只计算已经提交
到 replay 的 Provider output。没有增加再次摘要、后台任务、服务端 Compaction 或持久化状态。

真实 Provider smoke 又发现，合法响应可能同时包含 Assistant `message` preamble 和
`function_call`。第一次 Revision 运行因此被过严 Parser 拒绝。一次只记录 item type/函数名、
不记录文本的诊断确认响应形状为 `message(output_text) + function_call`。OpenAI 官方 Responses
参考也把 output 定义为顺序和长度可变的 `ResponseOutputItem` 数组，并允许 output message
作为后续 input。最终 Parser 验证 completed/assistant/output_text 结构，只构造规范化 replay
item；缺 text 的 malformed sibling 仍整轮拒绝，未恢复 raw-output 回放。
[OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

当前最终快照的真实结果：

| 场景 | 结果 | 事实 |
|---|---|---|
| Ask | 通过 | 2 次请求，唯一工具为 `read_file`；精确回答 `ORBIT-7`，无工作区修改 |
| Revision + Completion | 通过 | 5 次请求；`read → edit(conflict) → reread → edit`，Runtime 与独立验证均通过，只修改 `subject.txt` |
| Compaction + Rotation | **未通过** | 12 份证据按序各读一次，Compaction、Provider reset、WorkingState 保留和写范围均成立；模型却把空白规范化实现为 `" ".join(...)` 而不是要求的 `"-".join(...)`，两次错误修改、四次真实验证失败后由 Runtime 以 `completion_block_limit` 停止。未重跑到通过，也未修改 Runtime 掩盖模型错误 |

本地最终回归为 **376 passed，61.12 秒**；Day 3、Day 5 walkthrough、Ruff、compileall 和
`git diff --check` 均通过。三个新增失败复现（小 Context 恢复、Compaction 取消、rejected
output 计量）和合法 message preamble 回放均有正式测试。

随后按面试范围裁剪测试：删除 CLI/Application/评测脚本、完整 Subagent 附录，以及重复的
命令、Mutation、Resume、RepoMap、Verification 和 ToolRuntime 生产边界矩阵。当前只保留
7 个 Core 测试文件、**147 项测试**，8.88 秒全部通过。裁剪不删除或弱化对应实现；历史章节中
出现的旧测试名只记录当时的修复证据，不表示它仍属于当前精简套件。

## 修复后的复审与同类项目对照

复审重跑了 331 项测试（40.96 秒）和七个演示，全部通过；随后对相邻路径作针对性复现，确认以下遗漏。修改前的新回归为 15 failed、4 passed，正常对照保持通过。

| 问题 | 修复前复现 | 根因处理 |
|---|---|---|
| Q1 · 完成阶段的停止处理 | 在验证后观察期间请求 cancel/reset 或等待真实 deadline 到期，仍返回 completed；验证启动前取消被当作基础设施错误 | AgentLoop 复用同一轮异常/停止路径覆盖模型、工具和完成阶段；验证事实先保存，再检查执行状态；Final Diff 构建后、成功终态前也检查状态。取消前已经产生的效果仍保留 |
| Q2 · 评测目录被无条件清空 | 两个 prepare_workspace 对临时已有目录执行后，预先放入的用户文件消失 | 删除递归清空目录的逻辑，统一复用一个新工作区构造函数。默认创建独立临时目录，显式目录必须不存在；完整路径记录到报告，便于检查 |
| Q3 · 请求被当作已取得证据 | Ask 读取被拒绝或越过唯一答案行后仍能通过；压缩场景有一次读取被拒绝，仍计作 12 份证据且判通过 | Ask 从成功的 read_file 结果核对代号证据；压缩场景从成功、完整的读取结果计算证据路径，不再统计调用请求 |

Q3 是受控机制评测的准确性问题，不是通用 Runtime 必须先读文件的要求。普通 Ask/无修改任务仍可直接完成；不能为了评测给核心增加观察次数或非空 Diff 门槛。

针对性回归已通过 77 项，覆盖验证前后和 Diff 后的 cancel/reset/deadline 共九种组合、目录保留、默认工作区独立性、失败/空读取、正常成功路径。最终全量结果见本节末尾。

**2026-09-07 核对的上游原始来源：**

- [Gemini CLI ToolExecutor](https://github.com/google-gemini/gemini-cli/blob/85aca163f6c73ac6ce380b5447359146b8adcae4/packages/core/src/scheduler/tool-executor.ts#L142)：工具返回后先检查 AbortSignal，再决定 Cancelled/Success；异常中的取消也独立分类。这支持 Q1 的停止语义，但不表示 Gemini 使用 Pico 的整个验证/事件协议。
- [Pi AgentLoop](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/agent/src/agent-loop.ts#L215)：error/aborted 响应结束本轮；工具执行边界同样接收并检查 signal。参考的是现有控制流中的取消处理，不是新增持久化状态层。
- [Aider benchmark 工作区](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/benchmark/benchmark.py#L292)：完整重置需要显式 clean，先检查目录形状，再把旧目录移到 OLD；新场景由测试副本构建。其代码也会删除生成的 build/node_modules，因此不能仅凭存在 rmtree 就判断缺陷。Pico 的问题是默认删除调用者指定的整个已有目录。
- [Aider benchmark 判定](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/benchmark/benchmark.py#L981)：实际执行测试命令并读取退出结果。[SWE-bench grading](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L333) 从补丁和测试日志计算 resolved；没有可解析的测试输出时维持未解决。SWE-bench 是评测框架，不是 Coding Agent；这里仅参考判分依据。

用户提供的 `/Users/yankai/Documents/Course/Agents/claude-code-main` 也做了源码核对。它的 README
将仓库标为 2026-03-31 从公开 source map 提取的研究快照，并非 Anthropic 官方仓库或当前版本；
因此下面只说明该快照的实际实现：

- `src/query.ts:1015` 和 `:1485` 在流式响应、工具执行后检查 AbortSignal，分别返回
  `aborted_streaming`、`aborted_tools`；`src/query/stopHooks.ts:283` 在 Stop hooks 期间也检查取消。
  这与 Q1 的结论一致：取消不能落入正常完成路径。
- `src/tools/ExitWorktreeTool/ExitWorktreeTool.ts:175` 只允许退出本会话 EnterWorktree 创建的
  Worktree；无法确认状态或发现修改时，除非明确传入 `discard_changes`，否则拒绝删除。
  底层使用 `git worktree remove --force`，但上层先核对归属和修改，说明关键不是禁用删除命令，
  而是限定它只能处理自己创建且已明确处置的目录。
- `src/utils/queryHelpers.ts:56` 的 `isResultSuccessful` 主要检查最终消息形状；全为
  `tool_result` 的 user message 也可成功。SDK 的这个 `success` 表示查询协议正常收尾，不能据此
  断言业务任务、读取证据或测试已经通过。代码任务的额外验证可以由 Stop hook 等机制提供。

因此不照抄 Claude Code 的 SDK success 判定。Pico 保持自己的 Runtime Verification，同时把
“读取指定文件”限制在声明了这项要求的受控评测里。

用户提供的 `/Users/yankai/Documents/Course/Agents/opencode-dev`（package version 1.18.29，
目录没有 Git metadata）和 `/Users/yankai/Documents/Course/Agents/pi-main`（package version
0.85.1，目录没有 Git metadata）也做了本地源码核对：

- OpenCode 的 `packages/opencode/src/session/run-state.ts:77` 将 Session cancel 交给当前 Runner，
  并取消关联后台任务；`session/processor.ts:591` 把未完成工具持久化为
  `status: error`、`metadata.interrupted: true`，同时记录 Assistant Abort error。其正式测试
  `test/session/prompt.test.ts:1175` 验证 cancel 后保存 `MessageAbortedError`。这继续支持 Q1：
  停止和正常完成是不同事实，不能只把 UI 状态改成 idle。
- OpenCode 普通循环在 `session/prompt.ts:1295` 依据 Provider finish、Tool continuation 和
  Assistant error 决定结束，没有内建 Pico 式固定验证命令。它的 Session 正常结束不能用来证明
  业务正确；同样不支持把“读过文件”做成通用 Completion gate。
- OpenCode 的 `worktree/index.ts:388` 接受原始目录；如果目录不在 `git worktree list` 中但存在，
  `:407-412` 会把它当作孤立目录递归删除。实验 HTTP handler 在
  `server/routes/instance/httpapi/handlers/experimental.ts:122` 直接转交请求目录，没有先核对
  Project sandbox membership。这个实现显然假设调用者提供的是它管理的 sandbox；它不是 Pico
  评测脚本删除任意调用者目录的依据，也不是应当照抄的安全边界。
- Pi `packages/agent/src/agent-loop.ts:215` 遇到模型 `aborted` 直接发出 `agent_end`；工具准备和
  执行段在 `:476`、`:514`、`:636` 等位置检查 AbortSignal。它提供
  `shouldStopAfterTurn` 扩展点，但没有默认业务验证 gate。
- Pi 正式 eval harness 在 `packages/evals/src/pi-harness.ts:122` 使用 `mkdtemp` 创建自己拥有的
  eval 根目录，在 `:213-229` 先保存 Session JSONL、释放 Session，再删除该根目录。其示例 Judge
  `packages/evals/src/extensions.eval.ts:53` 同时核对生成源码、加载错误、成功工具调用及最终回答；
  缺少分数会在 `vitest-evals/summary.ts:173-179` 记为不完整观察。这与 Q2/Q3 的修复方向一致。

这两个本地目录都缺少 Git 提交信息，因此只能把版本字段和文件内容作为当前快照证据，不能断言
它们对应上游最新 commit。

据此保留 Q1、Q2 的基础行为修复；Q3 只修正场景评测，不移入 CompletionController。Pico 不照搬 Docker 后端、多级调度器、自动回滚或更多 gate。

**最终验证：346 passed，46.23 秒，无跳过；七个演示、Ruff、compileall、diff 检查全部通过。**
前述源码对照是静态核对上游实现；本次运行的是 Pico 的本地回归，没有运行上游项目或重新调用外部 LLM。

## 最终真实模型回归

用户指定新的 OpenAI-compatible endpoint 后，使用 `gpt-5.6-luna` 对当前修改创建独立、干净的
临时 Git 测试提交 `f02b128a1af3f807a248eae97647ced898e771fe`。没有提交或修改当前分支，
没有覆盖仓库中原有的历史真实评测工件。Key 只注入测试进程环境，没有写入源码、配置、日志或报告；
对新结果目录检查 `Authorization`、`Bearer` 和 `sk-` 形式内容，命中数为 0。

最终结果为 **9 / 9 场景通过**：

| 场景 | 结果 | 核心事实 |
|---|---|---|
| Ask | 通过 | 成功读取 README，精确回答 `ORBIT-7`，无工作区修改 |
| 审批拒绝 | 通过 | 写入只请求一次，拒绝后未 Started、未重试，文件不存在 |
| Revision 冲突 | 通过 | 注入外部漂移后首次编辑冲突，重新读取并只成功修复一次，保留并发内容 |
| Crash Resume | 通过 | 恢复 dormant Run 和 partial receipt，不重放中断调用，修复后验证通过 |
| 直接修复 | 通过 | 实际定位并修改目标；Runtime、可见和隐藏验证均通过 |
| 单 Child 集成 | 通过 | Child 生成 Patch，Parent 没有直接编辑，显式集成和验证通过 |
| Compaction | 通过 | 12 份证据完整读取一次，发生 Provider reset 和语义压缩，单次修改及验证通过 |
| 已正确内容恢复 | 通过 | 读取当前状态后直接提交，没有制造重复编辑，当前状态验证通过 |
| 顺序 Child | 通过 | `delegate → integrate → delegate → integrate`，两个 Child 分别验证，组合结果通过 |

全部报告、Patch 和运行日志保存在
`artifacts/real-llm-2btocken-20260907.5fHA77/`。该目录按项目规则忽略，不替换版本控制内的历史工件。
每份最终 JSON 的 `runtime.commit_sha` 均为上述测试快照，`passed` 均为 true，`failed_checks` 为空。

首次使用原配置 endpoint 的并发尝试中，直接修复和已正确内容恢复通过；Compaction 因连续
`server_error: Service temporarily unavailable` 失败，Harness 审批场景因连续 `IncompleteRead`
失败。它们属于 Provider 传输失败，不计入新 endpoint 的 9 / 9 结果，也没有被覆盖成通过记录。

## 修复验收

| 问题 | 实现结果 | 正式回归 |
|---|---|---|
| R1 | 主进程退出或管道 EOF 不再跳过 SIGKILL；完成原进程组的清理 | `test_cleanup_kills_same_group_descendant_after_parent_and_pipes_exit`：真实子进程，覆盖超时和取消 |
| R2 | Implement 和受控评测工具表包含 `read_artifact`，沿用 Run 内隔离 | `test_implement_child_reads_artifact_then_edits_and_integrates`：读取、分页、编辑、验证、父集成完整执行 |
| R3 | LF/CRLF 使用统一匹配语义，原始 revision 和前像不变，仅替换选中片段 | `test_multiline_edit_from_crlf_read_output_replays_exact_bytes` 及 LF/CRLF、混合换行、歧义、无修改回归 |
| R4 | 压缩评测要求 completed 和 Run 内验证通过，保存完整 outcome；统计真正的多调用响应 | `test_compaction_evaluation_requires_completed_verified_run`：真实 Run 分别停止/完成，实际执行可见和隐藏验证 |
| R5 | Ask 评测直接检查 `ORBIT-7`，复审进一步核对工具取得的证据 | `test_ask_evaluation_checks_observed_answer`：正确/错误答案及拒绝/空读取对照 |

新增的回归在修改实现前执行，确认失败场景；修改后定向 113 项和全量 **331 项测试通过**（40.70 秒，无跳过）。七个 Day 1–7 演示全部执行通过；Ruff、compileall 和 diff 检查通过。

Day 7 只预设模型动作。`RecordingVerificationCommandRunner` 已删除，实际 CommandRunner 先执行 pytest 得到 `1 failed`，再由 Runtime 在修改后的完成提交中执行同一测试得到 `1 passed`。输出和讲稿均说明这一边界。

本轮未调用外部 LLM；没有把确定性模型夹具描述成新的在线模型验收，也没有改写历史 JSON/Patch。

## 初次审查覆盖与执行结果

逐文件阅读了全部 50 个 Runtime/Application Python 文件（11,797 行）、20 个测试文件（8,149 行）、12 个脚本（4,384 行），并核对 README、架构/学习/面试文档、旧审查报告、全部受版本控制的 JSON/Patch 工件、项目配置和 CI。审查开始时共 114 个受版本控制文件。锁文件核对了依赖元数据并通过 locked 解析。

不将 `.venv`、构建产物、缓存、旧生成工作区视为当前源码；没有读取 `.env.local` 中的凭据。本轮没有调用外部 LLM。

| 验证 | 结果 |
|---|---|
| `uv run pytest -q` | 319 passed，37.22 秒，无跳过 |
| `uv run ruff check pico applications tests scripts` | 通过 |
| `uv run python -m compileall -q pico applications tests scripts` | 通过 |
| `git diff --check` | 通过 |
| `uv sync --locked --dry-run` | 通过，无需修改环境 |
| Day 1–7 七个 walkthrough | 全部实际执行通过 |
| 本轮针对性复现 | 以下 5 项均复现 |

初查的 319 项测试没有覆盖以下遗漏；修复阶段已新增相应回归。

## 修复前证据与修复结果

### R1 · P2 · 超时清理会漏掉仍在原进程组中的子进程

位置：[command_runner.py:191](../../pico/command_runner.py#L191)。

`_terminate_process_group()` 发送 SIGTERM 后，只要主进程的 `communicate()` 返回就直接结束，不再进入 SIGKILL 分支。一个子进程若忽略 SIGTERM，并把 stdout/stderr 重定向到 DEVNULL，主进程退出和管道关闭不能证明整个进程组已结束。

**实际复现：** 启动父进程和同组子进程；子进程忽略 SIGTERM、关闭输出管道，延迟写入文件。命令在 0.505 秒返回 `deadline_exceeded`，子进程随后仍创建了 `late.txt`。读取 PID/PGID 确认父子属于同一进程组，没有使用 `setsid` 或 `start_new_session` 脱离进程组。复现结束后显式杀掉了残余进程。

影响：超时/取消已经返回，诊断或验证产生的子进程仍可能修改工作区。这不同于文档已经声明的“脱离原进程组的后台进程可能存活”。

修复结果：输出收集完成不再提前结束信号序列；原进程组继续收到 SIGKILL。沿用现有输出排空和超时上限，真实子进程回归覆盖超时和取消后的迟到写入。

### R2 · P2 · Implement Child 无法读取自己生成的大输出 Artifact

位置：[subagents/runner.py:46](../../pico/subagents/runner.py#L46)。

`IMPLEMENT_TOOLS` 只有 `read_file/write_file/edit_file/update_working_state`，缺少 `read_artifact`。但 `read_file` 的大结果会由 ToolRuntime 转为预览，并要求模型调用 `read_artifact` 获取完整结果。

**实际复现：** 按 Implement 的实际白名单读取一个 14 KB 单行文件，尾部放置 `TARGET_AT_TAIL`。读取成功并生成 Artifact，尾部不在模型预览中；按返回提示读取 Artifact，结果为 `rejected / tool_not_allowed`。单行文件无法通过缩小行范围取得被截掉的尾部。

影响：实现子任务不能完整观察某些本可读取的文件，错误和大 Diff 的详细诊断也可能不可达。

修复结果：Implement Child 加入现有只读 `read_artifact`，仍限于当前 Child Run 的 Artifact namespace。评测脚本的显式工具表也同步提供该能力；真实子任务回归完成分页取尾部、编辑、验证和集成。

### R3 · P2 · CRLF 多行代码按读取结果编辑会匹配失败

位置：[tools.py:293](../../pico/tools.py#L293)、[mutations.py:292](../../pico/mutations.py#L292)。

`read_file` 把 `\r\n` 展示为 `\n`，而 `edit()` 在原始解码文本上做精确匹配。返回的 revision 是正确的，但模型看到的多行内容与实际匹配所需内容不一致。

**实际复现：** 文件字节为 `def f():\r\n    return 1\r\n`。读取显示两行代码；去掉行号、以展示的 LF 多行文本作为 `old_text`，使用同次读取的正确 revision 编辑，结果为 `error / text_not_found`。文件未变化。

已有 CRLF 回归只替换单行中不含换行符的字符串，因此没有覆盖此情况。

修复结果：读取继续显示 LF，编辑以相同语义查找 LF/CRLF 文本；其他字符全部按字面量匹配，仍拒绝多处匹配。替换片段的新增换行沿用匹配位置之后的首个换行格式，没有则沿用文件首个换行或 LF；块外字节不变。逻辑内容未变时直接保留原字节，混合换行也不会被无意转换。Revision、前像和 Diff 继续绑定原字节。

### R4 · P2 · 压缩评测会把未完成、未执行 Runtime 验证的 Run 计为通过

位置：[run_real_compaction.py:294](../../scripts/run_real_compaction.py#L294)。

`checks` 检查了压缩、读取、修改和脚本事后执行的可见/隐藏验证，却没有检查 `outcome.status == "completed"`，也没有检查 Run 内的成功 `verification_result`。最终工件还省略了结构化 outcome，进一步掩盖终态。

**实际复现：** 用真实临时 Git 工作区、真实读写工具和真实验证命令，仅替换模型与摘要输出。依次维护 WorkingState、读取 12 份证据、发生真实 Context 轮换与压缩、读取并修复目标文件；把主轮数设为 6，使 Run 在修改后以 `agent_turn_limit` 停止。直接计算当前脚本原有的完整 `checks` 表达式，全部为真：

```text
status: stopped
stop_reason: agent_turn_limit
runtime_verification_count: 0
passed_by_existing_checks: true
```

修复结果：评测要求 Run 真正 completed、Run 内验证 passed，并保存 `outcome.to_dict()`；脚本外的正确性检查继续保留。没有给 Runtime 新增完成 gate。

同处的 `tool_group_count` 已改为按 `len(event.tool_calls) > 1` 统计；单调用不再计作多调用响应。

**历史证据核对：** 当前 `artifacts/real-compaction.json` 对应的本地原始 Run Log 最后确实是 `assistant_final`，并存在 passed 的 Verification。因此这里确认的是评测存在假阳性入口，不是断言已经保存的这次历史运行失败。

### R5 · P2 · Ask 评测没有核对回答是否正确

位置：[run_real_harness_cases.py:240](../../scripts/run_real_harness_cases.py#L240)。

题目要求读取 README 并回答固定代号 `ORBIT-7`，但检查只涵盖工具调用、权限、无修改和终态，不检查答案。

**实际复现：** 运行原有 `run_ask()`，仅把网络模型替换为先读取 README、再回答 `WRONG-CODENAME` 的 FakeModelClient。函数返回 `passed: true`，所有检查为真。

修复结果：对固定答案直接检查最终回答等于 `ORBIT-7`；错误回答判失败，正确回答仍通过，没有引入模型判分器。

## 面试材料同步与项目边界

相邻说明已按实际实现同步：

- 学习路径和架构文档清除了 `reload_required`、`execute_pending`、`repeat guards`、Registry 的 History projection functions 等旧表述，修正了 CompletionDecision 字段与事件提交顺序。
- Day 7 使用真实 CommandRunner/pytest；README、学习路径和面试讲稿均明确只有模型动作是预设的。
- “一个调用被拒绝不取消合法兄弟调用”准确描述 ToolRuntime；Provider 对缺少名称、非法 JSON、未声明工具等响应仍会整组返回 invalid，且已有测试明确要求这种行为。面试讲解应说清这两个边界，不必为了统一一句口号而改协议。
- README 和面试讲稿将绝对的“外部编辑不会被覆盖”改为提交前检测 revision 冲突。检查 revision 与 `os.replace` 是两步，不是跨进程原子 compare-and-swap；没有新增跨进程锁或强事务保证。
- 工件来自不同历史提交：Ask/Approval/Revision/Resume 仍使用旧 `assistant_tool_call`，System/Child/Compaction 已使用新事件。保留其日期和 commit 作为历史证据即可，不应表述为当前未提交版本已经全部重跑。
- 学习材料、架构文档和简历说明里反复描述“删除了旧对象/没有第二份状态”等重构过程。面试首页更适合讲当前问题、设计和证据；历史迁移细节留在旧审查记录。

当前主要修改并不是无证据的补丁堆：统一 ToolOutcome 的输出边界、保留完整恢复事实、按调用关联集成回执，以及统一实时/重建反馈，都有对应的失败场景和正式回归。没有必要把这些已有安全措施拆掉。RepoMap 的图排序、Child 的组合恢复可以作为追问内容，不必继续泛化。

## 正式回归入口

从项目根运行：

```bash
uv run pytest -q
uv run ruff check pico applications tests scripts
uv run python scripts/day7_runtime_capstone.py
```

初查 `/tmp` 诊断脚本记录的是修复前行为；正式回归已进入 tests 并由现有 CI 执行，不依赖临时文件。
本轮没有新增 hash 链、冻结 Contract、baseline 文件、通用 gate、多写者存储或分布式恢复。
