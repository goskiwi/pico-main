# 真实 LLM 回归：2026-09-06

本次使用项目已有 API 凭据与 `gpt-5.6-luna` 请求配置，实际调用配置的 `www.rightapi.ai/codex/v1`。模型实际提出工具动作，Runtime 实际执行读写、验证与 Child 集成。恢复场景由测试程序制造中断现场，之后的模型决策使用真实接口。

## 代码与运行隔离

当前工作区有上一轮尚未提交的修复。测试复制当前源码到临时 Git 仓库并提交测试快照，保留已有的干净仓库检查。对比确认全部 50 个生产 Python 文件与对应快照逐字节一致。用户工作区没有提交、重置或覆盖。

快照与运行结果位于 [source.json](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/source.json)。首轮、单独重试和流式重跑分目录保存，失败结果没有覆盖。

## 实际发现与修改

首轮真实接口返回 HTTP 400：账户只允许流式请求，而适配器请求使用 `stream: false`。这阻断了直接修复、上下文压缩和一个 Child。

`pico/providers/clients.py` 改动两个配置值：请求使用 `stream: true`，Accept 为 `text/event-stream`。复用现有 SSE 解析器，保留终态一致性和未完成输出检查，没有添加自动回退或兼容开关。这是请求流式传输；模型动作仍在完整响应解析后执行，没有增加界面增量输出功能。

协议依据：[OpenAI 官方流式 Responses 文档](https://developers.openai.com/api/docs/guides/streaming-responses)。本次服务商限制来自真实 HTTP 错误，而非推断所有 OpenAI 账户都有此限制。

新增本地 HTTP 回归测试实际启动只接收流式请求的服务，发出增量事件和完整终态，验证适配器正确获得完整工具调用和 usage。新增 `scripts/run_real_recovery_children.py` 保留两个真实回归场景：内容已正确的中断恢复、同文件两次顺序 Child 交付。

第二个真实错误出现在顺序 Child 的续轮：服务端提示历史输入缺少 `text`。随后抓包确认，`streaming-trace/request-11.response` 的完整响应包含没有 `text` 的 `output_text` 消息，且没有工具调用；旧适配器虽然将它判为 invalid，仍把该消息加入后续历史。`request-12.json` 和 `request-13.json` 都带着这条非法消息。

修复改变历史提交规则：只有有效模型动作的响应才追加到续轮历史；invalid 响应仅由 Runtime 生成纠错反馈。没有补造缺失字段，也没有保留非法响应的兼容通道。有效响应中的 reasoning 和工具调用仍原样保留，符合 [Responses 工具调用的历史回传规则](https://developers.openai.com/api/docs/guides/function-calling)。新增的三种状态回归在修改前全部失败，修改后全部通过。

最后一轮使用包含这两个修复的独立快照，两个场景并发，单次 Provider 请求超时 90 秒。此前轮次默认 300 秒；各轮耗时不宜直接比较。没有更换模型或服务地址，也没有无限重试直到通过。

## 结果

最终统一版本：**6 / 9 场景通过**。未完成项按失败保留，没有把中途代码正确或早先版本的通过结果计入最终通过数。

| 场景 | 最终结果 | 说明 | 证据 |
|---|---|---|---|
| 只读问答 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/ask.json) |
| 审批拒绝 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/approval.json) |
| 并发冲突恢复 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/revision.json) |
| 中断后继续修复 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/resume.json) |
| 直接修复 | 未通过 | SSE 响应缺少可接受的完整终态；代码的可见、隐藏测试通过，Run 未完成 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/system.json) |
| 单 Child 交付 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/child.json) |
| 压缩后继续执行 | 通过 | 全部场景断言通过 | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/compaction.json) |
| 内容已正确时恢复完成 | 未通过 | 90 秒请求预算内传输失败：BrokenPipeError | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/resume-accepted.log) |
| 同文件顺序 Child 交付 | 未通过 | 90 秒请求预算内传输失败：BrokenPipeError | [记录](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/sequential-children.log) |

最后一轮原始汇总：[suite.json](/Users/yankai/Documents/Course/Agents/pico-main/artifacts/real-llm-20260906/final/suite.json)。本报告另核对了压缩场景的 completed 终态和 Runtime 验证事件。

首轮的无需修改恢复曾完整通过；顺序 Child 首轮也曾产出同时满足 a() == 1、b() == 2 的内容，但有一次失败后重新委派，严格次数断言未通过。这些历史结果只保留为诊断证据，不计入最终版本的成功率。

## 本地回归

- 最终全量测试：309 passed、5 skipped。
- 最终 Provider 定向测试：21 passed。
- 七个学习演示实际执行通过。
- Ruff、compileall、git diff --check 通过。

## 模型身份与证据边界

所有测试请求均使用配置的 `gpt-5.6-luna`。单独协议探针收到完整 `response.completed`，其中响应 `model` 字段是 `gpt-5.6-terra`。这说明本次不能根据请求模型名断言服务商实际使用的模型；没有改动项目模型配置。探针结果不替代九个完整场景的验收。

真实测试不覆盖 F01–F16 的每一种异常注入；并发、持久化和安全边界仍由相应确定性回归测试验收。
