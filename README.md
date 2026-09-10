# Pico

Pico 是一个便于阅读和讲解的本地 Coding Agent Runtime。模型负责决策，Runtime 负责工具权限、执行、持久化、恢复和最终验证。

```text
Session → Context → Model → ToolRuntime → Session
                     │
                     ├─ verify → VerificationService → test feedback
                     └─ final → Completion → VerificationService → result
```

当前实现采用单进程、单 Session 写入者模型。Session 原子快照是唯一可恢复状态，不使用 Event Log、Projection 或第二套任务状态。

模型编辑只提交路径、原文、替换内容；Runtime 内部保存已观察版本并在写入时复验，不接受模型传入 `expected_revision` 或读取凭据。配置验收命令后，模型可以随时调用无参数 `verify` 获取反馈，不必先宣布完成；最终提交仍独立验收。普通开发可在 `code` 模式审批后通过 `run_shell` 运行仓库测试。`auto` 或受限写范围不开放任意命令，固定验收不是任意命令的绕过入口。没有配置独立验收时，完成结果明确说明该限制。

## 快速开始

需要 Python 3.10+、uv 和 ripgrep。

```bash
uv sync
uv run pico --cwd /path/to/trusted/repo --mode ask "解释入口"
uv run pico --cwd /path/to/trusted/repo --mode auto \
  --verify-command 'python -m pytest -q' "修复计价错误"
uv run pico --cwd /path/to/trusted/repo --resume latest "继续任务"
```

模型配置：

```dotenv
PICO_OPENAI_API_KEY=your-key
PICO_OPENAI_API_BASE=https://example.com/v1
PICO_OPENAI_MODEL=gpt-5.4
```

## 一次任务

1. `Pico.ask()` 创建本轮预算并把用户消息写入 Session。
2. `ContextManager` 组装当前请求、历史摘要、近期完整交互、长期记忆、仓库规则、Workspace 状态。
3. 模型通过 OpenAI-compatible Responses function calling 返回工具调用或 `submit_final`。
4. `ToolRuntime` 校验 Schema、路径、写范围、读取版本和审批，然后持久化 `pending/running/finished` 阶段。
5. 文件修改保存前像与收据，使用读取时 Revision 和原子替换提交。
6. 模型提交最终回答后，`CompletionController` 处理未知副作用、执行固定验证并复查文件状态。
7. 最终文件工具 Diff 保存为 Artifact；完成后可更新项目级长期记忆。

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
| `write_file`、`edit_file` | — | 审批 | 自动 |
| `delegate` | — | ✓ | ✓ |
| `submit_final` | ✓ | ✓ | ✓ |

`run_shell` 只用于可信工作区中的诊断，不是沙箱。Auto 模式不暴露通用命令执行。

## 只读委派与并行

`delegate(task, max_turns)` 使用独立 Session 只读分析工作区，继承父任务截止时间和取消信号，不能修改文件或继续委派。不提供 Implement Child、Worktree 或 Patch Integration，也不兼容这些旧工具参数。

连续的只读调用按 `--max-parallel-tools` 并行执行（默认 4）；修改和命令独占执行。Session、Artifact 和结果记账均由主线程处理。

## 持久化与恢复

```text
workspace/.pico/
├── memory.json
└── sessions/<session-id>/
    ├── session.json
    ├── trace.jsonl
    ├── artifacts/
    └── delegates/
```

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
- 旧 Event Log 版本的 Session 不兼容当前快照格式，会明确拒绝加载。
- 只读 Delegate 是同步、有界能力；运行中的子任务不跨进程续跑。

旧测试套件未恢复。此次审查修复的定向验证入口为 `scripts/check_runtime_boundaries.py`，真实模型验证入口为 `scripts/validate_live.py`。后者需通过环境变量提供模型地址和凭证，每次使用新输出目录保留原始结果。
