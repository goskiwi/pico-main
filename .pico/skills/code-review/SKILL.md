---
name: code-review
description: Review PRs, diffs, patches, and code changes for bugs, regressions, safety issues, and missing tests. 中文: 代码审查, 审查, review, 检查改动, PR.
trigger_keywords: [code review, review, 审查, 检查改动, PR, diff, patch]
tools: [list_files, read_file, search, query_repo_map, read_task_canvas, read_task_event, read_tool_output, run_shell]
allowed_tools_strict: true
priority: 100
conflicts_with: [debugging, test-driven-development, runtime-invariants]
---

# Code Review

Review the change as production code. Prioritize concrete defects over style.

Use this to report findings. Do not edit code unless the user explicitly asks for fixes.

This is a read-only review profile. Do not call `write_file`, `patch_file`, or
delegate work to a child agent.

Check:
- behavior changes and edge cases
- security or safety regressions
- missing or weak tests
- unclear ownership boundaries
- failure modes that are not surfaced to users

Report findings with file and line references when possible. If no issue is found, say so and mention any remaining test gaps.
