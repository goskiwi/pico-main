# Metrics evidence map

这里只保留能直接支撑当前设计的证据。每组都链接到报告；对应的 reviewed JSON 位于
`artifacts/`，fixture 与隐藏 verifier 位于 `benchmarks/`。经过审阅、随仓库提交的结果收在
`benchmarks/results/`。

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

[`live-llm-v5-post-simplification-3x.md`](../../benchmarks/results/live-llm-v5-post-simplification-3x.md) ·
[`reviewed JSON`](../../benchmarks/results/live-llm-v5-post-simplification-3x.json)

删除 session-level history、无引用上下文辅助代码与重复 V4 fixture 自检后，以
`--require-clean-worktree` 对 V5 `full` 跑三轮：`gpt-5.4` 共通过 15/15，三轮均为 5/5，
没有 model failure 或 Action rejection。该结果只证明这次运行时精简没有使固定 V5 回归集退化。

## 6. 显式运行时验证的真实 OSS smoke

[`runtime-verification-real-oss-smoke-pytest13974.md`](runtime-verification-real-oss-smoke-pytest13974.md)

在干净 commit 上对冻结 pytest #13974 跑一条真实模型 smoke：Pico 的显式 runtime
verifier 首次通过 82 项公开 collection 回归，随后独立隐藏 verifier 通过 1 项。它只证明
运行时验证链路在一个真实任务上可审计地收敛；单任务、单次运行不能解释为总体成功率，且该次
模型留下了临时文件，文档已如实记录这个输出整洁性缺陷。

## 7. 冻结真实 OSS 的最小外部复核

[`run report`](../../benchmarks/results/real-oss-v1-repo-map-ablation-1x.md) ·
[`reviewed JSON`](../../benchmarks/results/real-oss-v1-repo-map-ablation-1x.json) ·
[`protocol`](../benchmarks/real-oss-v1.md)

在干净 commit 上，来自 Pydantic #13215、pytest #13974 和 Click #3487 的三个冻结 pre-fix
checkout 各运行一次 `full` 与 `no_repo_map`。每次尝试都从新副本开始，Agent 结束后才注入隐藏
verifier；六次尝试均通过 hidden verifier 与 workspace-isolation audit。

- `full` 与 `no_repo_map` 均为 3/3，因此没有观察到可归因于 Repo Map 的成功率差异；
- `full` 的平均工具步为 12.33，`no_repo_map` 为 19.00；这只是一次三题套件中的观察值；
- `full` 的 Pydantic 尝试包含一次 276.94 秒的 provider 长尾，因此不能从本次运行推断时延或成本优势。

这组证据补充了真实上游代码上的可复现性，不构成通用 coding-capability benchmark，也不构成
统计稳定的 A/B 结论。

## 8. 十仓库 Real OSS V2 冻结集

[`V2 protocol`](../benchmarks/real-oss-v2.md) ·
[`candidate freeze`](../benchmarks/real-oss-v2-candidates.md) ·
[`run report`](../../benchmarks/results/real-oss-v2-repo-map-ablation-1x.md) ·
[`reviewed JSON`](../../benchmarks/results/real-oss-v2-repo-map-ablation-1x.json)

V2 保留上述三项历史 smoke，并增加 tomlkit、tqdm、packaging、Werkzeug、more-itertools、
Jinja 和 urllib3，形成十个仓库、十个 pre-fix task 的冻结集。每项均已通过本地 preflight：
hidden verifier 在 pre-fix fixture 失败，应用对应上游 PR 补丁后通过。该结果验证了任务与
verifier 的区分力。

在干净 commit `3600c1d` 上，`gpt-5.6-luna` 对 `full` 与 `no_repo_map` 各跑一遍十题，
两组均为 10/10 hidden verifier 通过，且 20/20 workspace-isolation audit 通过。`full`
平均 14.30 个工具步骤、75.57 秒；`no_repo_map` 平均 16.90 个工具步骤、83.22 秒。由于只有
一轮固定十题，成功率没有差异，工具数和时长差异只能视为观察信号，不能解释为稳定的 A/B
或总体能力结论。

## 方法边界

统一方法、快照身份、delegate 成本口径和解释限制见
[`evaluation-methodology.md`](evaluation-methodology.md)。这些 suite 一旦被用于改进 runtime，
就成为回归集；新的 held-out 主张需要新的未观察任务。
