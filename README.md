# Pico

Pico 是一个轻量、本地、单协议的 Coding Agent Runtime。模型只负责通过
OpenAI-compatible Responses 原生 function calling 提出下一步动作；上下文、权限、
工具执行、恢复、验证和持久化均由 Runtime 持有。

项目主动不做多 Provider、XML 工具协议、Skills、MCP、子 Agent 和旧状态兼容。

## Runtime 主链路

```text
User request
  -> Context Ledger v4
  -> RepoMap + Working/Project Memory
  -> Token-budgeted prompt projection
  -> Responses function_call -> ModelAction
  -> Registry / Surface / Schema / Policy / Approval
  -> Docker tool execution or revision-bound atomic mutation
  -> ToolOutcome + Evidence + optional Policy Hook
  -> hash-chained Runtime Event v2 / transactional compaction / Checkpoint v6
  -> structured runtime verification -> Completion Gate
```

核心实现：

- 原生 function calling：Pydantic 参数模型生成 strict function schema；Responses output items 与匹配的 function output 在任务内连续回放，一次响应只接受一个函数调用，最终回答也通过 `submit_final`。
- 长上下文治理：任务内即时证据由 Responses 连续会话保留，append-only Context Ledger 是 reset/resume 的持久化事实源；重建 Prompt 时按 section floor、权重和共享余量分配 Token，压缩只覆盖完整工具批次，并校验 generation、active digest 和 Workspace 指纹后事务提交。
- RepoMap：基于 tree-sitter 构建 Python symbol/reference graph，以 lexical + personalized PageRank 在 Token 预算内返回任务相关签名。
- 分层记忆：Session Working Memory 保存目标、最近文件和 revision-bound 文件观察；Project Memory 以 Markdown card 为唯一事实源，生成 `MEMORY.md` 索引并保留 provenance、版本、过期时间及显式记忆优先级。
- 安全工具：路径锚定 Workspace；重复调用检测；写入必须携带 `read_file` 返回的 SHA-256 revision，并通过 fsync + atomic replace 提交。
- 大输出：模型直接获得一次 12 KiB 预览（shell 为尾部 16 KiB）；完整、脱敏输出写入不可变 artifact，截断结果可通过当前 run 限定的 `read_artifact` 按 8 KiB 字节页继续读取。
- 隔离执行：`run_shell` 强制进入临时 Docker 容器；inspect/verify Profile 均禁网、只读 rootfs 与 Workspace，并施加 cap-drop、进程/CPU/内存/输出限制和整轮 deadline。
- 事件溯源：运行事件以 strict schema、连续 sequence、因果/关联 ID 和 SHA-256 hash chain 写入单一 `events.jsonl`，首次打开完整校验，之后在锁内增量验证尾部并 flush + fsync；Task/Evidence/Policy/Report 均可由事件投影。
- 策略扩展：核心循环默认持续到模型提交最终答案、deadline、取消或错误；不内置“若干步未编辑”等任务猜测。宿主可显式传入 `before_tool_call`、`after_tool_result` 和 `should_stop_after_turn` hook，hook 只能进一步限制或提供指导，不能改写工具事实或绕过安全校验。
- 恢复：Checkpoint v6 校验 schema、Runtime 配置、hook 身份、provider conversation mode、内容级 Workspace 指纹、Context Ledger 及事件 cursor/digest；中断操作只核对 receipt，绝不重放潜在副作用。
- 完成证据：观察、修改和结构化 verifier 结果写入 Evidence Ledger；变更 Python 先做 AST 校验，Workspace 变更需通过绑定当前内容指纹的 Runtime verifier，未解决 partial/unknown 状态禁止成功结束。

## 安装与运行

需要 Python 3.10+、uv；使用 `run_shell` 还需要 Docker。

```bash
uv sync
docker build -f docker/sandbox.Dockerfile -t pico/sandbox:latest .
uv run pico --cwd /path/to/repo
uv run pico "inspect the failing test and implement a verified fix"
```

最小模型配置：

```dotenv
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_API_BASE="https://api.openai.com/v1"
PICO_OPENAI_MODEL="gpt-5.4"
```

常用运行参数：

```bash
uv run pico --approval ask
uv run pico --run-timeout 600
uv run pico --max-steps 40
uv run pico --provider-context-limit 64000
uv run pico --verify-command "python -m pytest -q"
uv run pico --resume latest
uv run pico events stats <run_id> --cwd /path/to/repo
uv run pico events replay <run_id> --cwd /path/to/repo
```

`--verify-command ""` 可显式关闭自动 verifier。若未提供，Python/Node 项目会按仓库文件自动选择默认命令。

## 状态与工件

```text
.pico/
  sessions/<session_id>.json
  memory/
    MEMORY.md
    cards/<type>_<name>.md
  runs/<run_id>/
    task_state.json
    context.jsonl
    events.jsonl
    report.json
    artifacts/*
```

所有 schema 都严格校验；当前版本不会迁移旧 XML、Durable JSONL、旧 Checkpoint 或旧 Session。

## 评测

```bash
uv run python scripts/run_evaluations.py
uv run pytest -q
uv run ruff check pico tests scripts
```

评测分为 native Harness regression、Context governance、revision-bound Working Memory、
Markdown Project Memory 和 RepoMap。它们衡量 Runtime 机制，不冒充真实模型能力指标。

当前随仓库发布的证据：

| 层级 | 结果 | 说明 |
|---|---:|---|
| Python tests | 全部通过 | Runtime contracts、恢复、安全、上下文与工具边界 |
| Native Harness | 5/5 | edit、recovery、safety、governance；失败时脚本非零退出 |
| Runtime policy | 全部通过 | 单一恢复建议、hook 边界、结构化 verifier 与事件重放 |
| Five-repository fixture preflight v2 | 5/5 | 每题均 fail-before/pass-after；绑定官方修复提交、fixture/verifier/patch digest 与 Docker image ID |
| Five-repository Real OSS suite v2 | 5/5 | clean commit `6ef7463`；统一 40 步；五题 Context Ledger 均无宿主路径泄露 |

旧的 10/12/14 步差异化结果已删除。Real OSS v2 使用统一 40 工具步预算；没有任务失败后的选择性重跑。Werkzeug 使用了预先固定的第 2 次 provider 基础设施尝试，其余四题均为第 1 次尝试。完整结果见 `artifacts/real-oss-suite-v2.{json,md}`。

五仓库冻结任务集可以从精确上游 commit 重新物化并运行：

```bash
uv run python scripts/materialize_real_oss.py --replace
docker build -f docker/real-oss-suite.Dockerfile -t pico/real-oss-suite:latest .
uv run python scripts/run_real_oss_suite.py --model gpt-5.6-luna
```

Real OSS runner 强制要求 clean worktree，并把 Runtime snapshot、manifest/verifier/reference-patch digest、fixture tree digest、Docker image ID 和统一工具预算写入证据。Preflight 必须证明每个 hidden verifier 在 pre-fix fixture 上失败，并在应用绑定上游修复提交的 reference patch 后通过。完整五题模型运行不复用旧结果，也不选择性重跑任务失败。

面试展示顺序见 [`docs/review-pack/interview-demo.md`](docs/review-pack/interview-demo.md)。

项目定位是本地单用户 Agent Runtime，不是模型网关、多租户平台或通用工作流引擎。
