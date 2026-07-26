# 90 秒真实 OSS Demo

这个演示只讲一条主线：Pico 如何在真实冻结仓库中完成一次受控修复，并留下可验证、可恢复的证据。
它不用于宣称通用 coding 能力；任务是 Click #3487 的冻结 pre-fix checkout，隐藏 verifier 只在
Agent 停止后注入。

## 首次准备（不计入演示时间）

需要在项目根目录配置可用的 `OPENAI_API_KEY`、`OPENAI_MODEL`，并准备 Click fixture 和离线镜像：

```bash
uv run python scripts/materialize_real_oss_v1.py --task click_empty_bytes_echo
docker build -f Dockerfile.real-oss-v1 -t pico-real-oss-v1:latest .
```

如果 fixture 已存在，不要重复 materialize；脚本会复用冻结 checkout。运行时始终无网络，模型端点
调用发生在 Pico host 端而非 sandbox 内。

## 演示命令

```bash
uv run python scripts/run_real_oss_v1_demo.py
```

该命令运行唯一的 `full` 尝试、执行运行后隐藏 verifier，并写入忽略的
`artifacts/demo-real-oss-v1/`。它会输出 benchmark Markdown、静态 `report.html`、实际 workspace，
以及两条可复制的 Undo 命令。预热后的典型 Click 尝试约一分钟，但 provider 时延不是 SLA。

若想在同一命令末尾展示 Undo：

```bash
uv run python scripts/run_real_oss_v1_demo.py --undo-after-run
```

先讲 report 再执行这个选项：Undo 会恢复 Agent 本次改动，运行目录与静态报告会保留。

## 讲解节奏

| 时间 | 做什么 | 要说什么 |
|---|---|---|
| 0–10 秒 | 展示任务和 frozen source | “这是 Click 的真实历史 bug；workspace 没有 Git 历史，隐藏测试尚未进入。” |
| 10–45 秒 | 执行 demo 命令 | “Agent 通过严格 tool schema 读取代码和测试；shell 在无网络、只读 RootFS 的 Docker 中执行。” |
| 45–60 秒 | 展示 `benchmark.md` | “Agent 停止后才注入 verifier；结果还会检查 trace、文件路径和 verifier 是否泄露。” |
| 60–75 秒 | 打开 `report.html` | “这里保留工具时间线、修改文件、任务画布和安全审计，而不是只接受模型说它完成了。” |
| 75–90 秒 | 运行输出的 Undo 命令 | “Undo 先比较运行后状态；任何后续人工修改都会整次拒绝恢复，避免半恢复。” |

## 不要这样表述

- 不说“这证明 Pico 比其他 Coding Agent 强”；
- 不说“Docker 等于绝对安全”；
- 不把单个 Demo 当成真实 OSS 评测的成功率证据。

更准确的收尾是：

> 这个 Demo 展示的是可审计的受控执行闭环。更小的真实 OSS 外部复核在 `full` 和 `no_repo_map`
> 下都达到 3/3，因此我只把它作为可复现性证据，而不是性能结论。
