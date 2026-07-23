# pico

**一个可审计、沙箱化的本地 Coding Agent Runtime。**

[`v0.1.0`](https://github.com/goskiwi/pico-main/tree/v0.1.0) ·
[架构](docs/architecture/agent-harness-v1-overview.md) ·
[安全模型](docs/security-model.md) ·
[评测证据](docs/metrics/README.md) ·
[审阅入口](docs/review-pack/README.md)

`pico` 不是把 LLM API 包一层 CLI：它把模型动作收敛为有界的 `tool / final / retry` 状态转换，
经过本地 schema、capability、审批和 Docker 隔离后执行，并把 task state、trace、workspace diff、
工具审计和最终报告完整落盘。

> 项目边界：本地单用户实验型 runtime，不是多租户生产平台；仓库微基准用于工程回归，不代表
> 通用 coding 能力。

## 核心工程工作

- **结构化控制流**：OpenAI-compatible 主路径使用 strict function calling；工具额度与结束协议
  分离，额度耗尽后只能进入一次 final-only 收尾，不能借机继续修改工作区。
- **强制执行边界**：文件工具限制敏感路径；`run_shell` 只能进入无网络、只读 RootFS、移除
  capabilities 且限制 CPU、内存和 PIDs 的 Docker 容器，没有宿主机回退。
- **可恢复、可审计**：session、checkpoint、任务图、完整工具输出和逐事件 trace 分层存储，既能
  续接长任务，也能解释一次任务为什么成功或失败。
- **真实模型验证闭环**：冻结 fixture 与隐藏 verifier，记录完整评测快照、Git 状态、token、时延、
  delegate 证据和失败分类；失败实验同样保留并说明回滚原因。

## 当前证据

| 证据 | 结果与边界 |
|---|---|
| `v0.1.0` 工程回归 | 252 passed，4 个 opt-in 测试默认跳过；Docker integration 9/9 |
| 最新已发布 LLM 微基准 | commit `0897195` 三轮通过 13/15，4/5 任务稳定 3/3；**不是当前 tag 的完整复测** |
| Shell containment | Docker-only、无网络、只读 RootFS、capability drop、CPU/内存/PID 限制 |
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

如果只有 10–15 分钟，请直接按 [review pack](docs/review-pack/README.md) 的顺序阅读主链路、安全
边界和评测证据。V1/V2 与旧协议对比保留在[历史证据索引](docs/metrics/archive/README.md)，不作为
当前能力结论。

## 适用场景

- 在本地仓库中排查和修复测试失败
- 基于现有代码完成小步功能修改或重构
- 用受限工具链执行代码阅读、测试和验证
- 通过持久化会话与运行工件续接长任务

## 界面与示例

当前 CLI 参数：

![pico help](assets/screenshots/pico-help.png)

匿名工作区中的启动界面：

![pico start](assets/screenshots/pico-start.png)

脱敏的示例交互；路径、session id 和回答均做了展示性归一化：

![pico repl](assets/screenshots/pico-repl.png)

## 5 分钟快速体验

需要 Python 3.10+、[`uv`](https://docs.astral.sh/uv/) 和 Docker。模型端点必须支持
OpenAI-compatible Responses API。

> PyPI 上的 [`pico`](https://pypi.org/project/pico/) 是另一个项目；本项目目前不发布 PyPI 包，
> 请从源码安装，不要执行 `pip install pico`。

先准备 runtime 和固定沙箱镜像：

```bash
git clone https://github.com/goskiwi/pico-main.git
cd pico-main
uv sync --locked
docker build -f Dockerfile.sandbox -t pico-sandbox:latest .
```

在准备让 `pico` 工作的目标仓库中创建 `.env.local`：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-5.4
# 使用第三方兼容端点时再设置：
# OPENAI_API_BASE=https://your-api.example/v1
```

回到 `pico` 仓库，执行一个 one-shot 任务：

```bash
uv run pico --cwd /path/to/target-repo \
  "inspect the failing tests, patch the smallest safe fix, and verify it"
```

运行结束后可直接检查审计工件：

```text
/path/to/target-repo/.pico/runs/<run_id>/
├── task_state.json
├── trace.jsonl
├── task_graph.mmd
├── report.json
└── tool_outputs/
```

交互模式与模块入口：

```bash
uv run pico --cwd /path/to/target-repo
uv run python -m pico --cwd /path/to/target-repo
```

### 配置与交付边界

不设置 `OPENAI_API_BASE` 时使用 OpenAI 官方端点；第三方兼容端点必须显式配置。自定义
端点会接收发送给模型的 prompt 和工具结果，使用前应确认其数据处理与密钥策略。

除显式传入的 `--model` 和 `--base-url` 外，启动时 `pico` 只读取 `--cwd` 目录下的该文件作为
模型配置来源，不会使用终端、CI 或 secret manager 中已有的同名环境变量，也不会把文件中的
任意键批量注入全局进程环境。`.env.local` 位于工作区但默认被 Git 忽略，不要提交密钥。

密钥通过显式配置映射传给模型客户端；`pico` 不会把它复制到 session、trace、report 或 benchmark
artifact，也不会传入 Docker shell 沙箱。

`run_shell` 使用 `--pull=never`，不会自动下载镜像；默认上限为 4 CPU、4 GB 内存和 512 个进程，
可通过 sandbox 参数调整。`uv build` 生成的 wheel/sdist 只包含 `pico` runtime；评测、测试、
Dockerfile 和证据文档属于完整源码仓库。需要可编辑安装时可运行 `pip install -e .`。

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

工具完整输出不会长期塞进 prompt。`pico` 会把完整工具结果写进 `tool_outputs/`，history 只保留摘要、`node_id` 和 `content_ref`。如果后续需要回看某个任务图节点对应的原始输出，模型会发起结构化的只读工具调用。下面是参数示意，不是用户在 REPL 中输入的命令：

```json
{
  "name": "read_tool_output",
  "args": {
    "run_id": "run_20260407",
    "node_id": "t001_run_shell"
  }
}
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
- `evaluation/real_benchmark.py`：真实模型任务清单、模型客户端和 benchmark runner。
- `evaluation/real_benchmark_evidence.py`：trace、delegate 证据和工作区隔离审计。
- `evaluation/real_benchmark_reporting.py`：指标汇总、报告渲染与 artifact 对比。

`evaluation/` 是源码仓库的验证资产，不属于本地构建的 runtime wheel/sdist。

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
所有已发布结果、状态和归档关系见 [Metrics evidence map](docs/metrics/README.md)，完整口径见
[Real-model evaluation methodology](docs/metrics/evaluation-methodology.md)。

### 最新已发布评测证据：冻结 V3（commit `0897195`）

[V3 frozen suite](benchmarks/real_world_tasks_v3.json) 包含 5 个全新实现任务。它的 prompt、fixture
和隐藏 verifier 在首次真实模型运行前提交为 `0897195`，运行前工作区干净，期间未据此调整
runtime。首次 3 轮结果为 13/15（86.7%）：4 题稳定通过 3/3，依赖排序题通过 1/3；15 次运行
artifact 记录了 0 次模型失败和 0 次 Action 拒绝。两次失败产出的实现都使用了不能满足全局
ready 顺序的 DFS，失败输出与实现缺陷一致；旧 artifact 没有汇总全部 tool status，因此这里不把
“所有工具调用成功”作为可由发布 JSON 独立验证的结论。见
[完整报告](docs/metrics/real-world-benchmark-v3-first-3x.md)和
[原始 JSON artifact](artifacts/real-world-benchmark-v3-first-3x.json)。V3 在这次运行后也只作为回归集；
该结果没有覆盖 `v0.1.0` 或当前 `master` 的完整 3× 复测。

一次后续实验在系统提示中加入了通用的“为交互约束编写区分性测试”要求，但 V3 回归降至
12/15，依赖排序题为 0/3，平均模型调用和耗时也上升；没有观察到收益，因此该提示修改已回滚。
这不是新的 held-out 结果。见[回归报告](docs/metrics/real-world-benchmark-v3-constraint-regression-3x.md)
和[原始 JSON artifact](artifacts/real-world-benchmark-v3-constraint-regression-3x.json)。

### Historical / archive evidence

早期 V1 文本/XML 与 structured actions 两次运行曾观察到 6/10 与 9/10；V2 首次 held-out 为
5/5。但旧 schema 没有锁定完整 runtime snapshot 和 working-tree dirty 状态，且 V2 后来已进入
开发反馈，因此这些结果只保留为工程演进记录，不能解释为严格因果 A/B 或当前泛化能力。原报告、
artifact 和适用边界均未删除，统一从 [Historical metrics archive](docs/metrics/archive/README.md)
进入。

### 复现与回归入口

```bash
uv run python scripts/run_real_world_benchmark.py \
  --variant full \
  --benchmark-path benchmarks/real_world_tasks_v3.json \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v3-rerun-3x.json \
  --report-path docs/metrics/real-world-benchmark-v3-rerun-3x.md
```

benchmark runner 只读取 `pico` 仓库根目录 `.env.local` 中的 `OPENAI_API_BASE`、
`OPENAI_API_KEY` 和 `OPENAI_MODEL`；这与 CLI 读取 `--cwd/.env.local` 不同。复现前需在 `pico`
仓库中单独准备该文件，`--model` 与 `--base-url` 只用于一次性显式覆盖。上述输出名故意使用
`rerun`，避免覆盖仓库中的首次运行证据；重复 V3 是回归，不会重新获得 held-out 身份。

首次检查环境时，建议先运行一个低成本 smoke：

```bash
uv run python scripts/run_real_world_benchmark.py \
  --variant full \
  --benchmark-path benchmarks/real_world_tasks_v2.json \
  --task url_query_before_fragment \
  --repetitions 1 \
  --artifact-path /tmp/pico-real-world-smoke.json \
  --report-path /tmp/pico-real-world-smoke.md
```

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
会拒绝 provider、model、任务集合或完整评测快照不一致的输入。仓库只提交经过检查的 JSON/Markdown
指标，工作区副本继续被 `.gitignore` 忽略；API Key 只留在未提交的 `.env.local`，不会被复制到
artifact。
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
