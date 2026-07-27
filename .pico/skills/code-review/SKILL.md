---
name: code-review
description: Review PRs, diffs, patches, and code changes for bugs, regressions, safety issues, and missing tests. 中文: 代码审查, 审查, review, 检查改动, PR.
when_to_use: Review a completed change, diff, PR, patch, or bug fix before it is accepted.
when_not_to_use: Implementing a change, diagnosing an unexplained failure, or answering a general code question.
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

Workflow:

1. Establish intent from the task, change evidence, and tests before judging the
   implementation.
2. Review the changed behavior on five axes: correctness and edge cases;
   readability and unnecessary complexity; architecture and ownership;
   security and safety; performance and boundedness.
3. Treat a passing test suite as evidence, not a verdict. Check whether tests
   would catch the regression and whether failure paths remain visible.
4. Report only concrete findings, ordered by severity, with file and line
   references plus the consequence. Separate an evidence gap from a confirmed
   defect.

Do not approve a changed task merely because an earlier verifier passed: a
later write or shell action can make that result stale. If no issue is found,
say so and name remaining test or verification gaps.
