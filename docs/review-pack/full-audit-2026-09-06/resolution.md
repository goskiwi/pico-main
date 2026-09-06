# F01–F16 修复验收

本文件记录最终计划的实施结果。report.md 和 results.jsonl 保留修复前的审查证据。旧诊断脚本已清理，正式验收保留在 tests/。

## 完整映射

| 问题 | 状态 | 实现结果 | 主要回归测试 |
|---|---|---|---|
| F01 | 已修复 | 拒绝跨源请求；同源流程保留 | [test_cross_origin_redirect_does_not_receive_credentials_or_body](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_provider_deadline.py:51) |
| F02 | 已修复 | 非完成态、缺失终止事件及矛盾终态不产生动作 | [test_nonterminal_responses_cannot_produce_actions](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_provider_tool_calls.py:223) |
| F03 | 已修复 | 机器事实保持精确，展示继续脱敏，非法投影不落盘 | [test_redaction_preserves_machine_paths_and_replay](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_tool_runtime.py:1072) |
| F04 | 已修复 | 事务前像与版本一致，提交点继续检查 revision | [test_preimage_and_commit_revision_cannot_disagree](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_tool_runtime.py:1115) |
| F05 | 已修复 | 不可读 Git 文件使观察明确失败 | [test_git_snapshot_rejects_unreadable_untracked_content](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_verification.py:241) |
| F06 | 已修复 | 恢复只关联当前工具事务 | [test_reused_call_id_is_scoped_to_the_pending_transaction](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_resume_runtime.py:608) |
| F07 | 已修复 | 单调用、分组调用、工具表面共享请求级预算 | [test_resumed_request_receives_a_new_tool_budget](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_resume_runtime.py:623) |
| F08 | 已修复 | latest 跳过并清理陈旧终态指针 | [test_resume_latest_skips_stale_terminal_pointer](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_cli.py:55) |
| F09 | 已修复 | 完整 WorkingState 保留；总窗口不足明确报错 | [test_complete_working_state_is_visible_after_resume](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_context_manager.py:26) |
| F10 | 已修复 | 迭代 AST 遍历，索引可降级 | [test_deep_syntax_does_not_overflow_the_python_stack](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_repo_map.py:36) |
| F11 | 已修复 | 删除 Python 回退，rg 搜索覆盖后续文件 | [test_rg_search_handles_later_files_and_backtracking_patterns](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_tool_runtime.py:1139) |
| F12 | 已修复 | 搜索消耗剩余时间和取消信号，不运行 Python 回溯正则 | [test_search_consumes_request_deadline_and_cancellation](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_tool_runtime.py:1154) |
| F13 | 已修复 | 分页至少 4 字节，非末页成功时必须前进 | [test_utf8_artifact_pages_advance_or_reject_small_capacity](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_tool_runtime.py:1171) |
| F14 | 已修复 | 组合、冲突、顺序委派、前后中断及后续修改的恢复统一处理 | [test_combined_child_delivery_and_recovery_use_transaction_state](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_subagents.py:757) |
| F15 | 已修复 | rename/copy 两端纳入用户脏路径保护 | [test_staged_rename_source_is_protected_from_automatic_delivery](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_coding_application.py:33) |
| F16 | 已修复 | 所有 CLI 入口使用统一工作区根 | [test_run_commands_resolve_the_repository_root_from_subdirectories](/Users/yankai/Documents/Course/Agents/pico-main/tests/test_cli.py:72) |

## 整体验证

- F01–F16 修复完成后的原始全量测试为 305 passed、5 skipped；后续精简非面试评测并完成
  per-tool 并发调度重构后，当前回归为 301 passed。
- Ruff、compileall、git diff --check 通过。
- 七个学习演示均实际执行通过；Day 3 的模拟响应已使用明确 completed 状态。
- 单源 Provider 重定向、临时 Git 合并、文件修改和验证命令均经过实际执行或针对性的失败注入。
- 相对审查时 11,332 行，当前 50 个生产 Python 文件为 11,316 行，净变化 -16 行；测试与文档另计。

## 对外行为与保留的边界

- search 需要系统 ripgrep；缺少 rg 返回 search_unavailable。Python 正则回退及相关旧接口已删除。
- read_artifact 的 max_bytes 最小为 4；成功的非末页会推进 offset。
- Child 可以基于父 Run 已接纳的改动继续委派与组合。准备输入使用私有工作树提交，父 HEAD/index 不变。
- 仍拒绝未知副作用、未归属外部修改、冲突和非法日志；没有为旧非法数据增加兼容迁移。
- 单 Run Log 仍采用单写者模型，宿主验证器仍只用于可信仓库；没有新增通用调度/状态机框架。
- 原有 Git 忽略文件交付、失败工作树保留、原子产物发布和有界命令清理测试继续保留。

本轮未提交 Git commit，也未发布或部署。
