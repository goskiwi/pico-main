# Pico 面试阅读主线

先掌握三个亮点：上下文治理、可恢复执行、工具安全。沿一次任务阅读，不按目录逐文件背代码。

## 第一遍：一次任务怎样完成

1. [runtime.py](pico/runtime.py)：只看 Pico 构造函数和 ask。Session 选择 Run，Pico 组装对象。
2. [agent_loop.py](pico/agent_loop.py)：先看 run，再看 _step。run 负责初始化、循环和收尾；_step 请求模型并分派一种动作。
3. 同文件的 _next_model_turn：获取工具、准备上下文、请求模型。
4. 同文件的 _handle_tool_turn：按模型顺序逐个调用 ToolRuntime，再把这一轮的全部结果交回模型。
5. 同文件的 _handle_final_action：把模型的 `submit_final` 转成交给 RunLifecycle 的终态声明。

请求准备只走 `PromptBuilder.build_for_run()`：准备预算 → 必要时生成并提交摘要 → 渲染。AgentLoop 的 `_reset_context()` 统一处理 Provider 会话与 Prompt 缓存重建。RunLifecycle 只负责初始化、恢复和终态收尾。工具仍经过同一套校验、审批和 `_execute_prepared()`；局部编辑读取当前内容、验证唯一锚点，再原子替换。

例子：read_file → edit_file → run_shell 运行测试 → 根据失败继续 edit_file → 再次运行测试 → submit_final。
第一次把 ToolRuntime 当成“执行一个工具并给出真实结果”，把 PromptBuilder 当成“构造有预算的输入”。

## 第二遍：三个亮点落在哪

| 问题 | 阅读入口 | 能讲清的行为 |
| --- | --- | --- |
| 历史太长怎么办 | prompt_builder.py 的 prepare/build/plan_compaction；history.py；compaction_summary.py | 完整日志和模型上下文分开；旧历史摘要，近期调用和结果配对保留；当前请求与关键指令优先 |
| 工具写完进程退出怎么办 | run_lifecycle.py 的 initialize；tool_runtime.py 的 reconcile_interrupted | 日志先记录调用和 started；没有可靠 result 时检查当前文件，不自动重放 |
| Shell 超时、等待输入或输出过大怎么办 | tools.py 的 tool_run_shell；command_runner.py | Shell 先审批且默认 stdin 为 EOF；Timeout 有界，输出保留 Head/Tail，超时终止进程组并返回已有输出 |

测试由模型通过 `run_shell` 主动运行，失败结果作为普通 ToolOutcome 返回下一轮。Runtime 不运行隐藏验收，也不声称能自动证明自然语言需求已经满足。

## 第三遍：状态只回答当前问题

- ActiveRunState：RunLog + 当前执行上下文。没有 RunLog 就没有真实任务投影。
- RunLog：完整事件，唯一持久执行事实；Projection 通过回放获得。
- RunProjection：任务状态、Metrics、单个 pending 调用、关键反馈和终态结果。
- ExecutionContext：截止时间 + 共享取消信号。

成功编辑把路径、Diff 和前后状态保存在 ToolOutcome。无法解释的中断命令保留为 unknown，不自动重放；恢复后的第一轮把事实和当前 Workspace 提供给模型，由模型检查后继续。

## 被追问时再读

文件安全：tool_runtime.py 的 _execute_edit → mutations.py。记住审批、当前内容中的唯一锚点、原子替换和写入回执。

协议安全：run_projection.py 的 PendingToolCall。只有可能产生副作用的 Intent 才成为 pending；只读和执行前拒绝以单条 Exchange 闭合，Settlement 必须与 Intent 配对。

输出过大：artifacts.py。主循环只拿预览和工件引用，全文按需读取。

模型接入：providers/clients.py。OpenAI SDK 负责 HTTP、SSE 和标准重试；Pico 负责 Session 级连接复用、消息回放、有序多工具解析、用量和上下文溢出映射。当前只实现 Responses-compatible Adapter，不支持 Chat Completions、Anthropic `/messages` 或 OpenAI Agents SDK，也不宣称已经完成多厂商协议适配。

## 本次改动与验证

完成：整理主循环；删除重复 Evidence 投影、无用执行 ID 和父子取消结构；去掉无文件路径分支里的空快照计算；删除 Prompt、History 和 Compaction 生成后无人消费的诊断元数据。局部编辑读取当前内容、验证唯一锚点并原子替换；成功路径直接采用可信写入回执，不再重复扫描文件。保留审批、FailureInfo.recovery、超时取消、工具事务配对、摘要覆盖校验和关键反馈保护。

定向检查覆盖：编辑—运行测试—根据失败再编辑—再次运行测试，日志重放，未开始调用恢复，局部编辑保留无关外部变化，部分修改与未知命令副作用区分，取消信号。
这些是本地确定性检查，不是真实 LLM 修复基准成绩。

当前代码使用 DeepSeek `deepseek-v4-flash` 做过真实模型冒烟回归：模型读取并修复错误函数，尝试现有测试，在临时环境缺少 pytest 后改用等价断言完成验证，最后通过 `submit_final` 正常结束。该结果只证明真实 Provider 主链可运行，不作为公开 Coding Benchmark 成绩。SDK 接管 HTTP、SSE 与标准重试；Pico 用剩余 deadline 限制请求，并在读取流事件时检查取消。

History 先形成完整工具事务，ContextManager 只做一次预算选择，PromptBuilder 负责采集、摘要协调和最终渲染。规则范围固定为仓库根 `AGENTS.md`。
