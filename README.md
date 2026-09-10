# Pico

Pico 是一个便于阅读和讲解的本地 Coding Agent Runtime。模型负责决策，Runtime 负责工具权限、执行、持久化、恢复和最终验证。

```text
Session → Context → Model → ToolRuntime → Session
                     │
                     ├─ verify → VerificationService → test feedback
                     └─ final → Completion → VerificationService → result
```

当前实现采用单进程、单 Session 写入者模型。完整对话追加到 `messages.jsonl`，当前执行状态原子保存到 `session.json`；SessionStore 管理两者的提交位置，不使用旧版 RunLog/Projection。

模型编辑只提交路径、原文、替换内容；Runtime 内部保存已观察版本并在写入时复验，不接受模型传入 `expected_revision` 或读取凭据。配置验收命令后，模型可以随时调用无参数 `verify` 获取反馈，不必先宣布完成；最终提交仍独立验收。普通开发可在 `code` 模式审批后通过 `run_shell` 运行仓库测试。`auto` 或受限写范围不开放任意命令，固定验收不是任意命令的绕过入口。没有配置独立验收时，完成结果明确说明该限制。

## 快速开始

需要 Python 3.10+、uv 和 ripgrep。

```bash
uv sync
uv run pico --cwd /path/to/trusted/repo --mode ask "解释入口"
uv run pico --cwd /path/to/trusted/repo "修复计价错误"
uv run pico --cwd /path/to/trusted/repo --resume latest "继续任务"
```

模型配置：

```dotenv
PICO_OPENAI_API_KEY=your-key
PICO_OPENAI_API_BASE=https://example.com/v1
PICO_OPENAI_MODEL=gpt-5.4
```

CLI 只保留 `--cwd`、`--resume`、`--mode`、`--model`、`--trace` 和帮助入口。项目级记忆始终开启；Runtime 自动识别项目验收，不要求用户提供命令。内部预算和上下文参数统一由 `PicoConfig` 管理，不接受旧 CLI 参数。模型服务温度与请求超时可通过 `PICO_OPENAI_TEMPERATURE`（默认 0.2）和 `PICO_OPENAI_TIMEOUT`（默认 300 秒）设置。脱敏由 `security.py` 统一处理；特殊环境变量名可通过 `PICO_SECRET_ENV_NAMES` 补充，不进入 Runtime 配置。写入权限检查和命令审批不变。

## 一次任务

配置职责：`pico/env.py` 只负责加载 `.env` 和环境变量；`pico/config.py` 定义并校验唯一的运行配置 `PicoConfig`。

1. `Pico.ask()` 创建本轮预算并把用户消息写入 Session。
2. `ContextManager` 组装最新用户请求、历史摘要、近期完整交互、长期记忆、仓库规则和 Workspace 状态。
3. 模型通过 OpenAI-compatible Responses function calling 返回工具调用或 `submit_final`。
4. `ToolRuntime` 校验 Schema、路径、写范围、读取版本和审批，然后持久化 `pending/running/finished` 阶段。
5. 文件修改保存前像与收据，使用读取时 Revision 和原子替换提交。
6. 模型提交最终回答后，`CompletionController` 处理未知副作用、执行固定验证并复查文件状态。
7. 完整历史始终留在 Transcript，最终文件工具 Diff 保存为 Artifact；完成后可更新项目级长期记忆。

## 核心模块

建议按这个顺序阅读：

1. `pico/runtime.py`：依赖组装。
2. `pico/agent_loop.py`：唯一任务循环。
3. `pico/session.py`：持久状态和中断恢复。
4. `pico/tool_runtime.py`：工具准入、阶段和结果。
5. `pico/tools.py`：具体工具 Schema 与 Runner。
6. `pico/context_manager.py` 与 `pico/compaction.py`：上下文和增量摘要。
7. `pico/completion.py`：验证与最终完成权。

`verification.py`、`mutations.py` 和 Provider 是相对独立的底层实现，第一次阅读主链时可以跳过内部算法。

## 工具与模式

| 工具 | Ask | Code | Auto |
|---|:---:|:---:|:---:|
| `list_files`、`read_file`、`read_artifact`、`search` | ✓ | ✓ | ✓ |
| `run_shell` | — | 审批 | — |
| `verify`（检测到项目验收时） | — | 审批 | 自动 |
| `write_file`、`edit_file` | — | 审批 | 自动 |
| `delegate` | — | ✓ | ✓ |
| `submit_final` | ✓ | ✓ | ✓ |

`run_shell` 只用于可信工作区中的诊断，不是沙箱。Auto 模式不暴露通用命令执行。Code 模式中的拒绝只作用于当前工具调用；相同操作再次提交时会重新询问，不保存隐式拒绝规则。

## 只读委派与工具顺序

`delegate(task)` 使用独立 Session 只读分析工作区，继承父任务截止时间和取消信号，内部最多执行 4 个模型轮次，不能修改文件或继续委派。不提供 Implement Child、Worktree 或 Patch Integration，也不兼容这些旧工具参数。

模型返回的工具调用严格按顺序执行。每个调用都在开始前把 `running` 状态保存到 Session，完成后再保存结果；读取、修改和命令共享同一条执行与 Trace 生命周期。

## 持久化与恢复

```text
workspace/.pico/
├── memory.json
└── sessions/<session-id>/
    ├── session.json
    ├── messages.jsonl
    ├── trace.jsonl
    ├── artifacts/
    │   ├── tool_<id>.txt      # 大工具结果
    │   └── diff_<id>.txt      # 最终文件差异
    └── delegates/
```

完成的对话记录按行追加到 `messages.jsonl`，工具调用与结果作为一个完整批次保存。尚未完成的批次及其执行阶段保存在 `session.json`，不会被当作已完成记录。每次先同步 Transcript，再原子提交快照中的字节位置和记录数；恢复只采用快照已提交的部分，截去未提交尾部，结合收据检查未完成写操作。

接近上下文预算时，先裁剪旧的成功读取结果正文，必要时更新滚动摘要并保留近期完整交互。最新用户请求保留原文。压缩不会删除原始对话；模型可用已有 `read_file/search` 回查当前会话的 Transcript 文件。只开放这个文件的读取与搜索，其他内部文件仍禁止访问，写工具禁止修改 `.pico`。读取历史不会清除当前工作区的未知副作用。

工具调用先保存为 `pending`，执行前保存为 `running`，结果落盘后保存为 `finished`。恢复时：

- `pending` 记为未执行；
- 只读 `running` 记为中断；
- 修改操作结合前像、收据和当前文件 Revision 判断没有修改、已经修改或结果未知；
- 旧操作不会自动重放。

恢复可以收窄写范围，不能扩大原任务已经保存的写范围；已经产生修改或未知副作用的任务仍要求验证。

## 边界

- Pico 只面向用户信任的本地仓库，不提供容器或系统级沙箱。
- 文件 Revision 处理读取后被其他进程修改的场景，不是跨进程事务。
- 验证通过只说明固定命令在被观察的文件状态上通过，不证明所有外部系统副作用。
- 旧快照与归档格式不兼容，会明确拒绝加载；旧文件保留，不自动迁移。
- 摘要可能有损，历史回读不保证模型自动发现错误。Transcript 和内存历史随会话增长；当前不支持多个进程同时写同一 Session。
- 只读 Delegate 是同步、有界能力；运行中的子任务不跨进程续跑。

旧测试套件未恢复。此次审查修复的定向验证入口为 `scripts/check_runtime_boundaries.py`，真实模型验证入口为 `scripts/validate_live.py`。后者需通过环境变量提供模型地址和凭证，每次使用新输出目录保留原始结果。
