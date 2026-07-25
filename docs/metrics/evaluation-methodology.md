# Real-model evaluation methodology

Pico 的真实模型评测衡量：Agent 能否在全新 fixture 副本中完成仓库任务，并通过运行结束后才注入的
隐藏测试。它是工程回归，不是通用 coding 能力榜单。

Runner 没有 fake-model 路径。缺少远程模型配置时预检直接失败，artifact 记录
`execution_mode=live_llm`。

## 可复核控制

- 每次尝试从全新 fixture 副本开始；
- hidden verifier 只在 Agent 停止后注入；
- verifier 在无网络 Docker 沙箱中运行；
- `evaluation_snapshot_id` 覆盖 prompt、工具/步数限制、fixture、命令和 hidden verifier；
- artifact 记录 provider、model、Git commit、dirty 状态、Python/platform、Docker 限制、
  temperature、token limit 和 verifier timeout；
- 发布结果必须使用 `--require-clean-worktree`。

## 重复运行

稳定性结论至少跑三次完整 suite：

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_world_tasks_v5.json \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v5-rerun-3x.json \
  --report-path artifacts/real-world-benchmark-v5-rerun-3x.md
```

报告包含 pooled pass rate、每轮 pass rate、population standard deviation、每任务稳定性、失败分类、
token、模型调用和时延。相同任务的重复尝试不是独立样本，标准差只描述固定 suite 的运行波动，
不是对未知仓库的置信区间。

## Parent / Delegate / Total 成本

当前 artifact schema 明确记录 `parent_*`、`delegate_*` 和 `total_*`：

- model calls；
- input/output/cached tokens；
- model failures 与 Action rejections；
- action protocols；
- cumulative model-call duration。

Delegate 只计入当前 attempt 下、workspace root 相同且 parent-agent chain 可验证的 child run。
历史 run、并发无关 run 和外部目录 run 都不计入。需要 delegation 的任务会核对结构化 outcome、
child identity、数量和完成状态；证据缺失时 fail closed。

工具策略证据仍来自 parent trace；child 成本来自相关 child traces。累计模型时长表示工作量，
并发 child 的时长可能重叠；`agent_duration_ms` 才是 parent attempt 的 wall time。

## Repo Map A/B

`no_repo_map` 同时关闭自动 Repo Map section 和 `query_repo_map`，其余模型、任务快照、工具步数、
安全策略和 final 要求保持一致：

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_world_tasks_v4.json \
  --variant full \
  --variant no_repo_map \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v4-rerun-3x.json \
  --report-path artifacts/real-world-benchmark-v4-rerun-3x.md
```

远程模型按顺序调用，因此不是配对确定性推理。结论必须同时阅读 pass rate、每任务稳定性、
token 和 wall time。

V4 专门覆盖 Repo Map 的目标使用场景：同一个多 package fixture、跨模块修改路径，以及
`legacy/`/`experiments/` 中的同名干扰实现。V5 用新的 fixture 检查预算决策，避免在已经观察过的
V4 上把调参结果包装成 held-out 结论。

## 解释边界

- 同一 model label 的服务端实现可能变化；
- temperature 0 不保证远程推理确定；
- suite 被观察并用于改进后即成为回归集；
- pass rate 不衡量 verifier 范围外的可维护性、安全性或用户偏好；
- 任何结果都应同时链接 JSON artifact、Markdown report 和冻结快照。
