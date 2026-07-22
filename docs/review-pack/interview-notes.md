# pico 秋招面试讲稿

## 30 秒项目介绍

pico 是一个面向本地代码仓库的轻量 coding agent runtime，不是简单的 LLM API 包装。它包含受约束的文件/命令工具、强制 Docker 沙箱、上下文和记忆管理、checkpoint、逐事件审计，以及带隐藏测试的真实模型仓库微基准。我的重点不是做一个大而全的平台，而是把“模型动作如何可靠进入工程控制流”做完整。

## 最值得讲的亮点

原始版本让模型输出 XML/JSON 文本，再由 runtime 猜测它是工具调用还是最终答案。这种方式容易出现格式错误、混入解释文字、重复读取，以及结束条件不明确。

改造后，OpenAI-compatible 主循环把所有工具暴露为 strict functions，并增加独立的 `submit_final`：

```text
prompt
  -> function_call(name, arguments, call_id)
  -> 本地参数校验 / 审批 / Docker 工具执行
  -> function_call_output(call_id, result)
  -> 下一次结构化 Action
```

主循环只处理统一的 `ModelAction(tool | final | retry)`。兼容端点不保存 `previous_response_id` 时，客户端会重发结构化 conversation items，而不是退回文本协议。非法参数、缺失调用和运行时拒绝都会进入 `trace.jsonl` 与 `report.json`。

## 用数据说明证据

早期在同一个 `gpt-5.4`、同一组 10 个 task ID、同一 fixture snapshot 上观察到：

| 指标 | 文本/XML | Structured Actions |
|---|---:|---:|
| 隐藏测试通过率 | 60% | 90% |
| 平均模型调用 | 9.50 | 6.40 |
| Action 格式拒绝 | 未单独记录 | 0 |

旧 artifact 没有记录完整 runtime snapshot 和 working-tree dirty 状态，因此不能声称协议是唯一变量。
面试时把它表述为促使后续实验设计升级的历史观察，不包装成严格因果消融。

当前主证据是冻结 V3：在干净 commit 上运行 3 轮共通过 13/15，4/5 任务稳定 3/3。所有 verifier
都在 Agent 停止后注入，并在无网络 Docker 中执行。后续提示实验降到 12/15 后被回滚，这段失败
分析比单次成功 demo 更能说明评测闭环。

## 2 分钟架构讲解

1. `ContextManager` 把稳定规则、memory、history 和当前请求按预算组 prompt。
2. OpenAI-compatible client 通过 strict function calling 返回 `ModelAction`。
3. `agent_loop` 执行有界循环，工具先经过 schema、路径、read-only、approval 和危险命令校验。
4. `run_shell` 没有宿主机回退，只能进入 4 CPU / 4 GB / 512 PIDs、无网络、只读根文件系统的 Docker 容器。
5. 每一步写入 task state、trace、checkpoint、task graph 和最终 report，便于恢复、复盘和评测。

## 面试官可能追问

### 为什么不直接解析 JSON？

JSON 仍是字符串，模型可能在前后加解释、给出多个动作或输出半截内容。function calling 把“选哪个动作”和“参数是什么”放到 provider 协议中；runtime 仍保留自己的 schema 与权限校验，不能因为 provider 声称 strict 就跳过本地边界。

### 为什么需要 submit_final？

最终回答也是状态转换。显式函数让 runtime 能在结束前执行“必须有有效 workspace change”等 guard；被拒绝时可把原因作为对应 call 的结果返回模型，而不是把自然语言误当完成。

### 为什么不用 previous_response_id？

测试的 compatible endpoint 返回 response ID，但不保存服务端状态。客户端因此维护并重发 `function_call` 和 `function_call_output` items。代价是输入会增长，优点是行为可审计、端点无状态也能工作。

### 这是不是生产级 Agent 平台？

不是。它是本地单用户 runtime。生产化仍需要远程隔离执行、多租户身份与配额、集中式 secret manager、持久队列、幂等任务、横向扩容、限流重试、监控告警和供应链策略。项目刻意把边界写清楚，避免用“本地 Docker + trace”冒充完整生产平台。

### 4 CPU / 4 GB / 512 PIDs 合理吗？

它是适合当前 Python 小仓库测试镜像的默认上限，不是安全常数。面试时强调三点：资源必须有界；默认值要避免测试框架正常并发被误杀；CPU、内存和 PID 上限可通过 CLI 参数按仓库调整。安全边界还包括无网络、只读容器根文件系统和 workspace 定向挂载；支持其他语言需要另行构建包含对应工具链的镜像。

## 3 分钟演示顺序

1. 展示 `pico/actions.py` 和 `pico/tools.py` 的 strict function schema。
2. 展示一条 run 的 `trace.jsonl`：`model_parsed -> tool_executed -> checkpoint_created`。
3. 展示 V3 clean-worktree 三轮报告、失败分类和后续负向实验回滚。
4. 展示 frozen manifest 与隐藏 verifier，说明它们在 Agent 结束后才注入。
5. 最后主动说明本地单用户边界与仓库微基准的外部有效性限制。

## 投递前检查

1. 确认工作区干净，提交历史能按功能、验证和证据顺序阅读。
2. 运行离线测试、Docker integration suite 和 runtime package smoke test。
3. 不提交 API Key、`.pico/` 运行目录或 benchmark workspace 副本；只保留已脱敏的指标、报告、fixture 和 verifier。
