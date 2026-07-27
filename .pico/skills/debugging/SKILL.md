---
name: debugging
description: Debug and investigate failing tests, crashes, errors, exceptions, regressions, bugs, or unexpected behavior before changing code. 中文: 调试, 定位问题, 测试失败, 报错, 异常.
when_to_use: A concrete failure has been observed and needs reproduction, localization, and a minimal fix.
when_not_to_use: Routine feature work, a read-only review, or a request to explain code with no observed failure.
---

# Debugging

Understand the failure before editing code.

Use this for broken behavior. Do not use it for routine implementation when no failure has been observed.

Workflow:

1. Record the smallest reproducer, its expected result, and the observed failure.
2. Read the failure output, then inspect only the code path and recent change that
   can explain it.
3. State one falsifiable hypothesis. Confirm or reject it with one targeted check
   before changing code.
4. Make the smallest root-cause fix; do not bundle cleanup or speculative
   refactors into it.
5. Rerun the original reproducer and the relevant verifier. Add a focused
   regression test when the failure was not already covered.

Do not claim the issue is fixed without a passing command or explicitly state
the remaining evidence gap. Avoid broad rewrites until the failure is explained.
