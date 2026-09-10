# Snapshot Runtime 审查修复与验证

后续使用用户提供的新接口完成真实模型验证，并追加修复原生消息结构和完成工具权限清单问题。最终五个场景通过，详见 [新接口验证记录](live-validation-2btocken.md)。下文保留原接口失败的历史记录。

对照旧分支 `codex/interview-ready-runtime` 的 `9d07b59` 修复。本报告只描述当前修复，不沿用旧版测试成绩。

## 已修复

- 主模型每轮发送当前 Session 组装的输入；不再在旧 Provider 输入上续接。预算按同一请求编码估算。
- 摘要先形成候选，确认整包变小且可装入窗口后才保存摘要与覆盖位置；失败保留原快照。
- Session 恢复区分观察路径和修改路径；收据使用历史位置和调用 ID 关联，写前保存预期 after_revision。每次 ask 从持久快照恢复。
- 修改后读取实际文件版本；已跟踪文件发生外部变化时，完成前要求重新读取。失败读取不清除未知修改，读取一个文件不清除其他文件的记录。
- partial 副作用同样要求验证；验证保存后复查仓库和文件状态。
- 审批返回后重新校验工具权限、写范围和目标路径；持久拒绝在 Auto 模式下仍有效。
- 新访问路径的目录规则先进入上下文，再让模型重新决定，原调用组不执行。
- 取消直接传播，只读取消不产生未知写入记录。
- 恢复有界只读并行，Runner 在工作线程运行，Session/Artifact 结果在主线程提交。
- 移除 Implement Child、Worktree 和 Patch Integration；只保留 `delegate(task, max_turns)`，不兼容旧 Child 参数或工厂入口。
- 主请求、摘要、记忆共享用量观测；失败或发生缺少用量的重试时标记不完整。Delegate 用量单独记录。

## 本地验证

`uv run python scripts/check_runtime_boundaries.py`：19 项通过。覆盖修改前后恢复、同进程续接、权限撤销、拒绝缓存、partial 验证、写后漂移、失败读取、多路径未知状态、取消、并行、目录规则、实际请求输入、压缩提交、验证后漂移及用量统计。

`ruff check` 覆盖生产代码、应用和两个新验证脚本，检查通过；`git diff --check` 通过。

旧测试套件没有恢复。这些是针对本次修复的检查，不代表穷尽了所有运行场景。

## 真实模型验证

使用用户提供的地址 `https://www.rightapi.ai/codex/v1` 和凭证，模型沿用 `gpt-5.6-luna`。凭证仅通过进程环境传入，未写入代码或报告。

| 场景 | 实际结果 |
| --- | --- |
| CLI 只读任务，首次 | CLI 装配成功；3 次传输尝试后连接被重置；没有模型响应或工具调用 |
| 修改与固定验收 | HTTP 503：Pricing configuration is temporarily unavailable；没有模型响应或修改。原始带缺陷样例的独立验收失败 |
| CLI 只读任务，最终复测 | 同一 HTTP 503，http_attempts=3、model_responses=0、usage_complete=false |

真实模型端到端验收尚未通过。没有将无模型响应当成运行成功；没有切换其他模型或服务以替代结果。真实压缩、崩溃后模型续接、只读委派尚未运行，服务恢复后可用 `scripts/validate_live.py` 中对应场景继续。

原始结果保留在 `/tmp/pico-live-fixed-r6BDJ9/`，分别为 `cli_read/result.json`、`repair/result.json`、`cli_read_final/result.json`；各目录包含 Session 和 Trace。临时目录可能被系统清理。
