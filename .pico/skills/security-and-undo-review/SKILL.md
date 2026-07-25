---
name: security-and-undo-review
description: Review Pico changes affecting tool permissions, sandbox isolation, secrets, workspace paths, or all-or-nothing Run Undo recovery. 中文: 安全审查, 沙箱, 密钥, 路径逃逸, 审批, Undo, 恢复.
when_to_use: Use for security reviews or changes to tools.py, tool_policy.py, sandbox.py, security.py, run_undo.py, or workspace boundaries.
trigger_keywords: [security review, sandbox, approval, secret, path traversal, undo, recovery, 安全审查, 沙箱, 审批, 密钥, 路径逃逸, 恢复]
tools: [list_files, read_file, search, query_repo_map, read_task_canvas, read_task_event, read_tool_output]
allowed_tools_strict: true
priority: 100
conflicts_with: [debugging, test-driven-development, runtime-invariants]
---

# Security and Undo review

This is a read-only security review. Never modify code or execute workspace
commands while assessing the change.

Check these invariants:

1. A skill, model response, or provider schema cannot grant a capability that
   Pico's local tool policy denies.
2. Paths remain anchored under the workspace after normalization and symlink
   resolution; protected runtime and environment paths stay unavailable.
3. Shell execution remains constrained by the allowlist, approval policy,
   network-disabled Docker sandbox, resource limits, and redacted artifacts.
4. Secrets cannot enter prompt metadata, traces, reports, or saved tool output.
5. Run Undo records first-touch preimages, validates every affected path before
   writing, and refuses the whole restore when any conflict exists.

Report findings by severity with exact file and line references. If no issue is
found, state the remaining verification gap instead of claiming absolute
security.
