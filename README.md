# pico

**一个可审计、沙箱化的本地 Coding Agent Runtime。**

`pico` 把模型输出变成有界的 `tool / final / retry` 状态转换，再经过本地 schema、权限、审批和
Docker 隔离后执行。它直接面向代码仓库工作，并把 task state、trace、工具审计、workspace diff
和最终报告保存在 `.pico/`，便于复盘一次任务为什么成功或失败。

项目定位是本地单用户的实验型 Agent runtime，不是多租户生产平台，也不把小型仓库回归集解释为
通用 coding 能力证明。

## 30 秒概览

| 证据 | 当前结果 |
|---|---|
| 离线工程回归 | 250+ 项测试；CI 覆盖 Python 3.10–3.12 |
| 冻结 V3 仓库微基准 | 干净工作区真实 LLM 三轮通过 13/15，4/5 任务稳定 3/3 |
| Shell 执行边界 | Docker-only、无网络、只读 RootFS、capability drop、CPU/内存/PID 限制 |
| 运行证据 | `task_state.json`、`trace.jsonl`、`report.json`、任务图和完整工具输出 |

```mermaid
flowchart LR
    U["User request"] --> C["ContextManager"]
    C --> M["Responses function call"]
    M --> L["Bounded agent loop"]
    L --> P["Schema / capability / approval"]
    P --> T["File tools or Docker sandbox"]
    T --> L
    L --> A["Trace / report / checkpoint"]
```

## 项目亮点

- **结构化 Agent loop**：OpenAI-compatible 路径使用 strict function calling，统一输出
  `tool / final / retry` Action；多调用响应只执行首个普通工具并延迟其余调用，异常格式和
  runtime guard 拒绝都会进入审计记录。
- **明确的终止语义**：工具额度与结束协议分离；额度耗尽后最多增加一次只暴露
  `submit_final` 的收尾调用，不能借机继续修改工作区。
- **安全执行边界**：文件工具限制敏感路径；Shell 强制进入无网络 Docker 沙箱，不提供宿主机
  回退，并限制 CPU、内存、PIDs、RootFS 和 Linux capabilities。
- **可追溯运行状态**：每次任务保存 `task_state`、trace、工具审计、任务图、完整工具输出和最终
  report，可复盘模型决策、失败类别与工作区变更。
- **真实模型仓库回归**：任务从全新 fixture 开始，Agent 停止后才注入隐藏 verifier；报告记录
  快照哈希、Git 状态、重复运行波动、token、时延和逐任务稳定性。

