# 修正协议后的真实 LLM 测试（2026-09-05）

**结果：pytest-dev__pytest-10051 通过既定独立验收；后续审查发现额外的行为回归。** 本轮只运行一个真实模型试次，
没有扩大到批量评测，也没有自动重开失败试次。它是已用于协议开发的样本，不是未见测试集或 RepoMap 对照结果。

逐过程复核补充：候选补丁会清空 setup 阶段调用方已经持有的日志列表；原代码和参考修复通过同一
检查，候选失败。因此“通过既定验收”不能扩大成“补丁完全正确”。原始成绩、补丁、日志保持不变。
详见 [逐过程复核](repo-eval-v2-process-audit-2026-09-05.md)。

## 运行条件

- 模型：`gpt-5.6-luna`；端点沿用项目配置，温度 0。
- 单 Agent、Auto、RepoMap 开启；32 轮、64 次工具执行、600 秒 Run 总预算，每次输出最多 4096 Token。
- 使用原有 CompletionController，TaskContract 中 `verify_changes=true`。
- 公开验证：`python -m pytest -q --tb=short -r fE testing/logging/test_fixture.py`。
- 每次验证在新 Docker 容器中执行当前代码；参考补丁和独立验收测试没有进入模型 workspace。

## 实际执行过程

1. 原代码在容器内执行公开回归测试，通过，才开始模型调用。
2. 模型首次提交后，公开测试失败：setup 日志错误地变成 call 阶段日志。Runtime 拒绝完成，将实际失败输出反馈给模型。
3. 模型自行继续修改，在阶段结束时保存该阶段记录的副本；第二次提交的 15 项公开测试全部通过。
4. Runtime 返回 completed。随后另一容器执行独立验收：1 项 FAIL_TO_PASS 与 15 项 PASS_TO_PASS 全部通过。

整个模型作答过程中没有人工追加提示或修改补丁。此前的预设动作检查只验证接线，不计入这次真实模型成绩。

## 用量与耗时

| 指标 | 实测 |
|---|---:|
| Run 总耗时（包含运行中验证） | 182.861 秒 |
| 逻辑模型调用 | 6 次 |
| API 上报输入 Token | 57,959 |
| API 上报输出 Token | 1,863 |
| 输入与输出字段相加 | 59,822 |
| 接口原始 total_tokens 字段合计 | 59,823 |
| 模型提交后的公开验证 | 失败一次、通过一次 |
| 独立验收耗时 | 9.810 秒 |
| 作答期人工干预 | 0 |

API 上报 Token 不等于实际账单；Provider 内部传输重试可能没有返回用量。本次六次逻辑调用均获得了用量字段。
第四次响应的 total_tokens 比其 input_tokens + output_tokens 多 1，以上保留两个口径，不擅自合并。
182.861 秒只覆盖 Run 区间，包含运行中验证；独立验收另耗时 9.810 秒，原代码预检、准备和两段执行间等待未计入 Run。

## 能说明什么

本次真实模型实际走通了“提交 → 公开测试失败 → 收到反馈继续修复 → 公开测试通过 → 独立验收通过”。
这支持把公开验证作为 Runtime 正常执行链的一部分，而不是省略后再把离线失败算作完整 Pico 的表现。

它不证明通用成功率是 100%，不证明 RepoMap 有收益，也不能与旧的无反馈协议 0/4 合并。
后续应先给未用于协议开发的任务配置并验证公开检查，再在相同条件下成对测试 RepoMap。

## 原始证据

- 运行条件：`artifacts/repo-eval-v2-real-20260905/experiment.json`
- 实际运行源码：同目录的 `runtime-source.zip`
- 指标与调用用量：试次目录内 `result.json`、`requests.json`
- 真实公开测试：试次目录内 `public-verification/check-001` 到 `check-003`（原代码、首次提交、第二次提交）
- 最终补丁：试次目录内 `candidate.patch`
- 独立验收：`artifacts/repo-eval-v2-real-20260905/judge/judgments.json`
- 运行中反馈和终态：试次 workspace 中的 `.pico/runs/*/events.jsonl`

[测试协议与复现命令](../../benchmarks/repo_eval/README.md)

评测接入修改后的本地回归：233 项测试通过，8 项历史报告测试按原配置排除；Ruff 通过。
