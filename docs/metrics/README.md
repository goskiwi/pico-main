# Metrics evidence map

这里只保留能直接支撑当前设计的四组证据。每组都链接到报告；对应的 reviewed JSON 位于
`artifacts/`，fixture 与隐藏 verifier 位于 `benchmarks/`。

## 1. Repo Map 的目标场景

[`real-world-benchmark-v4-repo-map-ablation-3x.md`](real-world-benchmark-v4-repo-map-ablation-3x.md)

- 五个任务共享一个多 package fixture；
- 修改需要沿跨模块 import/call path 定位；
- `legacy/` 和 `experiments/` 中存在同名干扰实现；
- clean commit 上各跑三轮：`full` 13/15，`no_repo_map` 6/15；
- 观察到 +46.7 个百分点，但代价是每次尝试 token 与 wall time 增加。

这是针对 Repo Map 使用场景的工程证据，不是通用 coding 能力结论。

## 2. Repo Map 预算决策

[`real-world-benchmark-v5-repo-map-budget-600-vs-full-first-3x.md`](real-world-benchmark-v5-repo-map-budget-600-vs-full-first-3x.md) ·
[`decision protocol`](v5-repo-map-budget-decision-protocol.md)

V5 首次冻结运行中，动态预算与 600-token cap 均通过 14/15。600 cap 每个成功尝试的
input-plus-output token 下降 3.36%，未达到预注册的 5% 默认切换阈值，因此保留动态默认。

## 3. Repo Map + Undo

[`reliability-benchmark-v1-live-3x.md`](reliability-benchmark-v1-live-3x.md) ·
[`frozen protocol`](reliability-benchmark-v1-protocol.md)

- 跨模块定位 3/3；
- 两个 Undo 场景恢复 6/6；
- 完整 workspace digest 回到运行前状态 6/6；
- 原有脏 README 保留 3/3；
- 九次尝试没有 model failure、Action rejection、trace parse error 或 isolation failure。

## 4. 失败输出与停滞反馈

[`progress-feedback-live-ab-3x.md`](progress-feedback-live-ab-3x.md)

在 snapshot-matched 的 `gpt-5.4` A/B 中，candidate 通过 6/6，baseline 通过 2/6。
pytest tail 定位从 0/3 变为 3/3；停滞恢复从 2/3 变为 3/3，并减少模型调用。

## 5. 精简后的 clean-worktree 回归

[`live-llm-v5-post-simplification-3x.md`](../../artifacts/live-llm-v5-post-simplification-3x.md) ·
[`reviewed JSON`](../../artifacts/live-llm-v5-post-simplification-3x.json)

删除 session-level history、无引用上下文辅助代码与重复 V4 fixture 自检后，以
`--require-clean-worktree` 对 V5 `full` 跑三轮：`gpt-5.4` 共通过 15/15，三轮均为 5/5，
没有 model failure 或 Action rejection。该结果只证明这次运行时精简没有使固定 V5 回归集退化。

## 方法边界

统一方法、快照身份、delegate 成本口径和解释限制见
[`evaluation-methodology.md`](evaluation-methodology.md)。这些 suite 一旦被用于改进 runtime，
就成为回归集；新的 held-out 主张需要新的未观察任务。