当前最可信的能力证据是冻结 V3 在干净 commit 上的三轮结果。早期 V1 两次运行曾观察到文本/XML
协议 6/10、structured actions 9/10，但旧 artifact 没有锁定完整 runtime/dirty 状态，因此只作为
历史相关性证据，不表述为严格因果 A/B。完整边界见[真实模型仓库微基准与回归评测](#真实模型仓库微基准与回归评测)。

## 适用场景

- 在本地仓库中排查和修复测试失败
- 基于现有代码完成小步功能修改或重构
- 用受限工具链执行代码阅读、测试和验证
- 通过持久化会话与运行工件续接长任务

## 使用截图

CLI 帮助信息：

![pico help](assets/screenshots/pico-help.png)

启动界面：

![pico start](assets/screenshots/pico-start.png)

REPL 内置命令与会话路径：

![pico repl](assets/screenshots/pico-repl.png)

## 安装

需要 Python 3.10+。

当前 `0.1.0` 以源码仓库作为完整交付物；`Dockerfile.sandbox`、benchmark fixtures 和评测脚本
没有承诺包含在独立 wheel 中。下面的安装与沙箱构建命令都假设你已经 clone 本仓库。

如果你用 `uv`，直接安装参考：

```bash
uv sync --locked
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

`run_shell` 强制使用 Docker 隔离，不提供宿主机执行回退。首次使用前需要构建固定的沙箱镜像：

```bash
docker build -f Dockerfile.sandbox -t pico-sandbox:latest .
```

运行时使用 `--pull=never`，不会自动下载镜像。可以通过 `--sandbox-image` 或
`PICO_SANDBOX_IMAGE` 选择提前构建好的其他镜像。

默认每个 Shell 容器最多使用 4 CPU、4 GB 内存和 512 个进程；可以通过
`--sandbox-cpus`、`--sandbox-memory`、`--sandbox-pids-limit` 按机器和仓库规模调整。

## 快速开始

在当前仓库里启动交互模式：

```bash
uv run pico
```

指定另一个工作目录：

```bash
uv run pico --cwd /path/to/repo
```

直接跑一次性任务：

```bash
uv run pico "inspect the test failures and propose a fix"
```

如果当前环境已经安装过包，也可以直接这样启动：

```bash
python -m pico
```

## 模型配置

在目标工作区的 `.env.local` 中设置 OpenAI-compatible Responses 接口配置：

```bash
OPENAI_API_BASE=https://your-api.example/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-5.4
```

不设置 `OPENAI_API_BASE` 时使用 OpenAI 官方端点；任何兼容或第三方端点都必须显式配置。自定义
端点会接收发送给模型的 prompt 和工具结果，使用前应确认其数据处理与密钥策略。

除显式传入的 `--model` 和 `--base-url` 外，启动时 `pico` 只读取 `--cwd` 目录下的该文件作为
模型配置来源，不会使用终端、CI 或 secret manager 中已有的同名环境变量，也不会把文件中的
任意键批量注入全局进程环境。`.env.local` 位于工作区但默认被 Git 忽略，不要提交密钥。

密钥通过显式配置映射传给模型客户端；`pico` 不会把它复制到 session、trace、report 或 benchmark
artifact，也不会传入 Docker shell 沙箱。

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/reload-skills`：重新从 `.pico/skills/` 加载技能文件
- `/exit` 或 `/quit`：退出 REPL

## Skills

`pico` 可以从本地 `.pico/skills/**/SKILL.md` 读取可复用工作流说明，并按当前用户请求自动匹配后注入 prompt。仓库里提供了一组可提交的示例 skill：

```text
examples/skills/
```

启用这些示例：

```bash
mkdir -p .pico/skills
cp -R examples/skills/* .pico/skills/
```

每个 `SKILL.md` 可以带简单 frontmatter：

```md
---
name: code-review
description: Review code changes for bugs, regressions, safety issues, and missing tests.
---

# Code Review

Review the change as production code.
```

`.pico/` 是本地运行时目录，默认不提交；`examples/skills/` 用来保存可复用模板。

## 安全与持久化

`pico` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

除了审批模式，`run_shell` 还会在执行前拦截明显危险的命令，例如递归强删、`git reset --hard`、强制 `git clean`、`curl | sh`、全局可写 chmod、直接写块设备等。这层检查发生在审批之前，所以即使是 `--approval auto` 也不会放行这些命令。

工具系统还会做更细的执行约束：

- 每个工具都有结构化 schema，会校验必填字段、类型、长度和数值范围。
- 每个工具都有 capability：`read`、`write`、`execute` 或 `delegate`。
- `delegate` 和 `delegate_many` 共用同一个调度器：最多并发 3 个只读子 agent、单批最多预留 12 个子步骤，并为每个任务返回成功、失败、超时或预算耗尽状态。
- 只读运行环境会拒绝非 `read` capability。
- shell 命令会做白名单分类；`pytest`、`python -m pytest`、`ruff check`、`python -m compileall` 以及对应的 `uv run ...` 形式会标记为 allowlisted。
- 非白名单 shell 命令在 `--approval never` 下会被拒绝；在 `ask/auto` 下会继续走审批/执行链路，但会写入审计字段。
- 写入类工具禁止修改内部或敏感路径，例如 `.pico/`、`.git/`、`.venv/`、`.env`。
- `--dry-run` 会正常执行读取类工具，但对写文件、patch、shell 这类高风险工具只返回“would ...”结果，不实际修改工作区。
- `run_shell` 只在临时 Docker 容器内执行，默认关闭网络、使用只读 RootFS、移除 Linux capabilities，并限制 CPU、内存和进程数。
- 容器以当前宿主用户的 UID/GID 运行；`.git` 只读挂载，`.pico`、`.venv` 使用临时文件系统，`.env*` 在容器内被 `/dev/null` 遮蔽。
- 命令超时后会强制删除容器；Docker 服务或预构建镜像不可用时任务明确失败，不会退回宿主机执行。

### 威胁模型与边界

这里的“安全”指有明确边界的本地执行 containment，而不是对任意恶意代码的形式化隔离保证：

- Docker 边界用于限制 shell 对宿主机、网络、容器 RootFS、Linux capabilities 和资源的访问；
  workspace 本身会以可写方式挂载，因为 coding agent 的目标就是在审批后修改它。
- 文件工具会阻止路径逃逸和敏感路径写入；它们不能替代版本控制、备份或人工 code review。
- `read_only` 子 agent 只表示没有 workspace 写入、执行和继续委派能力；它仍会在 `.pico/` 写入本次
  run 的内部审计工件。
- 自定义模型端点能够看到发送给模型的 prompt、选中的仓库上下文和工具结果。端点信任、数据保留
  和服务端安全不属于本地 Docker 沙箱的保护范围。
- 当前实现面向本地单用户进程，不提供多租户身份隔离、远程执行池、集中式 secret manager 或
  Docker daemon 的额外防护。

因此，未知仓库中的测试应像其他本地代码一样谨慎执行；重要工作区应在 Git 下运行并在合并前审查
workspace diff。更完整的资产、信任边界和非目标见
[Security model](docs/security-model.md)。

每次运行结束后，都会在 `.pico/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`
- `task_graph.mmd`
- `tool_outputs/*.txt`

此外 `.pico/runs/index.json` 会维护跨 run 的轻量索引，记录每次任务的目标、状态、更新时间、`task_graph.mmd` 路径和 `report.json` 路径。

`report.json` 里除了最终状态，还包含 `summary`、`tool_audit` 和 `task_graph_path`：

- `summary`：任务、停止原因、模型轮次、工具步数、改动文件、失败工具、安全事件。
- `tool_audit`：每次工具调用的名称、状态、错误码、capability、审批结果、dry-run 状态、shell 白名单结果、耗时、影响文件、结果预览；shell 调用会记录命令预览。
- `task_graph_path`：本次任务 Mermaid 任务图路径。

工具完整输出不会长期塞进 prompt。`pico` 会把完整工具结果写进 `tool_outputs/`，history 只保留摘要、`node_id` 和 `content_ref`。如果后续需要回看某个任务图节点对应的原始输出，可以用只读工具：

```xml
<tool>{"name":"read_tool_output","args":{"run_id":"run_20260407","node_id":"t001_run_shell"}}</tool>
```

不传 `run_id` 时，`read_tool_output` 默认读取当前 run。工具会校验 `ref` 必须位于对应 run 的 `tool_outputs/` 目录下，避免任务图引用逃逸到其他路径。

shell 执行安全链路可以按这条线理解：

```text
schema 校验
  -> 危险命令硬拦截
  -> shell 白名单分类
  -> capability / read-only 检查
  -> dry-run 或 approval policy
  -> 过滤环境变量
  -> Docker 隔离 + 网络/资源/权限约束
  -> workspace diff
  -> trace/report 审计
```

这些内容默认只保存在本地，不需要跟仓库一起提交。

`pico` 的长期记忆现在按四类结构化存储：`user`、`feedback`、`project`、`reference`。每条长期记忆都写入 `.pico/memory/entries/<type>.md`，`MEMORY.md` 只保留索引，供模型快速查看可用条目。相关记忆进入 prompt 时会带上时间感知提示，过旧的条目会标记为“需要先验证”。一次任务成功返回 final answer 后，runtime 会再跑一次独立的 LLM 提取器，把用户原话和最终回答里的长期信号整理成候选，再交给本地规则做拒绝、去重和落盘；secret、临时任务状态、代码位置和工具输出类内容会被拒绝。

### Agent Memory 链路

长任务和续接任务使用一条分层回溯链：

```text
tool_outputs/*.txt
  -> history summary + content_ref
  -> task_graph.mmd node_id/ref
  -> .pico/runs/index.json
  -> Recent runs prompt hint
  -> read_file(task_graph.mmd)
  -> read_tool_output(node_id)
```

当用户说“继续刚才那个 bug / 上次任务 / previous run”这类续接请求时，`ContextManager` 会把最近 run 的索引注入 `Relevant memory` 下的 `Recent runs:`。模型先用 `read_file` 读取 `task_graph.mmd`，再按图里的 `node_id` 调 `read_tool_output` 回读原始工具输出。

## 架构速读

一次任务的主链路是：

```text
用户输入
  -> pico.cli 构造 Pico runtime
  -> Pico.ask() 创建 task_state 和 run 目录
  -> ContextManager 组装 prompt
  -> model_client.complete_action() 返回统一 ModelAction
  -> Responses function_call / function_call_output 组成结构化工具循环
  -> Pico.run_tool() 校验、审批、执行工具
  -> 工具额度耗尽时进入一次 final-only 收尾状态
  -> session / task_state / trace / report 落盘
```

核心模块分工：

- `pico/cli.py`：命令行参数、REPL、模型后端选择。
- `pico/runtime.py`：agent 对象、工具护栏与运行能力编排。
- `pico/agent_loop.py`：单次请求的模型—工具循环、停止状态和落盘生命周期。
- `pico/actions.py`：统一的 `tool / final / retry` Action 类型和文本后端归一化边界。
- `pico/tools.py`：工具白名单、参数校验、文件与 shell 操作。
- `pico/delegate_scheduler.py`：只读子 agent 的并发调度、总步骤预算和 outcome 核算。
- `pico/sandbox.py`：强制 Docker Shell 沙箱、资源限制、超时回收和审计元数据。
- `pico/context_manager.py`：prompt 分区组装和上下文预算分配。
- `pico/context_history.py`：历史摘要、任务图压缩和 transcript 渲染。
- `pico/context_types.py`：token 估算、语义截断和 section 数据结构。
- `pico/memory.py`：短期工作记忆和可持久化记忆。
- `pico/run_store.py`：单次运行工件落盘。
- `pico/task_state.py`：一次 `ask()` 的状态机快照。
- `evaluation/real_benchmark.py`：真实模型工程任务、隐藏验收、对照和报告生成。

## 可视化报告

可以把单次运行的 `report.json`、`trace.jsonl` 和 `task_state.json` 渲染成静态 HTML：

```bash
uv run python scripts/render_run_report.py .pico/runs/<run_id>
```

输出会写到：

```text
.pico/runs/<run_id>/report.html
```

也可以批量渲染所有运行，并生成索引页：

```bash
uv run python scripts/render_run_report.py .pico/runs --all
```

生成的页面包含任务结果、工具调用时间线、Mermaid 任务图原文、安全事件、prompt/token 指标和完整 trace 展开项。

## 真实模型仓库微基准与回归评测

pico 的 Agent 效果评测强制调用真实模型 API，不提供 FakeModelClient 或离线 benchmark 模式。
这些任务是可复现的仓库级微基准和回归集，不等同于 SWE-bench、真实生产流量或通用 coding 能力评测。
真实模型结果分为原始文本协议基线、
[结构化 Action V1](docs/metrics/real-world-benchmark-v1-structured.md)、
[同快照对比](docs/metrics/structured-action-comparison.md)和
[V2 held-out](docs/metrics/real-world-benchmark-v2-heldout.md)。

### 已发布结果

V1 包含 10 个相互独立的仓库快照，覆盖 4 个 bugfix、2 个 feature、2 个测试补强、
1 个文档任务和 1 个重构任务。每轮都复制全新工作区；Agent 停止后才注入隐藏测试，并在
无网络的强制 Docker 沙箱中验收。报告记录 pass rate、失败分类、工具步数、模型调用、token
和总耗时。

评测 artifact v2 还记录完整 `evaluation_snapshot_id`（覆盖 prompt、fixture、任务配置和隐藏
verifier）、Git dirty 状态、Python/平台版本和运行参数。多次运行会报告整套任务的均值、标准差、
最好/最差结果，以及逐任务的稳定性。完整口径见
[Real-model evaluation methodology](docs/metrics/evaluation-methodology.md)。

早期同一模型、同一组 task ID、同一 fixture snapshot 的两次运行观察结果如下：

| 协议 | Pass rate | 平均模型调用 | Action 拒绝 |
|---|---:|---:|---:|
| 文本/XML 基线 | 60% (6/10) | 9.50 | 基线未单独记录 |
| strict structured actions | 90% (9/10) | 6.40 | 0 |

这组旧 artifact 没有记录完整 runtime snapshot 与 working-tree dirty 状态，因此不能证明协议是唯一
变量，也不能把差异解释为严格因果提升。它保留为历史工程观察；当前可复现性更强的主证据是下面
干净 commit 上冻结并运行三轮的 V3。

为了避免只对 V1 调优，结构化协议还在预先固定的 5 个独立 V2 held-out 仓库任务上运行，结果为
100% (5/5)，平均 6.20 次模型调用，Action 拒绝为 0。该结果只有一次真实模型重复，应理解为
带快照和模型约束的工程证据，不是通用 coding benchmark 结论。

V2 在首次 held-out 运行后已经用于 trace 分析和 runtime 开发，因此后续 V2 运行只作为回归，
不再作为新的独立 held-out 证据。未来若发布新的泛化结果，需要使用开发过程中未查看的新任务集；
不能通过反复运行 V2 把回归成绩包装成新的 held-out 提升。

[V3 frozen suite](benchmarks/real_world_tasks_v3.json) 包含 5 个全新实现任务。它的 prompt、fixture
和隐藏 verifier 在首次真实模型运行前提交为 `0897195`，运行前工作区干净，期间未据此调整
runtime。首次 3 轮结果为 13/15（86.7%）：4 题稳定通过 3/3，依赖排序题通过 1/3；15 次运行
artifact 记录了 0 次模型失败和 0 次 Action 拒绝。两次失败产出的实现都使用了不能满足全局
ready 顺序的 DFS，失败输出与实现缺陷一致；旧 artifact 没有汇总全部 tool status，因此这里不把
“所有工具调用成功”作为可由发布 JSON 独立验证的结论。见
[完整报告](docs/metrics/real-world-benchmark-v3-first-3x.md)和
[原始 JSON artifact](artifacts/real-world-benchmark-v3-first-3x.json)。V3 在这次运行后也只作为回归集。

一次后续实验在系统提示中加入了通用的“为交互约束编写区分性测试”要求，但 V3 回归降至
12/15，依赖排序题为 0/3，平均模型调用和耗时也上升；没有观察到收益，因此该提示修改已回滚。
这不是新的 held-out 结果。见[回归报告](docs/metrics/real-world-benchmark-v3-constraint-regression-3x.md)
和[原始 JSON artifact](artifacts/real-world-benchmark-v3-constraint-regression-3x.json)。

```bash
uv run python scripts/run_real_world_benchmark.py \
  --variant full \
  --benchmark-path benchmarks/real_world_tasks_v2.json \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v2-3x.json \
  --report-path docs/metrics/real-world-benchmark-v2-3x.md
```

该命令与 `pico` 一样，只读取项目根目录 `.env.local` 中的 `OPENAI_API_BASE`、
`OPENAI_API_KEY` 和 `OPENAI_MODEL`。`--model` 与 `--base-url` 只用于一次性显式覆盖。

并发委派另有一个非 held-out 的在线回归任务，既检查代码验收，也要求 trace 中恰好一次
`delegate_many` 请求两个子任务，并将结构化终态与两个真实 child run 的 agent id 交叉核对：

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_world_tasks_delegate.json \
  --task delegate_many_normalize_label \
  --artifact-path artifacts/delegate-live-regression.json \
  --report-path docs/metrics/delegate-live-regression.md
```

真实接口 smoke 默认跳过；模型配置仍只读取 `.env.local`，可用一次性开关运行：

```bash
PICO_RUN_LIVE_TESTS=1 uv run pytest -m live tests/test_live_delegate_smoke.py -q
```

使用 `scripts/compare_real_world_benchmarks.py BASELINE CANDIDATE` 可生成同快照协议对比；比较器
会拒绝 provider、model、任务集合或完整评测快照不一致的输入。建议先加
`--task url_query_before_fragment --repetitions 1` 做低成本 smoke run。仓库只提交经过检查的
JSON/Markdown 指标，工作区副本继续被 `.gitignore` 忽略；API Key 只留在未提交的 `.env.local`，
不会被复制到 artifact。
重复运行同一批任务并不等于增加了独立样本，报告中的
标准差只描述固定任务集上的运行波动。

## 开发

提交前运行完整回归和静态检查：

```bash
uv run pytest -q
uv run ruff check .
```

预先构建沙箱镜像后，可额外验证真实 Docker 网络、RootFS、secret masking、资源限制和超时边界：

```bash
docker build -f Dockerfile.sandbox -t pico-sandbox:latest .
PICO_RUN_DOCKER_TESTS=1 uv run pytest -q tests/test_sandbox.py
```

GitHub Actions 使用已提交的 `uv.lock`，在 Python 3.10、3.11、3.12 上运行离线测试；独立 job
构建并 smoke-test wheel，另一个 job 构建镜像并执行上述 Docker integration suite。真实 LLM
smoke 仍保持显式 opt-in，避免普通 CI 隐式消耗远程额度。
