---
name: code-review
description: Review PRs, diffs, patches, and code changes for bugs, regressions, safety issues, and missing tests. 中文: 代码审查, 审查, review, 检查改动, PR.
---

# Code Review

Review the change as production code. Prioritize concrete defects over style.

Use this to report findings. Do not edit code unless the user explicitly asks for fixes.

Check:
- behavior changes and edge cases
- security or safety regressions
- missing or weak tests
- unclear ownership boundaries
- failure modes that are not surfaced to users

Report findings with file and line references when possible. If no issue is found, say so and mention any remaining test gaps.
