# pico 秋招面试讲稿

## 30 秒项目介绍

pico 是一个面向本地代码仓库的轻量 coding agent runtime，不是简单的 LLM API 包装。它包含受约束的文件/命令工具、强制 Docker 沙箱、上下文和记忆管理、checkpoint、逐事件审计，以及带隐藏测试的真实模型 benchmark。我的重点不是做一个大而全的平台，而是把“模型动作如何可靠进入工程控制流”做完整。

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

## 用数据说明收益

在同一个 `gpt-5.4`、同一组 10 个 task ID、同一 fixture snapshot 上：

| 指标 | 文本/XML | Structured Actions |
|---|---:|---:|
| 隐藏测试通过率 | 60% | 90% |
| 平均模型调用 | 9.50 | 6.40 |
| Action 格式拒绝 | 未单独记录 | 0 |

另外，在改造后才运行的 5 个独立 V2 held-out 任务上通过 5/5，覆盖 URL、TTL cache、CSV、event bus tests 和 LRU。所有 verifier 都在 Agent 停止后注入，并在无网络 Docker 中执行。

不要把单次 5/5 说成模型能力的普遍结论。准确表述是：在固定模型、固定快照和一次重复下，held-out 结果没有显示明显的 V1 过拟合。

## 2 分钟架构讲解

1. `ContextManager` 把稳定规则、memory、history 和当前请求按预算组 prompt。
2. provider adapter 返回 `ModelAction`；OpenAI-compatible 使用 strict function calling，文本后端在适配层内部归一化。
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

它是适合常见 Python/Node 小仓库的默认上限，不是安全常数。面试时强调三点：资源必须有界；默认值要避免测试框架正常并发被误杀；CLI 和环境变量允许按仓库调整。安全边界还包括无网络、只读容器根文件系统和 workspace 定向挂载。

## 3 分钟演示顺序

1. 展示 `pico/actions.py` 和 `pico/tools.py` 的 strict function schema。
2. 展示一条 run 的 `trace.jsonl`：`model_parsed -> tool_executed -> checkpoint_created`。
3. 展示 `structured-action-comparison.md` 的 60% 到 90% 和调用数下降。
4. 展示 V2 manifest 与隐藏 verifier，说明它们在 Agent 结束后才注入。
5. 最后主动说明生产边界与单次 benchmark 的统计限制。

## 建议拆成的 Git 提交

当前工作树改动覆盖多轮连续重构，正式投递前建议按以下边界整理历史：

1. `refactor: split runtime context modules`
2. `feat: enforce docker-only shell sandbox and audited resource limits`
3. `feat: add strict structured action protocol for responses backends`
4. `test: add hidden-verifier real-world benchmarks and held-out suite`
5. `docs: publish benchmark evidence and interview review pack`

不要把 API Key、`.pico/` 运行目录或 benchmark workspace 副本提交；只提交已脱敏的指标 JSON、Markdown 报告、fixture 和 verifier。
